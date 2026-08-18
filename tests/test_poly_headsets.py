"""Poly module tests. No hardware, no vendor data.

The catalogue fixture is hand-written in Poly's JSON shape rather than copied from a real one:
these tests must pass on a machine that has never run the vendor import.
"""

from __future__ import annotations

import json

from hardware_ui.core import Kind
from hardware_ui.modules.poly_headsets import assets
from hardware_ui.modules.poly_headsets import capabilities as C
from hardware_ui.modules.poly_headsets.protocol import catalogue as cat
from hardware_ui.modules.poly_headsets.protocol.framing import Frame, FrameBuffer, MessageType


def block(deckard_id: str, values=None) -> dict:
    out: dict = {"deckardId": deckard_id}
    if values is not None:
        out["possibleValues"] = values
    return out


def byte(name: str, value: int) -> dict:
    return {"name": name, "payload": [{"type": "BYTE", "value": str(value)}]}


RAW = {
    "pid": "0x16a",
    "settings": [
        {
            "settingName": "sideToneLevel",
            "get": block("0x410"),
            "set": block("0x410", [byte("low", 0), byte("medium", 1), byte("high", 2)]),
            "event": block("0x410"),
        },
        {
            "settingName": "G616",
            "get": block("0xf0c"),
            "set": block("0xf0c", [byte("true", 1), byte("false", 0)]),
            "event": block("0xf0c"),
        },
        {
            "settingName": "muteReminderFrequency",
            "get": block("0xa22"),
            "set": block("0xa20", [
                {"name": "300", "payload": [{"type": "UNSIGNED_SHORT", "value": "300"}]},
                {"name": "900", "payload": [{"type": "UNSIGNED_SHORT", "value": "900"}]},
            ]),
            # Poly's Windows catalogues name this after the *get* id. It is wrong.
            "event": block("0xa22"),
        },
        {
            "settingName": "restoreDefaults",
            "set": block("0xf13", [byte("restore", 1)]),
        },
    ],
}


def parsed() -> cat.Catalogue:
    return cat._parse(json.loads(json.dumps(RAW)))


def keys(page) -> list[str]:
    return [c.key for c in page]


ALL = ["sideToneLevel", "G616", "muteReminderFrequency"]


# --------------------------------------------------------------------------- the event-id fix


def test_event_id_follows_the_set_id_where_they_differ():
    """Measured across all 231 Windows catalogues: 418 settings have get != set, and 107 name the
    event after the get id. A live capture shows the device emits it on the *set* id."""
    data = json.loads(json.dumps(RAW))
    assert assets.correct_event_ids(data) == 1
    by_name = {s["settingName"]: s for s in data["settings"]}
    assert by_name["muteReminderFrequency"]["event"]["deckardId"] == "0xa20"


def test_the_correction_leaves_agreeing_settings_alone():
    data = json.loads(json.dumps(RAW))
    assets.correct_event_ids(data)
    by_name = {s["settingName"]: s for s in data["settings"]}
    assert by_name["sideToneLevel"]["event"]["deckardId"] == "0x410"
    assert by_name["G616"]["event"]["deckardId"] == "0xf0c"


def test_the_correction_is_idempotent():
    data = json.loads(json.dumps(RAW))
    assert assets.correct_event_ids(data) == 1
    assert assets.correct_event_ids(data) == 0


# --------------------------------------------------------------------------- id namespaces


def test_read_and_write_ids_are_kept_separate():
    """`Setting` and `Command` are different namespaces -- 0x0E1C is PARTITION_INFORMATION to read
    and the destructive REMOVE_PARTITION_INFORMATION to write. Never compute one from the other."""
    setting = parsed().by_name("muteReminderFrequency")
    assert (setting.get_id, setting.set_id) == (0x0A22, 0x0A20)
    assert setting.read_write_ids_differ


def test_a_write_only_entry_is_an_action_and_never_a_control():
    action = parsed().by_name("restoreDefaults")
    assert action.is_action and action.get_id is None


# --------------------------------------------------------------------------- capabilities


def test_a_two_valued_boolean_becomes_a_switch():
    page = C.build(parsed(), supported=ALL)
    assert page.by_key("setting.G616").kind is Kind.TOGGLE
    assert page.by_key("setting.sideToneLevel").kind is Kind.CHOICE


