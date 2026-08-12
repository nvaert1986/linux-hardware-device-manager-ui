"""The Logitech module's mapping layer: a Solaar setting -> a control.

The HID++ protocol and the setting definitions are Solaar's and are exercised by Solaar's own test
suite. What is new here is the translation into the shell's schema, the isolation of the config
file, and the discipline that keeps the vendored library from being imported twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hardware_ui.core import Kind
from hardware_ui.modules.logitech_peripherals import bootstrap, labels, store
from hardware_ui.modules.logitech_peripherals import capabilities as C

VENDOR = bootstrap.VENDOR

pytestmark = pytest.mark.skipif(
    not (VENDOR / "logitech_receiver").is_dir(),
    reason="Solaar not vendored — run tools/vendor_solaar.py",
)


@pytest.fixture(scope="module")
def solaar_kinds():
    bootstrap.ensure_path()
    from logitech_receiver.settings_validator import Kind as SolaarKind

    return SolaarKind


@pytest.fixture(scope="module")
def templates():
    bootstrap.ensure_path()
    import logitech_receiver.settings_templates as st

    return {cls.name: cls for cls in st.SETTINGS}


class FakeSetting:
    """A setting object shaped like Solaar's, built from a real template's metadata."""

    def __init__(self, template, kind, choices=(), rng=None, step=1):
        self.name = template.name
        self.label = template.label
        self.description = getattr(template, "description", "") or ""
        self.kind = kind
        self.choices = choices
        self.range = rng
        # Named ``_validator`` because that is what Solaar calls it. An earlier version of this
        # fake exposed ``validator``, which passed here and crashed on a real MX Master 3S.
        self._validator = type("V", (), {"step": step})()


# --------------------------------------------------------------------------- widget mapping

def test_each_supported_validator_kind_becomes_the_right_control(solaar_kinds, templates):
    made = [
        FakeSetting(templates["fn-swap"], solaar_kinds.TOGGLE),
        FakeSetting(templates["report_rate"], solaar_kinds.CHOICE, choices=(125, 500, 1000)),
        FakeSetting(templates["dpi"], solaar_kinds.RANGE, rng=(200, 4000)),
    ]
    page = C.build(made)
    assert page.by_key("setting.fn-swap").kind is Kind.TOGGLE
    assert page.by_key("setting.report_rate").kind is Kind.CHOICE
    assert page.by_key("setting.dpi").kind is Kind.RANGE


def test_a_packed_range_is_still_a_slider(solaar_kinds, templates):
    made = [FakeSetting(templates["dpi"], solaar_kinds.PACKED_RANGE, rng=(200, 4000))]
    assert C.build(made).by_key("setting.dpi").kind is Kind.RANGE


@pytest.mark.parametrize("kind_name", ["MAP_CHOICE", "MULTIPLE_TOGGLE", "MULTIPLE_RANGE", "HETERO"])
def test_per_key_settings_are_withheld_rather_than_flattened(kind_name, solaar_kinds, templates):
    """These describe a value per key. A single row cannot express one honestly, and pretending
    otherwise would write the wrong thing to the device."""
    kind = getattr(solaar_kinds, kind_name)
    page = C.build([FakeSetting(templates["divert-keys"], kind)])
    assert page.by_key("setting.divert-keys") is None


def test_a_choice_with_no_options_is_not_offered(solaar_kinds, templates):
    page = C.build([FakeSetting(templates["report_rate"], solaar_kinds.CHOICE, choices=())])
    assert page.by_key("setting.report_rate") is None


def test_a_range_with_no_bounds_is_not_offered(solaar_kinds, templates):
    page = C.build([FakeSetting(templates["dpi"], solaar_kinds.RANGE, rng=None)])
    assert page.by_key("setting.dpi") is None


def test_choices_carry_the_int_the_device_wants(solaar_kinds, templates):
    """Solaar's choices are NamedInt -- an int that prints as a name. Carrying the NamedInt through
    would make equality against a plain int fail on the way back in."""
    bootstrap.ensure_path()
    from logitech_receiver.common import NamedInt

    named = (NamedInt(125, "125Hz"), NamedInt(1000, "1000Hz"))
    page = C.build([FakeSetting(templates["report_rate"], solaar_kinds.CHOICE, choices=named)])
    values = [c.value for c in page.by_key("setting.report_rate").choices]
    assert values == [125, 1000]
    assert all(type(v) is int for v in values)
    assert [c.label for c in page.by_key("setting.report_rate").choices] == ["125Hz", "1000Hz"]


# --------------------------------------------------------------------------- wording & grouping

def test_solaars_own_label_is_used_not_a_generated_one(solaar_kinds, templates):
    """Upstream's labels are written for users and translated; regenerating them would be worse."""
    page = C.build([FakeSetting(templates["dpi"], solaar_kinds.RANGE, rng=(200, 4000))])
    assert page.by_key("setting.dpi").label == "Sensitivity (DPI)"


