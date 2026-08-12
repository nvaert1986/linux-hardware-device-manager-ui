"""Unit tests for the GNP protocol layer.

The expected byte layout is taken from GnProtocol.GnpPacketSerializer's IL, not guessed —
see docs/JABRA_GNP_PROTOCOL.md §3. Where a test encodes a vendor rule, the rule is named in
the docstring so a future reader can check it against the decompiled source.

Run with:  python -m unittest   (or: python -m pytest)
"""
from __future__ import annotations

import json
import unittest

from hardware_ui.modules.jabra_headsets import assets as catalogue_source
from hardware_ui.modules.jabra_headsets.protocol import (
    HEADER_SIZE,
    MAX_DATA_SIZE,
    Command,
    FramingError,
    Nack,
    Packet,
    PacketType,
    SequenceCounter,
    Target,
    command_name,
    nack_description,
)
from hardware_ui.modules.jabra_headsets.protocol.report_descriptor import find_report, parse

# The catalogue is not in the tree — it is fetched into the vendor directory on consent, so
# these guards skip rather than fail when it is absent. See the module's assets.py.


#: The property catalogue is not redistributed with this project (see catalogue_source), so tests
#: that need it skip rather than fail on a fresh checkout.
def _require_catalogue():
    if catalogue_source.find() is None:
        raise unittest.SkipTest(
            "property catalogue not present - run the app once to download it, or: "
            "npm pack @gnaudio/jabra-properties-definition"
        )


class TestFraming(unittest.TestCase):
    def test_encode_read_ancmode(self):
        """ancMode is CONFIG (0x13) subcommand 0x87 — from properties.json."""
        packet = Packet(command=Command.CONFIG, dest=0x01, src=0x00, seq=0x10,
                        type=PacketType.READ, data=bytes([0x87]))
        # dest, src, seq, (READ<<6)|6, command, subcommand
        self.assertEqual(packet.encode().hex(), "010010461387")

    def test_length_field_counts_the_header(self):
        """Serialize writes `len(data) + 5` into the low 6 bits, not len(data)."""
        packet = Packet(command=0x13, dest=0x01, data=b"\x87\x01")
        encoded = packet.encode()
        self.assertEqual(encoded[3] & 0x3F, HEADER_SIZE + 2)
        self.assertEqual(packet.total_length, 7)

    def test_type_occupies_the_top_two_bits(self):
        for ptype in PacketType:
            encoded = Packet(command=0x13, dest=0x01, type=ptype).encode()
            self.assertEqual(encoded[3] >> 6, int(ptype))
            self.assertEqual(encoded[3] & 0x3F, HEADER_SIZE)

    def test_zero_destination_rejected(self):
        """Serialize throws ArgumentOutOfRangeException('Destination address cannot be 0')."""
        with self.assertRaises(FramingError):
            Packet(command=0x13, dest=0x00).encode()

    def test_max_payload(self):
        """MAX_DATA_SIZE is 0x3A: 5 + 58 = 63, the largest the 6-bit length field holds."""
        self.assertEqual(MAX_DATA_SIZE, 0x3A)
        ok = Packet(command=0x13, dest=0x01, data=b"\x00" * MAX_DATA_SIZE)
        self.assertEqual(len(ok.encode()), 63)
        self.assertEqual(ok.encode()[3] & 0x3F, 63)
        with self.assertRaises(FramingError):
            Packet(command=0x13, dest=0x01, data=b"\x00" * (MAX_DATA_SIZE + 1)).encode()

    def test_roundtrip(self):
        original = Packet(command=Command.IDENT, dest=0x02, src=0x01, seq=0x7F,
                          type=PacketType.REPLY, data=bytes(range(10)))
        self.assertEqual(Packet.decode(original.encode()), original)

    def test_decode_ignores_trailing_report_padding(self):
        """A 63-byte HID report is zero-padded; only `total length` bytes are the packet."""
        wire = Packet(command=0x13, dest=0x01, data=b"\x87\x02").encode()
        padded = wire.ljust(63, b"\x00")
        self.assertEqual(Packet.decode(padded).data, b"\x87\x02")

    def test_decode_rejects_short_buffer(self):
        with self.assertRaises(FramingError):
            Packet.decode(b"\x01\x00\x00")

    def test_decode_rejects_length_below_header(self):
        # byte 3 declares a total length of 4, which cannot even hold the header
        with self.assertRaises(FramingError):
            Packet.decode(bytes([0x01, 0x00, 0x00, 0x04, 0x13]))

    def test_strict_rejects_overrun_but_lenient_clamps(self):
        """Vendor Deserialize() clamps to the buffer; we reject unless strict=False."""
        # declares total length 20 but only 6 bytes are present
        buf = bytes([0x01, 0x00, 0x00, 0x14, 0x13, 0x87])
        with self.assertRaises(FramingError):
            Packet.decode(buf)
        self.assertEqual(Packet.decode(buf, strict=False).data, b"\x87")

    def test_field_range_validation(self):
        with self.assertRaises(FramingError):
            Packet(command=0x100, dest=0x01)
        with self.assertRaises(FramingError):
            Packet(command=0x13, dest=0x01, seq=-1)


