"""Unit tests for the catalogue interpreter and HID reassembly.

Expected values are taken from the real Evolve2 85 / Link 390 where noted — those readings are
recorded in docs/STATUS.md so a future change that breaks them is obvious.
"""
from __future__ import annotations

import unittest

from hardware_ui.modules.jabra_headsets import assets as catalogue_source
from hardware_ui.modules.jabra_headsets.protocol import catalogue
from hardware_ui.modules.jabra_headsets.protocol.framing import Packet, PacketType
from hardware_ui.modules.jabra_headsets.protocol.hid import _Reassembler
from hardware_ui.modules.jabra_headsets.protocol.process import (
    Interpreter,
    ProcessError,
    UnsupportedStep,
    apply_converters,
    decode_bytes,
    encode_bytes,
)


#: The property catalogue is not redistributed with this project (see catalogue_source), so tests
#: that need it skip rather than fail on a fresh checkout.
def _require_catalogue():
    if catalogue_source.find() is None:
        raise unittest.SkipTest(
            "property catalogue not present - run the app once to download it, or: "
            "npm pack @gnaudio/jabra-properties-definition"
        )

class FakeSession:
    """Records requests and replays canned replies."""

    def __init__(self, replies: dict[tuple[int, int], bytes] | None = None):
        self.replies = replies or {}
        self.reads: list[tuple[int, int, int | None]] = []
        self.writes: list[tuple[int, int, bytes, int | None]] = []

    def request(self, command, subcommand, payload=b"", *, address=None, **kw):
        self.reads.append((command, subcommand, address))
        data = self.replies.get((command, subcommand), b"")
        return Packet(command=command, dest=0x04, type=PacketType.REPLY,
                      data=bytes([subcommand]) + data)

    def write(self, command, subcommand, payload=b"", *, address=None, **kw):
        self.writes.append((command, subcommand, payload, address))
        return b""

    def read(self, command, subcommand, *, address=None, **kw):
        self.reads.append((command, subcommand, address))
        return self.replies.get((command, subcommand), b"")


class TestByteConverters(unittest.TestCase):
    def test_number_signed_and_endianness(self):
        signed = {"typeName": "number", "sizeByte": 1, "isSigned": True,
                  "isLittleEndian": True}
        self.assertEqual(decode_bytes(signed, b"\xf4"), -12)
        self.assertEqual(encode_bytes(signed, -12), b"\xf4")
        big = {"typeName": "number", "sizeByte": 4, "isSigned": False,
               "isLittleEndian": False}
        self.assertEqual(decode_bytes(big, b"\x00\x00\x01\x7f"), 0x17F)
        self.assertEqual(encode_bytes(big, 0x17F), b"\x00\x00\x01\x7f")

    def test_length_prefixed_string(self):
        """Observed live: name reads as 0e 'Jabra Link 390'."""
        convert = {"typeName": "string", "encoding": "utf-8",
                   "isLengthPrefixed": True, "isNullTerminated": False}
        raw = bytes([14]) + b"Jabra Link 390"
        self.assertEqual(decode_bytes(convert, raw), "Jabra Link 390")
        self.assertEqual(encode_bytes(convert, "Jabra Link 390"), raw)

    def test_string_ignores_bogus_length_prefix(self):
        convert = {"typeName": "string", "encoding": "ascii",
                   "isLengthPrefixed": True, "isNullTerminated": False}
        # length byte claims more than is present — decode what there is instead of failing
        self.assertEqual(decode_bytes(convert, bytes([99]) + b"abc"), "abc")

    def test_list_length_prefixed(self):
        convert = {"typeName": "list", "isLengthPrefixed": True,
                   "convert": {"typeName": "number", "sizeByte": 1,
                               "isSigned": False, "isLittleEndian": True}}
        self.assertEqual(decode_bytes(convert, b"\x03\x0a\x14\x1e"), [10, 20, 30])
        self.assertEqual(encode_bytes(convert, [10, 20, 30]), b"\x03\x0a\x14\x1e")

    def test_json_object_with_match_and_list(self):
        """The configChangeEvents shape: a matched event id then a list of subcommands."""
        convert = {
            "typeName": "jsonObject",
            "fields": [
                {"fieldName": "eventId", "convert": {"typeName": "match", "value": 4}},
                {"fieldName": "subcommands",
                 "convert": {"typeName": "list", "isLengthPrefixed": False,
                             "convert": {"typeName": "number", "sizeByte": 1,
                                         "isSigned": False, "isLittleEndian": True}}},
            ],
        }
        decoded = decode_bytes(convert, b"\x04\x87\x81")
        self.assertEqual(decoded, {"eventId": 4, "subcommands": [0x87, 0x81]})

    def test_json_object_match_rejects_wrong_value(self):
        convert = {"typeName": "jsonObject", "fields": [
            {"fieldName": "eventId", "convert": {"typeName": "match", "value": 4}}]}
        with self.assertRaises(ProcessError):
            decode_bytes(convert, b"\x09")

    def test_number_short_buffer_raises(self):
        convert = {"typeName": "number", "sizeByte": 2, "isSigned": False,
                   "isLittleEndian": True}
        with self.assertRaises(ProcessError):
            decode_bytes(convert, b"")


