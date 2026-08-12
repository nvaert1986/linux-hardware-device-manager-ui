"""The device-facing half of the Logitech module, against fakes shaped like Solaar's objects.

The mapping layer is covered by ``test_logitech_peripherals``. This covers what talks to hardware:
resolving a node to a library object, describing a receiver's slots, writing a setting, pairing and
unpairing. None of it needs a device attached — a fake that answers the same API exercises the same
code, and the parts that genuinely need hardware (does the receiver really report six slots?) are
listed in ``docs/LOGITECH_UI_BEHAVIOUR.md`` §9.

The fakes deliberately mirror the real attribute names rather than being convenient: a fake that
answers ``level`` when the library answers ``battery().level`` would pass here and fail on a desk.
"""

from __future__ import annotations

import pytest

from hardware_ui.core import DeviceError, NotSupported, Unreachable
from hardware_ui.core.device import Category, DeviceInfo, Transport
from hardware_ui.modules.logitech_peripherals import bootstrap
from hardware_ui.modules.logitech_peripherals import capabilities as C
from hardware_ui.modules.logitech_peripherals.device import LogitechDevice

pytestmark = pytest.mark.skipif(
    not (bootstrap.VENDOR / "logitech_receiver").is_dir(),
    reason="Solaar not vendored — run tools/vendor_solaar.py",
)


# --------------------------------------------------------------------------- fakes


class FakeFirmware:
    def __init__(self, kind, version):
        self.kind, self.version = kind, version


class FakeSetting:
    def __init__(self, name, label, kind, value, choices=(), rng=None, fail_read=False,
                 refuse=False):
        self.name, self.label, self.description = name, label, ""
        self.kind, self.choices, self.range = kind, choices, rng
        self.validator = type("V", (), {"step": 1})()
        self._value, self._fail_read, self._refuse = value, fail_read, refuse
        self.written: list[tuple] = []

    def read(self, cached=True):
        if self._fail_read:
            raise RuntimeError("device did not answer")
        return self._value

    def write(self, value, save=True):
        self.written.append((value, save))
        if self._refuse:
            return None
        self._value = value
        return value


class FakePeripheral:
    def __init__(self, number=1, name="MX Master 3", settings=(), battery=75, online=True):
        self.number, self.name = number, name
        self.codename, self.kind, self.online = "MX Master", "mouse", online
        self.serial, self.unitId, self.wpid = "DEADBEEF", "1234ABCD", "4082"
        self.firmware = (FakeFirmware("Firmware", "12.01.B0021"),)
        self.settings = list(settings)
        self._battery, self.closed = battery, False

    def battery(self):
        if self._battery is None:
            return None
        return type("B", (), {"level": self._battery})()

    def close(self):
        self.closed = True


class FakeReceiver:
    def __init__(self, path="/dev/hidraw5", devices=(), max_devices=6, remaining=3,
                 kind="unifying", may_unpair=True):
        self.path, self.name = path, "Unifying Receiver"
        self.codename, self.kind, self.serial = "Unifying", None, "R123"
        self.firmware = (FakeFirmware("Firmware", "24.01.0039"),)
        self.max_devices, self.receiver_kind, self.may_unpair = max_devices, kind, may_unpair
        self._devices, self._remaining = list(devices), remaining
        self.unpaired: list[int] = []
        self.closed = False

    def __iter__(self):
        return iter(self._devices)

    def remaining_pairings(self, cache=True):
        return self._remaining

    def _unpair_device(self, number, force=False):
        self._devices = [d for d in self._devices if d.number != number]
        self.unpaired.append(int(number))

    def close(self):
        self.closed = True


def make(path="/dev/hidraw5", name="Logitech"):
    return LogitechDevice(
        DeviceInfo(uid="u", name=name, transport=Transport.HID, category=Category.INPUT,
                   vendor_id=0x046D, path=path)
    )


def attach(device, handle, receiver=None, is_receiver=False):
    device._handle, device._receiver, device._is_receiver = handle, receiver, is_receiver
    return device


# --------------------------------------------------------------------------- resolving a node


