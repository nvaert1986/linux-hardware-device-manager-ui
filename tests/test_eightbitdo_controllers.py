"""The 8BitDo module above the record layer: framing, transports, capabilities, device.

No controller is attached here, so the transports are exercised through fakes. What that can still
prove is the part most likely to be got wrong in a port: that the framing matches the shapes the
source project observed on the wire, that a write refuses to proceed when it cannot compute a
correct checksum, and that switching which profile is being *edited* never touches the device.

The record layer itself, including the checksum chain verified against real captured saves, is in
``test_eightbitdo_record``.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from hardware_ui.core.capability import Advisory, Kind
from hardware_ui.core.device import Category, DeviceInfo, Transport
from hardware_ui.modules.eightbitdo_controllers import capabilities as C
from hardware_ui.modules.eightbitdo_controllers import store, transport
from hardware_ui.modules.eightbitdo_controllers.device import EightBitDoController
from hardware_ui.modules.eightbitdo_controllers.protocol import fieldmap as fm
from hardware_ui.modules.eightbitdo_controllers.protocol import (
    gip,
    message,
    record,
)
from tests.test_eightbitdo_record import CAPTURED_PROFILE

# --------------------------------------------------------------------------- GIP framing


def test_a_header_is_always_an_even_number_of_bytes():
    """Not cosmetic: an odd-length header is rejected. The protocol pads by setting the
    continuation bit on the last varint byte and appending a zero, which decodes the same."""
    for length in (0, 1, 60, 127, 128, 532):
        assert len(gip.encode_header(gip.CMD_CONFIG_OUT, 0, 1, length)) % 2 == 0


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 532, 16384])
def test_varints_round_trip(value):
    assert gip.decode_varint(gip.encode_varint(value))[0] == value


def test_a_truncated_varint_is_an_error_not_a_silent_zero():
    with pytest.raises(ValueError):
        gip.decode_varint(b"\x80")


def test_the_client_id_rides_in_the_low_nibble():
    """This is a multi-client device: client 0 is the gamepad and owns config, client 1 is the
    headset. A config request sent to the wrong client gets silence."""
    frame = gip.build(gip.CMD_CONFIG_OUT, b"", client=gip.CLIENT_GAMEPAD, internal=True)
    assert frame[1] & 0x0F == 0
    assert frame[1] & gip.OPT_INTERNAL


def test_a_chunk_start_frame_carries_the_total_not_a_position():
    """The rule that cost the source project three attempts. On CHUNK_START the header's chunk
    offset is the total size and the data belongs at position 0; afterwards it is a real offset."""
    reassembler = gip.Reassembler()
    assert reassembler.feed(gip.OPT_CHUNK | gip.OPT_CHUNK_START, 8, b"abcd") is None
    assert reassembler.feed(gip.OPT_CHUNK, 4, b"efgh") == b"abcdefgh"


def test_a_zero_length_chunk_terminates_the_message():
    reassembler = gip.Reassembler()
    reassembler.feed(gip.OPT_CHUNK | gip.OPT_CHUNK_START, 16, b"abcd")
    assert reassembler.feed(gip.OPT_CHUNK, 4, b"") == b"abcd" + bytes(12)


def test_an_acknowledgement_reports_received_and_remaining():
    """An ack with zeros in those fields stalls the transfer silently, which is exactly what the
    source project hit."""
    ack = gip.build_ack(gip.CMD_IDENTIFY, received=40, total=106, sequence=7)
    _, _, _, length, _, start = gip.decode_header(ack)
    body = ack[start:start + length]
    assert int.from_bytes(body[3:5], "little") == 40
    assert int.from_bytes(body[7:9], "little") == 106 - 40


def test_sequence_numbers_wrap_without_ever_using_zero():
    sequence = gip.Sequence()
    seen = {sequence.take() for _ in range(600)}
    assert 0 not in seen
    assert seen == set(range(1, 256))


# --------------------------------------------------------------------------- inner messages


def test_a_request_header_is_one_byte_longer_than_a_response_header():
    packet = message.request(message.CMD_SLOT, field=2, size=45, total=176, offset=90,
                             data=b"\x01" * 45)
    assert packet[0] == message.REPORT_ID
    assert len(packet) == message.REQUEST_HEADER_LEN + 45
    # One byte longer than a response header, which is the trap this pins.
    assert message.REQUEST_HEADER_LEN == message.RESPONSE_HEADER_LEN + 1


def test_a_response_is_parsed_with_its_status_byte():
    """The response is not the request plus a status byte: the status displaces the field, so the
    two layouts differ at the same offsets."""
    raw = bytes([0x04, 0x00]) + (0x0D).to_bytes(2, "little") + \
        (45).to_bytes(4, "little") + (176).to_bytes(4, "little") + \
        (90).to_bytes(4, "little") + b"\xab" * 45
    status, command, size, total, offset, data = message.parse_response(raw)
    assert (status, command, size, total, offset) == (0, 0x0D, 45, 176, 90)
    assert data == b"\xab" * 45


def test_a_foreign_packet_parses_as_none_rather_than_raising():
    """A notification stream carries other traffic, and distinguishing "not mine" from "malformed"
    by catching exceptions swallows real errors."""
    assert message.parse_response(b"\x99\x00\x00") is None
    assert message.parse_response(b"") is None


def test_the_control_packet_carries_its_constant_in_the_offset_field():
    """Where the field boundary falls decides whether a write survives being unplugged.

    ``... 34 34 00 00 aa 00 00 00`` reads naturally as one payload whose ``aa`` means "save". It is
    not: the request header is seventeen bytes, so ``34 34 00 00`` is the *offset* and the payload
    is the four bytes after it. Splitting it the other way writes a record the controller reports
    back happily and loses at the next plug-in.
    """
    packet = message.request(message.CMD_CONTROL, size=4, total=4,
                             offset=message.CONTROL_OFFSET, data=message.CONTROL_SAVE)
    assert packet.hex(" ") == "04 0b 00 00 00 04 00 00 00 04 00 00 00 34 34 00 00 aa 00 00 00"
    assert message.CONTROL_SAVE != message.CONTROL_READ


def test_a_save_is_a_session_and_not_just_the_record():
    """The bug that made every write look like it worked and survive nothing.

    Sending the chunks alone is not a save. The controller accepts all 532 bytes and reads them
    back correctly for as long as it stays powered. What commits is the finalize at the end, and
    the vendor app's captured save sends three more packets before the record as well. Pinned in
    order, because it was reconstructed once already and the reconstruction was wrong.
    """
    commands = [struct.unpack_from("<H", p, 1)[0] for p in message.save_prologue()]
    assert commands == [message.CMD_CONTROL, message.CMD_REPORT_ENABLE,
                        message.CMD_CALIBRATION, message.CMD_READ_SUPER]
    assert struct.unpack_from("<H", message.finalize(), 1)[0] == message.CMD_FINALIZE
    assert struct.unpack_from("<H", message.finalize(), 3)[0] == message.FINALIZE_FIELD


def test_both_transports_send_the_same_save_sequence():
    """The capture is a Bluetooth session and the inner bytes are transport-independent, so there
    is one save sequence rather than two that can drift apart."""
    from hardware_ui.modules.eightbitdo_controllers.transport import ble
    assert message.save_prologue() == ble.WRITE_PREFIX
    assert message.finalize() == ble.FINALIZE


def test_chunking_covers_the_record_exactly():
    pieces = message.chunks(fm.SUPER_LEN, 41)
    assert sum(length for _, length in pieces) == fm.SUPER_LEN
    assert pieces[0][0] == 0
    assert pieces[-1][0] + pieces[-1][1] == fm.SUPER_LEN


# --------------------------------------------------------------------------- the checksum store


def test_a_checksum_survives_a_round_trip():
    store.remember("usb:B1-1", 0x43A0)
    assert store.remembered("usb:B1-1") == 0x43A0


def test_an_unknown_controller_has_no_remembered_checksum():
    assert store.remembered("usb:never-seen") is None


def test_forgetting_removes_only_that_controller():
    store.remember("usb:one", 0x1111)
    store.remember("usb:two", 0x2222)
    store.forget("usb:one")
    assert store.remembered("usb:one") is None
    assert store.remembered("usb:two") == 0x2222


def test_a_store_from_a_future_version_is_ignored_rather_than_misread():
    store.remember("usb:B1-1", 0x43A0)
    store.path().write_text('{"version": 999, "controllers": {"usb:B1-1": 1}}')
    assert store.remembered("usb:B1-1") is None


def test_a_corrupt_store_is_not_fatal():
    store.remember("usb:seed", 1)            # so the directory exists
    store.path().write_text("{ not json")
    assert store.remembered("usb:anything") is None


# --------------------------------------------------------------------------- the transport layer


def super_bytes(*, written: bool = True) -> bytes:
    profile = CAPTURED_PROFILE if written else bytes(fm.SLOT_LEN)
    return bytes([0xA0, 0x43, 0x00, 0x00]) + profile * fm.SLOT_COUNT


class FakeUsb:
    """Stands in for the GIP backend. Records what it was asked to write."""

    def __init__(self, record: bytes | None = None) -> None:
        self.record = record if record is not None else super_bytes()
        self.written: list[bytes] = []

    def read_super(self, product_id=None, serial=""):
        return self.record

    def write_super(self, record, product_id=None, serial=""):
        self.written.append(record)


class FakeBle:
    def __init__(self, slots: list[bytes] | None = None) -> None:
        self.slots = slots or [CAPTURED_PROFILE] * fm.SLOT_COUNT
        self.written: list[bytes] = []

    def read_slots(self, address, adapter=""):
        return self.slots

    def write_super(self, record, address, adapter=""):
        self.written.append(record)


@pytest.fixture
def fake_transports(monkeypatch):
    usb, ble = FakeUsb(), FakeBle()
    monkeypatch.setattr(transport.usb, "read_super", usb.read_super)
    monkeypatch.setattr(transport.usb, "write_super", usb.write_super)
    monkeypatch.setattr(transport.ble, "read_slots", ble.read_slots)
    monkeypatch.setattr(transport.ble, "write_super", ble.write_super)
    return usb, ble


def test_a_usb_read_yields_the_checksum_from_the_record(fake_transports):
    config, checksum = transport.read(transport.Link("usb", "B1-1", 0x2002))
    assert checksum == 0x43A0
    assert config.crc == 0x43A0


def test_a_usb_read_remembers_the_checksum_for_the_bluetooth_path(fake_transports):
    """The point of preferring USB: plug in once and the BLE path can write from then on."""
    transport.read(transport.Link("usb", "B1-1", 0x2002))
    assert store.remembered("ble:B1-1") == 0x43A0


def test_a_bluetooth_read_has_no_checksum_until_one_is_remembered(fake_transports):
    """The header is simply not readable over BLE, so there is nothing to chain from."""
    _, checksum = transport.read(transport.Link("ble", "E4:17:D8:50:43:42"))
    assert checksum is None


def test_a_bluetooth_read_uses_a_remembered_checksum_when_there_is_one(fake_transports):
    store.remember("ble:E4:17:D8:50:43:42", 0x8781)
    config, checksum = transport.read(transport.Link("ble", "E4:17:D8:50:43:42"))
    assert checksum == 0x8781
    assert config.crc == 0x8781


def test_a_bluetooth_write_needs_no_prior_usb_read(fake_transports):
    """The reverse of what this module used to assert, and the reversal is the point.

    Writing over Bluetooth was refused until the controller had been read once over USB, because
    the checksum chain was believed to need the value currently on the device and the header is not
    readable over Bluetooth. The chain starts from a fixed seed instead, so the restriction was
    inventing a limitation the working implementation does not have.
    """
    _, ble = fake_transports
    link = transport.Link("ble", "E4:17:D8:50:43:42")
    config, _ = transport.read(link)
    transport.write(link, config, None)
    assert ble.written, "the write never reached the controller"


def test_every_write_seeds_the_checksum_the_same_way_whatever_the_device_holds(fake_transports):
    """The bug that made a save survive until the cable came out.

    Chaining from the controller's own header is the obvious reading of the protocol and it is
    wrong: the controller takes the record, reports the new mapping while it stays powered, and
    comes back with the old one. The working implementation replays two header bytes and one seed
    on every save, so the same slots always produce the same record no matter what was read.
    """
    usb, _ = fake_transports
    link = transport.Link("usb", "B1-1", 0x2002)
    config, checksum = transport.read(link)

    first = transport.write(link, config, checksum)
    second = transport.write(link, config, first)
    assert first == second, "the record must not depend on what was last written"

    written = usb.written[-1]
    assert written[2:4] == record.HEADER_TAIL
    assert int.from_bytes(written[:2], "little") == record.roll_crc(record.SEED_CRC, written)


def test_a_write_rolls_the_checksum_and_remembers_the_result(fake_transports):
    usb, _ = fake_transports
    link = transport.Link("usb", "B1-1", 0x2002)
    config, checksum = transport.read(link)
    new = transport.write(link, config, checksum)
    assert usb.written, "nothing reached the transport"
    assert int.from_bytes(usb.written[0][:2], "little") == new
    assert new != checksum
    assert store.remembered(link.key) == new


def test_a_default_profile_is_cloned_from_one_the_controller_already_has(fake_transports):
    """Better than a synthetic default: the identity mapping is whatever this controller's own
    firmware wrote, so nothing is invented that the device has not seen."""
    config, _ = transport.read(transport.Link("usb", "B1-1", 0x2002))
    fresh = transport.default_slot(config)
    assert fresh.written
    assert fresh.get_paddle("PADDLE_L") == "LT"


# --------------------------------------------------------------------------- capabilities


def test_every_remappable_input_and_paddle_gets_a_row():
    keys = {c.key for c in C.build(profile_written=True)}
    for name in (*fm.REMAPPABLE, *fm.PADDLES):
        assert C.map_key(name) in keys


def test_every_remap_row_is_sectioned_by_the_drawing_that_shows_it():
    """The page and the artwork have one source. Bumpers and triggers used to sit among the face
    buttons, which stopped matching anything once they got their own view."""
    from hardware_ui.modules.eightbitdo_controllers import anchors

    rows = {c.key: c for c in C.build(profile_written=True) if c.key.startswith("map.")}
    assert rows, "no remap rows at all"
    for key, row in rows.items():
        name = C.map_name(key)
        view = anchors.view_of(name)
        assert view is not None, f"{name} is on no drawing"
        assert row.section == anchors.VIEW_LABELS[view], (
            f"{name} is sectioned {row.section!r} but drawn on the {view} view"
        )


def test_the_remap_sections_are_the_three_views_in_order():
    from hardware_ui.modules.eightbitdo_controllers import anchors

    seen = []
    for c in C.build(profile_written=True):
        if c.key.startswith("map.") and (not seen or seen[-1] != c.section):
            seen.append(c.section)
    assert seen == [anchors.VIEW_LABELS[v] for v in anchors.VIEWS], (
        "each view must contribute one contiguous run; the shell does not reorder rows"
    )


def test_key_helpers_round_trip():
    for name in fm.REMAPPABLE:
        assert C.map_name(C.map_key(name)) == name
    for name in fm.TOGGLES:
        assert C.toggle_name(C.toggle_key(name)) == name
    assert C.map_name("toggle.no_rumble") is None
    assert C.toggle_name("map.a") is None
    assert C.value_spec("value.dz_l") == ("DZ", "L")
    assert C.value_spec("map.a") is None


def test_an_empty_profile_offers_nothing_to_edit():
    """Greyed sliders over a profile that does not exist read as breakage. The selector and the
    create action stay live so there is a way out."""
    rows = {c.key: c for c in C.build(profile_written=False)}
    assert not rows[C.map_key("A")].writable
    assert not rows[C.toggle_key("NO_RUMBLE")].writable
    assert rows[C.KEY_PROFILE].writable
    assert rows[C.KEY_RESET].writable


def test_deleting_is_offered_only_for_the_two_secondary_profiles():
    row = {c.key: c for c in C.build(profile_written=True)}[C.KEY_DELETE]
    assert row.requires == C.KEY_PROFILE
    assert row.requires_value == (1, 2)


def test_both_destructive_actions_ask_first():
    rows = {c.key: c for c in C.build(profile_written=True)}
    assert rows[C.KEY_RESET].confirm
    assert rows[C.KEY_DELETE].confirm


def test_the_active_profile_is_a_readout_not_a_control():
    """Which profile is live is chosen with the controller's own button. Writing it from here
    would change the device under whoever is holding it."""
    row = {c.key: c for c in C.build(profile_written=True)}[C.KEY_ACTIVE]
    assert row.kind is Kind.READOUT


def test_sections_stay_contiguous_within_each_group():
    """The view groups adjacent rows and does not reorder them, so a split section renders as two
    headings with the same name."""
    from hardware_ui.core.capability import CapabilitySet

    for _, rows in CapabilitySet(C.build(profile_written=True)).groups().items():
        sections = [r.section for r in rows]
        runs = [s for i, s in enumerate(sections) if i == 0 or s != sections[i - 1]]
        assert len(runs) == len(set(runs))


# --------------------------------------------------------------------------- the device


def usb_info(**kwargs) -> DeviceInfo:
    base = dict(uid="usb:B1-1", name="8BitDo Ultimate Wired Controller for Xbox",
                transport=Transport.USB, category=Category.INPUT,
                vendor_id=0x2DC8, product_id=0x2002, serial="B1-1")
    return DeviceInfo(**{**base, **kwargs})


def connected(fake_transports) -> EightBitDoController:
    device = EightBitDoController(usb_info())
    asyncio.run(device.connect())
    return device


def test_no_row_is_labelled_with_a_raw_enum_name():
    """"PADDLE_L" reached the screen, on the drawing, because the short-label table was built from
    the *output* names and a paddle is not an output -- nothing can be remapped to one -- so it
    fell through to its own enum name. Cheap to assert for every row and every label at once."""
    for cap in C.build(profile_written=True):
        for text in (cap.label, cap.short_label):
            assert not (text.isupper() and ("_" in text or len(text) > 3)), (
                f"{cap.key} shows the raw name {text!r}")


def test_sync_writes_the_held_record_without_changing_it(fake_transports):
    """The Sync button. Its whole job is the write, so it must reach the transport.

    Every other action changes a byte and saves as a side effect; this one changes nothing, which
    is exactly why it is worth having when a save has not taken.
    """
    usb, _ = fake_transports
    device = connected(fake_transports)
    before = len(usb.written)

    result = asyncio.run(device.set(C.KEY_SYNC, True))

    assert len(usb.written) == before + 1, "Sync did not write anything"
    assert "controller" in str(result).lower(), "the button must say what it did"
    assert usb.written[-1][2:4] == record.HEADER_TAIL


def test_connecting_reads_the_record_and_builds_the_page(fake_transports):
    device = connected(fake_transports)
    assert len(device.capabilities) == 47
    assert asyncio.run(device.get(C.map_key("PADDLE_L"))) == "LT"


def test_switching_the_edited_profile_writes_nothing(fake_transports):
    """It is a view control. Writing on a profile change would save an unedited record and burn a
    checksum step for nothing."""
    usb, _ = fake_transports
    device = connected(fake_transports)
    asyncio.run(device.set(C.KEY_PROFILE, 2))
    assert asyncio.run(device.get(C.KEY_PROFILE)) == 2
    assert usb.written == []


def test_edits_are_held_until_sync_and_then_go_as_one_record(fake_transports):
    """Editing writes nothing; Sync writes everything, once.

    Saving per change is the shape this module started with and it is wrong for a device whose
    configuration is one indivisible block: remapping four buttons became four full sessions, four
    kernel-driver detaches and four one-second gaps where the controller stops being a gamepad, to
    express a single intent. It also left a failed save with nothing to retry.
    """
    usb, _ = fake_transports
    device = connected(fake_transports)

    asyncio.run(device.set(C.map_key("A"), "B"))
    asyncio.run(device.set(C.map_key("X"), "Y"))
    assert usb.written == [], "an edit must not reach the controller on its own"
    assert asyncio.run(device.get(C.map_key("A"))) == "B", "but it must show in the held record"

    asyncio.run(device.set(C.KEY_SYNC, True))
    assert len(usb.written) == 1, "Sync writes once, whatever was changed"
    assert len(usb.written[0]) == fm.SUPER_LEN


def test_unsaved_edits_say_so_on_every_tab(fake_transports):
    """Nothing reaches the controller until Sync, so the page has to admit when it is ahead of it.

    On every tab, because the button is on every tab: a warning only where the user is not looking
    is the same as no warning.
    """
    device = connected(fake_transports)
    assert not any("Sync" in a.message for a in device.advisories().values())

    asyncio.run(device.set(C.map_key("A"), "B"))
    groups = device.capabilities.groups()
    warned = {
        group for group, members in groups.items()
        if any("Sync" in (device.advisories().get(c.key) or Advisory()).message for c in members)
    }
    assert warned == set(groups), f"no unsaved warning on {set(groups) - warned}"

    asyncio.run(device.set(C.KEY_SYNC, True))
    assert not any("Sync" in a.message for a in device.advisories().values())


def test_sync_holds_the_whole_page_while_it_runs(fake_transports):
    """Otherwise a dropdown can be changed after the record was built and before it lands.

    The controller stores its configuration as one block with one checksum, so a write is all or
    nothing; a control left live during it puts a value on screen that the controller was never
    given. ``exclusive`` is how a capability says the write owns the device.
    """
    device = connected(fake_transports)
    assert device.capabilities.by_key(C.KEY_SYNC).exclusive
    # Nothing else claims the device, or every edit would freeze the page.
    others = [c.key for c in device.capabilities if c.exclusive and c.key != C.KEY_SYNC]
    assert not others, f"unexpectedly exclusive: {others}"


def test_the_sync_button_is_on_every_tab(fake_transports):
    """Otherwise finishing the job means remembering which tab the button was on."""
    device = connected(fake_transports)
    groups = device.capabilities.groups()
    missing = [g for g, members in groups.items() if not any(c.key == C.KEY_SYNC for c in members)]
    assert not missing, f"no Sync button on {missing}"


def test_a_remap_to_a_button_the_controller_cannot_emit_is_refused(fake_transports):
    device = connected(fake_transports)
    with pytest.raises(RuntimeError):
        asyncio.run(device.set(C.map_key("A"), "TRIANGLE"))


def test_profile_one_cannot_be_deleted(fake_transports):
    """It is the controller's base profile; deleting it leaves nothing to fall back to."""
    device = connected(fake_transports)
    asyncio.run(device.set(C.KEY_PROFILE, 0))
    with pytest.raises(RuntimeError):
        asyncio.run(device.set(C.KEY_DELETE, True))