def test_a_setting_without_a_label_still_gets_a_readable_one():
    assert labels.generated_label("hires-smooth-invert") == "Hires smooth invert"
    assert labels.generated_label("dpi_extended").startswith("DPI")


@pytest.mark.parametrize(
    ("name", "group"),
    [
        ("dpi", "Pointer"),
        ("report_rate", "Pointer"),
        ("hi-res-scroll", "Scrolling"),
        ("smart-shift", "Scrolling"),
        ("fn-swap", "Keys & Buttons"),
        ("backlight_level", "Lighting"),
        ("sidetone", "Sound & Haptics"),
        ("adc_power_management", "Power"),
        ("onboard_profiles", "Device"),
    ],
)
def test_settings_land_in_a_sensible_section(name, group):
    assert labels.group_for(name) == group


def test_an_unrecognised_setting_still_appears(solaar_kinds, templates):
    """A setting added by a future Solaar must land somewhere, not vanish."""
    template = type("T", (), {"name": "some_future_setting", "label": "Future", "description": ""})
    page = C.build([FakeSetting(template, solaar_kinds.TOGGLE)])
    assert page.by_key("setting.some_future_setting") is not None
    assert labels.group_for("some_future_setting") == "Other"


# --------------------------------------------------------------------------- receiver & pairing

def receiver_page(used=2, total=6, remaining=3):
    devices = {n: f"device {n}" for n in range(1, used + 1)}
    return C.build(
        [],
        identity={"name": "Unifying Receiver", "kind": "Receiver"},
        pairing={"used": used, "total": total, "remaining": remaining,
                 "devices": devices, "display": {}},
    )


def test_a_receiver_offers_pairing_when_a_slot_is_free():
    assert receiver_page(used=2, total=6).by_key(C.PAIR_KEY) is not None


def test_a_full_receiver_does_not_offer_pairing():
    """Offering a button that cannot succeed is worse than not offering it."""
    assert receiver_page(used=6, total=6).by_key(C.PAIR_KEY) is None


def test_every_paired_device_can_be_unpaired_and_asks_first():
    page = receiver_page(used=2)
    for index in (1, 2):
        row = page.by_key(C.unpair_key(index))
        assert row.kind is Kind.ACTION
        assert row.confirm, "unpairing the keyboard you are typing on must ask"
        assert "another way to control this machine" in row.confirm_detail


def test_unpair_keys_round_trip():
    assert C.unpair_index(C.unpair_key(3)) == 3
    assert C.unpair_index("setting.dpi") is None


def test_a_receiver_that_reports_no_pairing_limit_shows_no_limit_row():
    page = C.build([], pairing={"used": 1, "total": 6, "remaining": None,
                                "devices": {1: "x"}, "display": {}})
    assert page.by_key("info.remaining_pairings") is None


def test_a_peripheral_has_no_pairing_section(solaar_kinds, templates):
    page = C.build([FakeSetting(templates["dpi"], solaar_kinds.RANGE, rng=(200, 4000))])
    assert C.GROUP_PAIRING not in page.groups()


def test_an_offline_device_says_so():
    page = C.build([], identity={"name": "MX Master 3"}, online=False)
    assert "not currently reachable" in page.by_key("info.name").note


# --------------------------------------------------------------------------- config isolation

def test_settings_are_not_written_into_solaars_config():
    """Another application's config file is not ours to write, and there is no locking between
    two processes that both think they own it."""
    from hardware_ui.core import paths

    target = store.config_path()
    assert target.name == "logitech.yaml"
    assert "solaar" not in str(target)
    # Under this application's own config directory, wherever the suite has pointed that.
    assert target.parent == paths.config_dir()


def test_the_redirect_actually_moves_the_librarys_path():
    store.redirect()
    from solaar import configuration

    assert configuration._yaml_file_path == str(store.config_path())
    # The legacy JSON path too: upstream falls back to it, so leaving it aimed at Solaar's
    # directory would quietly pick up a stray config.json there.
    assert "solaar" not in configuration._json_file_path


# --------------------------------------------------------------------------- import discipline

def test_the_vendored_copy_is_what_gets_imported():
    bootstrap.ensure_path()
    import logitech_receiver

    assert Path(logitech_receiver.__file__).is_relative_to(VENDOR), (
        "a system-installed Solaar was imported instead of the patched vendored copy"
    )
    assert bootstrap.loaded_from_vendor()