def resolve_with(monkeypatch, entries, receivers, paired_paths=None):
    """Patch the library's enumeration so ``_resolve`` can run without hardware."""
    made = {r.path: r for r in receivers}

    class FakeInfo:
        def __init__(self, path, is_device):
            self.path, self.isDevice = path, is_device

    fake_base = type("base", (), {
        "receivers_and_devices": staticmethod(lambda: [FakeInfo(p, d) for p, d in entries])
    })
    fake_device = type("device", (), {
        "create_device": staticmethod(lambda base, info: FakePeripheral(name="Cabled Mouse"))
    })
    fake_receiver = type("receiver", (), {
        "create_receiver": staticmethod(lambda base, info: made.get(info.path))
    })
    monkeypatch.setattr(
        "hardware_ui.modules.logitech_peripherals.device.vendored",
        lambda: (fake_base, fake_device, fake_receiver),
    )
    bootstrap.ensure_path()
    import hidapi.udev_impl

    monkeypatch.setattr(
        hidapi.udev_impl, "find_paired_node",
        lambda receiver_path, index, timeout: (paired_paths or {}).get((receiver_path, index)),
    )


def test_a_receiver_node_resolves_to_the_receiver(monkeypatch):
    receiver = FakeReceiver(path="/dev/hidraw5")
    resolve_with(monkeypatch, [("/dev/hidraw5", False)], [receiver])
    handle, parent, is_receiver = make("/dev/hidraw5")._resolve()
    assert handle is receiver and parent is None and is_receiver


def test_a_directly_connected_device_resolves_to_a_device(monkeypatch):
    resolve_with(monkeypatch, [("/dev/hidraw9", True)], [])
    handle, parent, is_receiver = make("/dev/hidraw9")._resolve()
    assert handle.name == "Cabled Mouse" and parent is None and not is_receiver


def test_a_paired_device_resolves_through_its_receiver(monkeypatch):
    """The common case, and the newest logic here: the node handed to us belongs to a device that
    is reached by iterating its receiver, not by opening that node."""
    mouse = FakePeripheral(number=2, name="MX Master 3")
    receiver = FakeReceiver(path="/dev/hidraw5", devices=[mouse])
    resolve_with(monkeypatch, [("/dev/hidraw5", False)], [receiver],
                 paired_paths={("/dev/hidraw5", 2): "/dev/hidraw7"})
    handle, parent, is_receiver = make("/dev/hidraw7")._resolve()
    assert handle is mouse
    assert parent is receiver, "the receiver must be kept, or the connection label has no name"
    assert not is_receiver


def test_an_unknown_node_resolves_to_nothing(monkeypatch):
    resolve_with(monkeypatch, [("/dev/hidraw5", False)], [FakeReceiver(path="/dev/hidraw5")])
    assert make("/dev/hidraw99")._resolve()[0] is None


def test_a_receiver_searched_but_not_matched_is_closed(monkeypatch):
    """Otherwise every failed resolve leaks an open handle on the receiver."""
    receiver = FakeReceiver(path="/dev/hidraw5", devices=[FakePeripheral(number=1)])
    resolve_with(monkeypatch, [("/dev/hidraw5", False)], [receiver])
    make("/dev/hidraw99")._resolve()
    assert receiver.closed


# --------------------------------------------------------------------------- describing


def solaar_kind(name):
    bootstrap.ensure_path()
    from logitech_receiver.settings_validator import Kind

    return getattr(Kind, name)


def test_a_peripheral_describes_its_settings_and_battery():
    setting = FakeSetting("dpi", "Sensitivity (DPI)", solaar_kind("RANGE"), 1600, rng=(200, 4000))
    device = attach(make(), FakePeripheral(settings=[setting], battery=75))
    device._describe()

    assert device.capabilities.by_key("setting.dpi").kind.value == "range"
    assert device._values["setting.dpi"] == 1600
    assert device._values[C.BATTERY_KEY] == 75
    assert device.capabilities.by_key("info.name") is not None