class TestJsonConverters(unittest.TestCase):
    ANC = {"typeName": "translate", "direction": "toValue",
           "values": {"off": 0, "anc": 1, "hearThrough": 2, "hearThroughMix": 3}}

    def test_translate_to_value_and_back(self):
        self.assertEqual(apply_converters(self.ANC, 1), "anc")
        to_int = dict(self.ANC, direction="toInt")
        self.assertEqual(apply_converters(to_int, "hearThrough"), 2)

    def test_translate_booleans(self):
        convert = {"typeName": "translate", "direction": "toValue",
                   "values": {"false": 0, "true": 1}}
        self.assertIs(apply_converters(convert, 1), True)
        self.assertIs(apply_converters(convert, 0), False)
        back = dict(convert, direction="toInt")
        self.assertEqual(apply_converters(back, True), 1)

    def test_translate_to_int_rejects_unknown(self):
        with self.assertRaises(ProcessError):
            apply_converters(dict(self.ANC, direction="toInt"), "nonsense")

    def test_bitmask_extract(self):
        convert = {"typeName": "bitmaskExtract", "offset": 4, "bitCount": 1}
        self.assertEqual(apply_converters(convert, 0x17F), 1)
        self.assertEqual(apply_converters({"typeName": "bitmaskExtract",
                                           "offset": 9, "bitCount": 1}, 0x17F), 0)

    def test_scale_and_constant(self):
        self.assertAlmostEqual(apply_converters({"typeName": "scale", "by": 0.5}, 8), 4.0)
        self.assertEqual(apply_converters({"typeName": "constant", "value": 7}, None), 7)

    def test_join_with_dotnet_format(self):
        convert = {"typeName": "join", "separator": "-", "format": "{0:X2}"}
        self.assertEqual(apply_converters(convert, [0x0A, 0xFF]), "0A-FF")

    def test_map_applies_per_item(self):
        convert = {"typeName": "map", "convert": [self.ANC]}
        self.assertEqual(apply_converters(convert, [0, 2]), ["off", "hearThrough"])

    def test_unknown_converter_raises(self):
        with self.assertRaises(UnsupportedStep):
            apply_converters({"typeName": "somethingNew"}, 1)