def test_deleting_a_secondary_profile_empties_it(fake_transports):
    device = connected(fake_transports)
    asyncio.run(device.set(C.KEY_PROFILE, 1))
    asyncio.run(device.set(C.KEY_DELETE, True))
    assert not device._require().slots[1].written


def test_the_page_always_says_that_saving_interrupts_the_gamepad(fake_transports):
    device = connected(fake_transports)
    assert "gamepad" in device.advisories()[C.KEY_PROFILE].message


def test_a_controller_with_no_known_checksum_says_so_instead_of_failing_later(fake_transports):
    device = EightBitDoController(DeviceInfo(
        uid="bt:E4:17:D8:50:43:42", name="82CE", transport=Transport.BLUETOOTH,
        category=Category.INPUT, address="E4:17:D8:50:43:42"))
    asyncio.run(device.connect())
    assert "USB" in device.advisories()[C.KEY_PROFILE].message


def test_a_disconnected_device_reports_it_rather_than_raising_attribute_errors():
    device = EightBitDoController(usb_info())
    assert device.connection_label().route == "Not connected"
    with pytest.raises(RuntimeError):
        device._require()


def test_the_link_follows_the_row_rather_than_being_sniffed(fake_transports):
    usb = EightBitDoController(usb_info())
    assert usb._link.kind == "usb"
    ble = EightBitDoController(DeviceInfo(
        uid="bt:AA:BB", name="82CE", transport=Transport.BLUETOOTH, address="AA:BB"))
    assert ble._link.kind == "ble"


# --------------------------------------------------------------------------- the manifest


def test_the_module_is_registered_and_loadable():
    from hardware_ui.core.modules import ModuleRegistry

    manifest = ModuleRegistry.discover().get("eightbitdo_controllers")
    assert manifest is not None
    assert manifest.load() is EightBitDoController


def test_the_manifest_claims_a_product_id_rather_than_the_whole_vendor():
    """The opposite of the Creative and Jabra modules, deliberately. There is no capability query
    here: the byte offsets *are* the capability list, and 8BitDo's other families use different
    records. A vendor-wide rule would offer a confident page of wrong offsets and write them."""
    from hardware_ui.core.modules import ModuleRegistry

    manifest = ModuleRegistry.discover().get("eightbitdo_controllers")
    usb_rules = [r for r in manifest.match if str(r.transport) == "usb"]
    assert usb_rules
    assert all(r.product_id for r in usb_rules)
