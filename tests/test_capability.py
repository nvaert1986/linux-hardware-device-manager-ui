"""Schema and matching tests.

These cover the two things every module depends on and that are expensive to change later: how a
capability's gate resolves, and how a manifest claims a device. Written after the Sony port
because that is what showed truthiness-based gating to be wrong.
"""

from __future__ import annotations

import pytest

from hardware_ui.core import (
    Capability,
    CapabilitySet,
    Category,
    Choice,
    DeviceInfo,
    Enablement,
    Kind,
    MatchRule,
    ModuleManifest,
    ModuleRegistry,
    Support,
    Tier,
    Transport,
)
from hardware_ui.core.capability import gate_satisfied


def cap(key: str, **kw) -> Capability:
    kw.setdefault("kind", Kind.TOGGLE)
    kw.setdefault("label", key)
    return Capability(key=key, **kw)


# --------------------------------------------------------------------------- validation


def test_choice_without_choices_is_rejected():
    with pytest.raises(ValueError, match="CHOICE requires choices"):
        Capability(key="x", kind=Kind.CHOICE, label="x")


def test_range_with_inverted_bounds_is_rejected():
    with pytest.raises(ValueError, match="minimum < maximum"):
        Capability(key="x", kind=Kind.RANGE, label="x", minimum=10, maximum=1)


# --------------------------------------------------------------------------- gating


def test_ungated_capability_is_always_satisfied():
    assert gate_satisfied(cap("a"), {}.get)


def test_default_gate_is_truthiness():
    c = cap("child", requires="parent")
    assert gate_satisfied(c, {"parent": True}.get)
    assert not gate_satisfied(c, {"parent": False}.get)
    assert not gate_satisfied(c, {}.get)


def test_value_specific_gate_distinguishes_truthy_values():
    """The Sony case: ambient level applies in "ambient" mode, not in the equally truthy "anc"."""
    c = cap("anc.ambient_level", requires="anc.mode", requires_value="ambient")
    assert gate_satisfied(c, {"anc.mode": "ambient"}.get)
    assert not gate_satisfied(c, {"anc.mode": "anc"}.get)
    assert not gate_satisfied(c, {"anc.mode": "off"}.get)


def test_tuple_gate_accepts_any_listed_value():
    c = cap("x", requires="mode", requires_value=("a", "b"))
    assert gate_satisfied(c, {"mode": "a"}.get)
    assert gate_satisfied(c, {"mode": "b"}.get)
    assert not gate_satisfied(c, {"mode": "c"}.get)


def test_none_is_a_real_value_not_unset():
    """A device may legitimately report None; it must not be confused with "no gate set"."""
    c = cap("x", requires="parent", requires_value=None)
    assert gate_satisfied(c, {"parent": None}.get)
    assert not gate_satisfied(c, {"parent": 1}.get)


# --------------------------------------------------------------------------- set operations


def test_search_spans_label_key_group_and_description():
    s = CapabilitySet(
        [
            cap("anc.mode", label="Noise control", group="Noise Control"),
            cap("eq.preset", label="Equaliser", group="Sound", kind=Kind.CHOICE,
                choices=(Choice(1, "One"),)),
            cap("sound.dsee", label="Upscaling", group="Sound", description="restores detail"),
        ]
    )
    assert {c.key for c in s.search("noise")} == {"anc.mode"}
    assert {c.key for c in s.search("sound")} == {"eq.preset", "sound.dsee"}
    assert {c.key for c in s.search("restores")} == {"sound.dsee"}
    assert len(s.search("  ")) == 3


def test_tier_filter_and_grouping():
    s = CapabilitySet(
        [cap("a", tier=Tier.COMMON, group="G1"), cap("b", group="G1"), cap("c", group="G2")]
    )
    assert [c.key for c in s.tier(Tier.COMMON)] == ["a"]
    assert list(s.groups()) == ["G1", "G2"]
    assert len(s.groups()["G1"]) == 2


# --------------------------------------------------------------------------- matching


def sony_manifest(tmp_path):
    (tmp_path / "sony").mkdir()
    p = tmp_path / "sony" / "module.toml"
    p.write_text(
        """
id = "sony"
name = "Sony Headsets"
category = "audio"
implementation = "does.not.exist:Nope"

[[match]]
transport = "bluetooth"
uuid = "96cc203e-5068-46ad-b32d-e316f5e069ba"
status = "family"

[[match]]
transport = "bluetooth"
name_glob = "WH-1000XM4"
status = "verified"
"""
    )
    return ModuleManifest.from_toml(p)


def bt(name: str, uuids: frozenset[str] = frozenset()) -> DeviceInfo:
    return DeviceInfo(uid=f"bt:{name}", name=name, transport=Transport.BLUETOOTH, uuids=uuids)


SONY_UUID = frozenset({"96CC203E-5068-46AD-B32D-E316F5E069BA"})


def test_verified_match_wins_over_family(tmp_path):
    reg = ModuleRegistry([sony_manifest(tmp_path)])
    claimed = reg.claim(bt("WH-1000XM4", SONY_UUID))
    assert claimed.module_id == "sony"
    assert claimed.support is Support.VERIFIED


def test_uuid_alone_claims_an_unknown_model(tmp_path):
    """The property that makes an untested future model work without a manifest change."""
    reg = ModuleRegistry([sony_manifest(tmp_path)])
    claimed = reg.claim(bt("Some Unreleased Sony", SONY_UUID))
    assert claimed.module_id == "sony"
    assert claimed.support is Support.FAMILY


