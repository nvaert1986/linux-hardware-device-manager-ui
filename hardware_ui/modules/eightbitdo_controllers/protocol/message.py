"""The inner configuration message, shared by both transports.

The single most useful fact about this protocol: **USB and BLE carry the same bytes.** Over BLE
they are written straight to an ATT characteristic; over USB they are wrapped in a GIP ``0x13``
frame. Everything in this file is therefore transport-independent, which is why the two backends
end up so thin.

    request   04 | cmd:u16 | field:u16 | size:u32 | total:u32 | offset:u32 | data   (17 bytes)
    response  04 | status:u8 | cmd:u16 | size:u32 | total:u32 | offset:u32 | data   (16 bytes)

**The two headers are different lengths**, which is easy to miss and expensive to get wrong. A
response is not a request with a status byte bolted on: the ``u8`` status replaces the ``u16``
field, so the header is one byte shorter and the data starts at 16 rather than 17. The source
project sliced requests at ``[17:]`` and responses at ``[16:]`` for exactly this reason.

Every packet is prefixed with report id ``0x04``. Captured from the vendor Android app's own
logging, so this is what the app sends rather than a reconstruction.
"""

from __future__ import annotations

import struct

REPORT_ID = 0x04

#: Header lengths. Different by one byte; see the module docstring.
REQUEST_HEADER_LEN = 17
RESPONSE_HEADER_LEN = 16

# ------------------------------------------------------------------ commands

CMD_WRITE_SUPER = 0x0001
"""Write the 532-byte super-config. Used by both transports."""

CMD_READ_SUPER = 0x0002
"""Read the 532-byte super-config. **USB only** -- BLE does not answer this, which is why the BLE
path has to rebuild the record from per-slot reads and a cached header."""

CMD_SLOT = 0x000D
"""Read or write one 176-byte slot; the ``field`` selects which. The BLE read path."""

CMD_CONTROL = 0x000B
"""Session control: the handshake's opening packet, and the commit that saves."""

CMD_STATE = 0x0011
"""setConfigState. Sent with param 1 then param 0 around a write."""

CMD_REPORT_ENABLE = 0x0007
"""setReportEnable, param 0x0501 then 0x0500 after a write."""

CMD_CALIBRATION = 0x0003
"""Stick calibration, sent as part of the post-write sequence."""

CMD_FINALIZE = 0x0006
"""**Commits the record.** The last packet of every save the vendor app makes.

Named "finalize" when it was thought to be session bookkeeping, and left unsent by the USB path
for that reason. It is not bookkeeping: without it the controller accepts all 532 bytes, reports
them back correctly for as long as it stays powered, and has forgotten them by the next plug-in.
Sent with :data:`FINALIZE_FIELD`."""

FINALIZE_FIELD = 0x005B

REPORT_ENABLE_ON = 0x0501
"""``field`` of the setReportEnable that opens the save block."""

CALIBRATION_DATA = bytes(4) + b"\x7f\x7f\x7f\x7f" + bytes(4)
"""The twelve bytes of the calibration packet inside a save, replayed as captured. Centre values
for four axes; this module changes nothing about calibration and sends it only because the save
sequence contains it."""

#: The ``0x000B`` control packet, and where its fields actually are.
#:
#: The whole packet is ``04 0b 00 | 0000 | 04000000 | 04000000 | 34340000 | aa000000``, and the
#: only hard part is counting: the request header is seventeen bytes, so ``34 34 00 00`` lands in
#: the **offset** field -- a constant this command always carries -- and the payload is the four
#: bytes after it. ``00`` opens a read session, ``aa`` opens a save.
#:
#: Split it the other way, as offset ``0`` with ``34 34 00 00`` as the payload, and the controller
#: takes the record, reports it back for as long as it stays powered, and loses it at the next
#: plug-in. That mis-split is what the USB path in ``8bitdo-cfg`` has, which is why its own header
#: says the USB backend was never validated on hardware; the captured Bluetooth session is the one
#: that saves, and this is its reading.
CONTROL_OFFSET = 0x00003434
CONTROL_READ = bytes(4)
CONTROL_SAVE = b"\xaa\x00\x00\x00"


def request(command: int, *, field: int = 0, size: int = 0, total: int = 0,
            offset: int = 0, data: bytes = b"") -> bytes:
    """Build a request packet."""
    return (struct.pack("<BHHIII", REPORT_ID, command, field, size, total, offset) + data)


def parse_response(packet: bytes) -> tuple[int, int, int, int, int, bytes] | None:
    """``(status, command, size, total, offset, data)``, or None if this is not one of ours.

    Returns None rather than raising: a notification stream carries other traffic, and a caller
    that has to distinguish "not for me" from "malformed" by catching exceptions ends up swallowing
    real errors too.
    """
    if len(packet) < RESPONSE_HEADER_LEN or packet[0] != REPORT_ID:
        return None
    status = packet[1]
    command = struct.unpack_from("<H", packet, 2)[0]
    size, total, offset = struct.unpack_from("<III", packet, 4)
    return (status, command, size, total, offset,
            packet[RESPONSE_HEADER_LEN:RESPONSE_HEADER_LEN + size])


def save_prologue() -> tuple[bytes, ...]:
    """What the vendor app sends before the record, in its order.

    Replayed rather than reasoned about. Only the first packet has an understood job -- it opens a
    *save* session rather than a read -- and the rest are carried because a sequence that survives a
    power cycle is worth more than a tidy one. Shared by both transports because the capture this
    comes from is a Bluetooth session and the bytes inside the frames are identical either way.
    """
    return (
        request(CMD_CONTROL, size=4, total=4, offset=CONTROL_OFFSET, data=CONTROL_SAVE),
        request(CMD_REPORT_ENABLE, field=REPORT_ENABLE_ON),
        request(CMD_CALIBRATION, size=len(CALIBRATION_DATA), total=len(CALIBRATION_DATA),
                data=CALIBRATION_DATA),
        request(CMD_READ_SUPER, size=2, total=2, offset=2, data=b"\xff\xff"),
    )


def finalize() -> bytes:
    """The packet that commits. See :data:`CMD_FINALIZE`."""
    return request(CMD_FINALIZE, field=FINALIZE_FIELD)


def chunks(total: int, size: int) -> list[tuple[int, int]]:
    """``[(offset, length), ...]`` covering *total* bytes in pieces of at most *size*.

    The last piece is short rather than padded; the device takes the length from the header.
    """
    out = []
    offset = 0
    while offset < total:
        out.append((offset, min(size, total - offset)))
        offset += size
    return out


__all__ = [
    "CMD_CALIBRATION", "CMD_CONTROL", "CMD_FINALIZE", "CMD_READ_SUPER", "CMD_REPORT_ENABLE",
    "CMD_SLOT", "CMD_STATE", "CMD_WRITE_SUPER", "CONTROL_OFFSET", "CONTROL_READ", "CONTROL_SAVE",
    "CONTROL_TAIL",
    "REPORT_ID", "REQUEST_HEADER_LEN", "RESPONSE_HEADER_LEN", "chunks", "parse_response",
    "request",
]
