"""Mapping the MDR feature set onto the shared capability schema.

Every id, label, range and offset here is **imported from the ported protocol code**, never
retyped. An earlier revision hand-wrote the EQ preset ids and auto-power-off labels from
plausibility and got them wrong -- 0x10 is "when headphones removed", not "5 minutes" -- which is
why this module now reads `messages.EQ_PRESETS`, `messages.APO_ELEMENT_LABELS`,
`messages.SOUND_QUALITY_MODES` and friends directly.

Groups are the original application's tab names, in its order, because those were arrived at by
someone actually using the thing.

The set is built from the device's own `CONNECT_RET_SUPPORT_FUNCTION` reply rather than a
per-model table, so an untested model reports its own features and gets a correct page.
"""

from __future__ import annotations

from typing import Any

from hardware_ui.core import Capability, CapabilitySet, Choice, Kind, Tier

from .protocol import messages as M
from .protocol.enums import MAX_ASM_STEPS_XM3
from .protocol.messages import FT

# Tab names, in the original app's order. CapabilitySet.groups() preserves insertion order, so
# building in this order is what orders the tabs.
G_INFO = "Info"
G_NOISE = "Noise && Ambient"
G_EQ = "Equalizer"
G_STC = "Speak-to-Chat"
G_DSEE = "DSEE"
G_CONTROLS = "Controls"
G_CONNECTIVITY = "Connectivity"

# Sub-headings inside the Info tab.
S_GENERAL = "General"
S_BATTERY = "Battery"


# --------------------------------------------------------------------------- Info

MODEL = Capability(
    key="info.model", kind=Kind.READOUT, label="Model", group=G_INFO,
    section=S_GENERAL, writable=False
)
SERIAL = Capability(
    key="info.serial", kind=Kind.READOUT, label="Serial", group=G_INFO,
    section=S_GENERAL, writable=False
)
FIRMWARE = Capability(
    key="info.firmware", kind=Kind.READOUT, label="Firmware", group=G_INFO,
    section=S_GENERAL, writable=False
)
MODEL_ID = Capability(
    key="info.model_id", kind=Kind.READOUT, label="Model ID", group=G_INFO,
    section=S_GENERAL, writable=False
)
DEVICE_ID = Capability(
    key="info.device_id", kind=Kind.READOUT, label="Device ID", group=G_INFO,
    section=S_GENERAL, writable=False
)
CODES = Capability(
    key="info.codes",
    section=S_GENERAL,
    kind=Kind.READOUT,
    label="Codes",
    group=G_INFO,
    tier=Tier.ADVANCED,
    writable=False,
    description="Identifier fields 0x02 and 0x04, as reported during the handshake.",
)
PROTOCOL = Capability(
    key="info.protocol",
    section=S_GENERAL,
    kind=Kind.READOUT,
    label="Protocol info",
    group=G_INFO,
    tier=Tier.ADVANCED,
    writable=False,
    description="Raw RET_PROTOCOL_INFO payload. Useful when reporting an unsupported model.",
)

CODEC = Capability(
    key="info.codec",
    section=S_GENERAL,
    kind=Kind.READOUT,
    label="Active codec",
    group=G_INFO,
    tier=Tier.COMMON,
    writable=False,
    description="LDAC, AAC or SBC, as negotiated with this host.",
)

# Keys that share one protocol message. Writing any member re-sends the whole set.
NCASM_GROUP = ("anc.mode", "anc.ambient_level", "anc.voice_passthrough")
STC_GROUP = ("sound.speak_to_chat", "sound.stc_sensitivity", "sound.stc_timeout")
# A preset change rewrites the whole curve, so the reference implementation marks the preset
# and every band pending together.
EQ_GROUP = ("eq.preset", *(f"eq.band{i}" for i in range(6)))

# --------------------------------------------------------------------------- Noise & Ambient