def test_unrelated_device_is_left_unclaimed(tmp_path):
    reg = ModuleRegistry([sony_manifest(tmp_path)])
    assert reg.claim(bt("Some Speaker")).module_id == ""


def test_empty_rule_never_matches():
    """A rule with no criteria must not claim the world."""
    assert not MatchRule().matches(bt("anything"))


def test_matching_imports_no_module_code(tmp_path):
    """The startup guarantee: claiming a device must not import the implementation."""
    import sys

    reg = ModuleRegistry([sony_manifest(tmp_path)])
    before = set(sys.modules)
    reg.claim(bt("WH-1000XM4", SONY_UUID))
    assert not {m for m in set(sys.modules) - before if "does.not.exist" in m}


# --------------------------------------------------------------------------- write path
#
# These cover the plumbing, not the schema: a defect here shows up as a control that stays
# greyed, silently reverts, or is written from stale state -- all of which reached the user
# before the tests existed.


@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the widget tests."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def form_with(*caps: Capability):
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet(list(caps)))
    return form


def test_ui_marshal_forwards_keyword_arguments():
    """`_ui` must pass **kwargs through -- set_value(confirmed=True) depends on it.

    Dropping them turned every successful write into a TypeError the task reporter swallowed, so
    the UI reported failure for a change the device had applied.
    """
    import inspect

    from hardware_ui.shell.app import Controller

    kinds = {p.kind for p in inspect.signature(Controller._ui).parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD in kinds, "_ui drops **kwargs"
    assert inspect.Parameter.VAR_POSITIONAL in kinds, "_ui drops *args"


def test_write_lifecycle(qapp):
    """Pending disables, blocks stale updates, and is always released."""
    form = form_with(cap("a"), cap("b"))
    form.set_value("a", False, confirmed=True)

    form.set_pending(["a"], "a", True)
    assert not form._rows["a"].control.isEnabled(), "a pending control must be disabled"

    form.set_value("a", False)  # a refresh captured before the write landed
    assert form.value_of("a") is True, "stale update overwrote a pending value"

    form.set_value("a", True, confirmed=True)
    form.clear_pending(["a"])
    assert form._rows["a"].control.isEnabled()
    assert form.value_of("a") is True


def test_rejection_is_not_permanent(qapp):
    """One NotSupported must not disable a control for the rest of the session."""
    form = form_with(cap("a"))
    form.mark_failed("a")
    assert not form._rows["a"].control.isEnabled()

    form.set_value("a", True, confirmed=True)  # a successful read proves it works
    assert form._rows["a"].control.isEnabled()


def test_gate_dependents_react_when_the_gate_changes(qapp):
    """Enabling a gate must re-enable the controls that depend on it."""
    form = form_with(cap("stc"), cap("stc.sens", requires="stc"))
    form.set_value("stc", False, confirmed=True)
    assert not form._rows["stc.sens"].control.isEnabled()

    form.set_value("stc", True, confirmed=True)
    assert form._rows["stc.sens"].control.isEnabled(), "dependent stayed disabled"

    form.set_value("stc", False, confirmed=True)
    assert not form._rows["stc.sens"].control.isEnabled()


def test_composite_write_holds_all_its_siblings(qapp):
    """Sony sends enable, sensitivity and timeout in one set_stc.

    Leaving the enable toggle live while sensitivity was written let it be re-sent from state
    captured mid-sequence, switching speak-to-chat off while only sensitivity was being changed.
    """
    group = ("stc.on", "stc.sens", "stc.time")
    form = form_with(*(cap(k, writes_with=group) for k in group))
    for k in group:
        form.set_value(k, True, confirmed=True)

    form.set_pending(list(group), "stc.sens", False)
    assert all(not form._rows[k].control.isEnabled() for k in group), "siblings stayed editable"

    form.clear_pending(list(group))
    assert all(form._rows[k].control.isEnabled() for k in group), "siblings were not released"


def test_ungrouped_capability_only_holds_itself(qapp):
    form = form_with(cap("a"), cap("b"))
    form.set_value("a", True, confirmed=True)
    form.set_value("b", True, confirmed=True)
    form.set_pending(["a"], "a", False)
    assert not form._rows["a"].control.isEnabled()
    assert form._rows["b"].control.isEnabled(), "an unrelated capability must not be held"


def test_advisory_locks_a_control_and_shows_why(qapp):
    """The LDAC case: the message is the useful part, not the lock.

    Notes are per-tab, as in the reference implementation's EqualizerPanel._note -- a
    state-dependent message belongs to the panel, not to one row.
    """
    from hardware_ui.core import Advisory

    form = form_with(cap("eq.preset", note="Pick a Custom slot to edit the bands."))
    form.set_value("eq.preset", 1, confirmed=True)
    assert form._rows["eq.preset"].control.isEnabled()
    assert "Custom slot" in form._note.text(), "the static note should show by default"

    form.set_advisories({"eq.preset": Advisory(message="LDAC is active", locked=True)})
    assert not form._rows["eq.preset"].control.isEnabled()
    assert "LDAC" in form._note.text(), "an advisory must win over the static note"

    form.set_advisories({})
    assert form._rows["eq.preset"].control.isEnabled()
    assert "Custom slot" in form._note.text(), "the static note should come back"


def test_description_becomes_a_tooltip(qapp):
    """Static help does not occupy the form; a wrapped label there breaks row heights."""
    form = form_with(cap("info.codec", kind=Kind.READOUT, writable=False,
                         description="LDAC, AAC or SBC, as negotiated with this host."))
    assert "negotiated" in form._rows["info.codec"].control.toolTip()


def test_readonly_capability_is_not_rendered_as_disabled(qapp):
    """A readout was never interactive; dimming it made Info unreadable."""
    form = form_with(cap("info.model", kind=Kind.READOUT, writable=False))
    assert form._rows["info.model"].label.isEnabled(), "read-only label must stay legible"


def test_rebuilding_restores_the_page(qapp):
    """Navigating away and back must not leave a connected device showing an empty page."""
    live = CapabilitySet([cap("a", group="G"), cap("b", group="G")])
    form = form_with(*live)
    assert len(form.keys()) == 2

    form.build(CapabilitySet())
    assert form.keys() == []

    form.build(live)
    assert len(form.keys()) == 2, "the page was not restored"


# --------------------------------------------------------------------------- property matching


def display(name: str, vendor: str = "DEL", **props) -> DeviceInfo:
    return DeviceInfo(
        uid=f"drm:{name}",
        name=name,
        transport=Transport.DISPLAY,
        properties={"edid_vendor": vendor, **props},
    )


def test_a_property_rule_claims_by_edid_vendor_not_by_name():
    """The vendor id is three bytes the panel cannot be renamed out of, and some models publish
    no name descriptor at all -- so it, not a name glob, is what claims a family."""
    rule = MatchRule(transport=Transport.DISPLAY, properties=(("edid_vendor", "DEL"),))
    assert rule.matches(display("DELL P2425D"))
    assert rule.matches(display(""))  # no name descriptor: still ours
    assert not rule.matches(display("Bencq GW2480", vendor="BNQ"))


def test_property_matching_is_case_insensitive_and_stringly_typed():
    rule = MatchRule(properties=(("edid_vendor", "del"),))
    assert rule.matches(display("x"))
    assert MatchRule(properties=(("edid_product", "53659"),)).matches(
        display("x", edid_product=53659)
    )


def test_an_absent_property_does_not_match():
    assert not MatchRule(properties=(("edid_vendor", "DEL"),)).matches(
        DeviceInfo(uid="u", name="n", transport=Transport.DISPLAY)
    )


def test_a_rule_with_only_properties_still_counts_as_a_rule():
    """An all-default MatchRule must never claim everything; adding a field has to opt in here."""
    assert not MatchRule().matches(display("DELL P2425D"))
    assert MatchRule(properties=(("edid_vendor", "DEL"),)).matches(display("DELL P2425D"))


def test_a_verified_name_glob_beats_the_family_property_rule():
    manifest = ModuleManifest(
        id="dell_monitors",
        name="Dell",
        category=Category.DISPLAY,
        implementation="x:Y",
        match=(
            MatchRule(transport=Transport.DISPLAY, properties=(("edid_vendor", "DEL"),)),
            MatchRule(
                transport=Transport.DISPLAY, name_glob="DELL P2425D", support=Support.VERIFIED
            ),
        ),
    )
    assert manifest.match_for(display("DELL P2425D")).support is Support.VERIFIED
    assert manifest.match_for(display("DELL U4025QW")).support is Support.FAMILY


# --------------------------------------------------------------------------- new schema fields


def test_a_capability_can_declare_its_own_write_timeout():
    """A range calibration writes and reads back thirty values; a timeout sized for an RFCOMM
    round-trip would cancel it halfway through, mid-probe."""
    assert cap("x", kind=Kind.ACTION, timeout=240.0).timeout == 240.0
    assert cap("y").timeout == 0.0  # 0 means "use the shell's default"


def test_confirm_is_distinct_from_reboots():
    """``reboots`` additionally means the write cannot be confirmed and the shell must reconnect.
    A monitor input switch is disruptive but does neither."""
    c = cap("input", kind=Kind.CHOICE, choices=(Choice(1, "DP"),), confirm=True)
    assert c.confirm and not c.reboots


# --------------------------------------------------------------------------- module specialisation


def manifest(module_id, *, extends="", rules=(), impl="x:Y"):
    return ModuleManifest(
        id=module_id, name=module_id, category=Category.OTHER, implementation=impl,
        extends=extends, match=tuple(rules),
    )


def hid(name="Yubico YubiKey", vid=0x1050, pid=0x0407, **props):
    return DeviceInfo(
        uid=f"hid:{name}", name=name, transport=Transport.HID, vendor_id=vid, product_id=pid,
        properties={"hid_usage_page": "f1d0", **props},
    )


FIDO = MatchRule(transport=Transport.HID, properties=(("hid_usage_page", "f1d0"),))
YUBI = MatchRule(transport=Transport.HID, vendor_id=0x1050, support=Support.VERIFIED)


def test_the_most_specialised_module_claims_the_device_once():
    """A YubiKey is a CTAP authenticator, so both modules match it. Without a rule for which wins,
    the answer would be whichever manifest happened to be read first."""
    registry = ModuleRegistry([
        manifest("fido2_security_keys", rules=[FIDO]),
        manifest("yubikeys", extends="fido2_security_keys", rules=[YUBI]),
    ])
    claimed = registry.claim(hid())
    assert claimed.module_id == "yubikeys"
    assert claimed.support is Support.VERIFIED


def test_the_order_manifests_are_discovered_in_does_not_decide():
    a = ModuleRegistry([
        manifest("fido2_security_keys", rules=[FIDO]),
        manifest("yubikeys", extends="fido2_security_keys", rules=[YUBI]),
    ])
    b = ModuleRegistry([
        manifest("yubikeys", extends="fido2_security_keys", rules=[YUBI]),
        manifest("fido2_security_keys", rules=[FIDO]),
    ])
    assert a.claim(hid()).module_id == b.claim(hid()).module_id == "yubikeys"


def test_a_key_with_no_specialised_module_falls_back_to_the_base():
    registry = ModuleRegistry([
        manifest("fido2_security_keys", rules=[FIDO]),
        manifest("yubikeys", extends="fido2_security_keys", rules=[YUBI]),
    ])
    nitro = hid(name="Nitrokey 3", vid=0x20A0, pid=0x42B2)
    assert registry.claim(nitro).module_id == "fido2_security_keys"


def test_specialisation_beats_a_deeper_chain_only_when_it_is_deeper():
    registry = ModuleRegistry([
        manifest("fido2_security_keys", rules=[FIDO]),
        manifest("yubikeys", extends="fido2_security_keys", rules=[YUBI]),
        manifest("yubikey_bio", extends="yubikeys", rules=[
            MatchRule(transport=Transport.HID, vendor_id=0x1050, product_id=0x0407),
        ]),
    ])
    assert registry.claim(hid()).module_id == "yubikey_bio"


def test_the_base_chain_is_reported_for_a_module():
    registry = ModuleRegistry([
        manifest("fido2_security_keys"),
        manifest("yubikeys", extends="fido2_security_keys"),
        manifest("yubikey_bio", extends="yubikeys"),
    ])
    assert registry.base_chain("yubikey_bio") == [
        "yubikey_bio", "yubikeys", "fido2_security_keys",
    ]
    assert registry.base_chain("fido2_security_keys") == ["fido2_security_keys"]


def test_a_cycle_in_extends_does_not_hang():
    registry = ModuleRegistry([
        manifest("a", extends="b", rules=[FIDO]),
        manifest("b", extends="a", rules=[FIDO]),
    ])
    assert registry.claim(hid()).module_id in {"a", "b"}
    assert len(registry.base_chain("a")) <= 2


def test_disabling_a_specialisation_falls_back_to_its_base():
    """Turning off the YubiKey module must leave the key working as a generic CTAP key, not
    unsupported."""
    registry = ModuleRegistry([
        manifest("fido2_security_keys", rules=[FIDO]),
        manifest("yubikeys", extends="fido2_security_keys", rules=[YUBI]),
    ])
    registry.set_enablement("yubikeys", Enablement.OFF)
    assert registry.claim(hid()).module_id == "fido2_security_keys"


# --------------------------------------------------------------------------- vendor gating


def _claim(registry, **kw):
    kw.setdefault("uid", "u")
    kw.setdefault("path", "/dev/hidraw0")
    kw.setdefault("transport", Transport.HID)
    return registry.claim(DeviceInfo(**kw)).module_id


def test_every_shipped_module_is_gated_on_something_vendor_specific():
    """Except the CTAP base, which is vendor-neutral by design and says so in its manifest.

    A rule with no vendor id, no uuid and no vendor-specific property would claim whole classes of
    other makers' hardware -- a Logitech mouse landing in the Razer module, say.
    """
    registry = ModuleRegistry.discover()
    for module_id, manifest in registry._manifests.items():
        if module_id == "fido2_security_keys":
            continue
        for rule in manifest.match:
            gated = (
                rule.vendor_id is not None
                or rule.uuid
                or rule.properties
                or (rule.name_glob and rule.transport is not Transport.HID)
            )
            assert gated, f"{module_id}: {rule} claims by transport alone"


def test_a_logitech_device_goes_to_the_logitech_module_and_nowhere_else():
    """The concrete version of the rule above, on the vendor most likely to collide.

    Updated when `logitech_peripherals` was added: these used to be claimed by nothing. What the
    test is really guarding has not changed -- that a Logitech device does not end up in the Razer,
    Dell-dock or FIDO module because of a name or a usage page.
    """
    registry = ModuleRegistry.discover()
    for name, product_id in (
        ("Logitech G502 HERO", 0xC08B),
        ("Logitech USB Receiver", 0xC52B),
        ("Logitech MX dock", 0xC52B),  # 'dock' in the name must not reach the Dell dock module
    ):
        assert _claim(
            registry, name=name, vendor_id=0x046D, product_id=product_id
        ) == "logitech_peripherals"
    # Bluetooth is not this module's transport: it matches hid nodes only, so a Bluetooth-only
    # Logitech headset is still unclaimed rather than being offered a page that cannot open.
    assert _claim(
        registry, name="Logitech Zone", vendor_id=0x046D, transport=Transport.BLUETOOTH
    ) == ""


def test_peripherals_from_other_makers_are_left_alone():
    registry = ModuleRegistry.discover()
    assert _claim(registry, name="Corsair K70", vendor_id=0x1B1C) == ""
    assert _claim(registry, name="SteelSeries Rival", vendor_id=0x1038) == ""
    # A Dell device that is not a dock: the vendor id matches, the rest of the rule does not.
    assert _claim(registry, name="Dell KB216", vendor_id=0x413C, product_id=0x2113) == ""


def test_each_vendor_reaches_its_own_module():
    registry = ModuleRegistry.discover()
    assert _claim(registry, name="Razer BlackWidow", vendor_id=0x1532) == "razer_peripherals"
    assert _claim(
        registry, name="Dell Thunderbolt Dock", vendor_id=0x413C, product_id=0xB06E
    ) == "dell_docks"
    assert _claim(registry, name="Poly BT700", vendor_id=0x047F) == "poly_headsets"
    assert _claim(registry, name="Yubico YubiKey", vendor_id=0x1050) == "yubikeys"


def test_the_ctap_base_is_vendor_neutral_on_purpose():
    """The one deliberate exception: a security key from any maker must be claimed."""
    registry = ModuleRegistry.discover()
    for name, vendor_id in (("Nitrokey 3", 0x20A0), ("SoloKeys Solo 2", 0x0483)):
        assert _claim(
            registry, name=name, vendor_id=vendor_id, properties={"hid_usage_page": "f1d0"}
        ) == "fido2_security_keys"
    # ...and it is the usage page doing the work, not the transport: a Logitech mouse on the
    # generic desktop page goes to its own module, never to the security-key one.
    assert _claim(
        registry, name="Logitech G502", vendor_id=0x046D, properties={"hid_usage_page": "0001"}
    ) == "logitech_peripherals"


# --------------------------------------------------------------------------- connection labels


def test_a_connection_label_is_route_then_identifier():
    from hardware_ui.core.connection import ConnectionLabel

    assert ConnectionLabel("via BT700", "S/NFH39CL").display() == (
        "Connection: via BT700 · S/NFH39CL"
    )
    assert ConnectionLabel("USB").display() == "Connection: USB"
    assert ConnectionLabel(identifier="S/N123").display() == "Connection: S/N123"


def test_an_empty_label_says_nothing_at_all():
    """A module that supplies none loses nothing: the row is exactly as it was."""
    from hardware_ui.core.connection import NONE

    assert not NONE
    assert NONE.display() == ""


def test_a_display_gets_its_label_before_anything_is_opened():
    """The one case answerable at enumeration -- DRM names the connector for free."""
    from hardware_ui.core.connection import NONE, from_connector

    assert from_connector("card1-DP-3").route == "DP-3"
    assert from_connector("nonsense") is NONE


def test_the_default_device_says_nothing():
    from hardware_ui.core import Device
    from hardware_ui.core.connection import NONE

    assert Device.connection_label(object()) is NONE  # type: ignore[arg-type]


def test_the_identifier_appears_only_when_two_rows_share_a_name(qapp):
    """A serial tells identical devices apart and clutters a row that is already unique."""
    from hardware_ui.core import ConnectionLabel, DeviceInfo, State, Transport
    from hardware_ui.shell.window import Sidebar

    def poly(uid, serial):
        return DeviceInfo(
            uid=uid, name="Poly BT700", transport=Transport.HID, state=State.CONNECTED,
            connection=ConnectionLabel("via BT700", serial),
        )

    bar = Sidebar()
    bar.reconcile([poly("a", "S/N-AAA")])
    rows = [bar._list.item(i).text() for i in range(bar._list.count())]
    assert any("via BT700" in r and "S/N-AAA" not in r for r in rows)

    bar.reconcile([poly("a", "S/N-AAA"), poly("b", "S/N-BBB")])
    rows = [bar._list.item(i).text() for i in range(bar._list.count())]
    assert sum("S/N-AAA" in r or "S/N-BBB" in r for r in rows) == 2


def test_a_restarting_bluetooth_device_is_not_declared_back_too_early(qapp):
    """BlueZ lists a paired headset whether it is on or not, so presence is not readiness.

    Waiting on presence alone would have reconnected to a headset still rebooting -- the exact
    thing the flat sleep it replaced was avoiding.
    """
    from hardware_ui.core import DeviceInfo, State, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    off = DeviceInfo(uid="bt:aa", name="WH-1000XM4", transport=Transport.BLUETOOTH,
                     state=State.PAIRED)
    on = DeviceInfo(uid="bt:aa", name="WH-1000XM4", transport=Transport.BLUETOOTH,
                    state=State.CONNECTED)

    controller._devices = [off]
    assert controller._by_uid("bt:aa") is not None, "still listed while switched off"
    assert not controller._is_back("bt:aa"), "listed is not the same as reachable"

    controller._devices = [on]
    assert controller._is_back("bt:aa")


def test_a_usb_device_is_back_as_soon_as_it_is_found(qapp):
    """Its node exists only while the hardware does, so presence and readiness coincide."""
    from hardware_ui.core import DeviceInfo, State, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    controller._devices = []
    assert not controller._is_back("hid:3-3")
    controller._devices = [
        DeviceInfo(uid="hid:3-3", name="YubiKey", transport=Transport.HID,
                   path="/dev/hidraw21", state=State.PRESENT)
    ]
    assert controller._is_back("hid:3-3")


def _waiting_controller(sweeps):
    """A controller whose enumerate() replays a scripted sequence of sightings."""
    from hardware_ui.core import DeviceInfo, State, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    here = DeviceInfo(uid="hid:3-3", name="Key", transport=Transport.HID, state=State.PRESENT)
    controller._devices = [here] if sweeps[0] else []
    remaining = list(sweeps[1:])

    async def enumerate_():
        if remaining:
            controller._devices = [here] if remaining.pop(0) else []

    controller.enumerate = enumerate_
    return controller


def test_the_wait_does_not_trust_the_sweep_taken_before_the_restart(qapp):
    """`_devices` still lists the device we have just told to restart.

    Asking "is it back?" straight away answered yes against stale data and reopened the device
    mid-reset, which fails with ENODEV — the user saw "No such device. Switch it on, then Rescan."
    """
    import asyncio

    from hardware_ui.shell import app as mod

    # Present (stale), then gone, then gone, then back.
    controller = _waiting_controller([True, True, False, False, True])
    mod_poll, mod.REBOOT_POLL = mod.REBOOT_POLL, 0.0
    try:
        assert asyncio.run(controller._await_return("hid:3-3")) is True
    finally:
        mod.REBOOT_POLL = mod_poll
    # It must have waited for the gap rather than returning on the first look.
    assert controller._devices, "should have come back"


def test_a_device_that_never_leaves_is_not_waited_on_forever(qapp):
    """It applied the change without restarting after all; do not hold the reconnect hostage."""
    import asyncio

    from hardware_ui.shell import app as mod

    controller = _waiting_controller([True] * 12)
    poll, drop = mod.REBOOT_POLL, mod.REBOOT_DROP_TIMEOUT
    mod.REBOOT_POLL, mod.REBOOT_DROP_TIMEOUT = 0.0, 0.05
    try:
        assert asyncio.run(controller._await_return("hid:3-3")) is True
    finally:
        mod.REBOOT_POLL, mod.REBOOT_DROP_TIMEOUT = poll, drop


def test_a_device_that_does_not_come_back_gives_up(qapp):
    import asyncio

    from hardware_ui.shell import app as mod

    controller = _waiting_controller([True, False] + [False] * 20)
    poll, timeout = mod.REBOOT_POLL, mod.REBOOT_RECONNECT_TIMEOUT
    mod.REBOOT_POLL, mod.REBOOT_RECONNECT_TIMEOUT = 0.0, 0.05
    try:
        assert asyncio.run(controller._await_return("hid:3-3")) is False
    finally:
        mod.REBOOT_POLL, mod.REBOOT_RECONNECT_TIMEOUT = poll, timeout


# --------------------------------------------------------------------------- modules page


def test_the_modules_page_lists_every_installed_module(qapp):
    from PyQt6.QtWidgets import QComboBox

    from hardware_ui.core.modules import ModuleRegistry
    from hardware_ui.shell.modules_page import ModulesDialog

    registry = ModuleRegistry.discover()
    dialog = ModulesDialog(registry)
    assert len(dialog.findChildren(QComboBox)) == len(list(registry))


def test_it_offers_three_states_not_a_checkbox(qapp):
    """A boolean conflates "when the hardware is there" with "always, I am testing something"."""
    from hardware_ui.core.modules import Enablement
    from hardware_ui.shell.modules_page import CHOICES

    assert [state for state, _, _ in CHOICES] == [
        Enablement.AUTO, Enablement.ALWAYS, Enablement.OFF
    ]


def test_changing_a_state_persists_it_and_asks_for_a_rescan(qapp, tmp_path):
    from PyQt6.QtWidgets import QComboBox

    from hardware_ui.core.modules import Enablement, ModuleRegistry
    from hardware_ui.shell.modules_page import ModulesDialog

    registry = ModuleRegistry.discover()
    dialog = ModulesDialog(registry)
    asked: list[int] = []
    dialog.changed.connect(lambda: asked.append(1))

    dialog._set("razer_peripherals", Enablement.OFF)
    assert registry.enablement("razer_peripherals") is Enablement.OFF
    # Written through, not merely held: a reopened registry sees it.
    assert ModuleRegistry.discover().enablement("razer_peripherals") is Enablement.OFF

    dialog.accept()
    assert asked == [1]

    registry.set_enablement("razer_peripherals", Enablement.AUTO)
    assert isinstance(dialog.findChildren(QComboBox)[0], QComboBox)


def test_looking_without_touching_does_not_rescan(qapp):
    """Closing a page you only read should not make the device list flicker."""
    from hardware_ui.core.modules import ModuleRegistry
    from hardware_ui.shell.modules_page import ModulesDialog

    dialog = ModulesDialog(ModuleRegistry.discover())
    asked: list[int] = []
    dialog.changed.connect(lambda: asked.append(1))
    dialog.accept()
    assert asked == []


def test_setting_a_state_to_what_it_already_is_is_not_a_change(qapp):
    from hardware_ui.core.modules import Enablement, ModuleRegistry
    from hardware_ui.shell.modules_page import ModulesDialog

    dialog = ModulesDialog(ModuleRegistry.discover())
    dialog._set("dell_monitors", Enablement.AUTO)
    assert dialog._dirty is False


def test_a_specialisation_says_what_disabling_it_costs(qapp):
    """`yubikeys` extends the CTAP module; switching it off is not the same as losing the key."""
    from hardware_ui.core.modules import ModuleRegistry
    from hardware_ui.shell.modules_page import _detail

    registry = ModuleRegistry.discover()
    detail = _detail(registry.get("yubikeys"), registry)
    assert "Extends" in detail
    assert "fewer settings" in detail


def test_a_module_needing_vendor_data_says_so(qapp):
    from hardware_ui.core.modules import ModuleRegistry
    from hardware_ui.shell.modules_page import _detail

    registry = ModuleRegistry.discover()
    assert "vendor data" in _detail(registry.get("poly_headsets"), registry)


# --------------------------------------------------------------------------- startup painting


def test_cached_devices_are_not_filed_as_disconnected():
    """`load_cache` promises "rendered normally, not greyed out". State UNKNOWN means "not scanned
    yet", not "unreachable", and filing those under DISCONNECTED made a normal startup look like
    every device had vanished for the first moment."""
    from hardware_ui.core.device import Category, DeviceInfo, State, Transport
    from hardware_ui.shell.window import DISCONNECTED, _section

    cached = DeviceInfo(uid="u", name="MX Master 3S", transport=Transport.HID,
                        category=Category.INPUT, state=State.UNKNOWN)
    assert _section(cached) == "INPUT"

    # A Bluetooth device BlueZ merely remembers is still disconnected, which is the case the
    # bucket exists for. PAIRED, because that is what enumeration now emits for anything it cannot
    # reach -- PRESENT is reserved for "switched on and linked to this machine".
    absent = DeviceInfo(uid="u", name="WH-1000XM4", transport=Transport.BLUETOOTH,
                        category=Category.AUDIO, state=State.PAIRED)
    assert _section(absent) == DISCONNECTED

    # The other half of that rename: a headset BlueZ reports as connected is reachable, so it
    # files under its own category even though nobody has pressed Connect yet. It does *not* get
    # a green dot -- that is State.CONNECTED, which only the shell writes.
    live = DeviceInfo(uid="u", name="WH-1000XM4", transport=Transport.BLUETOOTH,
                      category=Category.AUDIO, state=State.PRESENT)
    assert _section(live) == "AUDIO"

    # ...and a *cached* Bluetooth device is the same case, not an unscanned one. BlueZ remembers
    # every headset ever paired and most are switched off, so treating UNKNOWN as settled here put
    # two powered-off headsets under AUDIO as though they were ready to configure.
    cached_bt = DeviceInfo(uid="u", name="WH-1000XM3", transport=Transport.BLUETOOTH,
                           category=Category.AUDIO, state=State.UNKNOWN)
    assert _section(cached_bt) == DISCONNECTED


def test_the_discovery_cache_round_trips_the_icon(tmp_path, monkeypatch):
    """Without the icon, a cached keyboard falls back to Category.INPUT's `input-gaming` and comes
    up as a gamepad until the live scan lands."""
    from hardware_ui.core import discovery
    from hardware_ui.core.device import Category, DeviceInfo, State, Transport

    monkeypatch.setattr(discovery, "cache_dir", lambda: tmp_path)
    original = DeviceInfo(
        uid="hid:logitech:ABC:1", name="MX Keys S", transport=Transport.HID,
        category=Category.INPUT, icon_name="input-keyboard", state=State.PRESENT,
        properties={"logitech_slot": 1},
    )
    discovery.save_cache([original])
    restored = discovery.load_cache()
    assert len(restored) == 1
    assert restored[0].icon_name == "input-keyboard"
    assert restored[0].icon == "input-keyboard", "not the category's gamepad"
    # A module needs the slot to find a device that has no node of its own.
    assert restored[0].properties.get("logitech_slot") == 1


def test_the_interaction_dialog_lives_on_the_gui_thread(qapp):
    """Built lazily it would be constructed inside `_connect`, which runs on the asyncio thread --
    and a QObject's queued signals go to *its* thread's event loop. The asyncio thread has no Qt
    loop, so every message would be queued and never delivered: the pairing dialog simply never
    appears and nothing raises."""
    import threading

    from PyQt6.QtCore import QThread

    from hardware_ui.shell.interaction import QtInteraction

    made: dict[str, QtInteraction] = {}

    def build_off_thread() -> None:
        made["worker"] = QtInteraction(None)

    worker = threading.Thread(target=build_off_thread)
    worker.start()
    worker.join()

    on_gui = QtInteraction(None)
    assert on_gui.thread() is QThread.currentThread()
    assert made["worker"].thread() is not QThread.currentThread(), (
        "constructing it off the GUI thread gives it the wrong affinity -- which is why the "
        "controller builds it in __init__ and never on first use"
    )


def test_an_unwritable_control_is_disabled_but_a_readout_is_not(qapp):
    """Two different things wear `writable=False`. A readout was never interactive and greying it
    makes it unreadable; an unwritable *dropdown* is a control the module must not change, and
    leaving it live offered values that would silently disable a physical mouse button."""
    from hardware_ui.core import Capability, CapabilitySet, Choice, Kind
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet([
        Capability(key="ro.text", kind=Kind.READOUT, label="Serial", writable=False),
        Capability(key="ro.meter", kind=Kind.METER, label="Battery", writable=False),
        Capability(key="frozen.choice", kind=Kind.CHOICE, label="Diversion", writable=False,
                   choices=(Choice("a", "Regular"), Choice("b", "Diverted"))),
        Capability(key="frozen.toggle", kind=Kind.TOGGLE, label="Locked", writable=False),
        Capability(key="live.choice", kind=Kind.CHOICE, label="Action",
                   choices=(Choice("a", "Left Click"),)),
    ]))

    assert form._rows["ro.text"].control.isEnabled(), "a readout must stay readable"
    assert form._rows["ro.meter"].control.isEnabled(), "a meter must stay readable"
    assert not form._rows["frozen.choice"].control.isEnabled()
    assert not form._rows["frozen.toggle"].control.isEnabled()
    assert form._rows["live.choice"].control.isEnabled()


