"""The Jabra module's mapping layer: catalogue property -> control.

The protocol underneath is covered by ``test_jabra_framing`` and ``test_jabra_process``, ported
from the project that verified it on hardware. What is new here is the translation into the
shell's schema, and the two things that translation must never get wrong: offering a destructive
property as a control, and claiming a write was verified when the property cannot be read back.
"""

from __future__ import annotations

import pytest

from hardware_ui.core import DeviceError, Kind
from hardware_ui.modules.jabra_headsets import capabilities as C
from hardware_ui.modules.jabra_headsets import categories, labels
from hardware_ui.modules.jabra_headsets.protocol.catalogue import Catalogue

GNP_READ = {"typeName": "gnpRead", "command": 0x13, "subcommand": 0x87}
GNP_WRITE = {"typeName": "gnpWrite", "command": 0x13, "subcommand": 0x87}


def catalogue(**entries) -> Catalogue:
    return Catalogue.from_json(entries)


def entry(value_type: dict, *, readable=True, writable=True) -> dict:
    return {
        "valueType": value_type,
        "read": [GNP_READ] if readable else [],
        "write": [GNP_WRITE] if writable else [],
    }


BOOLEAN = {"type": "boolean"}
ENUM = {"type": "string", "enum": ["off", "anc", "hearThrough"]}
RANGED = {"type": "integer", "minimum": 0, "maximum": 100}


# --------------------------------------------------------------------------- value types

@pytest.mark.parametrize(
    ("value_type", "kind"),
    [
        (BOOLEAN, Kind.TOGGLE),
        (ENUM, Kind.CHOICE),
        (RANGED, Kind.RANGE),
        ({"type": "string"}, Kind.TEXT),
        ({"type": "integer"}, Kind.TEXT),
        ({"type": "object"}, Kind.READOUT),
    ],
)
def test_the_declared_value_type_picks_the_widget(value_type, kind):
    built = C.build(catalogue(sidetone=entry(value_type)), supported=["sidetone"])
    assert built.by_key("setting.sidetone").kind is kind


def test_an_unbounded_integer_is_text_because_a_slider_would_invent_its_range():
    built = C.build(catalogue(volume=entry({"type": "integer"})), supported=["volume"])
    assert built.by_key("setting.volume").kind is Kind.TEXT


def test_a_property_that_cannot_be_written_is_a_readout():
    built = C.build(catalogue(pid=entry(BOOLEAN, writable=False)), supported=["pid"])
    row = built.by_key("setting.pid")
    assert row.kind is Kind.READOUT
    assert row.writable is False


# --------------------------------------------------------------------------- safety

DESTRUCTIVE = ["factoryReset", "firmwareUpdate", "dfuMode", "clearPairingList"]


@pytest.mark.parametrize("name", DESTRUCTIVE)
def test_destructive_properties_are_never_controls(name):
    """A generic setter would fire these from a stray click; they must not reach the page."""
    built = C.build(catalogue(**{name: entry(BOOLEAN)}), supported=[name])
    assert built.by_key(C.setting_key(name)) is None
    assert categories.is_dangerous(name)


def test_a_property_the_device_does_not_answer_is_absent_not_disabled():
    """Support comes from the live probe. An unsupported property gets no control at all."""
    built = C.build(catalogue(sidetone=entry(BOOLEAN), ancMode=entry(ENUM)), supported=["sidetone"])
    assert built.by_key("setting.sidetone") is not None
    assert built.by_key("setting.ancMode") is None


def test_a_write_only_property_says_its_change_cannot_be_confirmed():
    """13 catalogue properties are writable but unreadable; a write to one is 'sent', not 'set'."""
    built = C.build(catalogue(beep=entry(BOOLEAN, readable=False)), supported=["beep"])
    assert C.NOTE_UNVERIFIABLE in built.by_key("setting.beep").note


