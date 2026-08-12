"""Dell module tests, driven from real capability strings.

Every ``CAPS_*`` below is the ``vcp(...)`` content of a monitor someone has actually had on a
desk, taken from the reference project's per-model findings. That is the point: the module is
capability-driven, so a capability string is a complete input, and these tests exercise nine
models' worth of firmware differences without nine monitors.

Nothing here touches a bus. ``is_read_only`` is stubbed, because the real one shells out to
``ddcutil vcpinfo``.
"""

from __future__ import annotations

from hardware_ui.core import Kind
from hardware_ui.modules.dell_monitors import capabilities as C
from hardware_ui.modules.dell_monitors.protocol import features as F
from hardware_ui.modules.dell_monitors.protocol.calibration import Range
from hardware_ui.modules.dell_monitors.protocol.ddcutil import VcpReading

# MCCS marks 0xAA read-only and everything else here writable. The real check is a subprocess.
READ_ONLY = {0xAA}


def read_only(code: int) -> bool:
    return code in READ_ONLY


# --------------------------------------------------------------------------- fixtures

#: P2425D, the baseline. Merged preset via DC+14, 0xE2 present, no ComfortView, no PIP/MST/KVM.
CAPS_P2425D: dict[int, list[int] | None] = {
    0x02: [], 0x04: [], 0x05: [], 0x08: [], 0x10: None, 0x12: None,
    0x14: [0x05, 0x08, 0x0B, 0x0C], 0x16: None, 0x18: None, 0x1A: None,
    0x52: [], 0x60: [0x0F, 0x11], 0x87: None, 0xAA: [0x01, 0x02, 0x04],
    0xAC: [], 0xAE: [], 0xB2: [], 0xB6: [], 0xC0: [], 0xC6: [], 0xC8: [], 0xC9: [],
    0xCC: [0x02, 0x03, 0x04, 0x06, 0x0A, 0x0D], 0xD6: [0x01, 0x04, 0x05],
    0xDC: [0x00, 0x03, 0x05], 0xDF: [], 0xE2: [], 0xF1: [], 0xF2: [], 0xFD: [],
}

#: U2412M. The first tested panel with **no 0xE2** and no sharpness, and 0xF0 bare.
CAPS_U2412M: dict[int, list[int] | None] = {
    0x02: [], 0x04: [], 0x06: [], 0x10: None, 0x12: None,
    0x14: [0x01, 0x05, 0x08, 0x0B, 0x0C], 0x16: None, 0x18: None, 0x1A: None,
    0x60: [0x01, 0x03, 0x0F], 0xAA: [0x01, 0x02], 0xD6: [0x01, 0x04, 0x05],
    0xDC: [0x00, 0x02, 0x03, 0x05], 0xF0: [],
}

#: P2319H. Has ComfortView on 0xF0, which folds into the merged preset.
CAPS_P2319H: dict[int, list[int] | None] = {
    0x10: None, 0x12: None, 0x14: [0x05, 0x08, 0x0B, 0x0C], 0x16: None, 0x18: None,
    0x1A: None, 0x60: [0x01, 0x0F, 0x11], 0x87: None, 0xD6: [0x01, 0x04, 0x05],
    0xDC: [0x00, 0x02, 0x03, 0x05], 0xE2: [], 0xF0: [0x00, 0x0C],
}

#: P3424WE. PIP/PBP and the bit-packed USB-KVM regime, both hardware-verified.
CAPS_P3424WE: dict[int, list[int] | None] = {
    0x04: [], 0x10: None, 0x12: None, 0x14: [0x05, 0x08, 0x0B, 0x0C],
    0x16: None, 0x18: None, 0x1A: None,
    0x60: [0x1B, 0x0F, 0x11], 0xCC: [0x02, 0x03], 0xD6: [0x01, 0x04, 0x05],
    0xDC: [0x00, 0x03, 0x05], 0xE2: [],
    0xE5: [], 0xE7: [0x00, 0x01], 0xE8: [0x0F, 0x11, 0x1B],
    0xE9: [0x00, 0x01, 0x02, 0x21, 0x22, 0x24], 0xEF: [0x00, 0x01, 0x0F],
}

