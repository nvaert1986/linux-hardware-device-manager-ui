"""YubiKey module tests. No key needed.

The point of most of these is generality: the module has to serve every YubiKey `ykman` supports,
not the YubiKey 5 NFC that happened to be on the desk. So the fixtures are a NEO, a Security Key
with no serial number, a FIPS key and a 5.7 key with NFC restricted — none of which exist here.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from hardware_ui.core import DeviceInfo, Kind, Transport
from hardware_ui.core.modules import ModuleRegistry
from hardware_ui.modules.yubikeys import capabilities as C
from hardware_ui.modules.yubikeys.device import YubiKey

USB, NFC = "usb", "nfc"


class Version(tuple):
    """Stands in for ``yubikit.core.Version``, which prints itself dotted rather than as a tuple."""

    def __str__(self) -> str:
        return ".".join(str(part) for part in self)


#: A YubiKey 5, as the default fixture. Other generations are passed explicitly.
V5 = Version((5, 2, 6))

# CAPABILITY bit values, from yubikit.management. Spelled out so the tests need no import.
# Named with an _APP suffix where the bare name is taken by an imported module.
OTP_APP, U2F, FIDO2, OPENPGP, PIV, OATH_APP, HSMAUTH = (
    0x01, 0x02, 0x200, 0x08, 0x10, 0x20, 0x100
)
ALL_5 = OTP_APP | U2F | FIDO2 | OATH_APP | PIV | OPENPGP


def fake_info(
    *,
    version=V5,
    serial=12345678,
    form_factor=1,
    usb_supported=ALL_5,
    usb_enabled=None,
    nfc_supported=ALL_5,
    nfc_enabled=None,
    is_locked=False,
    is_fips=False,
    is_sky=False,
    pin_complexity=False,
    nfc_restricted=None,
    part_number=None,
    fips_capable=0,
):
    """A stand-in for ``yubikit.management.DeviceInfo``, keyed by the transport strings above."""
    supported = {USB: usb_supported}
    enabled = {USB: usb_supported if usb_enabled is None else usb_enabled}
    if nfc_supported:
        supported[NFC] = nfc_supported
        enabled[NFC] = nfc_supported if nfc_enabled is None else nfc_enabled
    # A real DeviceConfig, not a stand-in: the module calls `dataclasses.replace` on it, which a
    # SimpleNamespace silently fails. The transports stay as the strings above.
    from yubikit.management import DeviceConfig

    return SimpleNamespace(
        version=version, serial=serial, form_factor=form_factor,
        supported_capabilities=supported,
        config=DeviceConfig(enabled, None, None, None, nfc_restricted),
        is_locked=is_locked, is_fips=is_fips, is_sky=is_sky,
        pin_complexity=pin_complexity, part_number=part_number, fips_capable=fips_capable,
    )


@pytest.fixture
def device(monkeypatch):
    """A ``YubiKey`` whose transport constants are the strings above, so no ``ykman`` is needed."""
    dev = YubiKey(
        DeviceInfo(
            uid="hid:yubikey", name="Yubico YubiKey OTP+FIDO+CCID", transport=Transport.HID,
            path="/dev/hidraw0", vendor_id=0x1050, product_id=0x0407,
        )
    )
    monkeypatch.setattr(
        "yubikit.management.TRANSPORT", SimpleNamespace(USB=USB, NFC=NFC), raising=False
    )
    return dev


def caps_by_section(dev, section):
    return [c for c in dev.extra_capabilities() if c.section == section]


def _capability_for_test(self, key):
    """The freshly built capability with this key, without going through the shell."""
    return next(c for c in self.extra_capabilities() if c.key == key)


YubiKey.capabilities_for_test = _capability_for_test


# --------------------------------------------------------------------------- module scope


def test_ykman_is_not_imported_until_a_key_is_opened():
    """The dependency is this module's, not the application's — and it is optional even here."""
    import hardware_ui.modules.yubikeys.device as mod

    top = [
        line for line in inspect.getsource(mod).splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]
    assert not any("ykman" in line or "yubikit" in line for line in top), top


def test_the_manifest_extends_the_base_and_wins_the_claim():
    registry = ModuleRegistry.discover()
    yubikeys = registry.get("yubikeys")
    assert yubikeys.extends == "fido2_security_keys"
    assert registry.base_chain("yubikeys") == ["yubikeys", "fido2_security_keys"]

    info = DeviceInfo(
        uid="hid:yubikey", name="Yubico YubiKey OTP+FIDO+CCID", transport=Transport.HID,
        path="/dev/hidraw0", vendor_id=0x1050, product_id=0x0407,
        properties={"hid_usage_page": "f1d0"},
    )
    assert registry.claim(info).module_id == "yubikeys"


def test_the_match_rule_does_not_depend_on_the_fido_interface():
    """A YubiKey with FIDO switched off must still be claimed.

    Matching on the FIDO usage page as well would mean disabling FIDO removed the key from the
    application — taking the control needed to undo it along with it.
    """
    registry = ModuleRegistry.discover()
    info = DeviceInfo(
        uid="hid:yubikey", name="Yubico YubiKey OTP+CCID", transport=Transport.HID,
        path="/dev/hidraw0", vendor_id=0x1050, product_id=0x0405,
        properties={"hid_usage_page": ""},
    )
    assert registry.claim(info).module_id == "yubikeys"


def test_a_non_yubico_key_still_goes_to_the_base_module():
    registry = ModuleRegistry.discover()
    info = DeviceInfo(
        uid="hid:nitrokey", name="Nitrokey 3", transport=Transport.HID,
        path="/dev/hidraw0", vendor_id=0x20A0, product_id=0x42B2,
        properties={"hid_usage_page": "f1d0"},
    )
    assert registry.claim(info).module_id == "fido2_security_keys"


# --------------------------------------------------------------------------- identity rows


def test_a_yubikey_5_reports_firmware_serial_and_form_factor(device):
    device._yk_rows = device._identity_rows(fake_info())
    assert device._yk_rows["firmware"] == "5.2.6"
    assert device._yk_rows["serial"] == "12345678"
    assert device._yk_rows["lock"] == "Not set"


def test_a_security_key_has_no_serial_and_no_form_factor(device):
    """A Security Key by Yubico reports neither. The rows are absent, not blank."""
    rows = device._identity_rows(
        fake_info(serial=None, form_factor=0, usb_supported=U2F | FIDO2, nfc_supported=0,
                  is_sky=True)
    )
    assert "serial" not in rows
    assert "form_factor" not in rows
    assert rows["series"] == "Security Key series"


def test_a_neo_gets_no_configuration_lock_row(device):
    """Lock codes arrived with the YubiKey 4; on a NEO the row would always read 'Not set'."""
    rows = device._identity_rows(fake_info(version=Version((3, 4, 9)), nfc_restricted=None))
    assert "lock" not in rows
    assert rows["firmware"] == "3.4.9"


def test_a_locked_key_says_so_whatever_its_firmware(device):
    rows = device._identity_rows(fake_info(version=Version((3, 4, 9)), is_locked=True))
    assert "lock code" in rows["lock"]


def test_fips_and_part_number_are_reported_when_present(device):
    rows = device._identity_rows(fake_info(is_fips=True, part_number="ABC-123"))
    assert rows["series"] == "FIPS · part ABC-123"


def test_an_ordinary_key_gets_no_series_row(device):
    assert "series" not in device._identity_rows(fake_info())


def test_pin_complexity_and_nfc_restriction_appear_only_when_real(device):
    """Both are firmware 5.7 features; older keys must not grow empty rows."""
    plain = device._identity_rows(fake_info(nfc_restricted=None))
    assert "pin_complexity" not in plain
    assert "nfc_restricted" not in plain

    modern = device._identity_rows(fake_info(pin_complexity=True, nfc_restricted=True))
    assert modern["pin_complexity"] == "Required"
    assert "until the key is next powered over USB" in modern["nfc_restricted"]


def test_nfc_restriction_is_not_reported_on_a_key_without_nfc(device):
    rows = device._identity_rows(fake_info(nfc_supported=0, nfc_restricted=False))
    assert "nfc_restricted" not in rows


# --------------------------------------------------------------------------- applications


def test_applications_come_from_the_capability_enum_not_a_list_here(device):
    """Everything the key supports appears, once per transport; what it lacks is absent."""
    apps = device._applications(fake_info())
    usb = [name for transport, name, _, _ in apps if transport == C.USB]
    assert usb == ["OTP", "U2F", "FIDO2", "OATH", "PIV", "OPENPGP"]
    assert "HSMAUTH" not in usb  # a YubiKey 5 NFC does not have it
    assert all(enabled for _, _, _, enabled in apps)


def test_an_application_disabled_on_one_transport_is_off_only_there(device):
    apps = {
        (t, n): on for t, n, _, on in device._applications(fake_info(nfc_enabled=ALL_5 & ~FIDO2))
    }
    assert apps[(C.USB, "FIDO2")] is True
    assert apps[(C.NFC, "FIDO2")] is False
    assert apps[(C.NFC, "PIV")] is True


def test_an_application_off_everywhere_is_off_on_both_rows(device):
    apps = {
        (t, n): on for t, n, _, on in device._applications(
            fake_info(usb_enabled=ALL_5 & ~OATH_APP, nfc_enabled=ALL_5 & ~OATH_APP)
        )
    }
    assert apps[(C.USB, "OATH")] is False
    assert apps[(C.NFC, "OATH")] is False


def test_a_key_without_nfc_gets_no_nfc_rows_at_all(device):
    apps = device._applications(fake_info(nfc_supported=0))
    assert {t for t, _, _, _ in apps} == {C.USB}


def test_labels_come_from_ykman_so_a_future_application_needs_no_change(device):
    labels = {name: label for _, name, label, _ in device._applications(fake_info())}
    assert labels["OTP"] == "Yubico OTP"
    assert labels["OPENPGP"] == "OpenPGP"


# --------------------------------------------------------------------------- degrading


def test_without_ykman_the_page_says_so_and_keeps_working(device):
    device._yk = None
    device._yk_problem = C.INSTALL_HINT
    rows = device.extra_capabilities()
    assert [c.key for c in rows] == [C.UNAVAILABLE_KEY]
    assert "yubikey-manager" in rows[0].note
    assert device.advisories()[C.UNAVAILABLE_KEY].message == C.INSTALL_HINT


def test_a_key_with_no_management_application_degrades_to_the_base_page(device):
    """A Security Key answers nothing here. That is the right page for it, not an error."""
    device._yk = None
    device._yk_problem = "no management application"
    assert device.extra_values() == {C.UNAVAILABLE_KEY: "Not available"}


def test_every_vendor_row_is_read_only(device):
    device._yk = fake_info()
    device._slots_read = True
    device._accounts = OATH.Accounts()
    device._yk_rows = device._identity_rows(fake_info())
    device._yk_apps = device._applications(fake_info())
    rows = [c for c in device.extra_capabilities() if c.group == C.GROUP_VENDOR]
    assert rows, "expected vendor rows"
    assert all(c.kind is Kind.READOUT and not c.writable for c in rows)


def test_the_model_name_replaces_the_usb_product_string(device):
    """The USB descriptor says 'Yubico YubiKey OTP+FIDO+CCID' — an interface list, not a model."""
    device._yk = fake_info()
    device._yk_rows = {}
    device._apps = []
    device._model_name = lambda info: "YubiKey 5 NFC"
    assert device.extra_values()["info.model"] == "YubiKey 5 NFC"


def test_an_unreadable_model_name_leaves_the_base_row_alone(device):
    device._yk = fake_info()
    device._yk_rows = {}
    device._apps = []
    device._model_name = lambda info: ""
    assert "info.model" not in device.extra_values()


# --------------------------------------------------------------------------- page order


def base_page():
    """A stand-in for what the CTAP module contributes: an Information and a Configuration tab."""
    from hardware_ui.core import Capability

    return [
        Capability(key="info.model", kind=Kind.READOUT, label="Model",
                   group=C.GROUP_INFO, section="Identity", writable=False),
        Capability(key="info.versions", kind=Kind.READOUT, label="CTAP versions",
                   group=C.GROUP_INFO, section="Capabilities", writable=False),
        Capability(key="action.refresh", kind=Kind.ACTION, label="Details",
                   group=C.GROUP_INFO, section="Actions"),
        Capability(key="action.reset", kind=Kind.ACTION, label="Factory reset",
                   group="Configuration", section="Maintenance"),
    ]


def test_the_vendor_rows_live_on_their_own_tab(device):
    """Not on the CTAP Information tab, which is long enough and is not vendor-specific."""
    device._yk = fake_info()
    device._yk_rows = {"firmware": "5.2.6"}
    device._apps = []
    device._slots_read = True
    device._accounts = OATH.Accounts()
    groups = {c.group for c in device.extra_capabilities()}
    # Applications is there for the timing controls even with no application rows: they are
    # fields of the same DeviceConfig and gated on which applications are enabled.
    assert groups == {C.GROUP_VENDOR, C.GROUP_ACCOUNTS, C.GROUP_APPS}


def test_the_applications_are_a_tab_of_toggles_not_readouts(device):
    """They were read-only sentences on the vendor tab; the point of the tab is changing them."""
    from hardware_ui.core import Kind

    device._yk = fake_info()
    device._yk_rows = {}
    device._apps = device._applications(fake_info())
    rows = [
        c for c in device.extra_capabilities()
        if c.group == C.GROUP_APPS and c.section != C.SECTION_TIMING
    ]
    assert rows and all(c.kind is Kind.TOGGLE and c.writable for c in rows)
    assert {c.section for c in rows} == {"Over USB", "Over NFC"}
    # Every toggle is a field of one DeviceConfig, so each carries all the others.
    assert all(len(c.writes_with) == len(rows) - 1 for c in rows)


def test_the_vendor_tab_is_named_for_any_vendor_not_for_yubico(device):
    """A Nitrokey or Logitech module reuses the name, so a reader learns it once."""
    assert "yubi" not in C.GROUP_VENDOR.casefold()


def test_the_vendor_tab_sits_directly_after_information(device):
    """One form is built per group in list order, so group order is tab order."""
    device._yk = fake_info()
    device._yk_rows = {"firmware": "5.2.6"}
    device._apps = []
    device._slots_read = True
    device._accounts = OATH.Accounts()
    ordered = device._in_reading_order(base_page() + list(device.extra_capabilities()))

    tabs = list(dict.fromkeys(c.group for c in ordered))
    assert tabs == [
        C.GROUP_INFO, C.GROUP_VENDOR, C.GROUP_APPS, C.GROUP_ACCOUNTS, "Configuration"
    ]


def test_the_information_tab_keeps_the_base_modules_own_order(device):
    """Nothing is reshuffled inside a tab this module does not own."""
    device._yk = fake_info()
    device._yk_rows = {"firmware": "5.2.6"}
    device._apps = []
    device._slots_read = True
    device._accounts = OATH.Accounts()
    ordered = device._in_reading_order(base_page() + list(device.extra_capabilities()))
    info = [c.section for c in ordered if c.group == C.GROUP_INFO]
    assert info == ["Identity", "Capabilities", "Actions"]


def test_ordering_a_page_without_vendor_rows_changes_nothing(device):
    """A key with no management application must not have its tabs rearranged."""
    page = base_page()
    assert device._in_reading_order(list(page)) == page


def test_ordering_never_splits_a_section_in_two(device):
    """Two headings with the same name would appear if a section were not contiguous."""
    device._yk = fake_info()
    device._yk_rows = {"firmware": "5.2.6", "serial": "1"}
    device._apps = device._applications(fake_info())
    ordered = device._in_reading_order(base_page() + list(device.extra_capabilities()))
    for group in {c.group for c in ordered}:
        seen: list[str] = []
        for cap in [c for c in ordered if c.group == group]:
            if not seen or seen[-1] != cap.section:
                seen.append(cap.section)
        assert len(seen) == len(set(seen)), (group, seen)


# --------------------------------------------------------------------------- OTP slots

from hardware_ui.core import DeviceError, NotSupported  # noqa: E402
from hardware_ui.modules.yubikeys import otp as OTP  # noqa: E402


@pytest.fixture
def slots(device, monkeypatch):
    """A device with slot 1 programmed and slot 2 empty — what a stock YubiKey looks like."""
    device._yk = fake_info()
    device._slots_read = True
    device._accounts = OATH.Accounts()
    device._slots_read = True
    device._slots = {
        1: OTP.SlotState(configured=True, touch_triggered=True),
        2: OTP.SlotState(configured=False, touch_triggered=False),
    }
    calls: list[tuple] = []
    for name in ("program_chalresp", "program_static", "program_ndef", "swap", "delete"):
        monkeypatch.setattr(
            OTP, name,
            lambda *a, _n=name, **kw: calls.append((_n, a, kw)),
        )
    monkeypatch.setattr(OTP, "read_state", lambda serial: device._slots)
    monkeypatch.setattr(device, "_repaint", lambda: None)
    device.calls = calls
    return device


def test_no_ykman_import_at_module_scope_in_otp():
    """Same rule as the device module: the dependency is optional right up to the first call."""
    top = [
        line for line in inspect.getsource(OTP).splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]
    assert not any("ykman" in line or "yubikit" in line for line in top), top


def test_slot_state_uses_yubicos_own_wording():
    assert C.describe_slot(True) == "Slot is configured"
    assert C.describe_slot(False) == "Slot is empty"
    assert C.slot_label(1) == "Slot 1 · short touch"
    assert C.slot_label(2) == "Slot 2 · long touch"


def test_each_slot_owns_its_own_actions(slots):
    """No shared "programme into" dropdown: a button says which slot it writes to.

    The first version had one target dropdown that every action silently read, which made the page
    shorter and much harder to trust.
    """
    keys = {c.key for c in slots.extra_capabilities()}
    for slot in (1, 2):
        assert C.chalresp_key(slot) in keys
        assert C.static_key(slot) in keys
        assert C.delete_key(slot) in keys
    assert not any(k.endswith("target") for k in keys)


def test_the_warning_names_the_slot_that_is_about_to_be_overwritten(slots):
    """Slot 1 occupied, slot 2 empty — the two dialogs must not say the same thing."""
    assert "nothing is lost" in slots.capabilities_for_test(C.chalresp_key(2)).confirm_detail
    assert "factory Yubico OTP credential" in (
        slots.capabilities_for_test(C.chalresp_key(1)).confirm_detail
    )


def test_an_occupied_slot_two_warns_that_the_secret_is_unrecoverable(slots):
    slots._slots[2] = OTP.SlotState(configured=True)
    assert "exists nowhere else" in (
        slots.capabilities_for_test(C.chalresp_key(2)).confirm_detail
    )


def test_erasing_an_empty_slot_is_locked_with_a_reason(slots):
    """Locked rather than hidden, so the slot keeps its row and the reason is on screen."""
    advisory = slots.advisories()[C.delete_key(2)]
    assert advisory.locked
    assert "this slot is empty" in advisory.message.lower()
    assert C.delete_key(1) not in slots.advisories()


def test_erasing_an_empty_slot_is_refused_even_if_it_is_reached(slots):
    with pytest.raises(DeviceError, match="already empty"):
        slots.handle_set(C.delete_key(2), None)
    assert slots.calls == []


def test_erasing_an_occupied_slot_goes_through(slots):
    assert "erased" in slots.handle_set(C.delete_key(1), None)
    assert slots.calls[0][0] == "delete"


def test_a_generated_secret_is_shown_once_and_a_supplied_one_is_not(slots):
    """It cannot be read back off the key, and a backup key needs the same secret."""
    generated = slots.handle_set(C.chalresp_key(2), {C.F_SECRET: ""})
    assert "cannot be read back" in generated
    name, args, kwargs = slots.calls[-1]
    assert name == "program_chalresp"
    assert args[1] == 2  # slot
    assert len(args[2]) == OTP.SECRET_BYTES
    assert args[2].hex() in generated

    supplied = slots.handle_set(C.chalresp_key(2), {C.F_SECRET: "ab" * 20})
    assert "cannot be read back" not in supplied
    assert slots.calls[-1][1][2] == bytes.fromhex("ab" * 20)


def test_require_touch_and_access_code_come_from_the_dialog(slots):
    """Both used to sit on the page beside four buttons and belong to none of them visibly."""
    slots.handle_set(
        C.chalresp_key(2),
        {C.F_SECRET: "", C.F_TOUCH: True, C.F_ACCESS: "01:02:03:04:05:06"},
    )
    kwargs = slots.calls[-1][2]
    assert kwargs["require_touch"] is True
    assert kwargs["access_code"] == bytes.fromhex("010203040506")


def test_a_secret_is_checked_here_because_the_key_accepts_a_wrong_one_happily():
    assert len(OTP.parse_secret("")) == OTP.SECRET_BYTES
    assert OTP.parse_secret("aa:bb-cc dd" + "00" * 16) == bytes.fromhex("aabbccdd" + "00" * 16)
    with pytest.raises(DeviceError, match="hexadecimal"):
        OTP.parse_secret("zz" * 20)
    with pytest.raises(DeviceError, match="exactly 40"):
        OTP.parse_secret("00" * 19)


def test_an_access_code_is_six_bytes_or_nothing():
    assert OTP.parse_access_code("") is None
    assert OTP.parse_access_code("00 11 22 33 44 55") == bytes.fromhex("001122334455")
    with pytest.raises(DeviceError, match="12 hex"):
        OTP.parse_access_code("0011")


def test_a_static_password_is_rejected_before_it_reaches_the_key(monkeypatch):
    """Stored as key presses, so a character absent from the layout cannot be typed at all."""
    with pytest.raises(DeviceError, match="No password"):
        OTP.program_static(1, 2, "", layout="US", access_code=None)
    with pytest.raises(DeviceError, match="at most"):
        OTP.program_static(1, 2, "x" * 60, layout="US", access_code=None)


def test_there_is_no_options_section_left_on_the_page(slots):
    """Every modifier moved into the dialog of the action it modifies."""
    sections = {c.section for c in slots.extra_capabilities() if c.group == C.GROUP_SLOTS}
    assert "Options" not in sections


def test_a_write_re_reads_the_slots_instead_of_painting_what_was_asked_for(slots, monkeypatch):
    reads: list[int] = []
    monkeypatch.setattr(slots, "_read_slots", lambda: reads.append(1))
    slots.handle_set(C.SWAP_KEY, None)
    assert reads == [1]


def test_the_otp_tab_is_absent_when_the_interface_cannot_be_reached(device):
    device._yk = fake_info()
    device._slots_read = True
    device._accounts = OATH.Accounts()
    device._slots = {}
    device._slot_problem = "OTP is switched off"
    rows = [c for c in device.extra_capabilities() if c.group == C.GROUP_SLOTS]
    assert [c.key for c in rows] == [C.SLOTS_UNAVAILABLE_KEY]
    assert device.advisories()[C.SLOTS_UNAVAILABLE_KEY].message == "OTP is switched off"


def test_a_key_with_no_otp_problem_and_no_slots_adds_no_tab(device):
    """A key that never got as far as trying — no empty tab, no invented explanation."""
    device._yk = fake_info()
    device._slots_read = True
    device._accounts = OATH.Accounts()
    device._slots = {}
    device._slot_problem = ""
    assert [c for c in device.extra_capabilities() if c.group == C.GROUP_SLOTS] == []


def test_the_ndef_row_is_absent_on_a_key_without_nfc(device):
    device._yk = fake_info(nfc_supported=0)
    device._slots_read = True
    device._accounts = OATH.Accounts()
    device._slots = {1: OTP.SlotState(False), 2: OTP.SlotState(False)}
    keys = [c.key for c in device.extra_capabilities()]
    assert C.NDEF_KEY not in keys
    device._yk = fake_info()
    assert C.NDEF_KEY in [c.key for c in device.extra_capabilities()]


def test_the_three_tabs_stay_in_reading_order(slots):
    slots._yk_rows = {"firmware": "5.2.6"}
    slots._apps = [(C.USB, "OTP", "Yubico OTP", True)]
    ordered = slots._in_reading_order(base_page() + list(slots.extra_capabilities()))
    assert list(dict.fromkeys(c.group for c in ordered)) == [
        C.GROUP_INFO, C.GROUP_VENDOR, C.GROUP_APPS, C.GROUP_ACCOUNTS, C.GROUP_SLOTS,
        "Configuration",
    ]


def test_the_slots_tab_still_lands_before_configuration_with_no_vendor_tab(slots):
    """A key with no management application has no vendor tab; the slots tab must not slip past
    Configuration because of the gap."""
    slots._yk_rows = {}
    slots._apps = []
    ordered = slots._in_reading_order(base_page() + list(slots.extra_capabilities()))
    assert list(dict.fromkeys(c.group for c in ordered)) == [
        C.GROUP_INFO, C.GROUP_APPS, C.GROUP_ACCOUNTS, C.GROUP_SLOTS, "Configuration",
    ]


def test_an_unknown_key_is_still_not_supported(slots):
    with pytest.raises(NotSupported):
        slots.handle_set("otp.nonsense", 1)


def test_the_tabs_build_as_real_widgets(qapp, slots):
    """Builds the actual forms, which is the only thing that checks the schema is well formed.

    Written after two defects that every value-level test passed: `choices` given as bare tuples
    instead of `Choice` objects, and a `disabled=` argument `Capability` does not have. Both are
    invisible until a widget is constructed from them.
    """
    from hardware_ui.core import CapabilitySet
    from hardware_ui.shell.form import build_forms

    slots._yk_rows = {"firmware": "5.2.6"}
    slots._apps = [(C.USB, "OTP", "Yubico OTP", True)]
    caps = CapabilitySet(
        slots._in_reading_order(base_page() + list(slots.extra_capabilities()))
    )
    forms = build_forms(caps, lambda *a: None, lambda *a: None)
    assert list(forms) == [
        C.GROUP_INFO, C.GROUP_VENDOR, C.GROUP_APPS, C.GROUP_ACCOUNTS, C.GROUP_SLOTS,
        "Configuration",
    ]


def test_every_choice_offers_choice_objects(slots):
    """A bare `(label, value)` tuple survives every value-level assertion and dies in the widget."""
    from hardware_ui.core import Choice, Kind

    for cap in slots.extra_capabilities():
        if cap.kind is Kind.CHOICE:
            assert cap.choices, cap.key
            assert all(isinstance(c, Choice) for c in cap.choices), cap.key


# --------------------------------------------------------------------------- toggling applications


@pytest.fixture
def apps(device, monkeypatch):
    """A YubiKey 5 NFC with everything enabled, and the config write captured rather than sent."""
    device._yk = fake_info()
    device._apps = device._applications(fake_info())
    device._dev = object()
    written: list[tuple] = []

    class FakeSession:
        def __init__(self, dev):
            pass

        def write_device_config(self, config, reboot, cur, new):
            written.append((config, reboot))

    monkeypatch.setattr("yubikit.management.ManagementSession", FakeSession)
    monkeypatch.setattr(device, "_reopen", lambda: None)
    monkeypatch.setattr(device, "_repaint", lambda: None)
    device.written = written
    return device


def flip(dev, **changes):
    """The composite payload the shell hands over: every toggle's value, some of them changed."""
    out = {C.app_key(t, n): on for t, n, _, on in dev._apps}
    for name, value in changes.items():
        transport, app = name.split("_", 1)
        out[C.app_key(transport, app.upper())] = value
    return out


