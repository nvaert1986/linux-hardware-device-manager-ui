"""Vendor controls that live in UVC extension units, as a table.

**The module is generic; this file is the only part that is not.** Every UVC camera gets the
standard controls it reports through :mod:`.session`, whatever the make. What is listed here is the
extra that specific models publish in a vendor extension unit, and a camera absent from this table
loses nothing it could otherwise have had -- there is simply no standard way to ask for a field of
view.

Two gates, and a control has to pass both:

**The unit must be present.** Its GUID is looked for in the camera's own USB descriptors. A camera
without it does not have the unit.

**The selector must answer.** ``GET_LEN`` is issued against it and a control that does not reply is
not offered. So a table entry is a *claim to check*, never an assumption: the peripheral unit below
lists pan/tilt, a Brio has that unit and does not answer for pan/tilt, and the rows quietly do not
appear.

Some entries additionally name **product ids**, and that is not belt-and-braces. Where a unit's
GUID is shared across vendors -- ``23e49ed0`` is used by both the Razer Kiyo Pro and the Dell
UltraSharp, and answers on a Logitech Brio too -- the payloads are model-specific, so sending one
model's bytes to another is writing an unknown value to an unknown control. The product id is what
stops that.

Provenance: the GUIDs, selectors, offsets and values are facts about the hardware, discovered by
``cameractrls`` (LGPL-3+, Gergo Koteles). The Logitech entries have been re-verified here against a
BRIO ``046d:085e``; everything else is carried from that project **unverified** and marked
``experimental``. See ``docs/UVC_CAMERAS_UI_BEHAVIOUR.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- shapes

#: How a vendor control is written. The two are not interchangeable and getting it wrong writes
#: rubbish to a real camera.
#:
#: ``byte``
#:     One byte at an offset inside a longer buffer, read-modify-written. The Logitech LED control
#:     is five bytes carrying *two* settings, so writing the whole buffer to change one would reset
#:     the other.
#: ``blob``
#:     A whole opaque byte string, sent as-is. Razer, Dell and AnkerWork controls are commands
#:     rather than fields, and there is no reliable read: their current value is unknown, so the
#:     page shows the control without claiming to know where it is set.
WRITE_BYTE = "byte"
WRITE_BLOB = "blob"


@dataclass(frozen=True)
class VendorControl:
    """One control inside an extension unit."""

    key: str
    name: str
    kind: str
    """``choice``, ``range`` or ``action``."""

    selector: int
    write: str = WRITE_BYTE
    offset: int = 0
    """Which byte of the control's buffer this setting occupies. ``WRITE_BYTE`` only."""

    choices: tuple[tuple[str, object], ...] = ()
    """``(label, value)``. An ``int`` for ``WRITE_BYTE``, ``bytes`` for ``WRITE_BLOB``."""

    product_ids: tuple[int, ...] = ()
    """Restrict to these models. Empty means any camera whose unit answers for the selector."""

    prelude: tuple[tuple[str, bytes], ...] = ()
    """``(choice label, bytes)`` sent immediately before the choice's own payload.

    The Kiyo Pro's field of view needs it: two of its three settings are a pair of writes, and
    sending only the second does nothing."""

    description: str = ""
    experimental: bool = False
    """Carried from another project's reverse engineering and never run here. The shell marks it."""

    readable: bool = True
    """False for a command with no meaningful current value -- every ``WRITE_BLOB`` control."""

    minimum: int = 0
    maximum: int = 255
    """Bounds for a ``range``, as placeholders. The real ones are read from the unit with ``GET_MIN``
    and ``GET_MAX`` at connect: a vendor control reports its own range just as a V4L2 control does,
    and hardcoding one here would be inventing a limit the camera never stated."""


@dataclass(frozen=True)
class Extension:
    """One extension unit: a GUID, and the controls it may carry."""

    guid: bytes
    name: str
    controls: tuple[VendorControl, ...] = field(default_factory=tuple)


def _guid(text: str) -> bytes:
    """A GUID as it appears in a USB descriptor.

    UVC writes the first three fields little-endian and the rest big-endian, which is Microsoft's
    layout and the reason a descriptor never contains the GUID in the order it is printed. Doing the
    conversion here means the table below can be read against ``lsusb -v`` output directly.
    """
    a, b, c, d, e = text.split("-")
    return (bytes.fromhex(a)[::-1] + bytes.fromhex(b)[::-1] + bytes.fromhex(c)[::-1]
            + bytes.fromhex(d) + bytes.fromhex(e))


# ---------------------------------------------------------------- Logitech
#
# Verified on a BRIO 046d:085e: peripheral unit 11, Brio unit 10.

