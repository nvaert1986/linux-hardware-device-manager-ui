"""GNP (GN Protocol) packet encoding/decoding.

Wire format, taken from GnProtocol.GnpPacketSerializer in Jabra's own GnProtocol.dll
(see docs/JABRA_GNP_PROTOCOL.md §3):

    byte 0    destination address   (must be non-zero)
    byte 1    source address
    byte 2    sequence number
    byte 3    (type << 6) | total length      total length includes this 5-byte header
    byte 4    command
    byte 5..  payload                         0..58 bytes

There is no checksum and no escaping — GNP relies on HID for integrity and delimiting.
Because of that, `total length` is the only framing signal we get, so it is validated against
the real report length on every decode rather than trusted.

One HID report carries exactly one packet: the maximum total length is 63 (6 bits) and the
Link 390's report 0x05 is 63 bytes. Smaller devices declare smaller reports (the Evolve2 85
deskstand declares 32), so the transport must not assume 63.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

HEADER_SIZE = 5
#: GnpPacket.MAX_DATA_SIZE — the largest payload that fits the 6-bit length field.
MAX_DATA_SIZE = 0x3A
#: The length field is 6 bits, so a whole packet can never exceed this.
MAX_PACKET_SIZE = 0x3F

_LENGTH_MASK = 0x3F
_TYPE_SHIFT = 6


class PacketType(enum.IntEnum):
    """GnpPacket.PacketType — the top two bits of byte 3."""

    EVENT = 0
    READ = 1
    WRITE = 2
    REPLY = 3


class FramingError(ValueError):
    """Raised when a byte string is not a well-formed GNP packet."""


@dataclass(frozen=True)
class Packet:
    command: int
    dest: int
    src: int = 0
    seq: int = 0
    type: PacketType = PacketType.READ
    data: bytes = b""

    def __post_init__(self) -> None:
        for name, value in (("dest", self.dest), ("src", self.src),
                            ("seq", self.seq), ("command", self.command)):
            if not 0 <= value <= 0xFF:
                raise FramingError(f"{name} out of range: {value}")

    @property
    def total_length(self) -> int:
        return HEADER_SIZE + len(self.data)

    def encode(self) -> bytes:
        # Serialize() throws on a zero destination; mirror that rather than emitting a packet
        # the device will ignore.
        if self.dest == 0:
            raise FramingError("destination address cannot be 0")
        if len(self.data) > MAX_DATA_SIZE:
            raise FramingError(
                f"payload too long: {len(self.data)} > {MAX_DATA_SIZE}"
            )
        return bytes(
            [
                self.dest,
                self.src,
                self.seq,
                (int(self.type) << _TYPE_SHIFT) | self.total_length,
                self.command,
            ]
        ) + self.data

    @classmethod
    def decode(cls, buf: bytes, *, strict: bool = True) -> Packet:
        """Decode one packet.

        `strict` rejects a declared length that overruns the buffer. The vendor's
        Deserialize() silently clamps instead; pass strict=False to match it when replaying
        vendor captures, but keep it on for live traffic so truncation is not mistaken for a
        short payload.
        """
        if len(buf) < HEADER_SIZE:
            raise FramingError(f"truncated: need {HEADER_SIZE} bytes, got {len(buf)}")
        flags = buf[3]
        total = flags & _LENGTH_MASK
        if total < HEADER_SIZE:
            raise FramingError(f"declared length {total} is shorter than the header")
        available = len(buf) - HEADER_SIZE
        want = total - HEADER_SIZE
        if want > available:
            if strict:
                raise FramingError(
                    f"declared length {total} overruns the {len(buf)}-byte buffer"
                )
            want = available
        return cls(
            dest=buf[0],
            src=buf[1],
            seq=buf[2],
            type=PacketType(flags >> _TYPE_SHIFT),
            command=buf[4],
            data=bytes(buf[HEADER_SIZE:HEADER_SIZE + want]),
        )

    def __str__(self) -> str:
        return (
            f"{self.type.name:5} cmd=0x{self.command:02X} "
            f"{self.src:#04x}->{self.dest:#04x} seq={self.seq:3} "
            f"len={self.total_length:2} data={self.data.hex(' ') or '-'}"
        )


class SequenceCounter:
    """Sequence numbers, matching GnProtocolOverUsbHid exactly.

    Two rules, both read out of the vendor's IL:

    * The constructor seeds from `Random().Next(0, 0xFE)` — a random start, not 0.
    * `GetSequenceNumber()` takes a lock, pre-increments, and **if the result is 0 it forces
      it to 1**. So a live sequence number is 1..255 and zero never goes on the wire; 0xFF is
      allowed, despite 0xFE being the NACK *command* value (a different field).

    The pre-increment matters: the first number issued is `seed + 1`, not `seed`.
    """

    def __init__(self, start: int | None = None) -> None:
        if start is None:
            import random

            start = random.randrange(0, 0xFE)
        self._value = start & 0xFF

    def peek(self) -> int:
        """The value `next()` would return."""
        return self._advance(self._value)

    @staticmethod
    def _advance(value: int) -> int:
        nxt = (value + 1) & 0xFF
        return 1 if nxt == 0 else nxt

    def next(self) -> int:
        self._value = self._advance(self._value)
        return self._value
