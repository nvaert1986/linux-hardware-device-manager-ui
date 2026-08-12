"""HID enumeration: one row per physical device, and the right icon on it.

Built against a synthetic sysfs tree shaped like the real one, so these run anywhere. The layout
and the awkward values are taken from a real Razer keyboard and mouse.
"""

from __future__ import annotations

import pytest

from hardware_ui.core import discovery


def build_sysfs(tmp_path, devices):
    """Create /sys/class/hidraw + /sys/bus/usb-style trees.

    *devices* is ``{usb_name: (vid, pid, name, [(subclass, protocol), ...], node_count)}``.
    """
    hidraw = tmp_path / "class" / "hidraw"
    hidraw.mkdir(parents=True)
    node_no = 0
    for usb_name, (vid, pid, name, interfaces, nodes) in devices.items():
        usb = tmp_path / "devices" / usb_name
        usb.mkdir(parents=True)
        (usb / "idVendor").write_text(f"{vid:04x}\n")
        (usb / "idProduct").write_text(f"{pid:04x}\n")
        (usb / "bNumInterfaces").write_text(f"{len(interfaces):2d}\n")
        for i, (subclass, protocol) in enumerate(interfaces):
            iface = usb / f"{usb_name}:1.{i}"
            iface.mkdir()
            (iface / "bInterfaceClass").write_text("03\n")
            (iface / "bInterfaceSubClass").write_text(f"{subclass:02x}\n")
            (iface / "bInterfaceProtocol").write_text(f"{protocol:02x}\n")
        for _ in range(nodes):
            iface = usb / f"{usb_name}:1.0"
            hid = iface / f"0003:{vid:04X}:{pid:04X}.000{node_no}"
            hid.mkdir(parents=True, exist_ok=True)
            (hid / "uevent").write_text(
                f"HID_ID=0003:{vid:08X}:{pid:08X}\nHID_NAME={name}\nHID_UNIQ=\n"
            )
            node = hidraw / f"hidraw{node_no}"
            node.mkdir()
            (node / "device").symlink_to(hid)
            node_no += 1
    return hidraw


#: A BlackWidow Chroma V2: boot keyboard, plus a non-boot interface whose protocol byte is 2.
KEYBOARD = (0x1532, 0x0221, "Razer Razer BlackWidow Chroma V2", [(1, 1), (0, 1), (0, 2)], 3)
#: A DeathAdder V2: it really does advertise a boot *keyboard* interface too, for its macro keys.
MOUSE = (0x1532, 0x0084, "Razer Razer DeathAdder V2", [(1, 2), (0, 1), (1, 1), (0, 2)], 4)


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    def _build(devices):
        root = build_sysfs(tmp_path, devices)
        monkeypatch.setattr(discovery, "SYS_HIDRAW", root)
        return discovery.enumerate_hid()

    return _build


def test_many_nodes_of_one_device_become_one_row(sysfs):
    """Seven hidraw nodes for two Razer devices showed as three keyboards and four mice."""
    found = sysfs({"3-1.1": KEYBOARD, "3-1.2": MOUSE})
    assert len(found) == 2
    assert sorted(len(d.properties["nodes"]) for d in found) == [3, 4]


def test_every_node_is_kept_because_a_module_may_need_a_specific_one(sysfs):
    """Poly's Deckard tunnel lives on exactly one node of several; collapsing must not lose them."""
    found = sysfs({"3-1.2": MOUSE})
    assert len(found[0].properties["nodes"]) == 4
    assert found[0].path in found[0].properties["nodes"]


def test_two_identical_devices_stay_two_rows(sysfs):
    """Grouping on vendor and product id would wrongly merge them; the USB path is the key."""
    found = sysfs({"3-1.1": MOUSE, "3-2.1": MOUSE})
    assert len(found) == 2
    assert found[0].uid != found[1].uid


def test_the_protocol_byte_is_only_read_on_a_boot_interface(sysfs):
    """The keyboard advertises 03:00:02 -- class HID, subclass 0, protocol 2. On subclass 0 the
    protocol byte is unspecified; reading it anyway classified the keyboard as a mouse."""
    found = sysfs({"3-1.1": KEYBOARD})
    assert found[0].properties["hid_kind"] == "keyboard"
    assert found[0].icon == "input-keyboard"