LED_MODES = (("Off", 0x00), ("On", 0x01), ("Blink", 0x02), ("Automatic", 0x03))
LED_DESCRIPTION = (
    "Automatic is the camera's own behaviour: lit while it is streaming, dark when it is not. The "
    "other three override that whether the camera is in use or not, which is why Off does not mean "
    "the camera is off."
)

BRIO_FOV_PRODUCTS = (
    0x085E,  # BRIO
    0x0943,  # Brio 500
    0x0946,  # Brio 501
    0x0919,  # Brio 505
    0x086B,  # Brio 4K Stream Edition
    0x0944,  # MX Brio
)

PANTILT_PRESET_PRODUCTS = (
    0x0853,  # PTZ Pro
    0x0858,  # Group
    0x085F,  # PTZ Pro 2
    0x0866,  # MeetUp
    0x0881, 0x0888, 0x0889,  # Rally
)

#: Relative pan and tilt, as four-byte payloads: a signed pan word then a signed tilt word, both
#: little-endian. Transcribed from cameractrls verbatim rather than computed, because the values are
#: not symmetric -- a one-unit step one way is 0x0100 and the other way 0xfeff, and an eight-unit
#: pan step is 0x0800 against 0xf7ff. Deriving them from a step size would produce numbers this
#: hardware was never tested with.
PAN_NUDGES = (
    ("←← 8", bytes.fromhex("00080000")),
    ("← 1", bytes.fromhex("00010000")),
    ("1 →", bytes.fromhex("fffe0000")),
    ("8 →→", bytes.fromhex("fff70000")),
)

#: Tilt's large step is three units, not eight. That is what the source says and what the mechanism
#: allows; it is not a truncated copy of the pan table.
TILT_NUDGES = (
    ("↑↑ 3", bytes.fromhex("0000fffc")),
    ("↑ 1", bytes.fromhex("0000fffe")),
    ("1 ↓", bytes.fromhex("00000001")),
    ("3 ↓↓", bytes.fromhex("00000003")),
)

#: Eight stored positions, each with a save and a recall. All eight of both: the camera has them,
#: and offering four would quietly cost the user half the feature.
PANTILT_PRESETS = tuple(
    [(f"Go to {n}", 0x0C + n - 1) for n in range(1, 9)]
    + [(f"Save {n}", 0x04 + n - 1) for n in range(1, 9)]
)

MOTOR_FOCUS_PRODUCTS = (
    0x0809,  # Webcam Pro 9000
    0x0990,  # QuickCam Pro 9000
    0x0991,  # QuickCam Pro for Notebooks
    0x0994,  # QuickCam Orbit/Sphere AF
)

LOGITECH_PERIPHERAL = Extension(
    guid=_guid("ffe52d21-8030-4e2c-82d9-f587d00540bd"),
    name="Logitech peripheral",
    controls=(
        VendorControl(
            key="led_mode", name="Status light", kind="choice",
            selector=0x09, offset=1, choices=LED_MODES, description=LED_DESCRIPTION,
        ),
        VendorControl(
            key="led_frequency", name="Status light blink rate", kind="range",
            selector=0x09, offset=3,
            description=(
                "In units of 0.05 Hz, and it only does anything while the light is set to Blink."
            ),
        ),
        # Mechanical pan and tilt. Present on the conference cameras and *not* on a Brio, whose
        # pan and tilt are digital and therefore standard V4L2 controls. The selector simply does
        # not answer there, so these rows do not appear.
        VendorControl(
            key="pan_relative", name="Nudge left / right", kind="action",
            selector=0x01, write=WRITE_BLOB, readable=False,
            choices=PAN_NUDGES,
            description=(
                "Moves the motor by a step from wherever it is now, so there is no current value "
                "to show — only the four steps. Larger steps are not multiples of the small one; "
                "the values are the camera's own."
            ),
        ),
        VendorControl(
            key="tilt_relative", name="Nudge up / down", kind="action",
            selector=0x01, write=WRITE_BLOB, readable=False,
            choices=TILT_NUDGES,
            description=(
                "As with pan, a step from the present position. Tilt offers a smaller large step "
                "than pan does, which is the camera's range and not a transcription slip."
            ),
        ),
        VendorControl(
            key="pan_tilt_reset", name="Recentre", kind="action",
            selector=0x02, offset=0,
            choices=(("Pan", 0x01), ("Tilt", 0x02), ("Both", 0x03)),
            description="Drives the motor back to its centre position.",
        ),
        VendorControl(
            key="pan_tilt_preset", name="Stored position", kind="action",
            selector=0x02, offset=0, product_ids=PANTILT_PRESET_PRODUCTS,
            choices=PANTILT_PRESETS,
            description=(
                "The camera's own stored pan and tilt positions — the one Logitech feature here "
                "that survives a power cycle, because the camera keeps them itself."
            ),
        ),
    ),
)

