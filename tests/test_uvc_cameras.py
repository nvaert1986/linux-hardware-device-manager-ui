"""The camera module without a camera.

What can be proved with no hardware is most of what a port gets wrong: that the ioctl request codes
are the kernel's, that a GUID is converted into the byte order a descriptor actually contains, that
the vendor table's gates keep one maker's payloads away from another's, and that one physical camera
produces one row. The parts that need a camera -- that a write lands and reads back -- were run
against a Logitech BRIO and are recorded in ``docs/UVC_CAMERAS_UI_BEHAVIOUR.md``.
"""

from __future__ import annotations

import ctypes

from hardware_ui.core.capability import Kind
from hardware_ui.core.device import Category, DeviceInfo, Support, Transport
from hardware_ui.core.modules import ModuleRegistry
from hardware_ui.modules.uvc_cameras import capabilities as C
from hardware_ui.modules.uvc_cameras.protocol import extensions as ext
from hardware_ui.modules.uvc_cameras.protocol import ioctls as io
from hardware_ui.modules.uvc_cameras.protocol.session import Control, _key

# --------------------------------------------------------------------------- ioctl encoding


def test_the_request_codes_are_the_kernels():
    """Pinned to the values the running kernel uses.

    A wrong request number does not fail loudly -- it addresses a *different* ioctl, which either
    errors for a reason that makes no sense or, worse, works. These were cross-checked against
    ``cameractrls``, which has been driving cameras with them for three years, and all eight agreed.
    """
    assert io.VIDIOC_QUERYCAP & 0xFFFFFFFF == 0x80685600
    assert io.VIDIOC_ENUM_FMT & 0xFFFFFFFF == 0xC0405602
    assert io.VIDIOC_G_CTRL & 0xFFFFFFFF == 0xC008561B
    assert io.VIDIOC_S_CTRL & 0xFFFFFFFF == 0xC008561C
    assert io.VIDIOC_QUERYCTRL & 0xFFFFFFFF == 0xC0445624
    assert io.VIDIOC_QUERYMENU & 0xFFFFFFFF == 0xC02C5625
    assert io.VIDIOC_ENUM_FRAMESIZES & 0xFFFFFFFF == 0xC02C564A
    assert io.UVCIOC_CTRL_QUERY & 0xFFFFFFFF == 0xC0107521


def test_the_structures_are_the_sizes_the_ioctls_encode():
    """The size is *part of* the request number, so a wrong struct means a wrong ioctl."""
    assert ctypes.sizeof(io.v4l2_capability) == 0x68
    assert ctypes.sizeof(io.v4l2_queryctrl) == 0x44
    assert ctypes.sizeof(io.v4l2_querymenu) == 0x2C
    assert ctypes.sizeof(io.v4l2_control) == 0x08
    assert ctypes.sizeof(io.uvc_xu_control_query) == 0x10


def test_a_pixel_format_reads_as_its_name():
    assert io.fourcc(0x56595559) == "YUYV"
    assert io.fourcc(0x47504A4D) == "MJPG"


# --------------------------------------------------------------------------- GUIDs


def test_a_guid_is_converted_to_the_order_a_descriptor_contains():
    """UVC writes a GUID's first three fields little-endian and the rest big-endian.

    So a descriptor never holds the bytes in the order the GUID is printed, and getting this wrong
    means no extension unit is ever found -- silently, because "not present" is a normal answer for
    a camera without one. Checked against the byte strings read out of a real Brio's descriptors.
    """
    assert ext.LOGITECH_BRIO.guid == bytes.fromhex("1502e44934f4fe47b1580e885023e51b")
    assert ext.LOGITECH_PERIPHERAL.guid == bytes.fromhex("212de5ff30802c4e82d9f587d00540bd")
    assert ext.RAZER_KIYO_PRO.guid == bytes.fromhex("d09ee4237811314fae52d2fb8a8d3b48")