def test_a_mouse_that_also_claims_a_boot_keyboard_is_still_a_mouse(sysfs):
    found = sysfs({"3-1.2": MOUSE})
    assert found[0].properties["hid_kind"] == "mouse"
    assert found[0].icon == "input-mouse"


def test_an_unclassifiable_device_falls_back_to_its_category_icon(sysfs):
    plain = (0x0bda, 0x1100, "Realtek HID Device", [(0, 0)], 1)
    found = sysfs({"3-3.1": plain})
    assert found[0].properties["hid_kind"] == ""
    assert found[0].icon == found[0].category.icon


def test_the_doubled_vendor_prefix_is_dropped(sysfs):
    """sysfs concatenates USB manufacturer and product, and Razer puts the brand in both."""
    found = sysfs({"3-1.1": KEYBOARD})
    assert found[0].name == "Razer BlackWidow Chroma V2"


def test_the_chosen_icon_names_exist_in_breeze():
    from pathlib import Path

    theme = Path("/usr/share/icons/breeze")
    if not theme.is_dir():
        pytest.skip("breeze-icons not installed")
    for name in ("input-keyboard", "input-mouse"):
        assert list(theme.rglob(f"{name}.svg")), name


def test_a_secondary_endpoint_of_one_product_is_distinguishable(sysfs):
    """A WD22TB4 appears twice on USB: the dock itself (HID plus USB Billboard) and a bare
    companion with a single HID interface. Only the first is the dock, and a manifest can say so
    with `usb_multi_interface` — a flag rather than a count, because match rules compare exactly
    and "more than one" cannot be written as a number."""
    dock = (0x413C, 0xB06E, "Dell Thunderbolt Dock WD22TB4", [(0, 0), (0, 0)], 1)
    companion = (0x413C, 0xB06F, "Dell dock", [(0, 0)], 1)
    found = {d.name: d for d in sysfs({"3-1.5": dock, "3-1.3.5": companion})}
    assert found["Dell Thunderbolt Dock WD22TB4"].properties["usb_multi_interface"] == "yes"
    assert found["Dell dock"].properties["usb_multi_interface"] == "no"


def test_the_dock_manifest_claims_every_dell_dock_even_a_secondary_endpoint():
    """Deliberately ungated. Requiring more than one interface would show one row per dock here,
    but it is a guess from a single model — and a dock that exposed one interface would vanish.
    An extra row is cosmetic; a missing dock is not."""
    import tomllib
    from pathlib import Path

    from hardware_ui.core import DeviceInfo, MatchRule, Support, Transport

    raw = tomllib.loads(
        Path("hardware_ui/modules/dell_docks/module.toml").read_text()
    )
    rules = [
        MatchRule(
            transport=Transport(r["transport"]),
            vendor_id=int(r["vendor_id"], 16),
            product_id=int(r["product_id"], 16) if "product_id" in r else None,
            name_glob=r.get("name_glob", ""),
            properties=tuple(sorted((k, str(v)) for k, v in (r.get("properties") or {}).items())),
            support=Support(r.get("status", "family")),
        )
        for r in raw["match"]
    ]

    def info(name, pid, multi):
        return DeviceInfo(
            uid=name, name=name, transport=Transport.HID, vendor_id=0x413C, product_id=pid,
            properties={"usb_multi_interface": multi},
        )

    assert any(r.matches(info("Dell Thunderbolt Dock WD22TB4", 0xB06E, "yes")) for r in rules)
    assert any(r.matches(info("Dell dock", 0xB06F, "no")) for r in rules)
    # A single-interface dock -- which may well exist -- must still be claimed.
    assert any(r.matches(info("Dell WD19 Dock", 0xB06C, "no")) for r in rules)
    # And a Dell keyboard is never claimed as a dock.
    assert not any(r.matches(info("Dell KB216 Wired Keyboard", 0x2113, "yes")) for r in rules)