#: P2725HE. Old-spec 0xEF (MST is OSD-only) plus USB-C Prioritization on 0xEA.
CAPS_P2725HE: dict[int, list[int] | None] = {
    0x10: None, 0x12: None, 0x14: [0x05, 0x08, 0x0B, 0x0C], 0x16: None, 0x18: None,
    0x1A: None, 0x60: [0x0F, 0x11, 0x1B], 0x87: None, 0xCC: [0x02],
    0xD6: [0x01, 0x04, 0x05], 0xDC: [0x00, 0x02, 0x03, 0x05], 0xE2: [],
    0xEA: [0xF8], 0xEF: [0x00, 0x01, 0x0F],
}

#: A hypothetical new-spec 0xEF panel -- the only regime where MST is DDC-controllable.
CAPS_NEW_SPEC_MST: dict[int, list[int] | None] = {
    0x10: None, 0x60: [0x0F], 0xEF: [0xC000],
}


def build(caps, *, readings=None, ranges=None, names=None, info=()):
    if readings is None:
        readings = {
            code: VcpReading(code=code, kind="continuous", value=50, maximum=100)
            for code in caps
            if code in F.CONTINUOUS
        }
        readings.update(
            {
                code: VcpReading(code=code, kind="simple", value=(values or [0])[0])
                for code, values in caps.items()
                if code in F.ENUM_LABELS
            }
        )
    return C.build(
        caps=caps,
        readings=readings,
        ranges=ranges or {},
        info_rows=info,
        input_names=names or {},
        read_only=read_only,
    )


def keys(caps_set) -> set[str]:
    return {c.key for c in caps_set}


def groups(caps_set) -> list[str]:
    return list(caps_set.groups())


# --------------------------------------------------------------------------- the baseline


def test_p2425d_has_the_controls_the_hardware_has():
    page = build(CAPS_P2425D)
    assert {
        "image.brightness", "image.contrast", "image.sharpness",
        "image.gain_red", "image.gain_green", "image.gain_blue",
        "image.preset", "settings.input", "settings.osd_language", "settings.power",
    } <= keys(page)


def test_p2425d_has_no_pip_mst_or_kvm_tab():
    assert groups(build(CAPS_P2425D)) == ["Information", "Settings", "Color / Picture"]


def test_a_tab_is_absent_rather_than_empty():
    """The Sony rule, restated: a monitor with no PIP register gets no PIP tab, not a blank one."""
    for group in ("PIP / PBP", "MST", "KVM"):
        assert group not in groups(build(CAPS_P2425D))


def test_read_only_features_are_readouts_not_controls():
    orientation = build(CAPS_P2425D).by_key("settings.orientation")
    assert orientation is not None
    assert orientation.kind is Kind.READOUT
    assert not orientation.writable
    assert orientation.group == "Information"


def test_continuous_features_take_their_maximum_from_the_reading():
    """DDC/CI reports a maximum and nothing else -- it is only known once something is read."""
    readings = {0x10: VcpReading(code=0x10, kind="continuous", value=40, maximum=80)}
    brightness = build(CAPS_P2425D, readings=readings).by_key("image.brightness")
    assert (brightness.minimum, brightness.maximum, brightness.step) == (0, 80, 1)


def test_calibration_re_bounds_the_slider():
    readings = {0x12: VcpReading(code=0x12, kind="continuous", value=50, maximum=100)}
    ranges = {0x12: Range(minimum=25, maximum=100, step=5)}
    contrast = build(CAPS_P2425D, readings=readings, ranges=ranges).by_key("image.contrast")
    assert (contrast.minimum, contrast.maximum, contrast.step) == (25, 100, 5)


def test_disruptive_writes_confirm_with_the_reason():
    page = build(CAPS_P2425D)
    assert page.by_key("settings.input").confirm
    assert "away from this machine" in page.by_key("settings.input").confirm_detail
    assert page.by_key("settings.power").confirm
    assert not page.by_key("image.brightness").confirm