def test_boolean_mapping_round_trips():
    from hardware_ui.modules.poly_headsets.device import PolyHeadsetDevice as D

    g616 = parsed().by_name("G616")
    assert C.boolean_names(g616) == ("true", "false")
    assert D._to_ui(g616, "true") is True
    assert D._to_ui(g616, "false") is False
    assert D._to_wire(g616, True) == "true"
    assert D._to_wire(g616, False) == "false"
    # A list setting passes its value through untouched.
    side = parsed().by_name("sideToneLevel")
    assert D._to_ui(side, "medium") == "medium"
    assert D._to_wire(side, "medium") == "medium"


def test_a_setting_the_device_does_not_implement_is_absent_not_disabled():
    """The catalogue is the UI-exposed subset, not the capability list: 26 entries versus the 33
    ids a V4310 answers. Support comes from the live probe."""
    page = C.build(parsed(), supported=["G616"])
    assert "setting.G616" in keys(page)
    assert "setting.sideToneLevel" not in keys(page)


def test_actions_land_on_maintenance_and_confirm():
    action = C.build(parsed(), supported=ALL).by_key("action.restoreDefaults")
    assert action.kind is Kind.ACTION
    assert action.group == "Maintenance"
    assert action.confirm
    assert "cannot be undone" in action.confirm_detail


def test_an_action_is_never_offered_as_a_setting():
    assert "setting.restoreDefaults" not in keys(C.build(parsed(), supported=ALL))


def test_groups_follow_the_reference_implementations_tabs():
    page = C.build(parsed(), supported=ALL, identity={"FIRMWARE_VERSION": "1"}, has_battery=True)
    # Order is GROUP_ORDER from the reference implementation, not alphabetical: Audio, Mute,
    # Calls & Prompts, Ringtones, Hearing Safety, Other -- with Info first and Maintenance last.
    assert list(page.groups()) == ["Info", "Audio", "Mute", "Hearing Safety", "Maintenance"]


def test_battery_is_a_meter_because_the_device_reports_steps():
    page = C.build(parsed(), supported=ALL, has_battery=True)
    battery = page.by_key("info.battery")
    assert battery.kind is Kind.METER
    assert not battery.writable


def test_no_battery_means_no_battery_row():
    assert "info.battery" not in keys(C.build(parsed(), supported=ALL, has_battery=False))


def test_a_device_with_no_catalogue_still_gets_a_usable_page():
    """No vendor import yet, or an unknown PID. The page must not be empty -- Reconnect and
    Re-read are what the user needs in exactly that situation."""
    page = C.build(None)
    assert keys(page) == ["info.connection", "action.refresh", "action.reconnect"]


def test_labels_fall_back_without_the_vendor_label_tables():
    """The label JSONs are vendor-derived and imported, not shipped. Every lookup degrades."""
    from hardware_ui.modules.poly_headsets import labels

    assert labels.humanize("sideToneLevel") == "Side tone level"
    assert labels.humanize("ringToneVoip") == "Ring tone VoIP"
    assert labels.humanize("G616") == "G616"  # all-caps token survives
    # Curated names are hardcoded from Poly's own UI, so they work with no import at all.
    assert labels.setting_label(parsed().by_name("G616")) == "Anti-Startle"


def test_setting_key_round_trips():
    assert C.setting_name(C.setting_key("G616")) == "G616"
    assert C.setting_name(C.action_key("restoreDefaults")) == "restoreDefaults"
    assert C.setting_name("info.battery") == ""


# --------------------------------------------------------------------------- framing


def test_a_frame_round_trips():
    frame = Frame(MessageType.SETTINGS_REQUEST, 0x0410, b"\x02", reserved=0x2000)
    assert Frame.decode(frame.encode()) == frame


def test_the_reserved_field_is_the_bladerunner_address():
    """Not reserved at all: `(dest << 4) | src`. Zero over Bluetooth, 0x2000 for a headset behind
    a dongle. Treating it as padding is why the dongle path was silent for a whole session."""
    encoded = Frame(MessageType.SETTINGS_REQUEST, 0x0410, b"", reserved=0x2000).encode()
    assert encoded[2:4] == b"\x20\x00"
    assert Frame.decode(encoded).reserved == 0x2000


