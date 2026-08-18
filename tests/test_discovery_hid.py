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

    assert set(discovery.HOTPLUG_SUBSYSTEMS) == {"usb", "hidraw", "drm", "video4linux"}


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


# --------------------------------------------------------------------------- raw USB
#
# The fourth transport, and the only one with no kernel-provided character device to open: a
# vendor protocol tunnelled through CDC has to be claimed with libusb. Enumeration still opens
# nothing -- interface classes are sysfs attributes, read as files.


def usb_tree(root, name, *, vendor="041e", product_id="3278", interfaces=(), **attrs):
    """`product_id` is the hex idProduct; a `product=` keyword lands in **attrs as the *string*
    sysfs exposes, which is what the name is built from."""
    """One USB device directory with the interface children a real one would have."""
    device = root / name
    device.mkdir(parents=True)
    (device / "idVendor").write_text(vendor + "\n")
    (device / "idProduct").write_text(product_id + "\n")
    (device / "bNumInterfaces").write_text(f"{len(interfaces):2d}\n")
    for key, value in attrs.items():
        (device / key).write_text(value + "\n")
    for index, signature in enumerate(interfaces):
        klass, subclass = signature[0], signature[1]
        protocol = signature[2] if len(signature) > 2 else 0x00
        iface = device / f"{name}:1.{index}"
        iface.mkdir()
        (iface / "bInterfaceClass").write_text(f"{klass:02x}\n")
        (iface / "bInterfaceSubClass").write_text(f"{subclass:02x}\n")
        (iface / "bInterfaceProtocol").write_text(f"{protocol:02x}\n")
    return device


CDC_ACM = (0x02, 0x02, 0x01)
CDC_DATA = (0x0A, 0x00, 0x00)
AUDIO = (0x01, 0x01, 0x00)
HID = (0x03, 0x00, 0x00)
#: Xbox Game Input Protocol: a vendor-specific class with a fixed subclass and protocol. 8BitDo's
#: Xbox controllers expose this and no hidraw at all.
GIP = (0xFF, 0x47, 0xD0)
#: A plain vendor-specific interface, which is what most dongles fall back to. Must NOT qualify.
VENDOR_OTHER = (0xFF, 0x00, 0x00)


def test_a_device_with_a_cdc_control_channel_is_enumerated(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "3-1", interfaces=(AUDIO, CDC_ACM, CDC_DATA),
             manufacturer="Creative Technology Ltd", product="Sound Blaster X4", serial="SB-1")

    found = discovery.enumerate_usb()
    assert len(found) == 1
    device = found[0]
    assert device.transport is discovery.Transport.USB
    assert (device.vendor_id, device.product_id) == (0x041E, 0x3278)
    # The uid keys settings and the capability cache, so it follows the serial rather than the
    # sysfs path -- moving the card to another port must not orphan its configuration.
    assert device.uid == "usb:SB-1"
    assert device.category is discovery.Category.AUDIO


def test_a_device_without_one_is_left_out_of_the_sidebar(tmp_path, monkeypatch):
    """The filter is the point. Enumerating every USB device would fill the list with hubs,
    webcams and card readers, and the list is the product."""
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "3-1", vendor="1d6b", product_id="0003", interfaces=(HID,), product="hub")
    assert discovery.enumerate_usb() == []


def test_a_communications_device_that_is_not_acm_is_not_claimed(tmp_path, monkeypatch):
    """Class 0x02 also covers ethernet, ATM and OBEX functions. A network adapter is somebody
    else's business."""
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "3-1", interfaces=((0x02, 0x06), (0x0A, 0x00)))   # 0x06 == ECM
    assert discovery.enumerate_usb() == []


def test_a_serialless_device_falls_back_to_its_bus_path(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "3-1.4", interfaces=(CDC_ACM, CDC_DATA), product="Widget")
    assert discovery.enumerate_usb()[0].uid == "usb:3-1.4"


def test_an_interface_directory_is_never_mistaken_for_a_device(tmp_path, monkeypatch):
    """`3-1` is a device and `3-1:1.0` is one of its interfaces. Only devices carry idVendor, so
    the attribute is the test rather than the name shape."""
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "3-1", interfaces=(CDC_ACM, CDC_DATA))
    assert len(discovery.enumerate_usb()) == 1


