"""What a camera offers, built from what the camera reported.

**No per-model table for the standard controls, and none is possible.** The device is asked what it
has: :meth:`Session.controls` walks the driver's own list and each entry arrives with its type,
range, default, menu items and flags. So a camera nobody has ever seen produces a correct page, and
a camera missing a control simply has no row for it -- which is why this module claims every UVC
capture device rather than a list of models.

The vendor extras in :mod:`.protocol.extensions` are the exception, and they are additive: a camera
with none is a camera with a shorter page, not a broken one.

Grouping and ordering are ours. V4L2 hands over controls in numeric id order, which puts brightness
next to exposure and white balance three rows from saturation; the tabs below follow what somebody
adjusting a camera is actually doing.
"""

from __future__ import annotations

from dataclasses import dataclass

from hardware_ui.core.capability import Capability, Choice, Kind, Tier

from .protocol import extensions as ext
from .protocol.session import Control, VendorControl

STANDARD_PREFIX = "v4l2."
VENDOR_PREFIX = "vendor."

KEY_CARD = "info.card"
KEY_DRIVER = "info.driver"
KEY_NODE = "info.node"
KEY_MODE = "info.mode"
KEY_PIXELFORMAT = "stream.pixelformat"
KEY_RESOLUTION = "stream.resolution"
KEY_FRAME_RATE = "stream.frame_rate"


@dataclass(frozen=True, slots=True)
class StreamFormat:
    """Everything one pixel format can do: every frame size, and the rates available at each.

    Enumerated in full rather than sampled. Measured on the two cameras here that is 43 and 12
    size/format pairs, taking 0.4 ms and 0.1 ms -- these are local ioctls on a character device, so
    there is nothing to save by being clever, and the resolution dropdown needs the whole list
    anyway.
    """

    pixelformat: str
    sizes: tuple[tuple[int, int], ...]
    """Largest first."""

    rates: dict[tuple[int, int], tuple[float, ...]]
    """Frame rates per size, fastest first. Keyed by ``(width, height)``."""

    @property
    def largest(self) -> tuple[int, int]:
        return self.sizes[0] if self.sizes else (0, 0)

    @property
    def all_sizes(self) -> tuple[tuple[int, int], ...]:
        return self.sizes

    def rates_at(self, width: int, height: int) -> tuple[float, ...]:
        return self.rates.get((width, height), ())

    def summary(self) -> str:
        """The top of this format, which is what a user compares between formats."""
        width, height = self.largest
        if not width:
            return "no sizes reported"
        size = f"{width}×{height}"
        rates = " / ".join(_fps(r) for r in self.rates_at(width, height))
        return f"{size} at {rates} fps" if rates else size


def _fps(rate: float) -> str:
    """``30.0`` -> ``"30"``, ``7.5`` -> ``"7.5"``. Frame rates are not all integers."""
    return f"{rate:g}"
#: One row per pixel format, keyed by the format itself: ``info.stream.MJPG``. Built from what the
#: camera reports rather than declared, like the vendor rows, because which formats exist is the
#: camera's business.
STREAM_PREFIX = "info.stream."


def stream_key(pixelformat: str) -> str:
    return f"{STREAM_PREFIX}{pixelformat}"

GROUP_CAMERA = "Camera"
GROUP_EXPOSURE = "Exposure"
GROUP_COLOUR = "Colour"
GROUP_ADVANCED = "Advanced"
GROUP_INFO = "Information"