def test_nothing_on_a_monitor_reboots():
    """``reboots`` means "the link dies and the shell must reconnect". A monitor never does that,
    even for a factory reset -- using it here would make the shell promise a reconnect it does
    not need to perform."""
    assert not any(c.reboots for c in build(CAPS_P3424WE))


# --------------------------------------------------------------------------- merged preset


def test_preset_merges_three_opcodes_into_one_control():
    page = build(CAPS_P2425D)
    labels = [c.label for c in page.by_key("image.preset").choices]
    assert labels == ["Standard", "Movie", "Game", "Cool", "Warm", "Custom Colour"]
    assert "image.display_mode" not in keys(page)  # 0xDC folded in
    assert "image.colour_temperature" not in keys(page)  # 0x14 folded in


def test_comfortview_folds_in_and_its_dead_off_value_does_not():
    """Writing ``F0=0x00`` is rejected by the panel: you leave ComfortView by picking another
    preset. Offering it as an item would be offering a control that cannot work."""
    labels = [c.label for c in build(CAPS_P2319H).by_key("image.preset").choices]
    assert "ComfortView" in labels
    assert not any("0x00" in label or label == "Off" for label in labels)


def test_preset_survives_a_monitor_with_no_e2():
    """The U2412M has no preset-status register. The control still writes and verifies; it just
    cannot reflect what the panel chose, and says so."""
    preset = build(CAPS_U2412M).by_key("image.preset")
    assert preset is not None
    assert "cannot report which preset is active" in preset.note


def test_preset_sorts_where_display_order_says_not_at_the_end():
    order = [c.key for c in build(CAPS_P2425D).groups()["Color / Picture"]]
    assert order.index("image.preset") < order.index("image.gain_red")
    assert order.index("image.sharpness") < order.index("image.preset")


# --------------------------------------------------------------------------- PIP / PBP


def test_pip_offers_modes_but_not_the_command_values():
    """0x01 and 0x02 are *commands* (toggle size, cycle position), not selectable modes."""
    page = build(CAPS_P3424WE)
    values = [c.value for c in page.by_key("pip.mode").choices]
    assert values == [0x00, 0x21, 0x22, 0x24]
    assert page.by_key("pip.toggle_size").kind is Kind.ACTION
    assert page.by_key("pip.toggle_position").kind is Kind.ACTION


def test_pip_mode_gets_a_timeout_long_enough_for_a_blanking_panel():
    assert build(CAPS_P3424WE).by_key("pip.mode").timeout >= 25.0


def test_pip_sub_window_uses_the_input_names():
    page = build(CAPS_P3424WE, names={0x11: "Games console"})
    labels = [c.label for c in page.by_key("pip.sub_input").choices]
    assert "Games console" in labels


# --------------------------------------------------------------------------- MST


def test_old_spec_mst_is_a_readout_with_an_explanation_not_a_dead_toggle():
    """Hardware-proven on the P2725HE: 0xEF reads 0x00 with MST both off and on, and every legal
    value was written to both ends of a live chain without disturbing it."""
    mst = build(CAPS_P2725HE).by_key("mst.enable")
    assert mst.kind is Kind.READOUT
    assert "on-screen menu" in mst.note


def test_new_spec_mst_gets_a_toggle_and_is_badged_untested():
    mst = build(CAPS_NEW_SPEC_MST).by_key("mst.enable")
    assert mst.kind is Kind.TOGGLE
    assert mst.experimental


def test_usbc_priority_is_offered_and_says_it_cannot_be_confirmed():
    priority = build(CAPS_P2725HE).by_key("mst.usbc_priority")
    assert [c.value for c in priority.choices] == [0xF800, 0xF801]
    assert "cannot be read back" in priority.note
    assert priority.confirm


def test_no_mst_register_means_no_mst_tab():
    assert "MST" not in groups(build(CAPS_P2425D))


# --------------------------------------------------------------------------- USB KVM