def test_a_device_that_reports_no_battery_gets_no_meter():
    device = attach(make(), FakePeripheral(battery=None))
    device._describe()
    assert device.capabilities.by_key(C.BATTERY_KEY) is None


def test_one_unreadable_setting_does_not_lose_the_page():
    good = FakeSetting("dpi", "DPI", solaar_kind("RANGE"), 800, rng=(200, 4000))
    bad = FakeSetting("fn-swap", "Fn swap", solaar_kind("TOGGLE"), True, fail_read=True)
    device = attach(make(), FakePeripheral(settings=[good, bad]))
    device._describe()
    assert device._values["setting.dpi"] == 800
    assert "setting.fn-swap" not in device._values, "an unreadable setting has no value"
    assert device.capabilities.by_key("setting.fn-swap") is not None, "but still has a control"


def test_identity_omits_what_the_device_will_not_answer():
    handle = FakePeripheral()
    handle.serial = ""
    device = attach(make(), handle)
    device._describe()
    assert device.capabilities.by_key("info.serial") is None
    assert device.capabilities.by_key("info.name") is not None


def test_a_receiver_describes_its_slots():
    receiver = FakeReceiver(devices=[FakePeripheral(1, "MX Master 3"), FakePeripheral(2, "K800")])
    device = attach(make(), receiver, is_receiver=True)
    device._describe()

    assert device._values["info.slots"] == "2 of 6"
    assert device._values["info.remaining_pairings"] == "3"
    assert device.capabilities.by_key(C.PAIR_KEY) is not None
    assert device.capabilities.by_key(C.unpair_key(2)) is not None


def test_a_receiver_with_no_pairing_limit_says_nothing_about_one():
    receiver = FakeReceiver(devices=[FakePeripheral(1)], remaining=-1)
    device = attach(make(), receiver, is_receiver=True)
    device._describe()
    assert "info.remaining_pairings" not in device._values


def test_a_receiver_has_no_battery():
    device = attach(make(), FakeReceiver(), is_receiver=True)
    assert device._battery() is None


# --------------------------------------------------------------------------- writing


def test_writing_a_setting_reaches_the_device_and_is_saved():
    setting = FakeSetting("dpi", "DPI", solaar_kind("RANGE"), 800, rng=(200, 4000))
    device = attach(make(), FakePeripheral(settings=[setting]))
    device._describe()

    assert device._set_sync("setting.dpi", 1600) == 1600
    assert setting.written == [(1600, True)], "save=True is what re-applies it on reconnect"
    assert device._values["setting.dpi"] == 1600


def test_a_refused_write_is_reported_not_swallowed():
    """`write` returning None is how the library says the device did not take it."""
    setting = FakeSetting("dpi", "DPI", solaar_kind("RANGE"), 800, rng=(200, 4000), refuse=True)
    device = attach(make(), FakePeripheral(settings=[setting]))
    device._describe()
    with pytest.raises(DeviceError, match="did not accept"):
        device._set_sync("setting.dpi", 1600)


def test_a_write_that_raises_becomes_a_device_error_naming_the_setting():
    setting = FakeSetting("dpi", "Sensitivity (DPI)", solaar_kind("RANGE"), 800, rng=(1, 2))
    setting.write = lambda value, save=True: (_ for _ in ()).throw(RuntimeError("no such feature"))
    device = attach(make(), FakePeripheral(settings=[setting]))
    device._describe()
    with pytest.raises(DeviceError, match="Sensitivity"):
        device._set_sync("setting.dpi", 1600)


def test_writing_something_that_is_not_a_setting_is_refused():
    device = attach(make(), FakePeripheral())
    device._describe()
    with pytest.raises(DeviceError):
        device._set_sync("setting.nonexistent", 1)


def test_a_written_named_int_is_stored_as_a_plain_int():
    bootstrap.ensure_path()
    from logitech_receiver.common import NamedInt

    setting = FakeSetting("report_rate", "Report Rate", solaar_kind("CHOICE"),
                          NamedInt(125, "125Hz"), choices=(NamedInt(125, "125Hz"),))
    device = attach(make(), FakePeripheral(settings=[setting]))
    device._describe()
    landed = device._set_sync("setting.report_rate", 1000)
    assert landed == 1000 and type(landed) is int