def test_a_switched_on_bluetooth_headset_gets_no_green_dot_until_connect(qapp):
    """Reported from the field: a Bluetooth headset showed the green "connected" dot before anyone
    had pressed Connect, which no other application does.

    The cause was `State.CONNECTED` doing two jobs. Enumeration used it for "BlueZ has a link to
    this device"; the shell uses it for "this application has an open session", and the dot reports
    the second. Enumeration now says PRESENT -- available, not yet opened.
    """
    from PyQt6.QtCore import Qt

    from hardware_ui.core.device import Category, DeviceInfo, State, Transport
    from hardware_ui.shell.window import Sidebar

    def row(state):
        return DeviceInfo(uid="bt:a", name="WH-1000XM4", transport=Transport.BLUETOOTH,
                          category=Category.AUDIO, state=state, module_id="sony_headsets")

    bar = Sidebar()

    def device_row():
        # The list carries section headings too, so find the row by uid rather than by index.
        for i in range(bar._list.count()):
            item = bar._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == "bt:a":
                return item
        raise AssertionError("the device row is missing from the sidebar")

    bar.reconcile([row(State.PRESENT)])
    item = device_row()
    assert item.data(Qt.ItemDataRole.UserRole + 1) is None, "no dot before Connect"
    assert "connected" not in item.text()

    # ...and green once the shell reports its own session.
    bar.reconcile([row(State.CONNECTED)])
    item = device_row()
    assert item.data(Qt.ItemDataRole.UserRole + 1) == "#27ae60"
    assert "connected" in item.text()