#: The device has exactly TWO modes here, not three.
#:
#: ``build_set_ncasm``'s docstring is explicit: ``enabled=True`` is Ambient Sound and
#: ``enabled=False`` is Noise Cancelling, and ``effect=OFF`` -- a notional third "off" -- is
#: ignored by the hardware, leaving ambient on. Offering an "Off" entry produced writes the
#: headset silently refused to confirm.
ANC_MODE = Capability(
    key="anc.mode",
    writes_with=NCASM_GROUP,
    kind=Kind.CHOICE,
    label="Noise control",
    group=G_NOISE,
    tier=Tier.COMMON,
    icon="audio-headphones",
    description="Noise cancelling, or ambient sound pass-through.",
    choices=(
        Choice("nc", "Noise cancelling", "audio-headphones"),
        Choice("ambient", "Ambient sound", "audio-input-microphone"),
    ),
)

AMBIENT_LEVEL = Capability(
    key="anc.ambient_level",
    writes_with=NCASM_GROUP,
    kind=Kind.RANGE,
    label="Ambient sound level",
    group=G_NOISE,
    tier=Tier.COMMON,
    # 1..19, matching the original app's setRange(1, ASM_MAX). Level 0 is not a quiet ambient
    # setting -- it is the noise-cancelling state -- so exposing it makes the device reinterpret
    # the write and the slider snap back.
    minimum=1,
    maximum=MAX_ASM_STEPS_XM3,
    step=1,
    description="Higher lets more of the outside world through.",
    requires="anc.mode",
    requires_value="ambient",
)

VOICE_PASSTHROUGH = Capability(
    key="anc.voice_passthrough",
    writes_with=NCASM_GROUP,
    kind=Kind.TOGGLE,
    label="Focus on voice",
    group=G_NOISE,
    tier=Tier.COMMON,
    description="Prioritise speech while ambient sound is on.",
    requires="anc.mode",
    requires_value="ambient",
)

# --------------------------------------------------------------------------- Battery

BATTERY = Capability(
    key="info.battery",
    kind=Kind.METER,
    label="Battery",
    group=G_INFO,
    section=S_BATTERY,
    tier=Tier.COMMON,
    unit="%",
    minimum=0,
    maximum=100,
    writable=False,
)
BATTERY_LR = Capability(
    key="info.battery_lr",
    kind=Kind.READOUT,
    label="Earbuds",
    group=G_INFO,
    section=S_BATTERY,
    writable=False,
    description="True-wireless models report each bud separately.",
)
CHARGING = Capability(
    key="info.charging",
    kind=Kind.READOUT,
    label="Charging",
    group=G_INFO,
    section=S_BATTERY,
    writable=False,
)

# --------------------------------------------------------------------------- Equalizer

#: Preset ids come straight from ``messages.EQ_PRESETS`` -- the list confirmed against an HCI
#: capture of what Sony's own app sends to an XM4.
EQ_PRESET = Capability(
    key="eq.preset",
    writes_with=EQ_GROUP,
    kind=Kind.CHOICE,
    label="Equaliser preset",
    group=G_EQ,
    tier=Tier.COMMON,
    icon="preferences-desktop-sound",
    choices=tuple(Choice(pid, label) for pid, label in M.EQ_PRESETS),
)

#: Six bands, not five: index 0 is Clear Bass, then five EQ bands. Editable only while a custom
#: preset is active -- named presets show a fixed, read-only curve.
EQ_BAND_LABELS = ("Clear Bass", "400 Hz", "1 kHz", "2.5 kHz", "6.3 kHz", "16 kHz")

EQ_BANDS = tuple(
    Capability(
        key=f"eq.band{i}",
        writes_with=EQ_GROUP,
        kind=Kind.RANGE,
        label=label,
        group=G_EQ,
        minimum=M.EQ_BAND_MIN,
        maximum=M.EQ_BAND_MAX,
        step=1,
        requires="eq.preset",
        requires_value=tuple(sorted(M.EQ_CUSTOM_PRESETS)),
    )
    for i, label in enumerate(EQ_BAND_LABELS)
)

# --------------------------------------------------------------------------- Speak-to-Chat

