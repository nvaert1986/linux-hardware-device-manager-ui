"""What a Dell monitor can do, expressed in the shell's schema.

Nothing here is a per-model table. Everything is derived from the panel's own VESA capability
string, exactly as the Sony module derives its page from the headset's function list -- which is
what lets an untested Dell get a correct page from a ``family`` match.

The Dell value maps, the tab order, the display order and the merged-preset logic all come from
:mod:`.protocol.features`, which is byte-identical to its origin. **Import those tables; never
retype them.** Hand-writing plausible-looking ids is how "Option 0x10" shipped in the first Sony
attempt while the correct table sat one import away.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hardware_ui.core import Capability, CapabilitySet, Choice, Kind

from .protocol import features as F
from .protocol.calibration import Range
from .protocol.ddcutil import VcpReading

# --------------------------------------------------------------------------- keys

# A key is a published identifier: it appears in the CLI, in bug reports and in anything a user
# scripts, so it must not track an opcode number that means nothing to them.
KEY_BY_CODE: dict[int, str] = {
    0x10: "image.brightness",
    0x12: "image.contrast",
    0x87: "image.sharpness",
    0x16: "image.gain_red",
    0x18: "image.gain_green",
    0x1A: "image.gain_blue",
    0x8A: "image.saturation",
    0x6C: "image.black_red",
    0x6E: "image.black_green",
    0x70: "image.black_blue",
    0xDC: "image.display_mode",
    0x14: "image.colour_temperature",
    0xF0: "image.hdr_mode",
    0xF4: "image.gaming_mode",
    0x62: "audio.volume",
    0x8D: "audio.mute",
    0x60: "settings.input",
    0xCC: "settings.osd_language",
    0xD6: "settings.power",
    0xAA: "settings.orientation",
}
CODE_BY_KEY: dict[str, int] = {key: code for code, key in KEY_BY_CODE.items()}

PRESET_KEY = "image.preset"
"""The merged Colour Preset: one control over 0xDC, 0x14 and 0xF0, read back through 0xE2."""

PIP_MODE_KEY = "pip.mode"
PIP_SUBINPUT_KEY = "pip.sub_input"
PIP_STATUS_KEY = "pip.status"
PIP_SIZE_KEY = "pip.toggle_size"
PIP_POSITION_KEY = "pip.toggle_position"

MST_KEY = "mst.enable"
USBC_PRIORITY_KEY = "mst.usbc_priority"

KVM_UPSTREAM_KEY = "kvm.upstream"
KVM_PAIR_PREFIX = "kvm.pair."

FACTORY_RESET_KEY = "action.factory_reset"
CALIBRATE_KEY = "action.calibrate"
REREAD_KEY = "action.reread"

INFO_PREFIX = "info."

GROUP_INFO, GROUP_SETTINGS, GROUP_IMAGE, GROUP_PIP, GROUP_MST, GROUP_KVM = F.TAB_ORDER


def pair_key(input_code: int) -> str:
    return f"{KVM_PAIR_PREFIX}{input_code:02x}"


def input_name_key(input_code: int) -> str:
    return f"settings.input_name.{input_code:02x}"


def input_label(code: int, names: Mapping[int, str]) -> str:
    """An input's display name: the user's label if they set one, else the DDC name.

    The monitor's own menu cannot be renamed over DDC/CI -- Dell's software writes those names
    through a channel ``ddcutil`` cannot reach, proven by a full register diff on the P3424WE --
    so an app-side label is the whole of what is possible, and it is worth having.
    """
    return (names.get(code) or "").strip() or F.enum_label(0x60, code)


# --------------------------------------------------------------------------- notes

NOTE_PIP = (
    "PIP/PBP only shows a second image when two inputs are active — that is the monitor's own "
    "behaviour. Switching mode briefly blanks the screen while the panel re-initialises."
)
NOTE_MST_OSD_ONLY = (
    "This monitor reports the older 0xEF specification, where MST is enabled from the monitor's "
    "own on-screen menu and not over DDC. Dell's own software offers no toggle here either."
)
NOTE_MST_NEW_SPEC = (
    "Reverse-engineered from Dell's software and never tested on hardware — no monitor using the "
    "newer 0xEF specification has been available. Enabling MST re-enumerates the DisplayPort "
    "link, so the monitors will disappear and come back."
)
NOTE_USBC_PRIORITY = (
    "This setting cannot be read back, so it is sent without confirmation and the box starts "
    "empty. Applying it re-negotiates the link: the screen may blank and USB devices may "
    "reconnect. It only has a visible effect while MST is active."
)
NOTE_KVM_PAIRING = (
    "Choose which USB upstream port feeds each video input. The keyboard and mouse only actually "
    "move when a second computer is connected to the other upstream — the monitor will not "
    "orphan them to an empty port. Switch the active input from the Settings tab; doing so hands "
    "the picture to the other computer, so it is best driven from the machine you switch to."
)
NOTE_KVM_OSD_ONLY = (
    "This monitor has a USB KVM but does not expose the upstream pairing over DDC — set it from "
    "the monitor's own on-screen menu. Switching the active input, on the Settings tab, still "
    "moves the USB hub with it."
)
NOTE_AUDIO_UNVERIFIED = (
    "Speaker volume and mute are standard MCCS codes, but no monitor with built-in speakers has "
    "been available to test them on."
)
NOTE_CALIBRATE = (
    "Some panels clamp or quantise a setting over DDC even though their menu offers the full "
    "range — this one refuses contrast below 25 and moves sharpness in steps of 10, for example. "
    "Calibrating writes brief test values to learn the real limits, so the sliders match the "
    "hardware. The screen will flash for a second or two, then your settings are restored."
)
NOTE_INPUT_NAMES = (
    "These labels live in this app, not in the monitor. Dell's own software writes names into the "
    "monitor's menu through a private channel that DDC/CI does not expose, so the names here "
    "cannot be pushed to the panel. Leave one blank for the default."
)

CONFIRM_INPUT = (
    "This changes the monitor's active input; the picture may switch away from this machine."
)
CONFIRM_POWER = "This can put the display into standby or turn it off."
CONFIRM_FACTORY_RESET = (
    "Every setting on the monitor goes back to its factory default, including ones this app "
    "does not control. There is no undo."
)
CONFIRM_PIP = "The panel will blank for a moment while it re-initialises."


# --------------------------------------------------------------------------- timeouts

CALIBRATE_TIMEOUT = 240.0
"""Six probe writes and read-backs for each of up to nine continuous features, each a separate
ddcutil invocation that re-probes the bus. Slow by construction, and slower again over MST."""

PIP_TIMEOUT = 30.0
"""Entering PIP blanks the panel; the verify polls for up to 10 s on top of the writes."""

RESET_TIMEOUT = 40.0
"""Factory reset is write-only, so the module waits for the panel to settle and re-reads."""


# --------------------------------------------------------------------------- building


def build(
    *,
    caps: Mapping[int, list[int] | None],
    readings: Mapping[int, VcpReading],
    ranges: Mapping[int, Range],
    info_rows: Sequence[tuple[str, str]],
    input_names: Mapping[int, str],
    read_only: Callable[[int], bool],
) -> CapabilitySet:
    """Assemble the page from what this panel reports.

    *caps* is the parsed capability string, *readings* the first snapshot (a continuous feature's
    real maximum is only known from a read), *ranges* any saved calibration, *info_rows* the
    read-only identity from ``get_monitor_info``, and *read_only* the MCCS writability check.

    Groups are emitted in ``TAB_ORDER`` because the shell tabs follow declaration order.
    """
    out: list[Capability] = []
    out += _information(info_rows, caps, readings, read_only)
    settings, image = _controls(caps, readings, ranges, read_only, input_names)
    out += settings
    out += _input_names(caps, input_names)
    out += _factory_reset(caps)
    out += image
    out += _calibration(caps, ranges, read_only)
    out += _pip(caps, readings, input_names)
    out += _mst(caps)
    out += _kvm(caps, input_names)
    return CapabilitySet(out)


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")


def _information(
    info_rows: Sequence[tuple[str, str]],
    caps: Mapping[int, list[int] | None],
    readings: Mapping[int, VcpReading],
    read_only: Callable[[int], bool],
) -> list[Capability]:
    out = [
        Capability(
            key=f"{INFO_PREFIX}{_slug(label)}",
            kind=Kind.READOUT,
            label=label,
            group=GROUP_INFO,
            section="Identity",
            writable=False,
        )
        for label, _ in info_rows
    ]

    # Features the MCCS spec marks read-only report real state but cannot be set. The origin
    # filtered them out entirely; showing them as readouts is strictly more informative and costs
    # nothing, because the renderer never disables a non-writable row.
    reported = [
        code
        for code in F.DISPLAY_ORDER
        if code in caps and code in readings and read_only(code) and code in F.ENUM_LABELS
    ]
    out += [
        Capability(
            key=KEY_BY_CODE.get(code, f"{INFO_PREFIX}vcp_{code:02x}"),
            kind=Kind.READOUT,
            label=F.feature_name(code),
            group=GROUP_INFO,
            section="Reported by the monitor",
            writable=False,
            description="The monitor reports this but the DDC/CI spec marks it read-only.",
        )
        for code in reported
    ]

    out.append(
        Capability(
            key=REREAD_KEY,
            kind=Kind.ACTION,
            label="Values",
            action_label="Re-read from monitor",
            group=GROUP_INFO,
            section="Actions",
            description="Read every setting back from the panel.",
            note=(
                "Settings are read when you connect and after each change, never on a timer — "
                "polling a monitor over I²C is slow and competes with every other DDC access. "
                "Use this after changing something from the monitor's own menu."
            ),
        )
    )
    return out


def _controls(
    caps: Mapping[int, list[int] | None],
    readings: Mapping[int, VcpReading],
    ranges: Mapping[int, Range],
    read_only: Callable[[int], bool],
    input_names: Mapping[int, str],
) -> tuple[list[Capability], list[Capability]]:
    """Every ordinary control, split into the Settings and Color / Picture tabs."""
    codes = [c for c in F.ordered_editable(dict(caps)) if not read_only(c)]
    if F.has_merged_preset(dict(caps)):
        codes = _insert_in_display_order(codes, F.PRESET_CODE)

    settings: list[Capability] = []
    image: list[Capability] = []
    for code in codes:
        cap = (
            _preset(caps)
            if code == F.PRESET_CODE
            else _control(
                code, caps.get(code), readings.get(code), ranges.get(code), input_names
            )
        )
        if cap is None:
            continue
        (settings if cap.group == GROUP_SETTINGS else image).append(cap)
    return settings, image


def _insert_in_display_order(codes: list[int], code: int) -> list[int]:
    """Put *code* where ``DISPLAY_ORDER`` says it belongs, not at the end."""
    if code in codes:
        return codes
    rank = F.DISPLAY_ORDER.index(code) if code in F.DISPLAY_ORDER else len(F.DISPLAY_ORDER)

    def key(c: int) -> int:
        return F.DISPLAY_ORDER.index(c) if c in F.DISPLAY_ORDER else len(F.DISPLAY_ORDER)

    at = next((i for i, c in enumerate(codes) if key(c) > rank), len(codes))
    return [*codes[:at], code, *codes[at:]]


def _control(
    code: int,
    values: list[int] | None,
    reading: VcpReading | None,
    rng: Range | None,
    input_names: Mapping[int, str],
) -> Capability | None:
    group = F.feature_category(code)
    kind = F.feature_kind(code, values)
    common = {
        "key": KEY_BY_CODE.get(code, f"vcp.{code:02x}"),
        "label": F.feature_name(code),
        "group": group,
    }
    if kind == "continuous":
        # DDC/CI reports a maximum and nothing else: the usable minimum and step are only known
        # once someone has calibrated, and until then the panel silently snaps what it is sent.
        maximum = rng.maximum if rng else max((reading.maximum if reading else 0), 1)
        minimum = rng.minimum if rng else 0
        if minimum >= maximum:
            minimum = 0
        return Capability(
            **common,
            kind=Kind.RANGE,
            minimum=minimum,
            maximum=maximum,
            step=rng.step if rng else 1,
            note=NOTE_AUDIO_UNVERIFIED if code in (0x62, 0x8D) else "",
            experimental=code in (0x62, 0x8D),
        )
    if kind == "enum":
        return Capability(
            **common,
            kind=Kind.CHOICE,
            choices=tuple(Choice(v, _choice_label(code, v, input_names)) for v in values or ()),
            confirm=code in F.CONFIRM_CODES,
            confirm_detail=(
                CONFIRM_INPUT if code == 0x60 else CONFIRM_POWER if code == 0xD6 else ""
            ),
            note=NOTE_AUDIO_UNVERIFIED if code in (0x62, 0x8D) else "",
            experimental=code in (0x62, 0x8D),
        )
    return None


def _choice_label(code: int, value: int, input_names: Mapping[int, str]) -> str:
    return input_label(value, input_names) if code == 0x60 else F.enum_label(code, value)


def _preset(caps: Mapping[int, list[int] | None]) -> Capability | None:
    """The merged Colour Preset.

    The single list in the monitor's menu is split over three writable opcodes, and a *fourth*
    read-only register reports which one is active. Presenting them separately would put
    "Standard" in one dropdown and "Warm" in another, which is not how the monitor thinks about
    them and not how Dell's own software presents them.
    """
    items = F.build_preset_items(dict(caps))
    if not items:
        return None
    return Capability(
        key=PRESET_KEY,
        kind=Kind.CHOICE,
        label="Colour Preset",
        group=GROUP_IMAGE,
        choices=tuple(Choice(item.label, item.label) for item in items),
        description=(
            "Picture modes, blue-light and colour temperatures, merged into one list the way the "
            "monitor's own menu presents them."
        ),
        note=(
            ""
            if 0xE2 in caps
            else "This monitor cannot report which preset is active, so the box shows your last "
            "choice rather than the panel's."
        ),
    )


def _input_names(
    caps: Mapping[int, list[int] | None], input_names: Mapping[int, str]
) -> list[Capability]:
    values = caps.get(0x60) or []
    if not values:
        return []
    return [
        Capability(
            key=input_name_key(code),
            kind=Kind.TEXT,
            label=F.enum_label(0x60, code),
            group=GROUP_SETTINGS,
            section="Input names",
            note=NOTE_INPUT_NAMES if i == 0 else "",
        )
        for i, code in enumerate(values)
    ]


def _factory_reset(caps: Mapping[int, list[int] | None]) -> list[Capability]:
    if not F.has_factory_reset(dict(caps)):
        return []
    return [
        Capability(
            key=FACTORY_RESET_KEY,
            kind=Kind.ACTION,
            label="Factory defaults",
            action_label="Restore factory defaults",
            group=GROUP_SETTINGS,
            section="Reset",
            confirm=True,
            confirm_detail=CONFIRM_FACTORY_RESET,
            timeout=RESET_TIMEOUT,
        )
    ]


def _calibration(
    caps: Mapping[int, list[int] | None],
    ranges: Mapping[int, Range],
    read_only: Callable[[int], bool],
) -> list[Capability]:
    if not any(
        c in F.CONTINUOUS and not read_only(c) and F.feature_category(c) == GROUP_IMAGE
        for c in caps
    ):
        return []
    return [
        Capability(
            key=CALIBRATE_KEY,
            kind=Kind.ACTION,
            label="Slider ranges",
            action_label="Calibrate ranges…" if not ranges else "Calibrate ranges again…",
            group=GROUP_IMAGE,
            section="Ranges",
            confirm=True,
            confirm_detail=(
                "Test values are written to each slider and then undone. The screen will flash "
                "for a second or two."
            ),
            timeout=CALIBRATE_TIMEOUT,
            note=NOTE_CALIBRATE,
        )
    ]


def _pip(
    caps: Mapping[int, list[int] | None],
    readings: Mapping[int, VcpReading],
    input_names: Mapping[int, str],
) -> list[Capability]:
    c = dict(caps)
    if not F.has_pip(c):
        return []
    out = [
        Capability(
            key=PIP_MODE_KEY,
            kind=Kind.CHOICE,
            label="Mode",
            group=GROUP_PIP,
            choices=tuple(Choice(v, label) for v, label in F.pip_modes(c)),
            confirm=True,
            confirm_detail=CONFIRM_PIP,
            timeout=PIP_TIMEOUT,
            note=NOTE_PIP,
        )
    ]
    sub_values = caps.get(F.PIP_SUBINPUT_CODE) or caps.get(0x60) or []
    if F.PIP_SUBINPUT_CODE in caps and sub_values:
        labels = F.input_labels_for(list(sub_values))
        out.append(
            Capability(
                key=PIP_SUBINPUT_KEY,
                kind=Kind.CHOICE,
                label="Sub-window input",
                group=GROUP_PIP,
                choices=tuple(
                    Choice(v, input_names.get(v, "").strip() or labels[v]) for v in sub_values
                ),
            )
        )
    if F.has_pip_size_toggle(c):
        out.append(
            Capability(
                key=PIP_SIZE_KEY,
                kind=Kind.ACTION,
                label="Sub-window size",
                action_label="Toggle small / large",
                group=GROUP_PIP,
                timeout=PIP_TIMEOUT,
            )
        )
    if F.has_pip_position_toggle(c):
        out.append(
            Capability(
                key=PIP_POSITION_KEY,
                kind=Kind.ACTION,
                label="Sub-window position",
                action_label="Move to next corner",
                group=GROUP_PIP,
                timeout=PIP_TIMEOUT,
            )
        )
    if F.PIP_STATUS_CODE in readings:
        out.append(
            Capability(
                key=PIP_STATUS_KEY,
                kind=Kind.READOUT,
                label="Status",
                group=GROUP_PIP,
                writable=False,
            )
        )
    return out


def _mst(caps: Mapping[int, list[int] | None]) -> list[Capability]:
    c = dict(caps)
    out: list[Capability] = []
    if F.has_ddc_mst_control(c):
        out.append(
            Capability(
                key=MST_KEY,
                kind=Kind.TOGGLE,
                label="DisplayPort daisy-chaining (MST)",
                group=GROUP_MST,
                confirm=True,
                confirm_detail=(
                    "The DisplayPort link is re-negotiated: every monitor on the chain will "
                    "disappear and come back."
                ),
                note=NOTE_MST_NEW_SPEC,
                experimental=True,
            )
        )
    elif F.has_mst(c):
        out.append(
            Capability(
                key=MST_KEY,
                kind=Kind.READOUT,
                label="DisplayPort daisy-chaining (MST)",
                group=GROUP_MST,
                writable=False,
                note=NOTE_MST_OSD_ONLY,
            )
        )
    if F.has_usbc_priority(c):
        out.append(
            Capability(
                key=USBC_PRIORITY_KEY,
                kind=Kind.CHOICE,
                label="USB-C Prioritization",
                group=GROUP_MST,
                choices=tuple(Choice(w, label) for w, label in F.USBC_PRIORITY_OPTIONS),
                confirm=True,
                confirm_detail=(
                    "The USB-C link is re-negotiated: the screen may blank and USB devices may "
                    "reconnect."
                ),
                note=NOTE_USBC_PRIORITY,
                experimental=True,
            )
        )
    return out


def _kvm(
    caps: Mapping[int, list[int] | None], input_names: Mapping[int, str]
) -> list[Capability]:
    c = dict(caps)
    if not F.has_usb_kvm(c):
        return []

    if F.usb_kvm_upstream_controllable(c):
        # Dell's other scheme: one word pinning the shared USB to Auto or a numbered computer.
        # Decoded from DDPM but never seen on hardware -- no monitor advertising 0xFE was
        # available -- so it is badged rather than presented as known-good.
        return [
            Capability(
                key=KVM_UPSTREAM_KEY,
                kind=Kind.CHOICE,
                label="Shared USB follows",
                group=GROUP_KVM,
                choices=tuple(Choice(w, label) for w, label in F.usb_kvm_options(c)),
                confirm=True,
                note=NOTE_KVM_PAIRING,
                experimental=True,
            )
        ]

    if F.usb_kvm_bitpacked(c):
        pairings = F.usb_kvm_pairings(c)
        indices = F.usb_kvm_upstream_indices(c)
        if pairings and indices:
            group_keys = tuple(pair_key(code) for code, _ in pairings)
            return [
                Capability(
                    key=pair_key(code),
                    kind=Kind.CHOICE,
                    label=f"USB for {input_label(code, input_names)}",
                    group=GROUP_KVM,
                    choices=tuple(Choice(i, F.usb_upstream_label(i)) for i in indices),
                    # Every input's setting lives in one 16-bit register, so writing any of them
                    # re-sends the others. Holding the whole group is the rule that stopped
                    # Sony's speak-to-chat switching itself off, and it applies verbatim here.
                    writes_with=group_keys,
                    confirm=True,
                    confirm_detail=(
                        "The keyboard and mouse attached to this monitor move to the other "
                        "upstream port."
                    ),
                    note=NOTE_KVM_PAIRING if i == 0 else "",
                )
                for i, (code, _bit) in enumerate(pairings)
            ]

    return [
        Capability(
            key=KVM_UPSTREAM_KEY,
            kind=Kind.READOUT,
            label="USB upstream pairing",
            group=GROUP_KVM,
            writable=False,
            note=NOTE_KVM_OSD_ONLY,
        )
    ]


def preset_by_label(caps: Mapping[int, list[int] | None], label: str) -> Any:
    """The :class:`PresetItem` a merged-preset choice refers to."""
    return next((i for i in F.build_preset_items(dict(caps)) if i.label == label), None)
