"""VCP feature registry and Dell value labels.

The value maps come from reverse-engineering Dell Display & Peripheral Manager
(see the DDPM_*.md docs in ~/Projects/Dell). ddcutil labels manufacturer codes
generically or not at all (e.g. 0xE2 = "interpretation unavailable"), so we
override with Dell's own names here.

Two things drive the GUI:
  * CONTINUOUS  — codes rendered as a slider (0..max, max read live via getvcp)
  * ENUM_LABELS — codes rendered as a dropdown; only the values the monitor
                  actually advertises are offered, labelled from these maps.
Anything not in either set is treated as read-only/opaque and not shown.
"""

from __future__ import annotations

# Human-readable feature names (superset; only advertised ones are shown).
FEATURE_NAMES: dict[int, str] = {
    0x10: "Brightness",
    0x12: "Contrast",
    0x14: "Colour Preset",
    0x16: "Gain — Red",
    0x18: "Gain — Green",
    0x1A: "Gain — Blue",
    0x60: "Input Source",
    0x62: "Audio Volume",
    0x6C: "Black Level — Red",
    0x6E: "Black Level — Green",
    0x70: "Black Level — Blue",
    0x87: "Sharpness",
    0x8A: "Colour Saturation",
    0x8D: "Audio Mute",
    0xAA: "Screen Orientation (OSD)",
    0xCC: "OSD Language",
    0xD6: "Power Mode",
    0xDC: "Display Mode",
    0xE2: "Preset Mode (Dell)",
    0xF0: "HDR Mode (Dell)",
    0xF4: "Gaming Mode (Dell)",
}

# Continuous features -> rendered as sliders. Range is 0..max (max via getvcp).
CONTINUOUS: set[int] = {0x10, 0x12, 0x16, 0x18, 0x1A, 0x62, 0x6C, 0x6E, 0x70, 0x87, 0x8A}

