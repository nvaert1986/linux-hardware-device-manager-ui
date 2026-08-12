"""Adapts the ported, hardware-verified ``Headphones`` client to the shared ``Device`` contract.

Three impedance mismatches are resolved here.

**Sync to async.** ``Headphones`` is blocking-socket code tested against real hardware. Rewriting
it as native asyncio would risk the one part of this module known to work, so it stays as-is and
every call is dispatched with :func:`asyncio.to_thread`. One lock serialises access, because MDR
is a single RFCOMM channel and overlapping requests would interleave their replies.

**Per-key writes to composite messages.** The shell writes one capability at a time, but
``set_ncasm`` carries mode, focus-on-voice and ambient level in a *single* message, and
``set_stc`` likewise carries enabled, sensitivity and timeout. Writing one means reading its
siblings and sending all of them. That composition is a property of Sony's protocol, so it lives
here rather than in the schema.

**Two noise modes, not three.** ``build_set_ncasm`` documents that ``enabled=True`` is Ambient
Sound and ``enabled=False`` is Noise Cancelling, and that the notional ``effect=OFF`` third mode
is ignored by the hardware. An earlier revision assumed off/ambient/anc, which inverted the modes
and made every noise-control write fail to confirm.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from pathlib import Path
from typing import Any

from hardware_ui.core import (
    Advisory,
    CapabilitySet,
    Device,
    DeviceError,
    DeviceInfo,
    NotSupported,
    Unreachable,
)
from hardware_ui.core.paths import cache_dir, ensure

from . import capabilities as caps
from .headphones import Headphones
from .protocol import messages as M
from .transport.rfcomm import TransportError

log = logging.getLogger(__name__)

#: Sentinel distinguishing "device has not reported this yet" from a real ``None``.
UNREAD = object()

#: Sentinel for "no previous value recorded", distinct from a device value of None.
_MISSING = object()


class SonyDevice(Device):
    """One connected WH/WF-1000X series headset."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        if not info.address:
            raise DeviceError("Sony module requires a Bluetooth address")
        self._hp = Headphones(info.address)
        self._caps = CapabilitySet()
        self._lock = asyncio.Lock()

    @property
    def capabilities(self) -> CapabilitySet:
        return self._caps

    # ---------------------------------------------------------------- lifecycle

    #: errnos that mean "not switched on / not in range", not a fault worth a traceback.
    _UNREACHABLE_ERRNOS = frozenset(
        {errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETUNREACH,
         errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ENODEV, errno.ENOTCONN}
    )

    def _cache_path(self) -> Path:
        """Where this headset's discovery cache lives.

        Under the *cache* directory, not data: it is entirely re-derivable, and deleting it costs
        one slower connect rather than losing anything.
        """
        safe = self.info.address.replace(":", "").lower()
        return ensure(cache_dir() / "sony_headsets") / f"{safe}.json"

    def _load_cache(self) -> dict | None:
        try:
            data = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _save_cache(self) -> None:
        try:
            path = self._cache_path()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._hp.export_discovery(), indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.debug("could not write discovery cache", exc_info=True)

    async def connect(self) -> None:
        try:
            await asyncio.to_thread(self._hp.connect)
            # handshake() already ends with sync() -- "Run the confirmed CONNECT sequence and an
            # initial state sync". Calling sync() again here repeated all 11 state GETs and
            # roughly doubled the time to open a device.
            #
            # The cache holds only what is fixed for this device and firmware: identity fields,
            # the function list, APO options, GS slots. No setting is cached, because Sony's
            # phone app can change those between sessions -- sync() always reads them fresh.
            cached = await asyncio.to_thread(self._load_cache)
            state = await asyncio.to_thread(self._hp.handshake, cached)
        except Exception as exc:
            # Close on any failure. Leaving a half-open socket behind is what turns one
            # "Host is down" into a cascade of "Device or resource busy" on every retry --
            # the second failure is ours, not the headset's.
            try:
                await asyncio.to_thread(self._hp.close)
            except Exception:
                log.debug("cleanup after failed connect also failed", exc_info=True)
            raise self._translate(exc) from exc
        self._caps = caps.build(
            set(state.supported_functions),
            apo_options=list(state.apo_options),
            state=state,
        )
        await asyncio.to_thread(self._save_cache)
        log.info(
            "%s: connected, %d functions advertised, %d capabilities",
            state.model_name,
            len(state.supported_functions),
            len(self._caps),
        )

    @classmethod
    def _translate(cls, exc: Exception) -> Exception:
        """Turn a transport failure into a :class:`Unreachable` the shell can phrase for a human.

        Classified by *exception type* first. Sniffing errnos or message text was not enough --
        the ported transport raises a plain ``TransportError`` for a connect timeout with no
        errno at all, and ``os.strerror()`` output is locale-dependent, so grepping for "Host is
        down" breaks on a Dutch or German system.

        Every ``TransportError`` from ``connect()`` means the same thing to the user: the headset
        could not be opened. None of the variants are actionable in different ways, so all of
        them become ``Unreachable`` with a message that says what to do.
        """
        if isinstance(exc, OSError) and exc.errno in cls._UNREACHABLE_ERRNOS:
            return Unreachable(str(exc))
        if isinstance(exc, OSError) and exc.errno == errno.EBUSY:
            return Unreachable("in use by another application")
        if isinstance(exc, TransportError):
            if os.strerror(errno.EBUSY) in str(exc):
                return Unreachable("in use by another application")
            return Unreachable("not reachable")
        return exc

    async def disconnect(self) -> None:
        try:
            await asyncio.to_thread(self._hp.close)
        except Exception:
            log.debug("close failed; dropping the socket anyway", exc_info=True)

    # ---------------------------------------------------------------- reads

    async def get(self, key: str) -> Any:
        async with self._lock:
            value = self._read(key)
            return None if value is UNREAD else value

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """One lock acquisition for the whole page.

        Values already live in ``Headphones.state`` after ``sync()``, so this is a dictionary walk
        rather than a round trip per key.

        A key whose value is :data:`UNREAD` is *omitted* rather than returned as ``None``. The
        distinction matters: the shell marks omitted keys unsupported, and conflating "not read
        yet" with "not supported" is what made the XM4's equaliser show as unsupported.
        """
        async with self._lock:
            out: dict[str, Any] = {}
            for key in keys:
                try:
                    value = self._read(key)
                except NotSupported:
                    continue
                if value is not UNREAD:
                    out[key] = value
            return out

    def _read(self, key: str) -> Any:
        """Return the current value, :data:`UNREAD` if not yet known, or raise NotSupported."""
        s = self._hp.state
        match key:
            case "info.model":
                return s.model_name
            case "info.serial":
                return s.serial or UNREAD
            case "info.model_id":
                return s.model_id or UNREAD
            case "info.device_id":
                return s.device_id or UNREAD
            case "info.codes":
                # The reference Info panel shows identifier fields 0x02 and 0x04.
                codes = [v for k, v in s.identifiers.items() if k in (0x02, 0x04)]
                return ", ".join(str(c) for c in codes) if codes else UNREAD
            case "info.protocol":
                return s.protocol_raw.hex(" ") if s.protocol_raw else UNREAD
            case "info.firmware":
                fields = s.version_fields
                return ".".join(str(f) for f in fields) if fields else UNREAD
            case "info.codec":
                return s.codec if s.codec else UNREAD
            case "info.battery":
                return s.battery.level if s.battery else UNREAD
            case "info.battery_lr":
                # True-wireless models report the buds separately; the reference Battery panel
                # shows "L x%  R y%" for these instead of a single figure.
                if not s.battery or s.battery.left is None:
                    raise NotSupported(key)
                return f"L {s.battery.left}%  R {s.battery.right}%"
            case "info.charging":
                if not s.battery:
                    return UNREAD
                return "Charging" if s.battery.charging else "Not charging"

            # Two modes: ncasm.enabled is specifically "ambient sound is on".
            case "anc.mode":
                return ("ambient" if s.ncasm.enabled else "nc") if s.ncasm else UNREAD
            case "anc.ambient_level":
                return s.ncasm.asm_level if s.ncasm else UNREAD
            case "anc.voice_passthrough":
                return s.ncasm.focus_on_voice if s.ncasm else UNREAD

            case "eq.preset":
                return s.eq.preset_id if s.eq else UNREAD
            case _ if key.startswith("eq.band"):
                if not s.eq or not s.eq.bands:
                    return UNREAD
                idx = int(key.removeprefix("eq.band"))
                return s.eq.bands[idx] if 0 <= idx < len(s.eq.bands) else UNREAD

            case "sound.speak_to_chat":
                return s.stc.enabled if s.stc else UNREAD
            case "sound.stc_sensitivity":
                return s.stc.sensitivity if s.stc else UNREAD
            case "sound.stc_timeout":
                return s.stc.timeout if s.stc else UNREAD
            case "sound.dsee":
                return s.dsee if s.dsee is not None else UNREAD
            case "sound.quality_mode":
                return s.sound_quality if s.sound_quality is not None else UNREAD

            case "system.touch_sensor":
                return s.touch_panel if s.touch_panel is not None else UNREAD
            case "system.auto_pause":
                return s.auto_pause if s.auto_pause is not None else UNREAD
            case "system.multipoint":
                return s.multipoint if s.multipoint is not None else UNREAD
            case "system.custom_button":
                return s.custom_button if s.custom_button is not None else UNREAD
            case "system.auto_power_off":
                return s.apo_current if s.apo_current is not None else UNREAD
        raise NotSupported(key)

    # ---------------------------------------------------------------- writes

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, key, value)

    def _write(self, key: str, value: Any) -> None:
        hp, s = self._hp, self._hp.state
        match key:
            case "anc.mode" | "anc.ambient_level" | "anc.voice_passthrough":
                self._write_ncasm(key, value)

            case "eq.preset":
                hp.set_eq_preset(int(value))
            case _ if key.startswith("eq.band"):
                if not s.eq or not s.eq.bands:
                    raise NotSupported("equaliser bands not yet read")
                bands = list(s.eq.bands)
                idx = int(key.removeprefix("eq.band"))
                if not 0 <= idx < len(bands):
                    raise NotSupported(key)
                bands[idx] = max(M.EQ_BAND_MIN, min(M.EQ_BAND_MAX, int(value)))
                hp.set_eq_bands(bands)

            case "sound.speak_to_chat" | "sound.stc_sensitivity" | "sound.stc_timeout":
                self._write_stc(key, value)
            case "sound.dsee":
                hp.set_dsee(bool(value))
            case "sound.quality_mode":
                hp.set_sound_quality(int(value))

            case "system.touch_sensor":
                hp.set_touch_panel(bool(value))
            case "system.auto_pause":
                hp.set_auto_pause(bool(value))
            case "system.multipoint":
                hp.set_multipoint(bool(value))
            case "system.custom_button":
                hp.set_custom_button(int(value))
            case "system.auto_power_off":
                hp.set_auto_power_off(int(value))
            case _:
                raise NotSupported(key)

    def _write_ncasm(self, key: str, value: Any) -> None:
        """Send mode, focus-on-voice and ambient level as the one message MDR expects."""
        s = self._hp.state
        ambient = bool(s.ncasm.enabled) if s.ncasm else False
        focus = bool(s.ncasm.focus_on_voice) if s.ncasm else False
        level = int(s.ncasm.asm_level) if s.ncasm else 0

        if key == "anc.mode":
            if value not in ("nc", "ambient"):
                raise NotSupported(f"unknown noise-control mode {value!r}")
            ambient = value == "ambient"
        elif key == "anc.voice_passthrough":
            focus = bool(value)
        else:
            level = int(value)

        level = max(0, min(M.enums.MAX_ASM_STEPS_XM3, level))
        self._hp.set_ncasm(enabled=ambient, focus_on_voice=focus, asm_level=level)

    def _write_stc(self, key: str, value: Any) -> None:
        """Speak-to-chat is composite too -- preserve the fields the user did not touch."""
        s = self._hp.state
        enabled = bool(s.stc.enabled) if s.stc else False
        sensitivity = int(s.stc.sensitivity) if s.stc else 0
        timeout = int(s.stc.timeout) if s.stc else 1

        if key == "sound.speak_to_chat":
            enabled = bool(value)
        elif key == "sound.stc_sensitivity":
            sensitivity = int(value)
        else:
            timeout = int(value)

        self._hp.set_stc(enabled=enabled, sensitivity=sensitivity, timeout=timeout)

    #: Models where applying an EQ change while on LDAC forces the codec down to SBC.
    #: From the reference implementation's ``_EQ_LDAC_INCOMPATIBLE``; see the protocol doc §7.3.1.
    EQ_LDAC_INCOMPATIBLE = frozenset({"WH-1000XM3"})

    def advisories(self) -> dict[str, Advisory]:
        """Equaliser locking and its three explanatory states.

        The messages matter more than the lock: a user whose equaliser is greyed out needs to be
        told it is LDAC doing it and which setting to change, not left guessing.
        """
        s = self._hp.state
        out: dict[str, Advisory] = {}
        if s.eq is None:
            return out

        ldac_locked = s.model_name in self.EQ_LDAC_INCOMPATIBLE and s.codec == "LDAC"
        editable = not ldac_locked and s.eq.preset_id in M.EQ_CUSTOM_PRESETS

        if ldac_locked:
            note = Advisory(
                message=(
                    "The equalizer is only supported in SBC mode on this model. It is disabled "
                    "while LDAC is active \u2014 set Connection preference to \u201cPriority on "
                    "Stable Connection\u201d (Connectivity tab) to use it."
                ),
                locked=True,
            )
            out["eq.preset"] = note
            for cap in self._caps:
                if cap.key.startswith("eq.band"):
                    out[cap.key] = Advisory(locked=True)
            return out

        out["eq.preset"] = Advisory(
            message=(
                "Drag a band and release to set your custom curve."
                if editable
                else "This preset\u2019s bands are fixed. Choose a Custom slot to edit them."
            )
        )
        return out

    async def refresh(self) -> dict[str, Any]:
        """Re-read the device, as the reference implementation's 30 s poll does.

        This uses ``sync()`` -- a full MDR read -- rather than the BlueZ-only ``refresh_status``.
        An earlier version chose the cheaper call on the strength of ``refresh_status``'s
        docstring warning about MDR traffic on an LDAC link. With no notification listener
        (see above), the poll is the *only* route by which a change made on the headset itself,
        or from Sony's phone app, ever reaches us -- so it has to read the real state.

        ``sync()`` is also what the reference implementation actually polls with, and that is the
        combination proven on hardware. Values for capabilities with a write in flight are
        dropped by the model, so a poll cannot repaint a control mid-change.
        """
        async with self._lock:
            await asyncio.to_thread(self._hp.sync)
            out: dict[str, Any] = {}
            for cap in self._caps:
                try:
                    value = self._read(cap.key)
                except NotSupported:
                    continue
                if value is not UNREAD:
                    out[cap.key] = value
            return out

    # ---------------------------------------------------------------- notifications
    #
    # There is deliberately no ``changes()`` override.
    #
    # An earlier version ran ``Headphones.listen()`` on a background thread to pick up NTFY
    # pushes. That was invented, not ported: the reference implementation never calls
    # ``listen()`` -- it appears only in a docstring example -- and its worker owns the session
    # on a single thread.
    #
    # The reason matters. A second thread reading frames from the same RFCOMM session races
    # every write: it consumes the ACK that ``send_command`` is blocking for, and ``_apply``
    # mutates ``state`` while a composite write is reading it. That is what made speak-to-chat
    # switch itself off while its sensitivity was being changed, and what produced intermittent
    # "device did not confirm" on writes that had actually landed.
    #
    # State changes are picked up by ``refresh()`` on the shell's poll instead, exactly as the
    # reference implementation does.
