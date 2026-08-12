"""Deckard over USB HID — the Poly BT700 dongle and USB-cabled headsets.

Wire format, established from a USBPcap capture of Poly Lens Desktop
(`captures/poly-voyager-4310.pcapng`, decoded by `tools/decode_usb.py`):

    open : SET_REPORT(Output, report 0x13) = 01     -- once, enables the channel
    TX   : SET_REPORT(Output, report 0x07)          -- 504 bytes total
    RX   : interrupt IN                             -- 63 bytes total

    both : [0x07][chunk index, 1-based][chunk count][deckard frame bytes][zero padding]

The Deckard frame inside is byte-identical to the Bluetooth transport — Poly's own DLLs share
BREncoder.cpp between the two, and only the wrapper differs. That is why this module can sit
behind the same session layer with no changes above it.

Transmit never actually needs to chunk (503 bytes covers any frame we send), but receive does:
the input report is only 62 bytes, so the metadata blob and UTF-16 strings arrive split.

Note: writes go through HIDIOCSOUTPUT — a control-transfer SET_REPORT, matching Windows'
HidD_SetOutputReport. A plain write() to hidraw uses the interrupt OUT endpoint instead and the
device ignores it.
"""
from __future__ import annotations

import array
import fcntl
import glob
import os
import re
import select
from dataclasses import dataclass

from .framing import Frame, FramingError

VENDOR_ID = 0x047F  # Plantronics / Poly

REPORT_DECKARD = 0x07
REPORT_ENABLE = 0x13

#: Output report 0x07 on a **BT700 dongle** carries 503 bytes after the report id; 2 of those go
#: to the chunk header. It is not a constant of the protocol -- a Voyager 4310 on its own USB
#: connection declares 62 -- so this is only the fallback for a descriptor that cannot be read.
#: See :func:`output_report_size`.
DEFAULT_TX_PAYLOAD = 503
CHUNK_HEADER = 2
#: Input report 0x07 is 62 bytes after the report id.
RX_REPORT_LEN = 1 + 62

_HID_ID_RE = re.compile(r"HID_ID=[0-9A-Fa-f]+:0*([0-9A-Fa-f]{1,8}):0*([0-9A-Fa-f]{1,8})")
_HID_NAME_RE = re.compile(r"HID_NAME=(.*)")

# ioctl encodings from linux/hidraw.h
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def _hidiocsoutput(size: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, "H", 0x0B, size)


class HidTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class HidDevice:
    path: str          # /dev/hidrawN
    vendor: int
    product: int
    name: str

    @property
    def description(self) -> str:
        return f"{self.name} ({self.path})"


def output_report_size(sysfs_device: str, report_id: int = REPORT_DECKARD) -> int:
    """Bytes the device says its output report holds, after the report id.

    **Read, never assumed.** A BT700 dongle declares 503 and a Voyager 4310 on its own USB
    connection declares 62. Sending 504 bytes to the headset made it stall the control transfer,
    which surfaced as ``SET_REPORT failed: [Errno 32] Broken pipe`` and looked for all the world
    like the headset being asleep -- so the module could talk to a headset through a dongle and
    never to the same headset on its own cable.

    A minimal HID item walk: globals carry report size, count and id; a Main *Output* item commits
    whatever is current.
    """
    try:
        with open(os.path.join(sysfs_device, "report_descriptor"), "rb") as fh:
            desc = fh.read()
    except OSError:
        return DEFAULT_TX_PAYLOAD

    bits = {}
    index = current_id = size = count = 0
    while index < len(desc):
        prefix = desc[index]
        length = prefix & 0x03
        length = 4 if length == 3 else length
        value = int.from_bytes(desc[index + 1:index + 1 + length], "little") if length else 0
        kind, tag = (prefix >> 2) & 0x03, (prefix >> 4) & 0x0F
        if kind == 1:  # Global
            if tag == 0x7:
                size = value
            elif tag == 0x9:
                count = value
            elif tag == 0x8:
                current_id = value
        elif kind == 0 and tag == 0x9:  # Main / Output
            bits[current_id] = bits.get(current_id, 0) + size * count
        index += 1 + length

    found = bits.get(report_id, 0) // 8
    return found or DEFAULT_TX_PAYLOAD


def _descriptor_supports_deckard(sysfs_device: str) -> bool:
    """True if the descriptor declares report 0x07, i.e. the Deckard tunnel."""
    try:
        with open(os.path.join(sysfs_device, "report_descriptor"), "rb") as fh:
            desc = fh.read()
    except OSError:
        return False
    # Report ID items are 0x85 <id>; we only need to know 0x07 is declared.
    return b"\x85\x07" in desc


def present_product_ids() -> set[int]:
    """USB product ids of every Poly device currently plugged in.

    Used to tell "behind this one" from "beside this one" -- see
    :meth:`PolyHeadset._attach_downstream`.
    """
    found: set[int] = set()
    for sysfs in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(os.path.join(sysfs, "device", "uevent")) as fh:
                match = _HID_ID_RE.search(fh.read())
        except OSError:
            continue
        if match and int(match.group(1), 16) == VENDOR_ID:
            found.add(int(match.group(2), 16))
    return found