class TestInterpreter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _require_catalogue()
        cls.catalogue = catalogue.load()

    def test_reads_ancmode_from_the_real_pipeline(self):
        """ancMode: CONFIG/0x87, one byte, translated to the enum name."""
        session = FakeSession({(0x13, 0x87): b"\x02"})
        value = Interpreter(session, address=0x04).read(self.catalogue["ancMode"])
        self.assertEqual(value, "hearThrough")
        self.assertEqual(session.reads, [(0x13, 0x87, 0x04)])

    def test_writes_ancmode_translating_back_to_bytes(self):
        session = FakeSession()
        Interpreter(session, address=0x04).write(self.catalogue["ancMode"], "anc")
        self.assertEqual(session.writes, [(0x13, 0x87, b"\x01", 0x04)])

    def test_explicit_catalogue_address_overrides_the_endpoint(self):
        """secondaryDeviceName declares address 4; it must not inherit ours."""
        prop = self.catalogue["secondaryDeviceName"]
        self.assertEqual(prop.address, 0x04)
        session = FakeSession({prop.endpoint("read"): b"\x03abc"})
        Interpreter(session, address=0x01).read(prop)
        self.assertEqual(session.reads[0][2], 0x04)

    def test_signed_range_property(self):
        """hearThroughLevel is a signed byte spanning -12..6."""
        prop = self.catalogue["hearThroughLevel"]
        self.assertEqual((prop.value_type.minimum, prop.value_type.maximum), (-12, 6))
        session = FakeSession({(0x13, 0x81): b"\xf4"})
        self.assertEqual(Interpreter(session).read(prop), -12)

    def test_decode_configchangeevents_payload(self):
        """The catch-all change stream yields the changed subcommands."""
        prop = self.catalogue["configChangeEvents"]
        value = Interpreter(None).decode_event(prop, b"\x04\x87\x81")
        self.assertEqual(value, [0x87, 0x81])

    def test_gatt_only_property_reports_why(self):
        gatt = [p for p in self.catalogue
                if any(s.get("typeName", "").startswith("gatt") for s in p.read)]
        if not gatt:
            self.skipTest("no gatt-only readable property in this catalogue version")
        with self.assertRaises(UnsupportedStep):
            Interpreter(FakeSession()).read(gatt[0])

    def test_read_without_session_raises_cleanly(self):
        with self.assertRaises(ProcessError):
            Interpreter(None).read(self.catalogue["ancMode"])


class TestCatalogueShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _require_catalogue()
        cls.catalogue = catalogue.load()

    def test_ancmode_capability_flags(self):
        prop = self.catalogue["ancMode"]
        self.assertTrue(prop.readable and prop.writable and prop.has_event)
        self.assertEqual(prop.endpoint("read"), (0x13, 0x87))
        self.assertEqual(prop.value_type.enum,
                         ("off", "anc", "hearThrough", "hearThroughMix"))

    def test_subscription_bit_offsets(self):
        """configChangeEvents is bit 4 of DEVICE/0x4C — the mask the device reports as 0x17F."""
        prop = self.catalogue["configChangeEvents"]
        self.assertTrue(prop.needs_subscription)
        insert = next(s for s in prop.subscribe if s["typeName"] == "bitmaskInsert")
        self.assertEqual(insert["offset"], 4)
        read = next(s for s in prop.subscribe if s["typeName"] == "gnpRead")
        self.assertEqual((read["command"], read["subcommand"]), (0x0D, 0x4C))

    def test_by_subcommand_resolves_change_notifications(self):
        """configChangeEvents reports subcommands, so this lookup must find the property.

        Live: writing ancMode produced `4c 04 87`, and 0x87 resolved to both ancMode and
        ancModePcApp — several names can share one subcommand, hence a list.
        """
        names = [p.name for p in self.catalogue.by_subcommand(0x13, 0x87)]
        self.assertIn("ancMode", names)
        self.assertIn("ancModePcApp", names)

    def test_event_id_equals_subscription_bit_except_ancambiencemode(self):
        """All DEVICE/0x4C notifications share one stream, keyed by eventId.

        eventId == the arming bit for every property but ancAmbienceMode (eventId 1, bit 9), so
        neither value may be derived from the other.
        """
        pairs = {}
        for prop in self.catalogue:
            if prop.endpoint("event") != (0x0D, 0x4C):
                continue
            event_id = None
            for step in prop.event:
                if step.get("typeName") != "gnpEvent":
                    continue                    # assign steps carry a *list* of converters
                convert = step.get("convert")
                if not isinstance(convert, dict):
                    continue
                for field in convert.get("fields", []):
                    if field["convert"].get("typeName") == "match":
                        event_id = field["convert"]["value"]
            bit = next((s.get("offset") for s in prop.subscribe
                        if s.get("typeName") == "bitmaskInsert"), None)
            if event_id is not None and bit is not None:
                pairs[prop.name] = (event_id, bit)

        self.assertEqual(pairs["configChangeEvents"], (4, 4))
        self.assertEqual(pairs["onHeadDetectionStatus"], (2, 2))
        self.assertEqual(pairs["boomArmPosition"], (6, 6))
        self.assertEqual(pairs["ancAmbienceMode"], (1, 9))     # the documented exception
        mismatched = {n for n, (e, b) in pairs.items() if e != b}
        self.assertEqual(mismatched, {"ancAmbienceMode"})

    def test_configchangeevents_rejects_a_foreign_event_id(self):
        """An onHeadDetection packet (eventId 2) must not decode as a config change."""
        prop = self.catalogue["configChangeEvents"]
        with self.assertRaises(ProcessError):
            Interpreter(None).decode_event(prop, b"\x02\x03")

    def test_bitmasked_properties_are_flagged(self):
        masked = [p for p in self.catalogue if p.is_bitmasked]
        self.assertTrue(masked, "expected some bitmask-packed properties")
        # These need read-modify-write; the flag is what device.py keys off.
        self.assertIn("configChangeEvents", [p.name for p in masked])

    def test_unverifiable_properties_are_identifiable(self):
        """13 writable properties are not readable, but 3 of those do emit an event.

        So only 10 are genuinely unconfirmable — for those, the absence of a NACK is the only
        evidence a write landed, and device.set() says so rather than implying confirmation.
        """
        write_only = [p for p in self.catalogue if p.writable and not p.readable]
        self.assertEqual(len(write_only), 13)
        self.assertEqual(len([p for p in write_only if p.has_event]), 3)
        unverifiable = [p for p in self.catalogue if p.writable and not p.verifiable]
        self.assertEqual(len(unverifiable), 10)


