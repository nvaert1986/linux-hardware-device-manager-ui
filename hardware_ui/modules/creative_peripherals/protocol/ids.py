"""Command, feature and parameter identifiers.

Every value here is either lifted from the decompiled Creative source
(`CDCRawLibrary`, `Creative.Platform.Devices.dll`) or confirmed on the wire in
the Windows USB capture. Provenance is noted per group.
"""

from __future__ import annotations

import enum


class Cmd(enum.IntEnum):
    """`CDCRawCommand` — from the decompiled source."""

    ACKNOWLEDGE = 2
    MAX_PAYLOAD_SIZE = 3
    DEVICE_INFO_V1 = 7
    DEVICE_INFO_V2 = 9
    CONNECTION_MODE = 12
    CONNECTION_STATUS = 14
    SUBFEATURE_SUPPORT = 16
    GET_MALCOLM_PARAM = 17
    SET_MALCOLM_PARAM = 18
    MALCOLM_PROFILE_DATA = 23
    MALCOLM_PROFILE_NAME = 24
    ACTIVE_MALCOLM_PROFILE = 26
    MALCOLM_PROFILE_INFO = 29
    GET_AUDIO_CONTROL_INFO = 33
    GET_AUDIO_LEVEL_RANGES = 34
    AUDIO_LEVEL = 35
    AUDIO_MUTE = 36
    HARDWARE_BUTTON = 38
    SPEAKER_CONFIGURATION = 41
    SPEAKER_OUTPUT_TARGET = 44
    FEATURE_CONTROL = 57
    LED_CONTROL = 58
    GRAPHIC_EQ_CONTROL = 68
    DEVICE_COLOR_CONTROL = 73
    SUPER_WIDE_CONTROL = 74
    SPEAKER_CHANNEL_CONFIG = 75
    UPGRADE = 83                       # dangerous
    AUDIO_FILTER = 108
    DEVICE_OPERATION_MODE = 109
    SUPER_XFI = 111
    HANDSHAKE_114 = 114
    DEVICE_CALLBACK = 150
    FACTORY_RESET = 155                # dangerous
    CUSTOM_BUTTON_CONTROL = 165
    SOUND_MODE_CONTROL = 167


#: Never send these: they reflash or wipe the device.
DANGEROUS = frozenset({Cmd.UPGRADE, Cmd.FACTORY_RESET})


class FeatureOp(enum.IntEnum):
    """`FeatureControlOperation` — from the decompiled source."""

    SET = 0
    GET = 1
    SUPPORT = 2
    STATUS_ONLY_SUPPORT = 3
    SET_ONLY_SUPPORT = 4
    ADDITIONAL_PARAM_SUPPORT = 5
    GET_ADDITIONAL_PARAM = 6
    SET_ADDITIONAL_PARAM = 7


class Feature(enum.IntEnum):
    """Bit position within the FeatureControl bitfield.

    `RawCmdFeatureControlSet` carries a `FeatureBitPos`, and the source's
    `FeatureMask` names each bit. Bit N == feature id N: verified for DirectMode
    (0x20 == bit 5) on hardware, and matched against the capability queries the
    Windows app issues per feature.
    """

    DIRECT_MODE = 5                    # FeatureMask 0x20   — hardware-verified
    HP_HIGH_GAIN = 6                   # 0x40               — capture-verified
    HRTF_OVER_SPEAKER = 10             # 0x400
    SPDIF_DIRECT_MODE = 13             # 0x2000
    DISABLE_NONESSENTIAL_LED = 14      # 0x4000
    SPDIF_PASSTHRU_MODE = 15           # 0x8000             — capture-verified
    AUTO_STANDBY = 17                  # 0x20000
    LED_BRIGHTNESS = 18                # 0x40000
    SAVE_HP_HIGH_GAIN = 20             # 0x100000
    HP_VIRTUALIZATION = 21             # 0x200000           — capture-verified
    AUDIO_PROMPT = 22                  # 0x400000
    STEREO_MIC = 23                    # 0x800000
    HDMI_CEC_STANDBY_LINK = 24         # 0x1000000
    ENABLE_UAC2 = 26                   # 0x4000000          — capture-verified
    SWITCH_USB_MUX = 27                # 0x8000000
    SYNC_MASTER_VR_TO_HOST = 28        # 0x10000000

    @property
    def mask(self) -> int:
        return 1 << int(self)


#: Human labels for the features worth exposing.
FEATURE_LABELS = {
    Feature.DIRECT_MODE: "Direct Mode",
    Feature.HP_HIGH_GAIN: "Headphone High Gain",
    Feature.SAVE_HP_HIGH_GAIN: "Remember Headphone Gain",
    Feature.SPDIF_PASSTHRU_MODE: "SPDIF Passthrough",
    Feature.HP_VIRTUALIZATION: "Headphone Virtualization (Line Out)",
    Feature.AUTO_STANDBY: "Auto Standby",
    Feature.AUDIO_PROMPT: "Audio Prompts",
    Feature.DISABLE_NONESSENTIAL_LED: "Dim Non-Essential LEDs",
}