# --- Dell enum value maps ---------------------------------------------------
_VCP14 = {  # 0x14 Basic colour / colour-temperature preset
    0x01: "sRGB", 0x04: "5000K", 0x05: "6500K", 0x06: "7500K",
    0x08: "Cool (9300K)", 0x09: "10000K", 0x0B: "Warm (5700K)", 0x0C: "Custom Colour",
}
_VCP60 = {  # 0x60 Input source
    0x01: "VGA-1", 0x02: "VGA-2", 0x03: "DVI-1", 0x04: "DVI-2",
    0x0F: "DisplayPort-1", 0x10: "mDP-1", 0x11: "HDMI-1", 0x12: "HDMI-2",
    0x13: "DisplayPort-2", 0x14: "mDP-2", 0x15: "HDMI-3", 0x16: "HDMI-4",
    0x17: "DisplayPort-3", 0x19: "Thunderbolt-1", 0x1A: "Thunderbolt-2",
    0x1B: "USB-C-1", 0x1C: "USB-C-2", 0x1D: "USB-C-3", 0x1E: "USB-C-4",
}
_VCPDC = {  # 0xDC Display / picture mode
    0x00: "Standard", 0x02: "Multimedia", 0x03: "Movie",
    0x04: "Nature", 0x05: "Game", 0x06: "Sport",
}
_VCPE2 = {  # 0xE2 Dell "Preset Modes Specific" (master preset)
    0x00: "Standard", 0x01: "Multimedia", 0x02: "Movie", 0x03: "Nature",
    0x04: "Game", 0x05: "Sport", 0x06: "Text", 0x07: "AdobeRGB", 0x08: "xvMode",
    0x09: "DICOM", 0x0A: "CAL1", 0x0B: "sRGB", 0x0C: "5000K", 0x0D: "5700K",
    0x0E: "Warm", 0x0F: "6500K", 0x10: "7500K", 0x11: "9300K", 0x12: "Cool",
    0x13: "10000K", 0x14: "Custom Colour", 0x15: "CAL2", 0x16: "CAL3",
    0x18: "Metro", 0x19: "Paper", 0x1A: "Rec.709 / BT.709", 0x1B: "DCI-P3",
    0x1C: "Rec.2020 / BT.2020", 0x1D: "ComfortView", 0x1E: "Game2", 0x1F: "Game3",
    0x20: "FPS Game", 0x21: "RTS Game", 0x22: "RPG Game", 0x23: "Movie HDR",
    0x24: "Game HDR", 0x25: "Standard HDR", 0x26: "Vivid HDR", 0x27: "Desktop",
    0x28: "Reference", 0x29: "Multiscreen Match", 0x2F: "Sports Game",
    0x30: "Custom Colour HDR", 0x31: "HDR Peak 1000", 0x3A: "DisplayHDR",
    0x3B: "HDR10", 0x3C: "HLG", 0x3D: "Display P3", 0x7F: "Presets Disabled",
}
_VCPF0 = {  # 0xF0 Dell "HDR Modes Specific"
    0x01: "Text", 0x02: "AdobeRGB", 0x03: "xvMode", 0x04: "DICOM", 0x05: "CAL1",
    0x09: "Rec.709 / BT.709", 0x0A: "DCI-P3", 0x0B: "Rec.2020 / BT.2020",
    0x0C: "ComfortView", 0x0D: "Game2", 0x0E: "Game3", 0x0F: "FPS Game",
    0x10: "RTS Game", 0x11: "RPG Game", 0x30: "Standard HDR", 0x31: "Movie HDR",
    0x32: "Game HDR", 0x33: "Vivid HDR", 0x34: "Desktop", 0x35: "Reference",
    0x36: "DisplayHDR", 0x37: "HDR10", 0x38: "HLG", 0x3A: "HDR Peak 1000",
}
_VCPAA = {0x01: "0°", 0x02: "90°", 0x03: "180°", 0x04: "270°"}
_VCPD6 = {0x01: "On", 0x04: "Standby / Suspend", 0x05: "Off (write-only)"}
_VCPCC = {  # 0xCC OSD language
    0x01: "Chinese (Traditional)", 0x02: "English", 0x03: "French",
    0x04: "German", 0x05: "Italian", 0x06: "Japanese", 0x07: "Korean",
    0x08: "Portuguese", 0x09: "Russian", 0x0A: "Spanish", 0x0B: "Swedish",
    0x0C: "Turkish", 0x0D: "Chinese (Simplified)", 0x0E: "Portuguese (Brazil)",
}

_VCP8D = {0x01: "Muted", 0x02: "Unmuted"}  # 0x8D Audio Mute (standard MCCS)
ENUM_LABELS: dict[int, dict[int, str]] = {
    0x14: _VCP14, 0x60: _VCP60, 0xDC: _VCPDC, 0xE2: _VCPE2,
    0xF0: _VCPF0, 0xAA: _VCPAA, 0xD6: _VCPD6, 0xCC: _VCPCC, 0x8D: _VCP8D,
}

# Disruptive writes that get a confirmation prompt before applying.
CONFIRM_CODES: set[int] = {0x60, 0xD6}

# Preferred display order in the panel; unlisted editable codes are appended.
DISPLAY_ORDER: list[int] = [
    0x10, 0x12, 0x87,             # image basics
    0xE2, 0xDC, 0x14, 0xF0, 0xF4, # presets / modes
    0x16, 0x18, 0x1A,             # RGB gain
    0x8A, 0x62, 0x8D,             # saturation / volume / mute
    0x6C, 0x6E, 0x70,             # black levels
    0x60, 0xCC, 0xAA, 0xD6,       # input / osd / power
]


