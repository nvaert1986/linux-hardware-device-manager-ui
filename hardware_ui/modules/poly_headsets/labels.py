"""Poly's own wording for settings and their values.

Ported verbatim from ``plasma-hp-poly-protocol-headphone-support/plasma_poly_headset/gui.py``
(the ``SETTING_GROUPS`` .. ``group_for`` section). These tables are not decoration: a catalogue's
``settingName`` is an internal identifier -- ``enableOLI``, ``G616``, ``twa`` -- and the readable
names are Poly's own, taken from Poly Studio's English translation block rather than invented.

The resolution order matters and is load-bearing: labels are resolved **per setting,
all-or-nothing**, because mixing sources produced dropdowns reading "1", "5 minutes", "6".

The two JSON tables are vendor-derived and therefore imported rather than shipped; every lookup
falls back to ``humanize()``, so an installation that has never run the import still reads
sensibly. The catalogues are different -- those carry message ids, and the module is useless
without them.
"""

from __future__ import annotations

import json
import logging
import re

from hardware_ui.core.paths import vendor_dir

from .protocol import catalogue as cat

log = logging.getLogger(__name__)

SETTING_GROUPS: dict[str, tuple[str, ...]] = {
    "Audio": (
        "sideToneLevel", "audioBandwidthMobile", "volumeLevelTone", "volumeToneMinMax",
        "muteTone", "independentVolumeControl",
    ),
    "Mute": ("muteAlert", "muteReminderFrequency", "muteOnOffTransitionPrompt"),
    "Calls & Prompts": (
        "answeringCallVP", "incomingCallAlert", "secondInboundCall", "scoTone",
        "enableNotificationTones", "enableOLI", "callerID",
    ),
    "Ringtones": ("ringToneMobile", "ringToneVoip"),
    "Hearing Safety": (
        "G616", "twa", "twaPeriod", "acousticIncidentReporting", "aalTwaReporting",
        "aalTwaReportingTimePeriod", "conversationDynamicsReporting",
        "conversationDynamicsReportingTimePeriod", "linkQualityReporting",
    ),
}
GROUP_ORDER = (
    "Audio", "Mute", "Calls & Prompts", "Ringtones", "Hearing Safety", "General",
)

#: Value names that read as a plain on/off switch rather than a list.
BOOLEAN_CHOICES = ({"true", "false"}, {"on", "off"}, {"enabled", "disabled"})


#: Words to render with their established capitalisation rather than as plain prose.
_ACRONYMS = {"voip": "VoIP", "sco": "SCO", "oli": "OLI", "twa": "TWA", "vp": "VP",
              "id": "ID", "aal": "AAL", "hd": "HD"}

#: Imported from the user's own copy of Poly Studio, never shipped -- see
#: docs/POLY_UI_BEHAVIOUR.md §11.
_DATA = vendor_dir("poly_headsets")


def _optional_json(name: str) -> dict:
    """Load a cosmetic data file, tolerating its absence.

    The label tables only improve wording — every lookup falls back to humanize() — so a build
    that omits them must still run. The device catalogues are different: those carry the message
    ids and payload types, and the app is useless without them.
    """
    try:
        return json.loads((_DATA / name).read_text())
    except FileNotFoundError:
        # Absent is the normal case: no tool in this tree produces these, and the built-in table
        # below already covers the settings a headset actually exposes. A warning on every connect
        # for a file that has no producer trains people to ignore warnings.
        log.debug("%s not present — using the built-in labels", name)
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("%s is unreadable — falling back to generated labels", name)
        return {}


#: Poly Studio's own English UI strings, keyed by the *set* of a setting's option names — the
#: outer keys in Poly's i18n are display names that do not match catalogue settingNames, but the
#: option keys do. Produced by ``tools/extract_ui_labels.py`` from an installer the user supplies;
#: optional enrichment, so the built-in table below still labels a page without it. Boolean
#: signatures are excluded, as every boolean shares them; those render as checkboxes anyway.
_UI_LABELS: dict[str, dict] = _optional_json("ui_labels.json")