def test_a_readable_property_claims_no_such_caveat():
    built = C.build(catalogue(sidetone=entry(BOOLEAN)), supported=["sidetone"])
    assert built.by_key("setting.sidetone").note == ""


# --------------------------------------------------------------------------- shape

def test_choices_are_choice_objects_not_bare_strings():
    """The renderer reads ``.value``/``.label``; bare strings only fail once a widget is built."""
    built = C.build(catalogue(ancMode=entry(ENUM)), supported=["ancMode"])
    for choice in built.by_key("setting.ancMode").choices:
        assert hasattr(choice, "value") and hasattr(choice, "label")


def test_the_page_survives_being_built_into_real_widgets(qapp):
    """The bug this guards against was invisible until a form was actually constructed."""
    from hardware_ui.shell.form import build_forms

    built = C.build(
        catalogue(
            ancMode=entry(ENUM), sidetone=entry(BOOLEAN), level=entry(RANGED),
            radioPower=entry(ENUM),
        ),
        supported=["ancMode", "sidetone", "level"],
        identity={"name": "Evolve2 85", "serialNumber": "ABC123"},
        relay=(["radioPower"], "Jabra Link 390"),
        bands=["60 Hz", "250 Hz", "1 kHz", "4 kHz", "7.6 kHz"],
    )
    forms = build_forms(built, lambda *a: None, lambda *a: None)
    assert set(forms) == set(built.groups())
    # Every capability got a real row -- a choice built from bare strings dies here, not earlier.
    assert sum(len(f._rows) for f in forms.values()) == len(list(built))


def test_sections_come_from_the_property_name():
    built = C.build(
        catalogue(ancMode=entry(ENUM), sidetone=entry(BOOLEAN)),
        supported=["ancMode", "sidetone"],
    )
    groups = built.groups()
    assert "ANC & HearThrough" in groups
    assert "Microphone" in groups


def test_setting_keys_round_trip():
    assert C.property_name(C.setting_key("ancMode")) == "ancMode"
    assert C.property_name("info.serialNumber") == ""


def test_identity_rows_appear_in_order_and_skip_what_the_device_withheld():
    built = C.build(None, identity={"name": "Evolve2 85", "firmwareVersion": "1.2.3"})
    labels_shown = [c.label for c in built.groups()["Info"]]
    assert labels_shown == ["Model", "Firmware"]


def test_other_endpoints_on_the_link_are_reported_but_not_configurable():
    """The dongle's own settings have never been written on hardware, so they are information."""
    built = C.build(
        catalogue(sidetone=entry(BOOLEAN)),
        supported=["sidetone"],
        identity={"name": "Evolve2 85"},
        peers=["0x01 Jabra Link 390 (0x2470, fw 1.0, relay)"],
    )
    peer = built.by_key("info.peer.0")
    assert peer.kind is Kind.READOUT and peer.writable is False
    assert C.NOTE_ENDPOINT in built.by_key("info.name").note


def test_a_device_with_no_catalogue_still_gets_an_identity_page():
    """Without the vendor data the headset can be identified; that page must still build."""
    built = C.build(None, identity={"name": "Evolve2 85"})
    assert built.by_key("info.name") is not None


# --------------------------------------------------------------------------- labels

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ancMode", "ANC mode"),
        ("bluetoothPairing", "Bluetooth pairing"),
        ("sidetone", "Sidetone"),
    ],
)
def test_labels_are_generated_from_the_property_name(name, expected):
    """Jabra resolves display strings from its own service, so there is no table to unpack."""
    assert categories.label_for(name) == expected


def test_acronyms_survive_the_label_generator():
    assert categories.label_for("ancMode").startswith("ANC")
    assert "USB" in categories.label_for("usbConnection")


# --------------------------------------------------------------------------- equalizer