def test_bitpacked_kvm_offers_one_selector_per_non_usb_c_input():
    """USB-C and Thunderbolt inputs carry USB on the same cable, so they self-pair and get no
    selector. On the P3424WE that leaves DP-1 and HDMI-1."""
    page = build(CAPS_P3424WE)
    pairs = sorted(k for k in keys(page) if k.startswith(C.KVM_PAIR_PREFIX))
    assert pairs == ["kvm.pair.0f", "kvm.pair.11"]
    assert C.pair_key(0x1B) not in keys(page)


def test_kvm_selectors_are_one_write_group():
    """All the fields live in one 16-bit register, so writing any of them re-sends the others."""
    page = build(CAPS_P3424WE)
    for key in ("kvm.pair.0f", "kvm.pair.11"):
        assert set(page.by_key(key).writes_with) == {"kvm.pair.0f", "kvm.pair.11"}


def test_kvm_upstream_choices_come_from_the_advertised_indices():
    labels = [c.label for c in build(CAPS_P3424WE).by_key("kvm.pair.0f").choices]
    assert labels == ["USB-C", "USB-B"]


def test_bit_positions_match_the_hardware_verified_transitions():
    """The P3424WE decode, replayed: 0x1400 (both on USB-B) -> HDMI to USB-C -> 0x1000."""
    pairings = dict(F.usb_kvm_pairings(CAPS_P3424WE))
    assert pairings == {0x0F: 12, 0x11: 10}
    assert F.usb_kvm_field_value(0x1400, 10) == 1
    assert F.usb_kvm_set_field(0x1400, 10, 0) == 0x1000
    assert F.usb_kvm_set_field(0x1000, 12, 0) == 0x0000
    # ...and back: from 0x0400 (HDMI on USB-B, DP on USB-C) put DP back on USB-B.
    assert F.usb_kvm_set_field(0x0400, 12, 1) == 0x1400


def test_a_kvm_without_a_known_regime_says_so_rather_than_guessing():
    caps = {0x60: [0x0F], 0xE7: [0xAB]}
    upstream = build(caps).by_key("kvm.upstream")
    assert upstream.kind is Kind.READOUT
    assert "on-screen menu" in upstream.note


# --------------------------------------------------------------------------- actions


def test_factory_reset_is_gated_on_the_monitor_advertising_it():
    assert "action.factory_reset" in keys(build(CAPS_P2425D))
    assert "action.factory_reset" not in keys(build(CAPS_P2319H))


def test_factory_reset_confirms_and_says_there_is_no_undo():
    reset = build(CAPS_P2425D).by_key("action.factory_reset")
    assert reset.confirm
    assert "no undo" in reset.confirm_detail


def test_calibration_is_offered_and_warns_about_the_flash():
    calibrate = build(CAPS_P2425D).by_key("action.calibrate")
    assert calibrate.kind is Kind.ACTION
    assert calibrate.confirm
    assert calibrate.timeout > 60
    assert "flash" in calibrate.confirm_detail


# --------------------------------------------------------------------------- input names


def test_input_names_are_editable_text_and_relabel_the_input_control():
    page = build(CAPS_P2425D, names={0x0F: "Work laptop"})
    assert page.by_key(C.input_name_key(0x0F)).kind is Kind.TEXT
    labels = [c.label for c in page.by_key("settings.input").choices]
    assert labels == ["Work laptop", "HDMI-1"]


def test_a_blank_input_name_falls_back_to_the_ddc_name():
    labels = [c.label for c in build(CAPS_P2425D, names={0x0F: "  "}).by_key(
        "settings.input").choices]
    assert labels == ["DisplayPort-1", "HDMI-1"]


# --------------------------------------------------------------------------- rendering