def test_the_vendor_directory_sits_first_on_the_path():
    """If a real Solaar is installed, which copy wins must not depend on import order luck."""
    bootstrap.ensure_path()
    assert sys.path[0] == str(VENDOR)
    assert sys.path.count(str(VENDOR)) == 1, "ensure_path must not stack duplicates"


def test_named_ints_are_flattened_for_the_shell():
    from hardware_ui.modules.logitech_peripherals.device import _plain

    bootstrap.ensure_path()
    from logitech_receiver.common import NamedInt

    flattened = _plain(NamedInt(500, "500Hz"))
    assert flattened == 500 and type(flattened) is int
    assert _plain(True) is True
    assert _plain([NamedInt(1, "one")]) == [1]


# --------------------------------------------------------------------------- expander cost
#
# Enumerating a receiver's slots is 2.13s on a Bolt receiver -- the library asks all six and the
# four empty ones time out -- while every discovery transport combined is 0.02s. That delay was the
# whole of what a user saw on rescan, so it is cached and the cache key is checked, not assumed.

def test_children_are_remembered_between_scans():
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.logitech_peripherals import children

    walks = 0

    class FakePaired:
        def __init__(self, number, name, kind):
            self.number, self.name, self.kind, self.serial = number, name, kind, f"S{number}"

    class FakeReceiver:
        path = "/dev/hidraw3"
        name = "Bolt Receiver"
        serial = "ABC123"
        receiver_kind = "bolt"

        def count(self):
            return 2

        def __iter__(self):
            nonlocal walks
            walks += 1
            return iter([FakePaired(1, "MX Keys S", "keyboard"),
                         FakePaired(2, "MX Master 3S", "mouse")])

    parent = DeviceInfo(uid="hid:3-3", name="Receiver", transport=Transport.HID,
                        category=Category.INPUT, vendor_id=0x046D, path="/dev/hidraw3")
    children.forget()
    first = children._children_of(FakeReceiver(), parent, "/dev/hidraw3")
    second = children._children_of(FakeReceiver(), parent, "/dev/hidraw3")

    assert len(first) == len(second) == 2
    assert walks == 1, "the second scan must not walk the slots again"
    assert [c.uid for c in first] == [c.uid for c in second]
    children.forget()


def test_pairing_something_invalidates_the_memo():
    """count() costs 4 ms and moves the moment a device is paired or unpaired, which is why it is
    the key rather than a timestamp."""
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.logitech_peripherals import children

    class FakePaired:
        def __init__(self, number):
            self.number, self.name, self.kind, self.serial = number, f"D{number}", "mouse", "S"

    class FakeReceiver:
        path = "/dev/hidraw3"
        name = "R"
        serial = "XYZ"
        receiver_kind = "bolt"

        def __init__(self, n):
            self._n = n

        def count(self):
            return self._n

        def __iter__(self):
            return iter([FakePaired(i) for i in range(1, self._n + 1)])

    parent = DeviceInfo(uid="u", name="R", transport=Transport.HID, category=Category.INPUT,
                        vendor_id=0x046D, path="/dev/hidraw3")
    children.forget()
    assert len(children._children_of(FakeReceiver(1), parent, "/dev/hidraw3")) == 1
    assert len(children._children_of(FakeReceiver(2), parent, "/dev/hidraw3")) == 2
    children.forget()


def test_a_slot_the_kernel_exposes_is_left_to_enumeration(monkeypatch):
    """Your rule: kernel first, per slot. Otherwise a dj-bound receiver shows the same mouse twice
    under two uids the registry cannot dedupe."""
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.logitech_peripherals import children

    bootstrap.ensure_path()
    import hidapi.udev_impl

    monkeypatch.setattr(
        hidapi.udev_impl, "find_paired_node",
        lambda path, index, timeout: "/dev/hidraw7" if index == 1 else None,
    )

    class FakePaired:
        def __init__(self, number, name):
            self.number, self.name, self.kind, self.serial = number, name, "mouse", "S"

    class FakeReceiver:
        path, name, serial, receiver_kind = "/dev/hidraw3", "R", "KKK", "unifying"

        def count(self):
            return 2

        def __iter__(self):
            return iter([FakePaired(1, "Has a node"), FakePaired(2, "Has none")])

    parent = DeviceInfo(uid="u", name="R", transport=Transport.HID, category=Category.INPUT,
                        vendor_id=0x046D, path="/dev/hidraw3")
    children.forget()
    found = children._children_of(FakeReceiver(), parent, "/dev/hidraw3")
    assert [c.name for c in found] == ["Has none"]
    children.forget()


