"""Razer module tests. No hardware and no OpenRazer needed.

The module must be importable and its page buildable on a machine with no `openrazer` package at
all — that is the whole point of it being a per-module requirement rather than an application one.
"""

from __future__ import annotations

from hardware_ui.core import Kind
from hardware_ui.modules.razer_peripherals import capabilities as C
from hardware_ui.modules.razer_peripherals.device import _normalise, _rgb

#: What a BlackWidow Chroma V2 reports, taken from the live device.
KEYBOARD_EFFECTS = [
    "none", "static", "spectrum", "wave", "reactive", "breath_single", "breath_dual",
    "breath_random", "starlight_single", "starlight_dual", "starlight_random", "ripple",
    "ripple_random",
]
#: What a DeathAdder V2's logo zone reports — no wave, starlight or ripple.
MOUSE_EFFECTS = ["none", "static", "spectrum", "reactive", "breath_single", "breath_dual",
                 "breath_random"]


def keys(page) -> list[str]:
    return [c.key for c in page]


# --------------------------------------------------------------------------- importability


def test_the_module_imports_without_openrazer_installed():
    """OpenRazer is this module's requirement, not the application's. Nothing outside device.py
    imports it, and device.py only imports it inside connect()."""
    import hardware_ui.modules.razer_peripherals.device as mod

    source = __import__("pathlib").Path(mod.__file__).read_text()
    top_level = [
        ln for ln in source.splitlines()
        if ln.startswith(("import ", "from ")) and "openrazer" in ln
    ]
    assert top_level == [], f"openrazer imported at module scope: {top_level}"


def test_a_missing_openrazer_reads_as_something_to_install():
    from hardware_ui.modules.razer_peripherals.device import INSTALL_HINT

    assert "emerge" in INSTALL_HINT
    assert "Nothing else in this application needs it" in INSTALL_HINT


# --------------------------------------------------------------------------- effect gating


def test_the_colour_appears_only_for_effects_that_take_one():
    page = C.build(zones=[("main", KEYBOARD_EFFECTS, True)])
    colour = page.by_key("light.main.color")
    assert colour.kind is Kind.COLOR
    assert colour.requires == "light.main.effect"
    takers = set(colour.requires_value)
    assert {"static", "reactive", "breath_single", "starlight_single", "ripple"} <= takers
    # Spectrum, Off and the random variants take no colour and must not offer one.
    assert takers.isdisjoint({"spectrum", "none", "breath_random", "ripple_random",
                              "starlight_random"})


def test_the_second_colour_is_only_for_dual_effects():
    page = C.build(zones=[("main", KEYBOARD_EFFECTS, True)])
    assert set(page.by_key("light.main.color2").requires_value) == {
        "breath_dual", "starlight_dual"
    }


def test_direction_appears_only_for_wave_and_only_where_wave_exists():
    with_wave = C.build(zones=[("main", KEYBOARD_EFFECTS, True)])
    assert with_wave.by_key("light.main.direction").requires_value == ("wave",)
    # The mouse has no wave, so it gets no direction control at all.
    without = C.build(zones=[("logo", MOUSE_EFFECTS, True)])
    assert "light.logo.direction" not in keys(without)


def test_speed_is_gated_to_the_effects_that_take_a_time_argument():
    """`reactive` and the starlight variants cannot be invoked without one."""
    page = C.build(zones=[("main", KEYBOARD_EFFECTS, True)])
    assert set(page.by_key("light.main.speed").requires_value) == {
        "reactive", "starlight_single", "starlight_dual", "starlight_random"
    }


def test_a_zone_offers_only_the_effects_it_advertises():
    page = C.build(zones=[("logo", MOUSE_EFFECTS, True)])
    offered = [c.value for c in page.by_key("light.logo.effect").choices]
    assert offered == ["none", "static", "spectrum", "reactive", "breath_single",
                       "breath_dual", "breath_random"]
    assert "wave" not in offered


def test_effect_order_is_the_declared_order_not_the_devices():
    page = C.build(zones=[("main", list(reversed(KEYBOARD_EFFECTS)), True)])
    offered = [c.value for c in page.by_key("light.main.effect").choices]
    assert offered[0] == "none" and offered[1] == "static"


# --------------------------------------------------------------------------- device settings


