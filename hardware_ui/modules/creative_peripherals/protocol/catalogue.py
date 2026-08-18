"""Message builders and the EQ preset table.

Keeps the GUI free of byte layouts. Every builder below corresponds to a frame
observed in the Windows USB capture.
"""

from __future__ import annotations

import struct

from . import framing
from .ids import (Cmd, EQ_BAND_CMDS, Feature, FeatureOp, Module, OutputTarget,
                  Playback, ProfileType)

# -- feature toggles (cmd 57) ----------------------------------------------


def set_feature(feature: int, on: bool) -> tuple[int, bytes]:
    """`5a 39 03 00 <bit> <0|1>` — RawCmdFeatureControlSet."""
    return Cmd.FEATURE_CONTROL, bytes([FeatureOp.SET, int(feature), 1 if on else 0])


def get_feature_state() -> tuple[int, bytes]:
    """`5a 39 01 01` — returns the whole bitfield."""
    return Cmd.FEATURE_CONTROL, bytes([FeatureOp.GET])


def get_feature_support() -> tuple[int, bytes]:
    """`5a 39 01 02` — mask of features this unit implements."""
    return Cmd.FEATURE_CONTROL, bytes([FeatureOp.SUPPORT])


def parse_feature_state(payload: bytes) -> int | None:
    """FeatureControl op1 reply -> the 32-bit state mask.

    Reply is `01 <u32 LE>`; on hardware `01 60 00 10 04` with bit 0x20 set for
    Direct Mode. Returns None if this isn't an op1 reply.
    """
    if len(payload) < 5 or payload[0] != FeatureOp.GET:
        return None
    return struct.unpack_from("<I", payload, 1)[0]


def parse_feature_support(payload: bytes) -> int | None:
    if len(payload) < 5 or payload[0] != FeatureOp.SUPPORT:
        return None
    return struct.unpack_from("<I", payload, 1)[0]


# -- output routing (cmd 44) ----------------------------------------------


def set_output(target: OutputTarget) -> tuple[int, bytes]:
    """`5a 2c 05 00 <u32 mask>`."""
    return Cmd.SPEAKER_OUTPUT_TARGET, bytes([0]) + struct.pack("<I", int(target))


def get_output() -> tuple[int, bytes]:
    return Cmd.SPEAKER_OUTPUT_TARGET, bytes([1])


def parse_output(payload: bytes) -> int | None:
    """`01 <u32 mask>` -> mask. Hardware returned `01 04 00 00 00` = Headphones."""
    if len(payload) < 5:
        return None
    return struct.unpack_from("<I", payload, 1)[0]


# -- Super X-Fi (cmd 38 button / cmd 111 mode) ----------------------------


def set_sxfi(on: bool) -> tuple[int, bytes]:
    """`5a 26 05 07 1e 00 <0|1> 00` — button 30 via HardwareButton op 7."""
    from .ids import ButtonID
    return Cmd.HARDWARE_BUTTON, bytes([7, int(ButtonID.SXFI_ON_OFF), 0,
                                       1 if on else 0, 0])


def set_button(button: int, on: bool) -> tuple[int, bytes]:
    """`5a 26 05 07 <id> 00 <0|1> 00` — HardwareButton op 7.

    Generalises the captured Super X-Fi write, which is this command with
    button 30. Scout Mode (2) and SBX (1) use the same shape; those two are
    derived from the capture rather than observed directly, so treat a silent
    device as "this unit does not have that button".
    """
    return Cmd.HARDWARE_BUTTON, bytes([7, int(button), 0, 1 if on else 0, 0])


def get_button(button: int) -> tuple[int, bytes]:
    """`5a 26 03 01 <id> 00` — read one button's state."""
    return Cmd.HARDWARE_BUTTON, bytes([1, int(button), 0])


def set_sxfi_mode(code: str) -> tuple[int, bytes]:
    """`5a 6f 05 0c <4 ASCII>` — e.g. 'PCG ' for Battle Mode."""
    raw = code.encode("ascii")[:4].ljust(4, b" ")
    return Cmd.SUPER_XFI, bytes([0x0C]) + raw