def test_disabling_one_application_writes_the_whole_matrix_once(apps):
    """Each toggle is a field of a single DeviceConfig — sending one alone reverts the rest."""
    apps.handle_set(C.app_key(C.NFC, "PIV"), flip(apps, nfc_piv=False))
    assert len(apps.written) == 1
    config, reboot = apps.written[0]
    from yubikit.management import CAPABILITY, TRANSPORT

    assert not config.enabled_capabilities[TRANSPORT.NFC] & CAPABILITY.PIV
    assert config.enabled_capabilities[TRANSPORT.NFC] & CAPABILITY.OATH
    assert config.enabled_capabilities[TRANSPORT.USB] & CAPABILITY.PIV


def test_an_nfc_change_never_asks_for_a_re_plug(apps):
    """Only the USB interface set can change, so only USB can need a reboot."""
    apps.handle_set(C.app_key(C.NFC, "FIDO2"), flip(apps, nfc_fido2=False))
    assert apps.written[0][1] is False


def test_a_usb_change_that_removes_an_interface_asks_for_a_re_plug(apps):
    """Disabling every CCID application drops the CCID interface, so the key re-enumerates."""
    result = apps.handle_set(
        C.app_key(C.USB, "OATH"), flip(apps, usb_oath=False, usb_piv=False, usb_openpgp=False)
    )
    assert apps.written[0][1] is True
    # No instruction to the user: the capability declares `reboots`, so the shell waits for the
    # key to come back and reopens it. Telling someone to press Rescan was the hand-rolled
    # version of machinery that already existed.
    assert "restarting" in result
    assert "Rescan" not in result


