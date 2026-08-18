"""The 8BitDo config record: field map, editing, and the rolling checksum.

Transport-independent, so all of this runs with no controller attached. Two things here are real
ground truth captured from hardware rather than fakes, and they are the tests that matter:

* :data:`CAPTURED_SAVES` -- four consecutive saves logged from the vendor Android app. Each save's
  stored checksum must be exactly one chain step from the previous one. If the CRC reconstruction
  broke in the port, this fails.
* :data:`CAPTURED_PROFILE` -- a 176-byte slot the source project documented as "paddles mapped to
  LT/RT". Decoding it must produce exactly that, which pins the paddle offsets.

The second one settled a real disagreement. The source had ``PADDLE_R`` at offset 116 in one file
and 124 in another; offset 124 turns out to be the second copy of the ``11 09 20 20`` record
marker, so writing a paddle there would have corrupted the record.
"""

from __future__ import annotations

import struct

import pytest

from hardware_ui.modules.eightbitdo_controllers.protocol import fieldmap as fm
from hardware_ui.modules.eightbitdo_controllers.protocol import record as rec

CAPTURED_SAVES = (
    # save 0, stored checksum 0x3b6b
    bytes.fromhex(
        "6b3b01ff11092020ffffffff000000000000000000000000000000000000000000000000"
        "000000000000000011092020002000000010000010000000200000000004000000080000"
        "004000000080000002000000040000000800000001000000000000080000020000020000"
        "0001000080000000400000000000000000000000110920200032c8460032c8460010c846"
        "0010c84611092020008000801109202000ff00ff1109202000000000ffffffff"
    ),
    # save 1, stored checksum 0x43a0
    bytes.fromhex(
        "a04301ff11092020ffffffff000000000000000000000000000000000000000000000000"
        "000000000000000011092020002000000010000010000000200000000004000000080000"
        "004000000080000002000000040000000800000001000000000000080000020000020000"
        "0001000080000000400000000000000000000000110920200032c8460032c8460010c846"
        "0010c84611092020008000801109202000ff00ff1109202000000000ffffffff"
    ),
    # save 2, stored checksum 0x8781
    bytes.fromhex(
        "818701ff11092020ffffffff000000000000000000000000000000000000000000000000"
        "000000000000000011092020002000000010000010000000200000000004000000080000"
        "004000000080000002000000040000000800000001000000000000080000020000020000"
        "0001000080000000400000000000000000000000110920200032c8460032c8460010c846"
        "0010c84611092020008000801109202000ff00ff1109202000000000ffffffff"
    ),
    # save 3, stored checksum 0xa9fd
    bytes.fromhex(
        "fda901ff11092020ffffffff000000000000000000000000000000000000000000000000"
        "000000000000000011092020002000000010000010000000200000000004000000080000"
        "004000000080000002000000040000000800000001000000000000080000020000020000"
        "0001000080000000400000000000000000000000110920200032c8460032c8460010c846"
        "0010c84611092020008000801109202000ff00ff1109202000000000ffffffff"
    ),
)

#: One profile with both paddles mapped, from the source project's write session.
CAPTURED_PROFILE = bytes.fromhex(
    "11092020ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "11092020000000000020000000100000100000002000000000040000000800000040000000"
    "80000002000000040000000800000001000000000000080000020000020000000100008000"
    "0000400000000080000000400000110920200032c846003 2c8460010c8460010c846"
    "1109202000800080110920200 0ff00ff1109202000000000ffffffffffff55aa".replace(" ", "")
)


def test_the_embedded_capture_data_is_the_right_shape():
    """Guards the hex literals above against a mangled edit."""
    assert len(CAPTURED_PROFILE) == fm.SLOT_LEN
    assert CAPTURED_PROFILE[:4] == fm.MARKER
    assert CAPTURED_PROFILE[-2:] == bytes.fromhex("55aa")
    assert all(len(s) == 176 for s in CAPTURED_SAVES)