# --------------------------------------------------------------------------- pairing


def test_unpairing_removes_the_device_and_says_which():
    receiver = FakeReceiver(devices=[FakePeripheral(1, "MX Master 3"), FakePeripheral(2, "K800")])
    device = attach(make(), receiver, is_receiver=True)
    device._describe()

    assert "K800" in device._unpair(2)
    assert receiver.unpaired == [2]
    # The page has to be rebuilt, or the row for a device that is gone stays on screen.
    assert device.capabilities.by_key(C.unpair_key(2)) is None
    assert device.capabilities.by_key(C.unpair_key(1)) is not None


def test_unpairing_an_empty_slot_says_so():
    device = attach(make(), FakeReceiver(devices=[]), is_receiver=True)
    device._describe()
    with pytest.raises(DeviceError, match="already empty"):
        device._unpair(3)


def test_pairing_is_refused_on_a_peripheral():
    device = attach(make(), FakePeripheral())
    with pytest.raises(NotSupported, match="receiver"):
        device._pair()


def test_the_passkey_is_typed_on_a_keyboard_and_clicked_on_a_mouse():
    """The difference is bit 0 of `authentication`. A mouse has no digits, so the same passkey is
    entered as a left/right click pattern -- ten bits, most significant first."""
    from hardware_ui.modules.logitech_peripherals.device import _passkey_instructions

    typed = _passkey_instructions("MX Keys S", 481625, authentication=0x01)
    assert "481625" in typed and "Enter" in typed

    clicked = _passkey_instructions("MX Master 3S", 0b1010000011, authentication=0x00)
    # Count in the pattern line alone -- the closing instruction names both buttons too.
    pattern = [p.strip() for p in clicked.split("\n")[2].split(",")]
    assert pattern == ["right", "left", "right", "left", "left",
                       "left", "left", "left", "right", "right"]
    assert len(pattern) == 10, "ten bits, most significant first"
    assert "together" in clicked, "the pattern ends with both buttons"


def test_a_cancelled_pairing_stops_rather_than_running_on():
    """Cancel is advisory: the loop notices between reads, so nothing is interrupted mid-write."""
    class Cancelling:
        def message(self, title, body): return None
        def cancelled(self): return True
        def close(self): return None

    device = attach(make(), FakeReceiver(kind="bolt"), is_receiver=True)
    device.interaction = Cancelling()
    with pytest.raises(DeviceError, match="cancelled"):
        device._pump(None, FakeReceiver(), until=lambda: False)


def test_unpairing_is_refused_on_a_peripheral():
    device = attach(make(), FakePeripheral())
    with pytest.raises(NotSupported, match="receiver"):
        device._unpair(1)


# --------------------------------------------------------------------------- connection label


def test_a_paired_device_is_labelled_by_the_receiver_it_speaks_through():
    mouse = FakePeripheral()
    device = attach(make(), mouse, receiver=FakeReceiver())
    device._describe()
    label = device.connection_label()
    assert label.route == "via Unifying Receiver"
    assert label.identifier == "DEADBEEF"


def test_a_receiver_is_labelled_as_usb():
    device = attach(make(), FakeReceiver(), is_receiver=True)
    device._describe()
    assert device.connection_label().route == "USB"


def test_a_cabled_device_is_labelled_as_usb():
    device = attach(make(), FakePeripheral(), receiver=None)
    device._describe()
    assert device.connection_label().route == "USB"


# --------------------------------------------------------------------------- lifecycle


def test_using_a_disconnected_device_raises_rather_than_crashing():
    with pytest.raises(Unreachable):
        make()._require()


def test_disconnect_closes_both_handles():
    """Both, not just the device: a paired peripheral holds its receiver open too."""
    import asyncio

    mouse, receiver = FakePeripheral(), FakeReceiver()
    device = attach(make(), mouse, receiver=receiver)
    asyncio.run(device.disconnect())
    assert mouse.closed and receiver.closed
    assert device._handle is None