def test_bands_become_sliders_that_write_together():
    """One message carries the whole table, so a single-band write does not exist on the wire."""
    built = C.build(catalogue(), bands=["60 Hz", "250 Hz", "1 kHz"])
    keys = [f"{C.EQ_PREFIX}{i}" for i in range(3)]
    for index, key in enumerate(keys):
        row = built.by_key(key)
        assert row.kind is Kind.RANGE
        assert row.unit == "dB"
        assert (row.minimum, row.maximum, row.step) == (C.EQ_MIN_DB, C.EQ_MAX_DB, C.EQ_STEP_DB)
        # Every band carries the whole group, so the shell disables them as one.
        assert set(row.writes_with) == set(keys), f"band {index}"


def test_band_labels_come_from_the_device_not_a_table():
    """Band frequencies are read off the wire; a hardcoded list would be wrong on another model."""
    built = C.build(catalogue(), bands=["60 Hz", "7.6 kHz"])
    assert [built.by_key(f"{C.EQ_PREFIX}{i}").label for i in range(2)] == ["60 Hz", "7.6 kHz"]


def test_flat_is_an_action_beside_the_bands():
    built = C.build(catalogue(), bands=["60 Hz"])
    flat = built.by_key(C.EQ_FLAT_KEY)
    assert flat.kind is Kind.ACTION
    assert flat.group == C.GROUP_EQ


def test_no_equalizer_means_no_equalizer_section():
    built = C.build(catalogue(sidetone=entry(BOOLEAN)), supported=["sidetone"])
    assert C.GROUP_EQ not in built.groups()
    assert built.by_key(C.EQ_FLAT_KEY) is None


def test_band_index_reads_back_the_key():
    assert C.band_index(f"{C.EQ_PREFIX}3") == 3
    assert C.band_index("setting.ancMode") is None


def test_the_band_table_round_trips_through_the_write_layout():
    """A write must return each band's opaque A field unchanged, so encode/decode must agree."""
    from hardware_ui.modules.jabra_headsets.equalizer import Band, Equalizer

    original = Equalizer((Band(0, 180, 60), Band(0x1234, 22800, -120)))
    assert Equalizer.decode(original.encode()) == original
    # Changing one gain leaves every A and frequency alone.
    changed = original.with_gains_db([3.0, -1.0])
    assert [b.a for b in changed.bands] == [b.a for b in original.bands]
    assert [b.freq_field for b in changed.bands] == [b.freq_field for b in original.bands]
    assert [b.db for b in changed.bands] == [3.0, -1.0]


def test_flat_zeroes_every_gain_and_keeps_the_rest():
    from hardware_ui.modules.jabra_headsets.equalizer import Band, Equalizer

    original = Equalizer((Band(0x11, 180, 300), Band(0x22, 750, -300)))
    flat = original.flat()
    assert [b.db for b in flat.bands] == [0.0, 0.0]
    assert [b.a for b in flat.bands] == [0x11, 0x22]


# --------------------------------------------------------------------------- the adapter

def test_the_adapter_gets_its_own_section_and_keyspace():
    """Property names collide across endpoints; the answers do not. radioPower is the adapter's."""
    cat = catalogue(radioPower=entry(ENUM), sidetone=entry(BOOLEAN))
    built = C.build(cat, supported=["sidetone"], relay=(["radioPower"], "Jabra Link 390"))
    assert built.by_key("relay.radioPower") is not None
    assert built.by_key("setting.radioPower") is None
    assert "Adapter — Jabra Link 390" in built.groups()


def test_the_same_property_can_appear_at_both_endpoints_without_colliding():
    cat = catalogue(radioPower=entry(ENUM))
    built = C.build(cat, supported=["radioPower"], relay=(["radioPower"], "Link 390"))
    assert built.by_key("setting.radioPower") is not None
    assert built.by_key("relay.radioPower") is not None


def test_relay_keys_round_trip_to_the_property_name():
    assert C.property_name(C.relay_key("radioPower")) == "radioPower"
    assert C.is_relay_key(C.relay_key("radioPower"))
    assert not C.is_relay_key(C.setting_key("radioPower"))