#: Which tab and heading each standard control belongs on, by the key derived from its own name.
#:
#: Keys are the driver's control names lowercased -- the same strings ``v4l2-ctl`` prints -- so this
#: table is readable against any camera's output. A control absent from it lands under Advanced /
#: Other rather than being dropped: the walk finds controls this module has never heard of, and
#: hiding one because it is unfamiliar is the opposite of asking the device what it has.
PLACEMENT: dict[str, tuple[str, str]] = {
    "zoom_absolute": (GROUP_CAMERA, "Framing"),
    "pan_absolute": (GROUP_CAMERA, "Framing"),
    "tilt_absolute": (GROUP_CAMERA, "Framing"),
    "pan_speed": (GROUP_CAMERA, "Framing"),
    "tilt_speed": (GROUP_CAMERA, "Framing"),
    "focus_automatic_continuous": (GROUP_CAMERA, "Focus"),
    "focus_absolute": (GROUP_CAMERA, "Focus"),
    "auto_exposure": (GROUP_EXPOSURE, "Exposure"),
    "exposure_time_absolute": (GROUP_EXPOSURE, "Exposure"),
    "exposure_dynamic_framerate": (GROUP_EXPOSURE, "Exposure"),
    "gain": (GROUP_EXPOSURE, "Exposure"),
    "backlight_compensation": (GROUP_EXPOSURE, "Dynamic range"),
    "wide_dynamic_range": (GROUP_EXPOSURE, "Dynamic range"),
    "white_balance_automatic": (GROUP_COLOUR, "White balance"),
    "white_balance_temperature": (GROUP_COLOUR, "White balance"),
    "brightness": (GROUP_COLOUR, "Picture"),
    "contrast": (GROUP_COLOUR, "Picture"),
    "saturation": (GROUP_COLOUR, "Picture"),
    "sharpness": (GROUP_COLOUR, "Picture"),
    "gamma": (GROUP_COLOUR, "Picture"),
    "hue": (GROUP_COLOUR, "Picture"),
    "power_line_frequency": (GROUP_ADVANCED, "Flicker"),
    "privacy": (GROUP_ADVANCED, "Privacy"),
    "horizontal_flip": (GROUP_ADVANCED, "Orientation"),
    "vertical_flip": (GROUP_ADVANCED, "Orientation"),
    "rotate": (GROUP_ADVANCED, "Orientation"),
}

#: Shown by default rather than behind "All settings". The handful somebody actually reaches for.
COMMON = frozenset({
    "zoom_absolute", "focus_automatic_continuous", "auto_exposure",
    "white_balance_automatic", "brightness", "power_line_frequency",
})

#: Wording the driver does not supply. Only where the control's own name is genuinely unclear --
#: restating "Brightness" as "brightness" would be noise.
NOTES: dict[str, str] = {
    "exposure_dynamic_framerate": (
        "Lets the camera drop the frame rate to expose for longer in poor light. Off keeps the "
        "frame rate constant and accepts a darker picture."
    ),
    "backlight_compensation": (
        "Exposes for the subject rather than the background, for someone sitting in front of a "
        "window."
    ),
    "power_line_frequency": (
        "Matches the camera's exposure to the mains frequency so lighting does not appear to "
        "flicker. 50 Hz in Europe, 60 Hz in North America."
    ),
    "zoom_absolute": "Digital: it crops into the sensor rather than moving a lens.",
    "sharpness": (
        "An edge filter, not focus. Raising it cannot recover detail the lens did not resolve."
    ),
}


def standard_key(control: Control) -> str:
    return f"{STANDARD_PREFIX}{control.key}"


def vendor_key(extension: ext.Extension, control: ext.VendorControl) -> str:
    """Namespaced by the unit, because two units can carry the same setting.

    Logitech publishes the status light on both its peripheral unit and its older hardware-control
    unit. A camera with both would otherwise produce two rows with one key, and the second would
    silently win.
    """
    return f"{VENDOR_PREFIX}{extension.guid[:4].hex()}.{control.key}"


def build(*, controls: list[Control], vendor: list[tuple[ext.Extension, VendorControl]],
          card: str, driver: str, node: str, formats: list[str],
          streams: list[StreamFormat], mode: tuple[int, int, str, float] | None = None,
          ) -> list[Capability]:
    """The page for one camera."""
    out: list[Capability] = []
    out.extend(_vendor_rows(vendor))
    out.extend(_standard_rows(controls))
    out.extend(_information(card, driver, node, formats, streams, mode))
    return out