class Module(enum.IntEnum):
    """`MalcolmModuleId` — DSP module addressing."""

    MASTER_CONTROL = 128
    VOICE_INPUT = 149
    PLAYBACK = 150
    DOLBY_DECODER = 151


class Playback(enum.IntEnum):
    """`MalcolmPlaybackManagerCommand` — parameters under Module.PLAYBACK."""

    CMSS3D_ENABLE = 0
    CMSS3D_IMMERSION = 1
    DIALOG_PLUS_ENABLE = 2
    DIALOG_PLUS_STRENGTH = 3
    SVM_ENABLE = 4
    SVM_STRENGTH = 5
    SVM_MODE = 6
    CRYSTALIZER_ENABLE = 7
    CRYSTALIZER_LEVEL = 8
    GRAPHIC_EQ_ENABLE = 9
    GRAPHIC_EQ_PREAMP = 10
    XBASS_CROSSOVER = 23
    XBASS_ENABLE = 24
    XBASS_STRENGTH = 25


#: EQ band gain parameter ids: bands 0..9 are commands 11..20.
EQ_BAND_CMDS = tuple(range(11, 21))
EQ_BAND_COUNT = len(EQ_BAND_CMDS)
#: Band gain limits, from the values the Windows app actually wrote.
EQ_GAIN_MIN, EQ_GAIN_MAX = -12.0, 12.0


class ButtonID(enum.IntEnum):
    """`ButtonIDV2` — addressed via Cmd.HARDWARE_BUTTON op 7."""

    SBX = 1
    SCOUT = 2
    SXFI_ON_OFF = 30


class OutputTarget(enum.IntEnum):
    """`SpeakersOutputTargetBitwiseMask` — capture-verified."""

    LINE_OUT = 0x01
    POWER_AMP = 0x02
    HEADPHONES = 0x04


class ProfileType(enum.IntEnum):
    """`MalcolmProfileType` — from the decompiled source."""

    PLAYBACK = 0
    RECORD = 1
    DEVICE = 2
    SIREN = 3
    GRAPHIC_EQUALIZER = 4


class SubFeature(enum.IntFlag):
    """`MalcolmSubFeatureMask` — what the DSP actually implements.

    This X4's firmware reports 0x40 (GRAPHIC_EQ only): Crystalizer, Surround and
    X-Bass reads time out, because those effects run host-side as Windows APOs.
    """

    SURROUND = 0x1
    CRYSTALIZER = 0x2
    SVM = 0x4
    XBASS = 0x8
    BASS_MANAGEMENT = 0x10
    DIALOG_PLUS = 0x20
    GRAPHIC_EQ = 0x40
    VOICE_FX = 0x80
    MIC_SVM = 0x100
    NOISE_CANCELLATION = 0x200
    AEC = 0x400
    VOICE_FOCUS = 0x800
    PROFILES = 0x1000
    DOLBY_DIGITAL_DECODING = 0x2000
    DRC_CTRL = 0x4000
    LINE_NOISE_REDUCTION = 0x8000
    DUAL_MIC_END_FIRING = 0x10000
    SPEAKER_CALIBRATION = 0x20000
    MIC_EQ = 0x40000
    OPTIONAL_SPEAKER = 0x80000
    SVM_PLUS = 0x100000
    SURROUND_V2 = 0x200000
    DIALOG_PLUS_V2 = 0x400000
    SUBWOOFER_BOOST = 0x800000


#: Super X-Fi modes: 4-character ASCII codes, captured from the wire.
#: Only these two were observed; others in the Windows app are unknown.
SXFI_MODES: dict[str, str] = {
    "MV  ": "Super X-Fi",
    "PCG ": "Battle Mode",
}


#: What the four stored equaliser profiles are, **per model**, because they are not user slots.
#:
#: The Sound Blaster X4's are fixed modes with fixed meanings — the button on the front cycles them
#: and lights a colour for each — so numbering them "Profile 1" to "Profile 4" named the thing
#: without saying what it is, which is the complaint that produced this table. Read off the card by
#: its owner, not decoded: there is a command to *name* a profile and one to select one, and no
#: captured reply to a name **read**, so the device cannot be asked.
#:
#: Keyed by product id and consulted only for a model that is listed. Every other Creative device
#: falls back to numbers, because a confident wrong name is worse than an honest number and this
#: module matches on vendor id alone.
PROFILE_NAMES: dict[int, tuple[str, ...]] = {
    0x3278: ("Music", "Movie", "Footstep Enhancer", "EQ for Super X-Fi"),  # Sound Blaster X4
}