def test_a_missing_usb_tree_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path / "nowhere")
    assert discovery.enumerate_usb() == []


def test_one_row_per_device_when_hidraw_saw_it_too():
    """A device can expose both a HID interface and a CDC channel, and each enumerator finds it
    independently. hidraw wins: its row carries an openable node, a device kind and an icon, and
    a module needing the CDC channel can still find it from the USB path both rows share."""
    from hardware_ui.core.device import DeviceInfo, Transport

    hid_row = DeviceInfo(uid="hid:x", name="Card", transport=Transport.HID,
                         properties={"usb": "3-1"})
    usb_row = DeviceInfo(uid="usb:x", name="Card", transport=Transport.USB, path="/sys/.../3-1")
    other = DeviceInfo(uid="usb:y", name="Other", transport=Transport.USB, path="/sys/.../3-2")

    kept = discovery._one_row_per_device([hid_row, usb_row, other])
    assert [d.uid for d in kept] == ["hid:x", "usb:y"]


# --------------------------------------------------------------------------- GIP
#
# 8BitDo's Xbox controllers speak the Xbox Game Input Protocol on a vendor-specific interface and
# expose **no hidraw at all**, so without this signature they are invisible to every transport
# here. The signature is matched as a full (class, subclass, protocol) triple on purpose: class
# 0xFF alone is what every dongle in the world falls back to.


def test_a_gip_controller_is_enumerated(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "1-4", vendor="2dc8", product_id="2002", interfaces=(GIP,),
             manufacturer="8BitDo", product="8BitDo Ultimate Wired Controller for Xbox",
             serial="B1-0001")

    found = discovery.enumerate_usb()
    assert len(found) == 1
    assert (found[0].vendor_id, found[0].product_id) == (0x2DC8, 0x2002)
    # The matched signature is reported so a manifest can ask for the transport it speaks rather
    # than re-reading sysfs.
    assert found[0].properties["control_interfaces"] == "gip"
    # The interface says what the device is: nothing but an Xbox controller speaks GIP, so it
    # belongs under INPUT with a gamepad icon rather than landing in OTHER.
    assert found[0].category is discovery.Category.INPUT
    assert found[0].icon_name == "input-gaming"


def test_a_cdc_device_is_not_assumed_to_be_a_controller(tmp_path, monkeypatch):
    """CDC-ACM says nothing about what the device is, so it falls through to the audio sniff."""
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "3-1", interfaces=(AUDIO, CDC_ACM, CDC_DATA), product="Sound Blaster X4")
    found = discovery.enumerate_usb()[0]
    assert found.category is discovery.Category.AUDIO
    assert found.icon_name == ""


def test_a_plain_vendor_specific_interface_does_not_qualify(tmp_path, monkeypatch):
    """The whole point of matching the triple. A wireless dongle claiming class 0xFF is not
    something this application can talk to, and admitting it would fill the sidebar."""
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "1-4", vendor="0bda", product_id="8153", interfaces=(VENDOR_OTHER,))
    assert discovery.enumerate_usb() == []