# --------------------------------------------------------------------------- per-key maps

def _map_setting(name, label, keys, options):
    """Keys are real ``NamedInt``s, because that is what the library hands over.

    An earlier version of this fake used plain strings; the code does ``int(key)`` on them, since
    the integer is the wire value. The fake passed and the real device raised.
    """
    bootstrap.ensure_path()
    from logitech_receiver.common import NamedInt

    named = [NamedInt(0x50 + i, k) for i, k in enumerate(keys)]
    class FakeMap:
        def __init__(self):
            self.name, self.label, self.description = name, label, ""
            self.kind = type("K", (), {"name": "MAP_CHOICE"})()
            self.choices = {k: options for k in named}
    return FakeMap()


def test_a_small_map_becomes_one_row_per_key():
    page = C.build([_map_setting("reprogrammable-keys", "Key/Button Actions",
                                 ["Left Button", "Middle Button"], ["Left Click", "Right Click"])])
    rows = [c for c in page if c.key.startswith("setting.reprogrammable-keys#")]
    assert [r.label for r in rows] == ["Left Button", "Middle Button"]
    assert all(r.section == "Key/Button Actions" for r in rows)
    assert all(r.writable for r in rows)


def test_a_large_map_is_withheld_rather_than_rendered_as_a_wall():
    """An MX KEYS S diverts 17 keys and per-key-lighting can reach 117. Page length is a real
    problem here -- this window has been pushed off a screen by it once already."""
    keys = [f"Key {i}" for i in range(C.MAP_MAX_KEYS + 1)]
    page = C.build([_map_setting("reprogrammable-keys", "Actions", keys, ["a", "b"])])
    assert not [c for c in page if "reprogrammable-keys" in c.key]


def test_diversion_is_shown_but_never_writable():
    """Three of its four values leave a physical button doing nothing, because the software that
    would consume the diverted event is a rule engine this application does not ship."""
    page = C.build([_map_setting("divert-keys", "Key/Button Diversion",
                                 ["Middle Button", "Back Button"],
                                 ["Regular", "Diverted", "Mouse Gestures", "Sliding DPI"])])
    rows = [c for c in page if c.key.startswith("setting.divert-keys#")]
    assert rows and not any(r.writable for r in rows)


def test_every_diversion_row_carries_the_note_not_just_the_first():
    """A reader who scrolls to the fourth button should not have to scroll back to find out why
    it cannot be changed."""
    page = C.build([_map_setting("divert-keys", "Diversion",
                                 ["A", "B", "C"], ["Regular", "Diverted"])])
    rows = [c for c in page if c.key.startswith("setting.divert-keys#")]
    assert all("Solaar" in r.note for r in rows)
    assert all("does not change it" in r.note for r in rows)


def test_remapping_stays_editable_alongside_read_only_diversion():
    page = C.build([
        _map_setting("reprogrammable-keys", "Actions", ["Middle Button"], ["Left Click"]),
        _map_setting("divert-keys", "Diversion", ["Middle Button"], ["Regular", "Diverted"]),
    ])
    editable = [c for c in page if "reprogrammable-keys" in c.key]
    frozen = [c for c in page if "divert-keys" in c.key]
    assert editable and all(c.writable for c in editable)
    assert frozen and not any(c.writable for c in frozen)


def test_map_keys_round_trip():
    assert C.map_entry(C.map_key("reprogrammable-keys", 195)) == ("reprogrammable-keys", 195)
    assert C.map_entry("setting.dpi") is None


def test_an_unnamed_peripheral_stays_untested_behind_a_verified_receiver():
    """A receiver being verified says nothing about a peripheral nobody has tried. The inheritance
    this replaced was worse in both directions: `child.support or parent.support` never inherited
    at all, because Support.FAMILY is truthy."""
    from hardware_ui.core.device import Category, DeviceInfo, Support, Transport
    from hardware_ui.core.modules import ModuleRegistry

    registry = ModuleRegistry.discover()

    def claim(name: str) -> Support:
        return registry.claim(
            DeviceInfo(uid=f"hid:logitech:X:{name}", name=name, transport=Transport.HID,
                       category=Category.INPUT, vendor_id=0x046D)
        ).support

    assert claim("MX Master 3S") is Support.VERIFIED
    assert claim("MX Keys S") is Support.VERIFIED
    assert claim("MX Anywhere 4") is Support.FAMILY, (
        "a device nobody has tested must not inherit a verified badge from its receiver"
    )
