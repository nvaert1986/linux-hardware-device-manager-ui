"""GNP over USB HID via hidraw.

Jabra tunnels GNP through a vendor-defined HID report: usage page 0xFF00, usage 0x01. The
report id and size are read from the device's own report descriptor rather than assumed —
they differ per product (Link 390: report 0x05 / 63 bytes; Evolve2 85 deskstand: report 0x05 /
32 bytes).

Writes use a plain write() on hidraw, i.e. the interrupt OUT endpoint. This matches libjabra,
whose own log strings name WriteFile ("GN_HID_USB_Driver.cpp:WriteReport"). That differs from
the Poly project, which needed the control-transfer SET_REPORT (HIDIOCSOUTPUT); if a device
ignores interrupt writes, set `use_set_report=True` to fall back.

Jabra serialises all GNP access behind a global mutex in its own stack, so this class holds one
session per device and never interleaves writes.
"""
from __future__ import annotations

import array
import errno
import fcntl
import glob
import logging
import os
import re
import select
import tempfile
import time
from collections import deque
from dataclasses import dataclass

from .framing import HEADER_SIZE, Packet
from .ids import VENDOR_ID
from .report_descriptor import find_report

#: The GNP tunnel, from GnProtocolOverUsbHid: SetHidValueArrayOutput(0xFF00, 1, bytes).
GNP_USAGE_PAGE = 0xFF00
GNP_USAGE = 0x01

_HID_ID_RE = re.compile(
    r"HID_ID=[0-9A-Fa-f]+:0*([0-9A-Fa-f]{1,8}):0*([0-9A-Fa-f]{1,8})"
)
_HID_NAME_RE = re.compile(r"HID_NAME=(.*)")

_IOC_WRITE, _IOC_READ = 1, 2

log = logging.getLogger(__name__)

#: Errno values that mean "this file descriptor is dead", not "the request failed". Jabra
#: dongles re-enumerate on their own — a firmware state change bumps the USB device number while
#: the /dev/hidrawN name often stays the same — so an open fd can go stale mid-session.
_STALE_ERRNOS = frozenset({errno.EPIPE, errno.ENODEV, errno.ESHUTDOWN, errno.EIO})

#: How many times a send retries across a re-enumeration. A freshly reopened node can still be
#: settling — the first write after reconnecting has been observed failing with EPIPE too — so
#: one retry is not enough.
_SEND_ATTEMPTS = 3

#: Let the device finish coming up before writing to a newly opened node.
_SETTLE_SECONDS = 0.25

#: Non-GNP input reports kept for callers to inspect (button presses).
_MAX_FOREIGN_REPORTS = 64


def _hidiocsoutput(size: int) -> int:
    return ((_IOC_WRITE | _IOC_READ) << 30) | (size << 16) | (ord("H") << 8) | 0x0B


class HidTransportError(RuntimeError):
    pass


class DeviceBusyError(HidTransportError):
    """Another process already holds this device's GNP channel."""


def _lock_path(product_id: int) -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, f"jabra-gnp-{product_id:04x}.lock")