def test_every_capability_this_module_can_produce_renders(qapp):
    """The renderer is shared, so a kind no widget handles fails silently as a grey label. This
    builds the widest page the module can produce and asserts each row got its real control."""
    from PyQt6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QPushButton

    from hardware_ui.shell.form import build_forms

    page = build(
        {**CAPS_P3424WE, **CAPS_P2725HE, 0x62: None, 0x8D: [0x01, 0x02], 0xAA: [0x01]},
        info=[("Brand", "Dell"), ("Model", "P3424WE")],
    )
    forms = build_forms(page, lambda *_: None, lambda *_: None)
    assert list(forms) == F.TAB_ORDER

    # ACTION and TEXT are each a holder -- a button plus a result indicator, a field plus Save --
    # so the widget inside is looked up rather than being the control itself.
    expected = {
        Kind.TOGGLE: QCheckBox,
        Kind.CHOICE: QComboBox,
    }
    seen = set()
    for form in (*forms.values(), *build_forms(build(CAPS_NEW_SPEC_MST), lambda *_: None,
                                               lambda *_: None).values()):
        for key, row in form._rows.items():
            seen.add(row.cap.kind)
            if row.cap.kind is Kind.ACTION:
                assert isinstance(row.control.button, QPushButton), key
                assert row.control.result is not None, key
                continue
            if row.cap.kind is Kind.TEXT:
                assert isinstance(row.control.line, QLineEdit), key
                assert isinstance(row.control.save, QPushButton), key
                continue
            widget = expected.get(row.cap.kind)
            assert widget is None or isinstance(row.control, widget), key
    # Every kind this module emits must have been exercised, or the assertion above proves
    # nothing about the one that was missed.
    assert seen == {Kind.READOUT, Kind.RANGE, Kind.CHOICE, Kind.TEXT, Kind.ACTION, Kind.TOGGLE}
    qapp.processEvents()


# --------------------------------------------------------------------------- sidebar


def sidebar_rows():
    from hardware_ui.core import Category, DeviceInfo, State, Support, Transport
    from hardware_ui.shell.window import Sidebar

    def dev(uid, name, category, transport, state, **props):
        return DeviceInfo(
            uid=uid, name=name, transport=transport, category=category, state=state,
            support=Support.VERIFIED, module_id="m", properties=props,
        )

    bar = Sidebar()
    bar.reconcile(
        [
            dev("a", "WH-1000XM4", Category.AUDIO, Transport.BLUETOOTH, State.CONNECTED),
            dev("b", "WH-1000XM3", Category.AUDIO, Transport.BLUETOOTH, State.PAIRED),
            dev("c", "DELL P2425D", Category.DISPLAY, Transport.DISPLAY, State.PRESENT,
                connector="card1-DP-3"),
            dev("d", "DELL P2425D", Category.DISPLAY, Transport.DISPLAY, State.PRESENT,
                connector="card1-DP-4"),
        ]
    )
    return bar


def rows_of(bar) -> list[str]:
    return [bar._list.item(i).text() for i in range(bar._list.count())]


def headings_of(bar) -> list[str]:
    """Headers are the items with no flags -- "DELL P2425D\nDP-3".isupper() is also True, so
    testing the text would quietly pass while measuring the wrong thing."""
    from PyQt6.QtCore import Qt

    return [
        bar._list.item(i).text()
        for i in range(bar._list.count())
        if bar._list.item(i).flags() == Qt.ItemFlag.NoItemFlags
    ]


def test_each_sidebar_heading_appears_once(qapp):
    """A switched-off headset used to sink below the monitors and re-emit an AUDIO heading, so
    the list showed AUDIO twice. Reachability groups before category, not after."""
    headings = headings_of(sidebar_rows())
    assert headings == ["AUDIO", "DISPLAY", "DISCONNECTED DEVICES"]
    assert len(headings) == len(set(headings))


def test_an_unreachable_device_sits_under_the_disconnected_heading(qapp):
    rows = rows_of(sidebar_rows())
    assert rows.index("DISCONNECTED DEVICES") < next(
        i for i, r in enumerate(rows) if r.startswith("WH-1000XM3")
    )


