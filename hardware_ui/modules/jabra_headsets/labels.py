"""Human labels for catalogue properties, and value formatting.

Label sources, in order of authority:

1. **Jabra's Android apps** — `com.jabra.moments` and `com.jabra.plus`. Their `resources.arsc`
   string pools parse with `jabra-re/apk/extract_strings.py` (no apktool needed: the pool holds
   the localized values, and resource *keys* would not match GNP property names anyway). 3 334
   usable strings, and the wording below marked "vendor" comes from there. Dutch and other
   locales are available the same way, from the `split_config.<lang>.apk` files.
2. **Hand-written**, for anything the consumer apps do not cover. That is a real gap: IntelliTone,
   wireless range and audio guidance are business-headset features that appear only in Jabra
   Direct on Windows, whose strings live in its Electron JS bundles rather than in any shipped
   resource file.

Jabra's per-model labels are **not** in the Windows installer either. They come from the cloud
device configuration —
`.../deviceconfiguration/{pid}/localizedtext?type=..&firmwareVersion=..`, advertised in the
`links` list of `devicecapabilities.jabra.com/v4/DeviceConfiguration`. That is per-model,
per-firmware and needs a network round trip, and on the test hardware it answers 404. So this
module carries a hand-written override map for the properties a user actually sees, with
`categories.label_for()` as the fallback for everything else.

Ported wholesale into this application: this *is* the label layer, and dropping it in favour of
``categories.label_for`` would replace Jabra's own wording ("Noise cancelling mode") with a
mechanical rendering of the protocol name ("ANC mode") on every control.

Also here: value formatting the generic widget layer cannot infer, notably language identifiers.
`currentLanguage` and `currentLanguageCode` both decode a raw **Microsoft LCID** (1033 = en-US)
even though the catalogue declares an 18-entry enum for the latter — so the enum must not be
trusted as the value space.
"""
from __future__ import annotations

import re

from .categories import label_for as _generic

#: Overrides where the auto-generated label is wrong, cryptic, or ambiguous.
OVERRIDES: dict[str, str] = {
    # ANC / ambience
    "ancMode": "Noise cancelling mode",
    "ancModePcApp": "Noise cancelling mode (set by app)",
    "ancAmbienceMode": "Ambience mode",
    "ancLevel": "Noise cancelling strength",
    "hearThroughLevel": "HearThrough level",
    "ancHearThroughMixEnabled": "Mix HearThrough with audio",
    "ancSoundModeLoop": "Modes cycled by the ANC button",
    "windNoiseReductionEnabled": "Wind noise reduction",
    # microphone / mute
    "boomArmPosition": "Boom arm position",
    "boomArmRotateAcceptCall": "Answer calls using boom arm",
    "boomArmRotateInCallAction": "Boom arm notification type",
    "microphoneMuteState": "Microphone muted",
    "earcupMicsEnabled": "Use earcup microphones",
    "sidetoneLevelDsp": "Sidetone level",
    "sidetoneEnabledDsp": "Sidetone",
    "muteReminderInterval": "Mute reminder interval",
    "intellitoneLevel": "Hearing protection (IntelliTone)",
    # sound
    "equalizerConfig": "Music equalizer",
    "soundMode": "Sound preset",
    "soundMode2": "Sound preset (secondary)",
    "voicePrompts": "Audio guidance",
    "buttonSoundsEnabled": "Button tones",
    "currentLanguage": "Voice prompt language",
    "currentLanguageCode": "Voice prompt language (code)",
    "availableLanguages": "Languages on the device",
    "leAudioCallProfile": "Call audio quality (LE Audio)",
    "automaticAudioDetection": "Detect when audio is active",
    # sensors / buttons
    "onHeadDetectionEnabled": "On-head detection",
    "onHeadDetectionStatus": "Headset is being worn",
    "autoPauseMusicOnHeadEnabled": "Pause music when taken off",
    "autoMuteCallAudio": "Mute call when taken off",
    "soundModeButtonFunction": "Function of the mode button",
    # Button remapping. The generic fallback mangles the digits and Jabra's abbreviations:
    # "Button function3 dots", "Button function mfb". MFB is Jabra's "multi-function button";
    # the "3 dots"/"4 dots" names refer to the moulded dot markings on Engage deskphone headsets.
    "button1Tap": "Button 1 — single tap",
    "button2Tap": "Button 2 — single tap",
    "button3Tap": "Button 3 — single tap",
    "buttonFunction3Dots": "Three-dot button",
    "buttonFunction4Dots": "Four-dot button",
    "buttonFunctionMfb": "Multi-function button",
    "buttonFunctionMute": "Mute button function",
    "callAndMediaButtonFunction": "Call and media button",
    "muteAndVoiceAssistantButtonFunction": "Mute / voice-assistant button",
    "eHookProtocol": "Deskphone hook-switch protocol",
    "answerCallGestureEnabled": "Answer calls by gesture",
    "autoAnswerCallOnHeadEnabled": "Answer calls by putting the headset on",
    "hsMotionSensorAnswerOnPickupEnabled": "Answer calls by picking the headset up",
    "touchSensorVolumeFeatureEnabled": "Touch volume control",
    "hsMotionSensorVolumeButtonInvertEnabled": "Invert volume buttons by orientation",
    # bluetooth / link
    "bluetoothName": "Bluetooth Name",
    "bluetoothName2": "Bluetooth Name (alternate)",
    "bluetoothMacAddress": "Bluetooth address",
    "bluetoothRadioEnabled": "Bluetooth radio",
    "bluetoothLinkQuality": "Link quality",
    "autoPairingEnabled": "Automatic pairing",
    "radioPower": "Wireless range",
    "publicModeEnabled": "Public mode",
    # calls / softphone
    "softphoneIntegrationEnabled": "Softphone integration",
    "prioritizedComputerAudioEnabled": "Prioritise computer audio",
    "inCallBusyLightEnabled": "Busylight",
    "autoRejectBgWaitingEnabled": "Auto-reject waiting calls",
    "callAcceptedSound": "Call-accepted tone",
    "usbBusy": "USB audio in use",
    # device / power
    "skuId": "SKU",
    "pid": "Product ID",
    "dfuProductId": "Firmware product ID",
    "batteryLevel": "Battery",
    "batteryLevelV2": "Battery detail",
    "isBatteryLow": "Battery low",
    "inactivityDelay": "Power-off after inactivity",
    "hidDevice": "HID control interface",
    "communicationProtocol": "USB audio protocol",
    "configChangeEvents": "Setting-change notifications",
}

