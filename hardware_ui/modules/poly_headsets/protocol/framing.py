"""Deckard frame encoding/decoding.

Wire format on the PltHeadsetDataService RFCOMM channel:

    byte 0..1   0x1LLL   high nibble 1 = SOF/version, low 12 bits = length of the remainder
    byte 2..3   reserved (always 0x0000 observed)
    byte 4..5   message type   (MessageType, big-endian u16)
    byte 6..7   message id     (Setting / Command / Event id, big-endian u16)
    byte 8..    payload

There is no checksum, no escaping and no sequence/ACK layer — RFCOMM's own reliability is relied
upon. Requests are matched to responses by (message type, message id).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

SOF_NIBBLE = 0x1
HEADER_LEN = 8
MAX_PAYLOAD = 0xFFF - (HEADER_LEN - 2)


class MessageType(enum.IntEnum):
    """From com.plantronics.headsetservice.model.MessageType (the complete table).

    Note the narrower com.poly.devicesdk.MessageType omits the transport-level values.
    """

    UNKNOWN = 0
    PROTOCOL_VERSION = 1
    SETTINGS_REQUEST = 2
    SETTING_RESULT_SUCCESS = 3
    SETTING_RESULT_EXCEPTION = 4
    PERFORM_COMMAND = 5
    PERFORM_COMMAND_RESULT_SUCCESS = 6
    PERFORM_COMMAND_RESULT_EXCEPTION = 7
    DEVICE_PROTOCOL_VERSION = 8
    METADATA = 9
    EVENT = 10
    CLOSE_SESSION = 11
    HOST_PROTOCOL_NEGOTIATION_REJECTION = 12
    START_DFU = 13
    STATUS_DFU = 14
    XEVENT = 15
    START_NEO = 16
    FINALIZE_NEO = 17
    FINALIZE_NEO_RESPONSE = 18


#: Message types whose ids come from the Setting table. See ids.table_for_type().
SETTING_TYPES = frozenset(
    {
        MessageType.SETTINGS_REQUEST,
        MessageType.SETTING_RESULT_SUCCESS,
        MessageType.SETTING_RESULT_EXCEPTION,
    }
)
COMMAND_TYPES = frozenset(
    {
        MessageType.PERFORM_COMMAND,
        MessageType.PERFORM_COMMAND_RESULT_SUCCESS,
        MessageType.PERFORM_COMMAND_RESULT_EXCEPTION,
    }
)


class FramingError(ValueError):
    """Raised when a byte string is not a well-formed Deckard frame."""


@dataclass(frozen=True)
class Frame:
    message_type: MessageType
    message_id: int
    payload: bytes = b""
    reserved: int = 0

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD:
            raise FramingError(f"payload too long: {len(self.payload)} > {MAX_PAYLOAD}")
        body = (
            self.reserved.to_bytes(2, "big")
            + int(self.message_type).to_bytes(2, "big")
            + self.message_id.to_bytes(2, "big")
            + self.payload
        )
        length = len(body)
        return bytes([(SOF_NIBBLE << 4) | (length >> 8), length & 0xFF]) + body

    @classmethod
    def decode(cls, data: bytes) -> "Frame":
        frame, rest = cls.decode_one(data)
        if rest:
            raise FramingError(f"{len(rest)} trailing bytes after frame")
        return frame

    @classmethod
    def decode_one(cls, data: bytes) -> tuple["Frame", bytes]:
        """Decode the first frame in `data`; return (frame, remaining bytes)."""
        if len(data) < 2:
            raise FramingError("truncated: need at least 2 bytes for the length header")
        if (data[0] >> 4) != SOF_NIBBLE:
            raise FramingError(f"bad SOF nibble 0x{data[0] >> 4:x}, expected 0x1")
        length = ((data[0] & 0x0F) << 8) | data[1]
        total = 2 + length
        if len(data) < total:
            raise FramingError(f"truncated: declared {total} bytes, got {len(data)}")
        if length < HEADER_LEN - 2:
            raise FramingError(f"frame too short to hold a header: length={length}")
        body = data[2:total]
        raw_type = int.from_bytes(body[2:4], "big")
        try:
            message_type = MessageType(raw_type)
        except ValueError:
            raise FramingError(f"unknown message type {raw_type}") from None
        return (
            cls(
                message_type=message_type,
                message_id=int.from_bytes(body[4:6], "big"),
                payload=bytes(body[6:]),
                reserved=int.from_bytes(body[0:2], "big"),
            ),
            data[total:],
        )


@dataclass
class FrameBuffer:
    """Accumulates socket reads and yields complete frames."""

    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            try:
                frame, rest = Frame.decode_one(bytes(self._buf))
            except FramingError as exc:
                if "truncated" in str(exc):
                    break  # wait for more bytes
                raise
            frames.append(frame)
            self._buf = bytearray(rest)
        return frames
