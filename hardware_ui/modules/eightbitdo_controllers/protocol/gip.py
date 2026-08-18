"""GIP framing: the Xbox Game Input Protocol, as far as this module needs it.

Ported from the source project's ``gipclient.py``, which reconstructed it against ``xone`` and a
live controller. Only the parts a configuration tool needs are here -- there is no input decoding,
no rumble, no audio.

    header = [command][options][sequence][varint length]   padded to an even length
             followed by a varint chunk offset when options & CHUNK

``options`` carries the client id in its low nibble, which matters because **this is a multi-client
device**: client 0 is the gamepad and owns the config commands, client 1 is the headset. Sending a
config request to the wrong client gets silence.

The chunking rule is the part that took the source project three attempts, so it is spelled out on
:class:`Reassembler`.
"""

from __future__ import annotations

import struct

#: Transport commands. 0x12 and 0x13 are the pair that carry configuration; the rest are the
#: handshake the controller needs before it will answer anything.
CMD_ACK = 0x01
CMD_ANNOUNCE = 0x02
CMD_STATUS = 0x03
CMD_IDENTIFY = 0x04
CMD_POWER = 0x05
CMD_INPUT = 0x20
CMD_CONFIG_IN = 0x12
"""Device to host: configuration read responses."""
CMD_CONFIG_OUT = 0x13
"""Host to device: configuration reads *and* writes both go out on this."""

#: Option bits. The low nibble is the client id, so these all sit above it.
OPT_ACK = 0x10
OPT_INTERNAL = 0x20
OPT_CHUNK_START = 0x40
OPT_CHUNK = 0x80

#: The gamepad client. Configuration lives here; client 1 is the headset and has no config
#: commands at all.
CLIENT_GAMEPAD = 0

#: Power sub-command that forces a fresh announce, so the handshake can be driven from a cold
#: start rather than waiting for the controller to volunteer one.
POWER_RESET = b"\x07"


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        seven = value & 0x7F
        value >>= 7
        out.append(seven | (0x80 if value else 0))
        if not value:
            return bytes(out)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """``(value, offset just past it)``."""
    value = shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def encode_header(command: int, options: int, sequence: int, length: int) -> bytes:
    """Frame header, padded to an even length.

    The padding is not cosmetic: an odd-length header is rejected, and the trick the protocol uses
    is to set the continuation bit on the last varint byte and append a zero -- which decodes to
    the same value while making the header even.
    """
    header = bytearray([command, options, sequence & 0xFF]) + encode_varint(length)
    if len(header) % 2:
        header[-1] |= 0x80
        header.append(0)
    return bytes(header)


def decode_header(data: bytes) -> tuple[int, int, int, int, int, int]:
    """``(command, options, sequence, length, chunk_offset, payload_start)``."""
    if len(data) < 4:
        raise ValueError(f"runt frame: {data.hex(' ')}")
    command, options, sequence = data[0], data[1], data[2]
    length, offset = decode_varint(data, 3)
    chunk_offset = 0
    if options & OPT_CHUNK:
        chunk_offset, offset = decode_varint(data, offset)
    return command, options, sequence, length, chunk_offset, offset


def build(command: int, payload: bytes = b"", *, client: int = CLIENT_GAMEPAD,
          sequence: int = 1, internal: bool = False, acknowledge: bool = False) -> bytes:
    options = (client & 0x0F)
    if internal:
        options |= OPT_INTERNAL
    if acknowledge:
        options |= OPT_ACK
    return encode_header(command, options, sequence, len(payload)) + payload


def build_ack(command: int, received: int, total: int, *, client: int = CLIENT_GAMEPAD,
              sequence: int = 1) -> bytes:
    """The acknowledgement a chunked transfer needs to keep flowing.

    **This is the part that had to be got exactly right.** The source project's chunk reassembly
    failed until the ack carried ``received`` as bytes-so-far and ``remaining`` as bytes-left; an
    ack with zeros in those fields stalls the transfer silently.
    """
    body = struct.pack("<BBBHHH", 0, command, (client & 0x0F) | OPT_INTERNAL,
                       received, 0, max(0, total - received))
    return build(CMD_ACK, body, client=client, sequence=sequence, internal=True)


class Reassembler:
    """Collects a chunked message.

    The rule, which cost the source project three attempts: on a frame carrying
    ``OPT_CHUNK_START`` the header's chunk offset is **the total size**, not a position, and its
    data belongs at position 0. On every later frame it is a genuine offset. A zero-length chunked
    frame terminates the message.
    """

    __slots__ = ("buffer", "total")

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.total = 0

    def feed(self, options: int, chunk_offset: int, payload: bytes) -> bytes | None:
        """Add a frame. Returns the complete message once it is finished, else None."""
        if options & OPT_CHUNK_START:
            self.total = chunk_offset
            self.buffer = bytearray(chunk_offset)
            position = 0
        else:
            position = chunk_offset

        if not payload:
            return bytes(self.buffer)

        end = position + len(payload)
        if len(self.buffer) < end:
            self.buffer.extend(bytes(end - len(self.buffer)))
        self.buffer[position:end] = payload
        if self.total and end >= self.total:
            return bytes(self.buffer[:self.total])
        return None


class Sequence:
    """Per-client outgoing sequence numbers, which wrap 1..255 and never use 0."""

    __slots__ = ("_next",)

    def __init__(self) -> None:
        self._next: dict[int, int] = {}

    def take(self, client: int = CLIENT_GAMEPAD) -> int:
        value = self._next.get(client, 0) + 1
        if value > 0xFF:
            value = 1
        self._next[client] = value
        return value


__all__ = [
    "CLIENT_GAMEPAD", "CMD_ACK", "CMD_ANNOUNCE", "CMD_CONFIG_IN", "CMD_CONFIG_OUT",
    "CMD_IDENTIFY", "CMD_INPUT", "CMD_POWER", "CMD_STATUS", "OPT_ACK", "OPT_CHUNK",
    "OPT_CHUNK_START", "OPT_INTERNAL", "POWER_RESET", "Reassembler", "Sequence",
    "build", "build_ack", "decode_header", "decode_varint", "encode_header", "encode_varint",
]