def test_a_dock_gets_a_dock_icon_not_the_generic_box(sysfs):
    """Breeze has no docking-station icon, so KDE's own Thunderbolt preferences icon stands in --
    it reads as a dock, where the category fallback is an anonymous rectangle. Name-based because
    nothing else identifies a dock before it is opened, and vendor-neutral so another vendor's
    dock gets it too."""
    dell = (0x413C, 0xB06E, "Dell Thunderbolt Dock WD22TB4", [(0, 0), (0, 0)], 1)
    other = (0x17EF, 0x306F, "Lenovo USB-C Dock", [(0, 0)], 1)
    found = {d.name: d for d in sysfs({"3-1.5": dell, "3-2.5": other})}
    # Both are docks; the transport comes from the name and only changes the icon, because
    # Breeze has a Thunderbolt icon and nothing that reads as a USB dock.
    assert found["Dell Thunderbolt Dock WD22TB4"].properties["hid_kind"] == "dock"
    assert found["Dell Thunderbolt Dock WD22TB4"].icon == "preferences-desktop-thunderbolt"
    assert found["Lenovo USB-C Dock"].properties["hid_kind"] == "dock_usb"
    assert found["Lenovo USB-C Dock"].icon == "preferences-devices-tree"
    assert {"dock", "dock_usb"} == set(discovery.DOCK_KINDS)


def test_dock_naming_does_not_swallow_ordinary_devices(sysfs):
    plain = (0x1532, 0x0084, "Razer DeathAdder V2", [(1, 2)], 1)
    found = sysfs({"3-1.5": plain})
    assert found[0].properties["hid_kind"] == "mouse"
    assert found[0].icon == "input-mouse"


def test_a_dock_that_answers_twice_becomes_one_row(sysfs, tmp_path, monkeypatch):
    """A WD22TB4 answers at two USB addresses on different branches of its own hubs. Both descend
    from a hub whose product string says "dock", and that hub is the dock."""
    hidraw = build_sysfs(tmp_path, {})
    devices = tmp_path / "devices"

    def usb(path, vid, pid, product, interfaces=1):
        d = devices / path
        d.mkdir(parents=True, exist_ok=True)
        (d / "idVendor").write_text(f"{vid:04x}\n")
        (d / "idProduct").write_text(f"{pid:04x}\n")
        (d / "product").write_text(product + "\n")
        (d / "bNumInterfaces").write_text(f"{interfaces:2d}\n")
        return d

    # The dock's internal hub, then its two control endpoints beneath it, then a keyboard also
    # plugged into the dock -- which must NOT be merged with it.
    usb("3-1.1", 0x0BDA, 0x5487, "Dell dock")
    primary = usb("3-1.1/3-1.1.5", 0x413C, 0xB06E, "Dell Thunderbolt Dock WD22TB4", 2)
    companion = usb("3-1.1/3-1.1.3.5", 0x413C, 0xB06F, "Dell dock", 1)
    keyboard = usb("3-1.1/3-1.1.3.2.1", 0x1532, 0x0221, "Razer BlackWidow", 1)

    for i, (parent, name) in enumerate(
        [(primary, "Dell Thunderbolt Dock WD22TB4"), (companion, "Dell dock"),
         (keyboard, "Razer Razer BlackWidow")]
    ):
        # Real sysfs nests a hidraw node under the USB *interface*, not the USB device:
        # hidrawN/device -> HID device -> usb interface -> usb device. Skipping the interface
        # level makes the lookup walk one level too high and merge a device with its hub.
        hid = parent / f"{parent.name}:1.0" / f"0003:X.{i}"
        hid.mkdir(parents=True)
        (hid / "uevent").write_text(f"HID_ID=0003:00000000:00000000\nHID_NAME={name}\n")
        node = hidraw / f"hidraw{i}"
        node.mkdir()
        (node / "device").symlink_to(hid)

    monkeypatch.setattr(discovery, "SYS_HIDRAW", hidraw)
    found = {d.name: d for d in discovery.enumerate_hid()}
    assert "Dell Thunderbolt Dock WD22TB4" in found, list(found)
    assert "Dell dock" not in found, "the dock's two endpoints must be one row"
    # The companion has no "thunderbolt" in its name, so grouping must not depend on the two
    # endpoints agreeing about the transport.
    assert found["Dell Thunderbolt Dock WD22TB4"].properties["hid_kind"] == "dock"
    assert len(found["Dell Thunderbolt Dock WD22TB4"].properties["nodes"]) == 2
    # Everything plugged INTO the dock descends from the same hub and must stay separate.
    assert "Razer BlackWidow" in found
    assert found["Razer BlackWidow"].properties["hid_kind"] != "dock"


