"""A Jabra headset as the shell sees it.

Thin on purpose. Everything that hardware taught lives in ``controller.py`` and the ``protocol``
package, ported from the standalone project that found it; this file is the adapter between that
blocking, synchronous stack and the shell's async :class:`~hardware_ui.core.device.Device`.

**Connecting is a probe, and the probe is the slow part.** The catalogue describes GN Audio's whole
range, not this headset, so what a given device supports is discovered by reading it: ~283
candidate properties, of which a Link 390 answers 69. That costs about 25 seconds the first time.
It is cached per (product id, firmware) so every later connect reads only what exists, which is
what turns 25 seconds into about 8. The cache is versioned against the catalogue's size, so a
catalogue update re-probes rather than hiding newly-supported properties.

**Nothing is polled.** ``configChangeEvents`` is armed at connect and the device pushes a list of
changed subcommands, which are resolved back to property names and re-read. That covers changes
made with the headset's own buttons without putting any traffic on an idle link.

**Both endpoints are probed.** The link fronts the adapter and the headset, and they answer
different property sets -- ``radioPower`` is the adapter's, ``ancMode`` the headset's. Each gets its
own probe, its own cache entry (keyed by its own product id and firmware) and its own section.

**The equalizer is read and written as a whole table.** One message carries every band, and each
band's opaque ``A`` field must be written back unchanged, so a single-band write does not exist at
the protocol level: the sliders share a ``writes_with`` group and one drag is one write.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator
from typing import Any

from hardware_ui.core import (
    Advisory,
    CapabilitySet,
    CapabilityValue,
    DependencyMissing,
    Device,
    DeviceError,
    DeviceInfo,
    Kind,
    NotSupported,
    Unreachable,
)
from hardware_ui.core.connection import ConnectionLabel

from . import capabilities as C
from . import capability_cache, categories, labels, photos
from .controller import (
    EARCUP_PREFIX,
    MODE_KEY,
    PHONE_MUTE_KEY,
    PROBE_TIMEOUT,
    JabraDevice,
    ValueRejected,
    VerificationFailed,
    WriteRefused,
)
from .equalizer import Equalizer
from .protocol.catalogue import Catalogue, CatalogueMissing
from .protocol.catalogue import load as load_catalogue
from .protocol.hid import DeviceBusyError, HidTransportError, find_devices
from .protocol.session import Endpoint, GnpError

log = logging.getLogger(__name__)

#: How often the change stream drains pushed events. A drain sends nothing -- it is a non-blocking
#: read of what the device already delivered -- so this costs no link traffic.
POLL_SECONDS = 0.5


class JabraHeadsetDevice(Device):
    """One Jabra link, presented as the headset behind it."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._jabra: JabraDevice | None = None
        self._catalogue: Catalogue | None = None
        self._capabilities = CapabilitySet([])
        self._values: dict[str, Any] = {}
        self._supported: list[str] = []
        self._relay_supported: list[str] = []
        self._relay: Endpoint | None = None
        self._equalizer: Equalizer | None = None
        self._armed: set[str] = set()
        self._earcups: dict[str, bool] = {}
        self._advisories: dict[str, Advisory] = {}
        #: What this model refuses, remembered across connects -- see ``capability_cache``.
        self._limits: dict = {"rejected": {}, "bounds": {}, "locked": []}
        self._limits_key: tuple[int | None, str] = (None, "")
        self._lock = asyncio.Lock()

    #: Both endpoints are probed property by property, and a cold connect measured ~30 s each on a
    #: Link 390 + Evolve2 85. The shell's 60 s default would fail exactly the connect this module
    #: is slowest on -- the first one, where there is no cache to fall back to.
    connect_timeout = 240.0

    @property
    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    def connect_notice(self) -> str:
        """Say that the first connect is slow, because it looks identical to a hang.

        Shown when nothing is cached for this link. Jabra publishes one catalogue for their whole
        range and says nothing about which device has which property, so the only way to find out
        is to ask the hardware ~350 times -- and that is a minute of "Connecting…" with no
        explanation, which a reasonable person cancels.
        """
        if capability_cache.entries_for(self.info.product_id):
            return ""
        return (
            "First connection to this Jabra device: reading which settings it supports. "
            "This takes up to a minute and only happens once — please leave it connected."
        )

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            catalogue = load_catalogue()
        except CatalogueMissing as exc:
            # Not "unreachable": the headset is fine, the data is not here. The shell offers the
            # import for exactly this, and saying so is what makes that offer make sense.
            raise DependencyMissing(
                "Jabra's property catalogue has not been downloaded yet. Without it this headset "
                "can be identified but not configured."
            ) from exc

        jabra = JabraDevice(self._resolve_node(), catalogue_=catalogue)
        try:
            primary = jabra.connect()
        except DeviceBusyError as exc:
            raise Unreachable(str(exc)) from exc
        except (HidTransportError, GnpError, OSError) as exc:
            raise Unreachable(f"cannot reach this Jabra device: {exc}") from exc

        self._jabra = jabra
        self._catalogue = catalogue
        try:
            self._discover(primary)
        except Exception:
            # A half-built page is worse than none: close the link rather than leave the flock
            # held by a device the shell believes failed to open.
            with contextlib.suppress(Exception):
                jabra.close()
            self._jabra = None
            raise

    def _resolve_node(self):
        """The hidraw node carrying the GNP tunnel for this device.

        The enumerated node is not necessarily the right one, and its number is not stable:
        ``find_devices`` orders candidates by report size and non-accessory name precisely because
        ``/dev/hidrawN`` numbering swaps between a dongle and its charging stand on
        re-enumeration. Prefer the enumerated path when it is genuinely a GNP node, then any node
        of the same product, then the best candidate on the link.
        """
        try:
            found = find_devices()
        except OSError:
            return None
        if not found:
            return None
        wanted = self.info.path or ""
        for device in found:
            if device.path == wanted:
                return device
        for device in found:
            if self.info.product_id is not None and device.product_id == self.info.product_id:
                log.info("%s does not carry GNP; using %s", wanted, device.path)
                return device
        log.info("%s does not carry GNP; using %s", wanted, found[0].path)
        return found[0]

    async def disconnect(self) -> None:
        jabra, self._jabra = self._jabra, None
        if jabra is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(jabra.close)

    # ------------------------------------------------------------------ discovery

    def _discover(self, primary: Endpoint) -> None:
        """Work out what this device supports, and read it. Cached after the first time."""
        jabra = self._require()
        catalogue = self._catalogue
        assert catalogue is not None

        candidates = self._candidates(catalogue)
        cached = capability_cache.load(primary.product_id, primary.firmware, len(catalogue))

        if cached is not None:
            names = [n for n in cached if n in catalogue]
            values = jabra.get_many(names)
            self._supported = names
        else:
            log.info("probing %d properties on %s — first connect for this model",
                     len(candidates), primary)
            self._supported, values = jabra.probe_and_read(candidates, timeout=PROBE_TIMEOUT)
            capability_cache.save(primary.product_id, primary.firmware, len(catalogue),
                                  self._supported)

        self._values = _flatten(values, C.setting_key)
        self._values.update(self._identity(primary, values))
        self._values.update(self._read_relay(catalogue, primary, candidates))
        self._values.update(self._read_equalizer())
        battery = self._battery_level(values)
        if battery is not None:
            self._values[C.BATTERY_KEY] = battery
        for name, _caption in C.STATE_ROWS:
            self._values.setdefault(C.state_key(name), C.STATE_UNREPORTED)

        # Arm the change stream before the page appears, so a button press on the headset is
        # reflected from the first moment rather than after the first manual refresh.
        try:
            self._armed = jabra.arm_events(self._subscribable(catalogue))
        except Exception:  # noqa: BLE001 - a device that will not arm still has settings
            log.info("could not arm the change stream", exc_info=True)
            self._armed = set()

        self._limits_key = (primary.product_id, primary.firmware)
        self._limits = capability_cache.load_limits(*self._limits_key)

        peers = [str(e) for e in jabra.endpoints if e.address != primary.address]
        self._capabilities = C.build(
            catalogue,
            supported=self._supported,
            identity={k: str(v) for k, v in self._identity_raw(primary, values).items()},
            values=values,
            peers=peers,
            relay=(self._relay_supported, self._relay.name if self._relay else ""),
            bands=[band.label for band in self._equalizer.bands] if self._equalizer else [],
            has_battery=primary.looks_like_headset and self._battery_level(values) is not None,
            states=self._state_rows(primary),
        )
        self._apply_limits()
        self._format_readouts()
        self._bump_capabilities()

    def _apply_limits(self) -> None:
        """Narrow the page by what this model has already been found to refuse.

        Applied at connect so a control never offers a value again once the hardware has rejected
        it. Without this the learning is thrown away on disconnect and the same wall is hit every
        session -- which is how it behaved when this was in-memory only, and it reads as the
        application being broken rather than the firmware being narrower than its catalogue.
        """
        rejected = self._limits.get("rejected") or {}
        bounds = self._limits.get("bounds") or {}
        locked = set(self._limits.get("locked") or ())
        if not (rejected or bounds or locked):
            return

        rows: list[Any] = []
        for cap in self._capabilities:
            # Keyed by capability key, not property name: the adapter answers many of the same
            # names as the headset and refuses different values, so one map keyed by name would
            # let the dongle's limits narrow the headset's controls.
            key = cap.key
            if key in locked:
                self._advisories[key] = Advisory(
                    "This device rejects this setting", locked=True
                )
            if key in rejected and cap.choices:
                keep = tuple(c for c in cap.choices if c.value not in rejected[key])
                cap = dataclasses.replace(cap, choices=keep) if keep else cap
            if key in bounds and cap.kind is Kind.RANGE:
                low, high = bounds[key]
                cap = dataclasses.replace(cap, minimum=float(low), maximum=float(high))
            rows.append(cap)
        self._capabilities = CapabilitySet(rows)
        log.info("applied learned limits: %d rejected, %d bounded, %d locked",
                 len(rejected), len(bounds), len(locked))

    def _format_readouts(self) -> None:
        """Run read-only values through ``labels.format_value``, as the source app does.

        Only readouts. That call formats for *display* -- language identifiers as names, button
        actions as their vendor wording, ``_0dB`` as "0 dB" -- and a formatted string written back
        to the device would not survive the trip. Editable controls keep their raw value and get
        their wording from ``Choice`` labels instead.
        """
        for capability in self._capabilities:
            if capability.kind is not Kind.READOUT or capability.key not in self._values:
                continue
            name = C.property_name(capability.key)
            if not name:
                continue
            self._values[capability.key] = labels.format_value(
                name, self._values[capability.key]
            )

    def _read_relay(self, catalogue: Catalogue, primary: Endpoint,
                    candidates: list[str]) -> dict[str, Any]:
        """Probe the adapter and read what it answers.

        Its own cache entry: the adapter is a different product with its own firmware, so the key
        differs and one endpoint's verdict can never stand in for the other's. Failures are not
        fatal -- an adapter that will not answer costs its section, not the headset's page.
        """
        jabra = self._require()
        relay = next((e for e in jabra.endpoints if e.address != primary.address), None)
        self._relay = relay
        if relay is None:
            return {}

        cached = capability_cache.load(relay.product_id, relay.firmware, len(catalogue))
        try:
            if cached is not None:
                names = [n for n in cached if n in catalogue]
                values = jabra.get_many(names, address=relay.address)
                self._relay_supported = names
            else:
                log.info("probing %d properties on the adapter %s", len(candidates), relay)
                self._relay_supported, values = jabra.probe_and_read(
                    candidates, timeout=PROBE_TIMEOUT, address=relay.address
                )
                capability_cache.save(relay.product_id, relay.firmware, len(catalogue),
                                      self._relay_supported)
        except (DeviceError, GnpError, OSError) as exc:
            log.info("adapter settings unavailable: %s", exc)
            self._relay_supported = []
            return {}
        return _flatten(values, C.relay_key)

    def _read_equalizer(self) -> dict[str, Any]:
        """The band table, or nothing when this device has no equalizer.

        The read takes an argument byte (``7d 00``); the bare subcommand returns ten useless bytes,
        which is what once made this look paginated and unreadable. The reply uses a *different*
        layout from the write payload, so ``decode_read`` is tried first.
        """
        jabra = self._require()
        try:
            raw = jabra.read_equalizer_raw()
        except (DeviceError, GnpError, OSError) as exc:
            log.info("no equalizer: %s", exc)
            return {}
        equalizer = _decode_equalizer(raw)
        if equalizer is None:
            return {}
        self._equalizer = equalizer
        return {f"{C.EQ_PREFIX}{i}": band.db for i, band in enumerate(equalizer.bands)}

    def _state_rows(self, primary: Endpoint) -> list[str]:
        """Which live-state rows to show.

        Mute and call state come from the Telephony HID reports rather than GNP -- the catalogue
        declares ``offHook`` as a ``hidInputReport`` step, so no GNP request returns it -- so they
        cannot be probed for and are listed on the strength of the endpoint being a headset.

        **Only when it is one.** Measured on this hardware: opened directly, a Link 390 and an
        Evolve2 85 deskstand each answer as the *only* endpoint on their link, and neither has a
        microphone or takes calls. Listing the rows anyway gave both of them a permanent
        "Microphone: not reported", which reads as a broken headset rather than a dongle.
        """
        if not primary.looks_like_headset:
            return list(self._armed)
        return [*self._armed, "microphoneMuteState", MODE_KEY]

    @staticmethod
    def _battery_level(values: dict[str, Any]) -> int | None:
        """Percentage from either the plain integer or the ``batteryLevelV2`` jsonObject form.

        Callers must also check the endpoint is a headset. A Link 390 answers ``batteryLevel`` with
        a flat **0** and ``isBatteryLow`` with False -- it has no battery, but it does have the
        subcommand -- and a meter reading 0 % on a bus-powered dongle is worse than no meter.
        """
        for name in ("batteryLevelV2", "batteryLevel", "batteryStatus"):
            raw = values.get(name)
            if isinstance(raw, bool) or raw is None:
                continue
            if isinstance(raw, (int, float)):
                return max(0, min(100, int(raw)))
            if isinstance(raw, dict):
                for field in ("primaryLevelInPercent", "value", "level", "percent"):
                    if isinstance(raw.get(field), (int, float)):
                        return max(0, min(100, int(raw[field])))
        return None

    @staticmethod
    def _candidates(catalogue: Catalogue) -> list[str]:
        """Properties worth probing: readable, and not destructive.

        Destructive ones are excluded from the *probe*, not merely from the page. Probing reads
        rather than writes, so it would be safe -- but a name in the cache implies a control, and
        keeping the two lists identical is what guarantees a factory reset can never reach the UI
        through a stale cache entry written by an older version.
        """
        return sorted(
            p.name for p in catalogue if p.readable and not categories.is_dangerous(p.name)
        )

    @staticmethod
    def _subscribable(catalogue: Catalogue) -> list[str]:
        """What to arm: the catch-all change stream, plus the event-only state properties.

        The state properties have no read pipeline at all, so an unarmed bit means the value is
        unobtainable rather than stale. Bits the device has no notification for are dropped
        individually by ``arm_events``, so asking for more than a model supports costs nothing.
        """
        from .controller import CONFIG_CHANGE_PROPERTY, STATE_PROPERTIES

        wanted = [CONFIG_CHANGE_PROPERTY, *STATE_PROPERTIES]
        return [name for name in wanted if name in catalogue]

    def _identity_raw(self, primary: Endpoint, values: dict[str, Any]) -> dict[str, Any]:
        """Identity fields, preferring what endpoint discovery already fetched."""
        out: dict[str, Any] = {}
        if primary.name:
            out["name"] = primary.name
        if primary.firmware:
            out["firmwareVersion"] = primary.firmware
        if primary.product_id is not None:
            out["pid"] = f"0x{primary.product_id:04X}"
        for name in ("serialNumber", "skuNumber", "variant"):
            if values.get(name) not in (None, ""):
                out[name] = values[name]
        return out

    def _identity(self, primary: Endpoint, values: dict[str, Any]) -> dict[str, Any]:
        return {
            f"{C.INFO_PREFIX}{k}": v
            for k, v in self._identity_raw(primary, values).items()
        }

    # ------------------------------------------------------------------ reading

    def _require(self) -> JabraDevice:
        if self._jabra is None:
            raise Unreachable("not connected")
        return self._jabra

    async def get(self, key: str) -> Any:
        return self._values.get(key)

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Whatever is already known. Values arrive at connect and then by event.

        Deliberately not a re-read: the shell calls this to paint, and putting ~70 round trips
        behind a repaint is what made the source project's first UI feel like it had frozen.
        """
        return {k: self._values[k] for k in keys if k in self._values}

    async def refresh(self) -> dict[str, Any]:
        """Re-read every supported property, at both endpoints, plus the band table.

        The manual refresh, not the paint path. It covers the adapter and the equalizer too: a
        refresh that silently skipped them would leave two sections stale while claiming the page
        had just been re-read.
        """
        async with self._lock:
            fresh = await asyncio.to_thread(self._refresh_sync)
        self._values.update(fresh)
        return fresh

    def _refresh_sync(self) -> dict[str, Any]:
        jabra = self._require()
        fresh = _flatten(jabra.get_many(self._supported), C.setting_key)
        if self._relay is not None and self._relay_supported:
            relay_values = jabra.get_many(self._relay_supported, address=self._relay.address)
            fresh.update(_flatten(relay_values, C.relay_key))
        if self._equalizer is not None:
            fresh.update(self._read_equalizer())
        return fresh

    def advisories(self) -> dict[str, Advisory]:
        return dict(self._advisories)

    # ------------------------------------------------------------------ writing

    async def set(self, key: str, value: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._set_sync, key, value)

    def _set_sync(self, key: str, value: Any) -> Any:
        if key == C.EQ_FLAT_KEY:
            return self._write_equalizer(flat=True)
        band = C.band_index(key)
        if band is not None:
            return self._write_equalizer(band=band, db=float(value))

        name = C.property_name(key)
        if not name:
            raise DeviceError(f"{key} is not a writable setting")
        # The adapter and the headset share property names and answer differently, so the endpoint
        # is part of the request, not a guess.
        address = self._relay.address if C.is_relay_key(key) and self._relay else None
        jabra = self._require()
        try:
            landed = jabra.set(name, value, address=address)
        except NotSupported:
            raise
        except VerificationFailed as exc:
            # No NACK: the device acknowledged the write and kept its old value. Measured on an
            # Evolve2 85, ``soundMode2`` does exactly this for every value but ``normal`` while
            # ``soundMode`` takes all three -- so the option is real in the catalogue and absent
            # from the hardware, which is the same conclusion as a 0xFA, reached differently.
            raise DeviceError(self._reject(key, name, value, acked=True)) from exc
        except ValueRejected as exc:
            # The catalogue lists a value this hardware does not have. Correct the control so the
            # option cannot be chosen again, then report it -- leaving it in place means the user
            # picks it, watches it revert, and reasonably concludes the application is broken.
            raise DeviceError(self._reject(key, name, value)) from exc
        except WriteRefused as exc:
            # Implemented but declined -- a policy lock or the wrong device state. Distinct from
            # "unsupported", and the user can often act on it, so it must not be flattened.
            raise DeviceError(str(exc)) from exc
        except (DeviceError, GnpError) as exc:
            raise DeviceError(f"{labels.label(name)}: {exc}") from exc
        self._values[key] = landed
        return landed

    def _reject(self, key: str, name: str, value: Any, *, acked: bool = False) -> str:
        """Narrow a control to what the device will actually accept, and say what happened.

        Ported from the source project's ``_on_failed``: a NACK 0xFA on a write means the value is
        not available on *this* hardware even though the catalogue lists it. Three shapes:

        * **a choice** loses the rejected option -- the Link 390 lists four wireless ranges and
          rejects ``ultraLow``; an Evolve2 85 lists six IntelliTone levels and takes three;
        * **a range** has the bound pulled in past the rejected value -- ``hearThroughLevel``
          declares −12..6 and the hardware stops at 0. Narrowed one step at a time rather than
          jumping to the current value, which would over-narrow when the user reaches far;
        * **anything else** is locked with the reason, which is the source's ``mark_unsupported``.

        Learned per session, not cached: it is a property of the firmware, but writing it to the
        capability cache would make one refused click permanent, and a mistake there is invisible
        and undoable.
        """
        cap = self._capabilities.by_key(key)
        label = labels.label(name)
        if cap is None:
            return f"{label}: this device does not accept that value."

        if cap.kind is Kind.CHOICE:
            remaining = tuple(c for c in cap.choices if c.value != value)
            if remaining:
                self._replace(key, dataclasses.replace(cap, choices=remaining))
                self._remember_limit(key, value=value)
                how = "ignored that option" if acked else "does not support that option"
                return f"{label}: this device {how} — removing it."
            self._advisories[key] = Advisory("This device rejects this setting", locked=True)
            self._remember_limit(key, locked=True)
            self._bump_capabilities()
            return f"{label}: this device does not support any of the listed options."

        if cap.kind is Kind.RANGE and isinstance(value, (int, float)):
            # Which end to pull in is decided by where the rejected value sits relative to the one
            # the device is holding: reaching up and being refused means the ceiling is lower.
            step = cap.step or 1
            current = self._values.get(key)
            reference = float(current) if isinstance(current, (int, float)) else cap.minimum
            unit = f" {cap.unit}" if cap.unit else ""
            if value > reference:
                edge = self._find_edge(name, reference, float(value), step)
                narrowed = dataclasses.replace(cap, maximum=edge)
                self._replace(key, narrowed)
                self._remember_limit(key, cap=narrowed)
                self._settle(key, name)
                return f"{label}: this device goes no higher than {edge:g}{unit}."
            if value < reference:
                edge = self._find_edge(name, reference, float(value), -step)
                narrowed = dataclasses.replace(cap, minimum=edge)
                self._replace(key, narrowed)
                self._remember_limit(key, cap=narrowed)
                self._settle(key, name)
                return f"{label}: this device goes no lower than {edge:g}{unit}."

        self._advisories[key] = Advisory("This device rejects this setting", locked=True)
        self._remember_limit(key, locked=True)
        self._bump_capabilities()
        return f"{label}: this device rejects this setting."

    def _find_edge(self, name: str, good: float, bad: float, step: float) -> float:
        """The furthest value this device accepts, between one that worked and one that did not.

        A binary search rather than creeping one step per click. ``hearThroughLevel`` declares
        −12..6 and stops at 0, so stepping down from a refused +6 would make the user hit the wall
        six times before the slider told the truth. Bounded at ten probes, which covers any range
        the catalogue declares, and every probe is a write to the setting the user was already
        changing. Ends by restoring the last value known to work.
        """
        jabra = self._require()
        for _ in range(10):
            if abs(bad - good) <= abs(step):
                break
            middle = good + (bad - good) / 2
            middle = round(middle / step) * step if step else middle
            if middle in (good, bad):
                break
            try:
                jabra.set(name, type(good)(middle) if isinstance(good, int) else middle)
            except (DeviceError, GnpError):
                bad = middle
            else:
                good = middle
        with contextlib.suppress(DeviceError, GnpError):
            jabra.set(name, good)
        return good

    def _settle(self, key: str, name: str) -> None:
        """Repaint the control from what the device actually holds after a probe."""
        with contextlib.suppress(DeviceError, GnpError, OSError):
            self._values[key] = self._require().get(name)

    def _remember_limit(self, key: str, *, value: Any = None, cap=None,
                        locked: bool = False) -> None:
        """Persist one refusal so the next connect starts already narrowed.

        Keyed by capability key rather than property name -- see ``_apply_limits``.
        """
        if locked:
            if key not in self._limits["locked"]:
                self._limits["locked"].append(key)
        elif cap is not None and cap.kind is Kind.RANGE:
            self._limits["bounds"][key] = [cap.minimum, cap.maximum]
        elif value is not None:
            self._limits["rejected"].setdefault(key, [])
            if value not in self._limits["rejected"][key]:
                self._limits["rejected"][key].append(value)
        capability_cache.save_limits(*self._limits_key, self._limits)

    def _replace(self, key: str, capability) -> None:
        """Swap one capability in place and tell the shell to repaint."""
        rows = [capability if c.key == key else c for c in self._capabilities]
        self._capabilities = CapabilitySet(rows)
        self._bump_capabilities()

    def _write_equalizer(self, *, band: int | None = None, db: float = 0.0,
                         flat: bool = False) -> Any:
        """Write the whole band table, because that is the only write the device has.

        Every band goes out in one message and each band's opaque ``A`` field must be returned
        unchanged, so this is a read-modify-write built on the table last read -- not a blind
        write of one slider. The reply is the new table, which is the confirmation.
        """
        current = self._equalizer
        if current is None:
            raise NotSupported("this device has no equalizer")

        gains = [b.db for b in current.bands]
        if flat:
            wanted = current.flat()
        else:
            if band is None or not 0 <= band < len(gains):
                raise DeviceError(f"there is no equalizer band {band}")
            gains[band] = max(C.EQ_MIN_DB, min(C.EQ_MAX_DB, db))
            wanted = current.with_gains_db(gains)

        jabra = self._require()
        try:
            raw = jabra.write_equalizer_raw(wanted.encode())
        except WriteRefused as exc:
            raise DeviceError(str(exc)) from exc
        except (DeviceError, GnpError, OSError) as exc:
            raise DeviceError(f"Equalizer: {exc}") from exc

        landed = _decode_equalizer(raw) or wanted
        self._equalizer = landed
        for index, entry in enumerate(landed.bands):
            self._values[f"{C.EQ_PREFIX}{index}"] = entry.db

        if flat:
            # Flat moves every band, and the shell only repaints siblings when the capability
            # revision changes -- ``writes_with`` groups a write, it does not refresh the others.
            # Without this the device went flat and the sliders stayed where they were, which is
            # the worst of both: the page disagrees with the hardware and looks like the button
            # failed. A single-band write needs no bump, because only that band moved.
            self._bump_capabilities()
            return "Equalizer set flat"
        return landed.bands[band].db

    async def fetch_photo(self) -> bytes | None:
        """Jabra's own product image, if their configuration service advertises one.

        The endpoint needs the *endpoint's* product id and firmware, not the USB link's: the
        hidraw node reports the adapter, while the picture wanted is the headset's.
        """
        jabra = self._jabra
        primary = jabra.primary if jabra is not None else None
        if primary is None:
            return None
        return await asyncio.to_thread(
            photos.download, primary.product_id, primary.firmware
        )

    # ------------------------------------------------------------------ where this device is

    def connection_label(self) -> ConnectionLabel:
        return ConnectionLabel(self._route(), self._identifier())

    def _route(self) -> str:
        """USB, or through whatever the headset is linked by.

        The link's own name is the honest label: "via Jabra Link 390" stays right on a Link 380 or
        a Link 400, where a hardcoded model would be wrong and "adapter" would say less. A headset
        on its own cable answers at the same address it was found on, and there is no hop to name.
        """
        jabra = self._jabra
        if jabra is None or jabra.primary is None:
            return ""
        relays = [e for e in jabra.endpoints if e.address != jabra.primary.address]
        if not relays:
            return "USB"
        return f"via {relays[0].name}" if relays[0].name else "USB"

    def _route_events(self, changed: dict[str, Any]) -> dict[str, Any]:
        """Map what the device pushed onto capability keys.

        Three destinations, not one. Settings go to their control; battery to the meter; the
        event-only state to its readout. Anything with no capability is dropped rather than
        silently accumulating -- but dropping *everything* without a setting, which an earlier
        version did, threw away the whole state panel.
        """
        out: dict[str, Any] = {}

        # On-head arrives one earcup at a time, and both sides fire in the same instant when the
        # headset is put on, so the pair is accumulated rather than last-one-wins.
        for side in ("left", "right"):
            if f"{EARCUP_PREFIX}{side}" in changed:
                self._earcups[side] = bool(changed[f"{EARCUP_PREFIX}{side}"])
        if self._earcups:
            worn = [s for s, on in sorted(self._earcups.items()) if on]
            out[C.state_key("onHeadDetectionStatus")] = (
                "Yes" if len(worn) == 2 else f"{', '.join(worn)} only" if worn else "No"
            )

        if PHONE_MUTE_KEY in changed:
            out[C.state_key("microphoneMuteState")] = (
                "Muted" if changed[PHONE_MUTE_KEY] else "Live"
            )
        if MODE_KEY in changed:
            out[C.state_key(MODE_KEY)] = changed[MODE_KEY]

        battery = self._battery_level(changed)
        if battery is not None:
            out[C.BATTERY_KEY] = battery

        for name, value in changed.items():
            if name.startswith("_"):
                continue
            for key in (C.setting_key(name), C.state_key(name)):
                if self._capabilities.by_key(key) is not None:
                    out[key] = labels.format_value(name, value) if key.startswith(
                        C.STATE_PREFIX) else value
        return {k: v for k, v in out.items() if self._capabilities.by_key(k) is not None}

    def _identifier(self) -> str:
        return str(self._values.get(f"{C.INFO_PREFIX}serialNumber") or "")

    # ------------------------------------------------------------------ changes

    def changes(self) -> AsyncIterator[CapabilityValue]:
        """Values the device pushed, without asking for them.

        Two sources, both free of link traffic: GNP events armed at connect, and the Telephony HID
        reports that arrive on the same node and would otherwise be discarded.
        """

        async def stream() -> AsyncIterator[CapabilityValue]:
            while True:
                await asyncio.sleep(POLL_SECONDS)
                jabra = self._jabra
                if jabra is None:
                    return
                try:
                    changed = await asyncio.to_thread(jabra.drain_events)
                except (DeviceError, GnpError, OSError) as exc:
                    log.debug("event drain failed: %s", exc)
                    continue
                try:
                    changed.update(await asyncio.to_thread(jabra.telephony_state))
                except (DeviceError, GnpError, OSError) as exc:
                    log.debug("telephony read failed: %s", exc)
                for key, value in self._route_events(changed).items():
                    self._values[key] = value
                    yield CapabilityValue(key=key, value=value)

        return stream()


def _flatten(values: dict[str, Any], key_for) -> dict[str, Any]:
    """Property values as capability values, splitting dictionaries into their fields.

    ``supportedEvents`` and ``versionExtended`` decode to dicts. Kept whole, one of them renders as
    a single row carrying its entire ``repr`` -- which on real hardware pushed the window off the
    edge of the screen. ``capabilities`` builds a row per field, so the values have to match.
    """
    out: dict[str, Any] = {}
    for name, value in values.items():
        key = key_for(name)
        if isinstance(value, dict) and value:
            for field, inner in value.items():
                out[C.field_key(key, field)] = inner
        else:
            out[key] = value
    return out


def _decode_equalizer(raw: object) -> Equalizer | None:
    """Decode a band table from whatever the device handed back.

    The device answers *reads* in one layout and takes *writes* in a more compact one, so the read
    layout is tried first -- that is what arrives from hardware. Returning ``None`` rather than
    raising is deliberate: a device with no equalizer is not an error, it simply has no such tab.
    """
    if isinstance(raw, Equalizer):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return None
    data = bytes(raw)
    for decoder in (Equalizer.decode_read, Equalizer.decode):
        try:
            return decoder(data)
        except ValueError as exc:
            log.debug("%s: %s", decoder.__name__, exc)
    log.info("cannot decode the equalizer payload (%d bytes)", len(data))
    return None


__all__ = ["JabraHeadsetDevice"]
