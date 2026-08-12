"""High-level device controller — read, write, verify, subscribe.

Ported from the standalone ``plasma-jabra-headphone-support`` project, where every rule below was
found against a Link 390 and an Evolve2 85. The GUI half of that project is dropped: the shell and
renderer here replace it. What is kept is the part hardware taught, comments included.

The one deliberate change is the exception hierarchy: these errors now derive from the shell's
own, so a failure surfaces as a device error the UI already knows how to present rather than a
RuntimeError nothing catches.

Implements the strategy argued in docs/READ-WRITE-VERIFY.md: Jabra's request/reply discipline
underneath, Poly's idle behaviour and write verification on top, Sony's in-flight guard for the
UI, and Jabra's own `configChangeEvents` subscription so nothing has to be polled.

Why not one single mechanism: of the catalogue's 307 writable properties only 10 emit a change
event, so echo-only verification (which is all the Sony project needed) cannot cover Jabra.
Equally, verify-by-read alone would miss changes made with the headset's own buttons unless we
polled. Using both, chosen per property from the catalogue, is what covers everything.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from hardware_ui.core import DeviceError, NotSupported

from .protocol import catalogue
from .protocol.catalogue import Property
from .protocol.hid import HidTransport, JabraHidDevice
from .protocol.ids import Command, Nack
from .protocol.process import Interpreter, ProcessError, UnsupportedStep  # noqa: F401
from .protocol.session import Endpoint, GnpNackError, GnpSession, GnpTimeout

log = logging.getLogger(__name__)

#: The event-subscription register: DEVICE (0x0D) subcommand 0x4C, a 4-byte big-endian bitmask.
#: Every subscribable property sets one bit in it, so arming must be read-modify-write — a blind
#: write turns the others off.
SUBSCRIPTION_COMMAND = Command.DEVICE
SUBSCRIPTION_SUBCOMMAND = 0x4C

#: Bit 4 arms `configChangeEvents`, which pushes the list of CONFIG subcommands that changed.
#: This is what removes any need to poll for externally-made changes.
CONFIG_CHANGE_PROPERTY = "configChangeEvents"

#: State properties worth arming beyond `configChangeEvents`. They are **event-only** — no `read`
#: pipeline exists for any of them — so an unarmed bit means the value is unobtainable, not merely
#: stale. The Evolve2 85 happens to come up with mask 0x0000017F, which already covers bits 2 and
#: 6, but relying on the device to self-arm would silently fail on a model that does not.
#: `deviceLink` is included because `tools/watch_events.py` arms it and that script demonstrably
#: worked; bits the device has no notification for are dropped individually rather than failing the
#: batch, so asking for more costs nothing.
STATE_PROPERTIES = ["onHeadDetectionStatus", "boomArmPosition", "microphoneMuteState",
                    "deviceLink"]

#: Telephony HID report on the GNP interface. Bit positions are this device's own descriptor order
#: on usage page 0x000B — see `telephony_state()`.
TELEPHONY_REPORT_ID = 0x02
TELEPHONY_HOOK_SWITCH = 0x01
TELEPHONY_PHONE_MUTE = 0x08

#: Synthetic key for call-versus-media mode; mirrors `status_widgets.MODE_KEY`.
MODE_KEY = "_mode"

#: On-head is reported **per earcup**, one event per side, and both sides fire in the same instant
#: when the headset is put on. Each side therefore gets its own synthetic key, because a dict keyed
#: by property name would keep only whichever arrived second.
ON_HEAD_PROPERTY = "onHeadDetectionStatus"
EARCUP_PREFIX = "_earcup_"

#: Mute from the HID Telephony report — the GNP event for it has never been observed
#: on this hardware, but bit 3 of report 0x02 decodes reliably.
PHONE_MUTE_KEY = "_phoneMute"

#: Write verification: how many settled re-reads, and how long between them. Matches the Poly
#: project's measured behaviour — a device can briefly report the old value after acking.
VERIFY_ATTEMPTS = 4
VERIFY_DELAY = 0.15

#: Timeout for capability probing. Short deliberately: an absent property usually NACKs within a
#: few milliseconds, but some answer nothing at all, and at the 5 s default a few hundred of those
#: dominate the connect. Measured on a Link 390 + Evolve2 85: 0.6 s gave ~42 s per device, 0.35 s
#: gives ~25 s, with no property changing its verdict. Lower than this starts to risk calling a
#: slow-but-present property absent, which the cache would then persist.
PROBE_TIMEOUT = 0.35


class WriteRefused(DeviceError):
    """The device implements it but declined — policy lock or wrong state."""


class ValueRejected(DeviceError):
    """The device implements the property but not *this value* — NACK 0xFA ILLEGAL_PARAM.

    Distinct from ``NotSupported`` (the property does not exist) and ``WriteRefused`` (it exists
    and the device is in the wrong state). Here the catalogue lists a value the hardware does not
    actually have: an Evolve2 85 offers six IntelliTone levels of which three work, and a Link 390
    lists four wireless ranges of which three do. Typed rather than detected from the message text,
    which is how the source project had to do it.
    """


class VerificationFailed(DeviceError):
    """The write was acked but the device does not report the new value."""


@dataclass
class Probed:
    """What we have learned about one property on this device.

    Named ``Probed`` rather than ``Capability`` as in the source project: here ``Capability`` is
    the shell's own schema type, and two different things under one name in one module tree is a
    confusion waiting to happen.
    """

    supported: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.supported


@dataclass
class PendingWrite:
    """A write in flight — the in-flight guard's record.

    While this exists the GUI must disable the control and ignore background refreshes for that
    property. Without it the control flickers old -> new -> old, which is the single bug both
    earlier projects had to go back and fix.
    """

    name: str
    intended: Any
    started: float = field(default_factory=time.monotonic)


class JabraDevice:
    """One Jabra endpoint, addressed through one GNP session."""

    def __init__(self, hid_device: JabraHidDevice | None = None, *,
                 catalogue_=None):
        self.transport = HidTransport(hid_device)
        self.session = GnpSession(self.transport)
        self.catalogue = catalogue_ or catalogue.load()
        self.endpoints: list[Endpoint] = []
        self.primary: Endpoint | None = None
        self._interpreter: Interpreter | None = None
        self._capabilities: dict[str, Probed] = {}
        self._interpreters: dict[int, Interpreter] = {}
        self._pending: dict[str, PendingWrite] = {}
        self._armed: set[str] = set()
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------------------------

    def connect(self) -> Endpoint:
        self.transport.connect()
        self.endpoints = self.session.discover_endpoints()
        if not self.endpoints:
            raise DeviceError("no GNP endpoint answered on this link")
        self.primary = next(
            (e for e in self.endpoints if e.address == self.session.primary_address),
            self.endpoints[0],
        )
        self._interpreter = Interpreter(self.session, address=self.primary.address)
        log.info("connected: %s", self.primary)
        return self.primary

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> JabraDevice:
        self.connect()
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    @property
    def description(self) -> str:
        return str(self.primary) if self.primary else self.transport.description

    def interpreter_for(self, endpoint: Endpoint) -> Interpreter:
        """An interpreter bound to another endpoint on the same link (e.g. the dongle)."""
        return Interpreter(self.session, address=endpoint.address)

    def _require(self, address: int | None = None) -> Interpreter:
        """The interpreter for an endpoint on this link; the primary one by default.

        Added for the port: the source project only ever addressed its primary endpoint, but a
        link fronts several GNP devices and the dongle's own settings are worth configuring. The
        addressing itself is unchanged -- ``Interpreter`` always took an address, and each request
        carries a destination -- so this only chooses which one to use.
        """
        if self._interpreter is None:
            raise DeviceError("not connected")
        if address is None or address == self._interpreter.address:
            return self._interpreter
        cached = self._interpreters.get(address)
        if cached is None:
            cached = Interpreter(self.session, address=address)
            self._interpreters[address] = cached
        return cached

    @staticmethod
    def _qualify(name: str, address: int | None) -> str:
        """Cache key for a property at a given endpoint.

        Endpoints on one link share property *names* and answer differently: ``ancMode`` is
        readable at the headset and "unknown sub-command" at the dongle. Keying the capability and
        pending-write maps by name alone would let one endpoint's verdict stand in for the other's.
        """
        return name if address is None else f"{address:#04x}:{name}"

    # -- capabilities ----------------------------------------------------------------------

    def capability(self, name: str, *, probe: bool = True) -> Probed:
        """Whether this device implements a property. Cached.

        Determined by reading it: an unimplemented subcommand answers NACK 0xF6/0xFF, and some
        answer nothing at all — the Link 390 times out on `voicePrompts` rather than NACKing —
        so a timeout counts as absent too.
        """
        with self._lock:
            if name in self._capabilities:
                return self._capabilities[name]

        prop = self.catalogue.get(name)
        if prop is None:
            result = Probed(False, "not in the property catalogue")
        elif not prop.readable:
            # Nothing to probe with; assume present and let the write speak.
            result = Probed(True, "write-only, unprobed")
        elif not probe:
            return Probed(True, "unprobed")
        else:
            result = self._probe(prop)

        with self._lock:
            self._capabilities[name] = result
        return result

    def _probe(self, prop: Property) -> Probed:
        try:
            self._require().read(prop)
        except GnpNackError as exc:
            if exc.unsupported:
                return Probed(False, "device answered 'unknown sub-command'")
            return Probed(True, f"present but {exc}")
        except GnpTimeout:
            return Probed(False, "no answer")
        except UnsupportedStep as exc:
            return Probed(False, str(exc))
        except ProcessError as exc:
            # It answered, so the subcommand exists; we just could not decode it.
            return Probed(True, f"undecodable: {exc}")
        return Probed(True)

    def supported_properties(self, names: list[str]) -> list[str]:
        """Filter a candidate list down to what this device actually has."""
        return [n for n in names if self.capability(n)]

    def probe_and_read(self, names: list[str], *, timeout: float = PROBE_TIMEOUT,
                       on_value=None, should_continue=None, address: int | None = None
                       ) -> tuple[list[str], dict[str, Any]]:
        """Discover support **and** collect values in a single pass.

        Probing separately and then re-reading doubles the round trips, which is what made the
        first connect take about a minute. One read answers both questions: if it returns a value
        the property exists, and we already have the value.

        `timeout` is short on purpose. A property that is simply absent usually NACKs immediately,
        but some (the Link 390's `voicePrompts`) answer nothing at all, and at the 5 s default a
        few hundred of those dominate the whole connect.

        `on_value(name, value, index, total)` is called as results arrive, so a GUI can populate
        progressively instead of waiting for the sweep to finish.

        `should_continue()` is checked between reads. The sweep takes tens of seconds, and without
        a cancellation point a request to stop (closing the window) has to wait for the whole
        thing — which is what made exiting mid-discovery hang.
        """
        supported: list[str] = []
        values: dict[str, Any] = {}
        total = len(names)
        for index, name in enumerate(names, start=1):
            if should_continue is not None and not should_continue():
                log.info("capability probe cancelled after %d of %d", index - 1, total)
                break
            prop = self.catalogue.get(name)
            if prop is None or not prop.readable:
                continue
            try:
                value = self._require(address).read(prop, timeout=timeout)
            except GnpNackError as exc:
                self._remember(self._qualify(name, address),
                               Probed(False, "device answered 'unsupported'")
                               if exc.unsupported else Probed(True, str(exc)))
                continue
            except GnpTimeout:
                self._remember(self._qualify(name, address), Probed(False, "no answer"))
                continue
            except (ProcessError, UnsupportedStep) as exc:
                # It answered, so the subcommand exists; we just cannot decode it yet.
                self._remember(self._qualify(name, address), Probed(True, f"undecodable: {exc}"))
                continue
            self._remember(self._qualify(name, address), Probed(True))
            supported.append(name)
            values[name] = value
            if on_value is not None:
                on_value(name, value, index, total)
        return supported, values

    def _remember(self, name: str, capability: Probed) -> None:
        with self._lock:
            self._capabilities[name] = capability

    # -- reading ---------------------------------------------------------------------------

    def get(self, name: str, *, address: int | None = None) -> Any:
        prop = self._property(name)
        if not prop.readable:
            raise DeviceError(f"{name} is not readable")
        try:
            return self._require(address).read(prop)
        except GnpNackError as exc:
            if exc.unsupported:
                raise NotSupported(f"{name}: {exc}") from exc
            raise DeviceError(f"{name}: {exc}") from exc

    def get_many(self, names: list[str], *, address: int | None = None) -> dict[str, Any]:
        """Read several properties, skipping any the device refuses. Never raises per-item."""
        values: dict[str, Any] = {}
        for name in names:
            try:
                values[name] = self.get(name, address=address)
            except (DeviceError, ProcessError, GnpTimeout) as exc:
                log.debug("skipping %s: %s", name, exc)
        return values

    # -- writing ---------------------------------------------------------------------------

    def set(self, name: str, value: Any, *, verify: bool = True,
            address: int | None = None) -> Any:
        """Write a property and confirm it took effect. Returns the confirmed value.

        Deliberately no pre-read: it costs a round trip only to improve an error message, and
        write latency is what the user feels.

        Verification order, per property:
          1. the device's change EVENT, if the catalogue declares one and it is armed;
          2. otherwise a settled re-read that expects the written value;
          3. if the property is not readable at all, the absence of a NACK is all there is —
             reported honestly rather than dressed up as confirmation.
        """
        prop = self._property(name)
        if not prop.writable:
            raise DeviceError(f"{name} is not writable")

        pending = PendingWrite(name, value)
        tracked = self._qualify(name, address)
        with self._lock:
            self._pending[tracked] = pending
        try:
            try:
                self._require(address).write(prop, value)
            except GnpNackError as exc:
                if exc.unsupported:
                    raise NotSupported(f"{name}: {exc}") from exc
                if exc.refused:
                    raise WriteRefused(f"{name}: {exc}") from exc
                if exc.code == Nack.ILLEGAL_PARAM:
                    raise ValueRejected(f"{name}: {exc}") from exc
                raise DeviceError(f"{name}: {exc}") from exc

            if not verify:
                return value
            return self._confirm(prop, value, address=address)
        finally:
            with self._lock:
                self._pending.pop(tracked, None)

    def _confirm(self, prop: Property, intended: Any, *, address: int | None = None) -> Any:
        if prop.has_event and prop.name in self._armed:
            observed = self._event_value(prop)
            if observed is not None and observed == intended:
                return observed

        if not prop.readable:
            log.info("%s is write-only — accepted without read-back", prop.name)
            return intended

        observed = self._settled_read(prop, intended, address=address)
        if observed != intended:
            raise VerificationFailed(
                f"{prop.name}: wrote {intended!r} but the device reports {observed!r}"
            )
        return observed

    def _settled_read(self, prop: Property, expect: Any, *, address: int | None = None) -> Any:
        """Re-read until the value settles on `expect`, or attempts run out."""
        observed = None
        for attempt in range(VERIFY_ATTEMPTS):
            try:
                observed = self._require(address).read(prop)
            except (GnpNackError, GnpTimeout, ProcessError) as exc:
                log.debug("%s: verify read failed: %s", prop.name, exc)
                return observed
            if observed == expect:
                return observed
            if attempt < VERIFY_ATTEMPTS - 1:
                time.sleep(VERIFY_DELAY)
        return observed

    # -- equalizer -------------------------------------------------------------------------
    #
    # Read raw rather than through the catalogue. `equalizerConfig`'s declared read pipeline needs
    # a parameterised request we have not matched, so the interpreter fails with "need 2 bytes,
    # got 0" — which also kept it out of the probed capability list. The payload format itself is
    # known from a capture (docs/MODES-AND-CAPTURE-MAP.md §2.2), so a direct read works.

    EQUALIZER_COMMAND = Command.CONFIG
    EQUALIZER_SUBCOMMAND = 0x7D

    #: The read takes an **argument byte**. From the capture, Jabra Direct sends
    #: `7d 00` — subcommand plus 0x00 — and gets a 39-byte reply. Sending the subcommand alone
    #: returns 10 useless bytes, which is what made this look paginated and unreadable.
    EQUALIZER_READ_ARG = b"\x00"

    def read_equalizer_raw(self, *, timeout: float = PROBE_TIMEOUT) -> bytes | None:
        """The equalizer payload, or None if this device has no equalizer.

        One read, one reply — no pagination. `timeout` is short because this runs during connect
        and a device without an equalizer should not cost the full 5 s response timeout.
        """
        try:
            payload = self.session.read(self.EQUALIZER_COMMAND, self.EQUALIZER_SUBCOMMAND,
                                        address=self.primary.address,
                                        payload=self.EQUALIZER_READ_ARG, timeout=timeout)
        except (GnpNackError, GnpTimeout) as exc:
            log.info("no equalizer: %s", exc)
            return None
        log.info("equalizer read: %d bytes  %s", len(payload), payload.hex(" "))
        return payload or None

    def write_equalizer_raw(self, payload: bytes) -> bytes | None:
        """Write the whole band table and read it back. Raises on refusal."""
        try:
            self.session.write(self.EQUALIZER_COMMAND, self.EQUALIZER_SUBCOMMAND, payload,
                               address=self.primary.address)
        except GnpNackError as exc:
            if exc.refused:
                raise WriteRefused(f"equalizer: {exc}") from exc
            raise DeviceError(f"equalizer: {exc}") from exc
        return self.read_equalizer_raw()

    # -- in-flight guard -------------------------------------------------------------------

    def pending(self, name: str) -> PendingWrite | None:
        """The in-flight write for a property, if any. The GUI uses this to ignore refreshes."""
        with self._lock:
            return self._pending.get(name)

    @property
    def pending_names(self) -> set[str]:
        with self._lock:
            return set(self._pending)

    def filter_stale(self, values: dict[str, Any]) -> dict[str, Any]:
        """Drop values for properties with a write in flight.

        A background refresh that lands mid-write carries the pre-commit value; applying it is
        exactly what makes a control snap back to its old setting.
        """
        blocked = self.pending_names
        return {k: v for k, v in values.items() if k not in blocked}

    # -- events ----------------------------------------------------------------------------

    def arm_events(self, names: list[str] | None = None) -> set[str]:
        """Arm change notifications, batching every wanted bit into one write.

        Most subscribable properties share the 4-byte register DEVICE/0x4C, so this reads it
        once, ORs in each requested bit, and writes once. Arming per property would issue N
        read-modify-writes and risk one clobbering the next.

        **Not all of them use that register.** `microphoneMuteState`'s subscribe pipeline is
        `assign / constant / gnpWrite` with no `bitmaskInsert` at all — it arms by writing a
        constant to its own subcommand. Those used to be skipped with a debug line, which meant
        mute state could never appear: the property is event-only, so an unarmed bit is not a
        stale value, it is no value ever. They are now armed by running their own pipeline
        through the interpreter, which is catalogue-driven and works for any such property.

        Sony's lesson applies here: an unarmed notification simply never fires, and the change
        looks silently dropped.
        """
        wanted = names if names is not None else [CONFIG_CHANGE_PROPERTY]
        self._require()                        # connection check

        bits: dict[str, int] = {}
        direct: list[str] = []
        for name in wanted:
            prop = self.catalogue.get(name)
            if prop is None or not prop.needs_subscription:
                continue
            bit = self._subscription_bit(prop)
            if bit is None:
                direct.append(name)
            else:
                bits[name] = bit

        # Properties that arm themselves, not via the shared register.
        armed_direct: set[str] = set()
        for name in direct:
            try:
                Interpreter(self.session, address=self.primary.address).subscribe(
                    self._property(name))
                armed_direct.add(name)
            except (DeviceError, ProcessError, UnsupportedStep, GnpNackError,
                    GnpTimeout) as exc:
                log.info("%s: cannot arm its own subscribe pipeline: %s", name, exc)

        if not bits:
            return armed_direct

        current = self._read_subscription_mask()
        if current is None:
            return armed_direct

        # The register **always** gets written, even when every wanted bit already reads as set.
        #
        # This used to skip the write as an optimisation, on the reasoning that an Evolve2 85 comes
        # up with mask 0x0000017F so bits 2, 4 and 6 need no help. That reasoning cost a long
        # debugging session: with the write skipped, no on-head or boom-arm event ever arrived,
        # while `tools/watch_events.py` — which also asks for bits 7, 9 and 10 and therefore *had*
        # to write — saw them fine. The difference between the two was the write itself.
        #
        # The read-back value evidently reflects stored configuration rather than a live
        # subscription: the mask persists, but the device only starts streaming when the register
        # is written during the session. Writing the same value back is idempotent and costs one
        # request per connect, which is nothing next to a state panel that never populates.
        already = {n for n, bit in bits.items() if current & (1 << bit)}
        armed = armed_direct | self._set_subscription_bits(current, bits)

        log.info("event subscriptions: armed %s%s",
                 ", ".join(sorted(armed)) or "none",
                 f" (already on: {', '.join(sorted(already))})" if already else "")
        with self._lock:
            self._armed |= armed
        return armed

    def _read_subscription_mask(self) -> int | None:
        try:
            raw = self.session.read(SUBSCRIPTION_COMMAND, SUBSCRIPTION_SUBCOMMAND,
                                    address=self.primary.address)
        except (GnpNackError, GnpTimeout) as exc:
            log.warning("cannot read the subscription register: %s", exc)
            return None
        return int.from_bytes(raw[:4].rjust(4, b"\x00"), "big")

    def _set_subscription_bits(self, current: int, wanted: dict[str, int]) -> set[str]:
        """OR the wanted bits in, dropping any the device rejects.

        The register is capability-limited: an Evolve2 85 answers NACK 0xFA ILLEGAL_PARAM for
        bits it has no notification for (9 = ancAmbienceMode, 10 = bluetoothLinkQuality), and
        rejects the *whole* write when one bit is bad. So try them together first — one round
        trip in the common case — and only fall back to one-at-a-time to find the good subset.
        """
        combined = current
        for bit in wanted.values():
            combined |= 1 << bit
        if self._write_subscription_mask(combined):
            return set(wanted)

        log.debug("combined subscription write refused; arming bits individually")
        armed: set[str] = set()
        mask = current
        for name, bit in wanted.items():
            candidate = mask | (1 << bit)
            # Write even when the bit already reads as set — see arm_events(): the read-back
            # reflects stored config, not a live subscription, so the write is what starts the
            # stream. Short-circuiting on `candidate == mask` was the bug that made on-head and
            # boom-arm state never arrive.
            if self._write_subscription_mask(candidate):
                armed.add(name)
                mask = candidate
            else:
                log.info("%s: this device has no notification for bit %d", name, bit)
        return armed

    def _write_subscription_mask(self, mask: int) -> bool:
        try:
            self.session.write(SUBSCRIPTION_COMMAND, SUBSCRIPTION_SUBCOMMAND,
                               mask.to_bytes(4, "big"), address=self.primary.address)
        except (GnpNackError, GnpTimeout) as exc:
            log.debug("subscription write 0x%08X refused: %s", mask, exc)
            return False
        return True

    @staticmethod
    def _subscription_bit(prop: Property) -> int | None:
        for step in prop.subscribe:
            if step.get("typeName") == "bitmaskInsert":
                return step.get("offset")
        return None

    def drain_events(self, timeout: float = 0.0) -> dict[str, Any]:
        """Decode pushed events into {property: value}. Sends nothing.

        Safe to call on a timer — it is a non-blocking read of data the device already pushed,
        so idle traffic stays at zero, matching what the vendor apps do.

        **A dict means last-one-wins, which loses data for per-side properties.** Putting the
        headset on emits two events in the same instant — `rightOn` then `leftOn` — and both land
        in one drain, so `decoded["onHeadDetectionStatus"]` keeps only the second. The UI then
        believes one earcup was never reported. Rather than change this method's contract, the
        two sides are also published under their own keys, which cannot collide.
        """
        decoded: dict[str, Any] = {}
        changed_subcommands: set[int] = set()

        for event in self.session.drain_events(timeout=timeout):
            subcommand = event.subcommand
            if subcommand is None:
                continue
            for prop in self.catalogue.by_subcommand(event.packet.command, subcommand):
                if not prop.has_event:
                    continue
                try:
                    value = Interpreter(None).decode_event(prop, event.payload)
                except (ProcessError, UnsupportedStep) as exc:
                    log.debug("%s: cannot decode event: %s", prop.name, exc)
                    continue
                if prop.name == CONFIG_CHANGE_PROPERTY and isinstance(value, list):
                    changed_subcommands.update(int(v) for v in value)
                else:
                    decoded[prop.name] = value
                    if prop.name == ON_HEAD_PROPERTY:
                        # Measured payloads: 4c 02 00/01/02/03 -> leftOn/leftOff/rightOn/rightOff.
                        # Give each earcup its own key so a same-drain pair survives.
                        text = str(value)
                        for side in ("left", "right"):
                            if text.startswith(side):
                                decoded[f"{EARCUP_PREFIX}{side}"] = text.endswith("On")

        # configChangeEvents names subcommands, not values — re-read just those.
        for name in self._properties_for_subcommands(changed_subcommands):
            if name in self.pending_names:
                continue                       # a write of ours; the writer confirms it
            try:
                decoded[name] = self.get(name)
            except (DeviceError, ProcessError, GnpTimeout) as exc:
                log.debug("changed %s but could not re-read: %s", name, exc)

        return self.filter_stale(decoded)

    def telephony_state(self) -> dict[str, Any]:
        """Call-versus-media mode, from the Telephony HID reports on this same hidraw node.

        The GNP catalogue cannot answer "am I in a call": `offHook` and `gnOffHook` are declared
        as `hidInputReport` steps, not `gnpRead`/`gnpEvent`, so there is no GNP request that
        returns them. But the interface carrying the GNP tunnel *also* carries the standard
        Telephony page, and the transport was already keeping those reports in
        `foreign_reports` rather than discarding them — they were just never decoded.

        Note this works even though the **kernel** throws Hook Switch away: `hid-input.c` has no
        `case 0x20:` under `HID_UP_TELEPHONY`, so it produces no evdev event, but the raw report
        still arrives over hidraw. Reading it here is the only way to see the answer button.

        Bit layout of report `0x02`, taken from this device's own report descriptor (declaration
        order on usage page `0x000B`): `0x20` Hook Switch, `0x97` Line Busy, `0x2A`, `0x2F` Phone
        Mute, `0x21` Flash, `0x24`, `0x50`.

        Returns `{}` when no telephony report has arrived, so the caller keeps its last value
        rather than falling back to a guess.
        """
        reports = self.transport.take_foreign_reports()
        state: dict[str, Any] = {}
        for report in reports:
            if len(report) < 2 or report[0] != TELEPHONY_REPORT_ID:
                continue
            flags = report[1]
            in_call = bool(flags & TELEPHONY_HOOK_SWITCH)
            state[MODE_KEY] = "Calling Mode" if in_call else "Music Mode"
            state[PHONE_MUTE_KEY] = bool(flags & TELEPHONY_PHONE_MUTE)
        return state

    def _properties_for_subcommands(self, subcommands: set[int]) -> list[str]:
        names: list[str] = []
        for subcommand in sorted(subcommands):
            for prop in self.catalogue.by_subcommand(Command.CONFIG, subcommand):
                if prop.readable:
                    names.append(prop.name)
        return names

    def _event_value(self, prop: Property) -> Any:
        """Newest pushed value for one property, if it already arrived."""
        for name, value in self.drain_events().items():
            if name == prop.name:
                return value
        return None

    # -- helpers ---------------------------------------------------------------------------

    def _property(self, name: str) -> Property:
        prop = self.catalogue.get(name)
        if prop is None:
            raise DeviceError(f"no property named {name!r}")
        return prop