def test_a_free_dpi_range_gives_both_axes():
    """OpenRazer stores DPI as an (x, y) pair and both are settable, so both are offered."""
    free = C.build(dpi_max=20000)
    assert free.by_key("device.dpi_x").kind is Kind.RANGE
    assert free.by_key("device.dpi_y").kind is Kind.RANGE
    assert free.by_key("device.dpi_x").maximum == 20000
    # One property holds the pair, so writing either must hold both pending.
    assert set(free.by_key("device.dpi_x").writes_with) == {"device.dpi_x", "device.dpi_y"}


def test_a_fixed_dpi_list_replaces_the_sliders():
    fixed = C.build(dpi_max=20000, dpi_fixed=[800, 1600, 3200])
    assert fixed.by_key("device.dpi").kind is Kind.CHOICE
    assert [c.value for c in fixed.by_key("device.dpi").choices] == [800, 1600, 3200]
    assert "device.dpi_x" not in keys(fixed)


def test_hardware_dpi_stages_appear_only_when_the_device_reports_them():
    assert "device.dpi_stage" not in keys(C.build(dpi_max=20000))
    staged = C.build(dpi_max=20000, dpi_stages=[(800, 800), (1600, 1600)])
    stage = staged.by_key("device.dpi_stage")
    assert [c.value for c in stage.choices] == [1, 2]
    assert "1600 × 1600" in stage.choices[1].label
    assert stage.experimental


def test_the_poll_rate_fallback_does_not_offer_rates_a_mouse_cannot_do():
    """A DeathAdder V2 reports has("poll_rate") true and has("supported_poll_rates") FALSE. Falling
    back to every rate the client defines offered 8000 Hz on a 1000 Hz mouse."""
    assert C.POLL_RATES_FALLBACK == (125, 500, 1000)
    assert 8000 not in C.POLL_RATES_FALLBACK
    page = C.build(poll_rates=C.POLL_RATES_FALLBACK)
    assert [c.value for c in page.by_key("device.poll_rate").choices] == [125, 500, 1000]


def test_a_device_that_reports_its_rates_gets_exactly_those():
    page = C.build(poll_rates=[125, 500, 1000, 2000, 4000, 8000])
    assert 8000 in [c.value for c in page.by_key("device.poll_rate").choices]


def test_a_keyboard_gets_game_mode_and_a_mouse_does_not():
    assert "device.game_mode" in keys(C.build(game_mode=True))
    assert "device.game_mode" not in keys(C.build(dpi_max=20000))


def test_battery_options_are_badged_because_no_wireless_device_was_available():
    page = C.build(battery=True)
    assert page.by_key("info.battery").kind is Kind.METER
    assert page.by_key("info.battery").experimental
    assert page.by_key("device.low_battery_threshold").experimental


def test_no_battery_means_no_power_section():
    assert "device.idle_time" not in keys(C.build(dpi_max=20000))


# --------------------------------------------------------------------------- zone naming


def test_the_scroll_capability_maps_to_the_scroll_wheel_attribute():
    """A DeathAdder V2 advertises `lighting_scroll_*` but the object is `fx.misc.scroll_wheel`.
    Deriving the attribute by concatenation raises AttributeError on a supported feature."""
    assert C.ZONE_ATTRS["scroll"] == "scroll_wheel"
    assert C.ZONE_ATTRS["logo"] == "logo"


def test_zones_become_sections_of_one_lighting_tab():
    page = C.build(zones=[("logo", MOUSE_EFFECTS, True), ("scroll", MOUSE_EFFECTS, True)])
    assert list(page.groups()) == ["Lighting"]
    sections = [c.section for c in page]
    assert sections[0] == "Logo"
    assert "Scroll wheel" in sections


def test_zone_is_recoverable_from_a_key():
    assert C.zone_of("light.scroll.color") == "scroll"
    assert C.zone_of("device.dpi") == ""


# --------------------------------------------------------------------------- helpers


def test_a_doubled_vendor_prefix_is_normalised():
    """sysfs concatenates USB manufacturer and product, and Razer puts the brand in both, so the
    daemon's "Razer DeathAdder V2" must match sysfs's "Razer Razer DeathAdder V2"."""
    assert _normalise("Razer Razer DeathAdder V2") == "razer deathadder v2"
    assert _normalise("Razer DeathAdder V2") == "razer deathadder v2"
    assert _normalise("Razer BlackWidow Chroma V2") == "razer blackwidow chroma v2"


