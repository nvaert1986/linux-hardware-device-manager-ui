"""An open camera: standard V4L2 controls, and vendor controls through extension units.

Qt-free and synchronous, like every other module's protocol layer. :class:`~..device.UvcCamera`
dispatches into it with ``asyncio.to_thread``.

**One file descriptor does everything.** That is the whole reason this module is small: standard
controls, vendor controls and format enumeration all go through ``/dev/videoN``. No libusb, no
interface to claim, no kernel driver to detach and put back -- which is what the Creative and 8BitDo
modules spend most of their transport code on.

Two discovery mechanisms, and neither hardcodes a model:

*Standard controls* are walked with ``VIDIOC_QUERYCTRL`` and ``V4L2_CTRL_FLAG_NEXT_CTRL``, so the
device lists what it has and reports each one's type, range, default and flags.

*Vendor controls* live in UVC **extension units**, addressed by a unit id that differs per model.
The id is found by locating the unit's 16-byte GUID in the device's own USB descriptors -- see
:func:`unit_id` -- and each control is then probed with ``GET_LEN`` before being offered.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
from dataclasses import dataclass, field
from fcntl import ioctl
from pathlib import Path

from . import ioctls as io

log = logging.getLogger(__name__)


class CameraError(Exception):
    pass


@dataclass
class Control:
    """One standard V4L2 control, as the device described itself."""

    ident: int
    key: str
    name: str
    kind: str
    """``integer``, ``boolean``, ``menu`` or ``button`` -- the four V4L2 offers that mean anything
    on a form. Compound and string controls are skipped; no camera has been seen to use them."""

    value: int | None = None
    default: int | None = None
    minimum: int = 0
    maximum: int = 0
    step: int = 1
    inactive: bool = False
    readonly: bool = False
    menu: list[tuple[int, str]] = field(default_factory=list)
    """``(value, label)`` in the device's own order and wording."""


@dataclass
class VendorControl:
    """One control inside an extension unit, as declared by :mod:`.extensions` and then probed."""

    key: str
    name: str
    kind: str
    unit: int
    selector: int
    length: int
    offset: int
    value: int | None = None
    minimum: int = 0
    maximum: int = 0
    menu: list[tuple[int, str]] = field(default_factory=list)
    description: str = ""


def _busy(exc: OSError) -> str:
    """A readable reason, because ``EBUSY`` here has exactly one cause worth naming.

    Unlike a control write, changing the format needs the node not to be in use -- and on a desktop
    running PipeWire, anything using the camera means PipeWire is holding it. Reporting the raw
    errno leaves the user with nothing to act on; naming the cause tells them what to close.
    """
    if exc.errno == errno.EBUSY:
        return (
            "the camera is in use, so its format cannot be changed. Close whatever is capturing "
            "from it -- on a PipeWire desktop that includes any browser tab or app with the camera "
            "open -- and try again. The image controls above work either way."
        )
    return str(exc)


def _fourcc(pixelformat: str) -> int:
    """``"YUYV"`` -> ``0x56595559``. The inverse of :func:`ioctls.fourcc`."""
    return sum(ord(c) << (8 * i) for i, c in enumerate(pixelformat.ljust(4)[:4]))


def unit_id(node: str, guid: bytes) -> int:
    """The extension unit carrying *guid*, or 0.

    Read out of ``descriptors`` in sysfs -- the device's own raw USB descriptor blob -- by finding
    the GUID and taking **the byte immediately before it**, which is where the unit id sits in an
    Extension Unit descriptor. The technique is ``cameractrls``'s and it is the reason none of this
    needs a per-model table of unit numbers: on the Brio here the ids came back 10 and 11, and
    nothing in this module had to know that in advance.

    Nothing is opened. A missing or unreadable file means "no such unit", which is the same answer
    as a camera that genuinely has none.
    """
    real = Path(os.path.realpath(node))
    path = Path("/sys/class/video4linux") / real.name / ".." / ".." / ".." / "descriptors"
    try:
        descriptors = path.resolve().read_bytes()
    except OSError as exc:
        log.debug("no descriptors for %s: %s", node, exc)
        return 0
    found = descriptors.find(guid)
    return descriptors[found - 1] if found > 0 else 0