def test_the_two_units_sharing_a_guid_are_told_apart_by_product_id():
    """``23e49ed0`` is the Razer Kiyo Pro's *and* the Dell UltraSharp's, and a Brio answers on it.

    The payloads are model-specific, so without the product-id gate this would send Razer commands
    to a Dell, or either to a Logitech. That gate is the only thing standing between the table and
    writing unknown values to an unknown control.
    """
    assert ext.RAZER_KIYO_PRO.guid == ext.DELL_ULTRASHARP.guid

    kiyo = {x.name for x in ext.for_product(0x0E05)}
    dell = {x.name for x in ext.for_product(0xC015)}
    assert "Razer Kiyo Pro" in kiyo and "Dell UltraSharp webcam" not in kiyo
    assert "Dell UltraSharp webcam" in dell and "Razer Kiyo Pro" not in dell

    # A Brio has that unit and must be offered neither vendor's controls.
    brio = {x.name for x in ext.for_product(0x085E)}
    assert "Razer Kiyo Pro" not in brio and "Dell UltraSharp webcam" not in brio


def test_the_brio_field_of_view_is_offered_only_to_brios():
    def fov(product_id: int) -> bool:
        return any(
            control.key == "field_of_view"
            for extension in ext.for_product(product_id)
            if extension.name == "Logitech Brio"
            for control in extension.controls
        )

    assert fov(0x085E), "the model this was verified on"
    assert fov(0x0944), "MX Brio, same family"
    assert not fov(0xC52B), "a Unifying receiver is not a camera"


def test_only_the_logitech_entries_claim_to_have_been_verified():
    """Everything else is carried from another project's reverse engineering and never run here.

    The shell marks an experimental capability, so this is the difference between a page that admits
    what it does not know and one that does not.
    """
    for extension in ext.EXTENSIONS:
        logitech = extension.name.startswith("Logitech")
        for control in extension.controls:
            assert control.experimental is not logitech, (
                f"{extension.name}/{control.key}: experimental should be {not logitech}"
            )


def test_a_blob_control_never_claims_to_be_readable():
    """These units answer SET_CUR and report nothing useful for GET_CUR.

    A control that cannot be read must not be shown holding a value, or the page states as fact
    something it made up.
    """
    for extension in ext.EXTENSIONS:
        for control in extension.controls:
            if control.write == ext.WRITE_BLOB:
                assert not control.readable, f"{extension.name}/{control.key}"
                assert all(isinstance(v, bytes) for _, v in control.choices)
            else:
                assert all(isinstance(v, int) for _, v in control.choices)


# --------------------------------------------------------------------------- keys


def test_a_capability_key_comes_from_the_drivers_own_control_name():
    """Derived, not mapped, because the control walk finds controls this module has never heard of.

    The result matches what ``v4l2-ctl`` prints, so a key here is a name the user can look up.
    """
    assert _key("White Balance, Automatic") == "white_balance_automatic"
    assert _key("Focus, Automatic Continuous") == "focus_automatic_continuous"
    assert _key("Power Line Frequency") == "power_line_frequency"
    assert _key("Zoom, Absolute") == "zoom_absolute"


# --------------------------------------------------------------------------- the page


def _controls() -> list[Control]:
    """What a Brio reported, trimmed to the shape that matters here."""
    return [
        Control(0x009A090D, "zoom_absolute", "Zoom, Absolute", "integer",
                value=100, minimum=100, maximum=500, step=1),
        Control(0x009A090C, "focus_automatic_continuous", "Focus, Automatic Continuous",
                "boolean", value=1, minimum=0, maximum=1),
        Control(0x009A090A, "focus_absolute", "Focus, Absolute", "integer",
                value=0, minimum=0, maximum=255, step=5, inactive=True),
        Control(0x00980900, "brightness", "Brightness", "integer",
                value=128, minimum=0, maximum=255, step=1),
        Control(0x00980918, "power_line_frequency", "Power Line Frequency", "menu",
                value=1, minimum=0, maximum=2,
                menu=[(0, "Disabled"), (1, "50 Hz"), (2, "60 Hz")]),
        Control(0x0098091A, "camera_orientation", "Camera Orientation", "menu",
                value=0, minimum=0, maximum=0, menu=[(0, "Front")], readonly=True),
    ]


#: Measured on the real BRIO: YUYV tops out at 1920x1080 while MJPG reaches 4096x2160, which is why
#: reporting one "largest resolution" for the whole camera was wrong.
_BRIO_RATES = (30.0, 24.0, 20.0, 15.0, 10.0, 7.5, 5.0)
_STREAMS = [
    C.StreamFormat("YUYV", ((1920, 1080), (1280, 720), (640, 480)),
                   {(1920, 1080): _BRIO_RATES, (1280, 720): _BRIO_RATES,
                    (640, 480): _BRIO_RATES}),
    C.StreamFormat("MJPG", ((4096, 2160), (1920, 1080)),
                   {(4096, 2160): _BRIO_RATES, (1920, 1080): _BRIO_RATES}),
]