# --- merged "Colour Preset" (Dell-style) -----------------------------------
# On panels like the P2425D the OSD's single "Preset Modes" list is split over
# two DDC opcodes: picture modes on 0xDC, colour temperatures on 0x14. 0xE2 is a
# read-only register that reports whichever single preset is active. We merge the
# two writable opcodes into one dropdown (as Dell's app does) and use the E2
# value only to highlight the current selection. Each entry is
# (write_code, write_value) -> (label, e2_readback_value); e2 values verified by
# probing and matching VcpCodeList.VCPE2.
_PRESET_FROM_DC: dict[int, tuple[str, int]] = {
    0x00: ("Standard", 0x00), 0x02: ("Multimedia", 0x01), 0x03: ("Movie", 0x02),
    0x04: ("Nature", 0x03), 0x05: ("Game", 0x04), 0x06: ("Sport", 0x05),
}
_PRESET_FROM_14: dict[int, tuple[str, int]] = {
    0x01: ("sRGB", 0x0B), 0x04: ("5000K", 0x0C), 0x05: ("Standard", 0x00),  # 6500K == Standard
    0x06: ("7500K", 0x10), 0x08: ("Cool", 0x12), 0x09: ("10000K", 0x13),
    0x0B: ("Warm", 0x0E), 0x0C: ("Custom Colour", 0x14),
}
# 0xF0 also carries presets on some panels (e.g. ComfortView on the P2319H, and
# HDR/wide-gamut modes on capable ones). Probing showed: writing F0=0x0C turns
# ComfortView ON and 0xE2 then reports 0x1D; writing F0=0x00 is REJECTED (a dead
# value) — you leave ComfortView by selecting another preset (which writes 0xDC/
# 0x14). So we fold the non-zero F0 values into the merged preset and drop the
# useless 0x00. e2 read-back values come from matching each F0 name to VCPE2.
_E2_BY_NAME: dict[str, int] = {name: val for val, name in _VCPE2.items()}
_PRESET_FROM_F0: dict[int, tuple[str, int]] = {
    val: (name, _E2_BY_NAME[name])
    for val, name in _VCPF0.items()
    if val != 0x00 and name in _E2_BY_NAME
}

# Synthetic code used to key the merged control (reads back through real 0xE2).
PRESET_CODE = 0xE2


class PresetItem:
    __slots__ = ("label", "write_code", "write_value", "e2_value")

    def __init__(self, label: str, write_code: int, write_value: int, e2_value: int):
        self.label = label
        self.write_code = write_code
        self.write_value = write_value
        self.e2_value = e2_value


def has_merged_preset(caps: dict[int, list[int] | None]) -> bool:
    return bool(caps.get(0xDC)) and bool(caps.get(0x14))


def f0_fully_folded(caps: dict[int, list[int] | None]) -> bool:
    """True if every non-zero 0xF0 value the panel advertises can be folded into
    the merged preset (so 0xF0 need not be shown as a separate control)."""
    f0 = caps.get(0xF0)
    if not f0:
        return False
    return all(v == 0x00 or v in _PRESET_FROM_F0 for v in f0)


def build_preset_items(caps: dict[int, list[int] | None]) -> list[PresetItem]:
    """Merge the panel's preset-carrying opcodes into one list, Dell-style:
    picture modes (0xDC), then ComfortView / HDR presets (0xF0), then colour
    temperatures (0x14). The dead 0xF0=0x00 "off" value is dropped — you leave
    those presets by picking another entry (which writes 0xDC/0x14)."""
    items: list[PresetItem] = []
    seen: set[str] = set()

    def add(source: dict[int, tuple[str, int]], write_code: int, values):
        for value in values or []:
            if value in source:
                label, e2 = source[value]
                if label not in seen:
                    items.append(PresetItem(label, write_code, value, e2))
                    seen.add(label)

    add(_PRESET_FROM_DC, 0xDC, caps.get(0xDC))       # Standard / Movie / Game …
    add(_PRESET_FROM_F0, 0xF0, caps.get(0xF0))       # ComfortView / HDR presets
    add(_PRESET_FROM_14, 0x14, caps.get(0x14))       # colour temperatures
    return items


# --- USB-C Prioritization / MST bandwidth (Dell two-level 0xEA, sub-code 0xF8) --
# From DDPM FormatVCP_EAF8: F800=High Resolution, F801=High Data Speed,
# F810=FHD, F811=4K. On USB-C hub monitors this is the video-vs-USB bandwidth
# split; on this panel it's tied to MST (only takes effect when DisplayPort
# daisy-chaining is active). Written as a 16-bit word (SH=0xF8 sub-code,
# SL=value). It re-negotiates the link, so it's a write-only, confirm-required
# control.
USBC_PRIORITY_CODE = 0xEA
USBC_PRIORITY_OPTIONS: list[tuple[int, str]] = [
    (0xF800, "High Resolution"),
    (0xF801, "High Data Speed"),
]


