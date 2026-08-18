"""What a Creative device offers, built from what the device itself reports.

**No per-model tables.** The vendor library justifies this rather than convenience: ``CTCDC.dll``
contains no supported-device list -- its only per-model code is a display-name lookup that does not
even include the Sound Blaster X4 -- and its device check reads vendor and product from a struct at
runtime. So the shape of the page comes from two masks the device answers with:

``FeatureControl`` op 2
    A 32-bit mask of the toggles this unit implements. Bit N is :class:`~.protocol.ids.Feature` N.

``SubFeatureSupport``
    A 32-bit mask of what the *DSP* implements -- equaliser, Crystalizer, surround. This X4 reports
    ``0x40``, graphic equaliser only, because the other effects run host-side as Windows APOs.

A unit that answers neither is treated permissively and shown everything, on the same principle as
``DeviceState.supports``: a control that turns out not to apply is a smaller failure than a working
control that was hidden.
"""

from __future__ import annotations

from hardware_ui.core.capability import Capability, Choice, Kind, PromptField, Tier

from .protocol import catalogue
from .protocol import presets as presets_mod
from .protocol.ids import (
    EQ_BAND_COUNT,
    EQ_GAIN_MAX,
    EQ_GAIN_MIN,
    FEATURE_LABELS,
    SXFI_MODES,
    Feature,
    OutputTarget,
    SubFeature,
)

#: Key prefixes. Stable identifiers: they key config files, the CLI and pinned favourites, so they
#: must not change once published.
FEATURE_PREFIX = "feature."
BAND_PREFIX = "eq.band"

KEY_OUTPUT = "output.target"
KEY_SXFI = "sxfi.enabled"
KEY_SXFI_MODE = "sxfi.mode"
KEY_EQ_ENABLED = "eq.enabled"
KEY_EQ_PREAMP = "eq.preamp"
KEY_EQ_PRESET = "eq.preset"
KEY_FIRMWARE = "info.firmware"
KEY_SERIAL = "info.serial"
KEY_VOLUME = "audio.volume"
KEY_MUTED = "audio.muted"
KEY_FEATURE_MASK = "info.feature_mask"
KEY_DSP_MASK = "info.dsp_mask"
KEY_PROFILE_STORE = "eq.profile.store"

#: "Off", the only value of the Equalizer selector that is not one of the card's modes.
#:
#: One control rather than a checkbox plus a profile dropdown, and that is the correction rather
#: than a simplification. The checkbox mapped to a real device operation and still described the
#: card badly: switching it on put the card into whatever stored mode was live, so the honest
#: reading of "tick this box" was "turn on Movie mode", and the question it produced -- *why did
#: enabling the equaliser put it in Movie mode?* -- is the one this list answers by construction.
#:
#: There is no bare "On" either, and there was: the card has no such state. It is off, or it is in
#: one of four named modes, which is exactly what the button on the front cycles through.
EQ_OFF = -1

GROUP_SOUND = "Sound"
GROUP_EQ = "Equalizer"
GROUP_DEVICE = "Device"

#: Direct Mode is a DSP **bypass**, so nothing downstream of the DSP applies while it is on. That
#: is the whole DSP chain, not only the equaliser: Super X-Fi and headphone virtualisation go with
#: it. Every affected row is gated on it rather than merely annotated, because a control that moves
#: and changes nothing is the exact confusion the ``requires`` mechanism exists to prevent.
#:
#: The interlock is **ours**, not Creative's -- the vendor's Equalizer module never references
#: Direct Mode. It was observed on hardware in the source project, and the vendor app behaves the
#: same way on the wire: in the reference capture it switched Direct Mode off first and left it off
#: for every DSP change, turning it back on only as the final action.
#:
#: What is deliberately *not* gated: output routing and Headphone High Gain. Those are analogue and
#: routing controls and work either way -- the source project's ``_apply_eq_gate`` says so in as
#: many words, and gating them here would have taken away two controls that work.
KEY_DIRECT_MODE = f"{FEATURE_PREFIX}{Feature.DIRECT_MODE.name.lower()}"

#: Applied to every row Direct Mode bypasses. One dict so the set cannot drift apart row by row.
DSP_GATE = {"requires": KEY_DIRECT_MODE, "requires_value": False}