# --------------------------------------------------------------------------- Bolt serial


def test_a_bolt_receivers_serial_is_decoded():
    """Measured on a Bolt receiver: ``extract_serial`` hexlifies the raw register, but Bolt's
    BOLT_UNIQUE_ID already holds printable ASCII, so the result is the hex *of the text* -- twice
    as long as it should be and matching nothing printed on the hardware."""
    from hardware_ui.modules.logitech_peripherals.device import _readable_serial

    assert _readable_serial("30384334464438443639393242433243", True) == "08C4FD8D6992BC2C"


def test_a_unifying_receivers_binary_serial_is_left_alone():
    """Four binary bytes hexlified is correct there; decoding would corrupt it."""
    from hardware_ui.modules.logitech_peripherals.device import _readable_serial

    assert _readable_serial("100A7C0E", True) == "100A7C0E"
    assert _readable_serial("30384334464438443639393242433243", False) == (
        "30384334464438443639393242433243"
    ), "only Bolt receivers are affected"


def test_hex_that_is_not_printable_ascii_is_left_alone():
    from hardware_ui.modules.logitech_peripherals.device import _readable_serial

    binary = "00010203040506070809000102030405"   # 32 chars, decodes to control bytes
    assert _readable_serial(binary, True) == binary


# --------------------------------------------------------------------------- on-board profiles
#
# A device driven by a stored profile ignores live writes to report rate, sensitivity and button
# actions -- Solaar says so in the DPI setting's own description. The write is accepted, the
# read-back disagrees, and the control appears to revert on its own, which is indistinguishable
# from a bug in this application unless the page says which. Neither device here has the feature,
# so the detection is faked; what is tested is that it fires only when it should.

def _with_profiles(monkeypatch, *, headers, enabled_byte):
    """Patch the two questions Solaar asks: are there profiles, and is profile mode on."""
    bootstrap.ensure_path()
    from logitech_receiver import hidpp20

    monkeypatch.setattr(
        hidpp20.OnboardProfiles, "get_profile_headers",
        classmethod(lambda cls, device: headers),
    )

    class Handle(FakePeripheral):
        def feature_request(self, feature, function=0x00, *args, **kwargs):
            return bytes([enabled_byte]) if function == 0x20 else None

    return Handle


def _device_with_dpi(handle_cls):
    setting = FakeSetting("dpi", "Sensitivity (DPI)", solaar_kind("CHOICE"), 1000,
                          choices=(800, 1000, 1600))
    device = attach(make(), handle_cls(settings=[setting]))
    device._describe()
    return device


def test_an_active_profile_warns_on_the_settings_it_governs(monkeypatch):
    handle = _with_profiles(monkeypatch, headers=[(1, 1)], enabled_byte=0x01)
    device = _device_with_dpi(handle)
    advisory = device.advisories().get("setting.dpi")
    assert advisory is not None
    assert "Onboard Profiles" in advisory.message
    # Advisory, not a lock: the write is still allowed, it may simply be ignored by the device.
    assert not advisory.locked


def test_profiles_present_but_disabled_says_nothing(monkeypatch):
    """`0x20` answering anything but 0x01 means host mode, where live writes are authoritative."""
    handle = _with_profiles(monkeypatch, headers=[(1, 1)], enabled_byte=0x00)
    assert _device_with_dpi(handle).advisories() == {}


def test_a_device_without_the_feature_is_never_asked_twice(monkeypatch):
    """Every device tested here is this case: no profile headers, so no second request."""
    handle = _with_profiles(monkeypatch, headers=[], enabled_byte=0x01)
    assert _device_with_dpi(handle).advisories() == {}


def test_settings_a_profile_does_not_govern_are_left_alone(monkeypatch):
    handle = _with_profiles(monkeypatch, headers=[(1, 1)], enabled_byte=0x01)
    setting = FakeSetting("hires-smooth-invert", "Scroll Wheel Direction",
                          solaar_kind("TOGGLE"), True)
    device = attach(make(), handle(settings=[setting]))
    device._describe()
    assert "setting.hires-smooth-invert" not in device.advisories()