def test_a_dock_is_not_filed_under_input(sysfs):
    """A Thunderbolt dock was appearing beside a keyboard, because INPUT was the only category a
    classified HID device could get."""
    from hardware_ui.core import Category

    dock = (0x413C, 0xB06E, "Dell Thunderbolt Dock WD22TB4", [(0, 0), (0, 0)], 1)
    found = sysfs({"3-1.5": dock})
    assert found[0].category is Category.DOCKS
    assert found[0].category is not Category.INPUT


def test_a_composite_key_is_a_security_key_not_a_keyboard(sysfs, tmp_path, monkeypatch):
    """A YubiKey answers on several interfaces: its OTP one types like a keyboard, its FIDO one
    carries usage page 0xF1D0. What the device *is* must beat what one interface looks like."""
    from hardware_ui.core import Category

    hidraw = build_sysfs(tmp_path, {})
    usb = tmp_path / "devices" / "3-4"
    usb.mkdir(parents=True)
    (usb / "idVendor").write_text("1050\n")
    (usb / "idProduct").write_text("0407\n")
    (usb / "bNumInterfaces").write_text(" 3\n")
    # A boot-keyboard interface, which is what the OTP applet presents.
    iface = usb / "3-4:1.0"
    iface.mkdir()
    (iface / "bInterfaceClass").write_text("03\n")
    (iface / "bInterfaceSubClass").write_text("01\n")
    (iface / "bInterfaceProtocol").write_text("01\n")

    for i, descriptor in enumerate([b"\x05\x01\x09\x06", discovery.FIDO_USAGE_PAGE]):
        hid = iface / f"0003:1050:0407.000{i}"
        hid.mkdir(parents=True)
        (hid / "uevent").write_text(
            "HID_ID=0003:00001050:00000407\nHID_NAME=Yubico YubiKey OTP+FIDO+CCID\n"
        )
        (hid / "report_descriptor").write_bytes(descriptor)
        node = hidraw / f"hidraw{i}"
        node.mkdir()
        (node / "device").symlink_to(hid)

    monkeypatch.setattr(discovery, "SYS_HIDRAW", hidraw)
    found = discovery.enumerate_hid()
    assert len(found) == 1
    assert found[0].properties["hid_kind"] == "security_key"
    assert found[0].properties["hid_usage_page"] == "f1d0"
    assert found[0].category is Category.SECURITY_KEYS
    assert found[0].icon == "application-pgp-keys"


def test_category_values_read_as_sidebar_headings():
    """The enum value doubles as the heading, so a two-word category must render as two words."""
    from hardware_ui.core import Category, DeviceInfo, State, Transport
    from hardware_ui.shell.window import _section

    def dev(category):
        return DeviceInfo(uid="u", name="n", transport=Transport.HID, category=category,
                          state=State.PRESENT)

    assert _section(dev(Category.SECURITY_KEYS)) == "SECURITY KEYS"
    assert _section(dev(Category.DOCKS)) == "DOCKS"
    assert _section(dev(Category.INPUT)) == "INPUT"


# --------------------------------------------------------------------------- hotplug


def test_hotplug_is_optional_and_says_so_when_absent(monkeypatch):
    """No pyudev is an ordinary answer: the application behaves exactly as it did before."""
    import builtins

    from hardware_ui.core import discovery

    real_import = builtins.__import__

    def no_pyudev(name, *args, **kwargs):
        if name == "pyudev":
            raise ImportError("no pyudev")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyudev)
    assert discovery.watch() is None
    assert "pyudev" in discovery.HOTPLUG_HINT
    assert "Rescan" in discovery.HOTPLUG_HINT