def _vendor_rows(vendor: list[tuple[ext.Extension, VendorControl]]) -> list[Capability]:
    """Vendor controls first, and deliberately.

    They are the reason to use this application rather than ``v4l2-ctl``: field of view and the
    status light are not reachable any other way. Burying them under thirty standard sliders would
    hide the only part that is not already solved.
    """
    out: list[Capability] = []
    for extension, control in vendor:
        key = vendor_key(extension, control)
        section = extension.name
        common = dict(
            key=key, label=control.name, group=GROUP_CAMERA, section=section,
            tier=Tier.COMMON, description=control.description,
            experimental=control.experimental,
        )
        if control.kind == "choice":
            out.append(Capability(
                kind=Kind.CHOICE,
                choices=tuple(Choice(label, label) for label, _ in control.choices),
                **common,
            ))
        elif control.kind == "range":
            out.append(Capability(
                kind=Kind.RANGE,
                minimum=control.minimum, maximum=max(control.maximum, control.minimum + 1),
                step=1, **common,
            ))
        else:
            # One action per choice: "Recentre" with Pan / Tilt / Both is three buttons, and a
            # dropdown you then have to press something else to apply is a worse fit for a command.
            for label, _ in control.choices:
                out.append(Capability(**{
                    **common,
                    "key": f"{key}.{label.lower().replace(' ', '_')}",
                    "label": control.name,
                    "kind": Kind.ACTION,
                    "action_label": label,
                }))
    return out


def _standard_rows(controls: list[Control]) -> list[Capability]:
    """Standard controls, grouped and ordered by :data:`PLACEMENT`.

    Sorted into the table's order rather than the driver's, and anything unrecognised is appended
    under Advanced / Other. Contiguity matters: the shell renders a heading when the section changes
    and does not reorder, so two runs of one section would print the heading twice.
    """
    known = {key: index for index, key in enumerate(PLACEMENT)}
    # Sections in the order PLACEMENT declares them, not alphabetically: "Framing" before "Focus"
    # is how somebody sets a camera up, and sorting by name put Focus first.
    sections = list(dict.fromkeys(section for _group, section in PLACEMENT.values()))

    def order(control: Control) -> tuple:
        group, section = PLACEMENT.get(control.key, (GROUP_ADVANCED, "Other"))
        return (
            list(GROUPS).index(group),
            sections.index(section) if section in sections else len(sections),
            known.get(control.key, len(known)),
            control.key,
        )

    ordered = sorted(controls, key=order)
    out: list[Capability] = []
    for control in ordered:
        group, section = PLACEMENT.get(control.key, (GROUP_ADVANCED, "Other"))
        common = dict(
            key=standard_key(control), label=control.name, group=group, section=section,
            tier=Tier.COMMON if control.key in COMMON else Tier.ALL,
            description=NOTES.get(control.key, ""),
            # The driver's own flag, not a guess. A read-only control is one the camera reports and
            # will not change -- camera orientation, autofocus status.
            writable=not control.readonly,
        )
        if control.kind == "boolean":
            out.append(Capability(kind=Kind.TOGGLE, **common))
        elif control.kind == "menu":
            out.append(Capability(
                kind=Kind.CHOICE,
                choices=tuple(Choice(value, label) for value, label in control.menu),
                **common,
            ))
        elif control.kind == "button":
            out.append(Capability(kind=Kind.ACTION, action_label=control.name, **common))
        else:
            out.append(Capability(
                kind=Kind.RANGE,
                minimum=control.minimum,
                # A control whose range is a single value cannot be a slider; the schema refuses it.
                maximum=max(control.maximum, control.minimum + 1),
                step=control.step, **common,
            ))
    return out


def _information(card: str, driver: str, node: str, formats: list[str],
                 streams: list[StreamFormat],
                 mode: tuple[int, int, str, float] | None) -> list[Capability]:
    """What the camera is, and what it can stream.

    Read-only, and the streaming rows are read-only **by decision** -- see
    ``docs/UVC_CAMERAS_UI_BEHAVIOUR.md`` section 6 for the measurements behind that.

    **One row per pixel format, not one "largest resolution".** The single row this replaced was
    worse than terse, it was wrong: it measured only the *first* format the camera listed, so a
    BRIO that does 4096x2160 in MJPG reported 1920x1080 because YUYV happened to come first. The
    resolution a camera can reach and the rate it can sustain both depend on the format, and on an
    integrated webcam here the difference is 1920x1080 at 30 fps compressed against 5 fps
    uncompressed -- which is the whole answer to "why is my webcam so slow".
    """
    rows = [
        Capability(key=KEY_CARD, kind=Kind.READOUT, label="Camera",
                   group=GROUP_INFO, section="Identity"),
        Capability(key=KEY_DRIVER, kind=Kind.READOUT, label="Driver",
                   group=GROUP_INFO, section="Identity"),
        Capability(key=KEY_NODE, kind=Kind.READOUT, label="Device", copyable=True,
                   group=GROUP_INFO, section="Identity"),
    ]
    # No "Pixel formats" readout: it sat directly above a "Pixel format" dropdown listing the same
    # values, and two rows a letter apart in the same section is a wart. The dropdown carries the
    # list, and the Modes section below says what each format can do.
    rows.extend(_mode_rows(mode, streams))
    if mode is not None:
        rows.append(Capability(
            key=KEY_MODE, kind=Kind.READOUT, label="In use now",
            group=GROUP_INFO, section="Streaming",
            description=(
                "What the node is set to at this moment, which is whatever last opened the camera "
                "rather than a preference. Reading it needs no exclusive access, so it is safe to "
                "show while something is streaming."
            ),
        ))
    rows.extend(
        Capability(
            key=stream_key(stream.pixelformat), kind=Kind.READOUT, label=stream.pixelformat,
            group=GROUP_INFO, section="Modes",
            description=(
                f"{len(stream.sizes)} frame size"
                f"{'' if len(stream.sizes) == 1 else 's'} in this format. "
                "The rates shown are those available at the largest of them; smaller sizes usually "
                "allow more."
            ),
        )
        for stream in streams
    )
    return rows