def test_the_buffer_reassembles_frames_split_across_reads():
    a = Frame(MessageType.SETTINGS_REQUEST, 0x0410, b"\x01").encode()
    b = Frame(MessageType.EVENT, 0x0A20, b"\x02\x58").encode()
    buf = FrameBuffer()
    assert buf.feed(a[:3]) == []
    frames = buf.feed(a[3:] + b)
    assert [f.message_id for f in frames] == [0x0410, 0x0A20]


def test_payload_widths_come_from_the_catalogue_not_the_sdk():
    """The SDK declares SIDE_TONE_LEVEL as `int`; on the wire it is one byte. Only the
    catalogue's own type is trustworthy."""
    side = parsed().by_name("sideToneLevel")
    assert side.choice("medium").payload == b"\x01"
    freq = parsed().by_name("muteReminderFrequency")
    assert freq.choice("300").payload == b"\x01\x2c"  # UNSIGNED_SHORT, big-endian


# --------------------------------------------------------------------------- read-only advisory


def test_a_refused_write_marks_the_setting_read_only_with_an_explanation():
    """`COMMAND_UNKNOWN` is only discoverable by attempting the write -- a V4310 reads
    `linkQualityReporting` happily and refuses to set it. The control must end up locked with a
    reason, not merely failed."""
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.modules.poly_headsets.device import PolyHeadsetDevice

    dev = PolyHeadsetDevice(
        DeviceInfo(uid="bt:x", name="Poly V4310", transport=Transport.BLUETOOTH)
    )
    assert dev.advisories() == {}
    dev._read_only.add("linkQualityReporting")
    advisory = dev.advisories()["setting.linkQualityReporting"]
    assert advisory.locked
    assert "read-only" in advisory.message


# --------------------------------------------------------------------------- downstream attach


class _FakeTransport:
    """Enough of a transport to drive `_attach_downstream`."""

    has_downstream_ports = True

    def __init__(self, peers):
        self._peers = set(peers)

    def peer_product_ids(self):
        return self._peers


def _headset(ports, product_by_port, peers):
    """A session whose downstream ports answer the given product ids."""
    from hardware_ui.modules.poly_headsets.protocol.session import (
        ADDRESS_LOCAL,
        IDENTITY,
        DeckardError,
        PolyHeadset,
        br_address,
    )

    hp = PolyHeadset.__new__(PolyHeadset)
    hp.transport = _FakeTransport(peers)
    hp.br_address = ADDRESS_LOCAL
    hp.endpoint = "USB HID /dev/hidrawX"
    hp.downstream_ports = lambda: list(ports)
    hp._handshake = lambda address: None

    def read_int(_name, setting_id):
        assert setting_id == IDENTITY["USB_PID"]
        for port, product in product_by_port.items():
            if hp.br_address == br_address(port):
                if product is None:
                    raise DeckardError("no answer")
                return product
        raise DeckardError("no answer")

    hp.read_int = read_int
    return hp


def test_a_headset_behind_a_dongle_is_attached_to():
    """The case that has always worked: the thing behind the dongle is not on the bus itself."""
    from hardware_ui.modules.poly_headsets.protocol.session import br_address

    hp = _headset(ports=[2], product_by_port={2: 0x016A}, peers={0x016C})
    hp._attach_downstream()
    assert hp.br_address == br_address(2)
    assert hp.endpoint.endswith("→ port 2")


def test_a_downstream_device_that_is_plugged_in_separately_is_not_attached_to():
    """The bug: a headset in its stand reports the dongle it is paired to as *its* downstream.

    Walking there made the stand's entry configure the dongle and show the dongle's serial number
    as though it were the headset's.
    """
    from hardware_ui.modules.poly_headsets.protocol.session import ADDRESS_LOCAL

    hp = _headset(ports=[2], product_by_port={2: 0x02E6}, peers={0x02E6})
    hp._attach_downstream()
    assert hp.br_address == ADDRESS_LOCAL, "should have stayed on the device it opened"
    assert "port" not in hp.endpoint


def test_a_port_that_names_no_product_is_an_empty_socket():
    """A dongle lists ports it *could* use. Landing on one gave a page of blanks."""
    from hardware_ui.modules.poly_headsets.protocol.session import ADDRESS_LOCAL

    hp = _headset(ports=[4], product_by_port={4: None}, peers=set())
    hp._attach_downstream()
    assert hp.br_address == ADDRESS_LOCAL