# -- graphic EQ (cmd 18 / cmd 26) -----------------------------------------


def set_eq_enabled(on: bool) -> tuple[int, bytes]:
    return Cmd.SET_MALCOLM_PARAM, framing.set_effect(
        Module.PLAYBACK, Playback.GRAPHIC_EQ_ENABLE, 1.0 if on else 0.0)


def set_eq_preamp(db: float) -> tuple[int, bytes]:
    return Cmd.SET_MALCOLM_PARAM, framing.set_effect(
        Module.PLAYBACK, Playback.GRAPHIC_EQ_PREAMP, db)


def set_eq_band(index: int, db: float) -> tuple[int, bytes]:
    if not 0 <= index < len(EQ_BAND_CMDS):
        raise ValueError(f"band index out of range: {index}")
    return Cmd.SET_MALCOLM_PARAM, framing.set_effect(
        Module.PLAYBACK, EQ_BAND_CMDS[index], db)


def get_eq_param(command: int) -> tuple[int, bytes]:
    return Cmd.GET_MALCOLM_PARAM, framing.get_effect(Module.PLAYBACK, command)


def write_profile(bands: tuple[float, ...] | list[float], preamp: float,
                  index: int = 0,
                  ptype: ProfileType = ProfileType.DEVICE) -> tuple[int, bytes]:
    """Write preamp + all ten band gains in ONE frame (`cmd 23`).

    The Windows app uses this instead of eleven separate SetMalcolmParameter
    writes. Layout captured verbatim:

        5a 17 53 00 02 02 00 46 00 00 | 0b 00 00 00 | 96 0a <f32> ... 96 14 <f32>
                 op ty ix  ?  ?  ?     count(u32)     preamp + bands 0..9

    The three bytes after the index were constant (`46 00 00`) across every
    captured write, so they are reproduced as-is rather than guessed at.
    """
    if len(bands) != 10:
        raise ValueError(f"expected 10 band gains, got {len(bands)}")
    params = [(int(Playback.GRAPHIC_EQ_PREAMP), float(preamp))]
    params += [(EQ_BAND_CMDS[i], float(bands[i])) for i in range(10)]
    body = b"".join(bytes([int(Module.PLAYBACK), pid]) + struct.pack("<f", val)
                    for pid, val in params)
    header = bytes([0, int(ptype), 2, index, 0x46, 0, 0])
    return Cmd.MALCOLM_PROFILE_DATA, (header + struct.pack("<I", len(params))
                                      + body + bytes(6))


#: The device stores this many equaliser profiles of its own.
PROFILE_SLOTS = 4

#: The vendor app refuses longer profile names ("Maximum 16 characters.").
PROFILE_NAME_MAX = 16


def set_profile_name(name: str,
                     ptype: ProfileType = ProfileType.DEVICE) -> tuple[int, bytes]:
    """Name the profile being written (`cmd 24`).

    The Windows app always sends this immediately before `write_profile`, so the
    card knows what the stored curve is called. Captured layout:

        5a 18 <len> 00 02 02 <n> <n:u16> <name UTF-16LE>
                             ^^ ^^ ^^
                             op ty const

    `n` is the character count, repeated as a byte and as a little-endian
    uint16 — reproduced exactly as observed rather than guessed at. Example, for
    "Acoustic": `00 02 02 08 08 00` then eight UTF-16LE characters.
    """
    if not name:
        raise ValueError("profile name must not be empty")
    if len(name) > PROFILE_NAME_MAX:
        raise ValueError(
            f"profile name is limited to {PROFILE_NAME_MAX} characters, "
            f"got {len(name)}")
    body = name.encode("utf-16-le")
    return (Cmd.MALCOLM_PROFILE_NAME,
            bytes([0, int(ptype), 2, len(name)])
            + struct.pack("<H", len(name)) + body)