def test_a_usb_change_that_keeps_the_same_interfaces_does_not(apps):
    """OATH off while PIV stays on leaves CCID present — nothing re-enumerates."""
    apps.handle_set(C.app_key(C.USB, "OATH"), flip(apps, usb_oath=False))
    assert apps.written[0][1] is False


def test_the_last_way_in_cannot_be_switched_off(apps):
    """Yubico's own rule: leaving only smartcard applications over USB is a one-way door.

    Nothing here speaks smartcard, and neither does ykman's OTP or FIDO path — so there would be
    no way to switch anything back on, from this application or any other.
    """
    with pytest.raises(DeviceError, match="At least one"):
        apps.handle_set(
            C.app_key(C.USB, "OTP"),
            flip(apps, usb_otp=False, usb_u2f=False, usb_fido2=False),
        )
    assert apps.written == []


def test_keeping_any_one_of_the_three_is_enough(apps):
    for keep in ("otp", "u2f", "fido2"):
        apps.written.clear()
        changes = {f"usb_{n}": (n == keep) for n in ("otp", "u2f", "fido2")}
        apps.handle_set(C.app_key(C.USB, "OTP"), flip(apps, **changes))
        assert len(apps.written) == 1, keep


def test_an_unchanged_toggle_keeps_what_the_key_reports(apps):
    """A partial payload must not be read as "everything absent is off"."""
    apps.handle_set(C.app_key(C.NFC, "PIV"), {C.app_key(C.NFC, "PIV"): False})
    from yubikit.management import CAPABILITY, TRANSPORT

    config = apps.written[0][0]
    assert config.enabled_capabilities[TRANSPORT.USB] & CAPABILITY.OATH
    assert config.enabled_capabilities[TRANSPORT.NFC] & CAPABILITY.OATH


