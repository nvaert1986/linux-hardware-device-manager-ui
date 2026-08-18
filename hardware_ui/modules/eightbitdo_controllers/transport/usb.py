"""Configuration over USB, wrapped in GIP frames.

Ported from the source project's ``gui/bit8/usb.py``, which was built from a Windows USB capture
of the vendor's own tool and then validated against the controller.

**This is the preferred transport.** It needs no phone, no config-mode button ritual and no BLE
scan, and -- the part that matters most -- the 532-byte super-config is *readable* over it, which
BLE cannot do. That readable header is what supplies the rolling checksum's previous value, so a
controller seen once over USB can then be configured over BLE indefinitely. See :mod:`.ble`.

Two changes from the source, both because this serves a family rather than one unit:

* The GIP interface and its bulk endpoints are read from the descriptors instead of being fixed at
  interface 0 and endpoints ``0x01``/``0x81``.
* The device is located by serial when there is one, so two identical controllers do not get
  confused for each other.

**Claiming the interface pauses gamepad input** for roughly a second, because the config channel
and the input channel are the same interface. Unavoidable, and the reason the module says so.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from ..protocol import gip, message
from ..protocol import fieldmap as fm

log = logging.getLogger(__name__)

#: 8BitDo. Product ids are not listed: the manifest decides which controllers this module claims,
#: and the transport opens whatever it is handed.
VENDOR_ID = 0x2DC8

#: The Xbox Game Input Protocol interface, as a (class, subclass, protocol) triple. Matched in
#: full because class 0xFF alone is what every dongle falls back to.
GIP_INTERFACE = (0xFF, 0x47, 0xD0)

#: Payload size of a config frame, and the inner data bytes each one carries. Both from the
#: capture; the difference is the 16-byte inner header plus GIP's own.
FRAME = 60
CHUNK = 41

#: How long to wait for a read response before giving up on that chunk.
READ_TIMEOUT_MS = 400

#: Re-enumeration after detaching the kernel driver is normal, so a fresh handle is taken and the
#: operation retried rather than reported as a failure.
RETRIES = 3


class TransportError(Exception):
    pass




def _all_devices(product_id: int | None) -> list[Any]:
    import usb.core

    query: dict[str, int] = {"idVendor": VENDOR_ID}
    if product_id:
        query["idProduct"] = product_id
    return sorted(usb.core.find(find_all=True, **query),
                  key=lambda d: (d.bus, d.port_number or 0))


def _find_gip(device: Any) -> Any | None:
    """The GIP interface, from the descriptors."""
    for configuration in device:
        for interface in configuration:
            triple = (interface.bInterfaceClass, interface.bInterfaceSubClass,
                      interface.bInterfaceProtocol)
            if triple == GIP_INTERFACE:
                return interface
    return None


def _endpoints(interface: Any) -> tuple[int, int]:
    """``(out, in)`` interrupt endpoint addresses. GIP uses interrupt, not bulk."""
    out = in_ = 0
    for endpoint in interface:
        if endpoint.bmAttributes & 0x03 != 0x03:      # interrupt only
            continue
        if endpoint.bEndpointAddress & 0x80:
            in_ = in_ or endpoint.bEndpointAddress
        else:
            out = out or endpoint.bEndpointAddress
    if not out or not in_:
        raise TransportError(
            f"GIP interface {interface.bInterfaceNumber} has no interrupt endpoint pair")
    return out, in_


class Session:
    """One open configuration session. Detaches the kernel driver and puts it back."""

    def __init__(self, product_id: int | None = None, serial: str = "") -> None:
        self.product_id = product_id
        self.serial = serial
        self._device: Any = None
        self._interface = 0
        self._out = self._in = 0
        self._reattach = False
        self._sequence = gip.Sequence()

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> Session:
        import usb.core
        import usb.util

        device = self._locate()
        interface = _find_gip(device)
        if interface is None:
            raise TransportError(
                f"{device.idVendor:04x}:{device.idProduct:04x} has no GIP interface; "
                "this controller is not configurable over USB")
        self._interface = interface.bInterfaceNumber

        try:
            if device.is_kernel_driver_active(self._interface):
                device.detach_kernel_driver(self._interface)
                self._reattach = True
                # Detaching can make the controller re-enumerate, which invalidates the handle.
                time.sleep(0.6)
                device = self._locate()
                interface = _find_gip(device) or interface
        except (NotImplementedError, usb.core.USBError) as exc:
            log.debug("kernel driver detach: %s", exc)

        try:
            usb.util.claim_interface(device, self._interface)
        except usb.core.USBError as exc:
            raise TransportError(
                f"cannot claim the GIP interface: {exc}\n"
                "Is the udev rule installed? See docs/INSTALL.md.") from exc

        self._device = device
        self._out, self._in = _endpoints(interface)
        return self

    def __exit__(self, *_: object) -> None:
        import usb.core
        import usb.util

        if self._device is None:
            return
        with contextlib.suppress(usb.core.USBError):
            usb.util.release_interface(self._device, self._interface)
        if self._reattach:
            # Put xpad back, or the controller stops working as a gamepad until it is replugged.
            with contextlib.suppress(usb.core.USBError, NotImplementedError):
                self._device.attach_kernel_driver(self._interface)
        with contextlib.suppress(usb.core.USBError):
            usb.util.dispose_resources(self._device)
        self._device = None

    def _locate(self) -> Any:
        devices = _all_devices(self.product_id)
        if not devices:
            raise TransportError(f"no 8BitDo controller ({VENDOR_ID:04x}) found")
        if self.serial:
            for device in devices:
                with contextlib.suppress(ValueError, NotImplementedError):
                    if device.serial_number == self.serial:
                        return device
        if len(devices) > 1:
            log.warning("%d controllers match and none by serial %r; using the first",
                        len(devices), self.serial)
        return devices[0]

    # ------------------------------------------------------------------ raw io

    def _send_config(self, inner: bytes) -> None:
        """Wrap an inner config message in a GIP frame and send it."""
        body = inner + bytes(FRAME - len(inner)) if len(inner) < FRAME else inner
        frame = gip.build(gip.CMD_CONFIG_OUT, body, sequence=self._sequence.take())
        self._device.write(self._out, frame, timeout=1000)

    def _read_frame(self, timeout_ms: int = READ_TIMEOUT_MS) -> bytes:
        import usb.core

        try:
            return bytes(self._device.read(self._in, 64, timeout=timeout_ms))
        except usb.core.USBError:
            return b""                       # a timeout is the normal idle case

    def _drain(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self._read_frame(60):
                return

    # ------------------------------------------------------------------ config

    def read_super(self) -> bytes:
        """The whole 532-byte record.

        Requested chunk by chunk rather than in one go because the frame is 60 bytes; the device
        answers each request with a ``0x12`` frame carrying its own offset, which is what gets
        trusted rather than the order they arrive in.
        """
        buffer = bytearray(fm.SUPER_LEN)
        seen: set[int] = set()
        for offset, size in message.chunks(fm.SUPER_LEN, CHUNK):
            self._send_config(message.request(
                message.CMD_READ_SUPER, size=size, total=fm.SUPER_LEN, offset=offset))
            self._collect(buffer, seen)
        missing = [o for o, _ in message.chunks(fm.SUPER_LEN, CHUNK) if o not in seen]
        if missing:
            raise TransportError(
                f"the controller answered {len(seen)} of "
                f"{len(message.chunks(fm.SUPER_LEN, CHUNK))} config chunks "
                f"(missing at {missing[:4]}{'...' if len(missing) > 4 else ''})")
        return bytes(buffer)

    def _collect(self, buffer: bytearray, seen: set[int], tries: int = 6) -> None:
        for _ in range(tries):
            frame = self._read_frame()
            if len(frame) < 4 or frame[0] != gip.CMD_CONFIG_IN:
                continue
            _, _, _, length, _, start = gip.decode_header(frame)
            parsed = message.parse_response(frame[start:start + length] if length else frame[4:])
            if parsed is None:
                continue
            _, _, size, _, offset, data = parsed
            if 0 <= offset < fm.SUPER_LEN and data:
                buffer[offset:offset + len(data)] = data
                seen.add(offset)
                return

    def write_super(self, record: bytes) -> None:
        """Write the record as a **save session**, which is what makes it survive a power cycle.

        The chunks alone are not a save. The controller accepts all 532 bytes, reads them back
        correctly for as long as it stays powered, and comes back with the old configuration at the
        next plug-in -- the failure that looks most like success, and the one that cost the most
        time here.

        The sequence is the one in the vendor app's own captured save, in its order:

        ==========================  ==========================================================
        ``0x000B`` control          opens a **save** session: offset ``0x3434``, payload ``aa``
        ``0x0007`` setReportEnable  field ``0x0501``
        ``0x0003`` calibration      twelve bytes, replayed as captured
        ``0x0002``                  a two-byte read the app makes here
        ``0x0001`` x N              the record
        ``0x0006`` finalize         field ``0x005B`` -- **this is the commit**
        ==========================  ==========================================================

        The USB backend this module was ported from sends the chunks and one malformed control
        packet, and nothing else; its own header notes that the USB path was never run against a
        controller. Over Bluetooth the same project replays the full session above, which is why
        that path worked and this one did not.
        """
        if len(record) != fm.SUPER_LEN:
            raise TransportError(f"a super-config is {fm.SUPER_LEN} bytes, got {len(record)}")

        for packet in message.save_prologue():
            self._send_config(packet)
            self._drain(0.05)
        for offset, size in message.chunks(fm.SUPER_LEN, CHUNK):
            self._send_config(message.request(
                message.CMD_WRITE_SUPER, size=size, total=fm.SUPER_LEN, offset=offset,
                data=record[offset:offset + size]))
            self._drain(0.02)
        self._send_config(message.finalize())
        # Longer than the others: this is the one the controller has to write to flash before the
        # interface is released underneath it.
        self._drain(0.5)


def _retry(operation, tries: int = RETRIES):
    """Run *operation*, taking a fresh handle if the controller re-enumerated underneath us."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            return operation()
        except OSError as exc:
            last = exc
            if getattr(exc, "errno", None) == 19:      # ENODEV
                time.sleep(0.8)
                continue
            raise
    raise last  # type: ignore[misc]


def read_super(product_id: int | None = None, serial: str = "") -> bytes:
    def once() -> bytes:
        with Session(product_id, serial) as session:
            return session.read_super()
    return _retry(once)


def write_super(record: bytes, product_id: int | None = None, serial: str = "") -> None:
    def once() -> None:
        with Session(product_id, serial) as session:
            session.write_super(record)
    _retry(once)


__all__ = ["GIP_INTERFACE", "VENDOR_ID", "Session", "TransportError", "read_super", "write_super"]
