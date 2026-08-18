"""USB CDC link to a Creative device, including the unlock handshake.

Ported from ``plasma_creative_x4/transport/usbcdc.py``, with one deliberate change: **nothing is
hardcoded to the Sound Blaster X4**. The source project targets one card and can name its
interfaces (1 and 2) and endpoints (``0x03``/``0x82``) as constants. This module has to open
whatever Creative device the registry matched, and those numbers differ per model, so the CDC
function and its endpoints are read from the device's own descriptors.

That generalisation is justified by the vendor library rather than assumed. ``CTCDC.dll``'s only
device-specific code is a display-name lookup -- the X4's own product id is not even in it -- and
its device check reads vendor and product from a struct at runtime. The DLL talks to whatever it
is handed. See ``docs/CREATIVE_UI_BEHAVIOUR.md`` §1.

**The device boots locked.** Every ``5A`` command is silently discarded until an ASCII
challenge/response handshake completes, so :meth:`unlock` runs before anything else and the whole
connect takes a few seconds. See :mod:`.protocol.unlock`.
"""

from __future__ import annotations

import contextlib
import logging
import struct
import time
from typing import Any

from .protocol import framing
from .protocol.ids import Cmd

log = logging.getLogger(__name__)

#: Creative Technology. The one constant that stays: it is what the module matches on.
VENDOR_ID = 0x041E

#: USB CDC class codes. The comm interface carries the line-coding control requests; the data
#: interface carries the bulk endpoints the protocol actually flows over.
CDC_COMM_CLASS, CDC_ACM_SUBCLASS, CDC_DATA_CLASS = 0x02, 0x02, 0x0A

SET_LINE_CODING = 0x20
SET_CONTROL_LINE_STATE = 0x22
DTR = 0x01

#: CTCDC.dll's serial setup, recovered by disassembly: 115200 8N1 with fDtrControl/fRtsControl
#: DISABLED, then EscapeCommFunction(SETDTR). So DTR is asserted and RTS stays low -- asserting
#: both does not work.
BAUD = 115200

#: CTCDC.dll retries the `whoareyou` probe 5x with a 1 s pause, and the capture shows why: the
#: first probe often goes unanswered.
UNLOCK_RETRIES = 5
UNLOCK_RETRY_DELAY = 1.0

#: Sent as the application name in the handshake. The device does not validate it -- the capture
#: shows Creative's own app sending its name -- but something has to go there.
APP_NAME = b"MyApp8"


class TransportError(Exception):
    pass