class TestSequenceCounter(unittest.TestCase):
    """GetSequenceNumber(): pre-increment, and force 1 when the result would be 0."""

    def test_pre_increments(self):
        counter = SequenceCounter(start=0x10)
        self.assertEqual(counter.next(), 0x11)
        self.assertEqual(counter.next(), 0x12)

    def test_skips_zero_on_wrap(self):
        counter = SequenceCounter(start=0xFE)
        self.assertEqual(counter.next(), 0xFF)
        self.assertEqual(counter.next(), 0x01)   # not 0x00

    def test_never_emits_zero(self):
        counter = SequenceCounter(start=0x00)
        self.assertNotIn(0, [counter.next() for _ in range(600)])

    def test_peek_matches_next(self):
        counter = SequenceCounter(start=0x41)
        self.assertEqual(counter.peek(), counter.next())

    def test_random_seed_in_vendor_range(self):
        for _ in range(200):
            self.assertLess(SequenceCounter()._value, 0xFE)


class TestIds(unittest.TestCase):
    def test_command_names(self):
        self.assertEqual(command_name(0x13), "CONFIG")
        self.assertEqual(command_name(0xFE), "NACK")
        # 0x26 is used by properties.json but unnamed in GnProtocol.GnpCommands
        self.assertEqual(command_name(0x26), "CMD_0x26")

    def test_nack_descriptions(self):
        self.assertEqual(nack_description(Nack.UNKNOWN_SUB_CMD), "Unknown sub-command")
        self.assertEqual(nack_description(Nack.ACCESS_DENIED), "Access denied")
        self.assertIn("0x42", nack_description(0x42))

    def test_targets(self):
        self.assertEqual(Target.SECONDARY, 0x04)


class TestReportDescriptor(unittest.TestCase):
    #: Minimal descriptor: vendor page 0xFF00, usage 0x01, report 0x05, 63 bytes in+out.
    #: Mirrors the shape of the Link 390's own descriptor.
    DESC = bytes([
        0x06, 0x00, 0xFF,        # Usage Page (0xFF00)
        0x09, 0x05,              # Usage (0x05)
        0xA1, 0x01,              # Collection (Application)
        0x85, 0x05,              #   Report ID (5)
        0x09, 0x01,              #   Usage (0x01)
        0x75, 0x08,              #   Report Size (8)
        0x95, 0x3F,              #   Report Count (63)
        0x81, 0x02,              #   Input
        0x09, 0x01,              #   Usage (0x01)
        0x75, 0x08,              #   Report Size (8)
        0x95, 0x3F,              #   Report Count (63)
        0x91, 0x02,              #   Output
        0xC0,                    # End Collection
    ])

    def test_finds_gnp_report(self):
        out = find_report(self.DESC, 0xFF00, 0x01, "output")
        self.assertIsNotNone(out)
        self.assertEqual((out.report_id, out.size_bytes), (0x05, 63))
        inp = find_report(self.DESC, 0xFF00, 0x01, "input")
        self.assertEqual((inp.report_id, inp.size_bytes), (0x05, 63))

    def test_absent_usage_returns_none(self):
        self.assertIsNone(find_report(self.DESC, 0xFF13, 0x00, "output"))

    def test_parse_does_not_crash_on_truncation(self):
        for cut in range(1, len(self.DESC)):
            parse(self.DESC[:cut])


class TestCatalogue(unittest.TestCase):
    """Guards on the vendored ISC data — see the module's assets.py."""

    @classmethod
    def setUpClass(cls):
        _require_catalogue()
        with open(catalogue_source.find()) as fh:
            cls.props = json.load(fh)

    def test_expected_size(self):
        self.assertEqual(len(self.props), 423)

    def test_ancmode_encoding_matches_the_documented_bytes(self):
        anc = self.props["ancMode"]
        self.assertEqual(anc["autogenName"], "CONFIG__ANC")
        read = next(s for s in anc["read"] if s["typeName"] == "gnpRead")
        self.assertEqual((read["command"], read["subcommand"]), (0x13, 0x87))
        self.assertEqual(read["convert"]["sizeByte"], 1)
        self.assertEqual(
            anc["valueType"]["enum"], ["off", "anc", "hearThrough", "hearThroughMix"]
        )

    def test_every_gnp_step_has_command_and_subcommand(self):
        """The schema marks both required; rely on it when building requests."""
        def walk(node):
            if isinstance(node, dict):
                if node.get("typeName") in ("gnpRead", "gnpWrite", "gnpEvent"):
                    self.assertIn("command", node)
                    self.assertIn("subcommand", node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.props)

    def test_addresses_are_known_targets(self):
        """Any `address` we meet should be one we have a name for, or the default."""
        found = set()

        def walk(node):
            if isinstance(node, dict):
                if (node.get("typeName") in ("gnpRead", "gnpWrite", "gnpEvent")
                        and (address := node.get("address")) is not None):
                    found.add(address)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.props)
        self.assertEqual(found, {int(t) for t in Target})


if __name__ == "__main__":
    unittest.main()