def test_two_identical_monitors_are_told_apart_by_their_connector(qapp):
    """Both panels publish the same EDID name. The connector is the only distinguishing thing a
    user can act on -- without it the sidebar shows the same row twice."""
    rows = [r for r in rows_of(sidebar_rows()) if r.startswith("DELL")]
    assert rows == ["DELL P2425D\nConnection: DP-3", "DELL P2425D\nConnection: DP-4"]


# --------------------------------------------------------------------------- several at once


def _fake_device_class():
    """A device that opens instantly, so the controller can be exercised without hardware.

    Subclasses the real ``Device`` so the controller's ``type(d).fetch_photo is not
    Device.fetch_photo`` check -- and anything else that inspects the base -- behaves as it does
    for a real module.
    """
    from hardware_ui.core import Device

    class _FakeDevice(Device):
        def __init__(self, info):
            super().__init__(info)
            self.closed = False
            self._caps = build(CAPS_P2425D)

        @property
        def capabilities(self):
            return self._caps

        async def connect(self):
            return None

        async def disconnect(self):
            self.closed = True

        async def get(self, key):
            return None

        async def set(self, key, value):
            return None

    return _FakeDevice


def controller_with_two_monitors(qapp):
    import asyncio

    from hardware_ui.core import Category, DeviceInfo, State, Support, Transport
    from hardware_ui.shell.app import Controller
    from hardware_ui.shell.window import MainWindow

    def dev(uid, connector):
        return DeviceInfo(
            uid=uid, name="DELL P2425D", transport=Transport.DISPLAY,
            category=Category.DISPLAY, state=State.PRESENT, support=Support.VERIFIED,
            module_id="dell_monitors", properties={"connector": connector},
        )

    class _Bridge:
        """Runs coroutines synchronously and applies UI calls straight away -- the threading is
        not what these tests are about."""

        def spawn(self, coro, label=""):
            asyncio.get_event_loop().run_until_complete(coro)

        def call_on_ui(self, fn, *a, **kw):
            fn(*a, **kw)

    class _Registry:
        def get(self, module_id):
            class M:
                @staticmethod
                def load():
                    return _fake_device_class()

            return M()

        def claim(self, info):
            return info

    window = MainWindow()
    controller = Controller(_Registry(), _Bridge(), window)
    controller._devices = [dev("a", "card1-DP-3"), dev("b", "card1-DP-4")]
    return controller


def test_connecting_a_second_monitor_leaves_the_first_open(qapp):
    """Opening monitor B used to silently close monitor A: the controller held exactly one
    device. DDC/CI has no session to hold -- every operation is its own ddcutil invocation -- so
    closing one to open another was pure loss."""
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    c = controller_with_two_monitors(qapp)

    c.select("a")
    c.connect_device()
    first = c._open["a"]
    assert c.connected

    c.select("b")
    c.connect_device()

    assert set(c._open) == {"a", "b"}
    assert not first.closed
    assert c.connected

    # Going back shows the still-open device rather than an empty page.
    c.select("a")
    assert c.connected and c._device is first


def test_disconnecting_one_leaves_the_other_alone(qapp):
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    c = controller_with_two_monitors(qapp)
    for uid in ("a", "b"):
        c.select(uid)
        c.connect_device()

    c.select("a")
    c.disconnect_device()

    assert set(c._open) == {"b"}
    assert not c.connected
    c.select("b")
    assert c.connected


def test_an_open_device_shows_as_connected_without_rewriting_enumeration(qapp):
    """A headset BlueZ reports as connected must not be demoted when we close its page: the two
    meanings of "connected" are different, and conflating them moved a live device into the
    disconnected section."""
    import asyncio

    from hardware_ui.core import State

    asyncio.set_event_loop(asyncio.new_event_loop())
    c = controller_with_two_monitors(qapp)
    c.select("a")
    c.connect_device()

    assert {d.uid: d.state for d in c._visible()} == {"a": State.CONNECTED, "b": State.PRESENT}
    assert [d.state for d in c._devices] == [State.PRESENT, State.PRESENT]

    c.disconnect_device()
    assert {d.uid: d.state for d in c._visible()} == {"a": State.PRESENT, "b": State.PRESENT}


