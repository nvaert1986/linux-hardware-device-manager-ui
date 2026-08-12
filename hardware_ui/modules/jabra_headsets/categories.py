"""Grouping the 423 catalogue properties into user-facing sections, and naming them.

The catalogue is protocol-shaped: ``autogenName`` prefixes are CONFIG / IDENT / VIDEO / STATUS,
which tells a user nothing. This maps property names onto sections a person would recognise, by
ordered keyword rules — so a property added by a future catalogue version lands somewhere
sensible without code changes, rather than vanishing.

Order matters: the first matching rule wins, so put narrow patterns before broad ones.

``label_for`` here is the **fallback** only. Jabra's own wording lives in ``labels.py``, taken from
their Android apps' string pools; this is what names a property that map does not cover. It keeps
the catalogue's acronyms upper-case so the result does not read as "Anc" or "Bluetooth le".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Sections, in display order. Anything unmatched falls through to "Other".
CATEGORY_ORDER = [
    "Device",
    "Sound",
    "ANC & HearThrough",
    "Microphone",
    "Buttons & Sensors",
    "Bluetooth",
    "Calls & Softphone",
    "Power",
    "Video",
    "Network",
    "Other",
]

#: Regexes tried in order; first hit assigns the category.
_RULES: list[tuple[str, str]] = [
    # Narrow first ------------------------------------------------------------------------
    ("ANC & HearThrough", r"^anc|hearthrough|windnoise|ambience|sealingtest"),
    ("Microphone", r"^microphone|sidetone|earcupmic|boomarm|mute|noisesuppress|"
                   r"whitenoise|intellitone"),
    ("Buttons & Sensors", r"button|onhead|touchsensor|motionsensor|voiceassist|ama|bisto|"
                          r"remotemmi|tap$|hold$"),
    ("Bluetooth", r"^bluetooth|^bt|pairing|autopair|^dect|wireless|radiopower|link"),
    ("Calls & Softphone", r"softphone|ringer|ringtone|call|hook|busylight|presence|line|"
                          r"audiotype|leaudio|teams|hardphone|deskphone|mobile"),
    ("Power", r"power|battery|idle|standby|sleep|inactivity|charg"),
    ("Video", r"^camera|video|zoom|whiteboard|pan$|tilt|whitebalance|fieldofview|"
              r"peoplecount|roomboundar|gallery|composition|hdr|flicker"),
    ("Network", r"wlan|ethernet|network|proxy|certificate|xpress|dhcp|^ip"),
    ("Sound", r"equali|sound|audio|volume|mysound|tone|language|voiceprompt|guidance"),
    ("Device", r"^name$|^pid$|serial|firmware|^sku|variant|version|reset|factory|"
               r"^devicename|asset|log|diagnostic|datetime|region|wizard"),
]

_COMPILED = [(name, re.compile(pattern, re.I)) for name, pattern in _RULES]

#: Properties that are destructive or make no sense as a generic control. A generic
#: catalogue-driven UI must never offer these — a stray click would reset the device or start a
#: firmware operation. Same reasoning as the Poly project's refusal to expose write-only actions.
DANGEROUS = re.compile(
    r"factoryreset|^reset|firmwareupdate|^fwu|^dfu|erase|clearpair|"
    r"restoredefault|creatediagnostic|panic|selftest|hearingtestactivate|"
    r"sealingtestactivate|playringtone|uploadringtone|uploadimage|"
    r"certificate|password|provisioning",
    re.I,
)


@dataclass(frozen=True)
class Grouped:
    """Property names bucketed by section, and the ones deliberately withheld."""

    by_category: dict[str, list[str]]
    withheld: list[str]

    def categories(self) -> list[str]:
        """Populated categories, in display order."""
        return [c for c in CATEGORY_ORDER if self.by_category.get(c)]


def categorise(name: str) -> str:
    for category, pattern in _COMPILED:
        if pattern.search(name):
            return category
    return "Other"


def is_dangerous(name: str) -> bool:
    return bool(DANGEROUS.search(name))


def group(names: list[str], *, include_dangerous: bool = False) -> Grouped:
    """Bucket property names into sections, withholding destructive ones."""
    by_category: dict[str, list[str]] = {}
    withheld: list[str] = []
    for name in names:
        if not include_dangerous and is_dangerous(name):
            withheld.append(name)
            continue
        by_category.setdefault(categorise(name), []).append(name)
    for bucket in by_category.values():
        bucket.sort()
    return Grouped(by_category=by_category, withheld=sorted(withheld))


def label_for(name: str) -> str:
    """A readable label from a camelCase property name.

    `ancHearThroughLevel` -> "ANC hear through level". Acronyms the catalogue uses are kept
    upper-case so the result does not read as "Anc" or "Bluetooth le".
    """
    spaced = re.sub(r"(?<!^)(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])", " ", name)
    words = spaced.split()
    acronyms = {"anc": "ANC", "bt": "BT", "le": "LE", "dect": "DECT", "pc": "PC",
                "usb": "USB", "hid": "HID", "wlan": "WLAN", "ip": "IP", "sku": "SKU",
                "fw": "FW", "id": "ID", "ama": "AMA", "hdr": "HDR", "dsp": "DSP"}
    out = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if lowered in acronyms:
            out.append(acronyms[lowered])
        elif index == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(lowered)
    return " ".join(out)