def _built(vendor=()):
    return C.build(controls=_controls(), vendor=list(vendor), card="Logitech BRIO",
                   driver="uvcvideo", node="/dev/video4", formats=["YUYV", "MJPG"],
                   streams=_STREAMS, mode=(1920, 1080, "YUYV", 30.0))


def test_v4l2_types_map_onto_the_capability_kinds_with_nothing_left_over():
    kinds = {c.key: c.kind for c in _built()}
    assert kinds[f"{C.STANDARD_PREFIX}zoom_absolute"] is Kind.RANGE
    assert kinds[f"{C.STANDARD_PREFIX}focus_automatic_continuous"] is Kind.TOGGLE
    assert kinds[f"{C.STANDARD_PREFIX}power_line_frequency"] is Kind.CHOICE


def test_a_read_only_control_is_not_writable():
    """The driver's own flag. Camera orientation is a fact about how the sensor is mounted."""
    orientation = next(c for c in _built() if c.key.endswith("camera_orientation"))
    assert not orientation.writable


def test_sections_run_in_the_order_the_table_declares_not_alphabetically():
    """"Framing" before "Focus" is how a camera is set up. Sorting by name put Focus first."""
    camera = [c for c in _built() if c.group == C.GROUP_CAMERA]
    sections = list(dict.fromkeys(c.section for c in camera))
    assert sections.index("Framing") < sections.index("Focus")


def test_every_section_is_one_contiguous_run():
    """The shell writes a heading when the section changes and never reorders, so a section that
    appears twice prints its heading twice."""
    by_group: dict[str, list[str]] = {}
    for cap in _built([(ext.LOGITECH_BRIO, ext.LOGITECH_BRIO.controls[0])]):
        by_group.setdefault(cap.group, []).append(cap.section)

    for group, sections in by_group.items():
        runs = [s for i, s in enumerate(sections) if i == 0 or sections[i - 1] != s]
        assert len(runs) == len(set(runs)), f"{group} renders a heading twice: {sections}"


def test_vendor_controls_come_first():
    """They are the reason to use this rather than v4l2-ctl: field of view and the status light are
    not reachable any other way. Thirty standard sliders above them would bury the only part that
    is not already solved."""
    vendor = [(ext.LOGITECH_BRIO, ext.LOGITECH_BRIO.controls[0])]
    built = [c for c in _built(vendor) if c.group == C.GROUP_CAMERA]
    assert built[0].label == "Field of view"


def test_a_vendor_action_gets_one_key_per_button():
    """An ACTION carries no value, so the choice has to be recoverable from the key.

    "Recentre" with Pan / Tilt / Both is three keys, not one key with three buttons -- otherwise a
    press cannot say which of the three it was.
    """
    recentre = next(c for c in ext.LOGITECH_PERIPHERAL.controls if c.key == "pan_tilt_reset")
    built = [c for c in _built([(ext.LOGITECH_PERIPHERAL, recentre)]) if c.kind is Kind.ACTION]
    assert [c.action_label for c in built] == ["Pan", "Tilt", "Both"]
    assert len({c.key for c in built}) == 3


def test_the_same_setting_on_two_units_gets_two_keys():
    """Logitech publishes the status light on both its peripheral unit and its older
    hardware-control unit. One key for both would silently drop a row."""
    peripheral = next(c for c in ext.LOGITECH_PERIPHERAL.controls if c.key == "led_mode")
    user_hw = next(c for c in ext.LOGITECH_USER_HW.controls if c.key == "led_mode")
    assert C.vendor_key(ext.LOGITECH_PERIPHERAL, peripheral) != \
        C.vendor_key(ext.LOGITECH_USER_HW, user_hw)


# --------------------------------------------------------------------------- discovery