#: The two outputs the vendor application offers, in its own order.
#:
#: ``OutputTarget`` carries a third bit, ``POWER_AMP``. It is **not** offered, and that is a
#: correction rather than an omission: the Windows app's routing box has exactly two entries, and
#: the source project -- the one that was driven against real hardware -- offers the same two. The
#: protocol has no query for which targets a unit actually has, so a third entry would be this
#: application inventing an output and then routing audio into it. Selecting a target the card
#: cannot reach is not a cosmetic mistake: it is silence.
OUTPUT_LABELS = {
    OutputTarget.HEADPHONES: "Headphones",
    OutputTarget.LINE_OUT: "Speakers (Line Out)",
}


def feature_key(feature: Feature) -> str:
    return f"{FEATURE_PREFIX}{feature.name.lower()}"


def band_key(index: int) -> str:
    return f"{BAND_PREFIX}{index}"


def band_index(key: str) -> int | None:
    """The band a key addresses, or None. The inverse of :func:`band_key`."""
    if not key.startswith(BAND_PREFIX):
        return None
    try:
        index = int(key[len(BAND_PREFIX):])
    except ValueError:
        return None
    return index if 0 <= index < EQ_BAND_COUNT else None


def build(*, feature_support: int, subfeatures: SubFeature | None,
          has_equalizer_state: bool, presets: dict[str, object] | None = None,
          profile_names: dict[int, str] | None = None) -> list[Capability]:
    """The capability list for a device reporting these masks.

    ``has_equalizer_state`` says whether the equaliser actually answered during the initial sync.
    It is separate from the ``SubFeature`` mask on purpose: the mask is what the device claims and
    this is what it did, and a control is only offered when both agree. A device that advertises
    an equaliser and then times out on all twelve reads should not get twelve dead sliders.
    """
    out: list[Capability] = []

    # -- features ---------------------------------------------------------------------------
    #
    # Only the labelled ones. The Feature enum carries sixteen bits recovered from the vendor
    # source, but several (SWITCH_USB_MUX, SYNC_MASTER_VR_TO_HOST) have no meaning to a user and
    # no observed behaviour, and offering an unlabelled toggle is offering a coin flip.
    for feature, label in FEATURE_LABELS.items():
        if feature_support and not feature_support & feature.mask:
            continue
        # Headphone virtualisation *is* DSP processing, so Direct Mode bypasses it exactly as it
        # bypasses the equaliser. The source project gates it for that reason and this port had
        # left it live, which offered a switch that does nothing.
        gate = DSP_GATE if feature is Feature.HP_VIRTUALIZATION else {}
        out.append(Capability(
            key=feature_key(feature),
            kind=Kind.TOGGLE,
            label=label,
            group=GROUP_SOUND,
            section="Output" if feature is Feature.DIRECT_MODE else "Options",
            tier=Tier.COMMON if feature is Feature.DIRECT_MODE else Tier.ALL,
            description=_FEATURE_NOTES.get(feature, ""),
            **gate,
        ))

    # -- routing and Super X-Fi -------------------------------------------------------------
    out.append(Capability(
        key=KEY_OUTPUT,
        kind=Kind.CHOICE,
        label="Output",
        group=GROUP_SOUND,
        section="Output",
        tier=Tier.COMMON,
        choices=tuple(Choice(int(t), label) for t, label in OUTPUT_LABELS.items()),
        description="Where the card sends audio. The equaliser is tuned separately per output.",
    ))
    # Both Super X-Fi rows carry the DSP gate and neither depends on the other. Gating the mode on
    # the toggle looked reasonable and was wrong twice over: the initial sync never reads Super
    # X-Fi -- the vendor's own sync does not either, and the device only reports it when a
    # HardwareButton frame arrives -- so the toggle reads as unknown, and an unknown value is
    # falsy, which left the mode permanently greyed out on a working card.
    out.append(Capability(
        key=KEY_SXFI,
        kind=Kind.TOGGLE,
        label="Super X-Fi",
        group=GROUP_SOUND,
        section="Super X-Fi",
        tier=Tier.COMMON,
        **DSP_GATE,
    ))
    out.append(Capability(
        key=KEY_SXFI_MODE,
        kind=Kind.CHOICE,
        label="Super X-Fi Mode",
        group=GROUP_SOUND,
        section="Super X-Fi",
        choices=tuple(Choice(code, name) for code, name in SXFI_MODES.items()),
        description=(
            "Only these two modes were seen on the wire. Creative's app offers more and their "
            "codes are unknown, so they are not offered here rather than guessed."
        ),
        **DSP_GATE,
    ))

    # -- equaliser --------------------------------------------------------------------------
    if _has_equalizer(subfeatures, has_equalizer_state):
        out.extend(_equalizer(presets or {}, profile_names or {}))

    # -- readouts ---------------------------------------------------------------------------
    out.append(Capability(key=KEY_FIRMWARE, kind=Kind.READOUT, label="Firmware",
                          group=GROUP_DEVICE, section="Identity"))
    out.append(Capability(key=KEY_SERIAL, kind=Kind.READOUT, label="Serial Number",
                          group=GROUP_DEVICE, section="Identity", copyable=True))
    # Volume and mute are the card's own analogue level, which the vendor app shows on its Device
    # page and this port had dropped. Read-only on purpose: on Linux the playback level belongs to
    # PipeWire, and a second slider claiming the same job is how two volume controls end up
    # fighting. This says what the *card* is doing.
    out.append(Capability(key=KEY_VOLUME, kind=Kind.READOUT, label="Volume",
                          group=GROUP_DEVICE, section="Identity", unit="dB"))
    out.append(Capability(key=KEY_MUTED, kind=Kind.READOUT, label="Muted",
                          group=GROUP_DEVICE, section="Identity"))

    # What the firmware itself reports it can do. This is the honest answer to "why is setting X
    # not here?" -- anything absent from these masks is not reachable over USB at all -- and the
    # vendor-app comparison that prompted it is exactly the question a user asks.
    out.append(Capability(
        key=KEY_FEATURE_MASK, kind=Kind.READOUT, label="Feature mask",
        group=GROUP_DEVICE, section="Reported by the device", copyable=True,
        description="Which of the toggles above this unit's firmware implements.",
    ))
    out.append(Capability(
        key=KEY_DSP_MASK, kind=Kind.READOUT, label="DSP mask",
        group=GROUP_DEVICE, section="Reported by the device", copyable=True,
        description=(
            "Which effects run on the card. Everything else Creative's application offers -- "
            "Crystalizer, Surround, Smart Volume, X-Bass, Dialog Plus, VoiceFX -- runs on the "
            "host as a Windows audio processing object and never reaches the device."
        ),
    ))
    return out


