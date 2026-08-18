"""Creative CDC wire framing.

    5A <cmd:u8> <len:u8>  <payload...>      short form
    6A <cmd:u8> <len:u16> <payload...>      long form (payload > 255)

`len` counts payload bytes only. There is no checksum. Values are little-endian.

Verified against both the Windows USB capture and the Android BLE capture — the
same framing is used on both transports.
"""

from __future__ import annotations

import struct

START_5A = 0x5A
START_6A = 0x6A
#: Status/mode frames the device emits use 0x5B rather than 0x5A.
START_5B = 0x5B


class ProtocolError(Exception):
    pass


def build(cmd: int, payload: bytes = b"", *, force_long: bool = False) -> bytes:
    """Frame a command, choosing the short form unless the payload is too big."""
    if len(payload) > 0xFFFF:
        raise ProtocolError(f"payload too large: {len(payload)} bytes")
    if force_long or len(payload) > 0xFF:
        return bytes([START_6A, int(cmd)]) + struct.pack("<H", len(payload)) + payload
    return bytes([START_5A, int(cmd), len(payload)]) + payload


def parse(data: bytes) -> tuple[int, bytes]:
    """Parse one frame -> (command, payload). Trailing bytes are ignored."""
    frames = split(data)
    if not frames:
        raise ProtocolError(f"no frame in {data[:16].hex(' ')}")
    return frames[0]


def split(data: bytes) -> list[tuple[int, bytes]]:
    """Split a bulk transfer into every frame it contains.

    A single USB transfer regularly carries several frames — a write triggers a
    burst (Acknowledge, then the changed state, then dependent state), so callers
    must handle more than one.
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    while i + 3 <= len(data):
        start = data[i]
        if start in (START_5A, START_5B):
            length, hdr = data[i + 2], 3
        elif start == START_6A:
            if i + 4 > len(data):
                break
            length, hdr = struct.unpack_from("<H", data, i + 2)[0], 4
        else:
            i += 1          # resync past padding
            continue
        end = i + hdr + length
        if end > len(data):
            break
        out.append((data[i + 1], data[i + hdr:end]))
        i = end
    return out


# -- payload helpers -------------------------------------------------------


def get_effect(module: int, command: int) -> bytes:
    """GetMalcolmParameter payload: [count=1, module, command]."""
    return bytes([1, int(module), int(command)])


def set_effect(module: int, command: int, value: float) -> bytes:
    """SetMalcolmParameter payload: [count=1, module, command] + float32 LE."""
    return bytes([1, int(module), int(command)]) + struct.pack("<f", float(value))


def parse_effect(payload: bytes) -> list[tuple[int, int, float]]:
    """Parse a parameter response -> [(module, command, value), ...].

        <count:u8> <more:u8> then count * (module u8, command u8, float32 LE)

    `count` is a single byte: a hardware push split the 12 EQ parameters as
    `09 01 ...` then `03 00 ...`, so reading it as a uint16 gives 265.
    """
    if len(payload) < 2:
        return []
    out: list[tuple[int, int, float]] = []
    off = 2
    for _ in range(payload[0]):
        if off + 6 > len(payload):
            break
        out.append((payload[off], payload[off + 1],
                    struct.unpack_from("<f", payload, off + 2)[0]))
        off += 6
    return out


def effect_has_more(payload: bytes) -> bool:
    """True when the device will send further frames for this batch."""
    return len(payload) >= 2 and payload[1] == 1


def parse_string(payload: bytes) -> str:
    """Text out of a DeviceInfo response.

    The layout is not uniform, verified on hardware:
        op2 firmware: 02 10 '1.7.250324.0910' 00   <- op, length, text, NUL
        op3 serial  : 03 'YDSB1815150001619Q' 00   <- op, text, NUL (no length)
    """
    if not payload:
        return ""
    body = payload[1:]
    # When present, the length byte counts the text *plus* its NUL terminator,
    # so it equals len(body) - 1. Requiring it to be non-printable stops a
    # serial like 'Y' (0x59) being mistaken for a length.
    if len(body) >= 2 and body[0] == len(body) - 1 and not (32 <= body[0] < 127):
        body = body[1:]
    return body.split(b"\x00")[0].decode("utf-8", errors="replace")