def has_usbc_priority(caps: dict[int, list[int] | None]) -> bool:
    vals = caps.get(0xEA)
    return bool(vals) and 0xF8 in vals


def feature_name(code: int) -> str:
    if code == PRESET_CODE:
        return "Colour Preset"
    if code == USBC_PRIORITY_CODE:
        return "USB-C Prioritization"
    return FEATURE_NAMES.get(code, f"VCP 0x{code:02X}")


# --- per-monitor sub-tab grouping -------------------------------------------
# Each monitor's panel is a QTabWidget of these sub-tabs, in this order.
# "Information" is a read-only identity/status tab (no controls) and is always
# leftmost. "MST" is rightmost. Control codes map to Settings / Color·Picture via
# feature_category; the MST tab (MST enable + USB-C Prioritization) is handled
# explicitly in the panel.
TAB_ORDER: list[str] = ["Information", "Settings", "Color / Picture", "PIP / PBP", "MST", "KVM"]

# "Settings" tab: input / OSD language / power / monitor audio (volume + mute).
_SETTINGS_CODES: set[int] = {0x60, 0xCC, 0xD6, 0x62, 0x8D}


def feature_category(code: int) -> str:
    """Which control sub-tab a feature belongs in (one of TAB_ORDER, never
    'Information'/'PIP / PBP'/'MST'/'KVM'). Everything not in Settings is image-related →
    Color / Picture (brightness, contrast, sharpness, colour preset, RGB gain).
    The PIP/PBP, MST and KVM codes are routed to their own tabs explicitly in the
    panel, so they never reach here."""
    if code in _SETTINGS_CODES:
        return "Settings"
    return "Color / Picture"


# --- PIP / PBP (Picture-in-Picture / Picture-by-Picture) --------------------
# From DDPM RE (DDPM.SA.Plugins.User.DeviceManager setter IL). 0xE9 is a
# mode/command register:
#   0x00 = Off, 0x21 = PIP small, 0x22 = PIP large, 0x24 (+ other advertised
#   values 0x28-0x2F / 0x31-0x35 / 0x41.. from DDPM's PbpModes set) = PBP layouts.
#   0x01 / 0x02 are *command* values (toggle PIP size / cycle PIP position), NOT
#   selectable modes — surfaced as buttons.
# 0xE8 = sub-window input source (encoded like the main input 0x60).
# 0xE5 = read-only status. PIP/PBP only shows a second image with two active inputs.
# Reverse-engineered from DDPM; verified working on the P3424WE (see DDC_ROADMAP.md).
PIP_MODE_CODE = 0xE9
PIP_SUBINPUT_CODE = 0xE8
PIP_STATUS_CODE = 0xE5
PIP_TOGGLE_SIZE = 0x01       # setvcp E9 0x01 — toggle PIP small <-> large
PIP_TOGGLE_POSITION = 0x02   # setvcp E9 0x02 — cycle PIP corner
_PIP_COMMAND_VALUES: set[int] = {PIP_TOGGLE_SIZE, PIP_TOGGLE_POSITION}

PIP_MODE_LABELS: dict[int, str] = {
    0x00: "Off",
    0x21: "PIP — Small",
    0x22: "PIP — Large",
    0x24: "PBP — 2 windows",
}


def has_pip(caps: dict[int, list[int] | None]) -> bool:
    """Whether the monitor advertises the PIP/PBP mode register (0xE9)."""
    return PIP_MODE_CODE in caps


def pip_modes(caps: dict[int, list[int] | None]) -> list[tuple[int, str]]:
    """Selectable PIP/PBP modes advertised on 0xE9, excluding the command values
    0x01/0x02 (the size/position toggles). Always includes Off first."""
    vals = caps.get(PIP_MODE_CODE) or []
    modes: list[tuple[int, str]] = []
    for v in vals:
        if v in _PIP_COMMAND_VALUES:
            continue
        label = "Off" if v == 0x00 else PIP_MODE_LABELS.get(v, f"PBP (0x{v:02X})")
        modes.append((v, label))
    if not any(v == 0x00 for v, _ in modes):
        modes.insert(0, (0x00, "Off"))
    return modes


