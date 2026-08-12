"""MDR frame layer — the wire format shared by every Sony WH-1000XM device.

This module is pure and has no I/O or Qt dependency, so it is unit-testable in
isolation (see tests/test_framing.py). It is a direct port of the framing in
Plutoberth's and mos9527's C++ clients, cross-checked against the reference doc
(SONY_WH1000_XM3_XM4_PROTOCOL.md §4).

Frame (before escaping):

    0x3E | DATA_TYPE(1) | SEQ(1) | LEN(4, big-endian) | PAYLOAD(LEN) | CKSUM(1) | 0x3C

* CKSUM = 8-bit sum of everything between the markers except the checksum byte,
  computed *before* escaping.
* Bytes 0x3C / 0x3D / 0x3E are escaped inside the region (0x3D is the sentinel).
"""

from __future__ import annotations

from dataclasses import dataclass

START_MARKER = 0x3E  # '>'
END_MARKER = 0x3C    # '<'
ESCAPE = 0x3D        # '='

# raw byte -> the byte that follows the 0x3D sentinel
_ESCAPE_MAP = {0x3C: 0x2C, 0x3D: 0x2D, 0x3E: 0x2E}
_UNESCAPE_MAP = {v: k for k, v in _ESCAPE_MAP.items()}


class FrameError(ValueError):
    """Raised when a frame cannot be decoded (bad checksum, truncation, ...)."""


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        mapped = _ESCAPE_MAP.get(b)
        if mapped is not None:
            out.append(ESCAPE)
            out.append(mapped)
        else:
            out.append(b)
    return bytes(out)


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == ESCAPE:
            if i + 1 >= n:
                raise FrameError("dangling escape sentinel at end of frame")
            nxt = data[i + 1]
            if nxt not in _UNESCAPE_MAP:
                raise FrameError(f"invalid escape sequence 0x3D {nxt:#04x}")
            out.append(_UNESCAPE_MAP[nxt])
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def encode(payload: bytes, data_type: int, seq: int) -> bytes:
    """Serialize one frame ready to write to the RFCOMM socket."""
    body = bytes((data_type & 0xFF, seq & 0xFF)) + len(payload).to_bytes(4, "big") + payload
    body += bytes((_checksum(body),))
    return bytes((START_MARKER,)) + _escape(body) + bytes((END_MARKER,))


@dataclass(frozen=True)
class Frame:
    data_type: int
    seq: int
    payload: bytes

    @property
    def command(self) -> int:
        """First payload byte = MDR Command (group << 4 | op). 0xFF if empty."""
        return self.payload[0] if self.payload else 0xFF


def decode(raw_between_markers: bytes) -> Frame:
    """Decode the bytes *between* (and excluding) the start and end markers."""
    body = _unescape(raw_between_markers)
    if len(body) < 7:
        raise FrameError(f"frame too short ({len(body)} bytes)")
    if _checksum(body[:-1]) != body[-1]:
        raise FrameError("checksum mismatch")
    data_type = body[0]
    seq = body[1]
    length = int.from_bytes(body[2:6], "big")
    payload = body[6:-1]
    if len(payload) != length:
        raise FrameError(f"declared length {length} != actual {len(payload)}")
    return Frame(data_type=data_type, seq=seq, payload=payload)


class FrameReader:
    """Incremental de-framer: feed it raw socket bytes, get whole frames out.

    Handles the fact that a single recv() may contain a partial frame, several
    frames, or a frame split across reads.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(START_MARKER)
            if start < 0:
                self._buf.clear()
                break
            end = self._buf.find(END_MARKER, start + 1)
            if end < 0:
                # keep from the start marker onward; wait for more bytes
                del self._buf[:start]
                break
            chunk = bytes(self._buf[start + 1:end])
            del self._buf[:end + 1]
            try:
                frames.append(decode(chunk))
            except FrameError:
                # skip a corrupt frame rather than stall the stream
                continue
        return frames