def test_a_sibling_port_is_skipped_and_a_real_one_still_found():
    from hardware_ui.modules.poly_headsets.protocol.session import br_address

    hp = _headset(ports=[1, 2], product_by_port={1: 0x02E6, 2: 0x016A}, peers={0x02E6})
    hp._attach_downstream()
    assert hp.br_address == br_address(2)


def test_with_nothing_else_plugged_in_the_old_behaviour_is_unchanged():
    """No peers means no way to be wrong, so the walk proceeds exactly as before."""
    from hardware_ui.modules.poly_headsets.protocol.session import br_address

    hp = _headset(ports=[2], product_by_port={2: 0x02E6}, peers=set())
    hp._attach_downstream()
    assert hp.br_address == br_address(2)


# --------------------------------------------------------------------------- adapter settings


def _fake_catalogue(names):
    """A catalogue of simple boolean settings."""
    from hardware_ui.modules.poly_headsets.protocol import catalogue as cat

    settings = [
        cat.Setting(
            name=name, description="", get_id=0x100 + i, set_id=0x200 + i, event_id=None,
            choices=(cat.Choice(name="ENABLED", fields=()),
                     cat.Choice(name="DISABLED", fields=())),
        )
        for i, name in enumerate(names)
    ]
    return cat.Catalogue(pid=0x02E6, settings=tuple(settings))


def test_the_adapter_gets_its_own_tab():
    """Attaching downstream to the headset put the dongle's own settings out of reach.

    Pairing an adapter to a headset is exactly the thing you cannot do from the headset.
    """
    from hardware_ui.modules.poly_headsets import capabilities as C

    adapter = _fake_catalogue(["ENABLE_PAIRING", "LYNC_DIAL_TONE"])
    caps = C.build(None, adapter=(adapter, ["ENABLE_PAIRING", "LYNC_DIAL_TONE"]))
    rows = [c for c in caps if c.group == C.GROUP_ADAPTER]
    assert len(rows) == 2
    assert "adapter itself" in rows[0].note
    assert rows[1].note == "", "the explanation belongs once, at the top"


def test_adapter_settings_have_their_own_keys():
    """Both catalogues use the same setting names; a shared keyspace would cross the wires."""
    from hardware_ui.modules.poly_headsets import capabilities as C

    adapter = _fake_catalogue(["ENABLE_PAIRING"])
    caps = C.build(None, adapter=(adapter, ["ENABLE_PAIRING"]))
    key = next(c.key for c in caps if c.group == C.GROUP_ADAPTER)
    assert key.startswith(C.ADAPTER_PREFIX)
    assert key != C.setting_key("ENABLE_PAIRING")


def test_a_headset_on_its_own_cable_gets_no_adapter_tab():
    """There is no separate adapter to configure -- the endpoint *is* the headset."""
    from hardware_ui.modules.poly_headsets import capabilities as C

    caps = C.build(None, adapter=(None, ()))
    assert not [c for c in caps if c.group == C.GROUP_ADAPTER]


def test_an_adapter_setting_the_device_did_not_answer_is_absent():
    from hardware_ui.modules.poly_headsets import capabilities as C

    adapter = _fake_catalogue(["ENABLE_PAIRING", "LYNC_DIAL_TONE"])
    caps = C.build(None, adapter=(adapter, ["ENABLE_PAIRING"]))
    assert len([c for c in caps if c.group == C.GROUP_ADAPTER]) == 1


# --------------------------------------------------------------------------- vendor data prompt


def test_a_module_with_no_vendor_assets_is_never_asked_about(qapp):
    """Only a module that declares a source can want one, so the rest open straight away."""
    from hardware_ui.shell.vendor_data import source_for

    for module_id in ("razer_peripherals", "dell_monitors", "yubikeys", "sony_headsets"):
        assert source_for(module_id) is None, module_id


def test_poly_declares_a_source_the_shell_can_ask_about(qapp):
    """The gap this closes: the data existed only via `cli --import-vendor`."""
    from hardware_ui.core.assets import AssetStatus
    from hardware_ui.shell.vendor_data import source_for

    source = source_for("poly_headsets")
    assert source is not None
    assert source.status() in set(AssetStatus)


def test_present_data_asks_nothing(qapp, monkeypatch):
    from hardware_ui.core.assets import AssetStatus
    from hardware_ui.shell import vendor_data

    class Present:
        def status(self):
            return AssetStatus.PRESENT

        def acquire(self, _ui):
            raise AssertionError("must not acquire when the data is already there")

    monkeypatch.setattr(vendor_data, "source_for", lambda _mid: Present())
    assert vendor_data.ensure_vendor_data("poly_headsets", "Poly Headsets") is True