#: Second source: Poly Lens Desktop's DeviceSetting.json, inverted to value -> label. Covers some
#: settings the i18n signature match misses ("Ring Once", "Minimum & Maximum Only"). Ambiguous
#: keys are dropped at extraction time. Optional, and there is deliberately no extractor for it:
#: Lens Desktop is a separate product, and a tool that has never been run against the real file
#: would be a guess. Drop the file in by hand if you have one.
_FALLBACK_LABELS: dict[str, str] = _optional_json("value_labels.json")


#: Poly's own English UI wording for settings whose catalogue name is an internal identifier.
#: Taken verbatim from Poly Studio's English translation block (see tools/extract_ui_labels.py) —
#: the app resolves these server-side, so there is no table in the bundle to match automatically.
#: Descriptions are Poly's too, where they add something a user would not already know.
SETTING_LABELS: dict[str, tuple[str, str]] = {
    "enableOLI": ("Online Indicator", "The light that shows others you are on a call."),
    "G616": (
        "Anti-Startle",
        "Acoustic shock protection. G616 is an Australian industry guideline that limits the "
        "sound level reaching your ear.",
    ),
    "scoTone": ("Active Audio Tone", "A tone when the audio channel to the headset opens."),
    "twa": (
        "Noise Exposure",
        "Limits your daily average sound exposure. Choose a limit or turn limiting off.",
    ),
    "twaPeriod": (
        "Hours on Phone per Day",
        "How long you typically wear the headset, used to calculate noise exposure.",
    ),
    "muteReminderFrequency": ("Mute Reminder Timing", "Minutes between mute reminder alerts."),
    "muteTone": ("Mute On/Off Alerts", ""),
    "secondInboundCall": ("Second Incoming Call", ""),
    "sideToneLevel": ("Sidetone", "How much of your own voice you hear in the headset."),
    "enableNotificationTones": ("Notification Tones", ""),
    "answeringCallVP": ("Answering Call", 'Hear the "answering call" voice prompt.'),
    "incomingCallAlert": ("Incoming Call", ""),
    "audioBandwidthMobile": ("HD Voice", "Wideband audio on the mobile connection."),
    "volumeLevelTone": ("Volume Level Tones", ""),
    "volumeToneMinMax": ("Volume Min/Max Alerts", ""),
    "linkQualityReporting": ("Link Quality Reporting", ""),
    "intellistand": ("Auto-Answer (No Sensor)", ""),
    "ringToneMobile": ("Ringtone (Mobile Phone)", ""),
    "ringToneVoip": ("Ringtone (PC)", ""),
    "callerID": ("Caller ID", "Hear the caller's name announced."),
    "independentVolumeControl": ("Independent Volume Control", ""),
    "wearingStateSensorEnabled": ("Wearing Sensor", ""),
    "autoAnswerOnDon": ("Auto-Answer", "Answer by putting the headset on."),
    "restoreDefaults": ("Restore Defaults", ""),
    "clearPairedDevices": ("Clear Paired Devices", ""),
    "clearTrustedDeviceList": ("Clear Trusted Devices", ""),
    "ringTone": ("Ringtone On/Off", ""),
    "acousticIncidentReporting": ("Acoustic Incident Reporting", ""),
    "aalTwaReporting": ("Noise Exposure Reporting", ""),
    "aalTwaReportingTimePeriod": ("Noise Exposure Reporting Interval", ""),
    "conversationDynamicsReporting": ("Conversation Dynamics Reporting", ""),
    "conversationDynamicsReportingTimePeriod": ("Conversation Dynamics Interval", ""),
    "Call Announcement": ("Call Announcement", ""),
}


#: Per-setting value wording, again Poly's own, for lists the automatic matching cannot reach
#: (their i18n keys these by English label rather than by the catalogue's value name).
SETTING_VALUE_LABELS: dict[str, dict[str, str]] = {
    "twa": {"off": "No limiting", "85db": "Limit at 85 dBA", "80db": "Limit at 80 dBA"},
    "twaPeriod": {"2": "2 hours", "4": "4 hours", "6": "6 hours", "8": "8 hours"},
}


