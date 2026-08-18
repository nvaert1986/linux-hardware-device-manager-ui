"""The V4L2 and UVC ioctls this module needs, as ctypes.

Hand-written rather than taken from a binding, for the same reason the Dell monitor module shells
out to ``ddcutil`` rather than linking libddcutil: the surface is small, stable and decades old, and
a dependency for six structures is a dependency for six structures. ``ctypes`` is in the standard
library and this file has no imports beyond it.

The structures and request codes are the kernel's own, from ``linux/videodev2.h`` and
``linux/uvcvideo.h``. They are cross-checked against ``cameractrls``, which has been driving real
cameras with them for three years -- see ``docs/UVC_CAMERAS_UI_BEHAVIOUR.md`` for what was taken
from where.

**Nothing here opens a device or performs an ioctl.** It only describes their shapes, so it is
importable and testable with no camera attached and no permissions.
"""

from __future__ import annotations

import ctypes

# ---------------------------------------------------------------- ioctl encoding
#
# The kernel packs direction, a type letter, a number and the argument size into one 32-bit
# request. Reimplemented rather than imported because Python has no _IOC.

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, letter: str, number: int, size: int) -> int:
    return (
        ctypes.c_int32(direction << _IOC_DIRSHIFT).value
        | ctypes.c_int32(ord(letter) << _IOC_TYPESHIFT).value
        | ctypes.c_int32(number << _IOC_NRSHIFT).value
        | ctypes.c_int32(size << _IOC_SIZESHIFT).value
    )


def _ior(letter: str, number: int, kind: type) -> int:
    return _ioc(_IOC_READ, letter, number, ctypes.sizeof(kind))


def _iowr(letter: str, number: int, kind: type) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, letter, number, ctypes.sizeof(kind))


# ---------------------------------------------------------------- capability

class v4l2_capability(ctypes.Structure):  # noqa: N801 - the kernel's own name
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


V4L2_CAP_VIDEO_CAPTURE = 0x00000001
"""The bit that separates a camera from the metadata node beside it.

Checked against ``device_caps`` rather than ``capabilities``: the latter describes everything the
*driver* can do across all its nodes, so on a UVC camera it is set for the metadata node too.
"""

# ---------------------------------------------------------------- controls

(
    V4L2_CTRL_TYPE_INTEGER,
    V4L2_CTRL_TYPE_BOOLEAN,
    V4L2_CTRL_TYPE_MENU,
    V4L2_CTRL_TYPE_BUTTON,
    V4L2_CTRL_TYPE_INTEGER64,
    V4L2_CTRL_TYPE_CTRL_CLASS,
    V4L2_CTRL_TYPE_STRING,
    V4L2_CTRL_TYPE_BITMASK,
    V4L2_CTRL_TYPE_INTEGER_MENU,
) = range(1, 10)

V4L2_CTRL_FLAG_READ_ONLY = 0x0004
V4L2_CTRL_FLAG_INACTIVE = 0x0010
"""Set while another control makes this one meaningless -- ``focus_absolute`` while continuous
autofocus is on. The device says so, which is a gate we get for free rather than having to infer."""

V4L2_CTRL_FLAG_NEXT_CTRL = 0x80000000
V4L2_CTRL_FLAG_NEXT_COMPOUND = 0x40000000
"""Walk the controls the device actually has, rather than probing a list of ids we hope it has."""

V4L2_CTRL_CLASS_MASK = 0x00FF0000
V4L2_CTRL_CLASS_USER = 0x00980000
V4L2_CTRL_CLASS_CAMERA = 0x009A0000


class v4l2_control(ctypes.Structure):  # noqa: N801
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