def test_a_device_offering_both_signatures_reports_both(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    usb_tree(tmp_path, "1-4", interfaces=(GIP, CDC_ACM, CDC_DATA))
    assert discovery.enumerate_usb()[0].properties["control_interfaces"] == "cdc-acm,gip"


def test_an_interface_missing_its_protocol_attribute_still_matches_cdc(tmp_path, monkeypatch):
    """Real sysfs always provides all three, but losing a whole interface because one file could
    not be read would hide the device silently. CDC-ACM does not care about protocol, so it must
    still match; GIP does care, so it must not."""
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    device = usb_tree(tmp_path, "1-4", interfaces=(CDC_ACM, CDC_DATA))
    for iface in device.glob("1-4:*"):
        (iface / "bInterfaceProtocol").unlink()

    found = discovery.enumerate_usb()
    assert len(found) == 1
    assert found[0].properties["control_interfaces"] == "cdc-acm"


def test_an_interface_with_no_readable_class_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_USB", tmp_path)
    device = usb_tree(tmp_path, "1-4", interfaces=(GIP,))
    (device / "1-4:1.0" / "bInterfaceClass").write_text("\n")
    assert discovery.enumerate_usb() == []


# --------------------------------------------------------------------------- Bluetooth HID
#
# A mouse or keyboard paired directly over Bluetooth is both a BlueZ device and a hidraw node, and
# neither enumerator knows about the other. Two bugs came out of that, both seen on a real
# MX Master 3S.


def bluetooth_hid_node(root, name="Logitech MX Master 3S", uniq="de:d7:4b:fc:27:ea",
                       product="b034", descriptor=None):
    """A hidraw node with HID_ID bus 0005 and, crucially, **no USB parent at all**."""
    hidraw = root / "class" / "hidraw"
    hidraw.mkdir(parents=True, exist_ok=True)
    hid = root / "bt" / f"0005:046D:{product.upper()}.0009"
    hid.mkdir(parents=True, exist_ok=True)
    (hid / "uevent").write_text(
        f"HID_ID=0005:0000046D:0000{product.upper()}\nHID_NAME={name}\nHID_UNIQ={uniq}\n")
    if descriptor is not None:
        (hid / "report_descriptor").write_bytes(descriptor)
    node = hidraw / "hidraw15"
    node.mkdir(exist_ok=True)
    (node / "device").symlink_to(hid)
    return hidraw


#: The real opening bytes of an MX Master 3S's Bluetooth report descriptor, read off the device:
#: Usage Page Generic Desktop, Usage Mouse, Collection Application, Report ID 2, Usage Pointer,
#: Collection Physical.
MOUSE_DESCRIPTOR = bytes.fromhex("0501 0902 a101 8502 0901 a100".replace(" ", ""))
KEYBOARD_DESCRIPTOR = bytes.fromhex("0501 0906 a101 8501".replace(" ", ""))


def test_a_bluetooth_mouse_is_classified_from_its_own_descriptor(tmp_path, monkeypatch):
    """`_hid_kind` reads the USB interface protocol byte, and a Bluetooth device has no USB
    interface -- so the mouse got no kind, no icon, and a category guessed from whether its name
    happened to contain the word "mouse". "Logitech MX Master 3S" does not, so it landed in OTHER
    with the generic peripherals icon."""
    monkeypatch.setattr(discovery, "SYS_HIDRAW",
                        bluetooth_hid_node(tmp_path, descriptor=MOUSE_DESCRIPTOR))
    found = discovery.enumerate_hid()
    assert len(found) == 1
    assert found[0].properties["hid_kind"] == "mouse"
    assert found[0].category is discovery.Category.INPUT
    assert found[0].icon_name == "input-mouse"


def test_a_bluetooth_keyboard_is_classified_too(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_HIDRAW",
                        bluetooth_hid_node(tmp_path, name="MX Keys S",
                                           descriptor=KEYBOARD_DESCRIPTOR))
    found = discovery.enumerate_hid()
    assert found[0].properties["hid_kind"] == "keyboard"
    assert found[0].icon_name == "input-keyboard"


def test_a_device_with_no_readable_descriptor_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "SYS_HIDRAW", bluetooth_hid_node(tmp_path))
    assert discovery.enumerate_hid()[0].properties["hid_kind"] == ""


def test_only_top_level_usages_count(tmp_path, monkeypatch):
    """A keyboard usage *inside* a mouse's collection must not reclassify it. Descriptors nest
    pointers, buttons and wheels inside collections, and taking usages from in there would pick up
    whatever appeared first."""
    nested = (bytes.fromhex("0501")            # Usage Page: Generic Desktop
              + bytes.fromhex("0902")          # Usage: Mouse            (top level)
              + bytes.fromhex("a101")          # Collection
              + bytes.fromhex("0906")          # Usage: Keyboard         (inside -- ignore)
              + bytes.fromhex("c0"))           # End Collection
    monkeypatch.setattr(discovery, "SYS_HIDRAW",
                        bluetooth_hid_node(tmp_path, descriptor=nested))
    assert discovery.enumerate_hid()[0].properties["hid_kind"] == "mouse"