def test_declining_still_opens_the_device(qapp, monkeypatch):
    """Refusing is a choice, not a failure: the module says on its own page what it cannot label."""
    from PyQt6.QtWidgets import QMessageBox

    from hardware_ui.core.assets import AssetStatus
    from hardware_ui.shell import vendor_data

    class Missing:
        def status(self):
            return AssetStatus.MISSING

        def acquire(self, _ui):
            raise AssertionError("must not acquire after the user says no")

    monkeypatch.setattr(vendor_data, "source_for", lambda _mid: Missing())
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    assert vendor_data.ensure_vendor_data("poly_headsets", "Poly Headsets") is True


def test_a_failed_import_is_reported_but_does_not_block_the_device(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from hardware_ui.core.assets import AssetError, AssetStatus
    from hardware_ui.shell import vendor_data

    class Failing:
        def status(self):
            return AssetStatus.MISSING

        def acquire(self, _ui):
            raise AssetError("that installer does not contain the catalogues")

    warned: list[str] = []
    monkeypatch.setattr(vendor_data, "source_for", lambda _mid: Failing())
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda _p, _t, body, *a, **k: warned.append(body))
    )
    assert vendor_data.ensure_vendor_data("poly_headsets", "Poly Headsets") is True
    assert "does not contain the catalogues" in warned[0]
    assert "still open" in warned[0]


# --------------------------------------------------------------------------- identity strings
#
# Reported from the field: a Voyager 4320 showed its Hardware row as Chinese characters while the
# serial and firmware beside it read fine. The reference implementation sniffed the first two
# bytes for a NUL to choose between UTF-8 and UTF-16BE, and it was only ever run against a 4310.
# Both encodings are now tried and the more legible result wins.

from hardware_ui.modules.poly_headsets.protocol.session import decode_string  # noqa: E402


def framed(body: bytes) -> bytes:
    """A Deckard string payload: u16 big-endian byte length, then the bytes."""
    return len(body).to_bytes(2, "big") + body


def test_plain_ascii_decodes():
    assert decode_string(framed(b"PLT-4310")) == "PLT-4310"


def test_ascii_padded_to_its_field_width_decodes():
    assert decode_string(framed(b"PLT-4310" + bytes(8))) == "PLT-4310"


def test_utf16be_decodes():
    assert decode_string(framed("PLT-4310".encode("utf-16-be"))) == "PLT-4310"


def test_utf16be_padded_to_its_field_width_decodes():
    assert decode_string(framed("PLT-4310".encode("utf-16-be") + bytes(8))) == "PLT-4310"


def test_a_single_leading_nul_no_longer_turns_ascii_into_chinese():
    """The first of the two failure modes. One stray NUL made the old sniff pick UTF-16BE, and
    every pair of ASCII bytes then lands in the CJK block."""
    assert decode_string(framed(b"\x00PLT-4310")) == "PLT-4310"


def test_a_field_that_is_not_text_decodes_to_nothing_rather_than_mojibake():
    """The second, and what the 4320 actually hit: bytes that are not a string in either encoding.
    UTF-16BE renders them as CJK without complaint, so the result has to be rejected on content."""
    assert decode_string(framed(bytes.fromhex("4FF5520052299A91002B0028"))) == ""


def test_a_genuinely_accented_name_still_survives():
    """The rejection is on illegibility, not on non-ASCII: a real name with an accent must pass."""
    assert decode_string(framed("Vy-4320é".encode("utf-16-be"))) == "Vy-4320é"


def test_an_empty_or_runt_payload_is_empty():
    assert decode_string(b"") == ""
    assert decode_string(framed(b"")) == ""
    assert decode_string(b"\x00") == ""


def test_utf16be_read_as_utf8_scores_badly_because_of_its_nuls():
    """The bug in the first attempt at this fix: NULs were stripped before scoring, so the wrong
    decoding scored a perfect 1.0 and won."""
    from hardware_ui.modules.poly_headsets.protocol.session import _legible

    wrong = "PLT-4310".encode("utf-16-be").decode("utf-8")
    assert _legible(wrong) < _legible("PLT-4310")