class TestReassembler(unittest.TestCase):
    """A 32-byte report (the Evolve2 85 deskstand) cannot hold a 63-byte packet."""

    def test_single_report_with_padding(self):
        packet = Packet(command=0x13, dest=0x04, data=b"\x87\x01")
        report = packet.encode().ljust(63, b"\x00")
        self.assertEqual(_Reassembler().feed(report), packet)

    def test_fragmented_across_two_reports(self):
        packet = Packet(command=0x02, dest=0x04, type=PacketType.REPLY,
                        data=bytes(range(40)))
        wire = packet.encode()
        self.assertEqual(len(wire), 45)
        reasm = _Reassembler()
        self.assertIsNone(reasm.feed(wire[:32]))     # first 32-byte report
        self.assertTrue(reasm.waiting)
        self.assertEqual(reasm.feed(wire[32:].ljust(32, b"\x00")), packet)
        self.assertFalse(reasm.waiting)

    def test_reset_discards_a_partial_packet(self):
        packet = Packet(command=0x02, dest=0x04, data=bytes(range(40)))
        reasm = _Reassembler()
        reasm.feed(packet.encode()[:32])
        reasm.reset()
        self.assertFalse(reasm.waiting)

    def test_runt_report_ignored(self):
        self.assertIsNone(_Reassembler().feed(b"\x01\x02"))


if __name__ == "__main__":
    unittest.main()