def test_an_adapter_that_answers_nothing_gets_no_section():
    built = C.build(catalogue(sidetone=entry(BOOLEAN)), supported=["sidetone"],
                    relay=([], "Link 390"))
    assert not any(g.startswith("Adapter") for g in built.groups())


def test_the_adapter_section_says_which_box_it_configures():
    cat = catalogue(radioPower=entry(ENUM))
    built = C.build(cat, relay=(["radioPower"], "Link 390"))
    assert C.NOTE_RELAY in built.by_key("relay.radioPower").note


def test_destructive_properties_are_withheld_at_the_adapter_too():
    cat = catalogue(factoryReset=entry(BOOLEAN))
    built = C.build(cat, relay=(["factoryReset"], "Link 390"))
    assert built.by_key(C.relay_key("factoryReset")) is None


# --------------------------------------------------------------------------- vendor labels

def test_jabras_own_wording_wins_over_the_generated_label():
    """The generated fallback says "ANC mode"; Jabra's own apps say "Noise cancelling mode"."""
    assert labels.label("ancMode") == "Noise cancelling mode"
    assert categories.label_for("ancMode") == "ANC mode"


def test_a_property_outside_the_override_map_falls_back_to_the_generated_label():
    assert labels.label("someUnknownFutureProperty") == categories.label_for(
        "someUnknownFutureProperty"
    )


def test_vendor_wording_reaches_the_control():
    built = C.build(catalogue(ancMode=entry(ENUM)), supported=["ancMode"])
    assert built.by_key("setting.ancMode").label == "Noise cancelling mode"


def test_radio_power_is_named_what_the_vendor_calls_it():
    """"Radio power" is the protocol name; users are shown "Wireless range"."""
    assert labels.label("radioPower") == "Wireless range"


# --------------------------------------------------------------------------- battery & state

def test_battery_is_a_meter_when_the_device_reports_one():
    built = C.build(catalogue(), has_battery=True)
    row = built.by_key(C.BATTERY_KEY)
    assert row.kind is Kind.METER and row.unit == "%" and not row.writable


def test_no_battery_means_no_battery_row():
    assert C.build(catalogue(), has_battery=False).by_key(C.BATTERY_KEY) is None


def test_only_armed_state_gets_a_row():
    """An unarmed notification never fires, so its row would say "Not reported" forever."""
    built = C.build(catalogue(), states=["boomArmPosition"])
    assert built.by_key(C.state_key("boomArmPosition")) is not None
    assert built.by_key(C.state_key("onHeadDetectionStatus")) is None


def test_state_rows_are_readouts_not_settings():
    built = C.build(catalogue(), states=["onHeadDetectionStatus", "_mode"])
    for name in ("onHeadDetectionStatus", "_mode"):
        row = built.by_key(C.state_key(name))
        assert row.kind is Kind.READOUT and not row.writable


def test_state_captions_match_the_source_app():
    built = C.build(catalogue(), states=[n for n, _ in C.STATE_ROWS])
    shown = {built.by_key(C.state_key(n)).label for n, _ in C.STATE_ROWS}
    assert shown == {"On your head", "Boom arm", "Microphone", "Mode"}


# --------------------------------------------------------------------------- object values

OBJECT = {"type": "object"}


def test_a_dict_valued_property_becomes_one_row_per_field():
    """Found on hardware: `supportedEvents` as a single readout put its whole repr on one line,
    stretching the window off the edge of the screen and onto the next monitor."""
    cat = catalogue(supportedEvents=entry(OBJECT, writable=False))
    built = C.build(
        cat,
        supported=["supportedEvents"],
        values={"supportedEvents": {"audioReady": False, "configChange": True}},
    )
    assert built.by_key("setting.supportedEvents") is None
    rows = [built.by_key(C.field_key("setting.supportedEvents", f))
            for f in ("audioReady", "configChange")]
    assert all(r is not None and r.kind is Kind.READOUT and not r.writable for r in rows)
    assert rows[0].label == "Supported events — Audio ready"