def test_the_confirm_text_names_what_stops_working(apps):
    fido2 = next(
        c for c in apps.extra_capabilities() if c.key == C.app_key(C.USB, "FIDO2")
    )
    assert "passkey" in fido2.confirm_detail
    assert "re-plug" in fido2.confirm_detail
    openpgp = next(
        c for c in apps.extra_capabilities() if c.key == C.app_key(C.NFC, "OPENPGP")
    )
    assert "Kleopatra" in openpgp.confirm_detail
    assert "re-plug" not in openpgp.confirm_detail


def test_a_locked_key_says_why_the_toggles_will_not_take(device):
    device._yk = fake_info(is_locked=True)
    device._apps = device._applications(fake_info())
    device._accounts = OATH.Accounts()
    first = next(c for c in device.extra_capabilities() if c.group == C.GROUP_APPS)
    assert "lock code" in first.note


def test_interfaces_are_derived_from_applications_exactly_as_ykman_does_it():
    from yubikit.management import CAPABILITY

    from hardware_ui.modules.yubikeys.device import _interfaces

    assert _interfaces(CAPABILITY.OATH) == _interfaces(CAPABILITY.PIV)  # both CCID
    assert _interfaces(CAPABILITY.FIDO2) != _interfaces(CAPABILITY.OATH)
    assert _interfaces(CAPABILITY.OATH | CAPABILITY.PIV) == _interfaces(CAPABILITY.OATH)


# --------------------------------------------------------------------------- interface reclaim


def test_the_slots_are_not_read_during_the_handshake(device):
    """The performance rule, as a test.

    A YubiKey serves one USB interface at a time and takes ~3 s to hand over. Everything else on
    the page lives on the FIDO interface, so reading the slots inline made every connect cost
    three seconds and every application toggle six. It happens just after instead — not behind a
    button, which the Yubico Authenticator does not do and which reads as broken.
    """
    device._yk = fake_info()
    device._yk_rows = {}
    device._apps = []
    rows = [c for c in device.extra_capabilities() if c.group == C.GROUP_SLOTS]
    assert [c.key for c in rows] == [C.SLOTS_UNAVAILABLE_KEY]
    assert "one USB interface at a time" in rows[0].note
    assert device._slot_values() == {C.SLOTS_UNAVAILABLE_KEY: "Reading…"}


def test_the_slots_arrive_on_their_own_through_the_change_stream(device, monkeypatch):
    """No button: connect returns fast, and the tab fills itself a moment later."""
    import asyncio

    from hardware_ui.modules.yubikeys import device as mod

    device._yk = fake_info()
    device._apps = []
    monkeypatch.setattr(mod, "RECLAIM_WAIT", 0)
    monkeypatch.setattr(
        OTP, "read_state", lambda serial: {1: OTP.SlotState(True), 2: OTP.SlotState(False)}
    )
    device._dev = object()

    device._accounts = OATH.Accounts()

    async def run():
        before = device.capabilities_revision
        await device._load_deferred()
        change = await asyncio.wait_for(device._pushed.get(), 1)
        return before, change

    before, change = asyncio.run(run())
    assert device._slots_read
    assert change.key == C.slot_key(1)
    # The shell repaints on a revision change, which is how the new rows reach the screen.
    assert device.capabilities_revision != before
    assert C.chalresp_key(2) in {c.key for c in device.extra_capabilities()}


def test_reading_the_slots_is_an_explicit_action(device, monkeypatch):
    device._yk = fake_info()
    device._apps = []
    monkeypatch.setattr(device, "_repaint", lambda: None)
    monkeypatch.setattr(
        OTP, "read_state", lambda serial: {1: OTP.SlotState(True), 2: OTP.SlotState(False)}
    )
    assert device.handle_set(C.READ_SLOTS_KEY, None) == "Read from the key."
    assert device._slots_read
    keys = [c.key for c in device.extra_capabilities() if c.group == C.GROUP_SLOTS]
    assert C.chalresp_key(2) in keys