class UsbCdcTransport:
    """Blocking USB CDC link. Not thread-safe: own it from one thread.

    The port keeps the source's threading contract exactly. ``Device.set`` is dispatched with
    ``asyncio.to_thread``, so one worker thread owns this object for the length of an operation.
    """

    def __init__(self, product_id: int | None = None, serial: str = "",
                 timeout_ms: int = 1000) -> None:
        self.product_id = product_id
        self.serial = serial
        self.timeout_ms = timeout_ms
        self.unlocked = False
        self._dev: Any = None
        self._claimed: list[int] = []
        self._ep_out = self._ep_in = 0
        self._comm_iface = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._dev is not None

    def open(self) -> None:
        import usb.core
        import usb.util

        matches = list(usb.core.find(find_all=True, idVendor=VENDOR_ID,
                                     **({"idProduct": self.product_id} if self.product_id else {})))
        if not matches:
            raise TransportError(
                f"no Creative device ({VENDOR_ID:04x}:"
                f"{self.product_id:04x}) found" if self.product_id else
                f"no Creative device ({VENDOR_ID:04x}) found")
        dev = self._pick(matches)

        comm, data = self._find_cdc(dev)
        if comm is None or data is None:
            raise TransportError(
                f"{dev.idVendor:04x}:{dev.idProduct:04x} exposes no CDC-ACM function; "
                "this device is not configurable over the Creative control channel")

        self._dev = dev
        self._comm_iface = comm.bInterfaceNumber
        for iface in (comm.bInterfaceNumber, data.bInterfaceNumber):
            # On kernels without `cdc_acm` nothing claims these, so the detach is usually a no-op.
            # It is attempted anyway because a kernel that *does* bind the driver would otherwise
            # fail the claim below with a bare EBUSY.
            try:
                if dev.is_kernel_driver_active(iface):
                    dev.detach_kernel_driver(iface)
            except (NotImplementedError, usb.core.USBError):
                pass
            try:
                usb.util.claim_interface(dev, iface)
                self._claimed.append(iface)
            except usb.core.USBError as exc:
                self.close()
                raise TransportError(
                    f"cannot claim interface {iface}: {exc}\n"
                    "Is the udev rule installed? See docs/INSTALL.md.") from exc

        self._ep_out, self._ep_in = self._endpoints(data)
        self._configure_line()

    def _pick(self, matches: list[Any]) -> Any:
        """The device this row means, when several of the same model are plugged in.

        Serial first, because that is what the uid is built from. Without one there is nothing to
        tell two identical cards apart, so the first is taken and the ambiguity is logged rather
        than hidden -- silently configuring the wrong card is the worse outcome.
        """
        if self.serial:
            for dev in matches:
                try:
                    if dev.serial_number == self.serial:
                        return dev
                except (ValueError, NotImplementedError):
                    continue  # serial unreadable without permission; fall through
        if len(matches) > 1:
            log.warning("%d Creative devices match and none by serial %r; using the first",
                        len(matches), self.serial)
        return matches[0]

    @staticmethod
    def _find_cdc(dev: Any) -> tuple[Any, Any]:
        """The CDC-ACM comm interface and its data interface, from the descriptors.

        Paired by proximity rather than by parsing the Union functional descriptor: the data
        interface of an ACM function is the next data-class interface after its comm interface,
        and every Creative device seen lays them out consecutively. Parsing the Union descriptor
        would be more correct in principle and needs a device that disagrees to be worth it.
        """
        comm = data = None
        for cfg in dev:
            for iface in cfg:
                if (iface.bInterfaceClass == CDC_COMM_CLASS
                        and iface.bInterfaceSubClass == CDC_ACM_SUBCLASS and comm is None):
                    comm = iface
                elif iface.bInterfaceClass == CDC_DATA_CLASS and comm is not None and data is None:
                    data = iface
            if comm is not None and data is not None:
                break
        return comm, data

    @staticmethod
    def _endpoints(data: Any) -> tuple[int, int]:
        """Bulk OUT and IN addresses from the data interface."""
        out = in_ = 0
        for ep in data:
            if ep.bmAttributes & 0x03 != 0x02:      # bulk only
                continue
            if ep.bEndpointAddress & 0x80:
                in_ = in_ or ep.bEndpointAddress
            else:
                out = out or ep.bEndpointAddress
        if not out or not in_:
            raise TransportError(
                f"CDC data interface {data.bInterfaceNumber} has no bulk endpoint pair")
        return out, in_

    def _configure_line(self) -> None:
        import usb.core

        dev = self._dev
        try:
            dev.ctrl_transfer(0x21, SET_LINE_CODING, 0, self._comm_iface,
                              struct.pack("<IBBB", BAUD, 0, 0, 8), timeout=self.timeout_ms)
            dev.ctrl_transfer(0x21, SET_CONTROL_LINE_STATE, 0x00, self._comm_iface,
                              None, timeout=self.timeout_ms)
            time.sleep(0.05)
            self.drain()
            dev.ctrl_transfer(0x21, SET_CONTROL_LINE_STATE, DTR, self._comm_iface,
                              None, timeout=self.timeout_ms)
        except usb.core.USBError as exc:
            log.warning("CDC line setup failed (continuing): %s", exc)
        time.sleep(0.15)
        self.drain()

    def close(self) -> None:
        if self._dev is None:
            return
        import usb.core
        import usb.util

        for iface in self._claimed:
            with contextlib.suppress(usb.core.USBError):
                usb.util.release_interface(self._dev, iface)
        self._claimed.clear()
        with contextlib.suppress(usb.core.USBError):
            usb.util.dispose_resources(self._dev)
        self._dev = None
        self.unlocked = False

    # -- raw io ------------------------------------------------------------

    def write_raw(self, data: bytes) -> None:
        if self._dev is None:
            raise TransportError("not connected")
        self._dev.write(self._ep_out, data, timeout=self.timeout_ms)

    def read_raw(self, timeout_ms: int = 300) -> bytes:
        import usb.core

        if self._dev is None:
            raise TransportError("not connected")
        try:
            return bytes(self._dev.read(self._ep_in, 512, timeout=timeout_ms))
        except usb.core.USBError:
            return b""          # timeout is the normal idle case

    def drain(self, timeout_ms: int = 40) -> list[tuple[int, bytes]]:
        """Read everything already queued and return the frames.

        Sends nothing, so it costs no link traffic. Loops until the endpoint is empty: one write
        can trigger a burst of eight or more frames.
        """
        frames: list[tuple[int, bytes]] = []
        while True:
            raw = self.read_raw(timeout_ms)
            if not raw:
                break
            frames.extend(framing.split(raw))
        return frames

    # -- unlock ------------------------------------------------------------

    def unlock(self, app_name: bytes = APP_NAME) -> None:
        """Complete the ASCII gate. Idempotent."""
        from .protocol.unlock import CHALLENGE_LEN, UnlockError, UnlockResponder

        # The unlock survives until the device is power-cycled, and once in command mode
        # `whoareyou` stops being answered -- so probe first.
        if self._probe_unlocked():
            self.unlocked = True
            log.info("device already unlocked")
            return

        reply = b""
        for attempt in range(UNLOCK_RETRIES):
            self.write_raw(b"whoareyou." + app_name + b"\r\n")
            for _ in range(4):
                reply = self.read_raw(500)
                if reply:
                    break
            if reply.startswith(b"whoareyou"):
                break
            if b"NotYet" in reply:
                log.info("device says NotYet, retrying (%d/%d)", attempt + 1, UNLOCK_RETRIES)
            reply = b""
            time.sleep(UNLOCK_RETRY_DELAY)
        if not reply.startswith(b"whoareyou"):
            raise TransportError(f"no unlock challenge after {UNLOCK_RETRIES} attempts")

        # Take exactly CHALLENGE_LEN bytes rather than trimming a trailing delimiter. The
        # challenge is binary and can itself end in \r or \n, so any strip-based parse depends on
        # guessing where the payload stops.
        start = len(b"whoareyou")
        challenge = reply[start:start + CHALLENGE_LEN]
        try:
            response = UnlockResponder().respond(challenge)
        except UnlockError as exc:
            # A missing `cryptography` is not a transport failure, and calling it one would tell
            # the user their card is broken when a package is simply not installed. The responder
            # wraps the ImportError to keep the source project's contract, so it is unwrapped here
            # and let through for the device layer to name the package.
            if isinstance(exc.__cause__, ImportError):
                raise exc.__cause__ from None
            raise TransportError(f"cannot compute unlock response: {exc}") from exc

        self.write_raw(b"unlock" + response + b"\r\n")
        ack = b""
        for _ in range(6):
            ack = self.read_raw(800)
            if ack:
                break
        if b"unlock_OK" not in ack:
            raise TransportError(f"unlock rejected (got {ack[:24]!r})")

        self.write_raw(b"SW_MODE1\r\n")
        self.read_raw(800)          # 0x5B mode-confirm frame
        self.unlocked = True
        log.info("unlocked; 5A command mode active")

    def _probe_unlocked(self) -> bool:
        import usb.core

        self.drain(30)
        try:
            self.write_raw(framing.build(Cmd.MAX_PAYLOAD_SIZE))
        except (usb.core.USBError, TransportError):
            return False
        reply = self.read_raw(700)
        return len(reply) >= 2 and reply[1] == Cmd.MAX_PAYLOAD_SIZE


__all__ = ["TransportError", "UsbCdcTransport", "VENDOR_ID"]