def test_colour_parsing_survives_junk():
    assert _rgb("#ff8000") == (255, 128, 0)
    assert _rgb("ff8000") == (255, 128, 0)
    assert _rgb("") == (0, 255, 0)
    assert _rgb("#nothex") == (0, 255, 0)


def test_the_lighting_note_says_what_is_deliberately_absent():
    note = C.build(zones=[("main", KEYBOARD_EFFECTS, True)]).by_key("light.main.effect").note
    assert "RGB editor" in note
    assert "OpenRGB" in note


def test_an_implausible_dpi_readback_is_ignored():
    """The daemon can answer (0, 0) transiently right after a write. Zero is not a DPI any mouse
    holds, so publishing it would show a 0 for a setting that applied correctly."""
    from hardware_ui.modules.razer_peripherals.device import _sane_dpi

    assert _sane_dpi((1600, 1600)) == (1600, 1600)
    assert _sane_dpi((0, 0)) is None
    assert _sane_dpi((1600, 0)) is None
    assert _sane_dpi(None) is None
    assert _sane_dpi((1600,)) is None


def test_default_stages_match_polychromatics_derivation():
    """A 20000 DPI mouse gets 2000/2500/5000/10000/20000 in both applications."""
    assert C.default_dpi_stages(20000) == [(2000, 2000), (2500, 2500), (5000, 5000),
                                           (10000, 10000), (20000, 20000)]
    # Maxima Polychromatic tabulates come from the table, not the formula.
    assert C.default_dpi_stages(16000)[0] == (800, 800)


def test_the_axis_lock_exists_and_stages_carry_both_axes():
    page = C.build(dpi_max=20000, stages=C.default_dpi_stages(20000), can_sync=False)
    assert page.by_key("device.dpi_lock").kind is Kind.TOGGLE
    assert page.by_key("dpi.stage.1.x").maximum == 20000
    assert page.by_key("dpi.stage.1.y").label == "Vertical"
    # Sync is present but not writable when the mouse cannot store stages.
    assert not page.by_key("dpi.sync").writable
    assert "cannot store DPI stages" in page.by_key("dpi.sync").note


def test_sync_is_writable_when_the_mouse_supports_stages():
    page = C.build(dpi_max=20000, stages=C.default_dpi_stages(20000), can_sync=True)
    assert page.by_key("dpi.sync").writable
    assert "DPI buttons cycle" in page.by_key("dpi.sync").note


# --------------------------------------------------------------------------- macros


def test_the_macro_tab_offers_no_record_or_test_button():
    """OpenRazer exposes exactly getMacros/addMacro/deleteMacro to a client. Recording is a
    keyboard gesture handled inside the daemon, and `play_macro` has no D-Bus endpoint — so a
    Record or Test button could not work, and offering one would be a lie."""
    page = C.macros([("M1", 4)], has_modifier=True, has_led=True, has_saved=True)
    buttons = [c.action_label.casefold() for c in page if c.kind is Kind.ACTION]
    assert buttons, "the tab should still have some actions"
    # No button starts a recording or plays one back. "Recorded macros" is a readout label, which
    # is why this checks the buttons rather than every string on the tab.
    assert not any(b.startswith(("record", "test", "play", "run")) for b in buttons), buttons


def test_the_macro_tab_explains_how_recording_actually_works():
    note = C.macros([]).by_key if False else next(
        c for c in C.macros([]) if c.key == C.MACRO_STATUS_KEY
    ).note
    assert "on the keyboard itself" in note
    assert "M1–M5" in note
    # The two facts a user is bitten by, straight from OpenRazer's behaviour.
    assert "lost when it stops" in note or "disappear" in note
    assert "replay instantly" in note


def test_recorded_macros_become_individually_deletable():
    page = C.macros([("M1", 4), ("M3", 12)])
    keys_ = [c.key for c in page]
    assert C.macro_delete_key("M1") in keys_
    assert C.macro_delete_key("M3") in keys_
    delete = next(c for c in page if c.key == C.macro_delete_key("M3"))
    assert delete.confirm and "cannot be recovered" in delete.confirm_detail
    assert "12 keys" in delete.label
    assert C.macro_bind_key(C.macro_delete_key("M3")) == "M3"