def test_an_object_property_with_no_value_read_stays_a_single_row():
    """Rows come from the value's own keys, so with nothing read there is nothing to split."""
    cat = catalogue(supportedEvents=entry(OBJECT, writable=False))
    built = C.build(cat, supported=["supportedEvents"], values={})
    assert built.by_key("setting.supportedEvents") is not None


def test_scalar_properties_are_untouched_by_the_expansion():
    built = C.build(
        catalogue(sidetone=entry(BOOLEAN)), supported=["sidetone"], values={"sidetone": True}
    )
    assert built.by_key("setting.sidetone") is not None


def test_the_adapter_gets_the_same_treatment():
    cat = catalogue(supportedEvents=entry(OBJECT, writable=False))
    built = C.build(cat, relay=(["supportedEvents"], "Link 390"),
                    values={"supportedEvents": {"docking": True}})
    assert built.by_key(C.field_key("relay.supportedEvents", "docking")) is not None


def test_values_are_flattened_to_match_the_rows():
    """The rows and the values must agree, or every expanded row shows nothing."""
    from hardware_ui.modules.jabra_headsets.device import _flatten

    flat = _flatten({"supportedEvents": {"docking": True}, "sidetone": False}, C.setting_key)
    assert flat == {
        C.field_key("setting.supportedEvents", "docking"): True,
        "setting.sidetone": False,
    }


def test_an_empty_dict_is_not_split_into_nothing():
    """Splitting an empty dict would silently drop the property instead of showing it."""
    from hardware_ui.modules.jabra_headsets.device import _flatten

    assert _flatten({"versionExtended": {}}, C.setting_key) == {"setting.versionExtended": {}}


def test_flat_signals_that_every_band_moved(monkeypatch):
    """Flat moves all five bands; the shell repaints siblings only on a revision bump."""
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.jabra_headsets.device import JabraHeadsetDevice
    from hardware_ui.modules.jabra_headsets.equalizer import Band, Equalizer

    dev = JabraHeadsetDevice(
        DeviceInfo(uid="x", name="Jabra", transport=Transport.HID, category=Category.AUDIO)
    )
    original = Equalizer((Band(0, 180, 300), Band(0x22, 750, -300)))
    dev._equalizer = original

    class FakeJabra:
        """``write_equalizer_raw`` re-reads after writing, so its reply is in the *read* layout.

        Returning the decoded object sidesteps that asymmetry -- which the layouts' own round-trip
        tests already cover -- and keeps this test about the repaint signal.
        """

        def write_equalizer_raw(self, payload):
            return Equalizer.decode(payload)

    dev._jabra = FakeJabra()
    before = dev.capabilities_revision

    dev._write_equalizer(band=0, db=3.0)
    assert dev.capabilities_revision == before, "one band moved; no repaint needed"

    dev._write_equalizer(flat=True)
    assert dev.capabilities_revision != before, "flat moved every band; the page must repaint"
    assert dev._values["eq.band0"] == 0.0 and dev._values["eq.band1"] == 0.0


# --------------------------------------------------------------------------- label wiring
#
# These exist because porting labels.py into the tree is not the same as calling it. Every one of
# these was shipped broken once, with the correct implementation sitting unused a file away.

LANG = {"type": "string", "enum": ["zh_CN", "en_US", "de_DE"]}