#: A slot the vendor app **deleted**, dumped from the controller over BLE. Deleting zeroes the
#: marker and nothing else, so the profile's old bytes are still sitting there -- this record still
#: decodes as paddles LT/RT if you ignore the marker. That is exactly why `written` tests the
#: marker instead of asking whether the record looks like it holds data.
CAPTURED_DELETED = bytes.fromhex(
        "00000000ffffffff00000000000000000000000000000000000000000000000000000000"
        "000000000000000000200000001000001000000020000000000400000008000000400000"
        "008000000200000004000000080000000100000000000008000002000002000000010000"
        "80000000400000000080000000400000000000000032c8460032c8460010c8460010c846"
        "00000000008000800000000000ff00ff0000000000000000ffffffffffff55aa"
)

#: A slot that has never been written, from the same dump: all 0xff, no footer.
CAPTURED_UNWRITTEN = bytes.fromhex(
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)


# --------------------------------------------------------------------------- rolling checksum


def test_the_checksum_chain_reproduces_four_captured_saves():
    """The one assertion here that is not synthetic.

    A real controller was saved to four times by the vendor app, and each save's stored checksum
    is one step from its predecessor: 0x3b6b -> 0x43a0 -> 0x8781 -> 0xa9fd.
    """
    stored = [struct.unpack_from("<H", s, 0)[0] for s in CAPTURED_SAVES]
    assert stored == [0x3B6B, 0x43A0, 0x8781, 0xA9FD], "the captured data itself changed"
    for i in range(1, len(CAPTURED_SAVES)):
        assert rec.roll_crc(stored[i - 1], CAPTURED_SAVES[i]) == stored[i]


def test_the_checksum_covers_only_the_first_176_bytes():
    """Which is why mappings in slots 1 and 2 sit outside it, and why a chain step needs no more
    than the record's head."""
    base = bytearray(CAPTURED_SAVES[1]) + bytearray(fm.SUPER_LEN - 176)
    first = rec.roll_crc(0x3B6B, bytes(base))
    base[200] ^= 0xFF                        # somewhere in slot 1, outside [2:176]
    assert rec.roll_crc(0x3B6B, bytes(base)) == first


def test_a_different_previous_value_gives_a_different_checksum():
    """It is a chain, not a checksum over the record: the previous value is a real input."""
    record = CAPTURED_SAVES[1]
    assert rec.roll_crc(0x3B6B, record) != rec.roll_crc(0x3B6C, record)


def test_the_polynomial_matches_a_known_vector():
    """CRC-16/MCRF4XX of "123456789" is 0x6F91."""
    assert rec.mcrf4xx(b"123456789") == 0x6F91


# --------------------------------------------------------------------------- the field map


def test_the_paddle_offsets_are_the_ones_the_capture_proves():
    """116 and 120, not 124. Offset 124 is a record marker."""
    assert fm.REL_PADDLE == {"PADDLE_R": 116, "PADDLE_L": 120}
    assert CAPTURED_PROFILE[124:128] == fm.MARKER


def test_the_captured_profile_decodes_as_its_description_says():
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    assert slot.written
    assert slot.get_paddle("PADDLE_L") == "LT"
    assert slot.get_paddle("PADDLE_R") == "RT"


def test_every_other_input_in_that_profile_is_left_at_identity():
    """A remap that is not a remap: each input maps to its own output code. Catches an off-by-one
    in the map array's stride, which would show up as neighbouring inputs swapping."""
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    for name in fm.REMAPPABLE:
        if name == "STAR":
            continue                          # idx 12 carries 0x0000 in this profile
        assert slot.get_map(name) == name, f"{name} is not at identity"


def test_the_output_code_table_is_a_bijection():
    assert len(fm.CODE_TO_NAME) == len(fm.CODE)


def test_every_remappable_input_has_a_distinct_map_offset():
    offsets = {name: fm.map_offset(name) for name in fm.REMAPPABLE}
    assert len(set(offsets.values())) == len(offsets)
    # And none of them collides with a paddle field or a value byte.
    others = set(fm.REL_PADDLE.values()) | set(fm.REL_VIB.values()) | set(fm.REL_DZ.values())
    assert not set(offsets.values()) & others