def test_restore_is_disabled_until_something_has_been_saved():
    assert not next(c for c in C.macros([], has_saved=False)
                    if c.key == C.MACRO_RESTORE_KEY).writable
    assert next(c for c in C.macros([], has_saved=True)
                if c.key == C.MACRO_RESTORE_KEY).writable


def test_restore_on_connect_is_opt_in():
    """It writes to the keyboard, which must not happen merely because a page was opened."""
    auto = next(c for c in C.macros([]) if c.key == C.MACRO_AUTORESTORE_KEY)
    assert auto.kind is Kind.TOGGLE
    assert "Off by default" in auto.description


def test_export_and_import_ask_the_shell_for_a_file():
    """A module runs on the asyncio thread and must never open a dialog itself; it declares the
    need and the shell raises the platform chooser."""
    page = C.macros([("M1", 3)], has_saved=True)
    export = next(c for c in page if c.key == C.MACRO_EXPORT_KEY)
    imp = next(c for c in page if c.key == C.MACRO_IMPORT_KEY)
    assert (export.file_dialog, imp.file_dialog) == ("save", "open")
    assert export.file_suffix == ".json"
    assert "*.json" in export.file_filter
    assert imp.confirm  # importing replaces whatever is on those keys


def test_export_is_disabled_when_there_is_nothing_to_export():
    assert not next(c for c in C.macros([]) if c.key == C.MACRO_EXPORT_KEY).writable
    assert next(c for c in C.macros([("M1", 1)]) if c.key == C.MACRO_EXPORT_KEY).writable


# --------------------------------------------------------------------------- product photo


def test_the_module_offers_a_vendor_photo_download():
    """OpenRazer carries a URL per device rather than an image, so this is the advertised-link
    case the photo rules were written for -- nothing is shipped and nothing is guessed."""
    from hardware_ui.core import Device
    from hardware_ui.modules.razer_peripherals.device import RazerDevice

    assert RazerDevice.fetch_photo is not Device.fetch_photo


def test_a_non_https_image_url_is_refused():
    """This is an unattended download of a remote file; plain http is not good enough for it."""
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.modules.razer_peripherals.device import RazerDevice

    class FakeRazer:
        device_image = "http://example.invalid/x.png"
        razer_urls = {}

    dev = RazerDevice(DeviceInfo(uid="hid:x", name="Razer X", transport=Transport.HID))
    dev._rdevice = FakeRazer()
    assert dev._fetch_photo_sync() is None


def test_no_image_url_is_a_normal_answer_not_an_error():
    from hardware_ui.core import DeviceInfo, Transport
    from hardware_ui.modules.razer_peripherals.device import RazerDevice

    class FakeRazer:
        device_image = ""
        razer_urls = {}

    dev = RazerDevice(DeviceInfo(uid="hid:x", name="Razer X", transport=Transport.HID))
    dev._rdevice = FakeRazer()
    assert dev._fetch_photo_sync() is None


def test_missing_software_is_reported_as_something_to_install_not_a_dead_device():
    """`Unreachable` means the hardware is not answering, and the shell adds "Switch it on, then
    Rescan" to it. For a missing package that advice is wrong and the wrapping mangled the
    message — "…is OpenRazer is needed… Switch it on, then Rescan." Hence a distinct exception,
    which the shell shows verbatim."""
    import inspect

    from hardware_ui.core import DependencyMissing, Unreachable
    from hardware_ui.modules.razer_peripherals import device as mod

    source = inspect.getsource(mod.RazerDevice._connect_sync)
    assert "DependencyMissing(INSTALL_HINT)" in source
    assert "Unreachable(INSTALL_HINT)" not in source
    assert issubclass(DependencyMissing, Exception)
    assert not issubclass(DependencyMissing, Unreachable)


def test_the_shell_prints_a_dependency_message_unchanged():
    import inspect

    from hardware_ui.shell import app

    source = inspect.getsource(app.Controller._connect)
    # The dependency branch must come before the Unreachable one and must not decorate the text.
    assert source.index("except DependencyMissing") < source.index("except Unreachable")
    assert "self._fail(str(exc), uid)" in source, "shown verbatim, with no wrapping"