def test_a_language_property_ignores_the_catalogue_enum():
    """`currentLanguageCode` declares 18 string identifiers and returns an LCID integer. Trusting
    the enum makes the combo fall back to item 0 and display zh_CN."""
    built = C.build(
        catalogue(currentLanguageCode=entry(LANG)),
        supported=["currentLanguageCode"],
        values={"currentLanguageCode": 1033, "availableLanguages": [1033]},
    )
    choices = built.by_key("setting.currentLanguageCode").choices
    assert [c.value for c in choices] == [1033]
    assert choices[0].label == "English (US) (1033)"
    assert not any(c.value == "zh_CN" for c in choices)


@pytest.mark.parametrize("name", ["currentLanguage", "currentLanguageCode",
                                  "currentLanguageInConfigMode"])
def test_every_language_property_is_named_not_numbered(name):
    built = C.build(catalogue(**{name: entry(LANG)}), supported=[name],
                    values={name: 1033, "availableLanguages": [1033]})
    assert built.by_key(C.setting_key(name)).choices[0].label == "English (US) (1033)"


def test_language_choices_come_from_the_device_not_the_full_table():
    """An Evolve2 85 reports exactly [1033]; offering 20 languages it cannot speak is a lie."""
    built = C.build(catalogue(currentLanguage=entry(LANG)), supported=["currentLanguage"],
                    values={"currentLanguage": 1033, "availableLanguages": [1033]})
    assert len(built.by_key("setting.currentLanguage").choices) == 1


def test_decibel_enums_are_rendered_as_decibels():
    """The real catalogue spells these minus6dB / _0dB / plus3dB."""
    for raw, shown in [("minus9dB", "−9 dB"), ("_0dB", "0 dB"), ("plus6dB", "+6 dB")]:
        assert labels.value_label("sidetoneLevelEnum", raw) == shown


def test_readouts_are_formatted_for_display():
    """`format_value` turns an LCID into a name and a bool into Yes/No. It is display-only: a
    formatted string written back to the device would not survive the trip."""
    assert labels.format_value("availableLanguages", [1033]) == "English (US) (1033)"
    assert labels.format_value("supportedEvents", True) == "Yes"


def test_an_integer_control_carries_its_unit():
    built = C.build(catalogue(muteReminderTiming=entry({"type": "integer"})),
                    supported=["muteReminderTiming"])
    # Stripped: the source's units are QSpinBox suffixes and carry a leading space, which this
    # shell adds itself -- keeping both renders "0  dB".
    assert built.by_key("setting.muteReminderTiming").unit == labels.unit(
        "muteReminderTiming"
    ).strip()


def test_units_carry_no_leading_space():
    built = C.build(catalogue(hearThroughLevel=entry({"type": "integer", "minimum": -12,
                                                      "maximum": 6})),
                    supported=["hearThroughLevel"])
    assert built.by_key("setting.hearThroughLevel").unit == "dB"


# --------------------------------------------------------------------------- refused values
#
# NACK 0xFA means the catalogue lists a value this hardware does not have. Measured on a Link 390
# + Evolve2 85: three of four wireless ranges work, three of six IntelliTone levels, two of four
# audio-detection modes, and hearThroughLevel declares −12..6 but stops at 0.

def _device_with(cap_kwargs, value=None):
    from hardware_ui.core import Capability, CapabilitySet
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.jabra_headsets.device import JabraHeadsetDevice

    dev = JabraHeadsetDevice(
        DeviceInfo(uid="x", name="Jabra", transport=Transport.HID, category=Category.AUDIO)
    )
    dev._capabilities = CapabilitySet([Capability(**cap_kwargs)])
    if value is not None:
        dev._values[cap_kwargs["key"]] = value
    return dev


def test_a_refused_option_is_removed_from_the_dropdown():
    from hardware_ui.core import Choice

    dev = _device_with({
        "key": "setting.radioPower", "kind": Kind.CHOICE, "label": "Wireless range",
        "choices": tuple(Choice(v, v) for v in ("normal", "low", "veryLow", "ultraLow")),
    })
    message = dev._reject("setting.radioPower", "radioPower", "ultraLow")
    left = [c.value for c in dev.capabilities.by_key("setting.radioPower").choices]
    assert left == ["normal", "low", "veryLow"]
    assert "does not support that option" in message


