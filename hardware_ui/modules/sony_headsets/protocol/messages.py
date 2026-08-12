"""MDR payload builders and parsers.

Each ``build_*`` returns the raw payload bytes (starting with the Command byte)
to hand to :meth:`Session.send_command`. Each ``parse_*`` turns a received
payload into a small dataclass. Byte layouts are ``✅ verified`` unless marked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import enums
from .enums import Command, ConnectInquiredType, DeviceInfoType, PowerInquiredType


# --------------------------------------------------------------------------
# Handshake (CONNECT group) — see protocol reference §6
# --------------------------------------------------------------------------

def build_get_protocol_info() -> bytes:
    return bytes((Command.CONNECT_GET_PROTOCOL_INFO, ConnectInquiredType.FIXED_VALUE))


def build_get_capability_info() -> bytes:
    return bytes((Command.CONNECT_GET_CAPABILITY_INFO, ConnectInquiredType.FIXED_VALUE))


def build_get_device_info(info_type: DeviceInfoType) -> bytes:
    return bytes((Command.CONNECT_GET_DEVICE_INFO, int(info_type)))


def build_get_support_function() -> bytes:
    return bytes((Command.CONNECT_GET_SUPPORT_FUNCTION, ConnectInquiredType.FIXED_VALUE))


@dataclass
class ProtocolInfo:
    protocol_version: int
    supports_table1: bool
    supports_table2: bool


def parse_protocol_info(payload: bytes) -> ProtocolInfo:
    # payload: [0x01, fixedType, <version bytes...>, table1, table2]  🔶 exact
    # offsets vary; the app treats version defensively (used only for gating).
    # Reference reads a u32 version + two support flags.
    body = payload[2:]
    version = int.from_bytes(body[:4], "big") if len(body) >= 4 else 0
    t1 = bool(body[4]) if len(body) > 4 else True
    t2 = bool(body[5]) if len(body) > 5 else False
    return ProtocolInfo(protocol_version=version, supports_table1=t1, supports_table2=t2)


def parse_device_info(payload: bytes) -> tuple[DeviceInfoType, str]:
    # payload: [0x05, type, len, <ascii string>]  ✅ (length-prefixed string)
    info_type = DeviceInfoType(payload[1])
    length = payload[2] if len(payload) > 2 else 0
    text = payload[3:3 + length].decode("ascii", errors="replace")
    return info_type, text


class FT:
    """v1/table1 FunctionType byte codes (Sony's per-feature support list).
    Confirmed from the XM4's CONNECT_RET_SUPPORT_FUNCTION response."""
    CODEC_INDICATOR = 0x13
    VOICE_GUIDANCE = 0x39
    PRESET_EQ = 0x51
    EBB = 0x52
    PRESET_EQ_NONCUSTOMIZABLE = 0x53
    NOISE_CANCELLING = 0x61
    NC_AND_ASM = 0x62
    AMBIENT_SOUND_MODE = 0x63
    AUTO_NC_ASM = 0x71
    NC_OPTIMIZER = 0x81
    PLAYBACK_CONTROLLER = 0xA1
    GENERAL_SETTING1 = 0xD1        # touch sensor panel on the XM4
    GENERAL_SETTING2 = 0xD2        # multipoint on the XM4
    GENERAL_SETTING3 = 0xD3
    CONNECTION_MODE = 0xE1         # sound quality mode
    UPSCALING = 0xE2               # DSEE
    CONTROL_BY_WEARING = 0xF3      # pause when removed
    AUTO_POWER_OFF = 0xF4
    SMART_TALKING_MODE = 0xF5
    ASSIGNABLE_SETTINGS = 0xF6     # CUSTOM button


def parse_support_function(payload: bytes) -> set[int]:
    """Parse CONNECT_RET_SUPPORT_FUNCTION.

    payload: [0x07, fixedType, count, ft0, ft1, ...] — a flat list of
    FunctionType byte codes (confirmed on the XM4).
    """
    if len(payload) < 3:
        return set()
    count = payload[2]
    return set(payload[3:3 + count])


# --------------------------------------------------------------------------
# Battery (POWER group) — ✅ verified layout
# --------------------------------------------------------------------------

def build_get_battery(kind: PowerInquiredType = PowerInquiredType.BATTERY) -> bytes:
    return bytes((Command.POWER_GET_STATUS, int(kind)))


@dataclass
class BatteryStatus:
    kind: PowerInquiredType
    level: int                # 0..100
    charging: bool
    left: int | None = None   # TWS only
    right: int | None = None


def parse_battery(payload: bytes) -> BatteryStatus:
    # single:    [0x23, type=0x00, level, chargingStatus]              ✅
    # left/right:[0x23, type=0x01, Llvl, Lchg, Rlvl, Rchg]            ✅
    kind = PowerInquiredType(payload[1])
    if kind == PowerInquiredType.LEFT_RIGHT_BATTERY:
        return BatteryStatus(
            kind=kind,
            level=min(payload[2], payload[4]),
            charging=bool(payload[3]) or bool(payload[5]),
            left=payload[2],
            right=payload[4],
        )
    return BatteryStatus(kind=kind, level=payload[2], charging=bool(payload[3]))


# --------------------------------------------------------------------------
# Noise Cancelling / Ambient Sound — legacy (v1) SET_PARAM, ✅ verified
# Mirrors CommandSerializer::serializeNcAndAsmSetting.
# --------------------------------------------------------------------------

def build_set_ncasm(
    *,
    enabled: bool,
    focus_on_voice: bool = False,
    asm_level: int = 0,
) -> bytes:
    """Build the legacy NC/ASM SET_PARAM payload (combined NC + Ambient mode).

    * ``enabled``       — ambient sound on (True) / off (False)
    * ``focus_on_voice``— voice emphasis within ambient mode
    * ``asm_level``     — 0..19 ambient level; ignored (disabled) when off

    Field layout mirrors the device's RET
    ``67 02 <effect> <ncAsmSettingType> <ncDualSingle> <asmSettingType> <asmId>
    <asmLevel>`` (verified from an XM4 capture: NC mode reads
    ``67 02 01 02 02 01 00 00``). The two user-facing modes differ in *which*
    field is authoritative, selected by ``ncAsmSettingType``:

    * **Ambient** — ``ncAsmSettingType=LEVEL_ADJUSTMENT``, NC ternary ``OFF``,
      the ambient level is live (0..19), ``asmId`` picks normal/voice.
    * **Noise Cancelling** — ``ncAsmSettingType=DUAL_SINGLE_OFF``, NC ternary
      ``DUAL`` (XM3/XM4 are dual-mic), the level is inert.

    Both use ``effect=ADJUSTMENT_COMPLETION`` (apply now); the device stores it
    as ``ON``. Unchecking Ambient must therefore send *Noise Cancelling*, not
    ``effect=OFF`` — the latter is the (unused) third "off" mode and the device
    ignores it here, leaving ambient on. Also: the NC ternary is NEVER derived
    from the level (an earlier bug), and Sony's app enforces no voice-level
    minimum (checked against the decompiled source), so voice works at any level.
    """
    if asm_level > enums.MAX_ASM_STEPS_XM3:
        raise ValueError("ASM level exceeds device maximum")
    effect = enums.NcAsmEffect.ADJUSTMENT_COMPLETION
    if enabled:  # Ambient Sound
        nc_setting_type = enums.NcAsmSettingType.LEVEL_ADJUSTMENT
        nc_dual_single = enums.NcDualSingleValue.OFF
        asm_id = enums.AsmId.VOICE if focus_on_voice else enums.AsmId.NORMAL
        level = asm_level
    else:        # Noise Cancelling
        nc_setting_type = enums.NcAsmSettingType.DUAL_SINGLE_OFF
        nc_dual_single = enums.NcDualSingleValue.DUAL
        asm_id = enums.AsmId.NORMAL
        level = 0
    return bytes((
        Command.NCASM_SET_PARAM,
        enums.NcAsmInquiredType.NOISE_CANCELLING_AND_AMBIENT_SOUND_MODE,
        int(effect),
        int(nc_setting_type),
        int(nc_dual_single),
        enums.AsmSettingType.LEVEL_ADJUSTMENT,
        int(asm_id),
        level & 0xFF,
    ))


def build_get_ncasm() -> bytes:
    return bytes((Command.NCASM_GET_PARAM,
                  enums.NcAsmInquiredType.NOISE_CANCELLING_AND_AMBIENT_SOUND_MODE))


@dataclass
class NcAsmState:
    enabled: bool
    focus_on_voice: bool
    asm_level: int
    raw: bytes = field(repr=False, default=b"")


# --------------------------------------------------------------------------
# Equalizer (EQEBB group) — layout confirmed from capture + reference
#   RET/SET payload: [cmd, type, presetId, numBands, band0..N]
#   band on the wire = UI value (-10..+10) + EQ_BAND_OFFSET
# The XM4 uses inquired-type 0x01 and 6 bands (Clear Bass + 5 EQ bands).
# --------------------------------------------------------------------------

EQ_INQUIRED_TYPE = 0x01
EQ_BAND_OFFSET = 10
EQ_BAND_MIN, EQ_BAND_MAX = -10, 10
# XM4 band labels, in payload order (index 0 = Clear Bass).
EQ_BAND_LABELS = ("Clear Bass", "400", "1k", "2.5k", "6.3k", "16k")


@dataclass
class EqConfig:
    preset_id: int
    bands: list[int]          # UI range -10..+10, index 0 = Clear Bass


def build_get_eq() -> bytes:
    return bytes((Command.EQEBB_GET_PARAM, EQ_INQUIRED_TYPE))


# Confirmed from HCI capture: custom band values are sent with preset id 0xFF
# ("manual"); numBands is 6 (Clear Bass + 5). Selecting a named preset sends the
# preset id with numBands 0.
EQ_PRESET_MANUAL = 0xFF

# EqPresetId values that are user-editable (CUSTOM 0xA0 + USER_SETTING1-5).
# Band sliders are only editable while one of these presets is active; named
# presets (Rock, Pop, ...) show a fixed read-only curve.
EQ_CUSTOM_PRESETS = frozenset({0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})

# EqPresetId values the WH-1000XM4 actually supports (confirmed from the HCI
# capture — the app only ever set these). The Rock/Pop/Jazz-style "genre"
# presets (0x01-0x07) are NOT available on the XM4 and are omitted; other models
# expose a different set, so this should become capability-driven (see ROADMAP).
EQ_PRESETS = [
    (0xA1, "Custom 1"), (0x00, "Off"),
    (0x10, "Bright"), (0x11, "Excited"), (0x12, "Mellow"), (0x13, "Relaxed"),
    (0x14, "Vocal"), (0x15, "Treble"), (0x16, "Bass"), (0x17, "Speech"),
]


def build_set_eq_bands(bands: list[int], preset_id: int = EQ_PRESET_MANUAL) -> bytes:
    """Set custom band levels. ``bands`` are UI values (-10..+10)."""
    steps = bytes((max(EQ_BAND_MIN, min(EQ_BAND_MAX, b)) + EQ_BAND_OFFSET) & 0xFF
                  for b in bands)
    return bytes((Command.EQEBB_SET_PARAM, EQ_INQUIRED_TYPE, preset_id & 0xFF,
                  len(steps))) + steps


def build_set_eq_preset(preset_id: int) -> bytes:
    """Select a named preset (device fills in the bands); no band data."""
    return bytes((Command.EQEBB_SET_PARAM, EQ_INQUIRED_TYPE, preset_id & 0xFF, 0))


def parse_eq(payload: bytes) -> EqConfig:
    # [cmd, type, presetId, numBands, band0..N]
    preset_id = payload[2]
    n = payload[3]
    steps = payload[4:4 + n]
    return EqConfig(preset_id=preset_id,
                    bands=[b - EQ_BAND_OFFSET for b in steps])


# --------------------------------------------------------------------------
# DSEE Extreme (AUDIO group, inquired-type 0x02) — confirmed from capture
#   set:  e8 02 00 <onoff>   (Off=0, Auto=1)   read: e6 02 -> e7 02 00 <onoff>
# --------------------------------------------------------------------------

DSEE_TYPE = enums.AUDIO_TYPE_DSEE


def build_get_dsee() -> bytes:
    return bytes((Command.AUDIO_GET_PARAM, DSEE_TYPE))


def build_set_dsee(on: bool) -> bytes:
    return bytes((Command.AUDIO_SET_PARAM, DSEE_TYPE, 0x00, 1 if on else 0))


def parse_dsee(payload: bytes) -> bool:
    return bool(payload[3]) if len(payload) > 3 else False


# --------------------------------------------------------------------------
# Sound Quality Mode (AUDIO type 0x01) — 0=Prioritize Sound Quality, 1=Stable
# Auto Power Off (SYSTEM type 0x04) — 0x10=off-when-removed, 0x11=never
# Custom button (SYSTEM type 0x03) — 0=Ambient, 1=Digital Assistant, 2=Alexa
# Touch sensor panel (SYSTEM type 0x06) — on/off
# Pause-when-removed (GENERAL_SETTING 0xd1) / Multipoint (0xd2) — on/off
# All confirmed from HCI captures 06/07/08.
# --------------------------------------------------------------------------

SOUND_QUALITY_TYPE = enums.AUDIO_TYPE_SOUND_QUALITY
SOUND_QUALITY_MODES = [(0, "Prioritize Sound Quality"),
                       (1, "Priority on Stable Connection")]

APO_TYPE = enums.SYSTEM_TYPE_AUTO_POWER_OFF
APO_OFF_WHEN_REMOVED = 0x10
APO_NEVER = 0x11

# CUSTOM button = ASSIGNABLE_SETTINGS (SYSTEM 0x06). Preset values are Sony's
# AssignableSettingsPreset enum. Payload: f8 06 <count=1> <preset>.
CUSTOM_BTN_TYPE = enums.SYSTEM_TYPE_ASSIGNABLE
CUSTOM_BTN_FUNCS = [(0x00, "Ambient Sound Control"),
                    (0x31, "Digital Assistant"),
                    (0x32, "Amazon Alexa")]

PAUSE_TYPE = enums.SYSTEM_TYPE_CONTROL_BY_WEARING
GS_TYPE_TOUCH_PANEL = enums.GS_TYPE_TOUCH_PANEL
GS_TYPE_MULTIPOINT = enums.GS_TYPE_MULTIPOINT


def build_get_sound_quality() -> bytes:
    return bytes((Command.AUDIO_GET_PARAM, SOUND_QUALITY_TYPE))


def build_set_sound_quality(mode: int) -> bytes:
    return bytes((Command.AUDIO_SET_PARAM, SOUND_QUALITY_TYPE, 0x00, mode & 0xFF))


def parse_sound_quality(payload: bytes) -> int:
    return payload[3] if len(payload) > 3 else 0


def build_get_apo() -> bytes:
    return bytes((Command.SYSTEM_GET_PARAM, APO_TYPE))


def build_set_apo(never: bool) -> bytes:
    return bytes((Command.SYSTEM_SET_PARAM, APO_TYPE, 0x01,
                  APO_NEVER if never else APO_OFF_WHEN_REMOVED, 0x00))


def parse_apo(payload: bytes) -> bool:
    """Return True when set to 'do not turn off'."""
    return len(payload) > 3 and payload[3] == APO_NEVER


def build_get_custom_button() -> bytes:
    return bytes((Command.SYSTEM_GET_PARAM, CUSTOM_BTN_TYPE))


def build_set_custom_button(preset: int) -> bytes:
    # f8 06 <count=1> <preset>
    return bytes((Command.SYSTEM_SET_PARAM, CUSTOM_BTN_TYPE, 0x01, preset & 0xFF))


def parse_custom_button(payload: bytes) -> int:
    return payload[3] if len(payload) > 3 else 0


def build_get_pause() -> bytes:
    return bytes((Command.SYSTEM_GET_PARAM, PAUSE_TYPE))


def build_set_pause(on: bool) -> bytes:
    # f8 03 <settingType=ON_OFF=0> <0=off/1=on>
    return bytes((Command.SYSTEM_SET_PARAM, PAUSE_TYPE, 0x00, 1 if on else 0))


def parse_pause(payload: bytes) -> bool:
    return bool(payload[3]) if len(payload) > 3 else False


def build_get_gs(gs_type: int) -> bytes:
    return bytes((Command.GS_GET_PARAM, gs_type))


def build_set_gs(gs_type: int, on: bool) -> bytes:
    return bytes((Command.GS_SET_PARAM, gs_type, 0x01, 1 if on else 0))


def parse_gs(payload: bytes) -> bool:
    return bool(payload[3]) if len(payload) > 3 else False


# GENERAL_SETTING is a generic settings mechanism: each slot carries a TITLE
# identifying what it is (device-specific!). Read it via GET_CAPABILITY (0xd0)
# and map by title, not slot number. Confirmed titles:
GS_TITLE_TOUCH_PANEL = "TOUCH_PANEL_SETTING"
GS_TITLE_MULTIPOINT = "MULTIPOINT_SETTING"
GS_TITLE_ASSIGNABLE_KEY = "ASSIGNABLE_KEY_SETTING"


def build_get_gs_capability(gs_type: int) -> bytes:
    return bytes((0xD0, gs_type, 0x01))


def parse_gs_capability(payload: bytes) -> tuple[int, str]:
    """Return (slot, title) from a GS RET_CAPABILITY.
    payload: [0xd1, slot, settingType, titleLen, <title ascii>, ...]"""
    slot = payload[1]
    title = ""
    if len(payload) > 3:
        tlen = payload[3]
        title = payload[4:4 + tlen].decode("ascii", errors="replace")
    return slot, title


# --- Auto Power Off (capability-driven; options differ per model) ----------
# element ids -> label. XM4: {0x10 when-removed, 0x11 never}. XM3: time + never.
APO_ELEMENT_LABELS = {
    0x00: "5 minutes", 0x01: "30 minutes", 0x02: "60 minutes",
    0x03: "180 minutes", 0x10: "When headphones removed",
    0x11: "Do not turn off",
}


def build_get_apo_capability() -> bytes:
    return bytes((Command.SYSTEM_GET_CAPABILITY, APO_TYPE))


def parse_apo_capability(payload: bytes) -> list[int]:
    """[0xf1, type, count, e0, e1, ...] -> list of supported element ids."""
    if len(payload) < 3:
        return []
    count = payload[2]
    return list(payload[3:3 + count])


def build_set_apo_element(element: int) -> bytes:
    # f8 04 01 <elementId> <selectTimeId>. For the timed options (5/30/60/180
    # min = element 0..3) the select-time byte MUST equal the element; for
    # never (0x11) / when-removed (0x10) it is 0. Confirmed from XM3 capture.
    select_time = element if element <= 0x03 else 0x00
    return bytes((Command.SYSTEM_SET_PARAM, APO_TYPE, 0x01, element & 0xFF,
                  select_time & 0xFF))


def parse_apo_element(payload: bytes) -> int:
    # [0xf7, type, 0x01, element, 0x00]
    return payload[3] if len(payload) > 3 else -1


# --------------------------------------------------------------------------
# Speak-to-Chat (SYSTEM group, inquired-type 0x05) — confirmed from capture
#   enable:   f8 05 01 <onoff>            (SET_PARAM)
#   sens/time: fc 05 00 <sens> 00 <time>  (SET_EXT_PARAM)
#   sensitivity: Auto=0 High=1 Low=2   timeout: Short=0 Standard=1 Long=2 Never=3
# --------------------------------------------------------------------------

STC_TYPE = enums.SYSTEM_TYPE_SPEAK_TO_CHAT
STC_SENSITIVITY = [(0, "Automatic"), (1, "High"), (2, "Low")]
STC_TIMEOUT = [(0, "Short (15s)"), (1, "Standard (30s)"), (2, "Long (60s)"),
               (3, "Does not close automatically")]


@dataclass
class StcState:
    enabled: bool
    sensitivity: int = 0
    timeout: int = 1


def build_get_stc_param() -> bytes:
    return bytes((Command.SYSTEM_GET_PARAM, STC_TYPE))


def build_get_stc_ext() -> bytes:
    return bytes((Command.SYSTEM_GET_EXT_PARAM, STC_TYPE))


def build_set_stc_enable(enabled: bool) -> bytes:
    return bytes((Command.SYSTEM_SET_PARAM, STC_TYPE, 0x01, 1 if enabled else 0))


def build_set_stc_ext(sensitivity: int, timeout: int) -> bytes:
    return bytes((Command.SYSTEM_SET_EXT_PARAM, STC_TYPE,
                  0x00, sensitivity & 0xFF, 0x00, timeout & 0xFF))


def parse_stc_param(payload: bytes, prev: "StcState | None") -> StcState:
    # [cmd, type, 0x01, onoff]
    enabled = bool(payload[3]) if len(payload) > 3 else False
    st = prev or StcState(enabled=enabled)
    st.enabled = enabled
    return st


def parse_stc_ext(payload: bytes, prev: "StcState | None") -> StcState:
    # [cmd, type, 0x00, sens, 0x00, timeout]
    st = prev or StcState(enabled=False)
    if len(payload) > 5:
        st.sensitivity = payload[3]
        st.timeout = payload[5]
    return st


def parse_ncasm(payload: bytes) -> NcAsmState:
    # Layout: [cmd, inquiredType, effect, ncAsmSettingType, ncDualSingle,
    #          asmSettingType, asmId, asmLevel]  (verified on an XM4).
    try:
        effect = payload[2]
        nc_dual_single = payload[4]
        asm_id = payload[6]
        level = payload[7]
    except IndexError:
        return NcAsmState(False, False, 0, raw=payload)
    # Ambient and NC BOTH report effect=ON, so effect alone can't tell them
    # apart. Ambient is the mode where noise-cancelling is off (NC ternary
    # == OFF); NC mode carries SINGLE/DUAL there.
    feature_off = (effect == enums.NcAsmEffect.OFF)
    ambient_on = (not feature_off
                  and nc_dual_single == enums.NcDualSingleValue.OFF)
    return NcAsmState(
        enabled=ambient_on,
        focus_on_voice=(asm_id == enums.AsmId.VOICE),
        asm_level=level if level != enums.ASM_LEVEL_DISABLED else 0,
        raw=payload,
    )