LOGITECH_BRIO = Extension(
    guid=_guid("49e40215-f434-47fe-b158-0e885023e51b"),
    name="Logitech Brio",
    controls=(
        VendorControl(
            key="field_of_view", name="Field of view", kind="choice",
            selector=0x05, offset=0,
            choices=(("90°", 0x00), ("78°", 0x01), ("65°", 0x02)),
            product_ids=BRIO_FOV_PRODUCTS,
            description=(
                "How wide a picture the camera sends. Narrower crops into the sensor rather than "
                "moving anything, so it costs field of view and not detail."
            ),
        ),
    ),
)

LOGITECH_USER_HW = Extension(
    guid=_guid("63610682-5070-49ab-b8cc-b3855e8d221f"),
    name="Logitech hardware control",
    controls=(
        VendorControl(
            key="led_mode", name="Status light", kind="choice",
            selector=0x01, offset=0, choices=LED_MODES, description=LED_DESCRIPTION,
        ),
        VendorControl(
            key="led_frequency", name="Status light blink rate", kind="range",
            selector=0x01, offset=2,
            description="In units of 0.05 Hz, and only while the light is set to Blink.",
        ),
    ),
)

LOGITECH_MOTOR = Extension(
    guid=_guid("63610682-5070-49ab-b8cc-b3855e8d2256"),
    name="Logitech motor control",
    controls=(
        VendorControl(
            key="motor_focus", name="Focus", kind="range",
            selector=0x03, offset=0, product_ids=MOTOR_FOCUS_PRODUCTS,
            description=(
                "Mechanical focus, in 256 steps with 0 at infinity and 255 at macro. There are no "
                "physical units."
            ),
        ),
    ),
)

# ---------------------------------------------------------------- other vendors
#
# All unverified: no such camera has been attached to this project. Marked experimental so the shell
# says so, and gated on product id because two of them share a GUID.

KIYO_PRO_PRODUCTS = (0x0E05,)
DELL_ULTRASHARP_PRODUCTS = (0xC015,)

RAZER_KIYO_PRO = Extension(
    guid=_guid("23e49ed0-1178-4f31-ae52-d2fb8a8d3b48"),
    name="Razer Kiyo Pro",
    controls=(
        VendorControl(
            key="autofocus_mode", name="Autofocus", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=KIYO_PRO_PRODUCTS, experimental=True,
            choices=(("Passive", bytes.fromhex("ff06010000000000")),
                     ("Responsive", bytes.fromhex("ff06000000000000"))),
        ),
        VendorControl(
            key="hdr", name="HDR", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=KIYO_PRO_PRODUCTS, experimental=True,
            choices=(("Off", bytes.fromhex("ff02000000000000")),
                     ("On", bytes.fromhex("ff02010000000000"))),
        ),
        VendorControl(
            key="hdr_mode", name="HDR bias", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=KIYO_PRO_PRODUCTS, experimental=True,
            choices=(("Dark", bytes.fromhex("ff07000000000000")),
                     ("Bright", bytes.fromhex("ff07010000000000"))),
        ),
        VendorControl(
            key="field_of_view", name="Field of view", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=KIYO_PRO_PRODUCTS, experimental=True,
            choices=(("Wide", bytes.fromhex("ff01000300000000")),
                     ("Medium", bytes.fromhex("ff01010301000000")),
                     ("Narrow", bytes.fromhex("ff01010302000000"))),
            prelude=(("Medium", bytes.fromhex("ff01000301000000")),
                     ("Narrow", bytes.fromhex("ff01000302000000"))),
        ),
        # The one camera here that can genuinely store its settings.
        VendorControl(
            key="save", name="Settings", kind="action",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=KIYO_PRO_PRODUCTS, experimental=True,
            choices=(("Save to the camera", bytes.fromhex("c003a80000000000")),),
            description=(
                "Writes the current settings into the camera's own memory, so they survive being "
                "unplugged. Most cameras cannot do this."
            ),
        ),
    ),
)