class _DeviceLock:
    """Cross-process guard: one GNP conversation per device, app-wide.

    GNP has no way to tell whose reply is whose beyond the sequence number, and two processes
    counting sequences independently will hand each other the wrong answers — the symptom is
    garbage decodes and apparent hangs. Jabra's own stack takes a *named* (cross-process) mutex
    for this reason; `flock` on a file under XDG_RUNTIME_DIR is the same idea.

    Non-blocking on purpose: a second instance should say so immediately rather than appear to
    freeze. The lock is released when the fd closes, including on crash.
    """

    def __init__(self, product_id: int):
        self.path = _lock_path(product_id)
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise DeviceBusyError(
                "another process is already using this Jabra device "
                f"(lock {self.path}). Close the other instance — GNP allows only one "
                "conversation per device."
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


class _Reassembler:
    """Rejoin a GNP packet that spans several HID reports.

    Mirrors GnProtocolOverUsbHid.OnInputReceived, which keeps `responsePacket`,
    `dataBytesReceived` and `waitingForMorePacketData`: a report is parsed as a fresh header
    unless the previous one declared more bytes than it carried, in which case the whole of
    this report is payload continuation.

    Whether this ever triggers depends on the product. The declared total length is at most 63
    (6 bits), so the Link 390's 63-byte report can always hold a whole packet — but the Evolve2
    85 deskstand declares only 32 bytes, and a longer packet must fragment there.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._total = 0

    def reset(self) -> None:
        self._buf.clear()
        self._total = 0

    @property
    def waiting(self) -> bool:
        return bool(self._buf)

    def feed(self, payload: bytes) -> Packet | None:
        """Add one report's payload (report id already stripped). Returns a whole packet."""
        if not self._buf:
            if len(payload) < HEADER_SIZE:
                return None
            total = payload[3] & 0x3F
            if total < HEADER_SIZE:
                return None
            if total <= len(payload):
                # Fits in this report; the rest of the report is zero padding.
                return self._decode(payload[:total])
            # Fragmented: every byte of this report is real.
            self._buf = bytearray(payload)
            self._total = total
            return None

        self._buf.extend(payload)
        if len(self._buf) < self._total:
            return None
        packet = self._decode(bytes(self._buf[: self._total]))
        self.reset()
        return packet

    def _decode(self, raw: bytes) -> Packet | None:
        try:
            return Packet.decode(raw)
        except Exception:
            self.reset()
            return None


@dataclass(frozen=True)
class JabraHidDevice:
    path: str                 # /dev/hidrawN
    product_id: int
    name: str
    report_id: int
    out_bytes: int            # payload bytes after the report id, output direction
    in_bytes: int             # payload bytes after the report id, input direction

    @property
    def is_accessory(self) -> bool:
        """A charging stand or cradle — it speaks GNP but is not the headset's link."""
        lowered = self.name.lower()
        return any(hint in lowered for hint in _ACCESSORY_HINTS)

    @property
    def description(self) -> str:
        kind = " [accessory]" if self.is_accessory else ""
        return f"{self.name} (0b0e:{self.product_id:04x}, {self.path}){kind}"


def _read_descriptor(sysfs_device: str) -> bytes | None:
    try:
        with open(os.path.join(sysfs_device, "report_descriptor"), "rb") as fh:
            return fh.read()
    except OSError:
        return None


#: Name fragments marking a node as an accessory rather than the device we want to talk to.
_ACCESSORY_HINTS = ("deskstand", "cradle", "charging", "stand", "dock")


def _selection_key(device: JabraHidDevice) -> tuple:
    """Order candidates best-first.

    Never rely on /dev/hidrawN order: the numbers are assigned as devices appear and **do swap**
    — after a re-enumeration this rig went from (15=Link 390, 16=deskstand) to
    (15=deskstand, 16=Link 390), which silently pointed tools at the charging stand.

    Prefer, in order: a bigger GNP report (the dongle declares 63 bytes, the deskstand 32), then
    a non-accessory name, then the path for determinism.
    """
    return (-device.out_bytes, device.is_accessory, device.path)


def find_devices() -> list[JabraHidDevice]:
    """Jabra hidraw nodes exposing the GNP tunnel, best candidate first."""
    found: list[JabraHidDevice] = []
    for sysfs in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        device = os.path.join(sysfs, "device")
        try:
            with open(os.path.join(device, "uevent")) as fh:
                uevent = fh.read()
        except OSError:
            continue
        match = _HID_ID_RE.search(uevent)
        if not match or int(match.group(1), 16) != VENDOR_ID:
            continue
        desc = _read_descriptor(device)
        if desc is None:
            continue
        out = find_report(desc, GNP_USAGE_PAGE, GNP_USAGE, "output")
        inp = find_report(desc, GNP_USAGE_PAGE, GNP_USAGE, "input")
        if out is None or inp is None:
            continue                      # a Jabra HID node without the GNP tunnel
        name = _HID_NAME_RE.search(uevent)
        found.append(
            JabraHidDevice(
                path=f"/dev/{os.path.basename(sysfs)}",
                product_id=int(match.group(2), 16),
                name=(name.group(1).strip() if name else os.path.basename(sysfs)),
                report_id=out.report_id,
                out_bytes=out.size_bytes,
                in_bytes=inp.size_bytes,
            )
        )
    return sorted(found, key=_selection_key)


class HidTransport:
    """One GNP session over hidraw. connect/close/send/receive."""

    def __init__(self, device: JabraHidDevice | None = None, *,
                 use_set_report: bool = False):
        self.device = device
        self.use_set_report = use_set_report
        self._fd: int | None = None
        self._reassembler = _Reassembler()
        self._device_lock: _DeviceLock | None = None
        #: Input reports that are not GNP, newest last. One HID interface carries the GNP
        #: tunnel *and* the standard Telephony/Consumer pages, so button presses arrive here.
        #: They must be kept rather than dropped: a read for GNP traffic would otherwise
        #: consume and discard a mute keypress that a caller wanted to see.
        self.foreign_reports: deque[bytes] = deque(maxlen=_MAX_FOREIGN_REPORTS)

    @property
    def description(self) -> str:
        return self.device.description if self.device else "Jabra USB HID"

    # -- lifecycle -------------------------------------------------------------------------

    def connect(self) -> str:
        if self.device is None:
            devices = find_devices()
            if not devices:
                raise HidTransportError(
                    "no Jabra device with a GNP interface found — is the dongle plugged in?"
                )
            self.device = devices[0]
        # One conversation per device, across processes — see _DeviceLock.
        self._device_lock = _DeviceLock(self.device.product_id)
        self._device_lock.acquire()
        try:
            self._fd = os.open(self.device.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            self._device_lock.release()
            raise HidTransportError(
                f"{self.device.path} is not accessible — install "
                "packaging/70-jabra-headset.rules, or run as root"
            ) from exc
        except OSError as exc:
            self._device_lock.release()
            raise HidTransportError(f"cannot open {self.device.path}: {exc}") from exc
        return self.description

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass                    # already gone; nothing useful to do
            finally:
                self._fd = None
        if self._device_lock is not None:
            self._device_lock.release()
            self._device_lock = None

    def reconnect(self, *, attempts: int = 5, delay: float = 0.4) -> bool:
        """Reopen after the device re-enumerated. True if the link is usable again.

        The hidraw node is re-resolved by product id rather than reused: on re-enumeration the
        old path may point at nothing, or at a different device. The GNP session above is
        unaffected — addresses and the sequence counter stay valid, because they belong to the
        protocol rather than the file descriptor.
        """
        if self.device is None:
            return False
        product_id = self.device.product_id
        self.close()
        self._reassembler.reset()
        for attempt in range(attempts):
            for candidate in find_devices():
                if candidate.product_id != product_id:
                    continue
                self.device = candidate
                try:
                    self._fd = os.open(candidate.path, os.O_RDWR | os.O_NONBLOCK)
                except OSError as exc:
                    log.debug("reconnect: %s not openable yet (%s)", candidate.path, exc)
                    continue
                # Opening can succeed while the device is still settling, in which case the
                # next write fails with EPIPE as well. Give it a moment.
                time.sleep(_SETTLE_SECONDS)
                log.info("reconnected to %s", candidate.description)
                return True
            if attempt < attempts - 1:
                time.sleep(delay)       # udev may not have re-applied the ACL yet
        log.warning("could not reconnect to 0b0e:%04x", product_id)
        return False

    def __enter__(self) -> HidTransport:
        self.connect()
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # -- io --------------------------------------------------------------------------------

    def send(self, packet: Packet) -> None:
        if self.device is None:
            raise HidTransportError("not connected")
        body = packet.encode()
        if len(body) > self.device.out_bytes:
            raise HidTransportError(
                f"packet is {len(body)} bytes but the report holds "
                f"{self.device.out_bytes}"
            )
        last: OSError | None = None
        for attempt in range(_SEND_ATTEMPTS):
            if self._fd is None and not self.reconnect():
                break
            # A new request invalidates any half-collected reply.
            self._reassembler.reset()
            report = (bytes([self.device.report_id])
                      + body.ljust(self.device.out_bytes, b"\x00"))
            try:
                self._emit(report)
                return
            except OSError as exc:
                last = exc
                if exc.errno not in _STALE_ERRNOS:
                    break
                log.info("write failed (%s), attempt %d/%d — reconnecting",
                         exc, attempt + 1, _SEND_ATTEMPTS)
                if not self.reconnect():
                    break
        raise HidTransportError(f"write failed: {last}") from last

    def _emit(self, report: bytes) -> None:
        if self.use_set_report:
            buf = array.array("B", report)
            fcntl.ioctl(self._fd, _hidiocsoutput(len(buf)), buf, True)
            return
        os.write(self._fd, report)

    def receive(self, timeout: float = 5.0) -> list[Packet]:
        """Packets available within `timeout`. Malformed reports are skipped, not raised.

        The 5 s default mirrors GnProtocolOverUsbHid's ResponseTimeout.
        """
        device = self.device
        if device is None:
            raise HidTransportError("not connected")
        # Snapshot the descriptor: close() can run on another thread (the GUI closing the window
        # while this worker is mid-read), and os.read(None, n) raises an opaque TypeError.
        fd = self._fd
        if fd is None:
            # Lost mid-conversation (see the stale-errno path below). Report "nothing arrived"
            # so the caller times out cleanly; the next send() reconnects and retries.
            return []
        packets: list[Packet] = []
        try:
            readable, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return []                       # closed underneath us
        while readable:
            if self._fd is None:
                break                       # closed while we were draining
            try:
                report = os.read(fd, device.in_bytes + 1)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in _STALE_ERRNOS:
                    # The device vanished mid-read. Surface it as an empty result and let the
                    # session time out; the next send() reconnects.
                    log.info("read failed (%s) — device re-enumerating", exc)
                    self.close()
                    return packets
                raise HidTransportError(f"read failed: {exc}") from exc
            if not report:
                break
            packet = self.decode_report(report)
            if packet is not None:
                packets.append(packet)
            try:
                readable, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                break
        return packets

    def decode_report(self, report: bytes) -> Packet | None:
        """Strip the report id and decode, reassembling across reports where needed.

        None means "nothing complete yet" — either not a GNP report, or a fragment. A non-GNP
        report is kept in `foreign_reports` rather than dropped, because this interface also
        carries the Telephony and Consumer pages: discarding them would swallow button presses
        that arrive while we happen to be waiting for a GNP reply.
        """
        if self.device is None or len(report) < 2:
            return None
        if report[0] != self.device.report_id:
            self.foreign_reports.append(bytes(report))
            return None
        return self._reassembler.feed(report[1:])

    def take_foreign_reports(self) -> list[bytes]:
        """Non-GNP input reports collected since the last call."""
        reports = list(self.foreign_reports)
        self.foreign_reports.clear()
        return reports