def has_pip_size_toggle(caps: dict[int, list[int] | None]) -> bool:
    return PIP_TOGGLE_SIZE in (caps.get(PIP_MODE_CODE) or [])


def has_pip_position_toggle(caps: dict[int, list[int] | None]) -> bool:
    return PIP_TOGGLE_POSITION in (caps.get(PIP_MODE_CODE) or [])


def input_labels_for(values: list[int]) -> dict[int, str]:
    """Map input-source values (0x60 encoding, reused for the 0xE8 sub-window
    input) to friendly labels."""
    return {v: _VCP60.get(v, f"0x{v:02X}") for v in values}


# --- Factory reset (standard MCCS 0x04) -------------------------------------
# Write-only command: `setvcp 04 01` restores ALL of the monitor's settings to
# factory defaults. Advertised by essentially every Dell. Offered as a button
# (not a normal control), gated on the monitor advertising 0x04.
FACTORY_RESET_CODE = 0x04
FACTORY_RESET_VALUE = 0x01


def has_factory_reset(caps: dict[int, list[int] | None]) -> bool:
    return FACTORY_RESET_CODE in caps


# --- MST (Multi-Stream Transport, DisplayPort daisy-chaining) ---------------
# 0xEF is a Dell manufacturer register. DDPM (DisplayVCPMethod.GetEFSupport) treats
# it in one of two ways, chosen by parsing the *advertised* 0xEF capability values
# as hex and testing bit 15 (>= 0x8000):
#   * "new spec" (an advertised value >= 0x8000): 0xEF is a bitmask — MST enable is
#     bit 4 (0x10), iMST bit 5, Hybrid PBP bit 6; support flags in bits 12-15.
#   * "old spec" (all advertised values < 0x8000, e.g. EF(00 01 0F)): a plain enum.
# HARDWARE FINDING (P2725HE, old spec, EF(00 01 0F)): writing 0xEF does NOT toggle
# MST. With MST off vs. on (enabled from the OSD, second monitor daisy-chained and
# enumerating) 0xEF reads 0x00 in BOTH states — it does not encode MST enable at
# all. A written 0x01 sticks but has no effect; 0x0F won't even store. So on
# old-spec monitors MST enable is OSD-only / not DDC-controllable. We therefore only
# offer the DDC toggle on new-spec monitors (where the bit-4 logic applies); on
# old-spec ones the MST tab shows an "OSD-only" note instead of a dead control.
MST_CODE = 0xEF
MST_ENABLE_BIT = 4  # 0x10


def has_mst(caps: dict[int, list[int] | None]) -> bool:
    """Whether the monitor exposes 0xEF at all (drives the MST tab's presence)."""
    return 0xEF in caps


def has_ddc_mst_control(caps: dict[int, list[int] | None]) -> bool:
    """Whether MST enable can actually be toggled over DDC — only true for
    'new-spec' 0xEF monitors (an advertised value with bit 15 set). Old-spec
    monitors advertise 0xEF but MST enable is OSD-only (verified on the P2725HE)."""
    vals = caps.get(0xEF)
    return bool(vals) and any(v >= 0x8000 for v in vals)