DELL_ULTRASHARP = Extension(
    guid=_guid("23e49ed0-1178-4f31-ae52-d2fb8a8d3b48"),
    name="Dell UltraSharp webcam",
    controls=(
        VendorControl(
            key="auto_framing", name="Auto framing", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=DELL_ULTRASHARP_PRODUCTS, experimental=True,
            choices=(("Off", bytes.fromhex("ff14010000000000")),
                     ("On", bytes.fromhex("ff14010100000000"))),
            description="Tracks a face and pans and zooms to keep it framed.",
        ),
        VendorControl(
            key="camera_transition", name="Smooth transitions", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=DELL_ULTRASHARP_PRODUCTS, experimental=True,
            choices=(("Off", bytes.fromhex("ff14100000000000")),
                     ("On", bytes.fromhex("ff14100100000000"))),
        ),
        VendorControl(
            key="tracking_sensitivity", name="Tracking speed", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=DELL_ULTRASHARP_PRODUCTS, experimental=True,
            choices=(("Normal", bytes.fromhex("ff14110100000000")),
                     ("Fast", bytes.fromhex("ff14110200000000"))),
        ),
        VendorControl(
            key="tracking_frame_size", name="Tracking frame", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=DELL_ULTRASHARP_PRODUCTS, experimental=True,
            choices=(("Standard", bytes.fromhex("ff14120100000000")),
                     ("Narrow", bytes.fromhex("ff14120200000000"))),
        ),
        VendorControl(
            key="field_of_view", name="Field of view", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=DELL_ULTRASHARP_PRODUCTS, experimental=True,
            choices=(("65°", bytes.fromhex("ff10014100000000")),
                     ("78°", bytes.fromhex("ff10014e00000000")),
                     ("90°", bytes.fromhex("ff10015a00000000"))),
        ),
        VendorControl(
            key="hdr", name="HDR", kind="choice",
            selector=0x01, write=WRITE_BLOB, readable=False,
            product_ids=DELL_ULTRASHARP_PRODUCTS, experimental=True,
            choices=(("Off", bytes.fromhex("ff11000000000000")),
                     ("On", bytes.fromhex("ff11010000000000"))),
        ),
    ),
)

ANKERWORK_PRODUCTS = (0x3367,)

ANKERWORK = Extension(
    guid=_guid("41769ea2-04de-e347-8b2b-f4341aff003b"),
    name="AnkerWork",
    controls=(
        VendorControl(
            key="field_of_view", name="Field of view", kind="choice",
            selector=0x10, write=WRITE_BLOB, readable=False,
            product_ids=ANKERWORK_PRODUCTS, experimental=True,
            choices=(("65°", bytes.fromhex("00015f00000000")),
                     ("78°", bytes.fromhex("00014e00000000")),
                     ("95°", bytes.fromhex("00014100000000")),
                     ("Auto framing", bytes.fromhex("02015f00000000"))),
        ),
        VendorControl(
            key="face_focus", name="Face focus", kind="choice",
            selector=0x1B, write=WRITE_BLOB, readable=False,
            product_ids=ANKERWORK_PRODUCTS, experimental=True,
            choices=(("Off", b"\x00"), ("On", b"\x01")),
        ),
        VendorControl(
            key="microphone_noise_reduction", name="Microphone noise reduction", kind="choice",
            selector=0x1D, write=WRITE_BLOB, readable=False,
            product_ids=ANKERWORK_PRODUCTS, experimental=True,
            choices=(("Off", b"\x00"), ("On", b"\x01")),
        ),
        VendorControl(
            key="microphone_pickup", name="Microphone pickup pattern", kind="choice",
            selector=0x1F, write=WRITE_BLOB, readable=False,
            product_ids=ANKERWORK_PRODUCTS, experimental=True,
            choices=(("360°", b"\x00\x00"), ("90°", b"\x5a\x00")),
        ),
    ),
)

#: Every extension unit this module knows, in the order they are offered.
#:
#: Logitech first because those are the verified ones. The two entries sharing GUID ``23e49ed0``
#: are both listed: they are told apart by product id, and a camera matching neither gets neither.
EXTENSIONS: tuple[Extension, ...] = (
    LOGITECH_PERIPHERAL,
    LOGITECH_BRIO,
    LOGITECH_USER_HW,
    LOGITECH_MOTOR,
    RAZER_KIYO_PRO,
    DELL_ULTRASHARP,
    ANKERWORK,
)


def for_product(product_id: int) -> tuple[Extension, ...]:
    """The units worth looking for on this model, with controls it is not gated out of.

    Filtering here rather than at probe time keeps the "does this camera have it" question in one
    place, and means a Brio never even asks the Kiyo Pro's selector whether it exists.
    """
    out = []
    for extension in EXTENSIONS:
        controls = tuple(
            control for control in extension.controls
            if not control.product_ids or product_id in control.product_ids
        )
        if controls:
            out.append(Extension(guid=extension.guid, name=extension.name, controls=controls))
    return tuple(out)


__all__ = ["EXTENSIONS", "Extension", "VendorControl", "WRITE_BLOB", "WRITE_BYTE", "for_product"]
