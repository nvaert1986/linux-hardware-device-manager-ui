"""High-level Deckard session and device model.

Ported verbatim from ``plasma-hp-poly-protocol-headphone-support/plasma_poly_headset/device.py``,
the implementation verified on a live V4310 over both Bluetooth and the BT700 dongle. Only the
imports changed, to match this package layout. Every rule it encodes -- event-before-ack, the
per-address handshake, the bounded event backlog, the refusal to issue write-only actions -- was
established on hardware, so the adapter wraps it rather than reimplementing it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import catalogue as cat
from .framing import Frame, MessageType
from .ids import COMMAND_UNKNOWN, SETTING_UNKNOWN, exception_name, name_for
from .rfcomm import RfcommTransport

PROTOCOL_VERSION_ID = 0x0102
CONNECTION_STATUS_ID = 0x0C00

#: BladeRunner address of the transport's own endpoint.
ADDRESS_LOCAL = 0x0000


def br_address(port: int) -> int:
    """BladeRunner address for a downstream port: byte0 is (dest << 4) | src."""
    return (port & 0x0F) << 12

# Identity settings worth reading at connect. Ids from the Setting table.
IDENTITY = {
    "USB_PID": 0x0A02,
    "TATTOO_SERIAL_NUMBER": 0x0A01,
    "TATTOO_BUILD_CODE": 0x0A03,
    "FIRMWARE_VERSION": 0x0A04,
    "HARDWARE_REVISION_STRING": 0x0A53,
    "GENES_GUID": 0x0A1E,
    "STACK_VERSION": 0x0A1F,
    "CURRENT_LANGUAGE": 0x0E1A,
}


class DeckardError(RuntimeError):
    pass


class SettingUnsupported(DeckardError):
    """The device answered SETTING_UNKNOWN — it does not implement this id."""


class CommandUnsupported(DeckardError):
    """The device answered COMMAND_UNKNOWN — the setting is readable but not writable.

    Seen on the V4310 for linkQualityReporting (0x0F54): the read succeeds, the write is refused.
    Treat the control as read-only rather than as an error.
    """


BATTERY_INFO_ID = 0x0A1A

#: Cap on unconsumed events kept in memory. The device reports things that are not settings —
#: HEADSET_BUTTONS_PRESSED_REPORT (0x0E2B) fires on every mute/volume press — and nothing ever
#: matches those to a catalogue entry, so without a bound they accumulate for the life of the
#: session. Only the newest matter: a write looks for its own event immediately after sending.
MAX_EVENT_BACKLOG = 32


@dataclass
class Reply:
    frame: Frame
    events: list[Frame] = field(default_factory=list)


@dataclass
class BatteryInfo:
    """BATTERY_INFO (0x0A1A) — CBatteryInfoSettingPayloadOut.

    Fields, in @Structure.FieldOrder: level, num_levels, charging, minutes_of_talk_time (u16),
    talk_time_is_high_estimate. The level is a step out of num_levels, not a percentage.
    """

    level: int
    num_levels: int
    charging: bool
    talk_minutes: int
    talk_time_is_high_estimate: bool

    @property
    def percent(self) -> int | None:
        if self.num_levels <= 0:
            return None
        return round(100 * self.level / self.num_levels)

    def __str__(self) -> str:
        pct = self.percent
        parts = [f"{pct}%" if pct is not None else f"{self.level}/{self.num_levels}"]
        parts.append(f"({self.level}/{self.num_levels} steps)")
        if self.charging:
            parts.append("charging")
        if self.talk_minutes:
            hours, minutes = divmod(self.talk_minutes, 60)
            estimate = "up to " if self.talk_time_is_high_estimate else ""
            parts.append(f"{estimate}{hours}h{minutes:02d}m talk time")
        return ", ".join(parts)

    @classmethod
    def parse(cls, payload: bytes) -> "BatteryInfo | None":
        if len(payload) < 6:
            return None
        return cls(
            level=payload[0],
            num_levels=payload[1],
            charging=bool(payload[2]),
            talk_minutes=int.from_bytes(payload[3:5], "big"),
            talk_time_is_high_estimate=bool(payload[5]),
        )


@dataclass
class DeviceState:
    """Everything the GUI renders for one headset."""

    address: str
    name: str = ""
    endpoint: str = ""
    pid: int | None = None
    identity: dict[str, str] = field(default_factory=dict)
    battery: BatteryInfo | None = None
    #: settingName -> current value name (or raw hex when the catalogue has no matching choice)
    settings: dict[str, str] = field(default_factory=dict)
    #: settingNames the device actually implements (answered without SETTING_UNKNOWN)
    supported: set[str] = field(default_factory=set)


def _legible(text: str) -> float:
    """Fraction of *text* that looks like something a device would print.

    Deliberately crude. Hardware revisions, serials and build codes are ASCII in practice --
    letters, digits, and a little punctuation -- so anything outside that is evidence the bytes
    were not text, or were not text in the encoding just tried.

    **NULs count against a candidate**, which is the whole reason this scores rather than sniffs.
    UTF-16BE text read as UTF-8 comes out as every other character being a NUL; ignore those and
    the wrong decoding scores a perfect 1.0 and wins.
    """
    if not text:
        return 0.0
    good = sum(1 for c in text if c.isascii() and (c.isalnum() or c in " .-_/+:,()[]#"))
    return good / len(text)


#: Below this, a decode is treated as "these bytes are not text in this encoding". Set low on
#: purpose: the job is to reject binary rendered as CJK, not to police unusual model names.
LEGIBLE_ENOUGH = 0.8


def decode_string(payload: bytes) -> str:
    """Deckard strings: u16 big-endian byte length, then the bytes.

    Most are ASCII; HARDWARE_REVISION_STRING is UTF-16BE and zero-padded to its field width.

    **Both encodings are tried and the more legible result wins**, rather than sniffing the first
    two bytes for a NUL. The sniff was carried over from the reference implementation, which was
    only ever run against a Voyager 4310, and it has two failure modes that a 4320 hit: an ASCII
    string with a single leading NUL decodes as CJK, and a field that is not text at all decodes
    as CJK with no complaint whatsoever. Returning "" for the second case lets the caller fall
    back to hex, which is at least true.

    Returns "" when neither encoding produces plausible text, so the caller can decide.
    """
    if len(payload) < 2:
        return ""
    length = int.from_bytes(payload[:2], "big")
    body = payload[2:2 + length]
    if not body:
        return ""

    # Trailing NULs are field padding in both encodings and say nothing about which is right.
    candidates = [body.decode("utf-8", "replace").rstrip("\x00")]
    if len(body) >= 2:
        # UTF-16BE needs an even number of bytes; an odd tail is padding, not a character.
        candidates.append(body[:len(body) - len(body) % 2]
                          .decode("utf-16-be", "replace").rstrip("\x00"))

    best = max(candidates, key=_legible)
    if _legible(best) < LEGIBLE_ENOUGH:
        return ""
    # Any NUL still inside the winner is padding the device wrote, not part of the name.
    return best.replace("\x00", "")


class PolyHeadset:
    """A connected headset. Reads are safe; writes are explicit and separate."""

    def __init__(self, address: str, transport: RfcommTransport | None = None):
        self.address = address
        self.transport = transport or RfcommTransport(address)
        #: BladeRunner address every frame is sent to. 0 over Bluetooth (we talk to the endpoint
        #: directly); on a USB dongle this becomes the downstream port the headset sits on.
        self.br_address = ADDRESS_LOCAL
        self.events: list[Frame] = []
        #: METADATA_TYPE capability blobs sent by the device at session start. Kept separate from
        #: events — its id field is a count, not an Event id, so it must not be name-resolved.
        self.metadata: list[Frame] = []
        self.catalogue: cat.Catalogue | None = None
        #: Human-readable transport description, e.g. "RFCOMM channel 15" or "USB HID /dev/hidraw15"
        self.endpoint: str = ""

    # -- lifecycle ---------------------------------------------------------------------------

    def connect(self) -> None:
        self.transport.connect()
        self.endpoint = self.transport.description
        self._handshake()
        if getattr(self.transport, "has_downstream_ports", False):
            self._attach_downstream()
        pid = self.read_int("USB_PID", IDENTITY["USB_PID"])
        if pid is not None:
            self.catalogue = cat.load(pid)

    def downstream_ports(self) -> list[int]:
        """Ports the transport endpoint reports as having a device attached.

        CONNECTION_STATUS payload is {downstream_port_ids, connected_port_ids,
        originating_port_id}, each array prefixed with a u16 count.
        """
        try:
            payload = self.read_raw(CONNECTION_STATUS_ID)
        except (SettingUnsupported, DeckardError):
            return []
        try:
            n = int.from_bytes(payload[0:2], "big")
            rest = payload[2 + n:]
            m = int.from_bytes(rest[0:2], "big")
            connected = list(rest[2:2 + m])
            originating = rest[2 + m]
        except IndexError:
            return []
        return [p for p in connected if p != originating]

    def _attach_downstream(self) -> None:
        """Greet the attached downstream device and talk to it from here on.

        A USB dongle answers for itself at address 0, but what the user cares about is the headset
        behind it. Each address needs its own PROTOCOL_VERSION handshake before it will answer
        anything.

        **Only if the walk went deeper.** Poly links are symmetric: a headset in its charging
        stand also reports a downstream device, and that device is the dongle it is paired to. Walk
        blindly and the stand's entry ends up configuring the dongle, and reporting the dongle's
        serial number as though it were the headset's -- which is exactly what happened.

        The test is whether the device we landed on is *separately plugged in*. If its USB product
        id belongs to something else on the bus, it has its own row in the application and this is
        not the way to reach it; stay where we are. A headset behind a dongle is not on the bus in
        its own right, so it passes.
        """
        peers = self._peer_product_ids()
        for port in self.downstream_ports():
            address = br_address(port)
            try:
                self._handshake(address)
            except DeckardError:
                continue
            self.br_address = address
            product = self._attached_product_id()
            # A port that greets but names no product is an empty socket -- a dongle lists ports
            # it could use, not only ports it is using. Landing there gave a page of blanks.
            if product is None or product in peers:
                self.br_address = ADDRESS_LOCAL
                continue
            self.endpoint += f" → port {port}"
            return

    def _peer_product_ids(self) -> set[int]:
        getter = getattr(self.transport, "peer_product_ids", None)
        return set(getter()) if callable(getter) else set()

    def _attached_product_id(self) -> int | None:
        """The USB product id of whatever we have just attached to, or ``None`` if it will
        not say -- which is itself the answer: there is nothing useful on that port."""
        try:
            return self.read_int("USB_PID", IDENTITY["USB_PID"])
        except (SettingUnsupported, DeckardError):
            return None

    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _handshake(self, address: int = ADDRESS_LOCAL) -> None:
        """Announce our protocol version to one BladeRunner address.

        Each addressable device needs its own handshake — a dongle will not route settings to
        the headset behind it until that headset has been greeted at its own address.
        """
        self.transport.send(
            Frame(MessageType.PROTOCOL_VERSION, PROTOCOL_VERSION_ID, bytes([1]),
                  reserved=address)
        )
        deadline = time.monotonic() + 5.0
        saw_version = False
        while time.monotonic() < deadline:
            for frame in self.transport.receive(timeout=1.0):
                if frame.message_type is MessageType.DEVICE_PROTOCOL_VERSION:
                    saw_version = True
                elif frame.message_type is MessageType.METADATA:
                    self.metadata.append(frame)
                elif frame.message_type is MessageType.EVENT:
                    self.events.append(frame)
            if saw_version:
                return
        raise DeckardError(
            f"no protocol version answer from address 0x{address:04X}")

    # -- request/response --------------------------------------------------------------------

    def _exchange(self, request: Frame, timeout: float = 3.0) -> Frame:
        """Send a request and wait for the reply that matches its id, stashing events."""
        if request.message_type is MessageType.SETTINGS_REQUEST:
            ok, err = (
                MessageType.SETTING_RESULT_SUCCESS,
                MessageType.SETTING_RESULT_EXCEPTION,
            )
        elif request.message_type is MessageType.PERFORM_COMMAND:
            ok, err = (
                MessageType.PERFORM_COMMAND_RESULT_SUCCESS,
                MessageType.PERFORM_COMMAND_RESULT_EXCEPTION,
            )
        else:
            raise ValueError(f"cannot match a reply for {request.message_type.name}")

        self.transport.send(request)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.transport.receive(timeout=max(0.1, deadline - time.monotonic())):
                # The device emits the EVENT notification BEFORE the command ack — never treat
                # an unexpected type as the reply, just stash it.
                if frame.message_type is MessageType.METADATA:
                    self.metadata.append(frame)
                    continue
                if frame.message_type is MessageType.EVENT:
                    self._stash(frame)
                    continue
                if frame.message_id != request.message_id:
                    self._stash(frame)
                    continue
                if frame.message_type is ok:
                    return frame
                if frame.message_type is err:
                    code = int.from_bytes(frame.payload[:2], "big") if frame.payload else -1
                    if code == SETTING_UNKNOWN:
                        raise SettingUnsupported(
                            f"{name_for(request.message_type, request.message_id)} "
                            f"(0x{request.message_id:04X}) not supported"
                        )
                    if code == COMMAND_UNKNOWN:
                        raise CommandUnsupported(
                            f"{name_for(request.message_type, request.message_id)} "
                            f"(0x{request.message_id:04X}) is read-only on this device"
                        )
                    raise DeckardError(
                        f"device returned {exception_name(code) or code} for "
                        f"0x{request.message_id:04X}"
                    )
        raise DeckardError(f"timed out waiting for reply to 0x{request.message_id:04X}")

    # -- reads -------------------------------------------------------------------------------

    def read_raw(self, setting_id: int, timeout: float = 3.0) -> bytes:
        return self._exchange(
            Frame(MessageType.SETTINGS_REQUEST, setting_id, reserved=self.br_address),
            timeout=timeout,
        ).payload

    def read_int(self, label: str, setting_id: int) -> int | None:
        try:
            return int.from_bytes(self.read_raw(setting_id), "big")
        except (SettingUnsupported, DeckardError):
            return None

    def supports(self, setting_id: int) -> bool:
        """Probe whether the device implements a setting. Safe: unsupported ids answer cleanly."""
        try:
            self.read_raw(setting_id)
            return True
        except SettingUnsupported:
            return False

    # -- writes ------------------------------------------------------------------------------

    def write_choice(self, setting: cat.Setting, choice_name: str, timeout: float = 5.0) -> str:
        """Set `setting` to one of its named catalogue values. Returns the value now in effect.

        Writes go out as PERFORM_COMMAND using the setting's *set* id, which is not always the
        same as its get id. Refuses write-only actions outright — those are destructive
        (restoreDefaults, clearPairedDevices) and must never be reachable from a generic setter.

        Verification uses the device's own change EVENT, which is authoritative and arrives
        *before* the command ack. Re-reading immediately instead is unreliable: the device can
        still report the old value for a short while after acking, which shows up in a UI as the
        control snapping back to its previous setting.
        """
        if setting.is_action:
            raise DeckardError(
                f"{setting.name} is a write-only action, not a setting — refusing to issue it"
            )
        if setting.set_id is None:
            raise DeckardError(f"{setting.name} has no set id — it is read-only")
        choice = setting.choice(choice_name)
        if choice is None:
            options = ", ".join(c.name for c in setting.choices) or "<none>"
            raise DeckardError(f"{setting.name} has no value {choice_name!r}; options: {options}")

        # No pre-read: it cost a round trip (~100 ms of a ~190 ms write) purely to make the
        # error message nicer, and halving write latency matters more for UI responsiveness.
        self._exchange(
            Frame(MessageType.PERFORM_COMMAND, setting.set_id, choice.payload,
                  reserved=self.br_address),
            timeout=timeout,
        )

        # Poly Lens Desktop takes the event *and* re-reads the one setting it wrote (observed
        # on 43/43 writes, ~40 ms later). The event alone is authoritative, but the confirming
        # read is cheap and catches a device that acked without committing.
        observed = self._event_value(setting)
        if observed is None or observed != choice_name:
            observed = self._settled_value(setting, expect=choice_name)
        if observed != choice_name:
            raise DeckardError(
                f"{setting.name}: wrote {choice_name!r} but device reports {observed!r}"
            )
        return observed

    def _stash(self, frame: Frame) -> None:
        """Keep an unsolicited frame for later, bounded — see MAX_EVENT_BACKLOG."""
        self.events.append(frame)
        if len(self.events) > MAX_EVENT_BACKLOG:
            del self.events[:-MAX_EVENT_BACKLOG]

    def _event_value(self, setting: cat.Setting) -> str | None:
        """Newest change EVENT for this setting, decoded. Consumes it from the inbox."""
        if setting.event_id is None:
            return None
        matching = [f for f in self.events if f.message_id == setting.event_id]
        if not matching:
            return None
        self.events = [f for f in self.events if f.message_id != setting.event_id]
        return setting.decode(matching[-1].payload)

    def _settled_value(self, setting: cat.Setting, expect: str | None = None,
                       attempts: int = 4, delay: float = 0.15) -> str | None:
        """Re-read until the value settles on `expect`, or attempts run out.

        Fallback for devices that do not emit a change event for a given setting.
        """
        value = None
        for i in range(attempts):
            value = self.read_choice(setting)
            if expect is None or value == expect:
                return value
            if i < attempts - 1:
                time.sleep(delay)
        return value

    def drain_events(self) -> dict[str, str]:
        """Non-blocking read of unsolicited change events -> {settingName: value}.

        This is how Poly Lens Desktop learns about changes made with the headset's own buttons:
        it polls nothing at all — the link sits silent for tens of seconds — and reacts to
        EVENT messages instead. Far cheaper than re-reading every setting, and it cannot publish
        stale state.
        """
        try:
            for frame in self.transport.receive(timeout=0):
                if frame.message_type is MessageType.EVENT:
                    self.events.append(frame)
                elif frame.message_type is MessageType.METADATA:
                    self.metadata.append(frame)
        except Exception:  # noqa: BLE001 - a dead link is reported by the next real exchange
            return {}
        if self.catalogue is None or not self.events:
            return {}

        by_event_id = {s.event_id: s for s in self.catalogue.settings if s.event_id is not None}
        changed: dict[str, str] = {}
        unconsumed = []
        for frame in self.events:
            setting = by_event_id.get(frame.message_id)
            value = setting.decode(frame.payload) if setting else None
            if value is None:
                unconsumed.append(frame)
                continue
            changed[setting.name] = value
        # Device reports that are not settings never match, so bound what we keep.
        self.events = unconsumed[-MAX_EVENT_BACKLOG:]
        return changed

    def read_choice(self, setting: cat.Setting) -> str | None:
        """Current value of a setting as its catalogue name, or raw hex if unrecognised."""
        if setting.get_id is None:
            return None
        raw = self.read_raw(setting.get_id)
        return setting.decode(raw) or raw.hex()

    # -- aggregate ---------------------------------------------------------------------------

    def read_battery(self) -> BatteryInfo | None:
        try:
            return BatteryInfo.parse(self.read_raw(BATTERY_INFO_ID))
        except (SettingUnsupported, DeckardError):
            return None

    def snapshot(self, name: str = "") -> DeviceState:
        """Read everything the GUI needs. Reads only — never issues a catalogue action."""
        state = DeviceState(
            address=self.address,
            name=name,
            endpoint=self.endpoint,
            pid=self.catalogue.pid if self.catalogue else None,
            identity=self.identity(),
            battery=self.read_battery(),
        )
        if self.catalogue is None:
            return state
        for setting in self.catalogue.settings:
            if setting.is_action or setting.get_id is None:
                continue
            try:
                value = self.read_choice(setting)
            except SettingUnsupported:
                continue
            except DeckardError:
                continue
            state.supported.add(setting.name)
            if value is not None:
                state.settings[setting.name] = value
        return state

    def identity(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for label, setting_id in IDENTITY.items():
            try:
                raw = self.read_raw(setting_id)
            except (SettingUnsupported, DeckardError):
                # Absent, not "<SettingUnsupported>". The reference project put the exception's
                # class name in the field as a debugging aid; on a page it is a row of jargon
                # where the rule everywhere else is that what a device will not answer is simply
                # not shown. A BT700 dongle refuses the Bluetooth-stack read and said so in Greek.
                continue
            if label in ("TATTOO_SERIAL_NUMBER", "TATTOO_BUILD_CODE", "HARDWARE_REVISION_STRING"):
                text = decode_string(raw)
                if text.strip():
                    out[label] = text
                elif any(raw[2:]):
                    # Answered with bytes that are not text in either encoding. Show them as hex,
                    # the way STACK_VERSION already is: hex is jargon, but it is *true*, and a
                    # 4320 rendering its hardware revision as Chinese characters is the failure
                    # this replaces. If a model turns up whose field is genuinely a string in some
                    # third encoding, the hex is what makes it diagnosable.
                    out[label] = raw[2:].hex()
                # else: answered with nothing -- a blank row says less than none.
            elif label == "GENES_GUID":
                body = raw[2:] if len(raw) > 16 else raw
                h = body.hex()
                out[label] = f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
            elif label == "USB_PID":
                out[label] = f"0x{int.from_bytes(raw, 'big'):04X}"
            else:
                out[label] = raw.hex()
        return out