SPEAK_TO_CHAT = Capability(
    key="sound.speak_to_chat",
    writes_with=STC_GROUP,
    kind=Kind.TOGGLE,
    label="Speak-to-chat",
    group=G_STC,
    tier=Tier.COMMON,
    description="Pause playback automatically when you start talking.",
)
STC_SENSITIVITY = Capability(
    key="sound.stc_sensitivity",
    writes_with=STC_GROUP,
    kind=Kind.CHOICE,
    label="Sensitivity",
    group=G_STC,
    choices=tuple(Choice(v, label) for v, label in M.STC_SENSITIVITY),
    requires="sound.speak_to_chat",
)
STC_TIMEOUT = Capability(
    key="sound.stc_timeout",
    writes_with=STC_GROUP,
    kind=Kind.CHOICE,
    label="Resume playback after",
    group=G_STC,
    choices=tuple(Choice(v, label) for v, label in M.STC_TIMEOUT),
    requires="sound.speak_to_chat",
)

# --------------------------------------------------------------------------- DSEE

DSEE = Capability(
    key="sound.dsee",
    kind=Kind.TOGGLE,
    label="DSEE HX upscaling",
    group=G_DSEE,
    description="Restores high-frequency detail lost to compression. Costs battery.",
    note="Upscales compressed audio. The XM4 offers only Auto or Off; it may be unavailable "
         "while an equalizer preset is active.",
)

# --------------------------------------------------------------------------- Controls

TOUCH_SENSOR = Capability(
    key="system.touch_sensor", kind=Kind.TOGGLE, label="Touch controls", group=G_CONTROLS
)
AUTO_PAUSE = Capability(
    key="system.auto_pause",
    kind=Kind.TOGGLE,
    label="Pause when removed",
    group=G_CONTROLS,
)
CUSTOM_BUTTON = Capability(
    key="system.custom_button",
    kind=Kind.CHOICE,
    label="Custom button",
    group=G_CONTROLS,
    tier=Tier.ADVANCED,
    choices=tuple(Choice(v, label) for v, label in M.CUSTOM_BTN_FUNCS),
    reboots=True,
)
#: Options are replaced in build() by whatever the device advertises; this is only a fallback for
#: a device that answers the capability query with nothing.
AUTO_POWER_OFF = Capability(
    key="system.auto_power_off",
    kind=Kind.CHOICE,
    label="Power off when idle",
    group=G_CONTROLS,
    choices=(
        Choice(M.APO_NEVER, M.APO_ELEMENT_LABELS[M.APO_NEVER]),
        Choice(M.APO_OFF_WHEN_REMOVED, M.APO_ELEMENT_LABELS[M.APO_OFF_WHEN_REMOVED]),
    ),
)

# --------------------------------------------------------------------------- Connectivity

QUALITY_MODE = Capability(
    key="sound.quality_mode",
    kind=Kind.CHOICE,
    label="Connection preference",
    group=G_CONNECTIVITY,
    choices=tuple(Choice(v, label) for v, label in M.SOUND_QUALITY_MODES),
    reboots=True,
    confirm_detail="Stable Connection disables LDAC.",
)
MULTIPOINT = Capability(
    key="system.multipoint",
    kind=Kind.TOGGLE,
    label="Connect to two devices",
    group=G_CONNECTIVITY,
    tier=Tier.ADVANCED,
    reboots=True,
    confirm_detail="LDAC cannot be used while connected to 2 devices.",
    note="Multipoint and Stable-Connection mode disable LDAC. Changing either reconnects "
         "the headphones.",
)


#: FunctionType code -> capabilities it unlocks. Sony splits noise control across three codes
#: depending on model; any of them means the device has noise control of some form.
FEATURE_MAP: dict[int, tuple[Capability, ...]] = {
    FT.NOISE_CANCELLING: (ANC_MODE, AMBIENT_LEVEL, VOICE_PASSTHROUGH),
    FT.NC_AND_ASM: (ANC_MODE, AMBIENT_LEVEL, VOICE_PASSTHROUGH),
    FT.AMBIENT_SOUND_MODE: (ANC_MODE, AMBIENT_LEVEL, VOICE_PASSTHROUGH),
    FT.PRESET_EQ: (EQ_PRESET, *EQ_BANDS),
    FT.PRESET_EQ_NONCUSTOMIZABLE: (EQ_PRESET,),
    FT.SMART_TALKING_MODE: (SPEAK_TO_CHAT, STC_SENSITIVITY, STC_TIMEOUT),
    FT.UPSCALING: (DSEE,),
    FT.GENERAL_SETTING1: (TOUCH_SENSOR,),
    FT.CONTROL_BY_WEARING: (AUTO_PAUSE,),
    FT.ASSIGNABLE_SETTINGS: (CUSTOM_BUTTON,),
    FT.AUTO_POWER_OFF: (AUTO_POWER_OFF,),
    FT.CONNECTION_MODE: (QUALITY_MODE,),
    FT.GENERAL_SETTING2: (MULTIPOINT,),
    FT.CODEC_INDICATOR: (CODEC,),
}