def _has_equalizer(subfeatures: SubFeature | None, has_state: bool) -> bool:
    if subfeatures is not None and SubFeature.GRAPHIC_EQ not in subfeatures:
        return False
    return has_state


def _equalizer(presets: dict[str, object], profile_names: dict[int, str]) -> list[Capability]:
    """Enable, preset, preamp, the ten bands and the card's own profile slots.

    **Everything here is gated on Direct Mode and on nothing else.** An earlier version of this
    port also gated the preset, the preamp and all ten bands on the Graphic Equalizer toggle,
    which greyed out the entire tab for anyone whose card had the equaliser switched off -- the
    factory state. It was not the source's behaviour and it was not the vendor's: both leave the
    curve editable while it is off, which is the only way to build one before turning it on.
    """
    curve = tuple([KEY_EQ_PREAMP, *(band_key(i) for i in range(EQ_BAND_COUNT))])
    out = [
        Capability(
            key=KEY_EQ_ENABLED,
            kind=Kind.CHOICE,
            label="Equalizer",
            group=GROUP_EQ, section="Equalizer", tier=Tier.COMMON,
            choices=(
                Choice(EQ_OFF, "Off"),
                *profile_choices(profile_names),
            ),
            description=(
                "Off, or one of the four modes the card stores itself — the same ones the button "
                "on the front cycles through, a colour each. The card reports which one is live, "
                "so this follows the button as well as driving it."
            ),
            # Choosing a stored profile replaces the curve on the device, so the sliders have to
            # be re-read. "Off" and "On" leave it alone, and re-reading them costs nothing.
            refreshes=curve,
            **DSP_GATE,
        ),
    ]
    if presets:
        # The vendor's own order, not alphabetical: `presets.load()` sorts by the vendor `Order`
        # field, which groups the list the way the Windows app presents it -- tone shapes, then
        # music genres, then anything imported. Re-sorting by name here threw that away.
        out.append(Capability(
            key=KEY_EQ_PRESET,
            kind=Kind.CHOICE,
            label="Preset",
            group=GROUP_EQ,
            section="Equalizer",
            tier=Tier.COMMON,
            choices=tuple(Choice(name, name) for name in presets),
            description=(
                "Writes the whole curve band by band, as Creative's own app does. Presets carry "
                "separate speaker and headphone tunings; the one written follows Output."
            ),
            # Eleven sliders move when this is applied. Without saying so the page kept showing
            # the previous curve until a manual refresh, which looks exactly like a preset that
            # did not apply -- and it had.
            refreshes=curve,
            **DSP_GATE,
        ))
    out.append(Capability(
        key=KEY_EQ_PREAMP, kind=Kind.RANGE, label="Preamp",
        group=GROUP_EQ, section="Equalizer",
        minimum=EQ_GAIN_MIN, maximum=EQ_GAIN_MAX, step=0.5, unit="dB",
        **DSP_GATE,
    ))
    for index, hz in enumerate(presets_mod.BAND_FREQUENCIES[:EQ_BAND_COUNT]):
        out.append(Capability(
            key=band_key(index),
            kind=Kind.RANGE,
            label=f"{hz} Hz" if hz < 1000 else f"{hz // 1000} kHz",
            group=GROUP_EQ,
            section="Bands",
            minimum=EQ_GAIN_MIN, maximum=EQ_GAIN_MAX, step=0.5, unit="dB",
            **DSP_GATE,
        ))
    out.extend(_profile_slots(profile_names, curve))
    return out