def test_an_action_reports_whether_it_worked(qapp):
    """A button whose effect is invisible -- a self-test, a reset -- must not leave "nothing
    happened" and "it worked" looking identical."""
    from hardware_ui.shell.form import CapabilityForm

    form = CapabilityForm()
    form.build(build(CAPS_P2425D))
    key = "action.calibrate"
    assert form._rows[key].control.result.text() == ""

    form.set_result(key, True, "Calibrated 6 sliders")
    assert form._rows[key].control.result.text() == "✓"
    assert "Calibrated" in form._rows[key].control.result.toolTip()

    form.set_result(key, False, "The monitor refused")
    assert form._rows[key].control.result.text() == "✗"

    form.clear_result(key)
    assert form._rows[key].control.result.text() == ""


# --------------------------------------------------------------------------- stale handles


def _controller_with_open(monkeypatch, opened_path):
    """A controller holding one open device, so staleness can be judged."""
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    controller._open = {}
    controller._polls = {}
    controller._watches = {}
    controller._busy_uids = set()
    controller._reconnecting_uids = set()
    controller._selected = None
    controller._ui = lambda *a, **k: None

    class FakeDevice:
        def __init__(self, info):
            self.info = info
            self.closed = False

        async def disconnect(self):
            self.closed = True

    info = DeviceInfo(uid="hid:3-3", name="Key", transport=Transport.HID, path=opened_path)
    device = FakeDevice(info)
    controller._open["hid:3-3"] = device
    return controller, device


def test_an_unplugged_device_stops_showing_as_connected(monkeypatch):
    """It kept a green dot forever: the row is connected because the uid is in `_open`."""
    import asyncio

    controller, device = _controller_with_open(monkeypatch, "/dev/hidraw19")
    controller._devices = []                       # gone from the bus
    asyncio.run(controller._drop_stale())
    assert controller._open == {}
    assert device.closed


def test_a_device_that_came_back_on_a_different_node_is_reopened(monkeypatch):
    """A YubiKey keeps its uid across a replug -- that is deliberate, settings follow it -- but
    its /dev/hidraw node is a new one, and the old descriptor still opens and reads nothing."""
    import asyncio

    from hardware_ui.core import DeviceInfo, Transport

    controller, device = _controller_with_open(monkeypatch, "/dev/hidraw19")
    controller._devices = [
        DeviceInfo(uid="hid:3-3", name="Key", transport=Transport.HID, path="/dev/hidraw21")
    ]
    asyncio.run(controller._drop_stale())
    assert controller._open == {}
    assert device.closed


def test_a_device_that_never_moved_is_left_alone(monkeypatch):
    import asyncio

    from hardware_ui.core import DeviceInfo, Transport

    controller, device = _controller_with_open(monkeypatch, "/dev/hidraw19")
    controller._devices = [
        DeviceInfo(uid="hid:3-3", name="Key", transport=Transport.HID, path="/dev/hidraw19")
    ]
    asyncio.run(controller._drop_stale())
    assert "hid:3-3" in controller._open
    assert not device.closed


def test_opening_one_device_does_not_disable_another_ones_button(qapp):
    """Start opening A, switch to B: B's Connect button must still work.

    A single busy flag followed the selection instead of the device, so B was greyed out and
    claimed to be connecting while A was the one actually being opened.
    """
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    controller._open = {}
    controller._busy_uids = set()
    controller._reconnecting_uids = set()
    controller._painted_open = frozenset()
    shown: list[dict] = []

    class FakePage:
        def set_connection(self, **kwargs):
            shown.append(kwargs)

    class FakeWindow:
        page = FakePage()

    controller._window = FakeWindow()
    a = DeviceInfo(uid="a", name="A", transport=Transport.HID)
    b = DeviceInfo(uid="b", name="B", transport=Transport.HID)

    controller._busy_uids = {"a"}       # A is being opened
    controller._selected = a
    controller._refresh_connection_ui()
    assert shown[-1]["busy"] is True    # A's own button: busy, as it should be

    controller._selected = b            # user switches to B mid-connect
    controller._refresh_connection_ui()
    assert shown[-1]["busy"] is False, "B must not be disabled by A opening"


