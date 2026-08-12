"""Grouping Logitech settings into sections, and naming them.

**The wording is Solaar's, not ours.** Every setting carries a ``label`` and usually a
``description``, written for users and translated upstream — "Sensitivity (DPI)", "Scroll Wheel
Ratchet Speed". Regenerating those from internal names would be strictly worse and would drift from
what every other Linux tool calls the same setting. ``label_for`` uses theirs and only falls back
to a generated label when a setting has none.

**The grouping is ours.** Solaar's own window shows one flat list, which is fine for a small
dialog and poor for a tabbed page. These rules bucket settings by keyword, in order, so a setting
added by a future Solaar version lands somewhere sensible instead of vanishing.

Order matters: the first matching rule wins, so narrow patterns come before broad ones.
"""

from __future__ import annotations

import re
from typing import Any

#: Sections, in display order. Anything unmatched falls through to "Other".
GROUP_ORDER = [
    "Info",
    "Pointer",
    "Scrolling",
    "Keys & Buttons",
    "Lighting",
    "Sound & Haptics",
    "Power",
    "Device",
    "Pairing",
    "Other",
]

#: Regexes tried in order against the setting's internal name; first hit assigns the section.
_RULES: list[tuple[str, str]] = [
    # Narrow first ------------------------------------------------------------------------
    # ``hires-`` is the wheel too: measured on an MX Master 3S, ``hires-smooth-invert`` and
    # ``hires-smooth-resolution`` are Scroll Wheel Direction and Resolution, and neither name
    # contains "scroll" -- so both landed in Other, next to their five siblings in Scrolling.
    ("Scrolling", r"scroll|smart-shift|ratchet|crown|thumb|^hires|^lowres|wheel"),
    ("Pointer", r"^dpi|pointer_speed|report_rate|speed-change|force-sensing|hand-detection|"
                r"sensitivity"),
    ("Keys & Buttons", r"key|button|fn-swap|gesture|multiplatform|remap|divert"),
    ("Lighting", r"backlight|led|rgb|lighting|brightness"),
    ("Sound & Haptics", r"equalizer|sidetone|haptic"),
    ("Power", r"power|battery|sleep|idle"),
    ("Device", r"change-host|onboard|profile|^device"),
]

_COMPILED = [(name, re.compile(pattern, re.I)) for name, pattern in _RULES]


def group_for(name: str) -> str:
    for group, pattern in _COMPILED:
        if pattern.search(name):
            return group
    return "Other"


#: Friendlier names for map keys whose own label is an abbreviation. ``dpi_extended`` calls its
#: three keys X, Y and LOD, which are exact and mean nothing to most people; everything else --
#: "Middle Button", "Volume Up" -- is already written for users and is left alone.
_KEY_LABELS = {
    "dpi_extended": {
        "X": "Horizontal (DPI)",
        "Y": "Vertical (DPI)",
        "LOD": "Lift-off distance",
    },
}

#: Maps that are shown but never written, and why. Diversion is only half a mechanism: it stops a
#: key doing its normal job so that *software* can decide what it does instead. That software is a
#: resident rule engine, which this application is not and does not ship -- so every value except
#: "Regular" would leave the key doing nothing at all.
#:
#: Read-only rather than hidden, because the state is worth seeing: if Solaar has diverted a key,
#: this page should say so rather than implying the key is untouched.
READ_ONLY_MAPS = {"divert-keys", "gesture2-divert"}

NOTE_DIVERSION = (
    "Shown for information — this application does not change it.\n\n"
    "Diverting a key stops it doing its normal job and hands the event to software, which then "
    "decides what it should do. That is a resident background program, not a device setting: the "
    "key itself is only being told to stay quiet. Everything on this page is hardware "
    "configuration written to the device, so this is outside what it does.\n\n"
    "For key remapping, gestures and Sliding DPI, install and run Solaar "
    "(app-misc/solaar), which has the rule engine these values need."
)

#: Said once per map, on its first row.
_MAP_NOTES = {
    "divert-keys": NOTE_DIVERSION,
    "gesture2-divert": NOTE_DIVERSION,
}


def key_label(setting_name: str, key: Any) -> str:
    """The label for one entry of a per-key map."""
    text = str(key)
    return _KEY_LABELS.get(setting_name, {}).get(text, text)


def map_note(setting_name: str) -> str:
    """A warning to put on a map's first row, or "" for the ones that need none."""
    return _MAP_NOTES.get(setting_name, "")


def label_for(setting: Any) -> str:
    """Solaar's own label, falling back to something readable from the internal name."""
    label = (getattr(setting, "label", "") or "").strip()
    if label:
        return label
    return generated_label(getattr(setting, "name", "") or "")


def description_for(setting: Any) -> str:
    """Solaar's description, flattened to one line.

    Several are written as multi-line help text, and a control's description is a single line here.
    """
    text = (getattr(setting, "description", "") or "").strip()
    return " ".join(text.split()) if text else ""


def choice_label(choice: Any) -> str:
    """A choice's display text.

    Solaar's choices are ``NamedInt`` — an integer that prints as its name — so ``str`` is already
    the right answer and reinventing it would lose the vendor's wording.
    """
    return str(choice)


def generated_label(name: str) -> str:
    """``hires-smooth-invert`` -> "Hires smooth invert", keeping known acronyms upper-case."""
    words = re.split(r"[-_\s]+", name.strip())
    acronyms = {"dpi": "DPI", "led": "LED", "rgb": "RGB", "os": "OS", "mr": "MR",
                "adc": "ADC", "hi": "Hi", "fn": "Fn"}
    out = []
    for index, word in enumerate(w for w in words if w):
        lowered = word.lower()
        if lowered in acronyms:
            out.append(acronyms[lowered])
        elif index == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(lowered)
    return " ".join(out) or name


__all__ = [
    "GROUP_ORDER",
    "NOTE_DIVERSION",
    "READ_ONLY_MAPS",
    "key_label",
    "map_note",
    "choice_label",
    "description_for",
    "generated_label",
    "group_for",
    "label_for",
]