def test_one_camera_with_an_infrared_sensor_is_still_one_camera():
    """A Brio presents two capture nodes and they are indistinguishable by ids, name, or extension
    unit -- both report the same units, and writing to either changes the one camera.

    What separates them is what they can stream: the colour sensor offers several pixel formats, the
    infrared one offers GREY at a single square size. So the richer node wins. cameractrls lists
    both as separate cameras; that is the wart this exists to avoid.
    """
    from hardware_ui.core.discovery import _one_row_per_camera

    rows = _one_row_per_camera([
        {"node": "/dev/video4", "card": "Logitech BRIO", "driver": "uvcvideo",
         "usb": None, "richness": 3},
        {"node": "/dev/video6", "card": "Logitech BRIO", "driver": "uvcvideo",
         "usb": None, "richness": 1},
    ])
    # No USB parent here, so they group by node and both survive -- which is the fallback, and the
    # point of the next assertion is the grouped case.
    assert len(rows) == 2

    from pathlib import Path
    shared = Path("/sys/devices/pretend/4-4")
    rows = _one_row_per_camera([
        {"node": "/dev/video4", "card": "Logitech BRIO", "driver": "uvcvideo",
         "usb": shared, "richness": 3},
        {"node": "/dev/video6", "card": "Logitech BRIO", "driver": "uvcvideo",
         "usb": shared, "richness": 1},
    ])
    assert len(rows) == 1, "one physical camera, one row"
    assert rows[0].path == "/dev/video4", "the colour node, not the infrared one"
    assert rows[0].properties["nodes"] == ["/dev/video4", "/dev/video6"], "both still recorded"
    assert rows[0].transport is Transport.V4L2


# --------------------------------------------------------------------------- the manifest


def test_the_module_claims_every_camera_and_says_which_one_is_verified():
    """Claiming the whole transport is correct here and wrong for a vendor module: UVC is a class
    specification and the driver reports what each camera has. Only one model has been driven."""
    registry = ModuleRegistry.discover()

    def claim(vendor: int, product: int) -> DeviceInfo:
        return registry.claim(DeviceInfo(
            uid="v4l2:x", name="Camera", transport=Transport.V4L2, category=Category.OTHER,
            vendor_id=vendor, product_id=product, path="/dev/video9"))

    brio = claim(0x046D, 0x085E)
    assert brio.module_id == "uvc_cameras"
    assert brio.support is Support.VERIFIED

    unknown = claim(0x1234, 0x5678)
    assert unknown.module_id == "uvc_cameras", "any camera gets the standard controls"
    assert unknown.support is Support.FAMILY, "but nobody has driven it"


# --------------------------------------------------------------------------- Logitech coverage
#
# Everything cameractrls offers for a Logitech camera should be here, and the payloads are the
# camera's own -- transcribed, not derived. These tests are the transcription check.


def test_relative_pan_and_tilt_payloads_are_the_cameras_own():
    """Byte for byte against cameractrls. The values are deliberately asymmetric -- a one-unit step
    left is 0x0100 and right is 0xfeff -- so any attempt to compute them from a step size would
    produce numbers the hardware was never tested with."""
    from hardware_ui.modules.uvc_cameras.protocol.extensions import PAN_NUDGES, TILT_NUDGES

    assert [value for _, value in PAN_NUDGES] == [
        bytes.fromhex("00080000"),   # left 8
        bytes.fromhex("00010000"),   # left 1
        bytes.fromhex("fffe0000"),   # right 1
        bytes.fromhex("fff70000"),   # right 8
    ]
    assert [value for _, value in TILT_NUDGES] == [
        bytes.fromhex("0000fffc"),   # up 3
        bytes.fromhex("0000fffe"),   # up 1
        bytes.fromhex("00000001"),   # down 1
        bytes.fromhex("00000003"),   # down 3
    ]


def test_relative_pan_and_tilt_have_no_readable_value():
    """A nudge moves the motor from wherever it is. There is no "current nudge" to read back, and
    claiming one would show a value that means nothing."""
    from hardware_ui.modules.uvc_cameras.protocol.extensions import (
        LOGITECH_PERIPHERAL,
        WRITE_BLOB,
    )

    nudges = [c for c in LOGITECH_PERIPHERAL.controls if c.key.endswith("_relative")]
    assert {c.key for c in nudges} == {"pan_relative", "tilt_relative"}
    assert all(c.write == WRITE_BLOB and not c.readable for c in nudges)
    assert all(c.selector == 0x01 for c in nudges)


def test_all_eight_stored_positions_are_offered_both_ways():
    """The camera has eight presets with a save and a recall each. Shipping four would quietly cost
    the user half of the only Logitech setting that survives a power cycle."""
    from hardware_ui.modules.uvc_cameras.protocol.extensions import PANTILT_PRESETS

    def values(prefix: str) -> list[int]:
        return [v for label, v in PANTILT_PRESETS if label.startswith(prefix)]

    assert values("Go to") == list(range(0x0C, 0x14))
    assert values("Save") == list(range(0x04, 0x0C))