def test_the_bluez_duplicate_of_a_bluetooth_mouse_is_dropped():
    """The consequential one. Every Logitech mouse paired over Bluetooth produced a working,
    claimed hidraw row *and* an unclaimed BlueZ row reading "no module" -- one mouse, two entries,
    one a dead end. Matched on the address, which hidraw reports as HID_UNIQ and BlueZ in a
    different case."""
    from hardware_ui.core.device import DeviceInfo, Transport

    hid_row = DeviceInfo(uid="hid:de:d7:4b:fc:27:ea", name="Logitech MX Master 3S",
                         transport=Transport.HID, address="de:d7:4b:fc:27:ea",
                         module_id="logitech_peripherals")
    bluez_row = DeviceInfo(uid="bt:DE:D7:4B:FC:27:EA", name="MX Master 3S",
                           transport=Transport.BLUETOOTH, address="DE:D7:4B:FC:27:EA")

    kept = discovery._one_row_per_device([hid_row, bluez_row])
    assert [d.uid for d in kept] == ["hid:de:d7:4b:fc:27:ea"]


def test_a_bluetooth_headset_keeps_its_only_row():
    """Audio is untouched: a headset has no hidraw node, so nothing matches its address."""
    from hardware_ui.core.device import DeviceInfo, Transport

    mouse = DeviceInfo(uid="hid:aa", name="mouse", transport=Transport.HID, address="AA:AA")
    headset = DeviceInfo(uid="bt:BB", name="WH-1000XM4", transport=Transport.BLUETOOTH,
                         address="BB:BB")
    kept = discovery._one_row_per_device([mouse, headset])
    assert {d.uid for d in kept} == {"hid:aa", "bt:BB"}


def test_a_dongle_without_a_hid_uniq_never_swallows_a_headset_row():
    """A Poly headset reached through its USB dongle keeps both rows: the dongle's node carries no
    HID_UNIQ, so its empty address must not match the headset's."""
    from hardware_ui.core.device import DeviceInfo, Transport

    dongle = DeviceInfo(uid="hid:x", name="Poly BT700", transport=Transport.HID, address="")
    headset = DeviceInfo(uid="bt:CC", name="Poly V4320", transport=Transport.BLUETOOTH,
                         address="CC:CC")
    kept = discovery._one_row_per_device([dongle, headset])
    assert {d.uid for d in kept} == {"hid:x", "bt:CC"}


def test_bluez_supplies_the_icon_and_category_it_already_knows(monkeypatch):
    """BlueZ derives a freedesktop icon name from the Class of Device or the BLE Appearance
    characteristic, and it is better evidence than a name regex: a real MX Master 3S reports
    `input-mouse`, while the string "MX Master 3S" contains no word the guess could have matched.
    """
    import subprocess

    from hardware_ui.core.device import Category

    listing = "Device DE:D7:4B:FC:27:EA MX Master 3S\nDevice AC:80:0A:C5:13:01 WH-1000XM4\n"
    info = {
        "DE:D7:4B:FC:27:EA": "\tConnected: no\n\tAppearance: 0x03c2 (962)\n\tIcon: input-mouse\n",
        "AC:80:0A:C5:13:01": "\tConnected: yes\n\tIcon: audio-headset\n",
    }

    def fake_run(cmd, *a, **k):
        if "devices" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=info[cmd[-1]], stderr="")

    import unittest.mock

    with unittest.mock.patch.object(subprocess, "run", fake_run):
        rows = {d.name: d for d in discovery._enumerate_bluetooth_cli()}

    assert rows["MX Master 3S"].icon_name == "input-mouse"
    assert rows["MX Master 3S"].category is Category.INPUT
    assert rows["WH-1000XM4"].icon_name == "audio-headset"
    assert rows["WH-1000XM4"].category is Category.AUDIO


def test_a_device_bluez_has_no_icon_for_falls_back_to_the_name_guess(monkeypatch):
    import subprocess
    import unittest.mock

    from hardware_ui.core.device import Category

    listing = "Device AA:BB:CC:DD:EE:01 Some Wireless Keyboard\n"
    def fake_run(cmd, *a, **k):
        if "devices" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="\tConnected: no\n", stderr="")

    with unittest.mock.patch.object(subprocess, "run", fake_run):
        row = discovery._enumerate_bluetooth_cli()[0]
    assert row.icon_name == ""
    assert row.category is Category.INPUT      # from the word "Keyboard"