def get_profile_name(index: int = 0,
                     ptype: ProfileType = ProfileType.DEVICE) -> tuple[int, bytes]:
    return Cmd.MALCOLM_PROFILE_NAME, bytes([1, int(ptype), index])


def select_profile(index: int,
                   ptype: ProfileType = ProfileType.DEVICE) -> tuple[int, bytes]:
    """`5a 1a 03 00 02 <index>` — ActiveMalcolmProfile."""
    if not 0 <= index < PROFILE_SLOTS:
        raise ValueError(f"profile slot out of range: {index}")
    return Cmd.ACTIVE_MALCOLM_PROFILE, bytes([0, int(ptype), index])


# -- device info -----------------------------------------------------------


def get_firmware() -> tuple[int, bytes]:
    return Cmd.DEVICE_INFO_V2, bytes([2])


def get_serial() -> tuple[int, bytes]:
    return Cmd.DEVICE_INFO_V2, bytes([3])


def get_max_payload() -> tuple[int, bytes]:
    return Cmd.MAX_PAYLOAD_SIZE, b""


def get_subfeatures() -> tuple[int, bytes]:
    return Cmd.SUBFEATURE_SUPPORT, b""


# -- EQ presets ------------------------------------------------------------
#
# Band gains for the ten bands, in dB. Two independent sources agree exactly:
#
#   1. the Windows USB capture — the app writes each band individually when a
#      preset is chosen, and "Flat" arriving as all zeros confirmed the ordering;
#   2. the Android app's own preset table, which stores the same gains.
#
# The device holds presets by *index* only (cmd 24 replies are bare echoes), so
# both the names and the gains are host-side data and must live here.

#: name -> (band0..band9 gains in dB, preamp dB)
EQ_PRESETS: dict[str, tuple[tuple[float, ...], float]] = {
    "Flat":      ((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0), 0.0),
    "Acoustic":  ((0.0, 0.0, 2.0, 0.0, 0.0, 2.5, 2.0, 2.0, 2.0, 3.0), 0.0),
    "Classical": ((0.0, 0.0, 1.5, 2.0, 1.0, 3.0, 1.0, 0.0, 3.0, 3.0), 0.0),
    "Country":   ((0.0, 0.0, 0.0, 0.0, 2.0, 2.5, 2.5, 2.0, 3.0, 3.0), 0.0),
    "Dance":     ((0.0, 0.0, 1.0, 1.5, 0.5, 1.5, 2.0, 1.0, 2.5, 2.5), 0.0),
    "Hip Hop":   ((0.0, 0.0, 2.0, 2.5, 1.5, 2.5, 0.0, -2.5, -1.0, -1.5), 0.0),
    "Jazz":      ((0.0, 0.0, 0.0, 2.5, 2.5, 3.0, 1.0, 1.0, 3.0, 3.0), 0.0),
    "Pop":       ((0.0, 0.0, 1.0, 1.0, 0.0, 2.5, 0.5, 0.5, 3.0, 3.0), 0.0),
    "R&B":       ((0.0, 0.0, 0.0, -1.5, 2.5, 1.5, 2.0, 0.0, 2.0, 1.0), 0.0),
    "Rock":      ((0.0, 0.0, 2.0, 1.0, 0.0, 3.0, 1.0, -1.0, 3.0, 3.0), 0.0),
    "Vocal":     ((0.0, 0.0, -1.5, -0.5, 3.0, 3.0, 3.0, 0.0, 0.0, 1.0), 0.0),
}

#: The per-game presets (Fortnite, Valorant, Battlefield Series, ...) are NOT
#: here. The installer ships no gains for them: the Creative App downloads them
#: at runtime into a per-machine data directory. Import them from your own
#: install with `tools/import_vendor_data.py` — see `protocol/presets.py`, which
#: merges the imported set with the table above.
#:
#: Those files also revealed that the vendor tunes Speaker and Headphone
#: separately, which the table above cannot express: its gains were captured
#: with the device routed to headphones and match the vendor headphone curve
#: exactly for all nine presets common to both sets.