#: Microsoft LCIDs, which is what the device reports for language. Only the languages the
#: catalogue's `currentLanguageCode` enum covers, plus the ones seen in `availableLanguages`.
LCID_NAMES: dict[int, str] = {
    1028: "Chinese (Traditional)",
    1029: "Czech",
    1030: "Danish",
    1031: "German",
    1033: "English (US)",
    1035: "Finnish",
    1036: "French",
    1038: "Hungarian",
    1040: "Italian",
    1041: "Japanese",
    1042: "Korean",
    1043: "Dutch",
    1044: "Norwegian (Bokmål)",
    1045: "Polish",
    1049: "Russian",
    1053: "Swedish",
    1055: "Turkish",
    2052: "Chinese (Simplified)",
    2057: "English (UK)",
    3082: "Spanish",
}

#: Properties whose integer value is an LCID rather than a quantity.
LANGUAGE_PROPERTIES = frozenset({"currentLanguage", "currentLanguageCode",
                                 "currentLanguageInConfigMode"})


#: Friendly names for enum *values*. The catalogue's identifiers are protocol-shaped
#: (`peakStopOnly`, `_0dB`, `level79`), so a dropdown built straight from them reads badly.
#: Keyed by property name, then raw value.
VALUE_LABELS: dict[str, dict[str, str]] = {
    "intellitoneLevel": {
        # Jabra's hearing-protection tiers. "G616" is the Australian/NZ AS/ACIF G616 limit;
        # PeakStop only clips sudden peaks without limiting average exposure.
        "peakStopOnly": "PeakStop only (clip sudden peaks)",
        "level1": "Level 1 — least limiting",
        "level2": "Level 2",
        "level3": "Level 3",
        "level4": "Level 4 — most limiting",
        "g616": "G616 (AS/ACIF G616 exposure limit)",
    },
    "intellitoneSoundLevel": {
        "level79": "79 dB", "level82": "82 dB", "level85": "85 dB",
        "level88": "88 dB", "level91": "91 dB",
    },
    "voicePrompts": {
        "none": "Off", "tone": "Tones only", "voice": "Spoken guidance",
    },
    "ancMode": {
        "off": "Off", "anc": "Noise cancelling",
        "hearThrough": "HearThrough", "hearThroughMix": "HearThrough mixed with audio",
    },
    "ancModePcApp": {
        "off": "Off", "on": "Noise cancelling",
        "htNotMixed": "HearThrough", "htMixed": "HearThrough mixed with audio",
    },
    "boomArmPosition": {
        "muted": "Parked up (muted)", "unmuted": "Down (live)", "parked": "Parked",
    },
    "automaticAudioDetection": {
        "disabled": "Never", "enabled": "When audio starts",
        "fast": "When audio starts (fast)", "always": "Always treat audio as active",
    },
    "leAudioCallProfile": {
        "usbAdapt": "Match the USB connection",
        "widebandMono": "Mono, wideband (voice)",
        "superWidebandMono": "Mono, super-wideband",
        "superWidebandStereo": "Stereo, super-wideband",
        "superWidebandStereo48K": "Stereo, super-wideband 48 kHz (best)",
    },
    "radioPower": {
        "veryLow": "Very short range (best battery)", "low": "Short range",
        "normal": "Normal range", "high": "Long range",
    },
    "soundMode": {"normal": "Neutral", "bass": "Bass boost", "treble": "Treble boost"},
    "soundMode2": {"normal": "Neutral", "bass": "Bass boost", "treble": "Treble boost"},
    "hidDevice": {
        "disable": "Disabled", "enable": "Enabled",
        "factoryResetEnable": "Enabled (factory default)",
        "factoryResetDisable": "Disabled (factory default)",
    },
    "communicationProtocol": {"standardHid": "Standard HID", "advancedCdc": "Advanced (CDC)"},
    "callAcceptedSound": {
        "soundEffects": "Sound effect", "prompts": "Spoken prompt", "off": "Silent",
    },
    "boomArmRotateInCallAction": {
        "none": "Nothing", "mute": "Mute the microphone", "endCall": "End the call",
    },
    "soundModeButtonFunction": {
        "noFunction": "Nothing", "callHandling": "Answer / end calls", "mute": "Mute",
        "speedDial": "Speed dial", "busylight": "Busylight", "pushToTalk": "Push to talk",
        "busylightHs": "Busylight (headset)", "msTeam": "Microsoft Teams",
        "music": "Play / pause", "soundMode": "Sound preset",
        "muteAndVoiceAssistant": "Mute, or voice assistant when held",
        "callAndMedia": "Calls and media",
    },
}