def test_enumeration_never_claims_a_session_it_has_not_opened():
    """The rule the fix rests on, asserted directly against the enumerator's own output."""
    import subprocess

    from hardware_ui.core import discovery
    from hardware_ui.core.device import State

    listing = "Device AA:BB:CC:DD:EE:01 Live\nDevice AA:BB:CC:DD:EE:02 Resting\n"
    info = {
        "AA:BB:CC:DD:EE:01": "\tConnected: yes\n\tPaired: yes\n",
        "AA:BB:CC:DD:EE:02": "\tConnected: no\n\tPaired: yes\n",
    }

    def fake_run(cmd, *a, **k):
        if "devices" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=info[cmd[-1]], stderr="")

    import unittest.mock

    with unittest.mock.patch.object(subprocess, "run", fake_run):
        found = {d.address: d.state for d in discovery._enumerate_bluetooth_cli()}

    assert found == {"AA:BB:CC:DD:EE:01": State.PRESENT, "AA:BB:CC:DD:EE:02": State.PAIRED}
    assert State.CONNECTED not in found.values()


def test_a_claiming_module_supplies_a_category_only_when_none_was_found():
    """A module that claims a device knows what it is; enumeration was guessing. But a
    classification drawn from real evidence -- a HID report descriptor, a USB interface class --
    must never be overruled by a manifest.

    The case: a Logitech mouse switched off has no hidraw node left, so only its BlueZ row remains,
    and that row's category could only be guessed from its name. "MX Master 3S" contains no word to
    match, so it went to OTHER and drew the generic peripherals icon -- while the very same mouse
    showed a mouse icon a moment earlier.
    """
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.core.modules import ModuleRegistry

    registry = ModuleRegistry.discover()

    unguessable = DeviceInfo(uid="bt:a", name="MX Master 3S", transport=Transport.BLUETOOTH,
                             category=Category.OTHER, address="DE:D7:4B:FC:27:EA",
                             uuids=frozenset({"00010000-0000-1000-8000-011f2000046d"}))
    claimed = registry.claim(unguessable)
    assert claimed.module_id == "logitech_peripherals"
    assert claimed.category is Category.INPUT, "the manifest should fill in an OTHER category"

    # And a category that came from evidence survives being claimed.
    evidenced = DeviceInfo(uid="bt:b", name="MX Master 3S", transport=Transport.BLUETOOTH,
                           category=Category.AUDIO, address="DE:D7:4B:FC:27:EA",
                           uuids=frozenset({"00010000-0000-1000-8000-011f2000046d"}))
    assert registry.claim(evidenced).category is Category.AUDIO