def test_a_restarting_device_only_marks_its_own_page(qapp):
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    controller._open = {}
    controller._busy_uids = set()
    controller._reconnecting_uids = {"a"}
    controller._painted_open = frozenset()
    shown: list[dict] = []

    class FakePage:
        def set_connection(self, **kwargs):
            shown.append(kwargs)

    class FakeWindow:
        page = FakePage()

    controller._window = FakeWindow()
    controller._selected = DeviceInfo(uid="b", name="B", transport=Transport.HID)
    controller._refresh_connection_ui()
    assert shown[-1]["reconnecting"] is False


def test_one_device_finishing_does_not_free_another_ones_button(qapp):
    """Two devices opening at once: whichever finishes first must not release the other.

    The narrow race left behind when a single busy flag became a single busy uid — device 1
    completing cleared the state device 2 was still relying on, and its Connect button came alive
    while it was mid-connect. Slow enumeration makes the window wide enough to hit.
    """
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    controller._busy_uids = set()
    controller._reconnecting_uids = set()
    controller._open = {}
    controller._painted_open = frozenset()
    shown: list[dict] = []

    class FakePage:
        def set_connection(self, **kwargs):
            shown.append(kwargs)

    class FakeWindow:
        page = FakePage()

    controller._window = FakeWindow()
    controller._ui = lambda fn, *a: fn(*a)

    one = DeviceInfo(uid="one", name="One", transport=Transport.HID)
    two = DeviceInfo(uid="two", name="Two", transport=Transport.HID)
    controller._selected = two          # set before anything repaints

    controller._busy(one.uid, True)
    controller._busy(two.uid, True)
    controller._refresh_connection_ui()
    assert shown[-1]["busy"] is True

    controller._busy(one.uid, False)          # device one finishes first
    controller._refresh_connection_ui()
    assert shown[-1]["busy"] is True, "two is still connecting; its button must stay disabled"

    controller._busy(two.uid, False)
    controller._refresh_connection_ui()
    assert shown[-1]["busy"] is False


def test_a_failure_releases_only_the_device_that_failed(qapp):
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.shell.app import Controller

    controller = Controller.__new__(Controller)
    controller._busy_uids = {"one", "two"}
    controller._reconnecting_uids = set()
    controller._open = {}
    controller._painted_open = frozenset()
    controller._idle_status = ""
    controller._ui = lambda *a, **k: None

    class FakeWindow:
        class page:
            @staticmethod
            def set_connection(**kwargs):
                pass

        class sidebar:
            @staticmethod
            def set_status(_):
                pass

        @staticmethod
        def notify(*_a, **_k):
            pass

    controller._window = FakeWindow()
    controller._selected = DeviceInfo(uid="two", name="Two", transport=Transport.HID)
    controller._fail("nope", "one")
    assert controller._busy_uids == {"two"}


def test_teardown_clears_every_bit_of_state_keyed_to_the_device(qapp):
    """Not only the handle.

    A device torn down while it was still opening — unplugged mid-connect — kept its uid in the
    busy set, and its Connect button stayed disabled for the rest of the session if it came back
    under the same uid. The same shape as every other bug this page has had.
    """
    import asyncio

    controller, device = _controller_with_open(None, "/dev/hidraw19")
    controller._busy_uids = {"hid:3-3"}
    controller._reconnecting_uids = {"hid:3-3"}
    async def run():
        controller._polls["hid:3-3"] = asyncio.get_running_loop().create_future()
        controller._watches["hid:3-3"] = asyncio.get_running_loop().create_future()
        await controller._teardown("hid:3-3")

    asyncio.run(run())

    assert controller._open == {}
    assert controller._busy_uids == set()
    assert controller._reconnecting_uids == set()
    assert controller._polls == {} and controller._watches == {}
