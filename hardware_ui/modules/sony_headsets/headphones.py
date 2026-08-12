"""High-level, Qt-free headset controller.

Wires transport + session + messages into a simple imperative API:

    hp = Headphones(mac)
    hp.connect()
    info = hp.handshake()      # DeviceState with model/fw/capabilities/battery
    hp.set_ncasm(enabled=True, asm_level=10)
    for frame in hp.listen(): ...   # NTFY pushes

The Qt worker (workers.py) runs one of these on a background thread and turns
its results into signals. The CLI (cli.py) drives it directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .protocol import messages
from .protocol.enums import Command, PowerInquiredType
from .protocol.session import Session
from .transport import bluez
from .transport.rfcomm import RfcommTransport

log = logging.getLogger(__name__)


# Model-id (UPDT sub 0x03) -> friendly name. Extend as more devices are seen.
MODEL_ID_NAMES = {
    "MDRID294301": "WH-1000XM4",
    "MDRID291601": "WH-1000XM3",
}


@dataclass
class DeviceState:
    mac: str
    model_id: str = ""            # UPDT 0x03, e.g. "MDRID294301"
    serial: str = ""             # UPDT 0x06
    device_id: str = ""          # UPDT 0x0b
    version_fields: list[int] = field(default_factory=list)  # UPDT 0x07-0x0a
    identifiers: dict[int, str] = field(default_factory=dict)  # all UPDT fields, raw
    protocol_raw: bytes = b""    # RET_PROTOCOL_INFO payload
    supported_functions: set = field(default_factory=set)  # FunctionType codes
    battery: messages.BatteryStatus | None = None
    ncasm: messages.NcAsmState | None = None
    eq: messages.EqConfig | None = None
    stc: messages.StcState | None = None
    dsee: bool | None = None
    sound_quality: int | None = None     # 0=quality, 1=stable
    custom_button: int | None = None     # func code
    touch_panel: bool | None = None
    auto_pause: bool | None = None       # pause when removed
    multipoint: bool | None = None
    codec: str | None = None             # active A2DP codec (via BlueZ)
    gs_slots: dict = field(default_factory=dict)   # GS title -> slot byte
    apo_options: list = field(default_factory=list)  # supported APO element ids
    apo_current: int | None = None       # current APO element id

    @property
    def model_name(self) -> str:
        return MODEL_ID_NAMES.get(self.model_id, self.model_id or "Unknown")


class Headphones:
    def __init__(self, mac: str, channel: int | None = None) -> None:
        self.mac = mac
        self.state = DeviceState(mac=mac)
        self._transport = RfcommTransport(mac, channel)
        self._session: Session | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        self._transport.connect()
        self._session = Session(self._transport)

    def close(self) -> None:
        self._transport.close()
        self._session = None

    @property
    def connected(self) -> bool:
        return self._transport.connected

    # -- handshake / sync --------------------------------------------------

    # Commands confirmed present in the official-app HCI capture on this XM4.
    # We deliberately do NOT send GET_DEVICE_INFO (0x04), GET_SUPPORT_FUNCTION
    # (0x06) or POWER_GET_STATUS (0x22): the app never sent them and probing
    # them made the headset reset/power off. Device identity comes from the
    # UPDT group (0x36) instead.
    # UPDT (0x36) device-identity sub-indices confirmed present on the XM4.
    UPDT_FIELDS = (0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b)

    def export_discovery(self) -> dict:
        """The parts of the handshake that are constant for a given device and firmware.

        DIVERGENCE from the reference implementation -- see docs/PORT_DIVERGENCES.md. Deliberately
        excludes every *setting*: those can be changed from Sony's phone app between sessions and
        are always re-read by sync().
        """
        st = self.state
        return {
            "supported_functions": sorted(st.supported_functions),
            "model_id": st.model_id,
            "serial": st.serial,
            "device_id": st.device_id,
            "version_fields": list(st.version_fields),
            "protocol_raw": st.protocol_raw.hex(),
            "identifiers": {str(k): v for k, v in st.identifiers.items()},
            "gs_slots": {str(k): v for k, v in st.gs_slots.items()},
            "apo_options": list(st.apo_options),
        }

    def _restore_discovery(self, cached: dict) -> None:
        st = self.state
        st.model_id = cached.get("model_id", "")
        st.serial = cached.get("serial", "")
        st.device_id = cached.get("device_id", "")
        st.version_fields = list(cached.get("version_fields", []))
        st.protocol_raw = bytes.fromhex(cached.get("protocol_raw", ""))
        st.identifiers = {int(k): v for k, v in cached.get("identifiers", {}).items()}
        st.gs_slots = {k: v for k, v in cached.get("gs_slots", {}).items()}
        st.apo_options = list(cached.get("apo_options", []))

    def handshake(self, cached: dict | None = None) -> DeviceState:
        """Run the confirmed CONNECT sequence and an initial state sync.

        With *cached* supplied (from a previous ``export_discovery``), the identity reads and
        capability discovery are skipped **only if** the device's advertised function list still
        matches — about 14 of ~27 round-trips. Everything else is unchanged, and with no cache
        the sequence is byte-identical to the original.

        Never skipped: the three arming commands, because ALERT_SET_STATUS is what registers us
        for the confirm dialog on reboot-inducing settings and dropping it silently breaks them;
        and sync(), because those are live values.
        """
        assert self._session is not None, "connect() first"
        s = self._session
        s.send_command(messages.build_get_protocol_info())    # 00 00
        self._drain()
        s.send_command(messages.build_get_capability_info())  # 02 00 -> uniqueId (MAC)
        self._drain()
        # "Arm" the session exactly as the official app does at init. In
        # particular ALERT_SET_STATUS (0x94 01 00) registers us for FIXED_MESSAGE
        # confirm dialogs — WITHOUT it the device never raises the 0x99 alert for
        # reboot-inducing settings (CUSTOM button / multipoint / sound quality),
        # so those changes are silently dropped. (COMMON status/param enable the
        # rest of the notification stream.)
        for arm in (bytes([0x14, 0x00]),        # COMMON_SET_STATUS
                    bytes([0x18, 0x00]),        # COMMON_SET_PARAM
                    bytes([0x94, 0x01, 0x00])): # ALERT_SET_STATUS (FIXED_MESSAGE)
            s.send_command(arm)
            self._drain()
        # capability discovery — the device lists its supported FunctionTypes.
        # We then only query/expose those features (safe for older models).
        s.send_command(messages.build_get_support_function())  # 06 00 -> 07 ...
        self._drain()

        # The function list is the fingerprint: it is what changes across a firmware update, and
        # everything skipped below is derived from it or fixed for the life of the device.
        reusable = bool(cached) and sorted(self.state.supported_functions) == sorted(
            cached.get("supported_functions", [])
        )
        if reusable:
            log.debug("reusing cached discovery for %s", self.mac)
            self._restore_discovery(cached)
        else:
            self._discover_capabilities()   # GS slot titles + APO options
            for sub in self.UPDT_FIELDS:                      # 36 xx -> identity fields
                s.send_command(bytes([0x36, sub]))
                self._drain()

        self.sync()
        return self.state

    def _discover_capabilities(self) -> None:
        """Resolve device-specific layouts by capability (not hardcoded):
        which GENERAL_SETTING slot is touch/multipoint/etc, and which Auto Power
        Off options exist. Titles/options come straight from the device."""
        FT = messages.FT
        for slot in (FT.GENERAL_SETTING1, FT.GENERAL_SETTING2, FT.GENERAL_SETTING3):
            if slot in self.state.supported_functions:
                self._session.send_command(messages.build_get_gs_capability(slot))
                self._drain()
        if self.supports(FT.AUTO_POWER_OFF):
            self._session.send_command(messages.build_get_apo_capability())  # f0 04
            self._drain()

    def supports(self, *function_types: int) -> bool:
        """True if the device advertised any of these FunctionTypes (or if no
        support list was obtained — then we optimistically allow it)."""
        fns = self.state.supported_functions
        return not fns or any(ft in fns for ft in function_types)

    def sync(self) -> DeviceState:
        """Fetch current state — only for FunctionTypes the device supports, so
        we never poke a feature (and risk a reset) on models that lack it."""
        assert self._session is not None
        FT = messages.FT

        def get(payload):
            self._session.send_command(payload)
            self._drain()

        if self.supports(FT.NOISE_CANCELLING, FT.NC_AND_ASM, FT.AMBIENT_SOUND_MODE,
                         FT.AUTO_NC_ASM):
            get(messages.build_get_ncasm())                              # 66 02
        if self.supports(FT.PRESET_EQ, FT.EBB, FT.PRESET_EQ_NONCUSTOMIZABLE):
            get(messages.build_get_eq())                                 # 56 01
        if self.supports(FT.SMART_TALKING_MODE):
            get(messages.build_get_stc_param())                          # f6 05
            get(messages.build_get_stc_ext())                            # fa 05
        if self.supports(FT.UPSCALING):
            get(messages.build_get_dsee())                               # e6 02
        if self.supports(FT.CONNECTION_MODE):
            get(messages.build_get_sound_quality())                      # e6 01
        if self.state.apo_options:
            get(messages.build_get_apo())                                # f6 04
        if self.supports(FT.ASSIGNABLE_SETTINGS):
            get(messages.build_get_custom_button())                      # f6 06
        if self.supports(FT.CONTROL_BY_WEARING):
            get(messages.build_get_pause())                              # f6 03
        # GENERAL_SETTING slots resolved by title (device-specific)
        touch_slot = self.state.gs_slots.get(messages.GS_TITLE_TOUCH_PANEL)
        if touch_slot is not None:
            get(messages.build_get_gs(touch_slot))
        mp_slot = self.state.gs_slots.get(messages.GS_TITLE_MULTIPOINT)
        if mp_slot is not None:
            get(messages.build_get_gs(mp_slot))
        self.refresh_status()
        return self.state

    def refresh_status(self) -> DeviceState:
        """Cheap refresh of battery + codec via BlueZ only — NO MDR traffic.
        Used for periodic polling: on LDAC the SPP config channel has almost no
        spare bandwidth, so sending a burst of MDR GETs flaps the codec/link.
        The device pushes MDR state changes as NTFYs instead."""
        pct = bluez.battery_percentage(self.mac)
        if pct is not None:
            self.state.battery = messages.BatteryStatus(
                kind=PowerInquiredType.BATTERY, level=pct, charging=False)
        self.state.codec = bluez.active_codec(self.mac)
        return self.state

    def _reget(self, payload: bytes) -> None:
        """Targeted read-back of ONE feature right after writing it, so
        ``self.state`` reflects the *committed* value.

        This mirrors how Sony's own app updates a single control (event-driven,
        one feature at a time) rather than re-reading the whole device. Two
        reasons we re-read instead of trusting the SET's own echo NTFY:
          * the echo can carry the *pre-commit* value (the device acks before it
            commits), which is exactly what made a control snap back in the UI;
          * the extra GET's round-trip gives the device the moment it needs to
            commit before we read.
        It is a single GET — far less traffic than the old full ``sync()`` after
        every write (which is what flapped LDAC on the XM3)."""
        assert self._session is not None
        self._session.send_command(payload)
        self._drain()

    def set_eq_bands(self, bands: list[int]) -> None:
        assert self._session is not None
        self._session.send_command(messages.build_set_eq_bands(bands))
        self._drain()
        self._reget(messages.build_get_eq())

    def set_eq_preset(self, preset_id: int) -> None:
        assert self._session is not None
        self._session.send_command(messages.build_set_eq_preset(preset_id))
        self._drain()
        self._reget(messages.build_get_eq())

    def set_stc(self, enabled: bool, sensitivity: int, timeout: int) -> None:
        assert self._session is not None
        self._session.send_command(messages.build_set_stc_enable(enabled))
        self._drain()
        self._session.send_command(messages.build_set_stc_ext(sensitivity, timeout))
        self._drain()
        self._reget(messages.build_get_stc_param())
        self._reget(messages.build_get_stc_ext())

    def set_dsee(self, on: bool) -> None:
        assert self._session is not None
        self._session.send_command(messages.build_set_dsee(on))
        self._drain()
        self._reget(messages.build_get_dsee())

    def set_sound_quality(self, mode: int) -> None:
        assert self._session is not None
        self._session.send_reboot_command(messages.build_set_sound_quality(mode))
        self._drain()

    def set_auto_power_off(self, element: int) -> None:
        """Set the Auto Power Off element id (capability-driven; model-specific)."""
        assert self._session is not None
        self._session.send_command(messages.build_set_apo_element(element))
        self._drain()
        self._reget(messages.build_get_apo())

    def set_custom_button(self, preset: int) -> None:
        assert self._session is not None
        self._session.send_reboot_command(messages.build_set_custom_button(preset))
        self._drain()

    def set_touch_panel(self, on: bool) -> None:
        assert self._session is not None
        slot = self.state.gs_slots.get(messages.GS_TITLE_TOUCH_PANEL)
        if slot is None:
            return
        self._session.send_command(messages.build_set_gs(slot, on))
        self._drain()
        self._reget(messages.build_get_gs(slot))

    def set_auto_pause(self, on: bool) -> None:
        assert self._session is not None
        self._session.send_command(messages.build_set_pause(on))
        self._drain()
        self._reget(messages.build_get_pause())

    def set_multipoint(self, on: bool) -> None:
        assert self._session is not None
        slot = self.state.gs_slots.get(messages.GS_TITLE_MULTIPOINT)
        if slot is None:
            return
        self._session.send_reboot_command(messages.build_set_gs(slot, on))
        self._drain()

    # -- feature commands --------------------------------------------------

    def set_ncasm(self, *, enabled: bool, focus_on_voice: bool = False,
                  asm_level: int = 0) -> None:
        assert self._session is not None
        self._session.send_command(messages.build_set_ncasm(
            enabled=enabled, focus_on_voice=focus_on_voice, asm_level=asm_level))
        self._drain()
        self._reget(messages.build_get_ncasm())

    # -- inbound handling --------------------------------------------------

    def listen(self):
        """Yield DeviceState after applying any pushed NTFY frames (blocks)."""
        assert self._session is not None
        for frame in self._session.pump():
            self._apply(frame)
        yield self.state

    def _drain(self) -> None:
        assert self._session is not None
        # snapshot + clear first: _apply may itself send (alert-confirm), which
        # appends new frames to the inbox — those get processed on the next drain.
        frames = list(self._session.inbox)
        self._session.inbox.clear()
        for frame in frames:
            self._apply(frame)

    def _apply(self, frame) -> None:
        cmd = frame.command
        try:
            if cmd == Command.CONNECT_RET_PROTOCOL_INFO:
                self.state.protocol_raw = frame.payload
            elif cmd == Command.CONNECT_RET_SUPPORT_FUNCTION:
                self.state.supported_functions = messages.parse_support_function(frame.payload)
            elif cmd in (Command.POWER_RET_STATUS, Command.POWER_NTFY_STATUS):
                self.state.battery = messages.parse_battery(frame.payload)
            elif cmd in (Command.NCASM_RET_PARAM, Command.NCASM_NTFY_PARAM):
                self.state.ncasm = messages.parse_ncasm(frame.payload)
            elif cmd in (Command.EQEBB_RET_PARAM, Command.EQEBB_NTFY_PARAM):
                self.state.eq = messages.parse_eq(frame.payload)
            elif cmd in (Command.AUDIO_RET_PARAM, Command.AUDIO_NTFY_PARAM):
                t = frame.payload[1] if len(frame.payload) > 1 else -1
                if t == messages.DSEE_TYPE:
                    self.state.dsee = messages.parse_dsee(frame.payload)
                elif t == messages.SOUND_QUALITY_TYPE:
                    self.state.sound_quality = messages.parse_sound_quality(frame.payload)
            elif cmd == Command.SYSTEM_RET_CAPABILITY:
                if len(frame.payload) > 1 and frame.payload[1] == messages.APO_TYPE:
                    self.state.apo_options = messages.parse_apo_capability(frame.payload)
            elif cmd in (Command.SYSTEM_RET_PARAM, Command.SYSTEM_NTFY_PARAM):
                t = frame.payload[1] if len(frame.payload) > 1 else -1
                if t == messages.STC_TYPE:
                    self.state.stc = messages.parse_stc_param(frame.payload, self.state.stc)
                elif t == messages.APO_TYPE:
                    self.state.apo_current = messages.parse_apo_element(frame.payload)
                elif t == messages.CUSTOM_BTN_TYPE:
                    self.state.custom_button = messages.parse_custom_button(frame.payload)
                elif t == messages.PAUSE_TYPE:
                    self.state.auto_pause = messages.parse_pause(frame.payload)
            elif (cmd in (Command.SYSTEM_RET_EXT_PARAM, Command.SYSTEM_NTFY_EXT_PARAM)
                    and len(frame.payload) > 1
                    and frame.payload[1] == messages.STC_TYPE):
                self.state.stc = messages.parse_stc_ext(frame.payload, self.state.stc)
            elif cmd == Command.GS_RET_CAPABILITY:
                slot, title = messages.parse_gs_capability(frame.payload)
                if title:
                    self.state.gs_slots[title] = slot
            elif cmd in (Command.GS_RET_PARAM, Command.GS_NTFY_PARAM):
                slot = frame.payload[1] if len(frame.payload) > 1 else -1
                if slot == self.state.gs_slots.get(messages.GS_TITLE_TOUCH_PANEL):
                    self.state.touch_panel = messages.parse_gs(frame.payload)
                elif slot == self.state.gs_slots.get(messages.GS_TITLE_MULTIPOINT):
                    self.state.multipoint = messages.parse_gs(frame.payload)
            elif cmd == Command.ALERT_NTFY_PARAM:
                self._confirm_alert(frame.payload)
            elif cmd == 0x37:  # UPDT_RET_PARAM — device identity (dual format)
                self._apply_updt(frame.payload)
        except (IndexError, ValueError) as exc:
            log.debug("could not parse frame cmd=%#04x: %s", cmd, exc)

    def _confirm_alert(self, payload: bytes) -> None:
        """The device raised a FIXED_MESSAGE dialog (e.g. 'this reboots the
        headphones'). We already warned the user in the GUI, so answer POSITIVE
        (OK) to let the change apply. Payload: [0x99, inquiredType, msgType, action].
        The reply drops the link when the device reboots — expected."""
        from .protocol.enums import (ALERT_ACTION_POSITIVE,
                                      ALERT_INQUIRED_FIXED_MESSAGE)
        if len(payload) < 3 or payload[1] != ALERT_INQUIRED_FIXED_MESSAGE:
            return
        msg_type = payload[2]
        confirm = bytes((Command.ALERT_SET_PARAM,
                         ALERT_INQUIRED_FIXED_MESSAGE, msg_type, ALERT_ACTION_POSITIVE))
        try:
            self._session.send_command(confirm)
        except Exception as exc:  # noqa: BLE001 — link drops on reboot
            log.debug("alert-confirm send failed (device likely rebooting): %s", exc)

    def _apply_updt(self, payload: bytes) -> None:
        """Parse a 0x37 field. Two formats by sub-index:
        length-prefixed string (0x02/03/04/06/0b) or a single byte value."""
        sub = payload[1]
        if len(payload) >= 4 and payload[2] == len(payload) - 3:
            value = payload[3:].decode("ascii", errors="replace")   # string field
        elif len(payload) >= 3:
            value = str(payload[2])                                  # single-byte field
        else:
            return
        self.state.identifiers[sub] = value
        if sub == 0x03:
            self.state.model_id = value
        elif sub == 0x06:
            self.state.serial = value
        elif sub == 0x0b:
            self.state.device_id = value
        elif sub in (0x07, 0x08, 0x09, 0x0a):
            self.state.version_fields.append(int(value))
