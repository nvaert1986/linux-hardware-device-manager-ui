"""Byte layout of an 8BitDo Xbox wired controller's stored configuration.

Ported from ``plasma-creative``-era sibling project ``8bitdo-cfg`` (``gui/bit8/fieldmap.py`` plus
``FIELD_MAP.md``), where it was decoded by the capture-diff method -- change exactly one setting in
the vendor app, sync, diff the logged packets -- and cross-checked against ``libadvance-lib.so``.
That work was validated against real hardware over both transports, so these tables are the
module's most valuable inheritance and are carried across unchanged except where noted below.

**Two records, one layout.** The device stores three profiles:

``0x01`` super-config, 532 bytes
    ``[crc:2][curslot:1][mode:1]`` then three 176-byte slots. This is what gets **written**.
``0x0d`` per-slot record, 176 bytes
    What gets **read** back, one slot at a time. Same field offsets within the slot.

Every per-slot offset here is relative to :func:`slot_base`, so it shifts by 176 per profile.

**Verification status**, kept from the source rather than flattened, because a value confirmed on
hardware and one inferred by elimination deserve different levels of trust:

* Confirmed by isolated captures: the A/B/X/Y map slots and the B/LB/RB/LT output codes; the
  L3/View/Menu slots; the D-Up/Left/Right slots; the RT code; both paddles (see below).
* Inferred by identity and elimination: the LB/RB/LT/RT map slots (they hold their own identity
  code by default), the R3 code, and the D-Down slot and code.

One correction was made during this port. ``configure.py`` in the source had ``PADDLE_R`` at
offset 124, while ``fieldmap.py`` had it at 116. The captured record settles it: in a profile
documented as "paddles mapped to LT/RT", offset 116 holds ``0x8000`` (RT) and 120 holds ``0x4000``
(LT), while **offset 124 is the second copy of the ``11 09 20 20`` record marker**. Writing a
paddle there would have corrupted the record, so 116/120 is right and the other was a latent bug.
"""

from __future__ import annotations

#: Record sizes. A slot is a slot whichever command carried it.
SLOT_LEN = 176
SUPER_LEN = 532
SLOT_COUNT = 3

#: Every real profile starts with this. An unwritten slot is all ``0xff``; a deleted one is zeroed,
#: which is what the vendor app writes to delete a profile.
MARKER = b"\x11\x09\x20\x20"

#: Output button codes: one bit each, written as u16-LE into a map or paddle field. ``NONE``
#: unmaps the input entirely.
CODE = {
    "NONE": 0x0000,
    "MENU": 0x0001, "L3": 0x0002, "R3": 0x0004, "VIEW": 0x0008,
    "X": 0x0010, "Y": 0x0020, "DPAD_RIGHT": 0x0040, "DPAD_LEFT": 0x0080,
    "DPAD_DOWN": 0x0100, "DPAD_UP": 0x0200, "LB": 0x0400, "RB": 0x0800,
    "B": 0x1000, "A": 0x2000, "LT": 0x4000, "RT": 0x8000,
}
CODE_TO_NAME = {value: name for name, value in CODE.items()}

#: Physical input -> index into the map array. Note 13 is absent: the array has a gap there, which
#: the capture-diff work found empty and which nothing writes.
INPUT_IDX = {
    "A": 0, "B": 1, "X": 2, "Y": 3, "LB": 4, "RB": 5, "LT": 6, "RT": 7,
    "L3": 8, "R3": 9, "VIEW": 10, "MENU": 11, "STAR": 12,
    "DPAD_UP": 14, "DPAD_DOWN": 15, "DPAD_LEFT": 16, "DPAD_RIGHT": 17,
}

#: Physical inputs a user can remap, in the order they should be offered.
REMAPPABLE = tuple(INPUT_IDX)

#: The two back paddles, which use the same output codes as the map array.
PADDLES = ("PADDLE_L", "PADDLE_R")

# ---------------------------------------------------------------- per-slot offsets

REL_MAP = 44
"""Base of the map array. Entries are u16-LE at ``REL_MAP + index * 4``, the upper two bytes of
each stride being padding."""

MAP_STRIDE = 4

#: See the module docstring: 116/120 is confirmed by the captured record, and 124 is a marker.
REL_PADDLE = {"PADDLE_R": 116, "PADDLE_L": 120}

REL_VIB = {"L": 129, "R": 133, "LT": 137, "RT": 141}
REL_DZ = {"L": 149, "R": 151}
REL_TRIG = {"L": 157, "R": 159}
REL_FLAG1 = 164
REL_FLAG2 = 165

#: Toggle -> (flag byte, bit).
TOGGLES = {
    "INVERT_LX": (REL_FLAG1, 0x01),
    "INVERT_LY": (REL_FLAG1, 0x02),
    "INVERT_RX": (REL_FLAG1, 0x04),
    "INVERT_RY": (REL_FLAG1, 0x08),
    "SWAP_STICKS": (REL_FLAG1, 0x10),
    "SWAP_TRIGGERS": (REL_FLAG1, 0x80),
    "SWAP_DPAD_LS": (REL_FLAG2, 0x01),
    "NO_DEADZONE": (REL_FLAG2, 0x10),
    "FOURWAY_DPAD": (REL_FLAG2, 0x20),
    "NO_IMPULSE": (REL_FLAG2, 0x40),
    "NO_RUMBLE": (REL_FLAG2, 0x80),
}

#: Bit 0x08 of FLAG2 is set by the vendor app whenever rumble or impulse is moved off its default.
#: Not a user-facing setting; carried because the app sets it and a record without it may not be
#: honoured. See :meth:`~.record.SlotConfig.set_toggle`.
VIBRATION_COMPANION = 0x08

#: Toggles that are mutually exclusive in the vendor app: enabling one clears the other. Kept
#: because the hardware may not do anything sensible with both set at once.
EXCLUSIVE = (("SWAP_STICKS", "SWAP_DPAD_LS"),)

#: Raw ranges for the value bytes, as (minimum, maximum). The UI works in 0-100.
VALUE_RANGE = {"VIB": (0, 160), "DZ": (9, 128), "TRIG": (19, 255)}


def slot_base(slot: int) -> int:
    """Byte offset of a slot within the 532-byte super-config."""
    if not 0 <= slot < SLOT_COUNT:
        raise ValueError(f"slot out of range: {slot}")
    return 4 + slot * SLOT_LEN


def map_offset(input_name: str) -> int:
    return REL_MAP + INPUT_IDX[input_name] * MAP_STRIDE


def pct_to_raw(pct: float, kind: str) -> int:
    low, high = VALUE_RANGE[kind]
    return max(low, min(high, round(low + pct / 100.0 * (high - low))))


def raw_to_pct(raw: int, kind: str) -> int:
    low, high = VALUE_RANGE[kind]
    return max(0, min(100, round((raw - low) * 100.0 / (high - low))))


__all__ = [
    "CODE", "CODE_TO_NAME", "EXCLUSIVE", "INPUT_IDX", "MAP_STRIDE", "MARKER", "PADDLES",
    "REL_DZ", "REL_FLAG1", "REL_FLAG2", "REL_MAP", "REL_PADDLE", "REL_TRIG", "REL_VIB",
    "REMAPPABLE", "SLOT_COUNT", "SLOT_LEN", "SUPER_LEN", "TOGGLES", "VALUE_RANGE",
    "VIBRATION_COMPANION", "map_offset", "pct_to_raw", "raw_to_pct", "slot_base",
]