def profile_choices(names: dict[int, str]) -> tuple[Choice, ...]:
    """The four modes the card stores, named where the model is known.

    They are **not** numbered slots and calling them "Profile 1" to "Profile 4" was the complaint
    that produced this: on a Sound Blaster X4 they are fixed modes with fixed meanings, and the
    button on the front cycles them with a colour for each. The name is the whole content of the
    choice, so the number is gone from the label — nothing a user does here is addressed by number.

    A model with no entry in :data:`PROFILE_NAMES` still gets numbers, because this module matches
    on vendor id alone and a confident wrong name is worse than an honest number.
    """
    return tuple(
        Choice(index, names.get(index) or f"Profile {index + 1}")
        for index in range(catalogue.PROFILE_SLOTS)
    )


def _profile_slots(names: dict[int, str], curve: tuple[str, ...]) -> list[Capability]:
    """Storing a curve into one of the four slots the card keeps itself.

    Recalling one is not here: it is a value of the Equalizer selector above, because choosing a
    stored profile and turning the equaliser on are the same act on this hardware.

    Storing is what this application cannot give a user any other way -- a curve written into a
    slot survives with no software running. The vendor app writes a name (command 24) and then
    preamp plus all ten gains in a single frame (command 23), and
    :meth:`CreativeController.store_profile` does the same two writes.

    A dialog rather than a row, because it takes a name as well as a slot, and a name field sitting
    on the form beside a slot dropdown says nothing about which button it belongs to -- the same
    reasoning that put the YubiKey slot modifiers into their dialogs.
    """
    return [
        Capability(
            key=KEY_PROFILE_STORE, kind=Kind.ACTION, label="Current curve",
            action_label="Store in the card…", group=GROUP_EQ, section="Device profiles",
            prompt_fields=(
                PromptField(key="name", label="Name", kind=Kind.TEXT,
                            max_length=catalogue.PROFILE_NAME_MAX),
                PromptField(key="slot", label="Mode", kind=Kind.CHOICE,
                            choices=profile_choices(names), default=0),
            ),
            prompt_detail=(
                "Writes the sliders above into one of the card's own four modes, under a name of "
                "your choosing. A curve stored there stays on the card with no software running — "
                "and it replaces that mode, which on a factory card is one of the four the button "
                "on the front cycles through."
            ),
            **DSP_GATE,
        ),
    ]


#: Wording for the features whose effect is not obvious from the name. Kept short: a description
#: that restates the label is noise, and the source project learned each of these on hardware.
_FEATURE_NOTES = {
    Feature.DIRECT_MODE: (
        "Bypasses the DSP entirely for bit-perfect output. The equaliser and every other effect "
        "stop applying while this is on."
    ),
    Feature.HP_HIGH_GAIN: "For high-impedance headphones. Loud on sensitive ones.",
    Feature.SAVE_HP_HIGH_GAIN: "Keep the gain setting across power cycles.",
    Feature.HP_VIRTUALIZATION: "Applies headphone virtualisation to the Line Out.",
}


__all__ = ["build", "band_index", "band_key", "feature_key", "profile_choices",
           "DSP_GATE", "EQ_OFF", "OUTPUT_LABELS",
           "KEY_DIRECT_MODE", "KEY_DSP_MASK", "KEY_EQ_ENABLED", "KEY_EQ_PREAMP", "KEY_EQ_PRESET",
           "KEY_FEATURE_MASK", "KEY_FIRMWARE", "KEY_MUTED", "KEY_OUTPUT",
           "KEY_PROFILE_STORE", "KEY_SERIAL", "KEY_SXFI", "KEY_SXFI_MODE", "KEY_VOLUME"]