# --- USB KVM (share one keyboard/mouse between PCs, over DDC) ----------------
# RE'd from DDPM (DDPM.UI.Module.Kvm.KvmViewModel + DisplayVCPMethod). A monitor
# with a built-in USB KVM shares its USB hub (keyboard/mouse) between the PCs on
# its different video inputs. Two DDC controls:
#
#   * Input switch = standard Input Source 0x60. The USB hub follows the active
#     video input, so writing 0x60 IS the KVM switch (already our input control;
#     the KVM tab re-presents it with an explicit "switch" action + warning).
#   * USB-upstream association = 0xE7, a Dell TWO-LEVEL 16-bit word 0xFF0N:
#         0xFF00 = Auto  — USB follows whichever input is active ("current USB"),
#         0xFF01..0xFF04 = pin the shared USB to computer/window 1..4.
#     Valid range 0xFF00..0xFF04 (KvmViewModel WindowList_Selected setter +
#     OnVCPChangedEvent decode). Written as a word (set_vcp_word), and it reads
#     back as 0xFF0N — so, unlike 0xEA USB-C Prioritization, it is *verifiable*.
#     Auto (0xFF00) is only offered when the 0xE7 capability advertises value 0xFE
#     (DisplayVCPMethod.isSupportedCurrentUSB → "E7 have FE").
#
# We gate the KVM tab on the monitor advertising 0xE7 (mirrors the MST tab gating
# on 0xEF). The number of USB-upstream slots isn't in the caps string in a
# decoded form (DDPM derives it from the active PIP/PBP layout), so we offer one
# slot per selectable input (a PC per input), clamped to 2..4; writes are
# verified, so an unsupported slot simply reports "did not take".
USB_KVM_CODE = 0xE7
USB_KVM_AUTO = 0xFF00           # 0xE7 word: USB follows the active input
_USB_KVM_AUTO_FLAG = 0xFE       # advertised 0xE7 cap value that enables Auto mode
USB_KVM_MAX_SLOTS = 4


def has_usb_kvm(caps: dict[int, list[int] | None]) -> bool:
    """Whether the monitor exposes the USB-KVM upstream register (0xE7). Drives
    the KVM tab's presence, like has_mst drives the MST tab."""
    return USB_KVM_CODE in caps


def usb_kvm_auto_supported(caps: dict[int, list[int] | None]) -> bool:
    """Whether 'Auto' (0xFF00, USB follows the active input) is offered — true
    when the 0xE7 capability advertises value 0xFE (isSupportedCurrentUSB)."""
    return _USB_KVM_AUTO_FLAG in (caps.get(USB_KVM_CODE) or [])


def usb_kvm_upstream_controllable(caps: dict[int, list[int] | None]) -> bool:
    """Whether the '0xFF0N current-USB' regime applies — a monitor signals it by
    advertising 0xFE on 0xE7 (DisplayVCPMethod.isSupportedCurrentUSB). That regime
    uses a single word (Auto / pin-to-computer). Monitors WITHOUT 0xFE use the
    bit-packed per-input regime instead (see usb_kvm_bitpacked). The two are
    mutually exclusive."""
    return usb_kvm_auto_supported(caps)


def usb_kvm_slot_count(caps: dict[int, list[int] | None]) -> int:
    """How many manual USB-upstream slots to offer — one per selectable input
    (a PC per input), clamped to 2..USB_KVM_MAX_SLOTS."""
    n = len(caps.get(0x60) or [])
    return max(2, min(USB_KVM_MAX_SLOTS, n or 2))


def usb_kvm_options(caps: dict[int, list[int] | None]) -> list[tuple[int, str]]:
    """(word, label) options for the USB-upstream dropdown: Auto (if supported)
    first, then Computer 1..N. Each word is written to 0xE7 as-is."""
    opts: list[tuple[int, str]] = []
    if usb_kvm_auto_supported(caps):
        opts.append((USB_KVM_AUTO, "Auto — USB follows the active input"))
    for n in range(1, usb_kvm_slot_count(caps) + 1):
        opts.append((USB_KVM_AUTO + n, f"Computer {n}"))
    return opts


# --- bit-packed per-input USB-upstream regime (0xE7 without 0xFE) ------------
# HARDWARE-CONFIRMED on the P3424WE by OSD-correlation (three predict-then-confirm
# steps): 0xE7 is a 16-bit word where each video input owns a 2-bit field at
# bit (14 - 2*index), index = the input's position in the advertised 0x60 list.
# The 2-bit value is the USB-upstream index (0 = USB-C, 1 = USB-B, ...), i.e. which
# upstream port feeds that input's KVM. Inputs that carry USB natively (USB-C /
# Thunderbolt) self-pair, so they get no selector. The advertised 0xE7 cap values
# ARE the valid upstream indices (e.g. "00 01" = two upstreams). Read: (w>>pos)&3;
# write: replace the 2-bit field and setvcp E7 <full word> (read-modify-write).
USB_KVM_FIELD_TOP_BIT = 14
_USB_NATIVE_INPUTS: set[int] = {0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E}  # TB1/2, USB-C 1-4