#: Units for bare integers, so a spin box does not read "15" with no idea of what.
#: Jabra Direct presents the inactivity timer in minutes and the mute reminder in seconds; both
#: ranges (0-255 and 0-120) fit that reading. Anything not listed gets no suffix rather than a
#: guessed one.
UNITS: dict[str, str] = {
    "inactivityDelay": " minutes",
    "powerNapDelay": " minutes",
    "muteReminderInterval": " seconds",
    "standbyModeInterval": " seconds",
    "hearThroughLevel": " dB",
    "sidetoneLevel": " dB",
    "ancLevel": "",
}


def unit(name: str) -> str:
    return UNITS.get(name, "")


def format_value(prop_name: str, value: object, kind: str = "") -> str:
    """How a **read-only** value is displayed. Never a disabled input widget.

    A greyed-out checkbox or a spin box you cannot change is worse than a plain label: it looks
    broken and invites clicking. So booleans read Yes/No, numbers carry their unit, and enums use
    the same friendly names as the dropdowns.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value_label(prop_name, value) if value else "—"
    if isinstance(value, (int, float)):
        if prop_name in LANGUAGE_PROPERTIES:
            return language_name(int(value))
        if "battery" in prop_name.lower() or kind == "percent":
            return f"{int(value)}%"
        return f"{value}{unit(prop_name)}"
    if isinstance(value, (list, tuple)):
        if prop_name in ("availableLanguages",):
            return ", ".join(language_name(int(v)) for v in value) or "—"
        return ", ".join(str(v) for v in value) or "—"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items()) or "—"
    return str(value)

#: Values shaped like `minus9dB` / `_0dB` / `plus6dB` appear on several sidetone properties, so
#: they are handled by rule rather than listed per property.
_DB_PATTERN = {"_0dB": "0 dB"}

#: Button *actions*, by rule rather than per property — 47 catalogue properties remap a control
#: (`button1Tap`, `leftButtonOngoingCallHold`, `softButtonFunctionTop`, …) and they draw on a
#: handful of shared enums, so listing them per property would repeat the same 24 values dozens of
#: times. Only values the camelCase fallback gets *wrong or opaque* are listed: it already turns
#: `answerOrEndCall` into "Answer or end call" perfectly well.
#:
#: Why this matters beyond cosmetics: the action chosen here decides what the host sees. Pick
#: `answerOrEndCall` and the button drives HID Telephony Hook Switch; pick `playOrPause` and it
#: emits a Consumer keypress instead; pick `batteryLevelReport` and it stays inside the headset
#: and reaches the host as nothing at all. See docs/STATE-CHANGES.md §4.
_BUTTON_ACTIONS: dict[str, str] = {
    # Teams-specific signalling. The generic fallback renders these "Ms teams invoke".
    "msTeam": "Microsoft Teams",
    "msTeamsInvoke": "Microsoft Teams — bring to front",
    "msTeamsCopilot": "Microsoft Teams — Copilot",
    "msTeamsRaiseHand": "Microsoft Teams — raise hand",
    # Push-to-talk variants. The suffixes are transport names, not English.
    "pushToTalk": "Push to talk",
    "pushToTalkRoip": "Push to talk (RoIP)",
    "cellularPtt": "Push to talk (cellular)",
    "escChatPtt": "Push to talk (ESChat)",
    "ffe0Ptt": "Push to talk (FFE0)",
    # Busylight. "Hs" is Jabra's abbreviation for the headset itself.
    "busylight": "Busylight",
    "busylightHs": "Busylight (headset only)",
    "busylightHeadset": "Busylight (headset only)",
    # Call handling, where the camelCase run-on is genuinely hard to read.
    "acceptCallEndCurrent": "Answer, ending the current call",
    "acceptCallHoldCurrent": "Answer, holding the current call",
    "endCurrentResumeCall": "End this call and resume the held one",
    "holdCurrentResumeCall": "Hold this call and resume the held one",
    "mobilePhoneCallControl": "Control calls on the paired phone",
    "secondDeviceAnswer": "Answer on the second paired device",
    "muteSpeakerAndMic": "Mute both speaker and microphone",
    "fullMute": "Mute speaker and microphone",
    "hookSwitch": "Answer / end call (hook switch)",
    "phoneMute": "Mute microphone",
    "flash": "Flash / switch call",
    "recordConv": "Record the conversation",
    "muteAll": "Mute everything",
    "reject": "Reject the call",
    "noFunction": "Nothing",
    "none": "Nothing",
    "customButton1": "Custom button 1",
    "customButton2": "Custom button 2",
    "customButton3": "Custom button 3",
    # Device-local actions: these never reach the host.
    "batteryLevelReport": "Announce battery level (headset only)",
    "toggleBusyState": "Toggle busy state",
    "toggleSoundModes": "Cycle sound modes",
    "toggleSidetone": "Toggle sidetone",
    "voiceAssistantLocked": "Voice assistant (even when locked)",
    "voiceCommands": "Voice commands",
    "cortana": "Cortana",
    "spotify": "Spotify",
    # Electronic hook-switch protocols on `eHookProtocol` — deskphone wiring standards, all of
    # which the fallback renders as meaningless four-letter words.
    "gn1000": "GN1000 remote handset lifter",
    "dhsg": "DHSG",
    "dhsgToggle": "DHSG (toggle)",
    "msh": "MSH",
    "rhl": "Remote handset lifter",
    "basicRhl": "Remote handset lifter (basic)",
    "gnUart": "GN UART",
    "cisco": "Cisco",
    "nec": "NEC",
    "auto": "Detect automatically",
}


def _decibel_label(value: str) -> str | None:
    if value in _DB_PATTERN:
        return _DB_PATTERN[value]
    for prefix, sign in (("minus", "−"), ("plus", "+")):
        if value.startswith(prefix) and value.endswith("dB"):
            digits = value[len(prefix):-2]
            if digits.isdigit():
                return f"{sign}{digits} dB"
    return None


#: Properties whose enum is a *button action*, so `_BUTTON_ACTIONS` applies. Gated by name rather
#: than applied to every enum, because several of those values are ordinary words elsewhere —
#: `auto`, `none` and `flash` would otherwise pick up call-handling wording on unrelated settings.
_BUTTON_PROPERTY = re.compile(
    r"^button\d|[Bb]uttonFunction|Button(IncomingCall|OngoingCall|Media)|"
    r"^softButtonFunction|^eHookProtocol|InCallAction$|^callAndMediaButtonFunction$|"
    r"^soundModeButtonFunction$|^muteAndVoiceAssistantButtonFunction$"
)


def is_button_property(name: str) -> bool:
    """Whether this property remaps a physical control. See docs/STATE-CHANGES.md §4."""
    return bool(_BUTTON_PROPERTY.search(name))


def value_label(prop_name: str, value: str) -> str:
    """A readable label for one enum value, or the raw value if we have no better name."""
    mapped = VALUE_LABELS.get(prop_name, {}).get(value)
    if mapped:
        return mapped
    if is_button_property(prop_name) and value in _BUTTON_ACTIONS:
        return _BUTTON_ACTIONS[value]
    decibels = _decibel_label(value)
    if decibels:
        return decibels
    return _generic(value)


#: Explanations shown as tooltips where the setting's name does not say what it does.
DESCRIPTIONS: dict[str, str] = {
    "sidetoneEnabled": "Sidetone plays your own voice back into the earcups so you do not "
                       "unconsciously raise your voice. It does not affect what the far end "
                       "hears.",
    "sidetoneEnabledDsp": "Sidetone plays your own voice back into the earcups so you do not "
                          "unconsciously raise your voice. It does not affect what the far end "
                          "hears.",
    "sidetoneLevel": "How loud your own voice is played back to you. Negative is quieter.",
    "sidetoneLevelDsp": "Select the volume of Sidetone. Sidetone plays your own voice back into "
                        "the earcups so you do not unconsciously raise it.",
    "sidetoneLevelEnum": "How loud your own voice is played back to you. Negative is quieter.",
    "intellitoneLevel": "Limits how loud the headset can get, to protect your hearing. "
                        "PeakStop only clips sudden peaks; the numbered levels and G616 also "
                        "limit sustained loudness.",
    "automaticAudioDetection": "When the headset treats audio as active. While it considers "
                               "itself idle, the volume buttons announce status instead of "
                               "changing volume.",
    "leAudioCallProfile": "Codec used for the wireless link. Mono/wideband profiles are for "
                          "voice and sound thin for music.",
    "currentLanguage": "Language of the spoken prompts. Only languages already installed on the "
                       "device can be selected; adding more needs a language-pack upload, which "
                       "this app does not do.",
    "radioPower": "Transmit power. Shorter range saves battery and reduces interference.",
    "boomArmRotateAcceptCall": "Answer an incoming call by swinging the boom arm down.",
    "autoMuteCallAudio": "Mute the microphone automatically when you take the headset off.",
    "autoPauseMusicOnHeadEnabled": "Pause playback automatically when you take the headset off.",
    "publicModeEnabled": "Reduces how much the headset leaks audio to people nearby.",
    "hidDevice": "The HID control interface. Changing this can cut the connection this app "
                 "uses — leave it alone unless you know why you are changing it.",
}


def description(name: str) -> str:
    return DESCRIPTIONS.get(name, "")


def label(name: str) -> str:
    return OVERRIDES.get(name) or _generic(name)


def language_name(lcid: int) -> str:
    """"English (US) (1033)", or "Language 4711" for one we do not know."""
    known = LCID_NAMES.get(int(lcid))
    return f"{known} ({lcid})" if known else f"Language {lcid}"


def language_choices(available: list[int] | None) -> list[tuple[str, int]]:
    """[(label, lcid)] for a dropdown, from the device's own `availableLanguages`.

    Falls back to the full known table when the device does not report a list — better a
    superset the device may refuse (which the GUI then removes) than an empty dropdown.
    """
    codes = [int(c) for c in available] if available else sorted(LCID_NAMES)
    return [(language_name(code), code) for code in codes]
