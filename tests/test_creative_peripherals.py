"""The Creative module, exercised without a Creative device.

No Sound Blaster is attached to the machine this was written on, so everything here runs against
fakes -- with one exception that is worth more than all the rest: the unlock handshake is checked
against a challenge/response pair **captured from real hardware**. If the port broke the crypto,
that assertion fails, and no amount of fake-driven testing would have caught it.

What the fakes are for is the layer above: that the masks a device reports turn into the right
controls, that a write reaches the right protocol call, and that the two interlocks carried over
from the source project -- Direct Mode bypassing the equaliser, and the refusal to send firmware
or factory-reset commands -- survive the port.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from hardware_ui.core.capability import Kind
from hardware_ui.core.device import Category, DeviceInfo, Transport
from hardware_ui.modules.creative_peripherals import capabilities as C
from hardware_ui.modules.creative_peripherals import transport as T
from hardware_ui.modules.creative_peripherals.controller import (
    CreativeController,
    DeviceError,
    SafetyError,
)
from hardware_ui.modules.creative_peripherals.device import CreativeDevice
from hardware_ui.modules.creative_peripherals.protocol import framing, unlock
from hardware_ui.modules.creative_peripherals.protocol.ids import (
    Cmd,
    Feature,
    Module,
    OutputTarget,
    SubFeature,
)

# --------------------------------------------------------------------------- ground truth


def test_the_unlock_reproduces_a_response_captured_from_hardware():
    """The one assertion here that is not a fake.

    A real X4 issued `CAPTURED_CHALLENGE` and Creative's own DLL answered `CAPTURED_RESPONSE`.
    Reproducing it byte-for-byte from the same random bytes is proof the AES-256-GCM
    reconstruction survived the port intact -- key patching, nonce placement and all.
    """
    assert unlock.self_test()


def test_the_session_key_is_the_base_key_with_four_challenge_bytes_patched_in():
    challenge = bytes(range(36))
    key = unlock.derive_key(challenge)
    assert key[0:2] == challenge[0:2]
    assert key[30:32] == challenge[2:4]
    assert key[2:30] == unlock.BASE_KEY[2:30]


def test_a_fresh_nonce_is_drawn_every_time():
    """Reusing a nonce under a fixed key is the one thing GCM must never do, and the key here is
    fixed by construction -- it is hardcoded in the vendor DLL."""
    challenge = bytes(range(36))
    responder = unlock.UnlockResponder()
    seen = {responder.respond(challenge)[:unlock.NONCE_LEN] for _ in range(20)}
    assert len(seen) == 20


def test_a_short_challenge_is_refused_rather_than_padded():
    with pytest.raises(unlock.UnlockError):
        unlock.UnlockResponder().respond(b"\x00" * 8)


# --------------------------------------------------------------------------- framing


def test_the_frame_form_follows_the_payload_size():
    assert framing.build(0x39, b"\x01")[0] == framing.START_5A
    assert framing.build(0x39, b"\x00" * 300)[0] == framing.START_6A


def test_one_transfer_carries_several_frames():
    """A write draws an Acknowledge, the changed state and any dependent state, and they arrive
    together. Reading only the first frame is how pushed state gets lost."""
    blob = (framing.build(Cmd.ACKNOWLEDGE, b"\x39\x00")
            + framing.build(Cmd.FEATURE_CONTROL, b"\x01\x20\x00\x00\x00"))
    assert [cmd for cmd, _ in framing.split(blob)] == [Cmd.ACKNOWLEDGE, Cmd.FEATURE_CONTROL]


# --------------------------------------------------------------------------- capabilities


def build(feature_support=0, subfeatures=None, has_eq=True, presets=None):
    return {c.key: c for c in C.build(
        feature_support=feature_support, subfeatures=subfeatures,
        has_equalizer_state=has_eq, presets=presets or {})}


def test_a_feature_the_unit_does_not_implement_is_not_offered():
    only_direct = Feature.DIRECT_MODE.mask
    caps = build(feature_support=only_direct)
    assert C.KEY_DIRECT_MODE in caps
    assert C.feature_key(Feature.HP_HIGH_GAIN) not in caps


def test_a_unit_reporting_no_support_mask_is_shown_everything():
    """Permissive on purpose, matching the source's `DeviceState.supports`: a control that turns
    out not to apply is a smaller failure than a working control that was hidden."""
    caps = build(feature_support=0)
    assert C.feature_key(Feature.HP_HIGH_GAIN) in caps


def test_the_equalizer_is_gated_on_direct_mode_being_off():
    caps = build(subfeatures=SubFeature.GRAPHIC_EQ)
    eq = caps[C.KEY_EQ_ENABLED]
    assert eq.requires == C.KEY_DIRECT_MODE
    assert eq.requires_value is False


def test_a_dsp_without_a_graphic_equalizer_gets_no_equalizer_rows():
    caps = build(subfeatures=SubFeature.CRYSTALIZER)
    assert C.KEY_EQ_ENABLED not in caps
    assert C.band_key(0) not in caps


def test_an_advertised_equalizer_that_never_answered_gets_no_sliders():
    """The mask is what the device claims; `has_equalizer_state` is what it did. Twelve reads that
    all timed out must not become twelve dead sliders."""
    assert C.KEY_EQ_ENABLED not in build(subfeatures=SubFeature.GRAPHIC_EQ, has_eq=False)


def test_every_band_is_a_range_in_the_gain_limits_the_windows_app_used():
    caps = build(subfeatures=SubFeature.GRAPHIC_EQ)
    for index in range(10):
        band = caps[C.band_key(index)]
        assert band.kind is Kind.RANGE
        assert (band.minimum, band.maximum) == (-12.0, 12.0)
    assert C.band_index(C.band_key(7)) == 7
    assert C.band_index("eq.band99") is None
    assert C.band_index("output.target") is None


def test_the_preset_row_appears_only_when_there_are_presets():
    assert C.KEY_EQ_PRESET not in build(subfeatures=SubFeature.GRAPHIC_EQ)
    assert C.KEY_EQ_PRESET in build(subfeatures=SubFeature.GRAPHIC_EQ, presets={"Flat": object()})


def test_only_the_two_super_xfi_modes_seen_on_the_wire_are_offered():
    """Creative's app has more and their four-character codes are unknown. Guessing one writes an
    unknown value to the device."""
    assert {c.value for c in build()[C.KEY_SXFI_MODE].choices} == {"MV  ", "PCG "}


# --------------------------------------------------------------------------- transport


class FakeEndpoint:
    def __init__(self, address, attributes=0x02):
        self.bEndpointAddress = address
        self.bmAttributes = attributes


class FakeInterface:
    def __init__(self, number, klass, subclass=0x00, endpoints=()):
        self.bInterfaceNumber = number
        self.bInterfaceClass = klass
        self.bInterfaceSubClass = subclass
        self._endpoints = list(endpoints)

    def __iter__(self):
        return iter(self._endpoints)


class FakeUsbDevice:
    """Only the descriptor surface the transport reads. Nothing is opened."""

    def __init__(self, interfaces, product_id=0x3278, serial="SB-1"):
        self.idVendor, self.idProduct = T.VENDOR_ID, product_id
        self.serial_number = serial
        self._config = list(interfaces)

    def __iter__(self):
        return iter([self._config])


def x4_like(**kwargs):
    """Interface layout matching the X4: audio, then a CDC-ACM function on 1 and 2."""
    return FakeUsbDevice([
        FakeInterface(0, 0x01),
        FakeInterface(1, T.CDC_COMM_CLASS, T.CDC_ACM_SUBCLASS),
        FakeInterface(2, T.CDC_DATA_CLASS, endpoints=[FakeEndpoint(0x03), FakeEndpoint(0x82)]),
    ], **kwargs)


def test_the_cdc_function_and_its_endpoints_come_from_the_descriptors():
    """The source project hardcodes interfaces 1 and 2 and endpoints 0x03/0x82 because it targets
    one card. Reading them is what lets an untested model work."""
    comm, data = T.UsbCdcTransport._find_cdc(x4_like())
    assert (comm.bInterfaceNumber, data.bInterfaceNumber) == (1, 2)
    assert T.UsbCdcTransport._endpoints(data) == (0x03, 0x82)


def test_a_device_with_no_cdc_function_is_reported_as_unconfigurable():
    plain_audio = FakeUsbDevice([FakeInterface(0, 0x01)])
    comm, data = T.UsbCdcTransport._find_cdc(plain_audio)
    assert comm is None and data is None


def test_a_data_interface_without_a_bulk_pair_is_an_error_not_a_guess():
    interrupt_only = FakeInterface(2, T.CDC_DATA_CLASS, endpoints=[FakeEndpoint(0x83, 0x03)])
    with pytest.raises(T.TransportError):
        T.UsbCdcTransport._endpoints(interrupt_only)


def test_the_right_unit_is_picked_by_serial_when_two_are_plugged_in():
    first, second = x4_like(serial="SB-1"), x4_like(serial="SB-2")
    picked = T.UsbCdcTransport(serial="SB-2")._pick([first, second])
    assert picked.serial_number == "SB-2"


def test_an_unreadable_serial_does_not_abort_the_search():
    """`serial_number` raises without permission on some kernels. Falling over there would make
    the device unreachable rather than merely ambiguous."""

    class Unreadable:
        idVendor, idProduct = T.VENDOR_ID, 0x3278

        @property
        def serial_number(self):
            raise ValueError("no permission")

    good = x4_like(serial="SB-2")
    picked = T.UsbCdcTransport(serial="SB-2")._pick([Unreadable(), good])
    assert picked is good


# --------------------------------------------------------------------------- controller safety


class FakeTransport:
    """Records what was written and replays canned frames."""

    def __init__(self, replies=None):
        self.written: list[bytes] = []
        self.unlocked = True
        self.connected = True
        self._replies = list(replies or [])

    def write_raw(self, data):
        self.written.append(data)

    def drain(self, timeout_ms=40):
        return self._replies.pop(0) if self._replies else []

    def close(self):
        self.connected = False


def controller_with(replies=None, **kwargs):
    controller = CreativeController(**kwargs)
    controller._t = FakeTransport(replies)
    return controller


def test_firmware_upgrade_and_factory_reset_are_refused():
    """Carried verbatim from the source. Neither is exposed as a capability, so this guard exists
    for the caller that has not been written yet."""
    controller = controller_with()
    for command in (Cmd.UPGRADE, Cmd.FACTORY_RESET):
        with pytest.raises(SafetyError):
            controller._send(command, b"\x00")
    assert controller._t.written == []


def test_they_are_allowed_only_when_explicitly_unlocked():
    controller = controller_with(allow_dangerous=True)
    controller._send(Cmd.FACTORY_RESET, b"\x00")
    assert len(controller._t.written) == 1


def test_a_failure_acknowledging_a_different_operation_is_ignored():
    """One Super X-Fi mode write draws *two* acknowledges: a failure for a different op, then ours
    with status 0. Raising on the first was a spurious rejection."""
    ours = bytes([Cmd.SUPER_XFI, 0x00, 0x0C])
    theirs = bytes([Cmd.SUPER_XFI, 0x01, 0x99])
    controller = controller_with(replies=[
        [],
        [(Cmd.ACKNOWLEDGE, theirs)],
        [(Cmd.ACKNOWLEDGE, ours)],
    ])
    assert controller.command(Cmd.SUPER_XFI, bytes([0x0C]) + b"PCG ") is True


def test_a_failure_acknowledging_our_own_operation_is_raised():
    controller = controller_with(replies=[
        [],
        [(Cmd.ACKNOWLEDGE, bytes([Cmd.FEATURE_CONTROL, 0x01, 0x00]))],
    ])
    with pytest.raises(DeviceError):
        controller.command(Cmd.FEATURE_CONTROL, bytes([0x00, 0x05, 0x01]))


def test_an_equalizer_write_is_confirmed_by_status_alone():
    """For SetMalcolmParameter byte 0 is an entry count, not an operation. Gating the *success*
    path on the op comparison made every equaliser write time out."""
    controller = controller_with(replies=[
        [],
        [(Cmd.ACKNOWLEDGE, bytes([Cmd.SET_MALCOLM_PARAM, 0x00, 0x63]))],
    ])
    assert controller.command(*__import__(
        "hardware_ui.modules.creative_peripherals.protocol.catalogue",
        fromlist=["catalogue"]).set_eq_band(0, 3.0)) is True


def test_silence_becomes_an_error_rather_than_a_pretence_of_success():
    controller = controller_with()
    with pytest.raises(DeviceError):
        controller.command(Cmd.FEATURE_CONTROL, bytes([0x00, 0x05, 0x01]), timeout_s=0.05)


# --------------------------------------------------------------------------- the device


def info(**kwargs):
    base = dict(uid="usb:SB-1", name="Creative Sound Blaster X4", transport=Transport.USB,
                category=Category.AUDIO, vendor_id=0x041E, product_id=0x3278, serial="SB-1")
    return DeviceInfo(**{**base, **kwargs})


def attached(feature_state=0, feature_support=0, subfeatures=SubFeature.GRAPHIC_EQ):
    """A CreativeDevice with a controller whose state is already populated."""
    device = CreativeDevice(info())
    controller = controller_with()
    state = controller.state
    state.feature_state, state.feature_support = feature_state, feature_support
    state.subfeatures = subfeatures
    state.eq_enabled, state.eq_preamp = True, -3.0
    state.eq_bands = [float(i) for i in range(10)]
    state.output = int(OutputTarget.HEADPHONES)
    state.firmware, state.serial = "1.7.250324.0910", "YDSB1815150001619Q"
    device._controller = controller
    device._describe()
    return device, controller


def test_values_are_served_from_held_state_without_touching_the_wire():
    """The device is push-driven and takes about half a second to commit, so an immediate re-read
    returns the *old* value. Held state is both faster and more accurate."""
    device, controller = attached()
    values = asyncio.run(device.get_many([C.KEY_EQ_PREAMP, C.KEY_FIRMWARE, C.band_key(3)]))
    assert values == {C.KEY_EQ_PREAMP: -3.0, C.KEY_FIRMWARE: "1.7.250324.0910", C.band_key(3): 3.0}
    assert controller._t.written == []


def test_a_band_write_reaches_the_protocol_as_that_band():
    device, controller = attached()
    controller._t = FakeTransport(replies=[
        [], [(Cmd.ACKNOWLEDGE, bytes([Cmd.SET_MALCOLM_PARAM, 0x00, 0x01]))],
    ])
    asyncio.run(device.set(C.band_key(4), 6.5))

    # Decoded against the *request* layout, `[count, module, command] + float32`. Note it differs
    # from the reply layout by one byte -- a response inserts a `more` flag after the count -- so
    # `framing.parse_effect` does not read a request and must not be used here.
    cmd, payload = framing.split(controller._t.written[0])[0]
    count, module, param = payload[:3]
    value = struct.unpack_from("<f", payload, 3)[0]
    assert cmd == Cmd.SET_MALCOLM_PARAM
    assert (count, module) == (1, Module.PLAYBACK)
    assert (param, round(value, 2)) == (15, 6.5)      # bands 0..9 are parameters 11..20


def test_an_unknown_key_is_refused_rather_than_silently_dropped():
    device, _ = attached()
    with pytest.raises(RuntimeError):
        asyncio.run(device.set("nonsense.key", 1))


def test_direct_mode_being_on_advises_why_the_equalizer_is_dead():
    """The `requires` gate already greys the rows out. The advisory carries what greying cannot:
    that the card is doing exactly what it was told."""
    device, _ = attached(feature_state=Feature.DIRECT_MODE.mask)
    advisory = device.advisories().get(C.KEY_EQ_ENABLED)
    assert advisory is not None and "Direct Mode" in advisory.message


def test_direct_mode_being_off_says_nothing():
    device, _ = attached(feature_state=0)
    assert device.advisories() == {}


def test_a_disconnected_device_reports_it_instead_of_raising_attribute_errors():
    device = CreativeDevice(info())
    assert device.connection_label().route == "Not connected"
    with pytest.raises(RuntimeError):
        device._require()


def test_the_connect_notice_warns_before_the_wait_not_after():
    assert "seconds" in CreativeDevice(info()).connect_notice()
    assert CreativeDevice(info()).connect_timeout >= 30


# --------------------------------------------------------------------------- dependencies
#
# Both packages are required rather than optional, and each fails at a different point: `pyusb`
# when the interface is claimed, `cryptography` when the card is unlocked. The second is the
# interesting one -- the responder wraps its ImportError in an UnlockError to keep the source
# project's contract, and calling that a transport failure would tell the user their card is
# broken when a package is simply not installed.


def test_a_missing_crypto_library_names_the_package_not_the_card(monkeypatch):
    from hardware_ui.core.device import DependencyMissing
    from hardware_ui.modules.creative_peripherals import device as device_module
    from hardware_ui.modules.creative_peripherals.protocol import unlock as unlock_module

    def no_crypto(self, challenge, rand_bytes=None):
        raise unlock_module.UnlockError("cryptography missing") from ImportError(
            name="cryptography")

    monkeypatch.setattr(unlock_module.UnlockResponder, "respond", no_crypto)

    transport = T.UsbCdcTransport()
    transport._probe_unlocked = lambda: False
    transport.write_raw = lambda data: None
    transport.read_raw = lambda timeout_ms=300: b"whoareyou" + bytes(36)

    with pytest.raises(ImportError) as caught:
        transport.unlock()
    # And the device layer turns exactly that into an actionable message.
    assert isinstance(device_module._dependency(caught.value), DependencyMissing)
    assert "cryptography" in str(device_module._dependency(caught.value))


def test_a_genuine_unlock_failure_is_still_a_transport_error(monkeypatch):
    """The unwrapping must be narrow: a rejected challenge is a device problem, not a package."""
    from hardware_ui.modules.creative_peripherals.protocol import unlock as unlock_module

    def bad_challenge(self, challenge, rand_bytes=None):
        raise unlock_module.UnlockError("challenge too short")

    monkeypatch.setattr(unlock_module.UnlockResponder, "respond", bad_challenge)

    transport = T.UsbCdcTransport()
    transport._probe_unlocked = lambda: False
    transport.write_raw = lambda data: None
    transport.read_raw = lambda timeout_ms=300: b"whoareyou" + bytes(36)

    with pytest.raises(T.TransportError):
        transport.unlock()


def test_the_module_does_not_claim_a_vendor_import_it_cannot_perform():
    """It has no importer: no `assets.py` here, and the source project's `import_vendor_data.py`
    was not carried across. Declaring `[vendor_assets]` would put "can use vendor data, imported
    from the manufacturer's own files" on the Modules page, describing a capability the code does
    not have. The reading side does work -- a hand-placed presets.json is merged -- so restore the
    block in the same commit that adds an importer.
    """
    from hardware_ui.core.modules import ModuleRegistry

    manifest = ModuleRegistry.discover().get("creative_peripherals")
    assert manifest is not None
    assert manifest.vendor_assets is None, "no importer exists, so nothing should promise one"

    from hardware_ui.modules import creative_peripherals

    module_dir = __import__("pathlib").Path(creative_peripherals.__file__).parent
    assert not (module_dir / "assets.py").exists(), (
        "an assets.py appeared -- restore [vendor_assets] in module.toml alongside it"
    )


def test_the_eleven_built_in_presets_still_ship_and_load():
    """Removing the manifest block must not touch what actually ships: the genre presets are Python
    in catalogue.py, not vendor data."""
    from hardware_ui.modules.creative_peripherals.protocol import catalogue, presets

    assert len(catalogue.EQ_PRESETS) == 11
    assert "Flat" in catalogue.EQ_PRESETS
    assert len(presets.load()) == 11


# --------------------------------------------------------------------------- reported field fixes


def _caps(**kwargs):
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.protocol import ids, presets

    defaults = dict(
        feature_support=0,
        subfeatures=ids.SubFeature(0x40),
        has_equalizer_state=True,
        presets=presets.load(),
        profile_names={},
    )
    return C.CapabilitySet(C.build(**{**defaults, **kwargs})) if hasattr(C, "CapabilitySet") \
        else C.build(**{**defaults, **kwargs})


def test_only_the_two_outputs_the_card_actually_has_are_offered():
    """Reported from hardware: a "Powered Speakers" option that does not exist.

    ``OutputTarget`` carries a third bit and the protocol has no query for which targets a unit
    really has, so a third entry would be this application inventing an output and then routing
    audio into it. The vendor application offers exactly two.
    """
    from hardware_ui.modules.creative_peripherals import capabilities as C

    output = next(c for c in _caps() if c.key == C.KEY_OUTPUT)
    assert [choice.label for choice in output.choices] == ["Headphones", "Speakers (Line Out)"]


def test_direct_mode_gates_the_whole_dsp_and_nothing_else():
    """Reported from hardware: the equaliser greyed out whichever way Direct Mode was set.

    Direct Mode is a DSP *bypass*, so it takes the equaliser, Super X-Fi and headphone
    virtualisation with it -- and must take nothing else, because output routing and headphone gain
    are analogue controls that work either way. The equaliser rows were also gated on the equaliser
    *toggle*, which greyed out the whole tab for anyone whose card had it switched off.
    """
    from hardware_ui.modules.creative_peripherals import capabilities as C

    caps = {c.key: c for c in _caps(feature_support=0x20 | 0x40 | 0x200000)}
    gated = {k for k, c in caps.items() if c.requires == C.KEY_DIRECT_MODE}
    assert C.KEY_SXFI in gated and C.KEY_SXFI_MODE in gated
    assert C.KEY_EQ_ENABLED in gated and C.band_key(0) in gated and C.KEY_EQ_PREAMP in gated
    assert C.feature_key(C.Feature.HP_VIRTUALIZATION) in gated

    assert caps[C.KEY_OUTPUT].requires == "", "routing is not DSP and must stay live"
    assert caps[C.feature_key(C.Feature.HP_HIGH_GAIN)].requires == "", "analogue gain, not DSP"
    assert all(c.requires != C.KEY_EQ_ENABLED for c in caps.values()), (
        "a curve must stay editable while the equaliser is off, or it can never be built"
    )


def test_applying_a_preset_repaints_the_curve_it_wrote():
    """Reported from hardware: the sliders did not move when a preset was chosen.

    A preset writes eleven values in one action. The module's held state was right immediately and
    the page was not, because the shell repaints the control that was written and nothing else.
    """
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.protocol.ids import EQ_BAND_COUNT

    preset = next(c for c in _caps() if c.key == C.KEY_EQ_PRESET)
    assert set(preset.refreshes) == {C.KEY_EQ_PREAMP, *(C.band_key(i) for i in range(
        EQ_BAND_COUNT))}


def test_presets_keep_the_vendors_order_rather_than_being_sorted_by_name():
    """`presets.load()` sorts by the vendor `Order` field, which groups the list the way the
    Windows app presents it. Re-sorting alphabetically here threw that away."""
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.protocol import presets

    preset = next(c for c in _caps() if c.key == C.KEY_EQ_PRESET)
    assert [choice.value for choice in preset.choices] == list(presets.load())


def test_the_equaliser_is_a_mode_list_not_a_checkbox():
    """The question this answers is a real one, asked twice: "why did enabling the equaliser put it
    in Movie mode?"

    Because a stored profile was already live, and a checkbox cannot say so. On this card turning
    the equaliser on and choosing which of its four curves to use are one act -- the button on the
    front cycles off, then each mode in turn -- so one control models it and the modes are named.
    """
    from hardware_ui.core.capability import Kind
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.protocol.ids import PROFILE_NAMES

    names = dict(enumerate(PROFILE_NAMES[0x3278]))
    equaliser = next(c for c in _caps(profile_names=names) if c.key == C.KEY_EQ_ENABLED)
    assert equaliser.kind is Kind.CHOICE
    # Named, not numbered: these are fixed modes, not slots anyone addresses by number.
    # Off or a named mode, and nothing else: there is no bare "On" state on this card.
    assert [choice.label for choice in equaliser.choices] == [
        "Off", "Music", "Movie", "Footstep Enhancer", "EQ for Super X-Fi"]

    # A recall replaces the curve on the card, so the sliders have to be re-read.
    assert C.KEY_EQ_PREAMP in equaliser.refreshes and C.band_key(0) in equaliser.refreshes

    # A model whose modes nobody has read still gets numbers: a confident wrong name is worse.
    numbered = next(c for c in _caps() if c.key == C.KEY_EQ_ENABLED)
    assert [choice.label for choice in numbered.choices][1:] == [
        "Profile 1", "Profile 2", "Profile 3", "Profile 4"]


def test_the_device_photo_path_does_not_raise():
    """It did: `import os` was dropped in the port, so `image_path()` raised NameError and every
    call to `fetch_photo` failed."""
    from hardware_ui.modules.creative_peripherals.protocol import presets

    assert presets.image_path() is None or presets.image_path().is_file()


def test_the_super_xfi_switch_reports_what_it_set():
    """Reported from hardware: ticking it appeared to do nothing and it sprang back to unticked.

    The write reaches the card — the mode selector beside it kept working — but the card answers no
    read for Super X-Fi, so `sxfi_enabled` stays None however often it is switched, and None
    repaints as off. So the module remembers what it set. Unknown until then, which is honest:
    a card that is already in Super X-Fi shows unticked and there is no way to know otherwise.
    """
    import asyncio

    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.controller import DeviceState
    from hardware_ui.modules.creative_peripherals.device import CreativeDevice

    class FakeController:
        state = DeviceState()          # a real one: everything else `_value` reads is on it
        presets: dict = {}
        wrote: list[bool] = []

        def set_sxfi(self, on):
            self.wrote.append(on)

        def settle(self, seconds=0.0):
            """The card reports nothing here, which is the case this test is about."""

    device = CreativeDevice(DeviceInfo(
        uid="usb:1-1", name="Sound Blaster X4", transport=Transport.USB,
        category=Category.AUDIO, vendor_id=0x041E, product_id=0x3278))
    device._controller = FakeController()

    assert device._value(C.KEY_SXFI) is None, "unknown before anything switches it"
    asyncio.run(device.set(C.KEY_SXFI, True))
    assert FakeController.wrote == [True], "the write must still reach the card"
    assert device._value(C.KEY_SXFI) is True, "and the switch must stay where it was put"


def test_the_card_reports_super_xfi_and_the_live_mode_on_different_opcodes():
    """Reported from hardware: Super X-Fi and its mode never showed, whatever was set.

    The card answers a write with a *different* opcode than the one that made it, and both this
    module and the project it was ported from matched the opcode they had sent. Captured from a
    Sound Blaster X4:

        -> 07 1e 00 01 00                       set button 30 (Super X-Fi) on
        <- 08 ff ff 01 00 ...                   op 8, no button id, value at byte 3
        -> 0c 50 43 47 20                       set mode "PCG "
        <- 0d 50 43 47 20 30 30 30 37 2e 31     op 0x0d, the mode, then a version
        <- 02 00                                ACTIVE_MALCOLM_PROFILE: type 2, index 0

    So none of the three is write-only after all, which is what the module used to claim.
    """
    from hardware_ui.modules.creative_peripherals.controller import CreativeController
    from hardware_ui.modules.creative_peripherals.protocol.ids import Cmd

    controller = CreativeController()
    controller.apply([
        (int(Cmd.HARDWARE_BUTTON), bytes.fromhex("08ffff0100000000")),
        (int(Cmd.SUPER_XFI), bytes.fromhex("0d") + b"PCG " + b"0007.1"),
        (int(Cmd.ACTIVE_MALCOLM_PROFILE), bytes.fromhex("0201")),
    ])
    assert controller.state.sxfi_enabled is True
    assert controller.state.sxfi_mode == "PCG "
    assert controller.state.profile == 1

    # Off is reported the same way, and must not be mistaken for "nothing said".
    controller.apply([(int(Cmd.HARDWARE_BUTTON), bytes.fromhex("08ffff0000000000"))])
    assert controller.state.sxfi_enabled is False


def test_a_mode_the_card_refuses_is_explained_rather_than_dumped():
    """What the user saw: "command 26 rejected, status=128 (1a 80 00 00 00 00 00 00 00 00)".

    Selecting the Super X-Fi curve with Super X-Fi off is refused by the card itself, which is the
    right place for the rule — the device is the authority, and two attempts to enforce it in the
    page got it wrong. What was wrong was the reporting.
    """
    import asyncio

    import pytest

    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.controller import DeviceError, DeviceState
    from hardware_ui.modules.creative_peripherals.device import CreativeDevice

    class FakeController:
        state = DeviceState(sxfi_enabled=False)
        presets: dict = {}
        eq_calls: list = []

        def select_profile(self, index):
            raise DeviceError("command 26 rejected, status=128 (1a 80 00 00 00 00 00 00 00 00)")

        def set_eq_enabled(self, on):
            self.eq_calls.append(on)

        def settle(self, seconds=0.0):
            pass

        def drain(self):
            return []

    device = CreativeDevice(DeviceInfo(
        uid="usb:1-1", name="Sound Blaster X4", transport=Transport.USB,
        category=Category.AUDIO, vendor_id=0x041E, product_id=0x3278))
    device._controller = FakeController()

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(device.set(C.KEY_EQ_ENABLED, 3))

    message = str(caught.value)
    assert "Super X-Fi" in message and "EQ for Super X-Fi" in message
    assert "status=128" not in message, "the protocol dump must not reach the user"
    assert FakeController.eq_calls == [], (
        "a refused mode must not leave the equaliser switched on into some other mode"
    )


def test_the_equaliser_modes_are_split_between_the_two_super_xfi_states():
    """Measured on a Sound Blaster X4, and symmetric — which took two rounds to see.

    The card keeps two sets of equaliser modes, one for each Super X-Fi state, and refuses the
    other set with ``status=128`` on command 26:

        Super X-Fi on   accepts EQ for Super X-Fi, refuses Music / Movie / Footstep Enhancer
        Super X-Fi off  accepts those three,       refuses EQ for Super X-Fi

    Reported as an advisory rather than enforced by hiding entries: the rule is exact and the
    values behind it are readable, so a gate is possible, but changing which choices exist means
    rebuilding the capability set, and that repaints every tab under the user's hands.
    """
    from hardware_ui.core.device import Category, DeviceInfo, Transport
    from hardware_ui.modules.creative_peripherals import capabilities as C
    from hardware_ui.modules.creative_peripherals.controller import DeviceState
    from hardware_ui.modules.creative_peripherals.device import CreativeDevice

    device = CreativeDevice(DeviceInfo(
        uid="usb:1-1", name="Sound Blaster X4", transport=Transport.USB,
        category=Category.AUDIO, vendor_id=0x041E, product_id=0x3278))

    class FakeController:
        presets: dict = {}

        def __init__(self, sxfi):
            self.state = DeviceState(sxfi_enabled=sxfi)

    device._controller = FakeController(True)
    on = device._modes_available()
    assert "EQ for Super X-Fi" in on and "Music" not in on

    device._controller = FakeController(False)
    off = device._modes_available()
    assert "Music" in off and "Footstep Enhancer" in off and "EQ for Super X-Fi" not in off

    device._controller = FakeController(None)
    assert device._modes_available() == "", "nothing claimed while Super X-Fi is unknown"

    # And the advisory does not fight the Direct Mode one, which is about the whole DSP.
    assert C.KEY_EQ_ENABLED  # the key both hang off


def test_routing_is_confirmed_by_reading_it_back_not_by_assuming():
    """Reported from hardware: choosing Speakers put the box back on Headphones, while the card
    really had switched.

    The card is reliable and slow, not flaky. Left alone for four seconds it reported the requested
    output three times out of three in both directions; what varies is *when* it commits — 200 ms
    on one attempt, 1.4 s on the next. Every "it reverted" was this application reading inside that
    window, and one wrong fix made it worse by confirming *before* draining, so a queued push
    describing the previous state put the old value back afterwards.
    """
    from hardware_ui.modules.creative_peripherals import controller as ctl
    from hardware_ui.modules.creative_peripherals.controller import CreativeController
    from hardware_ui.modules.creative_peripherals.protocol.ids import Cmd, OutputTarget

    controller = CreativeController()
    answers = iter([0x04, 0x04, 0x01])          # slow to commit, then settled

    def fake_request(cmd, payload=b"", timeout_s=1.0):
        controller.state.output = next(answers, 0x01)
        return b""

    controller.request = fake_request
    controller.settle = lambda seconds=0.0: None

    assert controller.confirm_output(int(OutputTarget.LINE_OUT)) is True
    assert controller.state.output == int(OutputTarget.LINE_OUT)

    # A card that never agrees is reported, not retried for ever, and is not an error: the page
    # shows what the card last said, which is honest either way.
    controller.request = lambda cmd, payload=b"", timeout_s=1.0: b""
    controller.state.output = int(OutputTarget.HEADPHONES)
    assert controller.confirm_output(int(OutputTarget.LINE_OUT), timeout_s=0.3) is False
    assert controller.state.output == int(OutputTarget.HEADPHONES)
    assert ctl.CONFIRM_S >= 2.0, "the observed worst case is 1.4s; leave headroom"
    assert Cmd.SPEAKER_OUTPUT_TARGET  # the command this all hangs off


def test_the_x4_claims_as_verified_and_other_creative_cards_do_not():
    """The Sound Blaster X4 is the one model opened, read and written through this application.

    A narrow verified rule sits ahead of the vendor-wide family rules, and the ordering is the whole
    mechanism — matched only by a family rule, a device is labelled an untested model in the
    sidebar, which is right for every Creative card except this one. A rule added in the wrong place
    would silently claim every card as verified, which is the kind of wrong that never fails a test
    unless the test is this one.
    """
    from hardware_ui.core.device import Category, DeviceInfo, Support, Transport
    from hardware_ui.core.modules import ModuleRegistry

    registry = ModuleRegistry.discover()

    def claim(product_id: int, transport: Transport) -> DeviceInfo:
        return registry.claim(DeviceInfo(
            uid=f"{transport.value}:{product_id:04x}", name="Creative", transport=transport,
            category=Category.OTHER, vendor_id=0x041E, product_id=product_id))

    # **Both transports**, and that is the point rather than thoroughness for its own sake. An X4
    # exposes a HID interface as well as its control channel, and one physical device gives one row
    # -- the hidraw one, which discovery prefers because it carries an openable node, a kind and an
    # icon. Marking only the USB rule verified left the card reading "untested model", because the
    # row being labelled was never the USB one.
    for transport in (Transport.USB, Transport.HID):
        x4 = claim(0x3278, transport)
        assert x4.module_id == "creative_peripherals"
        assert x4.support is Support.VERIFIED, f"an X4 is tested, on {transport.value} too"

        # An X3: same module, same protocol in all likelihood, and nobody has held one.
        other = claim(0x3264, transport)
        assert other.module_id == "creative_peripherals"
        assert other.support is Support.FAMILY, f"an X3 is not tested, on {transport.value} either"
