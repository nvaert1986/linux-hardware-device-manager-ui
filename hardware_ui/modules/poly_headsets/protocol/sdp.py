"""Minimal SDP client over L2CAP.

BlueZ 5.x ships no `sdptool`, and BlueZ's D-Bus API does not expose RFCOMM channel numbers — only
service UUIDs. So to find which channel `PltHeadsetDataService` is on we speak SDP ourselves.

This matters because Poly allocates the channel dynamically: it was 14 in one session and 15 in
the next. Hardcoding it, or sweeping channels to find it, are both wrong — sweeping in particular
can exhaust a headset's RFCOMM slots.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

SDP_PSM = 0x0001

SDP_ERROR_RSP = 0x01
SDP_SERVICE_SEARCH_ATTR_REQ = 0x06
SDP_SERVICE_SEARCH_ATTR_RSP = 0x07

ATTR_SERVICE_NAME = 0x0100
ATTR_PROTOCOL_DESCRIPTOR_LIST = 0x0004

UUID_L2CAP = 0x0100
UUID_RFCOMM = 0x0003

BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"


class SdpError(RuntimeError):
    pass


# --- data element codec --------------------------------------------------------------------

def _de_uuid(uuid: str | int) -> bytes:
    """Encode a UUID as a data element (16-bit shorthand or full 128-bit)."""
    if isinstance(uuid, int):
        return bytes([0x19]) + uuid.to_bytes(2, "big")          # type 3 (UUID), size 2 bytes
    raw = bytes.fromhex(uuid.replace("-", ""))
    if len(raw) != 16:
        raise ValueError(f"not a 128-bit UUID: {uuid}")
    return bytes([0x1C]) + raw                                   # type 3, size 16 bytes


def _de_seq(payload: bytes) -> bytes:
    return bytes([0x35, len(payload)]) + payload                 # type 6 (DES), u8 length


def _de_uint16(value: int) -> bytes:
    return bytes([0x09]) + value.to_bytes(2, "big")              # type 1 (uint), size 2


def _parse_de(data: bytes, off: int = 0):
    """Parse one data element; return (value, next_offset)."""
    if off >= len(data):
        raise SdpError("truncated data element")
    header = data[off]
    de_type, size_idx = header >> 3, header & 0x07
    off += 1
    if size_idx < 5:
        size = (1, 2, 4, 8, 16)[size_idx]
    else:
        nbytes = {5: 1, 6: 2, 7: 4}[size_idx]
        size = int.from_bytes(data[off:off + nbytes], "big")
        off += nbytes
    body = data[off:off + size]
    end = off + size

    if de_type == 0:                                   # nil
        return None, end
    if de_type in (1, 2):                              # uint / int
        return int.from_bytes(body, "big", signed=de_type == 2), end
    if de_type == 3:                                   # UUID
        if size == 2:
            return int.from_bytes(body, "big"), end
        if size == 4:
            return int.from_bytes(body, "big"), end
        return str(_fmt_uuid128(body)), end
    if de_type == 4 or de_type == 8:                   # text / url
        return body, end
    if de_type == 5:                                   # bool
        return bool(body[0]), end
    if de_type in (6, 7):                              # sequence / alternative
        items, inner = [], off
        while inner < end:
            item, inner = _parse_de(data, inner)
            items.append(item)
        return items, end
    raise SdpError(f"unknown data element type {de_type}")


def _fmt_uuid128(raw: bytes) -> str:
    h = raw.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# --- client --------------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceRecord:
    name: str | None
    rfcomm_channel: int | None


def _iter_records(attr_lists) -> list[list]:
    """SDP returns a sequence of per-record attribute sequences; normalise to a list of them."""
    if not attr_lists:
        return []
    if attr_lists and isinstance(attr_lists[0], list):
        return attr_lists
    return [attr_lists]


def _record_from_attrs(flat: list) -> ServiceRecord:
    """`flat` is [id, value, id, value, ...] for one service record."""
    attrs = dict(zip(flat[::2], flat[1::2]))
    name = attrs.get(ATTR_SERVICE_NAME)
    if isinstance(name, (bytes, bytearray)):
        name = name.split(b"\x00")[0].decode("utf-8", "replace")

    channel = None
    for proto in attrs.get(ATTR_PROTOCOL_DESCRIPTOR_LIST) or []:
        # each entry is [uuid, param...] — RFCOMM's param is the channel
        if isinstance(proto, list) and len(proto) >= 2 and proto[0] == UUID_RFCOMM:
            if isinstance(proto[1], int):
                channel = proto[1]
    return ServiceRecord(name=name, rfcomm_channel=channel)


def search(address: str, uuid: str | int, timeout: float = 10.0) -> list[ServiceRecord]:
    """Query `address` for services matching `uuid`; return their name and RFCOMM channel."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    sock.settimeout(timeout)
    try:
        sock.connect((address, SDP_PSM))
        records: list[ServiceRecord] = []
        cont = b"\x00"
        transaction = 1
        collected = b""
        while True:
            params = (
                _de_seq(_de_uuid(uuid))
                + struct.pack(">H", 0xFFFF)                      # max attribute bytes
                + _de_seq(_de_uint16(ATTR_SERVICE_NAME) + _de_uint16(ATTR_PROTOCOL_DESCRIPTOR_LIST))
                + cont
            )
            sock.send(
                bytes([SDP_SERVICE_SEARCH_ATTR_REQ])
                + struct.pack(">HH", transaction, len(params))
                + params
            )
            resp = sock.recv(65535)
            if len(resp) < 5:
                raise SdpError("short SDP response")
            pdu, _rx_tid, plen = struct.unpack(">BHH", resp[:5])
            body = resp[5:5 + plen]
            if pdu == SDP_ERROR_RSP:
                raise SdpError(f"SDP error {int.from_bytes(body[:2], 'big'):#06x}")
            if pdu != SDP_SERVICE_SEARCH_ATTR_RSP:
                raise SdpError(f"unexpected SDP PDU {pdu:#04x}")

            nbytes = int.from_bytes(body[:2], "big")
            collected += body[2:2 + nbytes]
            cont_field = body[2 + nbytes:]
            transaction += 1
            if not cont_field or cont_field[0] == 0:
                break
            cont = cont_field[: 1 + cont_field[0]]

        if collected:
            parsed, _ = _parse_de(collected)
            for record in _iter_records(parsed):
                if isinstance(record, list) and record:
                    records.append(_record_from_attrs(record))
        return records
    finally:
        sock.close()


def find_channel(address: str, uuid: str | int, timeout: float = 10.0) -> int | None:
    """RFCOMM channel for the first matching service that advertises one."""
    for record in search(address, uuid, timeout):
        if record.rfcomm_channel is not None:
            return record.rfcomm_channel
    return None
