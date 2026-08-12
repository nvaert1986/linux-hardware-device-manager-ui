"""The Dell adapter: capability keys in, ddcutil invocations out.

Three rules from the reference implementation shape everything here, and each of them cost
somebody a debugging session:

**A write is not applied until it has been read back.** ``setvcp`` exiting 0 means the monitor
acknowledged the message, not that it acted on it -- several Dell panels ACK a feature they only
implement in their own menu. Every write is ``--noverify`` followed by an explicit ``getvcp``.

**A quantised read-back is success.** The P2425D refuses contrast below 25 and moves sharpness in
steps of ten, so a request of 55 lands as 50. That is the change taking effect, and the honest
report is 50 -- not "failed", and not the 55 the user asked for.

**One thread owns the bus.** I²C is not concurrency-safe and every ddcutil invocation re-probes
the bus. Everything here runs under a single lock, and reads are batched into one invocation
because that alone is a 4-5x difference (12.07 s vs 2.65 s over MST, measured).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from hardware_ui.core import (
    Advisory,
    CapabilitySet,
    DependencyMissing,
    Device,
    DeviceError,
    DeviceInfo,
    NotSupported,
    Unreachable,
)
from hardware_ui.core.paths import cache_dir, ensure

from . import capabilities as C
from .protocol import features as F
from .protocol.calibration import Range, probe_range
from .protocol.ddcutil import (
    DDCError,
    Monitor,
    VcpReading,
    detect_monitors,
    get_capabilities,
    get_monitor_info,
    get_vcp,
    get_vcp_many,
    get_vcp_word,
    is_read_only,
    run_detect,
    set_vcp,
    set_vcp_bit,
    set_vcp_word,
)

log = logging.getLogger(__name__)

MODULE_ID = "dell_monitors"

_SETTLE_SECONDS = 0.2
"""The reference implementation's settle time: read the value back too soon and the panel is
still reporting the old one."""

_FACTORY_RESET_SETTLE = 4.0
"""``0x04`` is write-only, so the only way to show the result is to wait and re-read."""

_PIP_VERIFY_DELAYS = (1.0, 1.5, 2.0, 2.5, 3.0)
"""Entering or leaving PIP/PBP blanks the panel and re-initialises it, so the immediate read-back
errors or returns a transient value. Poll with growing delays and treat a read error as "still
coming back", not as failure."""

_PIP_COMMAND_DELAYS = (1.0, 1.5, 2.0)
"""A size or position toggle is fire-and-forget: 0xE9 reflects the *mode*, not the command, so
there is nothing to compare against -- take the first mode that reads cleanly."""

_DETECT_TTL = 10.0
"""How long a shared ``ddcutil detect`` may be reused. Detect takes no ``--bus``, so it is run
once for the whole system rather than once per monitor -- that was ~76 % of the Information tab's
cost in the origin. The TTL exists so opening a second monitor moments later pays nothing."""

_UNREACHABLE = ("no monitor detected", "cannot open", "no such file", "permission denied")
"""DDC failures that mean the bus is gone or unusable rather than the feature being unsupported."""


# --------------------------------------------------------------------------- shared detect

_detect_at = 0.0
_detect_monitors: list[Monitor] = []
_detect_text = ""


def _shared_detect(*, force: bool = False) -> tuple[list[Monitor], str]:
    """One ``ddcutil detect`` for every monitor, terse for the bus map and full for the EDID."""
    global _detect_at, _detect_monitors, _detect_text
    now = time.monotonic()
    if force or not _detect_monitors or now - _detect_at > _DETECT_TTL:
        _detect_monitors = detect_monitors()
        _detect_text = run_detect()
        _detect_at = now
    return _detect_monitors, _detect_text


# --------------------------------------------------------------------------- persistence


def _store_path(name: str) -> Any:
    return ensure(cache_dir() / MODULE_ID) / name