def test_removing_the_last_option_locks_the_control_instead():
    from hardware_ui.core import Choice

    dev = _device_with({"key": "setting.x", "kind": Kind.CHOICE, "label": "X",
                        "choices": (Choice("only", "Only"),)})
    dev._reject("setting.x", "x", "only")
    assert dev.advisories()["setting.x"].locked


class _BoundedDevice:
    """A device that accepts a range narrower than its catalogue declares.

    Measured on an Evolve2 85: ``hearThroughLevel`` declares −12..6 and takes −12..0.
    """

    def __init__(self, low=-12, high=0):
        self.low, self.high, self.value = low, high, 0
        self.writes: list[float] = []

    def set(self, name, value, **kw):
        self.writes.append(value)
        if not self.low <= value <= self.high:
            raise DeviceError(f"{name}: Illegal parameter")
        self.value = value
        return value

    def get(self, name, **kw):
        return self.value


def test_a_refused_high_value_finds_the_real_ceiling():
    """Not one step per click: stepping down from a refused +6 would make the user hit the wall
    six times before the slider told the truth."""
    from hardware_ui.core import DeviceError  # noqa: F401 - imported for _BoundedDevice

    dev = _device_with({"key": "setting.hearThroughLevel", "kind": Kind.RANGE,
                        "label": "HearThrough level", "minimum": -12.0, "maximum": 6.0,
                        "step": 1.0}, value=0)
    dev._jabra = _BoundedDevice(-12, 0)
    message = dev._reject("setting.hearThroughLevel", "hearThroughLevel", 6)
    cap = dev.capabilities.by_key("setting.hearThroughLevel")
    assert (cap.minimum, cap.maximum) == (-12.0, 0.0), "should land on the true ceiling"
    assert "no higher than 0" in message
    assert len(dev._jabra.writes) <= 6, "a search, not a sweep"


def test_a_refused_low_value_finds_the_real_floor():
    dev = _device_with({"key": "setting.level", "kind": Kind.RANGE, "label": "Level",
                        "minimum": -12.0, "maximum": 6.0, "step": 1.0}, value=0)
    dev._jabra = _BoundedDevice(-4, 6)
    dev._reject("setting.level", "level", -12)
    assert dev.capabilities.by_key("setting.level").minimum == -4.0


def test_the_probe_leaves_the_device_on_a_value_it_accepts():
    dev = _device_with({"key": "setting.hearThroughLevel", "kind": Kind.RANGE,
                        "label": "HearThrough level", "minimum": -12.0, "maximum": 6.0,
                        "step": 1.0}, value=0)
    fake = _BoundedDevice(-12, 0)
    dev._jabra = fake
    dev._reject("setting.hearThroughLevel", "hearThroughLevel", 6)
    assert fake.low <= fake.value <= fake.high, "must not be left holding a refused value"


def test_a_refused_toggle_is_locked_with_the_reason():
    """publicModeEnabled is a boolean the catalogue lists and the hardware refuses outright."""
    dev = _device_with({"key": "setting.publicModeEnabled", "kind": Kind.TOGGLE,
                        "label": "Public mode"})
    dev._reject("setting.publicModeEnabled", "publicModeEnabled", True)
    advisory = dev.advisories()["setting.publicModeEnabled"]
    assert advisory.locked and "rejects" in advisory.message


def test_narrowing_asks_the_shell_to_repaint():
    from hardware_ui.core import Choice

    dev = _device_with({"key": "setting.x", "kind": Kind.CHOICE, "label": "X",
                        "choices": (Choice("a", "A"), Choice("b", "B"))})
    before = dev.capabilities_revision
    dev._reject("setting.x", "x", "b")
    assert dev.capabilities_revision != before