def usb_kvm_bitpacked(caps: dict[int, list[int] | None]) -> bool:
    """The bit-packed per-input USB-upstream regime: 0xE7 advertised, NOT the 0xFE
    current-USB regime, and the advertised values look like small upstream indices
    (0..3). HARDWARE-CONFIRMED on the P3424WE."""
    if not has_usb_kvm(caps) or usb_kvm_auto_supported(caps):
        return False
    vals = caps.get(USB_KVM_CODE) or []
    return bool(vals) and all(0 <= v <= 3 for v in vals)


def usb_kvm_upstream_indices(caps: dict[int, list[int] | None]) -> list[int]:
    """Valid USB-upstream indices advertised on 0xE7 (e.g. [0, 1] = USB-C, USB-B)."""
    return sorted(v for v in (caps.get(USB_KVM_CODE) or []) if 0 <= v <= 3)


def usb_kvm_pairings(caps: dict[int, list[int] | None]) -> list[tuple[int, int]]:
    """(input_code, field_bit_position) for each input that needs a USB-upstream
    selector — i.e. non-USB-C/Thunderbolt inputs (those self-pair). Field position
    is 14 - 2*index over the advertised 0x60 order."""
    out: list[tuple[int, int]] = []
    for i, code in enumerate(caps.get(0x60) or []):
        pos = USB_KVM_FIELD_TOP_BIT - 2 * i
        if pos < 0:
            break
        if code not in _USB_NATIVE_INPUTS:
            out.append((code, pos))
    return out


def usb_upstream_label(index: int) -> str:
    """Friendly name for a USB-upstream index. 0/1 are the standard Dell two-upstream
    layout (USB-C / USB-B); higher indices fall back to a generic label."""
    return {0: "USB-C", 1: "USB-B"}.get(index, f"Upstream {index + 1}")


def usb_kvm_field_value(word: int, pos: int) -> int:
    """Extract the 2-bit USB-upstream field at bit position ``pos``."""
    return (word >> pos) & 0b11


def usb_kvm_set_field(word: int, pos: int, value: int) -> int:
    """Read-modify-write: set the 2-bit field at ``pos`` to ``value`` (0..3)."""
    return (word & ~(0b11 << pos)) | ((value & 0b11) << pos)


def feature_kind(code: int, advertised_values: list[int] | None) -> str | None:
    """Return 'continuous', 'enum', or None (not shown)."""
    if code in CONTINUOUS:
        return "continuous"
    if code in ENUM_LABELS and advertised_values:
        return "enum"
    return None


def enum_label(code: int, value: int) -> str:
    return ENUM_LABELS.get(code, {}).get(value, f"0x{value:02X}")


def ordered_editable(caps: dict[int, list[int] | None]) -> list[int]:
    """Editable codes present in caps, in preferred display order.

    Note: this is purely structural (based on the capability string). Read-only
    features (per ddcutil's MCCS metadata, e.g. 0xAA Screen Orientation) are
    filtered separately by the caller, which has ddcutil access.
    """
    editable = [c for c, v in caps.items() if feature_kind(c, v) is not None]

    # 0xE2 is a read-only status register on these panels — never an individual
    # control. When both 0xDC (picture modes) and 0x14 (colour temps) exist they
    # are merged into one "Colour Preset" control (see build_preset_items), so
    # drop the individual DC and 0x14 here; the panel inserts the merged control.
    if 0xE2 in editable:
        editable.remove(0xE2)
    if has_merged_preset(caps):
        for c in (0xDC, 0x14):
            if c in editable:
                editable.remove(c)
        # 0xF0 (ComfortView / HDR presets) is folded into the merged preset too,
        # but only drop the standalone control if we can fold *all* its values —
        # otherwise keep it so nothing becomes unreachable.
        if 0xF0 in editable and f0_fully_folded(caps):
            editable.remove(0xF0)

    ordered = [c for c in DISPLAY_ORDER if c in editable]
    ordered += [c for c in editable if c not in ordered]
    return ordered