def _load_store(name: str) -> dict[str, Any]:
    try:
        return json.loads(_store_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_store(name: str, data: dict[str, Any]) -> None:
    path = _store_path(name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.warning("could not write %s", path, exc_info=True)


# --------------------------------------------------------------------------- the device


class DellMonitor(Device):
    """One Dell display, addressed by its I²C bus number."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._lock = asyncio.Lock()
        self._monitor: Monitor | None = None
        self._caps: dict[int, list[int] | None] = {}
        self._readings: dict[int, VcpReading] = {}
        self._values: dict[str, Any] = {}
        self._ranges: dict[int, Range] = {}
        self._input_names: dict[int, str] = {}
        self._info_rows: list[tuple[str, str]] = []
        self._set = CapabilitySet()

    # ------------------------------------------------------------------ identity

    @property
    def _bus(self) -> int:
        if self._monitor is None:
            raise Unreachable("not connected")
        return self._monitor.bus

    @property
    def _store_key(self) -> str:
        """Keyed by serial, so a calibration survives being moved to another port -- and falls
        back to the model for the handful of panels that publish no serial."""
        mon = self._monitor
        serial = (self.info.serial or (mon.serial if mon else "")).strip()
        return serial or f"model:{(mon.model if mon else self.info.name).strip()}"

    @property
    def capabilities(self) -> CapabilitySet:
        return self._set

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self._monitor = self._resolve_monitor()
        log.info("%s on /dev/i2c-%d", self.info.name, self._monitor.bus)
        try:
            self._caps = get_capabilities(self._monitor.bus)
        except DDCError as exc:
            raise self._translate(exc) from exc
        self._ranges = self._load_calibration()
        self._input_names = self._load_input_names()
        self._read_snapshot()
        self._rebuild()

    def _resolve_monitor(self) -> Monitor:
        """Find this display's I²C bus.

        Enumeration deliberately never touches a bus, so the bus number is not known until the
        user opens the display. Serial is the primary match -- two identical panels differ only
        there -- with the DRM connector as the fallback for models that publish no serial.
        """
        connector = str(self.info.properties.get("connector", ""))
        serial = (self.info.serial or "").strip()
        for force in (False, True):
            try:
                monitors, _ = _shared_detect(force=force)
            except DDCError as exc:
                raise self._translate(exc) from exc
            for mon in monitors:
                if serial and mon.serial.strip() == serial:
                    return mon
            for mon in monitors:
                if connector and mon.connector == connector:
                    return mon
            # A stale cache is the likely cause the first time round; re-run before giving up.
        raise Unreachable(
            "not reachable over DDC/CI — check that DDC/CI is enabled in the monitor's menu "
            "and that i2c-dev is loaded"
        )

    async def disconnect(self) -> None:
        # Nothing is held open: every operation is its own ddcutil invocation. That is the whole
        # cost model of this backend, and it is why disconnecting can never fail.
        self._monitor = None

    # ------------------------------------------------------------------ reading

    def _snapshot_codes(self) -> list[int]:
        """Every code worth one batched read.

        Excludes 0x04 (write-only) and 0xEA (two-level, reads ``FFFF`` until written), and
        anything the panel did not advertise.
        """
        codes = {c for c, v in self._caps.items() if F.feature_kind(c, v) is not None}
        for extra in (
            0xE2,
            F.PIP_MODE_CODE,
            F.PIP_SUBINPUT_CODE,
            F.PIP_STATUS_CODE,
            F.MST_CODE,
            F.USB_KVM_CODE,
        ):
            if extra in self._caps:
                codes.add(extra)
        codes.discard(F.FACTORY_RESET_CODE)
        codes.discard(F.USBC_PRIORITY_CODE)
        return sorted(codes)

    def _read_snapshot(self) -> None:
        """One batched read for the whole page, then the Information rows."""
        self._readings = get_vcp_many(self._bus, self._snapshot_codes())
        try:
            _, detect_text = _shared_detect()
            self._info_rows = get_monitor_info(self._monitor, detect_text)
        except DDCError:
            log.debug("monitor info unavailable", exc_info=True)
            self._info_rows = []
        self._values = self._values_from(self._readings)

    def _values_from(self, readings: dict[int, VcpReading]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for code, reading in readings.items():
            key = C.KEY_BY_CODE.get(code)
            if key is None:
                continue
            # A read-only enum is shown as text, so it wants the Dell name rather than the raw
            # byte -- "0°", not "1". Writable enums keep their numeric value: that is what the
            # combo's choices are keyed on.
            values[key] = (
                F.enum_label(code, reading.value)
                if code in F.ENUM_LABELS and is_read_only(code)
                else reading.value
            )

        # The merged preset reads through 0xE2, which is read-only and absent on some panels
        # (the U2412M). Where it is missing the control keeps the user's last choice, which is
        # why every access here is guarded rather than assumed.
        e2 = readings.get(0xE2)
        if e2 is not None:
            item = next(
                (i for i in F.build_preset_items(self._caps) if i.e2_value == e2.value), None
            )
            if item is not None:
                values[C.PRESET_KEY] = item.label
        elif C.PRESET_KEY in self._values:
            values[C.PRESET_KEY] = self._values[C.PRESET_KEY]

        if (pip := readings.get(F.PIP_MODE_CODE)) is not None:
            values[C.PIP_MODE_KEY] = pip.value
        if (sub := readings.get(F.PIP_SUBINPUT_CODE)) is not None:
            values[C.PIP_SUBINPUT_KEY] = sub.value
        if (status := readings.get(F.PIP_STATUS_CODE)) is not None:
            values[C.PIP_STATUS_KEY] = f"0x{status.value:02X}"

        if (mst := readings.get(F.MST_CODE)) is not None:
            if F.has_ddc_mst_control(self._caps):
                values[C.MST_KEY] = bool(_word(mst) & (1 << F.MST_ENABLE_BIT))
            else:
                values[C.MST_KEY] = "Set from the monitor's menu"

        values.update(self._kvm_values(readings))

        for code, name in self._input_names.items():
            values[C.input_name_key(code)] = name
        for code in self._caps.get(0x60) or []:
            values.setdefault(C.input_name_key(code), "")

        for label, text in self._info_rows:
            values[f"{C.INFO_PREFIX}{C._slug(label)}"] = text
        return values

    def _kvm_values(self, readings: dict[int, VcpReading]) -> dict[str, Any]:
        e7 = readings.get(F.USB_KVM_CODE)
        if e7 is None:
            if F.has_usb_kvm(self._caps) and not (
                F.usb_kvm_upstream_controllable(self._caps) or F.usb_kvm_bitpacked(self._caps)
            ):
                return {C.KVM_UPSTREAM_KEY: "Set from the monitor's menu"}
            return {}
        word = _word(e7)
        if F.usb_kvm_upstream_controllable(self._caps):
            return {C.KVM_UPSTREAM_KEY: word}
        if F.usb_kvm_bitpacked(self._caps):
            return {
                C.pair_key(code): F.usb_kvm_field_value(word, bit)
                for code, bit in F.usb_kvm_pairings(self._caps)
            }
        return {C.KVM_UPSTREAM_KEY: "Set from the monitor's menu"}

    def _rebuild(self) -> None:
        self._set = C.build(
            caps=self._caps,
            readings=self._readings,
            ranges=self._ranges,
            info_rows=self._info_rows,
            input_names=self._input_names,
            read_only=is_read_only,
        )
        self._bump_capabilities()

    async def get(self, key: str) -> Any:
        if key not in self._values:
            raise NotSupported(key)
        return self._values[key]

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        # Served from the snapshot: the batched read already happened, and re-reading per key
        # would undo the single biggest speed-up this backend has.
        return {k: v for k, v in self._values.items() if k in set(keys)}

    def advisories(self) -> dict[str, Advisory]:
        out: dict[str, Advisory] = {}
        if self._set.by_key(C.CALIBRATE_KEY) is not None and not self._ranges:
            out[C.CALIBRATE_KEY] = Advisory(message=C.NOTE_CALIBRATE)
        return out

    # ------------------------------------------------------------------ writing

    async def set(self, key: str, value: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._set_sync, key, value)

    def _set_sync(self, key: str, value: Any) -> Any:  # noqa: PLR0911 - a dispatch table
        if self._monitor is None:
            raise Unreachable("not connected")
        try:
            if key == C.REREAD_KEY:
                return self._reread()
            if key == C.CALIBRATE_KEY:
                return self._calibrate()
            if key == C.FACTORY_RESET_KEY:
                return self._factory_reset()
            if key.startswith("settings.input_name."):
                return self._rename_input(key, value)
            if key == C.PRESET_KEY:
                return self._write_preset(value)
            if key == C.PIP_MODE_KEY:
                return self._write_pip_mode(int(value))
            if key in (C.PIP_SIZE_KEY, C.PIP_POSITION_KEY):
                return self._write_pip_command(key)
            if key == C.PIP_SUBINPUT_KEY:
                return self._write_code(F.PIP_SUBINPUT_CODE, int(value), key)
            if key == C.MST_KEY:
                return self._write_mst(bool(value))
            if key == C.USBC_PRIORITY_KEY:
                return self._write_usbc_priority(int(value))
            if key == C.KVM_UPSTREAM_KEY:
                return self._write_kvm_upstream(int(value))
            if key.startswith(C.KVM_PAIR_PREFIX):
                return self._write_kvm_pairing(key, int(value))
            code = C.CODE_BY_KEY.get(key)
            if code is None:
                raise NotSupported(key)
            return self._write_code(code, int(value), key)
        except DDCError as exc:
            raise self._translate(exc) from exc

    # -- the ordinary path ---------------------------------------------------

    def _write_code(self, code: int, value: int, key: str) -> Any:
        """Write, settle, read back. Returns the value the monitor actually landed on."""
        previous = self._values.get(key)
        set_vcp(self._bus, code, value)
        time.sleep(_SETTLE_SECONDS)
        reading = get_vcp(self._bus, code)
        landed = self._judge(code, key, reading.value, value, previous)
        self._readings[code] = reading
        self._values[key] = landed
        return landed

    def _judge(self, code: int, key: str, actual: int, requested: int, previous: Any) -> int:
        """Decide whether a read-back that differs from the request is success.

        Leniency applies only to an *uncalibrated* continuous feature. Once its real step is
        known every value sent is a valid one, so a mismatch there is a genuine mismatch and
        hiding it would mask the failure the calibration exists to prevent.
        """
        if actual == requested:
            return actual
        lenient = code in F.CONTINUOUS and code not in self._ranges
        if lenient and actual != previous:
            log.info("0x%02X: requested %d, panel snapped to %d", code, requested, actual)
            return actual
        raise DeviceError(
            f"{F.feature_name(code)}: asked for "
            f"{F.enum_label(code, requested) if code in F.ENUM_LABELS else requested}, "
            f"the monitor reports "
            f"{F.enum_label(code, actual) if code in F.ENUM_LABELS else actual}"
        )

    def _write_preset(self, label: str) -> str:
        """One control, three registers.

        Each entry in the merged list writes its own opcode; ``0xE2`` only ever reports which
        preset is active, so verification is against the opcode that was written.
        """
        item = C.preset_by_label(self._caps, label)
        if item is None:
            raise NotSupported(f"unknown preset {label!r}")
        set_vcp(self._bus, item.write_code, item.write_value)
        time.sleep(_SETTLE_SECONDS)
        reading = get_vcp(self._bus, item.write_code)
        if reading.value != item.write_value:
            raise DeviceError(f"Colour Preset: the monitor did not accept “{label}”")
        self._values[C.PRESET_KEY] = label
        if 0xE2 in self._caps:
            try:
                self._readings[0xE2] = get_vcp(self._bus, 0xE2)
            except DDCError:
                log.debug("0xE2 read-back failed after a preset change", exc_info=True)
        return label

    # -- PIP / PBP -----------------------------------------------------------

    def _write_pip_mode(self, value: int) -> int:
        """Apply a PIP/PBP mode, tolerating the panel blanking while it re-initialises.

        The origin reported "failed" here for a change that had visibly applied, because the
        read-back lands while the monitor is still coming back. Silence is the expected signal,
        exactly as it is for a Sony reboot write -- so poll, ignore read errors, and only call it
        unconfirmed if a clean read never arrives.
        """
        set_vcp(self._bus, F.PIP_MODE_CODE, value)
        last: VcpReading | None = None
        for delay in _PIP_VERIFY_DELAYS:
            time.sleep(delay)
            try:
                last = get_vcp(self._bus, F.PIP_MODE_CODE)
            except DDCError:
                last = None  # still away; keep waiting
                continue
            if last.value == value:
                self._readings[F.PIP_MODE_CODE] = last
                self._values[C.PIP_MODE_KEY] = value
                return value
        if last is not None:
            raise DeviceError(
                "PIP/PBP did not take — the monitor reports "
                f"{F.PIP_MODE_LABELS.get(last.value, hex(last.value))}"
            )
        log.info("PIP/PBP applied but not confirmed: the panel never read back cleanly")
        self._values[C.PIP_MODE_KEY] = value
        return value

    def _write_pip_command(self, key: str) -> Any:
        """Toggle the sub-window's size or position.

        0xE9 reports the current *mode*, never the command that was sent, so there is nothing to
        compare against: send it and take the first mode that reads cleanly.
        """
        command = F.PIP_TOGGLE_SIZE if key == C.PIP_SIZE_KEY else F.PIP_TOGGLE_POSITION
        set_vcp(self._bus, F.PIP_MODE_CODE, command)
        for delay in _PIP_COMMAND_DELAYS:
            time.sleep(delay)
            try:
                reading = get_vcp(self._bus, F.PIP_MODE_CODE)
            except DDCError:
                continue
            self._readings[F.PIP_MODE_CODE] = reading
            self._values[C.PIP_MODE_KEY] = reading.value
            break
        return None

    # -- MST and USB-C -------------------------------------------------------

    def _write_mst(self, on: bool) -> bool:
        """Read-modify-write bit 4 of 0xEF, preserving the high-byte support bits.

        Only ever reached on a "new-spec" panel: on the older specification 0xEF reads 0x00 with
        MST both on and off, proven on a live two-panel chain, so the capability is a readout
        there and this is unreachable. The new-spec path has never met hardware.
        """
        word = set_vcp_bit(self._bus, F.MST_CODE, F.MST_ENABLE_BIT, on)
        landed = bool(word & (1 << F.MST_ENABLE_BIT))
        if landed != on:
            raise DeviceError("MST: the monitor did not accept the change")
        self._values[C.MST_KEY] = landed
        return landed

    def _write_usbc_priority(self, word: int) -> int:
        """Write-only by nature: a two-level 0xEA reads back ``FFFF``, so there is nothing to
        verify. The capability's note says so rather than the app pretending to have confirmed."""
        set_vcp_word(self._bus, F.USBC_PRIORITY_CODE, word)
        self._values[C.USBC_PRIORITY_KEY] = word
        return word

    # -- USB KVM -------------------------------------------------------------

    def _write_kvm_upstream(self, word: int) -> int:
        set_vcp_word(self._bus, F.USB_KVM_CODE, word)
        time.sleep(_SETTLE_SECONDS)
        landed = get_vcp_word(self._bus, F.USB_KVM_CODE)
        if landed != word:
            raise DeviceError("USB upstream: the monitor did not accept the change")
        self._values[C.KVM_UPSTREAM_KEY] = landed
        return landed

    def _write_kvm_pairing(self, key: str, index: int) -> int:
        """Set one input's 2-bit field inside the shared 0xE7 word.

        Bit position is ``14 - 2 x (the input's place in the advertised 0x60 order)``, decoded
        from Dell's software and confirmed on a P3424WE by predicting three transitions from the
        monitor's menu before writing any of them.
        """
        bit = next(
            (b for code, b in F.usb_kvm_pairings(self._caps) if C.pair_key(code) == key), None
        )
        if bit is None:
            raise NotSupported(key)
        current = get_vcp_word(self._bus, F.USB_KVM_CODE)
        target = F.usb_kvm_set_field(current, bit, index)
        set_vcp_word(self._bus, F.USB_KVM_CODE, target)
        time.sleep(_SETTLE_SECONDS)
        landed_word = get_vcp_word(self._bus, F.USB_KVM_CODE)
        landed = F.usb_kvm_field_value(landed_word, bit)
        if landed != index:
            raise DeviceError("USB upstream: the monitor did not accept the change")
        # Siblings share the register, so refresh them all -- they are one write, and the shell
        # holds them pending together for exactly that reason.
        for code, other in F.usb_kvm_pairings(self._caps):
            self._values[C.pair_key(code)] = F.usb_kvm_field_value(landed_word, other)
        return landed

    # -- actions -------------------------------------------------------------

    def _reread(self) -> None:
        """Re-read everything from the panel.

        There is no periodic poll: every read is an ddcutil invocation that re-probes the bus,
        and a background timer doing that competes with every other DDC access on the machine.
        The reference implementation polls nothing either -- it offers this same explicit action.
        """
        self._read_snapshot()
        self._rebuild()
        return None

    def _factory_reset(self) -> None:
        """0x04 is write-only, so the result can only be shown by waiting and re-reading."""
        set_vcp(self._bus, F.FACTORY_RESET_CODE, F.FACTORY_RESET_VALUE)
        time.sleep(_FACTORY_RESET_SETTLE)
        self._read_snapshot()
        self._rebuild()
        return None

    def _calibrate(self) -> None:
        """Discover each slider's real minimum and step by probing the panel.

        DDC/CI reports a maximum and nothing else. Everything below comes from writing test
        values and reading back what the panel accepted, which is why the screen flashes and why
        this is never automatic.
        """
        found: dict[int, Range] = {}
        for code in sorted(self._caps):
            if code not in F.CONTINUOUS or is_read_only(code):
                continue
            reading = self._readings.get(code)
            if reading is None or reading.maximum <= 0:
                continue
            try:
                found[code] = probe_range(self._bus, code, reading.maximum)
            except DDCError:
                log.info("0x%02X: calibration probe failed, leaving it uncalibrated", code)
        if not found:
            raise DeviceError("Nothing on this monitor could be calibrated")
        self._ranges = found
        self._save_calibration()
        self._read_snapshot()
        self._rebuild()
        return None

    def _rename_input(self, key: str, value: Any) -> str:
        code = int(key.rsplit(".", 1)[-1], 16)
        name = str(value or "").strip()
        if name:
            self._input_names[code] = name
        else:
            self._input_names.pop(code, None)
        self._save_input_names()
        # The label appears inside the Input Source choices, so the page has to be rebuilt for
        # the rename to show up where it matters.
        self._rebuild()
        self._values[key] = name
        return name

    # ------------------------------------------------------------------ storage

    def _load_calibration(self) -> dict[int, Range]:
        entry = _load_store("calibration.json").get(self._store_key, {})
        out: dict[int, Range] = {}
        for code, r in entry.items():
            try:
                out[int(code)] = Range(r["minimum"], r["maximum"], r["step"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _save_calibration(self) -> None:
        data = _load_store("calibration.json")
        data[self._store_key] = {
            str(code): {"minimum": r.minimum, "maximum": r.maximum, "step": r.step}
            for code, r in self._ranges.items()
        }
        _save_store("calibration.json", data)

    def _load_input_names(self) -> dict[int, str]:
        entry = _load_store("input_names.json").get(self._store_key, {})
        out: dict[int, str] = {}
        for code, name in entry.items():
            try:
                out[int(code)] = str(name)
            except (TypeError, ValueError):
                continue
        return out

    def _save_input_names(self) -> None:
        data = _load_store("input_names.json")
        data[self._store_key] = {str(code): name for code, name in self._input_names.items()}
        _save_store("input_names.json", data)

    # ------------------------------------------------------------------ errors

    def _translate(self, exc: DDCError) -> Exception:
        """A dead bus is ``Unreachable`` ("switch DDC/CI on"); anything else is a device error.

        The distinction is the difference between an actionable sentence and a traceback: a
        monitor with DDC/CI disabled in its menu is the single most common way this fails, and
        it is not a bug to report.

        A *missing ddcutil* is neither. It came back as a plain device error, which the shell
        then wrapped in "switch it on, then Rescan" -- advice that cannot help, for a monitor that
        is working fine. ``DependencyMissing`` is shown verbatim instead, so the sentence names the
        package.
        """
        text = str(exc).casefold()
        if "not installed" in text or "not in path" in text:
            return DependencyMissing(
                "Monitor control needs app-misc/ddcutil, which is not installed or is not on "
                "PATH. The rest of the application is unaffected."
            )
        if any(t in text for t in _UNREACHABLE):
            return Unreachable(
                "not reachable over DDC/CI — check that DDC/CI is enabled in the monitor's menu "
                "and that you can run `ddcutil detect` without sudo"
            )
        return DeviceError(str(exc) or "the monitor rejected the request")


def _word(reading: VcpReading) -> int:
    """The full 16-bit SH:SL value of a complex reading; ``.value`` is only the low byte."""
    raw = reading.raw_bytes
    if raw and len(raw) >= 2:
        return (raw[-2] << 8) | raw[-1]
    return reading.value


__all__ = ["DellMonitor"]