def test_rebuilding_the_page_never_touches_the_otp_interface(slots, monkeypatch):
    """`_read_slots` is a lookup. A device read hidden in a rebuild costs a reclaim every time."""
    def boom(*a, **kw):
        raise AssertionError("the OTP interface was opened during a rebuild")

    monkeypatch.setattr(OTP, "read_state", boom)
    monkeypatch.setattr(OTP, "read_all_states", boom)
    slots._slots_by_serial = {fake_info().serial: dict(slots._slots)}
    slots._read_slots()
    assert slots._slots


def test_the_base_module_reads_pin_retries_once_not_on_every_rebuild():
    """Same rule one level down: `_rebuild()` used to call the key for the retry counter.

    On a YubiKey that turned every redraw into a CTAP round trip — and straight after an OTP read,
    into a three-second interface hand-over.
    """
    import inspect

    from hardware_ui.modules.fido2_security_keys.device import Fido2SecurityKey

    assert "_read_pin_retries" in inspect.getsource(Fido2SecurityKey._connect_sync)
    assert "_read_pin_retries" not in inspect.getsource(Fido2SecurityKey._describe)


def test_each_dialog_carries_its_own_modifiers(slots):
    """What Yubico's dialogs do: the switch and the picker live with the field they change."""
    def fields(key):
        return {f.key for f in slots.capabilities_for_test(key).prompt_fields}

    assert fields(C.chalresp_key(2)) == {C.F_SECRET, C.F_TOUCH, C.F_ACCESS}
    assert fields(C.hotp_key(2)) == {C.F_SECRET, C.F_DIGITS, C.F_ACCESS}
    assert fields(C.static_key(2)) == {C.F_PASSWORD, C.F_LAYOUT, C.F_ACCESS}
    assert fields(C.yubiotp_key(2)) == {
        C.F_PUBLIC_ID, C.F_PRIVATE_ID, C.F_KEY, C.F_ACCESS
    }


def test_the_secret_fields_are_masked_and_counted(slots):
    """Yubico shows a reveal button and an x/40 counter; both come from the field declaration."""
    secret = next(
        f for f in slots.capabilities_for_test(C.chalresp_key(2)).prompt_fields
        if f.key == C.F_SECRET
    )
    assert secret.secret and secret.optional and secret.max_length == 40 and secret.generate


# --------------------------------------------------------------------------- HOTP and Yubico OTP


@pytest.fixture
def slots2(slots, monkeypatch):
    for name in ("program_hotp", "program_yubiotp"):
        monkeypatch.setattr(
            OTP, name,
            lambda *a, _n=name, **kw: (
                slots.calls.append((_n, a, kw))
                or (("vvccccnjfvbg", "aa" * 6, "bb" * 16) if _n == "program_yubiotp" else None)
            ),
        )
    return slots


def test_every_slot_offers_all_four_credential_types(slots):
    """Yubico's own list: Yubico OTP, challenge-response, static password, OATH-HOTP."""
    keys = {c.key for c in slots.extra_capabilities()}
    for slot in (1, 2):
        assert C.chalresp_key(slot) in keys
        assert C.hotp_key(slot) in keys
        assert C.yubiotp_key(slot) in keys
        assert C.static_key(slot) in keys


def test_the_challenge_response_button_says_challenge_response(slots):
    """"Programme HMAC-SHA1" named the algorithm, which is not what anyone looks for."""
    cap = slots.capabilities_for_test(C.chalresp_key(2))
    assert "challenge-response" in cap.action_label.lower()
    assert "hmac" not in cap.action_label.lower()


def test_an_oath_secret_takes_base32_hex_or_nothing():
    """Base32 is what a service hands you; hex is what the rest of this page speaks."""
    assert len(OTP.parse_oath_secret("")) == OTP.SECRET_BYTES
    assert OTP.parse_oath_secret("ab" * 20) == bytes.fromhex("ab" * 20)
    import base64

    raw = bytes(range(20))
    assert OTP.parse_oath_secret(base64.b32encode(raw).decode()) == raw
    with pytest.raises(DeviceError, match="not a usable secret"):
        OTP.parse_oath_secret("!!!!")


def test_hotp_passes_the_chosen_code_length(slots2):
    result = slots2.handle_set(C.hotp_key(2), {C.F_SECRET: "ab" * 20, C.F_DIGITS: 8})
    assert "8-digit" in result
    assert slots2.calls[-1][2]["digits8"] is True

    slots2.handle_set(C.hotp_key(2), {C.F_SECRET: "ab" * 20, C.F_DIGITS: 6})
    assert slots2.calls[-1][2]["digits8"] is False


def test_a_generated_hotp_secret_comes_back_as_base32(slots2):
    """Base32 because that is the form whatever checks the codes will want to be given."""
    import base64

    result = slots2.handle_set(C.hotp_key(2), {C.F_SECRET: ""})
    secret = slots2.calls[-1][1][2]
    assert base64.b32encode(secret).decode().rstrip("=") in result
    assert "cannot be read back" in result


def test_a_supplied_hotp_secret_is_not_echoed(slots2):
    assert "cannot be read back" not in slots2.handle_set(
        C.hotp_key(2), {C.F_SECRET: "ab" * 20}
    )


def test_yubico_otp_returns_all_three_values_and_uploads_nothing(slots2):
    """Generated here and shown once: without registering them the codes verify nowhere."""
    result = slots2.handle_set(C.yubiotp_key(2), {})
    assert "vvccccnjfvbg" in result
    assert "aa" * 6 in result
    assert "bb" * 16 in result
    assert "Nothing was uploaded" in result


def test_yubico_otp_needs_a_serial_for_its_public_identity(device, monkeypatch):
    """The public id is derived from the serial, the way `ykman --serial-public-id` does it."""
    monkeypatch.setattr(OTP, "_put", lambda *a, **kw: None)
    with pytest.raises(NotSupported, match="serial number"):
        OTP.program_yubiotp(None, 2, access_code=None)


def test_the_public_identity_matches_ykmans_own_derivation():
    import struct

    from yubikit.core.otp import modhex_encode

    assert modhex_encode(b"\xff\x00" + struct.pack(b">I", 12078869)) == "vvccccnjfvbg"


def test_a_typed_public_identity_is_used_instead_of_the_serial(monkeypatch):
    """Yubico's dialog lets you supply all three; only what is left empty gets generated."""
    seen: list = []
    monkeypatch.setattr(OTP, "_put", lambda *a, **kw: seen.append(a))
    public, private, key = OTP.program_yubiotp(
        12078869, 2, public_id="cccccccccccb", private_id="00" * 6, key="11" * 16,
        access_code=None,
    )
    assert public == "cccccccccccb"
    assert private == "00" * 6
    assert key == "11" * 16


def test_a_bad_public_identity_is_refused_before_the_key_sees_it(monkeypatch):
    monkeypatch.setattr(OTP, "_put", lambda *a, **kw: None)
    with pytest.raises(DeviceError, match="modhex"):
        OTP.program_yubiotp(1, 2, public_id="zzzz", access_code=None)
    with pytest.raises(DeviceError, match="32 hex"):
        OTP.program_yubiotp(1, 2, key="abcd", access_code=None)


def test_the_multi_field_dialog_builds(qapp):
    """The whole point of PromptField is a dialog; nothing else proves it is well formed."""
    from hardware_ui.core import Choice, Kind, PromptField
    from hardware_ui.shell.window import MainWindow

    window = MainWindow()
    fields = (
        PromptField(key="s", label="Secret key", secret=True, max_length=40, generate=True),
        PromptField(key="t", label="Require touch", kind=Kind.TOGGLE),
        PromptField(
            key="d", label="Code length", kind=Kind.CHOICE, default=6,
            choices=(Choice(6, "6 digits"), Choice(8, "8 digits")),
        ),
    )
    assert callable(window.ask_fields)
    # Exercise the widget construction without showing the modal.
    holder: dict = {}
    for field in fields:
        if field.kind is Kind.TEXT:
            window._text_field(field, holder)
    assert "s" in holder


# --------------------------------------------------------------------------- OATH accounts

from hardware_ui.modules.yubikeys import oath as OATH  # noqa: E402


def test_no_ykman_import_at_module_scope_in_oath():
    top = [
        line for line in inspect.getsource(OATH).splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]
    assert not any("ykman" in line or "yubikit" in line for line in top), top


def test_the_smartcard_is_opened_per_operation_and_never_held():
    """`ykman` takes the smartcard exclusively, so holding it locks gpg-agent out of the card.

    Every entry point goes through `session`, which is a context manager -- there is no code path
    that keeps a connection between calls.
    """
    source = inspect.getsource(OATH)
    assert "@contextmanager" in source
    for func in ("def read(", "def add(", "def delete(", "def code_for("):
        body = source[source.index(func):]
        body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
        assert "with session(" in body, func


def test_a_busy_card_names_the_program_that_has_it():
    assert "gpg-agent" in OATH.BUSY_HINT
    assert "gpgconf --reload scdaemon" in OATH.BUSY_HINT