def test_slot_bases_tile_the_record_without_overlapping():
    bases = [fm.slot_base(i) for i in range(fm.SLOT_COUNT)]
    assert bases == [4, 180, 356]
    assert bases[-1] + fm.SLOT_LEN == fm.SUPER_LEN


def test_an_out_of_range_slot_is_refused():
    with pytest.raises(ValueError):
        fm.slot_base(fm.SLOT_COUNT)


@pytest.mark.parametrize("kind", ["VIB", "DZ", "TRIG"])
def test_percentages_round_trip_through_the_raw_ranges(kind):
    low, high = fm.VALUE_RANGE[kind]
    assert fm.pct_to_raw(0, kind) == low
    assert fm.pct_to_raw(100, kind) == high
    for pct in range(0, 101, 5):
        assert abs(fm.raw_to_pct(fm.pct_to_raw(pct, kind), kind) - pct) <= 1


def test_a_percentage_outside_the_range_is_clamped_not_wrapped():
    assert fm.pct_to_raw(-50, "VIB") == 0
    assert fm.pct_to_raw(500, "VIB") == 160


# --------------------------------------------------------------------------- editing a slot


def test_an_unwritten_slot_is_not_mistaken_for_a_profile():
    """Factory-fresh reads as 0xff, deleted reads as zeros, and only a real profile carries the
    marker. Getting this backwards would offer "reset" where "create" belongs, silently
    discarding whatever the user had."""
    assert not rec.SlotConfig(b"\xff" * fm.SLOT_LEN).written
    assert not rec.empty_slot().written
    assert rec.SlotConfig(CAPTURED_PROFILE).written


def test_editing_leaves_every_undecoded_byte_alone():
    """The record holds bytes nobody has decoded. Parsing into fields and re-serialising would
    zero them, so a slot is edited in place instead."""
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    slot.set_map("A", "B")
    changed = [i for i in range(fm.SLOT_LEN) if slot.raw[i] != CAPTURED_PROFILE[i]]
    # Within A's own 2-byte field and nowhere else. Not exactly [offset]: A is 0x2000 and B is
    # 0x1000, so only the high byte of the little-endian pair actually differs.
    field = range(fm.map_offset("A"), fm.map_offset("A") + 2)
    assert changed, "the write did not land at all"
    assert all(i in field for i in changed), f"bytes outside A's field changed: {changed}"


def test_a_wrong_sized_slot_is_refused_rather_than_padded():
    with pytest.raises(ValueError):
        rec.SlotConfig(b"\x00" * 100)


def test_the_exclusive_toggles_clear_each_other():
    """The vendor app clears one when the other is enabled, and the hardware may not do anything
    sensible with both set."""
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    slot.set_toggle("SWAP_STICKS", True)
    slot.set_toggle("SWAP_DPAD_LS", True)
    assert slot.get_toggle("SWAP_DPAD_LS")
    assert not slot.get_toggle("SWAP_STICKS")


def test_the_vibration_companion_bit_follows_rumble_and_impulse():
    """FLAG2 bit 0x08 is bookkeeping the app does, not a user setting. Carried because a record
    that disagrees with the app may not be honoured."""
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    assert not slot.raw[fm.REL_FLAG2] & fm.VIBRATION_COMPANION
    slot.set_toggle("NO_RUMBLE", True)
    assert slot.raw[fm.REL_FLAG2] & fm.VIBRATION_COMPANION
    slot.set_toggle("NO_RUMBLE", False)
    assert not slot.raw[fm.REL_FLAG2] & fm.VIBRATION_COMPANION


def test_unmapping_an_input_is_a_real_value_not_a_deletion():
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    slot.set_map("STAR", "NONE")
    assert slot.get_map("STAR") == "NONE"
    assert struct.unpack_from("<H", slot.raw, fm.map_offset("STAR"))[0] == 0


def test_a_clone_does_not_share_storage():
    slot = rec.SlotConfig(CAPTURED_PROFILE)
    twin = slot.clone()
    twin.set_map("A", "B")
    assert slot.get_map("A") == "A"
    assert twin != slot