# --------------------------------------------------------------------------- HID++ detection
#
# Logitech's vendor id says nothing about whether a node speaks HID++. A BRIO webcam and an MX
# Master are both 046d over hidraw, and the module that configures mice was claiming the webcam --
# which then inherited that module's category and drew a gamepad icon in the sidebar.
#
# The test Solaar itself applies is the report descriptor: an input report on id 0x10 of six
# payload bytes, or on 0x11 of nineteen. Its own filter lives in the vendored copy at
# third_party/hidapi/udev_impl.py, and deliberately does *not* also require usage page 0xFF00 --
# that check is present but commented out there as too strict.

#: The BRIO's real report descriptor, read from /sys/class/hidraw/hidraw15 on 2026-08-18. Thirty
#: four bytes: Consumer page, two one-bit media buttons, six bits of padding. No report ids at all,
#: so there is nothing for HID++ to be carried on.
BRIO_DESCRIPTOR = bytes.fromhex(
    "05 0c 09 01 a1 01"        # Usage Page (Consumer), Usage 1, Collection
    "05 0c 09 01 a1 01"        #   Usage Page (Consumer), Usage 1, Collection
    "09 ff 09 fe"              #     Usage 0xff, Usage 0xfe
    "15 00 25 01 75 01 95 02"  #     Logical 0..1, Report Size 1, Report Count 2
    "81 42"                    #     Input (Data, Var, Abs, Null)
    "95 01 75 06 81 01"        #     Report Count 1, Report Size 6, Input (Const) -- padding
    "c0 c0".replace(" ", "")
)

#: HID++ as Logitech declares it: a short report on 0x10 with six payload bytes and a long one on
#: 0x11 with nineteen. Both sizes were confirmed by parsing these same bytes with the vendored
#: `hid_parser`, independently of the walker under test, which read 0x10 as 48 bits and 0x11 as 152.
HIDPP_DESCRIPTOR = bytes.fromhex(
    "06 00 ff 09 01 a1 01"     # Usage Page (Vendor 0xff00), Usage 1, Collection
    "85 10 75 08 95 06"        #   Report ID 0x10, Report Size 8, Report Count 6
    "15 00 26 ff 00"           #   Logical 0..255
    "09 01 81 00"              #   Usage 1, Input
    "09 01 91 00"              #   Usage 1, Output
    "c0"                       # End Collection
    "06 00 ff 09 02 a1 01"     # Usage Page (Vendor 0xff00), Usage 2, Collection
    "85 11 75 08 95 13"        #   Report ID 0x11, Report Size 8, Report Count 19
    "15 00 26 ff 00"
    "09 02 81 00"
    "09 02 91 00"
    "c0".replace(" ", "")
)


def test_report_descriptor_sizes_match_the_brio_bytes():
    """Sizes in bits, keyed by report id, with `None` for a descriptor that declares none."""
    assert discovery._input_report_sizes(BRIO_DESCRIPTOR) == {None: 8}


def test_a_webcams_media_buttons_are_not_hidpp(monkeypatch, tmp_path):
    """The whole point. Two media-button bits must not read as a configurable mouse."""
    node = tmp_path / "hidraw15"
    (node / "device").mkdir(parents=True)
    (node / "device" / "report_descriptor").write_bytes(BRIO_DESCRIPTOR)
    assert discovery._speaks_hidpp(node) is False


def test_hidpp_reports_are_recognised(monkeypatch, tmp_path):
    """The positive case, which no locally attached device can supply: both report ids at both
    sizes. Solaar accepts either one alone, so each is also checked on its own."""
    assert discovery._input_report_sizes(HIDPP_DESCRIPTOR) == {0x10: 48, 0x11: 152}

    node = tmp_path / "hidraw3"
    (node / "device").mkdir(parents=True)
    (node / "device" / "report_descriptor").write_bytes(HIDPP_DESCRIPTOR)
    assert discovery._speaks_hidpp(node) is True


