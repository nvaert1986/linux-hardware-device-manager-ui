"""MDR protocol enums and constants.

Values are ``✅ verified`` against the reference C++ sources unless a comment
marks them ``🔶 to-confirm`` (verify on-device / by HCI capture). See
SONY_WH1000_XM3_XM4_PROTOCOL.md.
"""

from __future__ import annotations

from enum import IntEnum

# Sony configuration SDP service UUID (RFCOMM/SPP). ✅
SERVICE_UUID = "96CC203E-5068-46AD-B32D-E316F5E069BA"

# Gatekeeping: recognise Sony headphones only. Sony uses exclusive model naming
# for its headphones — WH-/WF-/WI- (current wireless over-ear/TWS/neckband) and
# MDR- (legacy) — plus a few branded families. A device that advertises the Sony
# config UUID is also accepted regardless of name.
SONY_NAME_PREFIXES = ("WH-", "WF-", "WI-", "MDR-")
SONY_NAME_KEYWORDS = ("LINKBUDS", "INZONE", "ULT WEAR")

# Specific models we have product images / known handling for.
KNOWN_MODEL_PREFIXES = ("WH-1000XM3", "WH-1000XM4", "MDR-1000X")


class DataType(IntEnum):
    """Frame DATA_TYPE (byte 1 of the frame). ✅"""
    DATA = 0
    ACK = 1
    DATA_MDR = 12        # Table 1 — the main control channel for XM3/XM4
    DATA_COMMON = 13
    DATA_MDR_NO2 = 14    # Table 2 — extended set (rarely used by XM3/XM4)
    UNKNOWN = 0xFF


class Command(IntEnum):
    """MDR Table-1 command byte = (function_group << 4) | operation. ✅

    Only the commands the app currently uses are enumerated; the full grid is
    documented in the protocol reference.
    """
    # CONNECT group (0x0) — handshake
    CONNECT_GET_PROTOCOL_INFO = 0x00
    CONNECT_RET_PROTOCOL_INFO = 0x01
    CONNECT_GET_CAPABILITY_INFO = 0x02
    CONNECT_RET_CAPABILITY_INFO = 0x03
    CONNECT_GET_DEVICE_INFO = 0x04
    CONNECT_RET_DEVICE_INFO = 0x05
    CONNECT_GET_SUPPORT_FUNCTION = 0x06
    CONNECT_RET_SUPPORT_FUNCTION = 0x07

    # POWER group (0x2) — battery
    POWER_GET_STATUS = 0x22
    POWER_RET_STATUS = 0x23
    POWER_NTFY_STATUS = 0x25

    # EQEBB group (0x5) — equalizer
    EQEBB_GET_CAPABILITY = 0x50
    EQEBB_RET_CAPABILITY = 0x51
    EQEBB_GET_PARAM = 0x56
    EQEBB_RET_PARAM = 0x57
    EQEBB_SET_PARAM = 0x58
    EQEBB_NTFY_PARAM = 0x59

    # NCASM group (0x6) — noise cancelling / ambient sound
    NCASM_GET_PARAM = 0x66
    NCASM_RET_PARAM = 0x67
    NCASM_SET_PARAM = 0x68
    NCASM_NTFY_PARAM = 0x69

    # AUDIO group (0xE) — DSEE / upscaling / sound-quality mode
    AUDIO_GET_PARAM = 0xE6
    AUDIO_RET_PARAM = 0xE7
    AUDIO_SET_PARAM = 0xE8
    AUDIO_NTFY_PARAM = 0xE9

    # GENERAL_SETTING group (0xD) — device-specific slots (touch/multipoint/...)
    GS_GET_CAPABILITY = 0xD0
    GS_RET_CAPABILITY = 0xD1
    GS_GET_PARAM = 0xD6
    GS_RET_PARAM = 0xD7
    GS_SET_PARAM = 0xD8
    GS_NTFY_PARAM = 0xD9

    # ALERT group (0x9) — confirm dialogs for reboot/disconnect-inducing changes
    ALERT_SET_PARAM = 0x98
    ALERT_NTFY_PARAM = 0x99

    # SYSTEM group (0xF) — Speak-to-Chat, touch buttons, etc.
    SYSTEM_GET_CAPABILITY = 0xF0
    SYSTEM_RET_CAPABILITY = 0xF1
    SYSTEM_GET_PARAM = 0xF6
    SYSTEM_RET_PARAM = 0xF7
    SYSTEM_SET_PARAM = 0xF8
    SYSTEM_NTFY_PARAM = 0xF9
    SYSTEM_GET_EXT_PARAM = 0xFA
    SYSTEM_RET_EXT_PARAM = 0xFB
    SYSTEM_SET_EXT_PARAM = 0xFC
    SYSTEM_NTFY_EXT_PARAM = 0xFD

    UNKNOWN = 0xFF