def test_a_secret_that_is_not_base32_is_refused_before_the_key_sees_it():
    with pytest.raises(DeviceError, match="base32"):
        OATH.add(
            1, issuer="", name="a", secret="!!!!", oath_type="TOTP", algorithm="SHA1",
            digits=6, period=30, touch=False,
        )


def test_an_account_needs_a_name_and_a_secret():
    for kwargs, match in (
        ({"name": "", "secret": "GEZDGNBV"}, "name is required"),
        ({"name": "a", "secret": ""}, "secret key is required"),
    ):
        with pytest.raises(DeviceError, match=match):
            OATH.add(
                1, issuer="", oath_type="TOTP", algorithm="SHA1", digits=6, period=30,
                touch=False, **kwargs,
            )


def test_the_add_dialog_offers_what_the_key_supports(device):
    """Time or counter based, SHA-1/256/512, 20/30/45/60 s, 6 or 8 digits — and a touch switch."""
    device._yk = fake_info()
    device._accounts = OATH.Accounts()
    fields = {
        f.key: f
        for c in device.extra_capabilities()
        if c.key == C.ADD_ACCOUNT_KEY
        for f in c.prompt_fields
    }
    assert [c.value for c in fields[C.F_PERIOD].choices] == list(OATH.PERIODS)
    assert [c.value for c in fields[C.F_ALGORITHM].choices] == list(OATH.ALGORITHMS)
    assert [c.value for c in fields[C.F_DIGITS].choices] == list(OATH.DIGITS)
    assert [c.value for c in fields[C.F_TYPE].choices] == ["TOTP", "HOTP"]
    assert fields[C.F_TOUCH].kind is Kind.TOGGLE
    assert fields[C.F_NAME].max_length == 64
    assert fields[C.F_ISSUER].optional


def test_a_locked_key_offers_only_an_unlock(device):
    device._yk = fake_info()
    device._accounts = OATH.Accounts(has_password=True, locked=True)
    rows = [c for c in device.extra_capabilities() if c.group == C.GROUP_ACCOUNTS]
    assert [c.key for c in rows] == [C.UNLOCK_KEY]
    assert rows[0].prompt == "secret"


def test_the_oath_password_does_not_outlive_the_connection(device):
    """Same rule as the FIDO PIN one level down."""
    import asyncio

    device._oath_password = "hunter2"
    device._dev = None
    asyncio.run(device.disconnect())
    assert device._oath_password == ""


def test_a_touch_account_shows_a_prompt_rather_than_a_code(device):
    device._yk = fake_info()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="aa", issuer="GitHub", name="me", oath_type="TOTP", code="123456"),
            OATH.Account(key="bb", issuer="", name="touchy", oath_type="TOTP", touch=True),
        )
    )
    values = device._account_values()
    assert values[C.account_key("aa")] == "123456"
    assert values[C.account_key("bb")] == "Needs a touch"
    # Only the touch one gets a "show the code" button -- the rest already have theirs.
    codes = [c.key for c in device.extra_capabilities() if c.key.startswith(C.ACCOUNT_CODE_PREFIX)]
    assert codes == [C.account_code_key("bb")]


def test_accounts_are_labelled_issuer_then_name():
    assert C.account_label("GitHub", "me") == "GitHub: me"
    assert C.account_label("", "me") == "me"


# --------------------------------------------------------------------------- live codes


def test_the_next_read_is_scheduled_from_the_key_not_from_a_constant():
    """Accounts may be 20, 30, 45 or 60 seconds and do not share a boundary."""
    accounts = OATH.Accounts(
        items=(
            OATH.Account(key="a", issuer="", name="slow", oath_type="TOTP", valid_to=200),
            OATH.Account(key="b", issuer="", name="fast", oath_type="TOTP", valid_to=100),
        )
    )
    assert accounts.expires_at == 100  # the soonest to go stale, not the first in the list


def test_a_key_with_nothing_time_based_schedules_no_refresh():
    """Counter-based and touch-required credentials never expire on their own."""
    assert OATH.Accounts().expires_at == 0
    assert OATH.Accounts(
        items=(
            OATH.Account(key="a", issuer="", name="hotp", oath_type="HOTP"),
            OATH.Account(key="b", issuer="", name="touch", oath_type="TOTP", touch=True),
        )
    ).expires_at == 0


def test_the_refresh_aims_past_the_expiry_not_at_it():
    """Asking a moment early returns the code that is about to die."""
    from hardware_ui.modules.yubikeys.device import CODE_MARGIN

    assert CODE_MARGIN > 0


def test_a_code_refresh_pushes_values_rather_than_rebuilding_the_page(device, monkeypatch):
    """The account list is unchanged 30 seconds later, so the tab must not flicker."""
    import asyncio

    device._yk = fake_info()
    device._dev = object()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(
                key="aa", issuer="GitHub", name="me", oath_type="TOTP", code="111111",
                valid_to=1,
            ),
        )
    )
    repaints: list[int] = []
    monkeypatch.setattr(device, "_repaint", lambda: repaints.append(1))

    def refreshed():
        device._accounts = OATH.Accounts(
            items=(
                OATH.Account(
                    key="aa", issuer="GitHub", name="me", oath_type="TOTP", code="222222",
                ),
            )
        )

    monkeypatch.setattr(device, "_refresh_accounts", refreshed)

    async def run():
        await asyncio.wait_for(device._code_loop(), 5)
        return [device._pushed.get_nowait() for _ in range(device._pushed.qsize())]

    pushed = {c.key: c.value for c in asyncio.run(run())}
    assert pushed[C.account_key("aa")] == "222222"
    # The countdown goes with it: a fresh code beside a stale "1 s" is worse than either.
    assert C.account_expires_key("aa") in pushed
    assert repaints == [], "an unchanged account list must not rebuild the page"


def test_an_added_or_removed_account_does_rebuild_the_page(device, monkeypatch):
    import asyncio

    device._yk = fake_info()
    device._dev = object()
    device._accounts = OATH.Accounts(
        items=(OATH.Account(key="aa", issuer="", name="a", oath_type="TOTP", valid_to=1),)
    )
    repaints: list[int] = []
    monkeypatch.setattr(device, "_repaint", lambda: repaints.append(1))
    monkeypatch.setattr(
        device, "_refresh_accounts",
        lambda: setattr(device, "_accounts", OATH.Accounts()),
    )
    asyncio.run(asyncio.wait_for(device._code_loop(), 5))
    assert repaints == [1]


def test_a_code_row_gets_a_copy_button_and_a_touch_row_does_not(device):
    """A six-digit code that is replaced every 30 s cannot usefully be selected with a mouse."""
    device._yk = fake_info()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="aa", issuer="GitHub", name="me", oath_type="TOTP",
                         code="123456", period=30, valid_to=1),
            OATH.Account(key="bb", issuer="", name="touchy", oath_type="TOTP", touch=True),
        )
    )
    rows = {c.key: c for c in device.extra_capabilities()}
    assert rows[C.account_key("aa")].copyable
    assert not rows[C.account_key("bb")].copyable  # nothing to copy until it is fetched


def test_a_countdown_is_offered_only_where_something_expires(device):
    """Superseded the standalone row: the countdown now rides on the codes themselves."""
    device._yk = fake_info()
    device._accounts = OATH.Accounts(
        items=(OATH.Account(key="a", issuer="", name="h", oath_type="HOTP"),)
    )
    rows = {c.key: c for c in device.extra_capabilities()}
    assert rows[C.account_key("a")].suffix_from == ""
    assert C.account_expires_key("a") not in device._account_values()

    device._accounts = OATH.Accounts(
        items=(OATH.Account(key="a", issuer="", name="t", oath_type="TOTP",
                            code="1", period=30, valid_to=1),)
    )
    rows = {c.key: c for c in device.extra_capabilities()}
    assert rows[C.account_key("a")].suffix_from == C.account_expires_key("a")
    assert C.account_expires_key("a") in device._account_values()


def test_the_countdown_never_shows_a_negative_or_a_stuck_zero():
    import time

    from hardware_ui.modules.yubikeys.device import _seconds_left

    assert _seconds_left(int(time.time()) - 30) == 0
    assert _seconds_left(int(time.time()) + 29) in (29, 30)


def test_the_period_is_shown_beside_each_code(device):
    device._yk = fake_info()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="a", issuer="", name="t", oath_type="TOTP", code="1",
                         period=60, valid_to=1),
            OATH.Account(key="b", issuer="", name="h", oath_type="HOTP", code="2"),
        )
    )
    rows = {c.key: c for c in device.extra_capabilities()}
    assert rows[C.account_key("a")].description == "Time based, 60 s."
    assert rows[C.account_key("b")].description == "Counter based."