def _sysfs_device_for(node: str) -> str:
    """``/dev/hidrawN`` -> the sysfs directory holding its ``report_descriptor``."""
    return f"/sys/class/hidraw/{os.path.basename(node)}/device"


def find_devices() -> list[HidDevice]:
    """Poly HID nodes that expose the Deckard report."""
    found: list[HidDevice] = []
    for sysfs in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        node = f"/dev/{os.path.basename(sysfs)}"
        try:
            with open(os.path.join(sysfs, "device", "uevent")) as fh:
                uevent = fh.read()
        except OSError:
            continue
        m = _HID_ID_RE.search(uevent)
        if not m or int(m.group(1), 16) != VENDOR_ID:
            continue
        if not _descriptor_supports_deckard(os.path.join(sysfs, "device")):
            continue
        name = _HID_NAME_RE.search(uevent)
        found.append(
            HidDevice(
                path=node,
                vendor=VENDOR_ID,
                product=int(m.group(2), 16),
                name=name.group(1).strip() if name else os.path.basename(sysfs),
            )
        )
    return found


class _Reassembler:
    """Rejoin chunked reports into whole Deckard frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, report: bytes) -> Frame | None:
        if len(report) < 4 or report[0] != REPORT_DECKARD:
            return None
        index, count, data = report[1], report[2], report[3:]
        if index == 1:
            self._buf = bytearray()
        self._buf += data
        if index != count:
            return None
        body = bytes(self._buf)
        self._buf = bytearray()
        if len(body) < 2 or (body[0] >> 4) != 1:
            return None
        length = ((body[0] & 0x0F) << 8) | body[1]
        if length + 2 > len(body):
            return None
        try:
            return Frame.decode(body[: length + 2])
        except FramingError:
            return None


class HidTransport:
    """Interchangeable with RfcommTransport: connect/close/send/receive."""

    def __init__(self, path: str | None = None):
        self.path = path
        self._fd: int | None = None
        self._reasm = _Reassembler()

    #: A dongle can have devices behind it; a direct Bluetooth link cannot.
    has_downstream_ports = True

    def peer_product_ids(self) -> set[int]:
        """Other Poly USB devices plugged in right now, by product id -- excluding this one.

        A device in this set has its own row in the application, so walking downstream onto it
        means the walk went sideways rather than deeper.
        """
        mine = self._own_product_id()
        return {pid for pid in present_product_ids() if pid != mine}

    def _own_product_id(self) -> int | None:
        if not self.path:
            return None
        try:
            with open(os.path.join(_sysfs_device_for(self.path), "uevent")) as fh:
                match = _HID_ID_RE.search(fh.read())
        except OSError:
            return None
        return int(match.group(2), 16) if match else None

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def description(self) -> str:
        return f"USB HID {self.path}" if self.path else "USB HID"

    def connect(self) -> str:
        if self.path is None:
            devices = find_devices()
            if not devices:
                raise HidTransportError(
                    "no Poly HID device found — is the dongle plugged in, and does "
                    "/dev/hidraw* grant access? (see 70-poly-headset.rules)"
                )
            self.path = devices[0].path
        # Sized from this device's own descriptor before a single frame is sent.
        self._tx_payload = output_report_size(_sysfs_device_for(self.path))
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise HidTransportError(
                f"{self.path} is not accessible — install 70-poly-headset.rules"
            ) from exc
        except OSError as exc:
            raise HidTransportError(f"cannot open {self.path}: {exc}") from exc

        self._set_report(bytes([REPORT_ENABLE, 0x01]))
        return self.description

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- io ----------------------------------------------------------------------------------

    def _set_report(self, payload: bytes) -> None:
        """Control-transfer SET_REPORT(Output), as HidD_SetOutputReport does on Windows."""
        if self._fd is None:
            raise HidTransportError("not connected")
        buf = array.array("B", payload)
        try:
            fcntl.ioctl(self._fd, _hidiocsoutput(len(buf)), buf, True)
        except OSError as exc:
            raise HidTransportError(f"SET_REPORT failed: {exc}") from exc

    def send(self, frame: Frame) -> None:
        payload = getattr(self, "_tx_payload", DEFAULT_TX_PAYLOAD)
        room = payload - CHUNK_HEADER
        data = frame.encode()
        chunks = [data[i:i + room] for i in range(0, len(data), room)] or [b""]
        for index, chunk in enumerate(chunks, start=1):
            report = bytes([REPORT_DECKARD, index, len(chunks)]) + chunk
            self._set_report(report.ljust(1 + payload, b"\x00"))

    def receive(self, timeout: float = 3.0) -> list[Frame]:
        if self._fd is None:
            raise HidTransportError("not connected")
        frames: list[Frame] = []
        readable, _, _ = select.select([self._fd], [], [], timeout)
        while readable:
            try:
                report = os.read(self._fd, RX_REPORT_LEN)
            except BlockingIOError:
                break
            if not report:
                break
            frame = self._reasm.feed(report)
            if frame is not None:
                frames.append(frame)
            readable, _, _ = select.select([self._fd], [], [], 0.05)
        return frames