def test_a_broken_udev_does_not_break_the_application(monkeypatch):
    from hardware_ui.core import discovery

    def explode(*_args, **_kwargs):
        raise RuntimeError("no udev on this machine")

    monkeypatch.setattr(discovery, "Hotplug", explode)
    assert discovery.watch() is None


def test_it_only_wakes_for_subsystems_that_can_hold_a_device():
    """Filtering is installed in the kernel, so an unrelated uevent never reaches the process."""
    from hardware_ui.core import discovery

    assert set(discovery.HOTPLUG_SUBSYSTEMS) == {"usb", "hidraw", "drm"}


def test_a_burst_of_events_drains_in_one_go():
    """One plug emits many events; the answer to all of them is a single re-enumeration."""
    from hardware_ui.core.discovery import Hotplug

    seen = iter([_FakeUdevDevice("usb"), _FakeUdevDevice("hidraw"), _FakeUdevDevice("usb"), None])
    watcher = Hotplug.__new__(Hotplug)
    watcher._monitor = _FakeMonitor(seen)
    assert watcher.drain() == {"usb", "hidraw"}


class _FakeUdevDevice:
    def __init__(self, subsystem):
        self.subsystem = subsystem


class _FakeMonitor:
    def __init__(self, events):
        self._events = events

    def poll(self, timeout=0):
        return next(self._events, None)


# --------------------------------------------------------------------------- bluetooth hotplug


def _properties_signal(interface, changed):
    from PyQt6.QtDBus import QDBusMessage

    message = QDBusMessage.createSignal(
        "/org/bluez/hci0/dev_AA_BB", "org.freedesktop.DBus.Properties", "PropertiesChanged"
    )
    message.setArguments([interface, changed, []])
    return message


def test_a_headset_switching_on_schedules_a_sweep(qapp):
    """`Connected` flipping is the event people notice, and it has no udev equivalent."""
    from hardware_ui.shell.bluetooth import BluetoothWatcher

    watcher = BluetoothWatcher()
    watcher._on_properties(_properties_signal("org.bluez.Device1", {"Connected": True}))
    assert watcher._settle.isActive()


def test_signal_strength_does_not_wake_the_application(qapp):
    """BlueZ reports RSSI on every advertisement it hears. Nothing displays it."""
    from hardware_ui.shell.bluetooth import BluetoothWatcher

    watcher = BluetoothWatcher()
    watcher._on_properties(_properties_signal("org.bluez.Device1", {"RSSI": -60}))
    assert not watcher._settle.isActive()


def test_an_adapter_property_is_not_a_device_property(qapp):
    from hardware_ui.shell.bluetooth import BluetoothWatcher

    watcher = BluetoothWatcher()
    watcher._on_properties(_properties_signal("org.bluez.Adapter1", {"Connected": True}))
    assert not watcher._settle.isActive()


def test_a_burst_of_bluez_activity_collapses_to_one_sweep(qapp):
    """A link coming up emits several property changes; the answer to all of them is one sweep."""
    from hardware_ui.shell.bluetooth import BluetoothWatcher

    watcher = BluetoothWatcher()
    fired: list[int] = []
    watcher.changed.connect(lambda: fired.append(1))
    for _ in range(5):
        watcher._on_properties(_properties_signal("org.bluez.Device1", {"Connected": True}))
    assert watcher._settle.isActive()
    assert fired == [], "nothing until the burst has settled"


def test_pairing_and_renaming_count_but_service_data_does_not(qapp):
    from hardware_ui.shell.bluetooth import BluetoothWatcher

    watcher = BluetoothWatcher()
    for prop in ("Paired", "Alias", "Name", "Trusted"):
        watcher._settle.stop()
        watcher._on_properties(_properties_signal("org.bluez.Device1", {prop: 1}))
        assert watcher._settle.isActive(), prop
    watcher._settle.stop()
    watcher._on_properties(_properties_signal("org.bluez.Device1", {"ServiceData": {}}))
    assert not watcher._settle.isActive()


def test_stopping_without_having_started_is_harmless(qapp):
    from hardware_ui.shell.bluetooth import BluetoothWatcher

    BluetoothWatcher().stop()