# --------------------------------------------------------------------------- the super-config


def full_super() -> bytes:
    return CAPTURED_SAVES[1] + bytes(fm.SUPER_LEN - 176)


def test_a_super_splits_into_a_header_and_three_slots():
    sup = rec.SuperConfig(full_super())
    assert len(sup.slots) == fm.SLOT_COUNT
    assert sup.crc == 0x43A0


def test_reassembly_is_byte_exact_when_no_checksum_is_rolled():
    raw = full_super()
    assert rec.SuperConfig(raw).to_bytes() == raw


def test_reassembly_rolls_the_checksum_when_a_previous_value_is_given():
    out = rec.SuperConfig(full_super()).to_bytes(previous_crc=0x3B6B)
    assert struct.unpack_from("<H", out, 0)[0] == 0x43A0
    assert out[2:] == full_super()[2:]


def test_the_active_slot_is_read_but_never_written():
    """Which profile is live is chosen with the controller's own button. Moving it from software
    would change the device out from under whoever is holding it."""
    sup = rec.SuperConfig(full_super())
    assert sup.active_slot == 1
    assert not hasattr(sup, "set_active_slot")


def test_an_out_of_range_active_slot_byte_reads_as_unknown():
    raw = bytearray(full_super())
    raw[2] = 0x7F
    assert rec.SuperConfig(bytes(raw)).active_slot is None


def test_slots_can_be_rebuilt_from_individual_reads():
    """All BLE can do: the 532-byte record is not readable over it, only the three slots are."""
    sup = rec.SuperConfig(full_super())
    rebuilt = rec.SuperConfig.from_slots(sup.slots, bytes(sup.header))
    assert rebuilt.to_bytes() == sup.to_bytes()


def test_rebuilding_refuses_a_wrong_header_or_slot_count():
    sup = rec.SuperConfig(full_super())
    with pytest.raises(ValueError):
        rec.SuperConfig.from_slots(sup.slots, b"\x00\x00")
    with pytest.raises(ValueError):
        rec.SuperConfig.from_slots(sup.slots[:2], bytes(sup.header))


def test_a_wrong_sized_super_is_refused():
    with pytest.raises(ValueError):
        rec.SuperConfig(b"\x00" * 500)


# --------------------------------------------------------------------------- real slot dumps
#
# Read off the controller over BLE with the source project's dump tool, so these are what the
# read path actually returns rather than what the write path sends.


def test_a_deleted_slot_is_not_read_as_a_profile():
    """The trap this guards: deleting zeroes only the four marker bytes, so a deleted slot still
    contains the old profile's mappings. Anything that decided "written" by looking for data would
    resurrect a profile the user deleted."""
    slot = rec.SlotConfig(CAPTURED_DELETED)
    assert not slot.written
    assert CAPTURED_DELETED[0:4] == b"\x00\x00\x00\x00"
    # The residue really is still there, which is the whole point.
    assert slot.get_paddle("PADDLE_L") == "LT"
    assert slot.get_paddle("PADDLE_R") == "RT"


def test_an_unwritten_slot_is_not_read_as_a_profile_either():
    slot = rec.SlotConfig(CAPTURED_UNWRITTEN)
    assert not slot.written
    assert set(CAPTURED_UNWRITTEN) == {0xFF}


def test_the_three_slot_states_are_distinguishable():
    """Unwritten, deleted and written are three different things, and the UI offers a different
    action for each."""
    states = [rec.SlotConfig(r).written
              for r in (CAPTURED_UNWRITTEN, CAPTURED_DELETED, CAPTURED_PROFILE)]
    assert states == [False, False, True]
    assert CAPTURED_UNWRITTEN[:4] != CAPTURED_DELETED[:4]


def test_a_freshly_deleted_slot_matches_what_the_app_writes():
    """The app deletes by zeroing, not by filling with 0xff, and matching it is the safer of the
    two. Only the marker is zeroed on the device; ours zeroes the whole slot, which is a superset
    and still reads as deleted."""
    assert rec.empty_slot().raw[0:4] == bytes(4)
    assert not rec.empty_slot().written
