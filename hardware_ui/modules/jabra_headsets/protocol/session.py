"""GNP session — request/reply discipline and the event inbox.

Modelled on GnProtocolOverUsbHid (docs/READ-WRITE-VERIFY.md §1):

* **One request in flight**, serialised behind a lock. Jabra's own stack uses a *process*-wide
  named mutex, so the channel is single-writer by the vendor's own design.
* **Replies and events are separate channels.** `OnInputReceived` dispatches by packet type:
  type 3 (REPLY) answers the pending request, anything else is an unsolicited event. Keeping
  them apart is what stops an event being mistaken for a read's answer.
* **A NACK raises.** `ConvertNackResponseToException` turns command 0xFE into an exception
  rather than a value, which keeps "unsupported" and "value 0" distinguishable.
* **Sequence numbers** pre-increment and skip 0 — see `SequenceCounter`.

Idle behaviour follows the Poly project instead: nothing is sent unless the caller asks.
`drain_events()` is a non-blocking read of data the device already pushed, so a periodic drain
puts no traffic on the link.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from .framing import Packet, PacketType, SequenceCounter
from .hid import HidTransport
from .ids import (
    COMMAND_NACK,
    NACK_REFUSED,
    NACK_UNSUPPORTED,
    Command,
    command_name,
    nack_description,
)

log = logging.getLogger(__name__)

#: GnProtocolOverUsbHid.ResponseTimeout — 5 s.
DEFAULT_TIMEOUT = 5.0

#: Unsolicited events are kept bounded; a stalled consumer must not grow memory without limit.
MAX_EVENT_BACKLOG = 256

#: Addresses to probe when mapping what is behind a link. Verified on a Link 390 + Evolve2 85:
#: 0x01 is the dongle, 0x04 the headset, and every other value in this range answers NACK 0xF5
#: ILLEGAL_ADDR (0x09 and up answer 0xF2, a code Jabra's own NackSubcommand enum does not list).
#: Kept short deliberately — this is a validated probe, not a sweep.
ADDRESS_CANDIDATES = (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08)

#: IDENT subcommands used to identify an endpoint, from properties.json.
_SUB_NAME = 0x00
_SUB_FIRMWARE = 0x03
_SUB_PID = 0x11

#: Our own address. The host is not a GNP device; 0x00 is what Deserialize sees for an
#: unset source and the vendor never validates it, so it stays 0 unless a capture says otherwise.
HOST_ADDRESS = 0x00

#: Serialisation is **per session**, not process-wide.
#:
#: A global lock was tried, to mirror Jabra's `Global\Jabra_GNProtocolSyncMutex`, after identity
#: strings came back as garbage ("X", product 0x000E, firmware "M") when two devices were read
#: concurrently. It fixed the symptom but the cause was elsewhere: replies from *timed-out*
#: requests were being accepted by the next request, leaving the session off by one. That is fixed
#: in `_await_reply` by matching the sequence number, and with it a global lock only serialises
#: independent devices — which doubled the first-connect time (~42 s to ~84 s) for no benefit.
#:
#: What still holds:
#:   * one request in flight **per session** (`self._lock`), and
#:   * one process at a time **per device** (`_DeviceLock`, a cross-process flock keyed on the
#:     product id) — two apps talking to the same headset really do corrupt each other.
#: Two *different* devices on separate transports are independent and may run in parallel.


class GnpError(RuntimeError):
    """Any GNP-level failure."""


class GnpTimeout(GnpError):
    """No reply within the timeout."""


class GnpNackError(GnpError):
    """The device answered with a NACK."""

    def __init__(self, code: int, command: int, subcommand: int | None):
        self.code = code
        self.command = command
        self.subcommand = subcommand
        where = f"{command_name(command)}"
        if subcommand is not None:
            where += f"/0x{subcommand:02X}"
        super().__init__(f"{where}: {nack_description(code)} (0x{code:02X})")

    @property
    def unsupported(self) -> bool:
        """The device does not implement this command or subcommand."""
        return self.code in NACK_UNSUPPORTED

    @property
    def refused(self) -> bool:
        """Implemented, but the device declined — policy lock or wrong state."""
        return self.code in NACK_REFUSED


@dataclass(frozen=True)
class Event:
    packet: Packet
    received: float

    @property
    def subcommand(self) -> int | None:
        return self.packet.data[0] if self.packet.data else None

    @property
    def payload(self) -> bytes:
        """Event payload after the subcommand byte."""
        return self.packet.data[1:]


class GnpSession:
    """One serialised GNP conversation with one device."""

    def __init__(self, transport: HidTransport, *, timeout: float = DEFAULT_TIMEOUT):
        self.transport = transport
        self.timeout = timeout
        self.primary_address: int | None = None
        self._seq = SequenceCounter()
        self._lock = threading.RLock()
        self._events: deque[Event] = deque(maxlen=MAX_EVENT_BACKLOG)

    # -- lifecycle -------------------------------------------------------------------------

    def __enter__(self) -> GnpSession:
        self.transport.connect()
        return self

    def __exit__(self, *exc) -> bool:
        self.transport.close()
        return False

    @property
    def description(self) -> str:
        return self.transport.description

    # -- core exchange ---------------------------------------------------------------------

    def request(
        self,
        command: int,
        subcommand: int | None = None,
        payload: bytes = b"",
        *,
        type: PacketType = PacketType.READ,
        address: int | None = None,
        timeout: float | None = None,
        raise_on_nack: bool = True,
    ) -> Packet:
        """Send one request and return its REPLY.

        `subcommand` is prepended to `payload`, which is how the property catalogue models it:
        every gnpRead/gnpWrite names a command *and* a subcommand, and the subcommand is the
        first payload byte.
        """
        dest = address if address is not None else self._require_primary()
        data = (bytes([subcommand]) if subcommand is not None else b"") + payload
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)

        with self._lock:
            packet = Packet(
                command=command,
                dest=dest,
                src=HOST_ADDRESS,
                seq=self._seq.next(),
                type=type,
                data=data,
            )
            log.debug("TX %s", packet)
            self.transport.send(packet)
            reply = self._await_reply(packet, deadline)

        if reply.command == COMMAND_NACK:
            code = reply.data[0] if reply.data else 0
            error = GnpNackError(code, command, subcommand)
            if raise_on_nack:
                raise error
            log.debug("NACK (suppressed): %s", error)
        return reply

    def _await_reply(self, request: Packet, deadline: float) -> Packet:
        """Collect reports until *our* REPLY arrives; stash events met on the way.

        Replies are matched on the sequence number, and a mismatch is **discarded**, not returned.
        This matters more than it looks: when a request times out, its reply still arrives — just
        late — and if the next request accepts it, every subsequent exchange is off by one for the
        rest of the session. Capability probing does hundreds of reads, some of which legitimately
        time out, so without matching the whole sweep decodes the wrong answers (observed: identity
        strings coming back as "X", product 0x000E).

        Some devices might not echo the sequence number faithfully, so this stays tolerant: if the
        deadline passes and the only thing that arrived was a mismatched reply, take the newest one
        rather than failing outright.
        """
        stale: Packet | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if stale is not None:
                    log.debug("accepting an unmatched reply for %s after timeout",
                              command_name(request.command))
                    return stale
                raise GnpTimeout(
                    f"no reply to {command_name(request.command)} within {self.timeout:g}s"
                )
            for packet in self.transport.receive(timeout=remaining):
                if packet.type is PacketType.REPLY or packet.command == COMMAND_NACK:
                    if packet.seq == request.seq:
                        log.debug("RX %s", packet)
                        return packet
                    # A late reply to a request that already gave up. Drop it, keep waiting.
                    log.debug("discarding stale reply seq %d (want %d, %s)",
                              packet.seq, request.seq, command_name(packet.command))
                    stale = packet
                    continue
                self._events.append(Event(packet, time.monotonic()))
                log.debug("RX event %s", packet)

    # -- convenience -----------------------------------------------------------------------

    def read(self, command: int, subcommand: int, *, address: int | None = None,
             timeout: float | None = None, payload: bytes = b"") -> bytes:
        """READ and return the reply payload *after* the echoed subcommand byte.

        Some reads take an **argument**: the equalizer is read as `7d 00`, and sending the bare
        subcommand yields a short, useless answer rather than an error.
        """
        reply = self.request(command, subcommand, payload, type=PacketType.READ,
                             address=address, timeout=timeout)
        return self._strip_subcommand(reply, subcommand)

    def write(self, command: int, subcommand: int, payload: bytes = b"", *,
              address: int | None = None, timeout: float | None = None) -> bytes:
        """WRITE and return the reply payload after the echoed subcommand byte."""
        reply = self.request(command, subcommand, payload, type=PacketType.WRITE,
                             address=address, timeout=timeout)
        return self._strip_subcommand(reply, subcommand)

    @staticmethod
    def _strip_subcommand(reply: Packet, subcommand: int) -> bytes:
        """Drop the leading subcommand byte if the device echoed it.

        Not all replies echo it, so this must not blindly slice — a value byte would be lost.
        """
        if reply.data and reply.data[0] == subcommand:
            return reply.data[1:]
        return reply.data

    def supports(self, command: int, subcommand: int, *,
                 address: int | None = None) -> bool:
        """Whether a read of this subcommand is implemented.

        Relies on the device answering an unknown subcommand with NACK 0xF6/0xFF rather than
        misbehaving. Use it to confirm a capability, not to sweep for them.
        """
        try:
            self.request(command, subcommand, type=PacketType.READ, address=address)
        except GnpNackError as exc:
            return not exc.unsupported
        except GnpTimeout:
            return False
        return True

    # -- events ----------------------------------------------------------------------------

    def drain_events(self, timeout: float = 0.0) -> list[Event]:
        """Events pushed by the device. Sends nothing, so it is safe to call on a timer."""
        with self._lock:
            for packet in self.transport.receive(timeout=timeout):
                if packet.type is PacketType.REPLY:
                    # A late reply to a request that already timed out; drop it rather than
                    # letting it masquerade as an event.
                    log.debug("discarding stray reply %s", packet)
                    continue
                self._events.append(Event(packet, time.monotonic()))
            events = list(self._events)
            self._events.clear()
        return events

    # -- endpoint discovery ----------------------------------------------------------------

    def _require_primary(self) -> int:
        if self.primary_address is None:
            raise GnpError(
                "primary address unknown — call discover_endpoints() first"
            )
        return self.primary_address

    def discover_endpoints(
        self, candidates: tuple[int, ...] = ADDRESS_CANDIDATES
    ) -> list[Endpoint]:
        """Identify every GNP device reachable on this link, cheapest reads only.

        One USB HID interface can front several GNP devices: on a Link 390 the dongle answers
        at 0x01 and the headset paired to it at 0x04, each with its own name, PID, firmware and
        settings. Which one owns a given setting is not guessable — `ancMode` is NACK
        "unknown sub-command" at the dongle and readable at the headset — so the caller has to
        know the difference. `select_primary()` applies the usual preference.

        Wrong addresses answer NACK 0xF5, so this is self-limiting; it is still a short
        validated probe rather than a sweep.
        """
        found: list[Endpoint] = []
        for address in candidates:
            try:
                raw_pid = self.read(Command.IDENT, _SUB_PID, address=address, timeout=1.5)
            except (GnpNackError, GnpTimeout):
                continue
            pid = int.from_bytes(raw_pid[:2], "little") if len(raw_pid) >= 2 else None
            found.append(
                Endpoint(
                    address=address,
                    product_id=pid,
                    name=self._identity_string(address, _SUB_NAME),
                    firmware=self._identity_string(address, _SUB_FIRMWARE),
                )
            )
        if found and self.primary_address is None:
            self.select_primary(found)
        return found

    def _identity_string(self, address: int, subcommand: int) -> str:
        """A length-prefixed IDENT string, or "" if the endpoint will not give it."""
        try:
            payload = self.read(0x02, subcommand, address=address, timeout=1.5)
        except (GnpNackError, GnpTimeout):
            return ""
        if not payload:
            return ""
        length = payload[0]
        body = payload[1:1 + length] if length <= len(payload) - 1 else payload
        return body.decode("utf-8", errors="replace").rstrip("\x00")

    def select_primary(self, endpoints: list[Endpoint]) -> int:
        """Choose the endpoint that owns the headset settings, and cache its address.

        Prefer a headset over a dongle or base: the interesting features (ANC, HearThrough,
        sidetone, button mapping) live on the headset, and a dongle NACKs them.
        """
        if not endpoints:
            raise GnpError("no GNP endpoints responded")
        headsets = [e for e in endpoints if e.looks_like_headset]
        chosen = (headsets or endpoints)[0]
        self.primary_address = chosen.address
        log.info("primary GNP endpoint: %s", chosen)
        return chosen.address


@dataclass(frozen=True)
class Endpoint:
    """One GNP device behind a link — a dongle, a base, or the headset itself."""

    address: int
    product_id: int | None
    name: str
    firmware: str

    #: Substrings that mark an endpoint as relaying rather than being the headset.
    RELAY_HINTS = ("link", "dongle", "base", "deskstand", "cradle")

    @property
    def looks_like_headset(self) -> bool:
        lowered = self.name.lower()
        return bool(lowered) and not any(h in lowered for h in self.RELAY_HINTS)

    def __str__(self) -> str:
        pid = f"0x{self.product_id:04X}" if self.product_id is not None else "?"
        kind = "headset" if self.looks_like_headset else "relay"
        return (f"0x{self.address:02X} {self.name or '<unnamed>'} "
                f"({pid}, fw {self.firmware or '?'}, {kind})")
