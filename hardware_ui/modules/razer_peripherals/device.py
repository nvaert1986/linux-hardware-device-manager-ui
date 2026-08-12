"""The Razer adapter: capability keys in, OpenRazer client calls out.

**OpenRazer is a requirement of this module, not of the application.** Nothing outside this file
imports it, and this file is loaded only when the user opens a Razer device -- so an installation
without OpenRazer runs normally and simply cannot use this module. Missing pieces are reported as
something to install, never as a traceback.

The daemon owns the hardware. This module does not touch hidraw, does not load kernel drivers and
does not duplicate anything OpenRazer already does; it is a second client of a supported API,
alongside Polychromatic.

Three failure modes were hit while probing two devices on one desk, and every access here is
wrapped for all of them:

* ``NotImplementedError`` -- properties are declared on the base class and raise when the device
  does not implement them, so ``hasattr`` is meaningless and ``has()`` is the only valid gate
* ``dbus.exceptions.DBusException`` -- reading ``logo.active`` answers ``UnknownMethod``; this is
  not a Python-level error, so catching ``NotImplementedError`` alone is not enough
* ``AttributeError`` -- an advertised zone may not exist under ``fx.misc`` at all
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
from typing import Any

from hardware_ui.core import (
    CapabilitySet,
    DependencyMissing,
    Device,
    DeviceError,
    DeviceInfo,
    NotSupported,
    Unreachable,
)
from hardware_ui.core.paths import config_dir, ensure

from . import capabilities as C

log = logging.getLogger(__name__)

MODULE_ID = "razer_peripherals"

PHOTO_MAX_BYTES = 8 * 1024 * 1024
"""Matches ``core.photos.MAX_BYTES``. A remote file is read to a bound, never to exhaustion."""

INSTALL_HINT = (
    "OpenRazer is needed for Razer devices and is not installed.\n\n"
    "On Gentoo:  emerge sys-apps/openrazer-daemon\n"
    "Then add yourself to the 'plugdev' group and log back in.\n\n"
    "Nothing else in this application needs it."
)
DAEMON_HINT = (
    "The OpenRazer daemon is not answering. Start it with 'openrazer-daemon' (it usually "
    "autostarts on login), then press Connect again."
)

IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Model"),
    ("type", "Kind"),
    ("serial", "Serial number"),
    ("firmware_version", "Firmware"),
    ("keyboard_layout", "Keyboard layout"),
)


def _errors() -> tuple[type[BaseException], ...]:
    """Everything an OpenRazer access can raise. See the module docstring."""
    errs: list[type[BaseException]] = [NotImplementedError, AttributeError, KeyError, ValueError]
    try:
        import dbus.exceptions

        errs.append(dbus.exceptions.DBusException)
    except ImportError:  # pragma: no cover - dbus ships with the client
        pass
    return tuple(errs)


def _safe(fn, default=None):
    """Read something from OpenRazer, or return *default*. Never let its errors escape."""
    try:
        return fn()
    except _errors():
        return default


class RazerDevice(Device):
    """One Razer peripheral, through the OpenRazer daemon."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._lock = asyncio.Lock()
        self._rdevice: Any = None
        self._set = CapabilitySet()
        self._values: dict[str, Any] = {}
        self._zones: list[tuple[str, list[str], bool]] = []
        self._stages: list[tuple[int, int]] = []
        self._macros: dict[str, list] = {}

    @property
    def capabilities(self) -> CapabilitySet:
        return self._set

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            from openrazer.client import DeviceManager
        except ImportError as exc:
            raise DependencyMissing(INSTALL_HINT) from exc

        try:
            manager = DeviceManager()
            devices = list(manager.devices)
        except Exception as exc:  # noqa: BLE001 - the daemon's failures are not a taxonomy
            raise DependencyMissing(DAEMON_HINT) from exc

        self._rdevice = self._match(devices)
        if self._rdevice is None:
            raise Unreachable(
                "OpenRazer does not list this device. It may not be supported by the installed "
                "version, or the daemon may need restarting after plugging it in."
            )
        self._rebuild()
        # Opt-in only: this writes to the keyboard, which should never happen because the user
        # merely opened a page.
        if self._has("macro_logic") and self._autorestore() and self._load_macros():
            with contextlib.suppress(Exception):
                self._restore_macros()

    def _match(self, devices: list[Any]) -> Any:
        """Find our device in the daemon's list.

        **USB product id first**, because it is the only field both sides agree on. The obvious
        keys both fail here, measured on this desk:

        * *hidraw node* -- one USB device exposes several (seven for two Razer devices), and the
          daemon never says which it took.
        * *serial* -- the daemon has one (``IO1725F08902201``); the sysfs HID walk reports an
          empty string, so there is nothing to compare.
        * *name* -- the daemon says ``Razer BlackWidow Chroma V2``, sysfs says
          ``Razer Razer BlackWidow Chroma V2``. The vendor prefix is doubled.

        ``_pid``/``_vid`` are private on the client object, but they are the only identity it
        exposes and Polychromatic reads the same pair. Name matching stays as a fallback, with the
        duplicated prefix normalised away.
        """
        pid, vid = self.info.product_id, self.info.vendor_id
        if pid is not None:
            for device in devices:
                if _safe(lambda d=device: int(d._pid)) == pid and (
                    vid is None or _safe(lambda d=device: int(d._vid)) in (vid, None)
                ):
                    return device

        serial = (self.info.serial or "").strip()
        if serial:
            for device in devices:
                if _safe(lambda d=device: str(d.serial).strip()) == serial:
                    return device

        want = _normalise(self.info.name)
        for device in devices:
            if _normalise(str(_safe(lambda d=device: d.name) or "")) == want:
                return device
        # A single Razer device and nothing else to go on: the only candidate beats refusing.
        return devices[0] if len(devices) == 1 else None

    async def disconnect(self) -> None:
        # The daemon owns the hardware and holds nothing open on our behalf.
        self._rdevice = None

    # ------------------------------------------------------------------ discovery

    def _has(self, capability: str) -> bool:
        return bool(_safe(lambda: self._rdevice.has(capability), False))

    def _zone_object(self, zone: str) -> Any:
        """The ``fx`` object for a zone, or ``None`` if the device does not really have it."""
        if zone == "main":
            return getattr(self._rdevice, "fx", None)
        attr = C.ZONE_ATTRS.get(zone, zone)
        return _safe(lambda: getattr(self._rdevice.fx.misc, attr))

    def _discover_zones(self) -> list[tuple[str, list[str], bool]]:
        found: list[tuple[str, list[str], bool]] = []
        for zone in ("main", *C.ZONE_ATTRS):
            prefix = "" if zone == "main" else f"{zone}_"
            effects = [e.name for e in C.EFFECTS if self._has(f"lighting_{prefix}{e.name}")]
            brightness = self._has(
                "brightness" if zone == "main" else f"lighting_{zone}_brightness"
            )
            if not effects and not brightness:
                continue
            if self._zone_object(zone) is None:
                # Advertised but absent -- exactly what `lighting_scroll_*` versus `scroll_wheel`
                # produces on a DeathAdder V2 when the attribute name is guessed.
                log.info("%s: zone %r advertised but not present under fx", self.info.name, zone)
                continue
            found.append((zone, effects, brightness))
        return found

    def _rebuild(self) -> None:
        self._zones = self._discover_zones()
        identity = [(f, label) for f, label in IDENTITY_FIELDS if _safe(
            lambda f=f: getattr(self._rdevice, f)) is not None]
        # `available_dpi` raises NotImplementedError on devices that report has("dpi") true, so
        # gate on the capability rather than on the attribute existing.
        fixed = _safe(lambda: self._rdevice.available_dpi) if self._has("available_dpi") else None
        stages = _safe(lambda: self._rdevice.dpi_stages) if self._has("dpi_stages") else None
        self._macros = self._read_macros()
        max_dpi = _safe(lambda: int(self._rdevice.max_dpi)) if self._has("dpi") else None
        self._stages = self._load_stages(max_dpi)
        self._set = C.build(
            zones=self._zones,
            stages=self._stages,
            can_sync=self._has("dpi_stages"),
            dpi_max=_safe(lambda: int(self._rdevice.max_dpi)) if self._has("dpi") else None,
            dpi_fixed=list(fixed or ()),
            dpi_stages=_stage_list(stages),
            poll_rates=self._poll_rates(),
            game_mode=self._has("game_mode_led"),
            macro_led=self._has("macro_mode_led") and not self._has("macro_logic"),
            battery=self._has("battery"),
            extra=[k for k, cap in C.EXTRA_TOGGLES.items() if self._has(cap)],
            identity=identity,
        )
        if self._has("macro_logic"):
            # The macro tab is appended rather than built inside C.build: it is the one part that
            # depends on daemon state (which keys currently hold a macro) rather than on the
            # device's capabilities.
            self._set = CapabilitySet(
                list(self._set)
                + C.macros(
                    [(k, len(v)) for k, v in sorted(self._macros.items())],
                    has_modifier=self._has("macro_mode_modifier"),
                    has_led=self._has("macro_mode_led"),
                    has_saved=bool(self._load_macros()),
                )
            )
        self._read_values(identity)
        self._bump_capabilities()

    def _poll_rates(self) -> tuple[int, ...]:
        """The rates this mouse can actually do.

        Gated on ``has("supported_poll_rates")``, which OpenRazer added in 3.2.0. A DeathAdder V2
        reports ``has("poll_rate")`` true and ``has("supported_poll_rates")`` **false**, and
        reading the attribute anyway raises. Falling back to every rate the client defines offered
        8000 Hz on a 1000 Hz mouse -- a setting that cannot work. Polychromatic falls back to
        125/500/1000, which is what OpenRazer hardcoded before it exposed the list, and that is
        the honest floor.

        The device's own current rate is always included, so a mouse doing something outside the
        fallback never shows a value it cannot select.
        """
        if not self._has("poll_rate"):
            return ()
        rates: tuple[int, ...] = ()
        if self._has("supported_poll_rates"):
            reported = _safe(lambda: tuple(int(r) for r in self._rdevice.supported_poll_rates))
            rates = reported or ()
        if not rates:
            rates = C.POLL_RATES_FALLBACK
        current = _safe(lambda: int(self._rdevice.poll_rate))
        if current is not None and current not in rates:
            rates = tuple(sorted({*rates, current}))
        return rates

    def _read_values(self, identity: list[tuple[str, str]]) -> None:
        values: dict[str, Any] = {}
        for field, _label in identity:
            values[f"info.{field}"] = _safe(lambda f=field: str(getattr(self._rdevice, f)))
        if self._has("dpi"):
            dpi = _safe(lambda: self._rdevice.dpi)
            # dpi is an (x, y) tuple, not a scalar, and both axes are settable.
            pair = _sane_dpi(dpi)
            if pair is not None:
                values[C.DPI_KEY] = pair[0]
                values[C.DPI_X_KEY], values[C.DPI_Y_KEY] = pair
                # Start locked when the axes already match, which is how they ship and how
                # Polychromatic decides the same checkbox.
                values.setdefault(C.DPI_LOCK_KEY, pair[0] == pair[1])
        if self._has("dpi_stages"):
            stages = _safe(lambda: self._rdevice.dpi_stages)
            if isinstance(stages, (tuple, list)) and stages:
                values[C.DPI_STAGE_KEY] = int(stages[0])
        if self._has("macro_logic"):
            names = ", ".join(sorted(self._macros)) or "None recorded"
            values[C.MACRO_STATUS_KEY] = names
            values[C.MACRO_AUTORESTORE_KEY] = self._autorestore()
            if self._has("macro_mode_modifier"):
                values[C.MACRO_MODIFIER_KEY] = bool(
                    _safe(lambda: self._rdevice.macro.mode_modifier, False)
                )
        for index, (x, y) in enumerate(self._stages, start=1):
            values[C.stage_key(index, "x")] = x
            values[C.stage_key(index, "y")] = y
        if self._has("poll_rate"):
            values[C.POLL_RATE_KEY] = _safe(lambda: int(self._rdevice.poll_rate))
        if self._has("game_mode_led"):
            values[C.GAME_MODE_KEY] = bool(_safe(lambda: self._rdevice.game_mode_led, False))
        if self._has("macro_mode_led"):
            values[C.MACRO_LED_KEY] = bool(_safe(lambda: self._rdevice.macro_mode_led, False))
        for key, cap in C.EXTRA_TOGGLES.items():
            if self._has(cap):
                attr = C.EXTRA_ATTRS.get(key, cap)
                values[key] = bool(_safe(lambda a=attr: getattr(self._rdevice, a), False))
        if self._has("battery"):
            level = _safe(lambda: int(self._rdevice.battery_level))
            if level is not None:
                values[C.BATTERY_KEY] = level
            values[C.CHARGING_KEY] = (
                "Yes" if _safe(lambda: self._rdevice.is_charging, False) else "No"
            )
            idle = _safe(lambda: int(self._rdevice.get_idle_time()))
            if idle is not None:
                values[C.IDLE_TIME_KEY] = max(1, round(idle / 60))
            low = _safe(lambda: int(self._rdevice.get_low_battery_threshold()))
            if low is not None:
                values[C.LOW_BATTERY_KEY] = low

        for zone, effects, has_brightness in self._zones:
            obj = self._zone_object(zone)
            if effects:
                # `effect` is readable and reports the active effect by name; `active` is the
                # trap next to it -- absent on a keyboard's fx, a D-Bus UnknownMethod on a
                # mouse's logo zone.
                current = _safe(lambda o=obj: str(o.effect))
                if current in C.EFFECT_BY_NAME:
                    values[C.effect_key(zone)] = current
                # `colors`, `speed` and `wave_dir` are all readable, so every argument control
                # shows what the device is actually doing rather than starting blank. `colors` is
                # a 9-byte blob: three RGB triplets, of which the effects here use the first two.
                blob = _safe(lambda o=obj: bytes(o.colors)) or b""
                for index in (1, 2, 3):
                    start = (index - 1) * 3
                    if len(blob) >= start + 3:
                        r, g, b = blob[start:start + 3]
                        values[C.colour_key(zone, index)] = f"#{r:02x}{g:02x}{b:02x}"
                speed = _safe(lambda o=obj: int(o.speed))
                if speed in {v for v, _ in C.SPEEDS}:
                    values[C.speed_key(zone)] = speed
                direction = _safe(lambda o=obj: int(o.wave_dir))
                if direction in {v for v, _ in C.WAVE_DIRECTIONS}:
                    values[C.direction_key(zone)] = direction
            if has_brightness:
                source = self._rdevice if zone == "main" else obj
                level = _safe(lambda s=source: float(s.brightness))
                if level is not None:
                    values[C.brightness_key(zone)] = round(level)
        self._values.update(values)

    async def get(self, key: str) -> Any:
        if key not in self._values:
            raise NotSupported(key)
        return self._values[key]

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        wanted = set(keys)
        return {k: v for k, v in self._values.items() if k in wanted}

    async def refresh(self) -> dict[str, Any]:
        """Cheap: the daemon holds the state and answers over D-Bus, no hardware round-trip."""
        async with self._lock:
            if self._rdevice is None:
                return {}
            before = dict(self._values)
            await asyncio.to_thread(
                self._read_values, [(f, label) for f, label in IDENTITY_FIELDS
                                    if f"info.{f}" in self._values]
            )
            return {k: v for k, v in self._values.items() if before.get(k) != v}

    # ------------------------------------------------------------------ writing

    async def set(self, key: str, value: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._set_sync, key, value)

    def _set_sync(self, key: str, value: Any) -> Any:
        if self._rdevice is None:
            raise Unreachable("not connected")
        try:
            if key == C.DPI_LOCK_KEY:
                self._values[key] = bool(value)
                if value:
                    # Locking makes the axes equal immediately, taking the horizontal as the
                    # one the user means -- leaving them apart would make the lock a lie.
                    self._write_dpi(C.DPI_X_KEY, int(self._values.get(C.DPI_X_KEY) or 800))
                return bool(value)
            if key in (C.DPI_KEY, C.DPI_X_KEY, C.DPI_Y_KEY):
                return self._write_dpi(key, int(value))
            if key == C.DPI_STAGE_KEY:
                return self._write_dpi_stage(int(value))
            if key == C.POLL_RATE_KEY:
                self._rdevice.poll_rate = int(value)
                return int(_safe(lambda: self._rdevice.poll_rate, value))
            if key == C.GAME_MODE_KEY:
                self._rdevice.game_mode_led = bool(value)
                return bool(_safe(lambda: self._rdevice.game_mode_led, value))
            if key == C.MACRO_LED_KEY:
                self._rdevice.macro_mode_led = bool(value)
                return bool(_safe(lambda: self._rdevice.macro_mode_led, value))
            if key == C.IDLE_TIME_KEY:
                self._rdevice.set_idle_time(int(value) * 60)
                return int(value)
            if key == C.LOW_BATTERY_KEY:
                self._rdevice.set_low_battery_threshold(int(value))
                return int(value)
            if key == C.MACRO_REFRESH_KEY:
                self._macros = self._read_macros()
                self._rebuild()
                return None
            if key == C.MACRO_SAVE_KEY:
                self._save_macros()
                return None
            if key == C.MACRO_RESTORE_KEY:
                return self._restore_macros()
            if key == C.MACRO_AUTORESTORE_KEY:
                self._values[key] = bool(value)
                self._set_autorestore(bool(value))
                return bool(value)
            if key == C.MACRO_EXPORT_KEY:
                return self._export_macros(str(value))
            if key == C.MACRO_IMPORT_KEY:
                return self._import_macros(str(value))
            if key == C.MACRO_MODIFIER_KEY:
                self._rdevice.macro.mode_modifier = bool(value)
                return bool(value)
            if key.startswith(C.MACRO_DELETE_PREFIX):
                return self._delete_macro(C.macro_bind_key(key))
            if key == C.STAGE_APPLY_KEY:
                return self._apply_stage(int(value))
            if key == C.STAGE_SYNC_KEY:
                return self._sync_stages()
            if C.stage_of(key) is not None:
                return self._edit_stage(key, int(value))
            if key in C.EXTRA_TOGGLES:
                attr = C.EXTRA_ATTRS.get(key, C.EXTRA_TOGGLES[key])
                setattr(self._rdevice, attr, bool(value))
                return bool(_safe(lambda a=attr: getattr(self._rdevice, a), value))
            if key.startswith("light."):
                return self._write_lighting(key, value)
        except _errors() as exc:
            raise DeviceError(f"OpenRazer refused that: {exc}") from exc
        raise NotSupported(key)

    def _write_dpi(self, key: str, value: int) -> int:
        """DPI is one property holding an (x, y) pair, so both axes go out together.

        Whichever slider moved supplies its axis and the other comes from current state -- writing
        only the touched one would reset the other to whatever this object last saw.
        """
        x = self._values.get(C.DPI_X_KEY) or value
        y = self._values.get(C.DPI_Y_KEY) or value
        locked = bool(self._values.get(C.DPI_LOCK_KEY, True))
        if key == C.DPI_KEY or locked:
            x = y = value
        elif key == C.DPI_X_KEY:
            x = value
        else:
            y = value
        self._rdevice.dpi = (int(x), int(y))
        landed = _sane_dpi(_safe(lambda: self._rdevice.dpi))
        if landed is None:
            # The daemon occasionally answers 0 immediately after a write. Zero is not a DPI any
            # mouse can hold, so it is a transient read, not a value -- publishing it would put a
            # 0 in front of the user for a setting that applied correctly.
            log.debug("implausible DPI read-back after write; keeping the requested value")
            self._values[C.DPI_X_KEY], self._values[C.DPI_Y_KEY] = int(x), int(y)
            self._values[C.DPI_KEY] = int(x)
            return self._values[key]
        self._values[C.DPI_X_KEY], self._values[C.DPI_Y_KEY] = landed
        self._values[C.DPI_KEY] = landed[0]
        return self._values[key]

    # -- product photo ---------------------------------------------------------------------

    async def fetch_photo(self) -> bytes | None:
        return await asyncio.to_thread(self._fetch_photo_sync)

    def _fetch_photo_sync(self) -> bytes | None:
        """Download the product photo from the URL OpenRazer advertises for this device.

        OpenRazer stores no images -- it carries a URL per device, pointing at Razer's own asset
        host, and Polychromatic uses the same field. That is the rule this project already follows
        for photos: **the vendor's own advertised link, on explicit request**, never a guessed CDN
        pattern and never a redistributed image. Nothing is shipped, and nothing is fetched until
        the user asks.
        """
        import urllib.request

        url = _safe(lambda: str(self._rdevice.device_image)) or ""
        if not url:
            # OpenRazer <= 2.8 carried the same thing under a deprecated dict.
            urls = _safe(lambda: dict(self._rdevice.razer_urls)) or {}
            url = str(urls.get("top_img") or "")
        if not url.startswith("https://"):
            # Refuse plain http: this is an unattended download of a remote file.
            log.debug("%s: no usable image URL (%r)", self.info.name, url)
            return None

        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url, headers={"User-Agent": "hardware-ui"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                if response.status != 200:
                    return None
                payload = response.read(PHOTO_MAX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 - reported to the user as "could not reach"
            log.info("%s: photo download failed: %s", self.info.name, exc)
            return None
        if not payload or len(payload) > PHOTO_MAX_BYTES:
            log.info("%s: photo was empty or larger than the cap", self.info.name)
            return None
        return payload

    # -- macros ----------------------------------------------------------------------------

    def _macro_dbus(self):
        macro = getattr(self._rdevice, "macro", None)
        return getattr(macro, "_macro_dbus", None) if macro is not None else None

    def _read_macros(self) -> dict[str, list]:
        """What the daemon currently holds, as plain JSON structures.

        The raw D-Bus payload is used rather than the client's ``MacroObject`` wrappers, because
        it round-trips exactly: ``getMacros`` returns JSON and ``addMacro`` takes JSON, so nothing
        has to be reconstructed and no macro type can be lost in translation.
        """
        bus = self._macro_dbus()
        if bus is None or not self._has("macro_logic"):
            return {}
        raw = _safe(lambda: str(bus.getMacros()))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _macros_path(self):
        return ensure(config_dir() / MODULE_ID) / "macros.json"

    def _load_macros(self) -> dict[str, list]:
        try:
            return json.loads(self._macros_path().read_text()).get(self._stage_id(), {}) or {}
        except (OSError, ValueError):
            return {}

    def _save_macros(self) -> None:
        """Keep a copy of what the daemon holds. It keeps none of its own."""
        path = self._macros_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data[self._stage_id()] = self._read_macros()
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except OSError:
            log.warning("could not save macros to %s", path, exc_info=True)
        self._rebuild()

    def _restore_macros(self) -> int:
        """Feed the saved macros back to the daemon. Returns how many were restored."""
        bus = self._macro_dbus()
        saved = self._load_macros()
        if bus is None or not saved:
            raise NotSupported("No saved macros for this keyboard")
        restored = 0
        for bind_key, sequence in saved.items():
            try:
                bus.addMacro(str(bind_key), json.dumps(sequence))
                restored += 1
            except Exception:  # noqa: BLE001 - one bad macro must not lose the rest
                log.warning("could not restore the macro on %s", bind_key, exc_info=True)
        self._macros = self._read_macros()
        self._rebuild()
        return restored

    def _export_macros(self, path: str) -> int:
        """Write the daemon's macros to a file the user chose. Returns how many were written."""
        macros = self._read_macros()
        if not macros:
            raise NotSupported("There are no macros to export")
        payload = {
            "format": "hardware-ui/razer-macros",
            "version": 1,
            "device": str(_safe(lambda: self._rdevice.name) or self.info.name),
            "serial": self._stage_id(),
            "macros": macros,
        }
        try:
            pathlib.Path(path).write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            raise DeviceError(f"Could not write {path}: {exc}") from exc
        return len(macros)

    def _import_macros(self, path: str) -> int:
        """Load macros from a file and put them on the keyboard.

        Accepts a file this application exported and a bare ``{bind_key: [...]}`` object alike,
        so a hand-written or hand-edited file works.
        """
        try:
            data = json.loads(pathlib.Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise DeviceError(f"Could not read {path}: {exc}") from exc
        macros = data.get("macros") if isinstance(data, dict) else None
        if macros is None and isinstance(data, dict):
            macros = {k: v for k, v in data.items() if isinstance(v, list)}
        if not isinstance(macros, dict) or not macros:
            raise DeviceError("That file contains no macros")

        bus = self._macro_dbus()
        if bus is None:
            raise NotSupported("This keyboard does not support macros")
        added = 0
        for bind_key, sequence in macros.items():
            try:
                bus.addMacro(str(bind_key), json.dumps(sequence))
                added += 1
            except Exception:  # noqa: BLE001 - one bad entry must not lose the rest
                log.warning("could not import the macro for %s", bind_key, exc_info=True)
        if not added:
            raise DeviceError("None of the macros in that file could be applied")
        self._macros = self._read_macros()
        self._rebuild()
        return added

    def _delete_macro(self, bind_key: str) -> None:
        bus = self._macro_dbus()
        if bus is None:
            raise NotSupported(bind_key)
        bus.deleteMacro(str(bind_key))
        self._macros = self._read_macros()
        self._rebuild()
        return None

    # -- saved DPI stages ------------------------------------------------------------------

    def _autorestore(self) -> bool:
        try:
            data = json.loads(self._macros_path().read_text())
        except (OSError, ValueError):
            return False
        return bool(data.get("_autorestore", {}).get(self._stage_id(), False))

    def _set_autorestore(self, enabled: bool) -> None:
        path = self._macros_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("_autorestore", {})[self._stage_id()] = enabled
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except OSError:
            log.warning("could not save the autorestore flag", exc_info=True)

    def _stages_path(self):
        return ensure(config_dir() / MODULE_ID) / "dpi_stages.json"

    def _stage_id(self) -> str:
        """Keyed by serial, so stages follow the mouse rather than the USB port."""
        serial = str(_safe(lambda: self._rdevice.serial) or "").strip()
        return serial or str(_safe(lambda: self._rdevice.name) or self.info.name)

    def _load_stages(self, max_dpi: int | None) -> list[tuple[int, int]]:
        if not max_dpi:
            return []
        try:
            saved = json.loads(self._stages_path().read_text()).get(self._stage_id())
        except (OSError, ValueError):
            saved = None
        if saved:
            out = [
                (int(p[0]), int(p[1]))
                for p in saved
                if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
            if out:
                return out[: C.MAX_STAGES]
        return C.default_dpi_stages(max_dpi)

    def _save_stages(self) -> None:
        path = self._stages_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data[self._stage_id()] = [[x, y] for x, y in self._stages]
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except OSError:
            log.warning("could not save DPI stages to %s", path, exc_info=True)

    def _edit_stage(self, key: str, value: int) -> int:
        """Change one axis of one saved stage. Ours to keep -- nothing is sent to the mouse."""
        parsed = C.stage_of(key)
        if parsed is None:
            raise NotSupported(key)
        index, axis = parsed
        if not 1 <= index <= len(self._stages):
            raise NotSupported(key)
        x, y = self._stages[index - 1]
        self._stages[index - 1] = (value, y) if axis == "x" else (x, value)
        self._values[key] = value
        self._save_stages()
        # The "Use stage" labels quote the values, so the page is rebuilt to keep them true.
        self._rebuild()
        return value

    def _apply_stage(self, index: int) -> int:
        if not 1 <= index <= len(self._stages):
            raise NotSupported(C.STAGE_APPLY_KEY)
        x, y = self._stages[index - 1]
        self._rdevice.dpi = (int(x), int(y))
        landed = _sane_dpi(_safe(lambda: self._rdevice.dpi)) or (int(x), int(y))
        self._values[C.DPI_X_KEY], self._values[C.DPI_Y_KEY] = landed
        self._values[C.DPI_KEY] = landed[0]
        return index

    def _sync_stages(self) -> None:
        """Write the stages onto the mouse, so its own DPI buttons cycle them.

        Only possible where ``dpi_stages`` exists. A DeathAdder V2 does not have it -- the daemon
        exposes only ``getDPI``/``setDPI``/``maxDPI`` for it -- and Polychromatic greys its Sync
        button for exactly the same reason.
        """
        if not self._has("dpi_stages"):
            raise NotSupported(
                "This mouse cannot store DPI stages, so its DPI buttons cannot be changed."
            )
        self._rdevice.dpi_stages = (1, [(int(x), int(y)) for x, y in self._stages])
        return None

    def _write_dpi_stage(self, stage: int) -> int:
        """Select which hardware DPI stage is active.

        The setter takes ``(active_stage, [(x, y), ...])`` -- the whole list, not just an index --
        so the existing stages are read back and re-sent unchanged. Untested: no device here
        reports the capability.
        """
        stages = _safe(lambda: self._rdevice.dpi_stages)
        pairs = _stage_list(stages)
        if not pairs:
            raise NotSupported(C.DPI_STAGE_KEY)
        self._rdevice.dpi_stages = (int(stage), [(int(x), int(y)) for x, y in pairs])
        return stage

    def _write_lighting(self, key: str, value: Any) -> Any:
        zone = C.zone_of(key)
        obj = self._zone_object(zone)
        if obj is None:
            raise NotSupported(key)

        if key == C.brightness_key(zone):
            target = self._rdevice if zone == "main" else obj
            target.brightness = float(value)
            self._values[key] = round(float(value))
            return self._values[key]

        # Effect, colour, speed and direction all end in the same place: the effect has to be
        # re-applied with its full argument list, because OpenRazer has no "change one argument"
        # call. Store the new value first, then replay.
        self._values[key] = value
        self._apply_effect(zone)
        return value

    def _apply_effect(self, zone: str) -> None:
        """Invoke the zone's current effect with the arguments currently selected."""
        obj = self._zone_object(zone)
        name = self._values.get(C.effect_key(zone))
        effect = C.EFFECT_BY_NAME.get(str(name))
        if obj is None or effect is None:
            return
        fn = getattr(obj, effect.name, None)
        if fn is None:
            raise NotSupported(f"{zone} does not support {effect.name}")

        args: list[Any] = []
        for index in (1, 2, 3):
            if effect.colours >= index:
                args += list(_rgb(self._values.get(C.colour_key(zone, index)) or "#00ff00"))
        if effect.direction:
            args.append(int(self._values.get(C.direction_key(zone)) or C.WAVE_DIRECTIONS[0][0]))
        if effect.speed:
            args.append(int(self._values.get(C.speed_key(zone)) or C.SPEEDS[1][0]))
        fn(*args)


def _sane_dpi(value: Any) -> tuple[int, int] | None:
    """An ``(x, y)`` pair, or ``None`` if it is not a DPI a mouse could hold.

    Zero is the case that matters: the daemon can answer ``(0, 0)`` transiently right after a
    write, and treating that as the value shows the user a 0 for a setting that applied.
    """
    if not isinstance(value, (tuple, list)) or len(value) < 2:
        return None
    try:
        x, y = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return (x, y) if x > 0 and y > 0 else None


def _stage_list(stages: Any) -> list[tuple[int, int]]:
    """``(active, [(x, y), ...])`` -> the pair list, tolerating anything unexpected."""
    if not isinstance(stages, (tuple, list)) or len(stages) < 2:
        return []
    out: list[tuple[int, int]] = []
    for pair in stages[1] or ():
        if isinstance(pair, (tuple, list)) and len(pair) >= 2:
            out.append((int(pair[0]), int(pair[1])))
    return out


def _normalise(name: str) -> str:
    """Collapse a doubled vendor prefix so sysfs and the daemon agree.

    sysfs concatenates the USB manufacturer and product strings, and Razer puts the brand in
    both -- "Razer" + "Razer DeathAdder V2".
    """
    words = name.strip().casefold().split()
    while len(words) > 1 and words[0] == words[1]:
        words.pop(0)
    return " ".join(words)


def _rgb(colour: str) -> tuple[int, int, int]:
    """``#rrggbb`` -> ``(r, g, b)``. Tolerates a missing hash and bad input."""
    text = str(colour).lstrip("#")
    if len(text) != 6:
        return (0, 255, 0)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (0, 255, 0)


__all__ = ["RazerDevice"]