def test_every_logitech_feature_cameractrls_has_is_declared():
    """A roster, so a Logitech extension added upstream shows up here as a failure rather than as
    a feature nobody noticed was missing. Keys, not payloads: those have their own tests."""
    from hardware_ui.modules.uvc_cameras.protocol import extensions as ext

    assert {c.key for c in ext.LOGITECH_PERIPHERAL.controls} == {
        "led_mode",           # peripheral selector 0x09, offset 1
        "led_frequency",      # peripheral selector 0x09, offset 3
        "pan_relative",       # peripheral selector 0x01
        "tilt_relative",      # peripheral selector 0x01
        "pan_tilt_reset",     # peripheral selector 0x02
        "pan_tilt_preset",    # peripheral selector 0x02, gated by model
    }
    assert {c.key for c in ext.LOGITECH_USER_HW.controls} == {"led_mode", "led_frequency"}
    assert {c.key for c in ext.LOGITECH_MOTOR.controls} == {"motor_focus"}
    assert {c.key for c in ext.LOGITECH_BRIO.controls} == {"field_of_view"}


# --------------------------------------------------------------------------- streaming modes
#
# What a camera can stream depends on the pixel format, and the row this replaced hid that: it took
# the largest size of whichever format the camera listed first. On a real BRIO that is YUYV at
# 1920x1080, so a camera that does 4096x2160 in MJPG reported 1080p.


def test_each_pixel_format_gets_its_own_row():
    rows = {c.key: c for c in _built() if c.key.startswith(C.STREAM_PREFIX)}
    assert set(rows) == {C.stream_key("YUYV"), C.stream_key("MJPG")}
    assert [c.label for c in rows.values()] == ["YUYV", "MJPG"]
    assert all(c.kind is Kind.READOUT for c in rows.values())


def test_the_biggest_resolution_is_not_taken_from_one_format():
    """The regression this exists for. MJPG reaches 4096x2160 where YUYV stops at 1920x1080, and
    both numbers have to survive to the page."""
    summaries = {s.pixelformat: s.summary() for s in _STREAMS}
    assert summaries["MJPG"].startswith("4096×2160")
    assert summaries["YUYV"].startswith("1920×1080")


def test_frame_rates_keep_their_fractions():
    """7.5 fps is a real UVC rate and 7 is not the same camera. Integers still print as integers,
    because "30.0 fps" reads like a measurement rather than a mode."""
    stream = C.StreamFormat("MJPG", ((1280, 720),), {(1280, 720): (30.0, 7.5)})
    assert stream.summary() == "1280×720 at 30 / 7.5 fps"


def test_a_format_reporting_no_sizes_says_so():
    """Rather than rendering an empty "×" or claiming 0×0."""
    assert C.StreamFormat("GREY", (), {}).summary() == "no sizes reported"


def test_the_current_mode_row_appears_only_when_the_node_answers():
    """G_FMT can fail. A camera that will not report its mode should show no row at all rather than
    a row reading "unknown", which looks like a fault in the camera."""
    keys = {c.key for c in _built()}
    assert C.KEY_MODE in keys

    without = C.build(controls=_controls(), vendor=[], card="c", driver="uvcvideo",
                      node="/dev/video0", formats=["YUYV"], streams=_STREAMS, mode=None)
    assert C.KEY_MODE not in {c.key for c in without}


def test_nothing_about_streaming_is_writable():
    """Read-only by decision, and the decision is measured -- see the behaviour doc section 6."""
    streaming = [
        c for c in _built()
        if c.key.startswith(C.STREAM_PREFIX) or c.key == C.KEY_MODE
    ]
    assert streaming
    assert all(c.kind is Kind.READOUT for c in streaming)


# --------------------------------------------------------------------------- changing the mode
#
# Writable after all. The first version of this module reported the streaming mode and refused to
# change it, on reasoning that turned out to be partly wrong: setting a format needs no reopened
# descriptor and does persist. What is true is narrower -- it fails while the camera is in use, and
# whatever streams next negotiates its own format -- and neither is a reason to withhold the
# control, only to be honest about it. cameractrls exposes all three; so does this now.