# Inquired-types — verified against Sony's own v1/table1 enums (decompiled APK).
AUDIO_TYPE_SOUND_QUALITY = 0x01     # AudioInquiredType.CONNECTION_MODE
AUDIO_TYPE_DSEE = 0x02              # AudioInquiredType.UPSCALING (Off=0 / Auto=1)
SYSTEM_TYPE_CONTROL_BY_WEARING = 0x03  # pause when headphones removed (ON_OFF)
SYSTEM_TYPE_AUTO_POWER_OFF = 0x04
SYSTEM_TYPE_SPEAK_TO_CHAT = 0x05
SYSTEM_TYPE_ASSIGNABLE = 0x06       # CUSTOM button (reboots on change)
GS_TYPE_TOUCH_PANEL = 0xD1          # touch sensor control panel on/off
GS_TYPE_MULTIPOINT = 0xD2           # connect to 2 devices (reboots; disables LDAC)

# ALERT (FIXED_MESSAGE) — confirm reboot/disconnect dialogs.
ALERT_INQUIRED_FIXED_MESSAGE = 0x01
ALERT_ACTION_NEGATIVE = 0x00
ALERT_ACTION_POSITIVE = 0x01


class ConnectInquiredType(IntEnum):
    FIXED_VALUE = 0x00  # ✅


class DeviceInfoType(IntEnum):
    MODEL_NAME = 0x01           # ✅
    FW_VERSION = 0x02           # ✅
    SERIES_AND_COLOR_INFO = 0x03  # ✅


class PowerInquiredType(IntEnum):
    BATTERY = 0x00                 # single battery (XM3/XM4) ✅
    LEFT_RIGHT_BATTERY = 0x01      # TWS earbuds
    CRADLE_BATTERY = 0x02          # charging case
    BATTERY_WITH_THRESHOLD = 0x03  # 🔶


class BatteryChargingStatus(IntEnum):
    NOT_CHARGING = 0x00  # 🔶 exact codes to confirm
    CHARGING = 0x01


# --- Legacy (Plutoberth) NC/ASM SET_PARAM payload fields -------------------
# This byte layout is fully ✅ verified from serializeNcAndAsmSetting and is
# known-good on the XM3 (and works for basic ANC on the XM4). It is the
# "v1-protocol" path this project targets first.

class NcAsmInquiredType(IntEnum):
    NO_USE = 0
    NOISE_CANCELLING = 1
    NOISE_CANCELLING_AND_AMBIENT_SOUND_MODE = 2  # what the legacy setter uses ✅
    AMBIENT_SOUND_MODE = 3


class NcAsmEffect(IntEnum):
    OFF = 0
    ON = 1
    ADJUSTMENT_IN_PROGRESS = 16
    ADJUSTMENT_COMPLETION = 17  # legacy setter sends this when enabling ✅


class NcAsmSettingType(IntEnum):
    ON_OFF = 0
    LEVEL_ADJUSTMENT = 1      # active field is the ambient level (Ambient mode)
    DUAL_SINGLE_OFF = 2       # active field is the NC ternary (Noise-Cancelling mode)


class AsmSettingType(IntEnum):
    ON_OFF = 0
    LEVEL_ADJUSTMENT = 1


class AsmId(IntEnum):
    NORMAL = 0
    VOICE = 1  # "focus on voice"


class NcDualSingleValue(IntEnum):
    OFF = 0
    SINGLE = 1
    DUAL = 2


# Ambient sound level range for the legacy NC/ASM path. ✅ (XM3 = 0..19)
MAX_ASM_STEPS_XM3 = 19
ASM_LEVEL_DISABLED = 0xFF  # -1 as an unsigned byte ✅