def test_copying_takes_the_value_not_what_is_on_screen(qapp):
    """The label may carry a unit; the clipboard should get the value."""
    from PyQt6.QtWidgets import QApplication

    from hardware_ui.core import Capability, CapabilitySet, Kind
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet([
        Capability(key="k", kind=Kind.READOUT, label="Code", writable=False, copyable=True,
                   unit="s"),
    ]))
    form.set_value("k", 42, confirmed=True)
    form._copy("k")
    assert QApplication.clipboard().text() == "42"


def test_no_tab_repeats_a_section_heading(slots, monkeypatch):
    """The form emits a heading when the section changes, so a repeat means a second header.

    Written after the Accounts tab grew a second "Accounts" heading below the delete buttons —
    the same mistake already fixed once on the vendor tab, made again in a new place.
    """
    slots._yk = fake_info()
    slots._yk_rows = {"firmware": "5.2.6"}
    slots._apps = slots._applications(fake_info())
    slots._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="aa", issuer="GitHub", name="me", oath_type="TOTP",
                         code="1", period=30, valid_to=1),
            OATH.Account(key="bb", issuer="", name="touchy", oath_type="TOTP", touch=True),
        )
    )
    ordered = slots._in_reading_order(base_page() + list(slots.extra_capabilities()))
    for group in dict.fromkeys(c.group for c in ordered):
        seen: list[str] = []
        for cap in [c for c in ordered if c.group == group]:
            if not seen or seen[-1] != cap.section:
                seen.append(cap.section)
        assert len(seen) == len(set(seen)), (group, seen)


def test_the_countdown_rides_on_each_code_rather_than_a_row_of_its_own(device):
    device._yk = fake_info()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="a", issuer="", name="t", oath_type="TOTP", code="1",
                         period=30, valid_to=1),
            OATH.Account(key="b", issuer="", name="h", oath_type="HOTP", code="2"),
        )
    )
    rows = {c.key: c for c in device.extra_capabilities()}
    assert C.account_expires_key("a") not in rows, "the countdown must not be a row"
    assert rows[C.account_key("a")].suffix_from == C.account_expires_key("a")
    assert rows[C.account_key("a")].suffix_total == 30
    # Counter-based codes do not expire, so nothing is shown after them.
    assert rows[C.account_key("b")].suffix_from == ""
    # Present, not merely truthy: an expired code counts down to nought and must still be sent.
    assert C.account_expires_key("a") in device._account_values()


def test_a_suffix_value_repaints_the_rows_that_show_it(qapp):
    """It has no row of its own, so `set_value` has to find its dependants."""
    from hardware_ui.core import Capability, CapabilitySet, Kind
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet([
        Capability(key="code", kind=Kind.READOUT, label="Code", writable=False,
                   copyable=True, suffix_from="left"),
    ]))
    form.set_value("code", "123456", confirmed=True)
    row = form._rows["code"]
    label = getattr(row.control, "readout", row.control)
    assert label.text() == "123456"
    form.set_value("left", "12 s")
    assert "123456" in label.text() and "12 s" in label.text()
    # The suffix is decoration: copying still gives the value alone.
    form._copy("code")
    from PyQt6.QtWidgets import QApplication

    assert QApplication.clipboard().text() == "123456"


def test_the_copy_button_is_an_icon_not_a_word(qapp):
    """It sits at the end of every code; a five-letter label is wider than the value."""
    from PyQt6.QtWidgets import QToolButton

    from hardware_ui.core import Capability, CapabilitySet, Kind
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet([
        Capability(key="k", kind=Kind.READOUT, label="Code", writable=False, copyable=True),
    ]))
    button = form._rows["k"].control.findChild(QToolButton)
    assert button.text() != "Copy"
    assert button.toolTip()
    assert button.autoRaise()


def test_a_suffix_push_reaches_the_form_that_has_no_row_for_it(qapp):
    """The regression that froze the countdown.

    `_form_for` finds the form owning a *row* with that key. A suffix source has no row anywhere,
    so every per-second push was dropped and the number sat still. The shell now offers a pushed
    value to every form and lets each ignore what it does not use.
    """
    from hardware_ui.core import Capability, CapabilitySet, Kind
    from hardware_ui.shell.form import CapabilityForm

    owning = CapabilityForm()
    owning.build(CapabilitySet([
        Capability(key="code", kind=Kind.READOUT, label="Code", writable=False,
                   suffix_from="left"),
    ]))
    unrelated = CapabilityForm()
    unrelated.build(CapabilitySet([
        Capability(key="other", kind=Kind.READOUT, label="Other", writable=False),
    ]))

    assert "left" not in owning.keys(), "suffix source must own no row"  # noqa: SIM118
    owning.set_value("code", "123456", confirmed=True)

    for form in (owning, unrelated):          # what the shell does with every pushed change
        form.set_value("left", "7 s")

    label = getattr(owning._rows["code"].control, "readout", owning._rows["code"].control)
    assert "7 s" in label.text()
    # A form with no row for the key still *records* it. It draws nothing — drawing is driven by
    # rows — but a row on this tab may be gated on a capability that belongs to another one, and
    # discarding the value left every such row disabled for ever. See the cross-group gate test.
    assert unrelated.value_of("left") == "7 s"
    assert unrelated.keys() == ["other"], "recording a value must not invent a row"


def test_each_account_counts_down_on_its_own_clock(device):
    """A 60-second account beside a 30-second one must not be told the other's time."""
    device._yk = fake_info()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="a", issuer="", name="fast", oath_type="TOTP", code="1",
                         period=30, valid_to=2_000_000_100),
            OATH.Account(key="b", issuer="", name="slow", oath_type="TOTP", code="2",
                         period=60, valid_to=2_000_000_130),
        )
    )
    rows = {c.key: c for c in device.extra_capabilities()}
    assert rows[C.account_key("a")].suffix_total == 30
    assert rows[C.account_key("b")].suffix_total == 60
    assert rows[C.account_key("a")].suffix_from != rows[C.account_key("b")].suffix_from
    values = device._account_values()
    assert values[C.account_expires_key("a")] != values[C.account_expires_key("b")]


def test_a_countdown_suffix_draws_a_bar_as_well_as_a_number(qapp):
    """A number says how long is left; a bar says it at a glance."""
    from PyQt6.QtWidgets import QProgressBar

    from hardware_ui.core import Capability, CapabilitySet, Kind
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet([
        Capability(key="code", kind=Kind.READOUT, label="Code", writable=False,
                   suffix_from="left", suffix_total=30, copyable=True),
    ]))
    form.set_value("code", "123456", confirmed=True)
    form.set_value("left", 12)
    control = form._rows["code"].control
    label = getattr(control, "readout", control)
    bar = control.findChild(QProgressBar)
    assert "12 s" in label.text()
    assert (bar.value(), bar.maximum()) == (12, 30)


def test_a_suffix_without_a_total_stays_plain_text(qapp):
    from PyQt6.QtWidgets import QProgressBar

    from hardware_ui.core import Capability, CapabilitySet, Kind
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(CapabilitySet([
        Capability(key="k", kind=Kind.READOUT, label="K", writable=False,
                   suffix_from="s", copyable=True),
    ]))
    form.set_value("k", "x", confirmed=True)
    form.set_value("s", "later")
    assert form._rows["k"].control.findChild(QProgressBar) is None


def test_every_countdown_value_occupies_exactly_one_second():
    """The bug class that produced a two-second stall at the boundary.

    `int(x) + 1` truncates *towards zero*, so once the remainder went negative it rounded the
    wrong way and held "1 s" from a second before expiry until a second after. Sampled across a
    whole period, each value must appear over exactly one second and nought must arrive at expiry.
    """
    from hardware_ui.modules.yubikeys import device as mod

    expires = 10_000.0
    seen: dict[int, list[float]] = {}
    now = expires - 30.0
    while now < expires + 2.0:
        mod.time.time = lambda n=now: n  # type: ignore[assignment]
        seen.setdefault(mod._seconds_left(int(expires)), []).append(now)
        now = round(now + 0.1, 1)
    import time as real_time

    mod.time.time = real_time.time  # type: ignore[assignment]

    for value, moments in seen.items():
        if value in (0, 30):
            continue  # the clamped ends are open-ended by design
        span = max(moments) - min(moments)
        assert span < 1.05, f"{value} s was shown for {span:.1f} s"
    assert min(seen[0]) >= expires - 0.05, "nought must not appear before the code expires"