def test_a_report_of_the_wrong_size_is_not_hidpp():
    """Solaar checks the size, not just the id, and so does this: 0x10 exists here but carries
    seven bytes rather than six, which is some other vendor protocol."""
    seven = HIDPP_DESCRIPTOR.replace(bytes.fromhex("9506"), bytes.fromhex("9507"))
    assert discovery._input_report_sizes(seven)[0x10] == 56
    assert not any(
        discovery._input_report_sizes(seven).get(rid) == payload * 8
        for rid, payload in discovery.HIDPP_REPORTS.items()
        if rid == 0x10
    )


def test_push_and_pop_are_declined_rather_than_guessed():
    """Push/Pop save and restore the global item state. Modelling them wrongly would corrupt the
    running report size for everything after, so the walker says it does not know instead."""
    assert discovery._input_report_sizes(bytes.fromhex("a4") + HIDPP_DESCRIPTOR) is None
    assert discovery._input_report_sizes(bytes.fromhex("fe")) is None


def test_every_node_of_a_device_is_asked_not_just_the_one_shown(tmp_path):
    """Measured on a Logi Bolt receiver: discovery represents the group by /dev/hidraw1, which
    declares no report ids at all, while HID++ answers on /dev/hidraw3. Asking only the
    representative would report a working receiver as not speaking HID++."""
    members = []
    for name, descriptor in (("hidraw1", BRIO_DESCRIPTOR), ("hidraw3", HIDPP_DESCRIPTOR)):
        node = tmp_path / name
        (node / "device").mkdir(parents=True)
        (node / "device" / "report_descriptor").write_bytes(descriptor)
        members.append({"node": node})

    assert discovery._hidpp_family(members) == "yes"
    assert discovery._hidpp_family(members[:1]) == "no"


def test_an_unreadable_descriptor_is_unknown_not_absent(tmp_path):
    """`""` and `"no"` are different answers. Reporting "nobody could tell" as "definitely not" is
    how a working receiver would go missing from the sidebar entirely."""
    node = tmp_path / "hidraw9"
    node.mkdir()
    assert discovery._speaks_hidpp(node) is None
    assert discovery._hidpp_family([{"node": node}]) == ""


# --------------------------------------------------------------------------- cameras win


def test_a_camera_displaces_its_own_hid_row():
    """A webcam exposes media buttons on hidraw as well as video nodes, and each enumerator finds
    it independently. Here the camera wins, against the rule that applies everywhere else, because
    its row is the one that can be configured -- the hidraw row carries two button bits and no
    settings at all, and it was being claimed by the Logitech mouse module."""
    from hardware_ui.core.device import DeviceInfo, Transport

    hid_row = DeviceInfo(uid="hid:brio", name="Logitech BRIO", transport=Transport.HID,
                         path="/dev/hidraw15",
                         properties={"usb": "4-4", "nodes": ["/dev/hidraw15"]})
    camera = DeviceInfo(uid="v4l2:4-4", name="Logitech BRIO", transport=Transport.V4L2,
                        path="/dev/video4", properties={"sysfs": "/sys/devices/pci0000:00/4-4"})
    mouse = DeviceInfo(uid="hid:mouse", name="MX Master", transport=Transport.HID,
                       properties={"usb": "1-2", "nodes": ["/dev/hidraw3"]})

    kept = discovery._one_row_per_device([hid_row, camera, mouse])
    assert [d.uid for d in kept] == ["v4l2:4-4", "hid:mouse"]
    # Nothing is lost: the buttons are still reachable from the row that survived.
    assert kept[0].properties["hid_nodes"] == ["/dev/hidraw15"]


def test_a_camera_without_a_hid_row_gains_no_empty_property():
    """Most cameras have no hidraw node. They should not sprout an empty list for one."""
    from hardware_ui.core.device import DeviceInfo, Transport

    camera = DeviceInfo(uid="v4l2:2-1", name="Integrated Camera", transport=Transport.V4L2,
                        path="/dev/video0", properties={"sysfs": "/sys/devices/2-1"})
    kept = discovery._one_row_per_device([camera])
    assert "hid_nodes" not in kept[0].properties