class v4l2_queryctrl(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("minimum", ctypes.c_int32),
        ("maximum", ctypes.c_int32),
        ("step", ctypes.c_int32),
        ("default", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class v4l2_querymenu(ctypes.Structure):  # noqa: N801
    class _u(ctypes.Union):
        _fields_ = [("name", ctypes.c_char * 32), ("value", ctypes.c_int64)]

    _fields_ = [
        ("id", ctypes.c_uint32),
        ("index", ctypes.c_uint32),
        ("_u", _u),
        ("reserved", ctypes.c_uint32),
    ]
    _anonymous_ = ("_u",)
    _pack_ = True
    _layout_ = "ms"


# ---------------------------------------------------------------- formats
#
# Read only, and that is a decision rather than an omission: changing the format or frame rate
# locks the device and needs the file descriptor reopened, which does not fit a page of settings.
# See docs/UVC_CAMERAS_UI_BEHAVIOUR.md.

class v4l2_fmtdesc(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


class v4l2_frmsize_discrete(ctypes.Structure):  # noqa: N801
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32)]


class v4l2_frmsize_stepwise(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
        ("step_width", ctypes.c_uint32), ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32), ("step_height", ctypes.c_uint32),
    ]


class v4l2_frmsizeenum(ctypes.Structure):  # noqa: N801
    class _u(ctypes.Union):
        _fields_ = [("discrete", v4l2_frmsize_discrete), ("stepwise", v4l2_frmsize_stepwise)]

    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("_u", _u),
        ("reserved", ctypes.c_uint32 * 2),
    ]
    _anonymous_ = ("_u",)


V4L2_FRMSIZE_TYPE_DISCRETE = 1


class v4l2_fract(ctypes.Structure):  # noqa: N801
    _fields_ = [("numerator", ctypes.c_uint32), ("denominator", ctypes.c_uint32)]


class v4l2_frmivalenum(ctypes.Structure):  # noqa: N801
    """Frame intervals available for one pixel format at one frame size.

    An *interval*, not a rate: the kernel reports seconds per frame, so 30 fps arrives as 1/30 and
    the reciprocal is taken where it is shown. Reporting it the kernel's way round would be accurate
    and unreadable.
    """

    class _u(ctypes.Union):
        # Stepwise is min, max and step -- three intervals. Webcams report discrete sets; this arm
        # exists so the union is the right size, not because anything reads it.
        _fields_ = [("discrete", v4l2_fract), ("stepwise", v4l2_fract * 3)]

    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("_u", _u),
        ("reserved", ctypes.c_uint32 * 2),
    ]
    _anonymous_ = ("_u",)


V4L2_FRMIVAL_TYPE_DISCRETE = 1


# ---------------------------------------------------------------- current mode
#
# What the node is set to right now, which is a different question from what it can do. Both are
# read and never written -- see docs/UVC_CAMERAS_UI_BEHAVIOUR.md section 6.