#: Present on every MDR device regardless of the advertised function list.
ALWAYS: tuple[Capability, ...] = (
    MODEL, MODEL_ID, SERIAL, DEVICE_ID, FIRMWARE, CODES, PROTOCOL,
    # Battery section last, charging above the level.
    CHARGING, BATTERY_LR, BATTERY,
)

#: Tab order. Anything not listed sorts to the end.
GROUP_ORDER = (
    G_INFO, G_NOISE, G_EQ, G_STC, G_DSEE, G_CONTROLS, G_CONNECTIVITY,
)


#: Capability-key prefix -> the DeviceState attribute that proves the device reported it.
#: Mirrors ``_apply_gating``: no state, no control.
STATE_GATES: dict[str, str] = {
    "anc.": "ncasm",
    "eq.": "eq",
    "sound.speak_to_chat": "stc",
    "sound.stc_": "stc",
    "sound.dsee": "dsee",
    "sound.quality_mode": "sound_quality",
    "system.touch_sensor": "touch_panel",
    "system.auto_pause": "auto_pause",
    "system.multipoint": "multipoint",
    "system.custom_button": "custom_button",
    "system.auto_power_off": "apo_current",
}


def observed(key: str, state: Any) -> bool:
    """Whether the device reported the state backing *key*.

    Info readouts are always kept -- the reference implementation always shows Info and Battery,
    even for a model with no configurable features at all.
    """
    # Only true-wireless models report per-bud levels; on over-ears the row would be permanently
    # blank, so it is dropped rather than shown empty.
    if key == "info.battery_lr":
        return getattr(getattr(state, "battery", None), "left", None) is not None

    for prefix, attr in STATE_GATES.items():
        if key.startswith(prefix):
            return getattr(state, attr, None) is not None
    return True


def build(
    supported: set[int],
    apo_options: list[int] | None = None,
    state: Any = None,
) -> CapabilitySet:
    """Turn the device's advertised FunctionType codes into capabilities.

    An empty *supported* set means no support list was obtained. The ported
    ``Headphones.supports`` treats that optimistically and so do we: build everything and let
    individual rows fail soft if the firmware rejects them.

    *apo_options* are the element ids the device reported for auto-power-off, labelled through
    ``messages.APO_ELEMENT_LABELS`` so we never offer a timeout the hardware lacks.
    """
    caps: list[Capability] = list(ALWAYS)
    seen = {c.key for c in caps}

    groups = FEATURE_MAP.values() if not supported else [
        v for k, v in FEATURE_MAP.items() if k in supported
    ]
    for group in groups:
        for cap in group:
            if cap.key not in seen:
                caps.append(cap)
                seen.add(cap.key)

    if apo_options:
        caps = [
            _with_apo_choices(c, apo_options) if c.key == AUTO_POWER_OFF.key else c for c in caps
        ]

    # Second gate: advertised in the function list, but did the device actually report a value?
    # An XM3 advertises features an XM4 has; only the ones that answered are real.
    if state is not None:
        caps = [c for c in caps if observed(c.key, state)]

    caps.sort(key=lambda c: (_group_rank(c.group), _section_rank(c.section)))
    return CapabilitySet(caps)


#: Section order within a group. Unlisted sections sort before Battery, which stays last.
SECTION_ORDER = (S_GENERAL, S_BATTERY)


def _section_rank(section: str) -> int:
    return SECTION_ORDER.index(section) if section in SECTION_ORDER else 0


def _group_rank(group: str) -> int:
    return GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)


def _with_apo_choices(cap: Capability, options: list[int]) -> Capability:
    """Label the device's own APO element ids using the protocol's table."""
    import dataclasses

    choices = tuple(
        Choice(el, M.APO_ELEMENT_LABELS.get(el, f"Option {el:#04x}")) for el in options
    )
    return dataclasses.replace(cap, choices=choices) if choices else cap