def usb_ids(node: str) -> tuple[int, int]:
    """``(vendor, product)`` of the USB device behind this video node, or ``(0, 0)``."""
    real = Path(os.path.realpath(node))
    base = Path("/sys/class/video4linux") / real.name / ".." / ".." / ".."
    out = []
    for name in ("idVendor", "idProduct"):
        try:
            out.append(int((base / name).resolve().read_text().strip(), 16))
        except (OSError, ValueError):
            return (0, 0)
    return (out[0], out[1])


class Session:
    """One open video node. Not thread-safe: own it from one thread."""

    def __init__(self, node: str) -> None:
        self.node = node
        self._fd: int | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> Session:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def opened(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        """Open the node read-write.

        Read-write even to *read* controls, because ``VIDIOC_S_CTRL`` needs it and reopening later
        would mean a second window in which the node could be taken. It does **not** claim the
        stream: another application can be using the camera while its settings are changed, which
        is the point of doing this through V4L2 rather than USB.
        """
        try:
            self._fd = os.open(self.node, os.O_RDWR)
        except OSError as exc:
            raise CameraError(
                f"cannot open {self.node}: {exc}\n"
                "Membership of the 'video' group is what normally grants this."
            ) from exc

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _require(self) -> int:
        if self._fd is None:
            raise CameraError(f"{self.node} is not open")
        return self._fd

    # ------------------------------------------------------------------ identity

    def capability(self) -> tuple[str, str, str]:
        """``(card, driver, bus_info)``. The camera's own name for itself."""
        cap = io.v4l2_capability()
        ioctl(self._require(), io.VIDIOC_QUERYCAP, cap)
        return (cap.card.decode(errors="replace"),
                cap.driver.decode(errors="replace"),
                cap.bus_info.decode(errors="replace"))

    def formats(self) -> list[str]:
        """Pixel formats, in the device's order. Read only -- see :mod:`.ioctls`."""
        out: list[str] = []
        desc = io.v4l2_fmtdesc()
        desc.type = io.V4L2_BUF_TYPE_VIDEO_CAPTURE
        while True:
            try:
                ioctl(self._require(), io.VIDIOC_ENUM_FMT, desc)
            except OSError:
                break
            out.append(io.fourcc(desc.pixelformat))
            desc.index += 1
        return out

    def resolutions(self, pixelformat: str) -> list[tuple[int, int]]:
        """Discrete frame sizes for one format, largest first.

        Only discrete sizes: a stepwise or continuous range is a scaler rather than a list of modes,
        and no UVC camera seen reports one.
        """
        out: list[tuple[int, int]] = []
        frame = io.v4l2_frmsizeenum()
        frame.pixel_format = _fourcc(pixelformat)
        while True:
            try:
                ioctl(self._require(), io.VIDIOC_ENUM_FRAMESIZES, frame)
            except OSError:
                break
            if frame.type != io.V4L2_FRMSIZE_TYPE_DISCRETE:
                break
            out.append((frame.discrete.width, frame.discrete.height))
            frame.index += 1
        return sorted(out, reverse=True)

    def frame_rates(self, pixelformat: str, width: int, height: int) -> list[float]:
        """Frame rates for one format at one size, fastest first.

        The kernel reports intervals -- seconds per frame -- so each is inverted here. A zero
        numerator would be a driver bug rather than an infinite rate, and is skipped rather than
        allowed to raise.
        """
        out: list[float] = []
        interval = io.v4l2_frmivalenum()
        interval.pixel_format = _fourcc(pixelformat)
        interval.width, interval.height = width, height
        while True:
            try:
                ioctl(self._require(), io.VIDIOC_ENUM_FRAMEINTERVALS, interval)
            except OSError:
                break
            if interval.type != io.V4L2_FRMIVAL_TYPE_DISCRETE:
                break
            numerator = interval.discrete.numerator
            if numerator:
                out.append(interval.discrete.denominator / numerator)
            interval.index += 1
        return sorted(out, reverse=True)

    def current_mode(self) -> tuple[int, int, str, float] | None:
        """``(width, height, format, fps)`` the node is set to now, or ``None`` if it will not say.

        Deliberately separate from :meth:`resolutions`: what a camera *can* do and what it is
        *currently* set to are different questions, and the second one changes under us whenever
        something else opens the camera and negotiates its own format.
        """
        try:
            fmt = io.v4l2_format()
            fmt.type = io.V4L2_BUF_TYPE_VIDEO_CAPTURE
            ioctl(self._require(), io.VIDIOC_G_FMT, fmt)
            parm = io.v4l2_streamparm()
            parm.type = io.V4L2_BUF_TYPE_VIDEO_CAPTURE
            ioctl(self._require(), io.VIDIOC_G_PARM, parm)
        except OSError:
            return None
        period = parm.capture.timeperframe
        fps = period.denominator / period.numerator if period.numerator else 0.0
        return fmt.pix.width, fmt.pix.height, io.fourcc(fmt.pix.pixelformat), fps

    # ------------------------------------------------------------------ streaming mode
    #
    # Ported from cameractrls' V4L2FmtCtrls, including the two rules that matter and are easy to
    # leave out. Every write is a read-modify-write of the whole format (never a fresh structure, so
    # the fields not being changed keep the driver's own values), and every write is *verified*:
    # V4L2 lets a driver accept the ioctl and substitute a different value, so what came back is
    # compared with what was asked and the difference is reported rather than swallowed.

    def set_pixelformat(self, pixelformat: str) -> str:
        """Set the output format. Returns what the driver settled on."""
        fmt = self._format()
        if io.fourcc(fmt.pix.pixelformat) == pixelformat:
            return pixelformat
        fmt.pix.pixelformat = _fourcc(pixelformat)
        landed = self._apply_format(fmt)
        if landed[2] != pixelformat:
            raise CameraError(f"the camera used {landed[2]} instead of {pixelformat}")
        return landed[2]

    def set_resolution(self, width: int, height: int) -> tuple[int, int]:
        """Set the frame size, keeping the current pixel format."""
        fmt = self._format()
        if (fmt.pix.width, fmt.pix.height) == (width, height):
            return width, height
        fmt.pix.width, fmt.pix.height = width, height
        landed = self._apply_format(fmt)
        if landed[:2] != (width, height):
            raise CameraError(
                f"the camera used {landed[0]}×{landed[1]} instead of {width}×{height}"
            )
        return landed[0], landed[1]

    def set_frame_rate(self, fps: float) -> float:
        """Set the frame rate, as an interval.

        The interval is sent as ``10 / (fps * 10)`` rather than ``1 / fps`` -- that is cameractrls'
        convention and it is not cosmetic: 7.5 fps is a real UVC rate and ``1/7.5`` is not
        expressible in the integer pair the kernel takes, where ``10/75`` is exact.
        """
        parm = io.v4l2_streamparm()
        parm.type = io.V4L2_BUF_TYPE_VIDEO_CAPTURE
        parm.capture.timeperframe.numerator = 10
        parm.capture.timeperframe.denominator = int(fps * 10)
        try:
            ioctl(self._require(), io.VIDIOC_S_PARM, parm)
        except OSError as exc:
            raise CameraError(_busy(exc)) from exc
        period = parm.capture.timeperframe
        if not period.numerator or not period.denominator:
            raise CameraError(f"the camera reported no valid rate for {fps:g} fps")
        landed = period.denominator / period.numerator
        if abs(landed - fps) > 0.01:
            raise CameraError(f"the camera used {landed:g} fps instead of {fps:g}")
        return landed

    def reopen(self) -> None:
        """Close and reopen the node.

        cameractrls marks all three of these controls ``reopener=True`` with the comment that
        changing them locks the device. Reopening after a format change keeps this module's
        descriptor from holding a lock that would make the *next* change fail against itself.
        """
        self.close()
        self.open()

    def _format(self) -> io.v4l2_format:
        fmt = io.v4l2_format()
        fmt.type = io.V4L2_BUF_TYPE_VIDEO_CAPTURE
        try:
            ioctl(self._require(), io.VIDIOC_G_FMT, fmt)
        except OSError as exc:
            raise CameraError(f"cannot read the current format: {exc}") from exc
        return fmt

    def _apply_format(self, fmt: io.v4l2_format) -> tuple[int, int, str]:
        try:
            ioctl(self._require(), io.VIDIOC_S_FMT, fmt)
        except OSError as exc:
            raise CameraError(_busy(exc)) from exc
        return fmt.pix.width, fmt.pix.height, io.fourcc(fmt.pix.pixelformat)

    # ------------------------------------------------------------------ standard controls

    def controls(self) -> list[Control]:
        """Every standard control the device admits to, with its current value.

        Walked rather than probed: ``V4L2_CTRL_FLAG_NEXT_CTRL`` asks the driver for the next control
        it *has*, so the list is the device's and a camera with an unusual control gets it offered
        without this module knowing the id.
        """
        fd = self._require()
        step = io.V4L2_CTRL_FLAG_NEXT_CTRL | io.V4L2_CTRL_FLAG_NEXT_COMPOUND
        query = io.v4l2_queryctrl(step)
        out: list[Control] = []
        while True:
            try:
                ioctl(fd, io.VIDIOC_QUERYCTRL, query)
            except OSError as exc:
                # EIO on one control is not the end of the list: some UVC cameras raise it for a
                # single control and answer for everything after it. cameractrls skips and carries
                # on, which is where this behaviour comes from.
                if exc.errno == 5:
                    query = io.v4l2_queryctrl(query.id + 1 | step)
                    continue
                break
            control = self._describe(query)
            if control is not None:
                out.append(control)
            query = io.v4l2_queryctrl(query.id | step)
        return out

    def _describe(self, query: io.v4l2_queryctrl) -> Control | None:
        kind = {
            io.V4L2_CTRL_TYPE_INTEGER: "integer",
            io.V4L2_CTRL_TYPE_BOOLEAN: "boolean",
            io.V4L2_CTRL_TYPE_MENU: "menu",
            io.V4L2_CTRL_TYPE_INTEGER_MENU: "menu",
            io.V4L2_CTRL_TYPE_BUTTON: "button",
        }.get(query.type)
        if kind is None:
            return None

        # A 0..1 integer with step 1 is a boolean wearing an integer's clothes, and every UVC
        # driver produces several. Rendering them as sliders with two positions is nobody's intent.
        if kind == "integer" and query.minimum == 0 and query.maximum == 1 and query.step == 1:
            kind = "boolean"

        name = query.name.decode(errors="replace")
        control = Control(
            ident=query.id,
            key=_key(name),
            name=name,
            kind=kind,
            default=None if kind == "button" else query.default,
            minimum=query.minimum,
            maximum=query.maximum,
            step=query.step or 1,
            inactive=bool(query.flags & io.V4L2_CTRL_FLAG_INACTIVE),
            readonly=bool(query.flags & io.V4L2_CTRL_FLAG_READ_ONLY),
        )
        if kind == "menu":
            control.menu = self._menu(query)
        if kind != "button":
            control.value = self.get(query.id)
        return control

    def _menu(self, query: io.v4l2_queryctrl) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for index in range(query.minimum, query.maximum + 1):
            item = io.v4l2_querymenu(query.id, index)
            try:
                ioctl(self._require(), io.VIDIOC_QUERYMENU, item)
            except OSError:
                continue        # a gap in the menu is normal; the device just has no such mode
            if query.type == io.V4L2_CTRL_TYPE_INTEGER_MENU:
                out.append((index, str(item.value)))
            else:
                out.append((index, item.name.decode(errors="replace")))
        return out

    def get(self, ident: int) -> int | None:
        control = io.v4l2_control(ident)
        try:
            ioctl(self._require(), io.VIDIOC_G_CTRL, control)
        except OSError as exc:
            log.debug("cannot read control 0x%08x: %s", ident, exc)
            return None
        return int(control.value)

    def set(self, ident: int, value: int) -> int:
        """Write a control and return what the driver settled on.

        The driver may clamp or round -- a step of 5 on the Brio's focus means 3 is not a value it
        can hold -- and it writes the accepted number back into the struct. That is returned rather
        than the request, so the page shows what the camera has rather than what it was asked for.
        """
        control = io.v4l2_control(ident, value)
        try:
            ioctl(self._require(), io.VIDIOC_S_CTRL, control)
        except OSError as exc:
            raise CameraError(f"the camera refused that value: {exc}") from exc
        return int(control.value)

    # ------------------------------------------------------------------ extension units

    def _query_xu(self, unit: int, selector: int, request: int, buffer) -> bool:
        query = io.uvc_xu_control_query()
        query.unit = unit
        query.selector = selector
        query.query = request
        query.size = ctypes.sizeof(buffer)
        query.data = ctypes.cast(ctypes.pointer(buffer), ctypes.c_void_p)
        try:
            ioctl(self._require(), io.UVCIOC_CTRL_QUERY, query)
        except OSError as exc:
            log.debug("xu unit %d selector 0x%02x request 0x%02x: %s",
                      unit, selector, request, exc)
            return False
        return True

    def xu_length(self, unit: int, selector: int) -> int:
        """How many bytes this vendor control is, or 0 if it does not exist.

        Doubles as the existence check: a unit answering ``GET_LEN`` has the control, and one that
        does not is a control this model does not implement. That is why the vendor table can list
        selectors freely -- an absent one is simply not offered rather than being a broken row.
        """
        length = ctypes.c_uint16(0)
        if not self._query_xu(unit, selector, io.UVC_GET_LEN, length):
            return 0
        return int(length.value)

    def xu_read(self, unit: int, selector: int, length: int, request: int = io.UVC_GET_CUR) -> bytes:
        buffer = (ctypes.c_uint8 * length)()
        if not self._query_xu(unit, selector, request, buffer):
            return b""
        return bytes(buffer)

    def xu_write_byte(self, unit: int, selector: int, length: int, offset: int, value: int) -> None:
        """Change one byte of a vendor control, leaving the rest as it was.

        Read-modify-write, and not merely tidy: the Brio's LED control is five bytes carrying two
        separate settings -- mode at offset 1, blink frequency at offset 3 -- so writing a whole
        buffer to change one of them would silently reset the other. Every vendor control here is
        addressed as an offset into its own buffer for that reason.
        """
        current = self.xu_read(unit, selector, length)
        if not current:
            raise CameraError(
                f"cannot read vendor control {selector:#04x} on unit {unit} before writing it")
        buffer = (ctypes.c_uint8 * length)(*current)
        buffer[offset] = value & 0xFF
        if not self._query_xu(unit, selector, io.UVC_SET_CUR, buffer):
            raise CameraError(f"the camera refused vendor control {selector:#04x}")

        # Read it back. A UVC extension unit does not report failure the way a V4L2 control does:
        # SET_CUR can succeed and the device keep its old value, which then reads as a control that
        # does nothing. Same discipline as the Creative module's routing.
        landed = self.xu_read(unit, selector, length)
        if landed and landed[offset] != (value & 0xFF):
            raise CameraError(
                f"the camera did not take that value (asked {value}, holds {landed[offset]})")

    def xu_write_blob(self, unit: int, selector: int, payload: bytes) -> None:
        """Send a whole vendor payload as-is.

        For the controls that are commands rather than fields -- Razer, Dell and AnkerWork address
        their features by sending an opaque byte string to one selector. There is no read-back to
        verify against: these units answer ``SET_CUR`` and report nothing meaningful for
        ``GET_CUR``, which is why such controls are declared ``readable=False`` and the page does
        not claim to know where they are set.
        """
        buffer = (ctypes.c_uint8 * len(payload))(*payload)
        if not self._query_xu(unit, selector, io.UVC_SET_CUR, buffer):
            raise CameraError(f"the camera refused vendor control {selector:#04x}")


def _key(name: str) -> str:
    """A stable capability key from the driver's own control name.

    ``"White Balance, Automatic"`` becomes ``"white_balance_automatic"``. Derived rather than mapped
    from a table of ids, because the walk above finds controls this module has never heard of and
    they need keys too. Matches ``v4l2-ctl`` and ``cameractrls``, so a key here is the name a user
    will find in either.
    """
    out = []
    for char in name.lower():
        if char.isalnum():
            out.append(char)
        elif char in " -_" and out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


__all__ = ["CameraError", "Control", "Session", "VendorControl", "unit_id", "usb_ids"]