class v4l2_pix_format(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class v4l2_format(ctypes.Structure):  # noqa: N801
    """``type``, then a 200-byte union.

    The explicit padding word is load-bearing. The union carries ``v4l2_window``, which holds a
    pointer, so on 64-bit the union is 8-byte aligned and the whole structure is 208 bytes rather
    than 204. Without the pad every field reads back shifted by four, and a 640x480 camera reports a
    width of zero and a height of 640 -- which looks like a driver quirk rather than a struct bug.
    """

    _fields_ = [
        ("type", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("pix", v4l2_pix_format),
        ("_rest", ctypes.c_uint8 * (200 - ctypes.sizeof(v4l2_pix_format))),
    ]


class v4l2_captureparm(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("capability", ctypes.c_uint32),
        ("capturemode", ctypes.c_uint32),
        ("timeperframe", v4l2_fract),
        ("extendedmode", ctypes.c_uint32),
        ("readbuffers", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


class v4l2_streamparm(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("capture", v4l2_captureparm),
        ("_rest", ctypes.c_uint8 * (200 - ctypes.sizeof(v4l2_captureparm))),
    ]
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1


# ---------------------------------------------------------------- UVC extension units

class uvc_xu_control_query(ctypes.Structure):  # noqa: N801
    """The kernel's passthrough to a UVC extension unit.

    This is what makes vendor features reachable without libusb, without claiming an interface and
    without detaching a driver: unit, selector and one of the UVC request codes below, through the
    same file descriptor the standard controls use.
    """

    _fields_ = [
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.c_void_p),
    ]


# UVC 1.5 spec, A.8: Video Class-Specific Request Codes.
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86
UVC_GET_DEF = 0x87

# ---------------------------------------------------------------- request codes

VIDIOC_QUERYCAP = _ior("V", 0, v4l2_capability)
VIDIOC_ENUM_FMT = _iowr("V", 2, v4l2_fmtdesc)
VIDIOC_G_CTRL = _iowr("V", 27, v4l2_control)
VIDIOC_S_CTRL = _iowr("V", 28, v4l2_control)
VIDIOC_QUERYCTRL = _iowr("V", 36, v4l2_queryctrl)
VIDIOC_QUERYMENU = _iowr("V", 37, v4l2_querymenu)
VIDIOC_ENUM_FRAMESIZES = _iowr("V", 74, v4l2_frmsizeenum)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr("V", 75, v4l2_frmivalenum)
VIDIOC_G_FMT = _iowr("V", 4, v4l2_format)
VIDIOC_S_FMT = _iowr("V", 5, v4l2_format)
VIDIOC_G_PARM = _iowr("V", 21, v4l2_streamparm)
VIDIOC_S_PARM = _iowr("V", 22, v4l2_streamparm)
UVCIOC_CTRL_QUERY = _iowr("u", 0x21, uvc_xu_control_query)


def fourcc(value: int) -> str:
    """``0x56595559`` -> ``"YUYV"``. The pixel format as the kernel spells it."""
    return "".join(chr((value >> shift) & 0xFF) for shift in (0, 8, 16, 24)).strip()


__all__ = [
    "UVCIOC_CTRL_QUERY", "UVC_GET_CUR", "UVC_GET_DEF", "UVC_GET_INFO", "UVC_GET_LEN",
    "UVC_GET_MAX", "UVC_GET_MIN", "UVC_GET_RES", "UVC_SET_CUR",
    "V4L2_BUF_TYPE_VIDEO_CAPTURE", "V4L2_CAP_VIDEO_CAPTURE", "V4L2_CTRL_CLASS_CAMERA",
    "V4L2_CTRL_CLASS_MASK", "V4L2_CTRL_CLASS_USER", "V4L2_CTRL_FLAG_INACTIVE",
    "V4L2_CTRL_FLAG_NEXT_COMPOUND", "V4L2_CTRL_FLAG_NEXT_CTRL", "V4L2_CTRL_FLAG_READ_ONLY",
    "V4L2_CTRL_TYPE_BOOLEAN", "V4L2_CTRL_TYPE_BUTTON", "V4L2_CTRL_TYPE_INTEGER",
    "V4L2_CTRL_TYPE_INTEGER_MENU", "V4L2_CTRL_TYPE_MENU", "V4L2_FRMIVAL_TYPE_DISCRETE",
    "V4L2_FRMSIZE_TYPE_DISCRETE",
    "VIDIOC_ENUM_FMT", "VIDIOC_ENUM_FRAMEINTERVALS", "VIDIOC_ENUM_FRAMESIZES", "VIDIOC_G_CTRL",
    "VIDIOC_G_FMT", "VIDIOC_G_PARM", "VIDIOC_QUERYCAP", "VIDIOC_S_FMT", "VIDIOC_S_PARM",
    "VIDIOC_QUERYCTRL", "VIDIOC_QUERYMENU", "VIDIOC_S_CTRL", "fourcc",
    "uvc_xu_control_query", "v4l2_capability", "v4l2_control", "v4l2_fmtdesc", "v4l2_format",
    "v4l2_fract", "v4l2_frmivalenum", "v4l2_frmsizeenum", "v4l2_querymenu", "v4l2_queryctrl",
    "v4l2_streamparm",
]