def _ui_entry(setting: cat.Setting) -> dict | None:
    return _UI_LABELS.get("|".join(sorted(c.name for c in setting.choices)))


def _minute_labels(setting: cat.Setting) -> dict[str, str] | None:
    """Labels for settings whose choices are a plain count of minutes.

    Poly's i18n keys these by the English label ("5 minutes") rather than by the catalogue value
    name ("5"), so signature matching cannot reach them. The payload is unambiguous though — a
    single UNSIGNED_SHORT of seconds — so derive the label from the data instead of guessing.
    """
    out: dict[str, str] = {}
    for choice in setting.choices:
        fields = choice.fields
        if len(fields) != 1 or fields[0].get("type") != "UNSIGNED_SHORT":
            return None
        try:
            seconds = int(str(fields[0].get("value")), 0)
        except (TypeError, ValueError):
            return None
        if seconds <= 0 or seconds % 60:
            return None
        minutes = seconds // 60
        out[choice.name] = "1 minute" if minutes == 1 else f"{minutes} minutes"
    return out or None


def setting_label(setting: cat.Setting) -> str:
    """Display name for a setting — Poly's own wording where we have it."""
    curated = SETTING_LABELS.get(setting.name)
    if curated:
        return curated[0]
    entry = _ui_entry(setting)
    return (entry or {}).get("name") or humanize(setting.name)


def setting_description(setting: cat.Setting) -> str:
    curated = SETTING_LABELS.get(setting.name)
    if curated and curated[1]:
        return curated[1]
    entry = _ui_entry(setting)
    described = (entry or {}).get("description") or setting.description
    # Poly's catalogues carry copy-pasted descriptions (many settings claim to be about volume
    # increment tones); those are worse than nothing.
    if described.startswith("Configuration of how a device plays volume"):
        return ""
    return described


def value_labels(setting: cat.Setting) -> dict[str, str]:
    """Labels for one setting's values.

    Resolved per setting and all-or-nothing: mixing sources produced inconsistent lists like
    "1", "5 minutes", "6" in the same dropdown.
    """
    curated = SETTING_VALUE_LABELS.get(setting.name)
    if curated and all(c.name in curated for c in setting.choices):
        return {c.name: curated[c.name] for c in setting.choices}
    derived = _minute_labels(setting)
    if derived:
        return derived
    entry = _ui_entry(setting)
    if entry:
        options = entry["options"]
        if all(c.name in options for c in setting.choices):
            return {c.name: options[c.name] for c in setting.choices}
    if all(c.name in _FALLBACK_LABELS for c in setting.choices):
        return {c.name: _FALLBACK_LABELS[c.name] for c in setting.choices}
    return {c.name: humanize(c.name) for c in setting.choices}


def humanize(name: str) -> str:
    """'sideToneLevel' -> 'Side tone level'; 'ringToneVoip' -> 'Ring tone VoIP'.

    Sentence case per the KDE HIG, but tokens that are already all-caps (G616) or known acronyms
    keep their casing.
    """
    if not name:
        return name
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    # "sound1" -> "sound 1", but leave all-caps tokens like G616 alone.
    spaced = re.sub(r"(?<=[a-z])(?=\d)", " ", spaced)
    words = spaced.replace("_", " ").split()
    if not words:
        return name

    out = []
    for i, word in enumerate(words):
        if word.lower() in _ACRONYMS:
            out.append(_ACRONYMS[word.lower()])
        elif word.isupper() or any(ch.isdigit() for ch in word):
            out.append(word)          # G616, 3D, etc.
        elif i == 0:
            out.append(word[0].upper() + word[1:])
        else:
            out.append(word.lower())
    return " ".join(out)


def group_for(setting_name: str) -> str:
    for group, members in SETTING_GROUPS.items():
        if setting_name in members:
            return group
    # "Other" is what a catch-all gets called by whoever is not going to read it.
    # These are ordinary device settings -- pairing, dial tone -- so: General.
    return "General"