# --------------------------------------------------------------------------- learned limits
#
# The catalogue over-promises and only a write finds out, so what a model refuses has to survive
# a reconnect. Kept in memory only, the slider offered +6 again every session and the same wall
# was hit every time -- which is what the source project did, and it reads as a broken app.

def test_learned_limits_are_applied_at_connect():
    from hardware_ui.core import Capability, CapabilitySet, Choice

    dev = _device_with({"key": "setting.hearThroughLevel", "kind": Kind.RANGE,
                        "label": "HearThrough level", "minimum": -12.0, "maximum": 6.0})
    dev._capabilities = CapabilitySet([
        Capability(key="setting.hearThroughLevel", kind=Kind.RANGE, label="HearThrough level",
                   minimum=-12.0, maximum=6.0),
        Capability(key="setting.intellitoneLevel", kind=Kind.CHOICE, label="Hearing protection",
                   choices=tuple(Choice(v, v) for v in
                                 ("peakStopOnly", "level1", "level3", "g616"))),
        Capability(key="setting.publicModeEnabled", kind=Kind.TOGGLE, label="Public mode"),
    ])
    dev._limits = {
        "bounds": {"setting.hearThroughLevel": [-12.0, 0.0]},
        "rejected": {"setting.intellitoneLevel": ["level1"]},
        "locked": ["setting.publicModeEnabled"],
    }
    dev._apply_limits()

    assert dev.capabilities.by_key("setting.hearThroughLevel").maximum == 0.0
    assert [c.value for c in dev.capabilities.by_key("setting.intellitoneLevel").choices] == [
        "peakStopOnly", "level3", "g616"
    ]
    assert dev.advisories()["setting.publicModeEnabled"].locked


def test_limits_are_keyed_per_endpoint_not_per_property():
    """The adapter answers many of the same property names and refuses different values."""
    from hardware_ui.core import Capability, CapabilitySet, Choice

    dev = _device_with({"key": "x", "kind": Kind.TOGGLE, "label": "x"})
    options = tuple(Choice(v, v) for v in ("normal", "low", "ultraLow"))
    dev._capabilities = CapabilitySet([
        Capability(key="setting.radioPower", kind=Kind.CHOICE, label="Wireless range",
                   choices=options),
        Capability(key="relay.radioPower", kind=Kind.CHOICE, label="Wireless range",
                   choices=options),
    ])
    dev._limits = {"rejected": {"relay.radioPower": ["ultraLow"]}, "bounds": {}, "locked": []}
    dev._apply_limits()

    assert len(dev.capabilities.by_key("setting.radioPower").choices) == 3, "headset untouched"
    assert len(dev.capabilities.by_key("relay.radioPower").choices) == 2, "adapter narrowed"


def test_the_limits_cache_round_trips(tmp_path, monkeypatch):
    from hardware_ui.modules.jabra_headsets import capability_cache

    monkeypatch.setattr(capability_cache, "cache_dir", lambda: tmp_path)
    limits = {"rejected": {"setting.x": ["a"]}, "bounds": {"setting.y": [-12.0, 0.0]},
              "locked": ["setting.z"]}
    capability_cache.save_limits(0x24BB, "1.5.7", limits)
    assert capability_cache.load_limits(0x24BB, "1.5.7") == limits
    # A different firmware is a different device as far as this is concerned.
    assert capability_cache.load_limits(0x24BB, "1.6.0")["rejected"] == {}


def test_a_stale_limits_version_is_ignored_rather_than_trusted(tmp_path, monkeypatch):
    import json

    from hardware_ui.modules.jabra_headsets import capability_cache

    monkeypatch.setattr(capability_cache, "cache_dir", lambda: tmp_path)
    path = tmp_path / "limits-24bb-1.5.7.json"
    path.write_text(json.dumps({"version": 0, "locked": ["setting.everything"]}))
    assert capability_cache.load_limits(0x24BB, "1.5.7")["locked"] == []