def test_a_switched_off_bluetooth_logitech_device_is_listed_rather_than_vanishing():
    """The reported bug. Switching the mouse off removes its hidraw node, leaving only the BlueZ
    row; with no Bluetooth match rule that row was unclaimed, and an unclaimed row is never
    rendered -- so the mouse disappeared instead of moving to Disconnected devices.

    Sony and Poly never had this because they match Bluetooth in the first place.
    """
    from hardware_ui.core.device import Category, DeviceInfo, State, Transport
    from hardware_ui.core.modules import ModuleRegistry
    from hardware_ui.shell.window import DISCONNECTED, _section

    off = DeviceInfo(uid="bt:a", name="MX Master 3S", transport=Transport.BLUETOOTH,
                     category=Category.OTHER, address="DE:D7:4B:FC:27:EA", state=State.PAIRED,
                     uuids=frozenset({"00010000-0000-1000-8000-011f2000046d"}))
    claimed = ModuleRegistry.discover().claim(off)
    assert claimed.supported, "an unclaimed row is never rendered, so the device would vanish"
    assert not claimed.ready, "switched off is not connectable"
    assert _section(claimed) == DISCONNECTED


def test_the_bluetooth_rule_needs_logitechs_own_service_not_just_the_transport():
    """Scoped to Logitech's vendor GATT service, whose last four hex digits are their vendor id.
    A Logitech Bluetooth speaker does not carry it, so this cannot claim a device the module has no
    business with."""
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.core.modules import ModuleRegistry

    speaker = DeviceInfo(uid="bt:z", name="Logitech Boombox", transport=Transport.BLUETOOTH,
                         category=Category.AUDIO, address="AA:BB:CC:DD:EE:FF",
                         uuids=frozenset({"0000110b-0000-1000-8000-00805f9b34fb"}))
    assert not ModuleRegistry.discover().claim(speaker).module_id
