"""Qt-free Creative controller: transport + protocol -> a simple imperative API.

Ported from ``plasma_creative_x4/device.py``. Two changes, both because this serves a registry of
devices rather than one card:

* ``SoundBlasterX4`` becomes :class:`CreativeController` and takes the product id and serial of
  the device the shell matched, instead of finding the X4 by hardcoded id.
* :meth:`sync` skips reads the device says it does not implement, rather than issuing all of them
  and letting the unsupported ones time out. On the X4 that costs nothing -- it supports what it
  is asked -- but a SXFI AIR reports a very different ``SubFeature`` mask, and eleven equaliser
  reads at 0.5 s each is six seconds of nothing on a device with no equaliser.

Everything else, including both acknowledge landmines, is the source's logic unchanged.

    x4 = CreativeController()
    x4.connect()            # opens, unlocks, initial sync
    state = x4.state
    x4.set_feature(Feature.DIRECT_MODE, False)
    for _ in x4.drain(): ...     # consume pushed state

The device is **push-driven**: after a write it volunteers an Acknowledge, the
changed state, and any dependent state (toggling Direct Mode pushes all 12 EQ
parameters). So there is no polling — `drain()` just collects what already
arrived. This mirrors Creative's own app, which registers a notification
callback and has no polling timers.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field

from .protocol import catalogue as cat
from .protocol import framing
from .protocol import presets as presets_mod
from .protocol.ids import (
    DANGEROUS,
    EQ_BAND_COUNT,
    Cmd,
    Feature,
    FeatureOp,
    Module,
    OutputTarget,
    Playback,
    ProfileType,
    SubFeature,
)
from .transport import UsbCdcTransport

log = logging.getLogger(__name__)

#: How long to wait for the Acknowledge that confirms a write.
ACK_TIMEOUT_S = 1.2

#: How long to keep applying pushed state after a write. The card commits in about half a second
#: and reports afterwards, so anything shorter reads back the value from before the write.
SETTLE_S = 0.7

#: How long to keep asking whether a routing change has landed.
#:
#: The card is reliable and slow, not flaky: left alone for four seconds it reported the requested
#: output three times out of three in both directions. What varies is *when* -- 200 ms on one
#: attempt, 1.4 s on the next -- so every "the output reverted to Headphones" report was this
#: application reading inside that window, never the device changing its mind.
CONFIRM_S = 3.0


class DeviceError(Exception):
    pass


class SafetyError(Exception):
    """Raised when a command is refused by the safety policy."""


@dataclass
class DeviceState:
    firmware: str = ""
    serial: str = ""
    max_payload: int | None = None
    subfeatures: SubFeature | None = None
    feature_state: int = 0          # FeatureControl bitfield
    feature_support: int = 0
    output: int | None = None       # OutputTarget mask
    eq_enabled: bool | None = None
    eq_preamp: float | None = None
    eq_bands: list[float] = field(default_factory=lambda: [0.0] * EQ_BAND_COUNT)
    eq_preset: str | None = None   # last preset we applied, if any
    sxfi_enabled: bool | None = None
    sxfi_mode: str | None = None
    #: Which stored profile the card says is live, or None if it has not said. See `_apply_one`.
    profile: int | None = None
    volume_db: float | None = None
    muted: bool | None = None

    def feature(self, bit: Feature) -> bool:
        return bool(self.feature_state & bit.mask)

    def supports(self, bit: Feature) -> bool:
        # A unit that reports no support mask at all is treated as permissive:
        # better to show a control that might not apply than to hide a working one.
        return not self.feature_support or bool(self.feature_support & bit.mask)

    @property
    def eq_available(self) -> bool:
        """The graphic EQ is bypassed while Direct Mode is on.

        Observed on the hardware and confirmed by the user. Note this interlock
        is *not* encoded anywhere in the Creative source — the Equalizer module
        never references Direct Mode — so it is our own rule, justified by Direct
        Mode being a DSP bypass.
        """
        return not self.feature(Feature.DIRECT_MODE)


class CreativeController:
    def __init__(self, product_id: int | None = None, serial: str = "", *,
                 allow_dangerous: bool = False) -> None:
        self.state = DeviceState()
        self.allow_dangerous = allow_dangerous
        self._t = UsbCdcTransport(product_id=product_id, serial=serial)
        self._presets: dict[str, presets_mod.EqPreset] | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._t.connected

    @property
    def unlocked(self) -> bool:
        return self._t.unlocked

    def connect(self) -> DeviceState:
        self._t.open()
        self._t.unlock()
        return self.sync()

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> CreativeController:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- request / response ------------------------------------------------

    def _send(self, cmd: int, payload: bytes) -> None:
        if cmd in DANGEROUS and not self.allow_dangerous:
            raise SafetyError(
                f"command {cmd} can reflash or factory-reset the device; "
                "construct with allow_dangerous=True to permit it")
        self._t.write_raw(framing.build(cmd, payload))

    def request(self, cmd: int, payload: bytes = b"",
                timeout_s: float = 1.0) -> bytes | None:
        """Send a query and return the payload of the matching reply.

        Any frames that arrive meanwhile are still applied to `state`, so nothing
        the device volunteers is lost.
        """
        self.apply(self._t.drain(20))       # clear stale pushes first
        self._send(cmd, payload)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            frames = self._t.drain(120)
            if not frames:
                continue
            self.apply(frames)
            for fcmd, fpayload in frames:
                if fcmd == cmd:
                    return fpayload
        return None

    def command(self, cmd: int, payload: bytes,
                timeout_s: float = ACK_TIMEOUT_S) -> bool:
        """Send a state-changing command and confirm it via the Acknowledge.

        The ACK echoes `<cmd> <status> <op> <feature> <value>`, so we can check
        it really is *our* command that was accepted, and that status == 0.
        Creative's own `RawSetValue` is fire-and-forget and ignores this; using
        it costs one read and turns silent failures into errors.

        Verification deliberately does *not* re-read the value: the device takes
        ~0.5 s to commit, so an immediate read returns the old one. The pushed
        state frame that follows the ACK is the authority, and `apply()` picks
        it up.
        """
        self.apply(self._t.drain(20))
        self._send(cmd, payload)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            frames = self._t.drain(120)
            if not frames:
                continue
            self.apply(frames)
            for fcmd, fpayload in frames:
                if fcmd != Cmd.ACKNOWLEDGE or not fpayload:
                    continue
                if fpayload[0] != cmd:
                    continue                     # someone else's ack
                status = fpayload[1] if len(fpayload) > 1 else 0
                if status == 0:
                    return True
                # A failure is only ours if the echoed operation matches. One
                # Super X-Fi mode write draws two acknowledges — a failure for a
                # different op, then ours with status 0 — so a failure whose op
                # is not ours must be ignored, not raised.
                #
                # The op is byte 0 of the request for the command families that
                # have one (FeatureControl, SuperXFi, HardwareButton, the
                # profile commands). For SetMalcolmParameter byte 0 is an entry
                # count instead, which is why this check must never gate the
                # success path: doing so made every equaliser write time out.
                ours = (not payload or len(fpayload) <= 2
                        or fpayload[2] == payload[0])
                if ours:
                    raise DeviceError(
                        f"command {cmd} rejected, status={status} "
                        f"({fpayload.hex(' ')})")
        raise DeviceError(f"no acknowledge for command {cmd} within {timeout_s}s")

    # -- push handling -----------------------------------------------------

    def settle(self, seconds: float = SETTLE_S) -> None:
        """Apply what the card volunteers in the moment after a write.

        The acknowledge is not the end of a write. The card commits in about half a second and
        *then* pushes the new state, so :meth:`command` -- which returns as soon as the
        acknowledge arrives -- is always a little ahead of the device:

            -> 00 01 00 00 00       set the output to Line Out
            <- 2c 00 ...            acknowledge, and `command` returns
            <- 01 01 00 00 00       the new output, some milliseconds later

        Reading held state at that point returns the value from before the write, which is why
        choosing Speakers put the box back on Headphones while the card really had switched. It is
        not specific to the output: a change draws a burst re-reporting the equaliser, the active
        mode and Super X-Fi as well, so one settle keeps the whole page honest.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            frames = self._t.drain(120)
            if frames:
                self.apply(frames)

    def drain(self) -> list[tuple[int, bytes]]:
        """Collect and apply whatever the device has pushed. Sends nothing."""
        frames = self._t.drain(40)
        self.apply(frames)
        return frames

    def apply(self, frames: list[tuple[int, bytes]]) -> None:
        for cmd, payload in frames:
            try:
                self._apply_one(cmd, payload)
            except Exception:            # noqa: BLE001 — never let one bad frame kill the loop
                log.debug("could not apply cmd %s payload %s", cmd, payload.hex())

    def _apply_one(self, cmd: int, payload: bytes) -> None:
        st = self.state
        if cmd == Cmd.ACKNOWLEDGE and len(payload) >= 5:
            # The Acknowledge is not just a receipt — for most features it is the
            # ONLY feedback there is. Direct Mode is the exception: it pushes a
            # fresh FeatureControl state frame afterwards, which is why it alone
            # appeared to work while Headphone High Gain, SPDIF Passthrough and
            # Headphone Virtualization silently reverted in the UI.
            #
            # Layout: <echoed cmd> <status> <op> <feature bit> <value>
            echoed, status, op, feature, value = payload[:5]
            if status == 0 and echoed == Cmd.FEATURE_CONTROL and op == FeatureOp.SET:
                bit = 1 << feature
                st.feature_state = ((st.feature_state | bit) if value
                                    else (st.feature_state & ~bit))
            return
        if cmd == Cmd.FEATURE_CONTROL:
            mask = cat.parse_feature_state(payload)
            if mask is not None:
                st.feature_state = mask
                return
            sup = cat.parse_feature_support(payload)
            if sup is not None:
                st.feature_support = sup
        elif cmd == Cmd.SPEAKER_OUTPUT_TARGET:
            m = cat.parse_output(payload)
            if m:
                st.output = m
        elif cmd in (Cmd.GET_MALCOLM_PARAM, Cmd.SET_MALCOLM_PARAM):
            for module, param, value in framing.parse_effect(payload):
                if module != Module.PLAYBACK:
                    continue
                if param == Playback.GRAPHIC_EQ_ENABLE:
                    st.eq_enabled = value >= 0.5
                elif param == Playback.GRAPHIC_EQ_PREAMP:
                    st.eq_preamp = value
                elif 11 <= param <= 20:
                    st.eq_bands[param - 11] = value
        elif cmd == Cmd.DEVICE_INFO_V2 and payload:
            text = framing.parse_string(payload)
            if payload[0] == 2:
                st.firmware = text
            elif payload[0] == 3:
                st.serial = text
        elif cmd == Cmd.MAX_PAYLOAD_SIZE and len(payload) >= 2:
            st.max_payload = struct.unpack_from("<H", payload, 0)[0]
        elif cmd == Cmd.SUBFEATURE_SUPPORT and len(payload) >= 4:
            st.subfeatures = SubFeature(struct.unpack_from("<I", payload, 0)[0])
        elif cmd == Cmd.AUDIO_LEVEL and len(payload) >= 4:
            # <domain> then N * (int16 LE level in 1/256 dB, channel)
            st.volume_db = struct.unpack_from("<h", payload, 1)[0] / 256.0
        elif cmd == Cmd.AUDIO_MUTE and len(payload) >= 2:
            st.muted = bool(payload[1])
        elif cmd == Cmd.HARDWARE_BUTTON and len(payload) >= 4:
            # The device answers a button write (op 7) with a **different** op, and that is why
            # Super X-Fi read as unknown for so long. Captured from a Sound Blaster X4:
            #
            #     -> 07 1e 00 01 00        set button 30 (Super X-Fi) on
            #     <- 08 ff ff 01 00 ...    op 8, no button id, the new value at byte 3
            #
            # The source project matches op 7 and button 30 on the way back, which never fires:
            # nothing arrives with op 7. So the state stayed None however often it was switched,
            # the box sprang back to unticked, and Super X-Fi Mode -- gated on it -- was dead.
            #
            # `ff ff` where the button id sits is read as "all buttons" rather than parsed: only
            # one button on this card reports at all, and inventing a meaning for it would be
            # guessing. Op 7 is still accepted in case another model echoes its own write.
            from .protocol.ids import ButtonID
            if payload[0] == 8 or payload[0] == 7 and payload[1] == ButtonID.SXFI_ON_OFF:
                st.sxfi_enabled = bool(payload[3])
        elif cmd == Cmd.ACTIVE_MALCOLM_PROFILE and len(payload) >= 2:
            # Pushed after anything that changes which stored profile is live, including a Super
            # X-Fi toggle: `02 00` is profile type 2 (DEVICE) and index 0. The card *can* be asked
            # which mode it is in after all -- it volunteers it -- so the mode selector no longer
            # has to report the last value this application chose.
            if payload[0] == int(ProfileType.DEVICE):
                st.profile = payload[1]
        elif cmd == Cmd.SUPER_XFI and len(payload) >= 5 and payload[0] == 0x0D:
            # Again a different op on the way back: 0x0C sets the mode, 0x0D reports it, followed
            # by a firmware version string. Matching 0x0C left the mode unknown for ever.
            st.sxfi_mode = payload[1:5].decode("latin1")
        elif cmd == Cmd.SUPER_XFI and len(payload) >= 5 and payload[0] == 0x0C:
            st.sxfi_mode = payload[1:5].decode("latin1")

    # -- initial sync ------------------------------------------------------

    def sync(self) -> DeviceState:
        """One-shot read of everything. Used on connect and by Refresh.

        Deliberately not periodic — Creative's app never polls, and the device
        pushes changes. A full re-read is a user action.
        """
        for builder in (cat.get_max_payload, cat.get_firmware, cat.get_serial,
                        cat.get_subfeatures, cat.get_feature_support,
                        cat.get_feature_state, cat.get_output):
            cmd, payload = builder()
            self.request(cmd, payload, timeout_s=0.8)
        # EQ: enable + preamp + the ten bands -- but only if the DSP says it has an equaliser.
        # `subfeatures` is the device's own answer to "what do you implement"; a unit that reports
        # a mask without GRAPHIC_EQ will let all twelve reads time out, which is six seconds of
        # nothing. A unit that reports no mask at all is read anyway, on the same permissive
        # principle as `DeviceState.supports`.
        self.sync_equalizer()
        return self.state

    def sync_equalizer(self) -> DeviceState:
        """Re-read enable, preamp and the ten bands. Part of :meth:`sync`, and usable alone.

        Alone after recalling one of the card's stored profiles: that replaces the whole curve
        **on the device**, so unlike a preset applied from here there is nothing in held state that
        knows the new gains, and the twelve sliders would go on showing the old ones.

        Skipped entirely when the DSP says it has no equaliser. ``subfeatures`` is the device's own
        answer to "what do you implement", and a unit reporting a mask without ``GRAPHIC_EQ`` lets
        all twelve reads time out -- six seconds of nothing. A unit that reports no mask at all is
        read anyway, on the same permissive principle as :meth:`DeviceState.supports`.
        """
        st = self.state
        if st.subfeatures is not None and SubFeature.GRAPHIC_EQ not in st.subfeatures:
            log.info("device reports no graphic equaliser (subfeatures %s); skipping EQ reads",
                     st.subfeatures)
            return st
        for param in (Playback.GRAPHIC_EQ_ENABLE, Playback.GRAPHIC_EQ_PREAMP, *range(11, 21)):
            cmd, payload = cat.get_eq_param(param)
            self.request(cmd, payload, timeout_s=0.5)
        return st

    # -- typed operations --------------------------------------------------

    def set_feature(self, feature: Feature, on: bool) -> None:
        self.command(*cat.set_feature(feature, on))

    def set_output(self, target: OutputTarget) -> None:
        """Set the routing, then **ask** what it is rather than waiting to be told.

        The card reports the change on its own, but not on a schedule anyone can rely on: a fixed
        settle caught it twice out of three times, and the miss put the box back on Headphones
        while the card really had switched to Line Out. Routing is one of the few things with a
        real getter, so the read costs one round trip and removes the race instead of shortening
        the odds on it.
        """
        self.command(*cat.set_output(target))

    def confirm_output(self, wanted: int, timeout_s: float = CONFIRM_S) -> bool:
        """Read the routing back until it is what was asked for, or the deadline passes.

        Measured on a Sound Blaster X4: the card takes 300-600 ms to commit routing, and a read
        inside that window returns the previous value -- the acknowledge is not the commit. So
        this is a poll rather than a single read, and a single read is what made choosing Speakers
        put the box straight back on Headphones. Once committed it stays: still Line Out at one
        second and at two.

        **Call it last**, after the post-write settle. Settling drains whatever is still queued,
        which can include a push describing the state *before* this write -- so confirming first
        and settling afterwards puts the stale value back, which is how this stayed intermittent
        through two attempts at fixing it.

        Returns whether it landed. False is not an error here: the caller shows what the card last
        reported, which is the honest thing whether or not it agreed.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            # Flush first. Anything still queued describes the state *before* this write, and
            # applying it after the read is what kept putting the old value back.
            self.settle(0.25)
            self.request(*cat.get_output(), timeout_s=0.5)
            if self.state.output == wanted:
                return True
        log.info("routing did not settle on 0x%02x within %.1fs (card reports %s)",
                 wanted, timeout_s, self.state.output)
        return False

    def set_sxfi(self, on: bool) -> None:
        self.command(*cat.set_sxfi(on))

    def set_sxfi_mode(self, code: str) -> None:
        self.command(*cat.set_sxfi_mode(code))

    def press_button(self, button: int, on: bool) -> None:
        """Set a hardware button. Only button 30 (Super X-Fi) is capture-verified.

        Scout Mode and SBX have IDs in the vendor enum but the Windows app never
        sends them: toggling Scout there emits only "Super X-Fi off" plus
        "graphic EQ off". Exposed for experimentation, not wired to the GUI.
        """
        self.command(*cat.set_button(button, on))

    # The device acknowledges equaliser writes but does NOT push the new values
    # back, so nothing would update the held state until the next full refresh —
    # which is why the sliders only moved after pressing Refresh. The ACK means
    # the device accepted the value, so record it locally.

    def set_eq_enabled(self, on: bool) -> None:
        self.command(*cat.set_eq_enabled(on))
        self.state.eq_enabled = on

    def set_eq_preamp(self, db: float) -> None:
        self.command(*cat.set_eq_preamp(db))
        self.state.eq_preamp = db

    def set_eq_band(self, index: int, db: float) -> None:
        self.command(*cat.set_eq_band(index, db))
        if 0 <= index < len(self.state.eq_bands):
            self.state.eq_bands[index] = db

    @property
    def presets(self) -> dict[str, presets_mod.EqPreset]:
        """All known EQ presets, loaded once and cached.

        Built-ins plus whatever `tools/import_vendor_data.py` has imported into
        the user's data directory.
        """
        if self._presets is None:
            self._presets = presets_mod.load()
        return self._presets

    def store_profile(self, name: str, bands=None, preamp: float | None = None,
                      index: int = 0) -> None:
        """Write a named curve into one of the card's own profile slots.

        This is what the Windows app does on every preset change: name the
        profile (cmd 24), then write preamp + all ten bands in a single frame
        (cmd 23) instead of eleven separate parameter writes. Storing it in a
        slot is what makes the curve survive without any software running.

        Defaults to the equaliser settings currently held in `state`.
        """
        bands = list(self.state.eq_bands) if bands is None else list(bands)
        if preamp is None:
            preamp = self.state.eq_preamp or 0.0
        self.command(*cat.set_profile_name(name))
        self.command(*cat.write_profile(bands, preamp, index=index))
        self.state.eq_preset = name

    def select_profile(self, index: int) -> None:
        """Make one of the device's stored profiles active (cmd 26)."""
        self.command(*cat.select_profile(index))

    def apply_eq_preset(self, name: str, output: int | None = None) -> None:
        """Write a preset band-by-band, exactly as the Windows app does.

        The vendor tunes Speaker and Headphone separately, so the curve written
        depends on where the card is currently routed. Pass `output` to override
        the device's reported routing (an OutputTarget mask).
        """
        preset = self.presets.get(name)
        if preset is None:
            raise DeviceError(f"unknown EQ preset: {name}")
        bands, preamp = preset.curve(self.state.output if output is None else output)
        for i, db in enumerate(bands):
            self.set_eq_band(i, db)
        self.set_eq_preamp(preamp)
        self.state.eq_preset = name