def test_the_countdown_sequence_over_a_whole_period(device, monkeypatch):
    """Watch the pushes, not the wiring.

    Every earlier test here asserted structure at an instant -- that a key was declared, that a
    suffix pointed somewhere. None of them could see a number that stalls, repeats, or restarts
    late, which is why three separate timing defects reached the user. This consumes the stream.
    """
    import asyncio

    from hardware_ui.modules.yubikeys import device as mod

    clock = {"now": 1_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    monkeypatch.setattr(mod, "RECLAIM_WAIT", 0)

    async def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)

    device._yk = fake_info()
    device._dev = object()
    device._accounts = OATH.Accounts(
        items=(
            OATH.Account(key="a", issuer="", name="t", oath_type="TOTP", code="000000",
                         period=30, valid_to=1_030),
        )
    )
    monkeypatch.setattr(device, "_repaint", lambda: None)
    rounds = {"n": 0}

    def refresh() -> None:
        rounds["n"] += 1
        if rounds["n"] > 1:
            raise asyncio.CancelledError
        device._accounts = OATH.Accounts(
            items=(
                OATH.Account(key="a", issuer="", name="t", oath_type="TOTP", code="111111",
                             period=30, valid_to=1_060),
            )
        )

    monkeypatch.setattr(device, "_refresh_accounts", refresh)

    async def run():
        with contextlib.suppress(asyncio.CancelledError):
            await device._code_loop()
        return [
            device._pushed.get_nowait() for _ in range(device._pushed.qsize())
        ]

    import contextlib

    pushed = asyncio.run(run())
    counts = [c.value for c in pushed if c.key.startswith("oath.expires.")]

    assert counts[0] == 30, counts[:3]
    assert 0 in counts, "the countdown must reach nought"
    first = counts[: counts.index(0) + 1]
    assert first == sorted(first, reverse=True), first
    assert len(first) == len(set(first)), f"a value was sent twice: {first}"
    assert counts[counts.index(0) + 1] == 30, "it must restart at the full period"


def test_the_application_toggles_declare_that_they_restart_the_key(device):
    """So the shell tolerates the dropped link and reopens the key, rather than erroring."""
    device._yk = fake_info()
    device._apps = device._applications(fake_info())
    rows = [
        c for c in device.extra_capabilities()
        if c.group == C.GROUP_APPS and c.section != C.SECTION_TIMING
    ]
    assert rows and all(c.reboots for c in rows)
    # On every toggle, not only the USB ones: they are written as one message, so an NFC click
    # carries any staged USB change with it.
    assert {c.section for c in rows} == {"Over USB", "Over NFC"}


def test_a_restart_write_reconnects_however_it_ends(qapp):
    """A write that restarts a device cannot report whether it applied.

    The reply and the link go together, so an error means "we do not know", not "it did not
    happen". Returning early on one left the device closed with the user pressing Connect by hand
    — the exact bug this path exists to prevent.
    """
    import asyncio

    from hardware_ui.core import DeviceError, Unreachable
    from hardware_ui.shell.app import Controller

    class Form:
        def set_pending(self, *a, **k): ...
        def clear_pending(self, *a, **k): ...
        def set_value(self, *a, **k): ...
        def value_of(self, _k): return None

    class Info:
        uid = "hid:3-3"

    class Dev:
        info = Info()

        def __init__(self, raises):
            self.raises = raises

        async def set(self, _k, _v):
            if self.raises:
                raise self.raises
            return "restarting"

    async def run(raises):
        controller = Controller.__new__(Controller)
        controller._selected = Info()
        controller._open = {"hid:3-3": Dev(raises)}
        # `page.publish` rather than a single form: a value is announced to every tab, because a
        # tab may be gated on a capability that belongs to another group.
        controller._window = type("W", (), {
            "notify": staticmethod(lambda *a, **k: None),
            "page": type("P", (), {
                "publish": staticmethod(lambda *a, **k: None),
                "publish_advisories": staticmethod(lambda *a, **k: None),
                "publish_pending": staticmethod(lambda *a, **k: None),
                "publish_clear_pending": staticmethod(lambda *a, **k: None),
                "publish_result": staticmethod(lambda *a, **k: None),
                "clear_result": staticmethod(lambda *a, **k: None),
                "all_keys": staticmethod(lambda: []),
            })(),
        })()
        controller._ui = lambda fn, *a, **k: None
        controller._form_for = lambda _k: Form()
        controller._group = lambda k: [k]
        reconnected = []

        async def fake(uid):
            reconnected.append(uid)

        controller._reconnect_after_reboot = fake
        await controller._write_rebooting("app.usb.OTP", False)
        return reconnected

    for raises in (None, OSError("link dropped"), Unreachable("gone"), DeviceError("SET_REPORT")):
        assert asyncio.run(run(raises)) == ["hid:3-3"], f"no reconnect after {raises!r}"


def test_a_restarting_key_does_not_report_the_dropped_link_as_a_failure(device, monkeypatch):
    """The module asked the key to restart; the transport failing is the expected outcome."""
    device._yk = fake_info()
    device._apps = device._applications(fake_info())
    device._dev = object()

    class Dropping:
        def __init__(self, _dev): ...

        def write_device_config(self, *_a, **_k):
            raise OSError("[Errno 32] Broken pipe")

    monkeypatch.setattr("yubikit.management.ManagementSession", Dropping)
    monkeypatch.setattr(device, "_reopen", lambda: None)
    monkeypatch.setattr(device, "_repaint", lambda: None)

    payload = {C.app_key(t, n): on for t, n, _, on in device._apps}
    payload[C.app_key(C.USB, "OATH")] = False
    payload[C.app_key(C.USB, "PIV")] = False
    payload[C.app_key(C.USB, "OPENPGP")] = False
    result = device.handle_set(C.app_key(C.USB, "OATH"), payload)
    assert "restarting" in result


# --------------------------------------------------------------------------- timing


def test_the_eject_controls_are_absent_without_a_smartcard(device):
    """Both are about the smartcard, so they say nothing on a key that has none enabled."""
    device._yk = fake_info(usb_enabled=OTP_APP | U2F | FIDO2, nfc_enabled=0)  # no OATH/PIV/OpenPGP
    device._apps = device._applications(fake_info())
    keys = {c.key for c in device.extra_capabilities()}
    assert C.CHALRESP_TIMEOUT_KEY in keys, "challenge-response is an OTP thing, not a CCID one"
    assert C.TOUCH_EJECT_KEY not in keys
    assert C.AUTO_EJECT_KEY not in keys

    device._yk = fake_info()                                   # CCID back on
    keys = {c.key for c in device.extra_capabilities()}
    assert C.TOUCH_EJECT_KEY in keys and C.AUTO_EJECT_KEY in keys


def test_the_timing_fields_are_written_together(device):
    """They are three fields of one DeviceConfig; sending one alone re-sends the others."""
    device._yk = fake_info()
    device._apps = device._applications(fake_info())
    rows = [c for c in device.extra_capabilities() if c.section == C.SECTION_TIMING]
    assert rows
    for row in rows:
        assert len(row.writes_with) == len(rows) - 1, row.key


def test_asking_to_eject_after_a_time_also_turns_the_button_on(device, monkeypatch):
    """ykman's rule: a time to eject after is meaningless unless the button ejects at all."""
    written: list = []

    class Session:
        def __init__(self, _dev): ...

        def write_device_config(self, config, *_a):
            written.append(config)

    device._yk = fake_info()
    device._dev = object()
    monkeypatch.setattr("yubikit.management.ManagementSession", Session)
    monkeypatch.setattr(device, "_reopen", lambda: None)

    device.handle_set(C.AUTO_EJECT_KEY, {C.AUTO_EJECT_KEY: 30})
    assert written[-1].auto_eject_timeout == 30
    assert written[-1].device_flags & C.DEVICE_FLAG_EJECT, "touch-eject must come with it"


def test_clearing_the_time_leaves_the_button_alone(device, monkeypatch):
    """Zero means "do not eject on a timer", not "undo the button setting"."""
    written: list = []

    class Session:
        def __init__(self, _dev): ...

        def write_device_config(self, config, *_a):
            written.append(config)

    info = fake_info()
    info.config.device_flags = C.DEVICE_FLAG_EJECT
    device._yk = info
    device._dev = object()
    monkeypatch.setattr("yubikit.management.ManagementSession", Session)
    monkeypatch.setattr(device, "_reopen", lambda: None)

    device.handle_set(C.AUTO_EJECT_KEY, {C.AUTO_EJECT_KEY: 0})
    assert written[-1].auto_eject_timeout == 0
    assert written[-1].device_flags & C.DEVICE_FLAG_EJECT


def test_a_timing_change_never_restarts_the_key(device, monkeypatch):
    """None of these alters the USB interface set, so the value can be read back."""
    seen: list = []

    class Session:
        def __init__(self, _dev): ...

        def write_device_config(self, _config, reboot, *_a):
            seen.append(reboot)

    device._yk = fake_info()
    device._dev = object()
    monkeypatch.setattr("yubikit.management.ManagementSession", Session)
    monkeypatch.setattr(device, "_reopen", lambda: None)

    device.handle_set(C.CHALRESP_TIMEOUT_KEY, {C.CHALRESP_TIMEOUT_KEY: 20})
    assert seen == [False]