class TestEqualizer(unittest.TestCase):
    """Codec for CONFIG/0x7D, against a payload captured from Jabra Direct."""

    #: One real write from jabraevolve.pcapng — see docs/MODES-AND-CAPTURE-MAP.md §2.2.
    CAPTURED = bytes.fromhex(
        "05000000" "00b4005a" "06c202ee" "00000676" "0bb8003c"
        "06292ee0" "00f0063d" "59100168" "0828"
    )

    def setUp(self):
        from hardware_ui.modules.jabra_headsets.equalizer import Equalizer
        self.Equalizer = Equalizer

    def test_decodes_the_captured_payload(self):
        eq = self.Equalizer.decode(self.CAPTURED)
        self.assertEqual([b.hz for b in eq.bands], [60, 250, 1000, 4000, 7600])
        self.assertEqual([b.db for b in eq.bands], [1.5, 0.0, 1.0, 4.0, 6.0])
        self.assertEqual(eq.trailer, 0x0828)

    def test_round_trip_is_byte_identical(self):
        """A write must reproduce the opaque per-band field and the frequencies exactly."""
        eq = self.Equalizer.decode(self.CAPTURED)
        self.assertEqual(eq.encode(), self.CAPTURED)

    def test_gain_is_db_times_60(self):
        eq = self.Equalizer.decode(self.CAPTURED)
        self.assertEqual(eq.bands[4].gain_raw, 360)          # 6.0 dB
        self.assertEqual(eq.bands[0].gain_raw, 90)           # 1.5 dB

    def test_flat_zeroes_gains_and_keeps_everything_else(self):
        eq = self.Equalizer.decode(self.CAPTURED)
        flat = eq.flat()
        self.assertEqual([b.db for b in flat.bands], [0.0] * 5)
        self.assertEqual([b.a for b in flat.bands], [b.a for b in eq.bands])
        self.assertEqual([b.freq_field for b in flat.bands],
                         [b.freq_field for b in eq.bands])

    def test_negative_gain_survives_the_round_trip(self):
        """Gains are signed; a cut must not wrap to a huge positive value."""
        eq = self.Equalizer.decode(self.CAPTURED).with_gains_db([-6.0, 0, 0, 0, 0])
        self.assertEqual(eq.bands[0].gain_raw, -360)
        self.assertEqual(self.Equalizer.decode(eq.encode()).bands[0].db, -6.0)

    def test_short_payload_raises_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            self.Equalizer.decode(b"\x05\x00")


class TestEqualizerReadLayout(unittest.TestCase):
    """The read reply uses a different layout from the write payload — see the capture."""

    #: Live from an Evolve2 85, flat: 38 bytes after the echoed subcommand.
    LIVE_FLAT = bytes.fromhex(
        "0500001e" "00b40000" "06c218" "02ee0000" "067618"
        "0bb80000" "062918" "2ee00000" "063d28" "59100000" "0828"
    )
    #: From jabraevolve.pcapng, bands boosted.
    CAPTURED = bytes.fromhex(
        "0500001e" "00b4001e" "06c218" "02ee0078" "067618"
        "0bb800b4" "062918" "2ee000f0" "063d28" "59100168" "0828"
    )
    #: The write Jabra Direct sent while the bands were in the state above (subcommand stripped).
    CAPTURED_WRITE_TAIL = bytes.fromhex(
        "06c202ee" "0078" "06760bb8" "00b4" "06292ee0" "00f0" "063d5910" "0168" "0828"
    )

    def setUp(self):
        from hardware_ui.modules.jabra_headsets.equalizer import Equalizer
        self.Equalizer = Equalizer

    def test_read_layout_gives_the_right_frequencies(self):
        eq = self.Equalizer.decode_read(self.LIVE_FLAT)
        self.assertEqual([b.hz for b in eq.bands], [60, 250, 1000, 4000, 7600])

    def test_band_zero_has_an_implicit_a_of_zero(self):
        """A arrives *before* each later band; band 0 has none, and the write shows it as 0."""
        eq = self.Equalizer.decode_read(self.LIVE_FLAT)
        self.assertEqual(eq.bands[0].a, 0)
        self.assertEqual([b.a for b in eq.bands[1:]], [1730, 1654, 1577, 1597])

    def test_gains_decode_from_the_capture(self):
        eq = self.Equalizer.decode_read(self.CAPTURED)
        self.assertEqual([b.db for b in eq.bands], [0.5, 2.0, 3.0, 4.0, 6.0])

    def test_encoding_reproduces_the_vendor_write(self):
        """A read then a write must produce exactly what Jabra Direct put on the wire."""
        payload = self.Equalizer.decode_read(self.CAPTURED).encode()
        self.assertEqual(len(payload), 34)
        self.assertTrue(payload.endswith(self.CAPTURED_WRITE_TAIL),
                        f"got {payload.hex(' ')}")

    def test_write_layout_is_not_mistaken_for_a_read(self):
        """The 34-byte write form must still decode via decode(), not decode_read()."""
        write_form = self.Equalizer.decode_read(self.CAPTURED).encode()
        self.assertEqual(
            [b.hz for b in self.Equalizer.decode(write_form).bands],
            [60, 250, 1000, 4000, 7600],
        )

    def test_truncated_read_raises(self):
        with self.assertRaises(ValueError):
            self.Equalizer.decode_read(self.LIVE_FLAT[:6])