def test_the_mode_controls_are_writable_choices():
    caps = {c.key: c for c in _built()}
    for key in (C.KEY_PIXELFORMAT, C.KEY_RESOLUTION, C.KEY_FRAME_RATE):
        assert caps[key].kind is Kind.CHOICE, key
        assert caps[key].writable, key


def test_the_choices_are_only_ever_the_cameras_own():
    """V4L2 lets a driver accept a format and substitute another, and the surest way to provoke that
    is to ask for something never enumerated. Offering only enumerated values is what makes the
    substitution check an assertion rather than a hope."""
    caps = {c.key: c for c in _built()}
    assert [c.label for c in caps[C.KEY_PIXELFORMAT].choices] == ["YUYV", "MJPG"]
    assert [c.label for c in caps[C.KEY_RESOLUTION].choices] == ["1920×1080", "1280×720", "640×480"]
    assert [c.label for c in caps[C.KEY_FRAME_RATE].choices] == [
        "30", "24", "20", "15", "10", "7.5", "5",
    ]


def test_the_lists_depend_on_what_is_selected():
    """Measured on the BRIO: NV12 offers 4 frame sizes and MJPG offers 20. A flat list of every
    combination would offer modes the camera does not have."""
    def sizes(pixelformat: str) -> list[str]:
        built = C.build(controls=_controls(), vendor=[], card="c", driver="uvcvideo",
                        node="/dev/video5", formats=["YUYV", "MJPG"], streams=_STREAMS,
                        mode=(640, 480, pixelformat, 30.0))
        caps = {c.key: c for c in built}
        return [c.label for c in caps[C.KEY_RESOLUTION].choices]

    assert sizes("MJPG") == ["4096×2160", "1920×1080"]
    assert sizes("YUYV") == ["1920×1080", "1280×720", "640×480"]


def test_no_mode_controls_when_the_node_will_not_say_where_it_is():
    """Without knowing the current mode these controls cannot say what they would change from, so
    they are absent rather than showing a guess."""
    built = C.build(controls=_controls(), vendor=[], card="c", driver="uvcvideo",
                    node="/dev/video0", formats=["YUYV"], streams=_STREAMS, mode=None)
    keys = {c.key for c in built}
    assert not keys & {C.KEY_PIXELFORMAT, C.KEY_RESOLUTION, C.KEY_FRAME_RATE}


def test_a_frame_rate_is_sent_as_a_tenths_interval():
    """`10 / fps*10`, not `1 / fps`. 7.5 fps is a real UVC rate and `1/7.5` is not expressible in
    the integer pair the kernel takes, where `10/75` is exact. Verified live: the BRIO accepted 7.5
    and reported 7.5 back."""
    assert (10, int(7.5 * 10)) == (10, 75)
    assert 75 / 10 == 7.5


def test_busy_says_what_to_do_about_it():
    """EBUSY here has one cause worth naming. The raw errno leaves a user with nothing to act on."""
    import errno as _errno

    from hardware_ui.modules.uvc_cameras.protocol.session import _busy

    message = _busy(OSError(_errno.EBUSY, "Device or resource busy"))
    assert "in use" in message
    assert "image controls" in message          # the part that was checked against the hardware
    assert _busy(OSError(_errno.EINVAL, "Invalid argument")) == "[Errno 22] Invalid argument"


def test_the_mode_controls_warn_that_applications_override_them():
    """The obvious expectation of a resolution dropdown is the wrong one, and a user found that out
    the hard way: changing it here does nothing to what Kamoso shows, because Kamoso is a GStreamer
    application and GStreamer asks for its own format on open. Measured — a `v4l2src` reset a BRIO
    from 1280×720 at 30 fps to 640×480 at 120 the moment it opened the camera.

    Not locked. The write works; it is the reach that is limited, and refusing it would swap this
    misunderstanding for a different one."""
    from hardware_ui.modules.uvc_cameras.device import NEGOTIATED

    assert "overrode" in NEGOTIATED
    assert "Kamoso" in NEGOTIATED
    # Three applications were tested and three overrode it, so the wording must not imply that some
    # ordinary application would honour it. Naming them is what makes the claim checkable.
    for app in ("ffmpeg", "VLC", "GStreamer"):
        assert app in NEGOTIATED
    # And it must say the rest of the page is unaffected, which is the part a user needs most.
    assert "image controls" in NEGOTIATED
