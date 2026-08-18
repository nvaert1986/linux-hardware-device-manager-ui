"""The shell's view of a Creative device.

Thin by design: :mod:`.controller` holds the protocol and the hard-won acknowledge handling, and
this file maps it onto the capability schema. The interesting decisions are all in the docstrings
below, because each one exists to prevent a specific failure the source project hit first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from hardware_ui.core.capability import Advisory, CapabilitySet
from hardware_ui.core.connection import ConnectionLabel
from hardware_ui.core.device import DependencyMissing, Device, DeviceInfo

from . import capabilities as C
from .controller import CreativeController, DeviceError, SafetyError
from .protocol import catalogue
from .protocol import presets as presets_mod
from .protocol.ids import (
    EQ_BAND_COUNT,
    FEATURE_LABELS,
    PROFILE_NAMES,
    Feature,
    OutputTarget,
    SubFeature,
)
from .transport import TransportError

log = logging.getLogger(__name__)

#: The unlock handshake retries five times with a one-second pause, and the initial sync issues
#: nineteen reads. Both are the vendor library's own timings, not padding, so a first connect is
#: measured in seconds rather than milliseconds.
CONNECT_TIMEOUT = 45.0

#: Which Gentoo package supplies each import this module needs at connect time. Neither is
#: optional: without ``pyusb`` there is no way to claim the interface, and without
#: ``cryptography`` the card cannot be unlocked, so it discards every command sent to it.
PACKAGES = {
    "usb": "dev-python/pyusb",
    "cryptography": "dev-python/cryptography",
}


def _dependency(exc: ImportError) -> DependencyMissing:
    """Turn a missing import into something a user can act on.

    Without this the shell shows a bare ``ImportError: No module named 'usb'`` on Connect, which
    reads as a bug in this application rather than a package that was never installed.
    """
    missing = (getattr(exc, "name", "") or "").split(".")[0]
    package = PACKAGES.get(missing)
    if package:
        return DependencyMissing(
            f"Creative devices need {package}, which is not installed. "
            f"The rest of the application is unaffected."
        )
    return DependencyMissing(f"Creative support is missing a dependency: {exc}")


class CreativeDevice(Device):
    """A Creative sound card or headphone amplifier reached over its CDC control channel."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._controller: CreativeController | None = None
        self._capabilities = CapabilitySet([])
        self._advisories: dict[str, Advisory] = {}
        #: Which of the card's four stored profiles was last recalled *from here*. The protocol
        #: has a command to select one (26) and none this project has seen to ask which is active,
        #: so this is the only honest answer -- and ``None`` until the user picks one, rather than
        #: a confident "Profile 1" that would be wrong three times in four.
        self._profile: int | None = None
        #: Whether Super X-Fi is on, as far as this application can tell.
        #:
        #: ``None`` means unknown, which is its state on connect and stays so until something here
        #: switches it. The card answers no read for it: the initial sync does not ask -- neither
        #: does the vendor application's -- ``HardwareButton`` op 1 goes unanswered on an X4, and
        #: ``SuperXFi`` op 1 returns a list of supported operations rather than any state. It is
        #: reported only when the device volunteers a frame.
        self._sxfi: bool | None = None

    # ------------------------------------------------------------------ lifecycle

    connect_timeout = CONNECT_TIMEOUT

    def connect_notice(self) -> str:
        """Said before the wait, not after it.

        The device boots locked and discards every command until an ASCII challenge/response
        completes; the handshake alone retries five times at one-second intervals. Silence for
        several seconds reads as a hang, which is the same reasoning behind the Jabra module's
        30-60 second warning.
        """
        return ("Connecting takes a few seconds: the card must complete its unlock handshake "
                "before it will answer anything.")

    async def connect(self) -> None:
        controller = CreativeController(
            product_id=self.info.product_id or None,
            serial=self.info.serial,
        )
        try:
            await asyncio.to_thread(controller.connect)
        except ImportError as exc:
            # Raised from inside the transport and the unlock, which import their libraries
            # lazily so that an installation without them still runs everything else.
            raise _dependency(exc) from exc
        except (TransportError, DeviceError) as exc:
            with contextlib.suppress(Exception):
                controller.close()
            raise RuntimeError(str(exc)) from exc
        self._controller = controller
        self._describe()

    async def disconnect(self) -> None:
        if self._controller is not None:
            await asyncio.to_thread(self._controller.close)
            self._controller = None

    def connection_label(self) -> ConnectionLabel:
        controller = self._controller
        if controller is None or not controller.connected:
            return ConnectionLabel("Not connected", "")
        state = controller.state
        detail = " · ".join(part for part in (
            f"firmware {state.firmware}" if state.firmware else "",
            "unlocked" if controller.unlocked else "locked",
        ) if part)
        return ConnectionLabel("USB control channel", detail)

    # ------------------------------------------------------------------ capabilities

    @property
    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    def _describe(self) -> None:
        controller = self._require()
        state = controller.state
        self._capabilities = CapabilitySet(C.build(
            feature_support=state.feature_support,
            subfeatures=state.subfeatures,
            # What the equaliser *did*, not only what the mask claims. Twelve reads that all timed
            # out would otherwise become twelve sliders that do nothing.
            has_equalizer_state=state.eq_enabled is not None,
            presets=dict(controller.presets),
            profile_names=self._profile_names(),
        ))
        self._advise()
        self._bump_capabilities()

    def _profile_names(self) -> dict[int, str]:
        """What the card's four stored modes are called, where that is known.

        Two sources, in order. :data:`PROFILE_NAMES` knows the models whose modes have been read
        off real hardware -- an X4's are Music, Movie, Footstep Enhancer and EQ for Super X-Fi, the
        four the button on the front cycles through. Imported vendor preset data overrides that per
        slot where it has an opinion, since it came from the card's own maker.

        Neither is read from the device: there is a command to *name* a mode and one to select one,
        and this project has never captured a reply to a name **read**, so decoding one would be
        guesswork and a wrong name is worse than a number. A model in neither source gets numbers.
        """
        names = dict(enumerate(PROFILE_NAMES.get(self.info.product_id or 0, ())))
        names.update(presets_mod.slots(self._require().presets))
        return names

    def _advise(self) -> None:
        """Say what the page cannot show by itself.

        Direct Mode already gates every DSP row through ``requires``, so the controls grey out on
        their own. The advisory covers the part greying cannot express: *why*, and that the card is
        doing exactly what it was asked to.

        One advisory per affected **tab**, because the shell shows a tab the message belonging to
        the first of its rows that has one. Equalizer and Sound are separate tabs, so a single
        advisory on the equaliser toggle left the greyed-out Super X-Fi rows with no explanation at
        all -- which is the report this exists to answer.
        """
        self._advisories = {}
        state = self._require().state
        if state.feature(Feature.DIRECT_MODE):
            bypassed = Advisory(
                "Direct Mode is on, which bypasses the DSP entirely — the equaliser, Super X-Fi "
                "and headphone virtualisation all have no effect while it is. Turn Direct Mode "
                "off, under Sound, to use them."
            )
            self._advisories[C.KEY_EQ_ENABLED] = bypassed
            self._advisories[C.KEY_SXFI] = bypassed
            return

        # Which equaliser modes the card will actually enter right now.
        #
        # An advisory rather than a gate on the list, deliberately. The rule is known exactly and
        # the values behind it are readable, so a gate is finally possible -- but changing which
        # choices exist means rebuilding the capability set, which repaints every tab and makes the
        # page jump under the user's hands. An advisory updates in place. The card refuses the
        # wrong mode anyway, with a message that now says the same thing.
        available = self._modes_available()
        if available:
            self._advisories[C.KEY_EQ_ENABLED] = Advisory(available)

    def advisories(self) -> dict[str, Advisory]:
        return dict(self._advisories)

    # ------------------------------------------------------------------ reads

    async def get(self, key: str) -> Any:
        return (await self.get_many([key])).get(key)

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Served from held state, not from the wire.

        The device is push-driven: a write draws an Acknowledge, the changed state and any
        dependent state, and the controller applies all of it as it arrives. So the held state is
        current by construction, and re-reading would be both slower and *less* accurate --
        the card takes about half a second to commit, so an immediate read returns the old value.
        Draining first collects anything pushed since the last call.
        """
        controller = self._require()
        await asyncio.to_thread(controller.drain)
        return {key: self._value(key) for key in keys}

    def _value(self, key: str) -> Any:
        state = self._require().state
        if key.startswith(C.FEATURE_PREFIX):
            feature = self._feature(key)
            return state.feature(feature) if feature is not None else None
        index = C.band_index(key)
        if index is not None:
            return state.eq_bands[index] if index < len(state.eq_bands) else None
        return {
            C.KEY_OUTPUT: state.output,
            # What the device said, or failing that what this application last set. Reporting
            # only the device's answer meant the box sprang straight back to unticked: the card
            # answers no read for Super X-Fi, so `sxfi_enabled` stays None however often it is
            # switched, and None repaints as off. The write does reach the card -- the mode
            # selector below it goes on working -- which is what made it look inert.
            C.KEY_SXFI: self._super_xfi(),
            C.KEY_SXFI_MODE: state.sxfi_mode,
            C.KEY_EQ_PREAMP: state.eq_preamp,
            C.KEY_EQ_PRESET: state.eq_preset,
            C.KEY_FIRMWARE: state.firmware,
            C.KEY_SERIAL: state.serial,
            C.KEY_VOLUME: None if state.volume_db is None else f"{state.volume_db:+.2f}",
            C.KEY_MUTED: None if state.muted is None else ("Yes" if state.muted else "No"),
            C.KEY_FEATURE_MASK: self._feature_mask_text(state),
            C.KEY_DSP_MASK: self._dsp_mask_text(state),
            # The card cannot be asked which profile is live, so this reports the last one this
            # application recalled and stays empty until it has recalled one. A confident "Profile
            # 1" would be wrong three times in four.
            # Off, "the curve below", or a stored profile. The card cannot be asked which of its
            # four profiles is live, so once one has been chosen here that is what is reported;
            # before that, on means the curve that is loaded, which is true whatever loaded it.
            # The card volunteers which mode it is in -- `ACTIVE_MALCOLM_PROFILE`, pushed after
            # anything that changes it -- so that is what is shown. What this application last
            # chose is the fallback for a card that has not said yet, and "on, mode unknown" is
            # the honest answer when neither has an opinion.
            # None -- an empty box -- when the equaliser is on and neither the card nor this
            # application has said which mode. Blank rather than a made-up "On" entry: the card has
            # no such state, so offering one would be describing a device that does not exist.
            C.KEY_EQ_ENABLED: (
                C.EQ_OFF if not state.eq_enabled
                else state.profile if state.profile is not None
                else self._profile
            ),
        }.get(key)

    @staticmethod
    def _feature_mask_text(state: Any) -> str | None:
        """The feature mask, decoded into names rather than left as a hex number.

        ``0x00204060`` tells nobody anything. The list is what answers "why is that toggle not
        here?", and the answer is that this unit's firmware does not implement it.
        """
        if not state.feature_support:
            return None
        names = [label for feature, label in FEATURE_LABELS.items()
                 if state.feature_support & feature.mask]
        return f"0x{state.feature_support:08x} — " + (", ".join(names) or "none of the above")

    @staticmethod
    def _dsp_mask_text(state: Any) -> str | None:
        if state.subfeatures is None:
            return None
        names = [f.name.replace("_", " ").title() for f in SubFeature if f & state.subfeatures]
        return f"0x{int(state.subfeatures):04x} — " + (", ".join(names) or "none")

    async def refresh(self) -> dict[str, Any]:
        """A full re-read, which is a user action rather than a timer.

        Creative's own application never polls -- it registers a notification callback and has no
        polling timers at all -- and copying that is why this module costs no idle traffic.
        """
        controller = self._require()
        await asyncio.to_thread(controller.sync)
        self._describe()
        return {c.key: self._value(c.key) for c in self._capabilities}

    # ------------------------------------------------------------------ writes

    async def set(self, key: str, value: Any) -> Any | None:
        self._require()
        try:
            await asyncio.to_thread(self._write, key, value)
        except SafetyError as exc:
            # Never reachable from the UI -- no dangerous command is exposed as a capability --
            # but the guard is carried from the source and must stay ahead of any future caller.
            raise RuntimeError(str(exc)) from exc
        except (TransportError, DeviceError) as exc:
            raise RuntimeError(f"{key}: {exc}") from exc

        # Collect what the card reports about the change before answering. Without this the value
        # returned is the one from before the write -- the acknowledge beats the state push -- and
        # the control springs back to where it was.
        await asyncio.to_thread(self._require().settle)
        if key == C.KEY_OUTPUT:
            # Read it back, and only now: routing takes 300-600 ms to commit, and the settle above
            # may have replayed a push from before the write. This is the authoritative answer.
            await asyncio.to_thread(self._require().confirm_output, int(value))

        # Direct Mode changes which controls apply, so the page has to be rebuilt rather than
        # merely repainted. Everything else leaves the shape alone.
        if key in (C.KEY_DIRECT_MODE, C.KEY_SXFI):
            # Both change which controls apply. `_advise` only rewrites messages, so this repaints
            # rather than rebuilding -- no capability revision bump, no page-wide repaint.
            self._advise()
        return self._value(key)

    def _write(self, key: str, value: Any) -> None:
        controller = self._require()
        feature = self._feature(key)
        if feature is not None:
            controller.set_feature(feature, bool(value))
            return
        index = C.band_index(key)
        if index is not None:
            controller.set_eq_band(index, float(value))
            return
        if key == C.KEY_OUTPUT:
            controller.set_output(OutputTarget(int(value)))
        elif key == C.KEY_SXFI:
            controller.set_sxfi(bool(value))
            # Remembered because it cannot be read back, and because a mode that only applies
            # under Super X-Fi is offered or withheld on the strength of it.
            self._sxfi = bool(value)
        elif key == C.KEY_SXFI_MODE:
            controller.set_sxfi_mode(str(value))
        elif key == C.KEY_EQ_ENABLED:
            self._select_equalizer(int(value))
        elif key == C.KEY_EQ_PREAMP:
            controller.set_eq_preamp(float(value))
        elif key == C.KEY_EQ_PRESET:
            controller.apply_eq_preset(str(value))
        elif key == C.KEY_PROFILE_STORE:
            answer = value or {}
            controller.store_profile(
                self._profile_name(answer), index=self._checked_slot(int(answer.get("slot", 0))))
        else:
            raise DeviceError(f"{key} is not writable")

    def _select_equalizer(self, choice: int) -> None:
        """Off, on with the current curve, or on with one of the card's four stored profiles.

        Two device operations behind one control, because on this hardware they are one act: the
        card's own button cycles off, then each stored mode in turn. Turning the equaliser on
        without saying which curve is what produced "why did enabling it put it in Movie mode?" --
        it put it in whichever mode was already live.
        """
        controller = self._require()
        if choice == C.EQ_OFF:
            controller.set_eq_enabled(False)
            return
        index = self._checked_slot(choice)
        # The mode first, then the switch. A mode the card will not take -- refused outright, or
        # accepted and then not entered -- must not leave the equaliser switched on into whatever
        # mode happened to be live already.
        try:
            controller.select_profile(index)
        except DeviceError as exc:
            raise self._not_taken(index, str(exc)) from exc
        controller.drain()
        live = controller.state.profile
        if live is not None and live != index:
            # Accepted with status 0 and then not entered. The card does this rather than refusing
            # in one direction of the Super X-Fi interlock, and a silent revert is indistinguishable
            # from a broken application unless it is said out loud.
            raise self._not_taken(index, f"the card stayed on mode {live + 1}")
        controller.set_eq_enabled(True)
        self._profile = index
        # Recalling replaces the whole curve *on the card*, so unlike a preset applied from here
        # nothing in held state knows the new gains. Re-read, which is what moves the sliders.
        controller.sync_equalizer()

    def _super_xfi(self) -> bool | None:
        """Super X-Fi as best it is known: what the card said, else what this application set.

        The card reports it, but only after something changes it and only once the push has been
        drained -- and an advisory recomputed straight after a write runs before that. Falling back
        on what was just sent is what stops the page saying nothing for a second.
        """
        reported = self._require().state.sxfi_enabled
        return reported if reported is not None else self._sxfi

    def _modes_available(self) -> str:
        """Which of the card's modes it will enter, given Super X-Fi, or "" if that is unknown.

        The interlock is exact and symmetric, measured on a Sound Blaster X4: with Super X-Fi on
        the card takes only its Super X-Fi curve and refuses the other three; with it off, exactly
        the reverse. Both directions answer ``status=128`` on command 26.
        """
        sxfi = self._super_xfi()
        if sxfi is None:
            return ""
        names = self._profile_names()
        wanted = [n for i, n in sorted(names.items())
                  if ("super x-fi" in n.casefold()) == bool(sxfi)]
        if not wanted:
            return ""
        listed = ", ".join(wanted[:-1]) + (f" and {wanted[-1]}" if len(wanted) > 1 else wanted[-1])
        state = "on" if sxfi else "off"
        opposite = "off" if sxfi else "on"
        return (
            f"Super X-Fi is {state}, so the card will only switch to {listed}. The card keeps two "
            f"sets of equaliser modes, one for each Super X-Fi state, and refuses the other set "
            f"until Super X-Fi is turned {opposite}."
        )

    def _not_taken(self, index: int, detail: str) -> DeviceError:
        """Say why a mode did not stick, in a sentence rather than as a protocol dump.

        The card enforces its own interlock and that is the right place for it -- two attempts to
        enforce it in the page got it wrong, once by hiding an available mode and once by
        rebuilding the page under the user's hands. What was wrong was the reporting: a refusal
        arrived as "command 26 rejected, status=128 (1a 80 00 ...)", and the other direction did
        not report at all.

        **The interlock runs both ways**, which is the part that took two rounds to see. The card
        keeps two equaliser sets, one for Super X-Fi and one for everything else, and only the set
        matching the current state can be entered: the Super X-Fi curve needs Super X-Fi on, and
        the other three need it off.
        """
        names = self._profile_names()
        name = names.get(index, f"mode {index + 1}")
        wants_sxfi = "super x-fi" in name.casefold()
        others = ", ".join(
            n for i, n in sorted(names.items())
            if i != index and "super x-fi" not in n.casefold()
        )
        if wants_sxfi:
            return DeviceError(
                f"The card would not switch to “{name}”: it is the equaliser curve for Super X-Fi "
                f"and applies only while Super X-Fi is on. Turn Super X-Fi on first, under Sound.")
        return DeviceError(
            f"The card would not switch to “{name}”: while Super X-Fi is on it uses its Super X-Fi "
            f"curve instead. Turn Super X-Fi off, under Sound, to use "
            f"{others or 'the other modes'}." if self._super_xfi()
            else f"The card would not switch to “{name}” ({detail}).")



    @staticmethod
    def _checked_slot(index: int) -> int:
        """Refused rather than clamped if it is out of range.

        Clamping would silently write to slot 3 for a request naming slot 9, and overwriting a
        profile the user did not name is not a rounding error.
        """
        if not 0 <= index < catalogue.PROFILE_SLOTS:
            raise DeviceError(f"the card has slots 0-{catalogue.PROFILE_SLOTS - 1}, not {index}")
        return index

    @staticmethod
    def _profile_name(answer: Any) -> str:
        name = str((answer or {}).get("name", "")).strip()
        if not name:
            raise DeviceError("a stored profile needs a name")
        # The dialog already enforces the length, so this is the guard for every other caller --
        # the CLI, a script. `catalogue.set_profile_name` raises too; failing here says why.
        if len(name) > catalogue.PROFILE_NAME_MAX:
            raise DeviceError(
                f"the card accepts at most {catalogue.PROFILE_NAME_MAX} characters")
        return name

    # ------------------------------------------------------------------ photo

    async def fetch_photo(self) -> bytes | None:
        """Only ever the user's own copy.

        Creative's product artwork is theirs. The source project's importer copies an image out of
        a Creative App installation the user already has; nothing is downloaded and nothing ships.
        """
        path = presets_mod.image_path()
        if path is None or not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _feature(key: str) -> Feature | None:
        if not key.startswith(C.FEATURE_PREFIX):
            return None
        name = key[len(C.FEATURE_PREFIX):].upper()
        try:
            return Feature[name]
        except KeyError:
            return None

    def _require(self) -> CreativeController:
        if self._controller is None:
            raise RuntimeError(f"{self.info.name} is not connected")
        return self._controller


#: Ten bands, and the schema builder derives its labels from the same table the protocol uses.
assert len(presets_mod.BAND_FREQUENCIES) >= EQ_BAND_COUNT

__all__ = ["CreativeDevice"]