def _mode_rows(mode: tuple[int, int, str, float] | None,
               streams: list[StreamFormat]) -> list[Capability]:
    """Pixel format, resolution and frame rate, as three dependent dropdowns.

    **Dependent, and in this order.** The resolutions a camera offers depend on the pixel format,
    and the frame rates depend on the format *and* the size -- so each list is built for what is
    currently selected, and changing one rebuilds the two below it. Offering every combination flat
    would list modes the camera does not have.

    Only ever the camera's own enumerated values. V4L2 permits a driver to accept a format and
    quietly substitute another, and the surest way to provoke that is to ask for something that was
    never on the list. Asking only for enumerated values is what makes the substitution check in
    :meth:`Session.set_frame_rate` an assertion rather than an expectation.

    No rows at all if the node will not report its current mode: without knowing where it *is*,
    these controls cannot say what they would change from.
    """
    if mode is None:
        return []
    width, height, pixelformat, fps = mode
    current = next((s for s in streams if s.pixelformat == pixelformat), None)

    rows = [
        Capability(
            key=KEY_PIXELFORMAT, kind=Kind.CHOICE, label="Pixel format",
            group=GROUP_INFO, section="Streaming", writable=True,
            choices=[Choice(label=s.pixelformat, value=s.pixelformat) for s in streams],
            description=(
                "Compressed formats reach higher resolutions and frame rates than uncompressed "
                "ones on the same camera, which is usually the reason to change this."
            ),
        ),
    ]
    sizes = current.all_sizes if current else ()
    if sizes:
        rows.append(Capability(
            key=KEY_RESOLUTION, kind=Kind.CHOICE, label="Resolution",
            group=GROUP_INFO, section="Streaming", writable=True,
            choices=[Choice(label=f"{w}×{h}", value=f"{w}×{h}") for w, h in sizes],
            description=f"The {len(sizes)} sizes this camera offers in {pixelformat}.",
        ))
    rates = current.rates_at(width, height) if current else ()
    if rates:
        rows.append(Capability(
            key=KEY_FRAME_RATE, kind=Kind.CHOICE, label="Frame rate", unit="fps",
            group=GROUP_INFO, section="Streaming", writable=True,
            choices=[Choice(label=_fps(r), value=_fps(r)) for r in rates],
            description=f"Available at {width}×{height} in {pixelformat}.",
        ))
    _ = fps
    return rows


#: Tab order.
GROUPS = (GROUP_CAMERA, GROUP_EXPOSURE, GROUP_COLOUR, GROUP_ADVANCED, GROUP_INFO)


__all__ = [
    "COMMON", "GROUPS", "GROUP_ADVANCED", "GROUP_CAMERA", "GROUP_COLOUR", "GROUP_EXPOSURE",
    "GROUP_INFO", "KEY_CARD", "KEY_DRIVER", "KEY_FRAME_RATE", "KEY_MODE",
    "KEY_NODE", "KEY_PIXELFORMAT", "KEY_RESOLUTION",
    "NOTES", "PLACEMENT", "STANDARD_PREFIX", "STREAM_PREFIX", "StreamFormat", "VENDOR_PREFIX",
    "build", "standard_key", "stream_key", "vendor_key",
]
