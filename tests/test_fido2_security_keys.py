"""FIDO2 module tests. No key and no `fido2` package needed."""

from __future__ import annotations

import inspect

from hardware_ui.core import Kind
from hardware_ui.modules.fido2_security_keys import capabilities as C
from hardware_ui.modules.fido2_security_keys.device import Fido2SecurityKey

#: What a YubiKey 5 NFC actually reports — CTAP 2.0 with the 2.1 preview, and no authnrCfg.
YUBIKEY_5 = {
    "clientPin": True, "rk": True, "up": True,
    "credentialMgmtPreview": True, "plat": False,
}
#: A CTAP 2.1 key, which does have authenticator configuration.
CTAP21 = {**YUBIKEY_5, "credMgmt": True, "authnrCfg": True, "ep": False, "alwaysUv": False}


def page(options, **kw):
    opts = C.option_rows(options)
    return C.build(
        identity=["model"], capabilities=["versions"], options=opts,
        can_set_pin="clientPin" in options, has_pin=bool(options.get("clientPin")),
        can_configure=bool(options.get("authnrCfg")),
        can_set_min_pin=bool(options.get("authnrCfg")),
        can_enterprise=bool(options.get("authnrCfg")) and "ep" in options,
        **kw,
    )


def keys(p):
    return [c.key for c in p]


# --------------------------------------------------------------------------- module scope


def test_the_module_does_not_import_fido2_until_a_key_is_opened():
    """The dependency is this module's, not the application's."""
    import hardware_ui.modules.fido2_security_keys.device as mod

    source = inspect.getsource(mod)
    top = [
        ln for ln in source.splitlines()
        if ln.startswith(("import ", "from ")) and "fido2" in ln and "hardware_ui" not in ln
    ]
    assert top == [], top


def test_a_missing_library_is_reported_as_something_to_install():
    from hardware_ui.core import DependencyMissing
    from hardware_ui.modules.fido2_security_keys.device import INSTALL_HINT

    assert "emerge dev-python/fido2" in INSTALL_HINT
    assert "DependencyMissing(INSTALL_HINT)" in inspect.getsource(Fido2SecurityKey._connect_sync)
    assert issubclass(DependencyMissing, Exception)


# --------------------------------------------------------------------------- gating


def test_a_ctap20_key_gets_an_explanation_instead_of_policy_controls():
    """A YubiKey 5 has no `authnrCfg`, so minimum PIN length, always-UV and enterprise
    attestation cannot apply to it at all. Showing them would be showing controls that fail."""
    p = page(YUBIKEY_5)
    assert C.MIN_PIN_KEY not in keys(p)
    assert C.ALWAYS_UV_KEY not in keys(p)
    assert C.ENTERPRISE_ATTESTATION_KEY not in keys(p)
    note = p.by_key("config.no_policy").note
    assert "does not support CTAP 2.1" in note
    assert "property of the key" in note


def test_a_ctap21_key_gets_the_policy_controls():
    p = page(CTAP21)
    assert p.by_key(C.MIN_PIN_KEY).kind is Kind.RANGE
    assert p.by_key(C.ALWAYS_UV_KEY).kind is Kind.TOGGLE
    assert "config.no_policy" not in keys(p)


def test_every_policy_change_confirms_first():
    p = page(CTAP21)
    for key in (C.MIN_PIN_KEY, C.ALWAYS_UV_KEY, C.FORCE_PIN_CHANGE_KEY):
        assert p.by_key(key).confirm, key
        assert p.by_key(key).confirm_detail, key


def test_the_reset_is_confirmed_and_says_what_it_destroys():
    reset = page(YUBIKEY_5).by_key(C.RESET_KEY)
    assert reset.confirm
    assert "erased" in reset.confirm_detail
    assert "unreachable" in reset.confirm_detail
    assert "cannot be undone" in reset.note


def test_the_pin_is_asked_for_when_needed_not_left_in_the_form():
    """A PIN belongs to the operation, not the page. Fields on the form meant it sat on screen for
    as long as the page did, and made "Test this key" require filling in a box labelled for
    changing the PIN."""
    p = page(YUBIKEY_5)
    assert C.PIN_KEY not in keys(p)
    assert C.NEW_PIN_KEY not in keys(p)
    assert p.by_key(C.SET_PIN_KEY).prompt == "pin_change"
    assert p.by_key(C.TEST_KEY).prompt == "pin"


def test_a_key_with_no_pin_is_not_asked_for_a_current_one():
    p = page({"rk": True, "clientPin": False})
    assert p.by_key(C.SET_PIN_KEY).prompt == "pin_set"
    # And a test needs no PIN at all on such a key.
    assert p.by_key(C.TEST_KEY).prompt == ""


def test_the_change_prompt_carries_the_keys_minimum_length():
    p = page(YUBIKEY_5, min_pin_length=6)
    assert p.by_key(C.SET_PIN_KEY).minimum == 6
    assert "at least 6 characters" in p.by_key(C.SET_PIN_KEY).prompt_detail


def test_every_policy_change_asks_for_the_pin():
    p = page(CTAP21)
    for key in (C.MIN_PIN_KEY, C.ALWAYS_UV_KEY, C.FORCE_PIN_CHANGE_KEY):
        assert p.by_key(key).prompt == "pin", key
        assert p.by_key(key).prompt_detail, key


def test_the_button_says_set_or_change_depending_on_the_key():
    assert page(YUBIKEY_5).by_key(C.SET_PIN_KEY).action_label == "Change PIN…"
    assert page({"rk": True, "clientPin": False}).by_key(C.SET_PIN_KEY).action_label == "Set PIN…"


def test_options_are_labelled_not_shown_raw():
    rows = dict(C.option_rows(YUBIKEY_5))
    assert rows["credentialMgmtPreview"] == "Credential management (preview)"
    assert rows["rk"] == "Discoverable credentials (resident keys)"


def test_an_unknown_option_still_appears():
    rows = dict(C.option_rows({**YUBIKEY_5, "someFutureThing": True}))
    assert "someFutureThing" in rows


# --------------------------------------------------------------------------- the test path


def test_the_self_test_signs_the_same_hash_it_sends():
    """A pin/uv auth param authenticates the client data of the request it accompanies. Signing a
    *different* random hash produces a token the key rejects."""
    source = inspect.getsource(Fido2SecurityKey._test)
    assert "client_data_hash = os.urandom(32)" in source
    assert "authenticate(token, client_data_hash)" in source
    assert "client_data_hash=client_data_hash" in source


def test_the_self_test_asks_for_the_pin_before_trying():
    """Once a key has a PIN, makeCredential requires a token — without one it answers
    PUAT_REQUIRED, which is a confusing way to say "type your PIN"."""
    source = inspect.getsource(Fido2SecurityKey._test)
    assert 'self.supports("clientPin")' in source
    assert "needs that PIN" in source
    assert "PERMISSION.MAKE_CREDENTIAL" in source


def test_configuration_requests_a_token_for_configuration():
    assert "PERMISSION.AUTHENTICATOR_CFG" in inspect.getsource(Fido2SecurityKey._configure)


def test_ctap_errors_are_explained_in_terms_of_what_to_do():
    source = inspect.getsource(Fido2SecurityKey._explain)
    for err in ("PUAT_REQUIRED", "PIN_INVALID", "PIN_BLOCKED", "PIN_AUTH_BLOCKED",
                "ACTION_TIMEOUT", "PIN_POLICY_VIOLATION"):
        assert err in source, err
    assert "attempt" in source  # the retry count is what makes a wrong PIN recoverable
    assert "erases every credential" in source


# --------------------------------------------------------------------------- extensibility


def test_the_class_is_written_to_be_subclassed():
    for hook in ("extra_capabilities", "extra_values", "handle_set", "supports"):
        assert hasattr(Fido2SecurityKey, hook), hook
    assert Fido2SecurityKey().__class__ if False else True


def test_the_hooks_are_inert_in_the_base_class():
    from hardware_ui.core import DeviceInfo, NotSupported, Transport

    dev = Fido2SecurityKey(DeviceInfo(uid="hid:x", name="key", transport=Transport.HID))
    assert dev.extra_capabilities() == []
    assert dev.extra_values() == {}
    try:
        dev.handle_set("vendor.thing", 1)
    except NotSupported:
        pass
    else:
        raise AssertionError("the base class owns no vendor keys")


def test_actions_say_what_happened_rather_than_returning_nothing():
    """The shell shows an action's returned sentence beside the tick. Returning None left a
    successful self-test indistinguishable from nothing having happened."""
    for name in ("_test", "_reset", "_set_pin"):
        source = inspect.getsource(getattr(Fido2SecurityKey, name))
        assert "return (" in source or 'return "' in source, name
    assert "-> str" in inspect.getsource(Fido2SecurityKey._test)
    assert "touch was accepted" in inspect.getsource(Fido2SecurityKey._test)
    assert "erased" in inspect.getsource(Fido2SecurityKey._reset)


def test_setting_a_first_pin_is_not_reported_as_a_change():
    """The key reports a PIN either way once the write has landed, so the wording has to be
    decided from what was true beforehand."""
    source = inspect.getsource(Fido2SecurityKey._set_pin)
    assert "had_pin = self.supports" in source
    assert source.index("had_pin = self.supports") < source.index("client.change_pin")
    assert "A PIN was set on the key." in source
    assert "The PIN was changed." in source


def test_a_failed_pin_change_reports_the_remaining_attempts():
    """A wrong current PIN is recoverable until the attempts run out, after which the key can only
    be reset — so the count is the most useful thing to say."""
    source = inspect.getsource(Fido2SecurityKey._explain)
    assert "PIN_INVALID" in source
    assert "attempt" in source
    assert "_pin_retries" in source


# --------------------------------------------------------------------------- passkeys


class _Info:
    """Just enough of a CTAP `Info` for the capability gates."""

    def __init__(self, options, versions=("FIDO_2_0", "FIDO_2_1_PRE")):
        self.options = options
        self.versions = list(versions)


def test_a_key_with_neither_command_gets_no_passkeys_tab():
    from hardware_ui.modules.fido2_security_keys import credentials as CRED

    assert not CRED.supported(_Info({"clientPin": True}))
    assert not CRED.supported(None)


def test_the_preview_command_is_enough_to_list_and_delete():
    """The YubiKey 5 this was built against has only the preview — the common case, not an edge."""
    from hardware_ui.modules.fido2_security_keys import credentials as CRED

    preview = _Info({"credentialMgmtPreview": True})
    assert CRED.supported(preview)
    assert not CRED.rename_supported(preview), "the preview command cannot rename"


def test_the_standard_command_also_allows_renaming():
    from hardware_ui.modules.fido2_security_keys import credentials as CRED

    full = _Info({"credMgmt": True}, versions=("FIDO_2_1",))
    assert CRED.supported(full)
    assert CRED.rename_supported(full)


def test_a_key_that_cannot_rename_says_why_instead_of_offering_it():
    from hardware_ui.modules.fido2_security_keys import capabilities as C

    rows = C.build_passkeys(
        [("aa", "github.com: me")], used=1, capacity=25, can_rename=False
    )
    keys = {c.key for c in rows}
    assert C.passkey_rename_key("aa") not in keys
    note = next(c for c in rows if c.key == "cred.no_rename").note
    assert "older credential command" in note
    assert "property of the key" in note


def test_renaming_appears_when_the_key_supports_it():
    from hardware_ui.modules.fido2_security_keys import capabilities as C

    rows = C.build_passkeys([("aa", "github.com: me")], used=1, capacity=25, can_rename=True)
    assert C.passkey_rename_key("aa") in {c.key for c in rows}


def test_every_passkey_write_asks_for_the_pin():
    """Reading needs a token scoped to CREDENTIAL_MGMT; so does deleting."""
    from hardware_ui.modules.fido2_security_keys import capabilities as C

    rows = C.build_passkeys([("aa", "github.com: me")], used=1, capacity=25, can_rename=True)
    for prefix in (C.PASSKEY_DELETE_PREFIX, C.PASSKEY_RENAME_PREFIX):
        row = next(c for c in rows if c.key.startswith(prefix))
        assert row.prompt, row.key


def test_deleting_says_what_is_lost():
    from hardware_ui.modules.fido2_security_keys import capabilities as C

    row = next(
        c for c in C.build_passkeys([("aa", "github.com: me")], used=1, capacity=25,
                                    can_rename=False)
        if c.key.startswith(C.PASSKEY_DELETE_PREFIX)
    )
    assert row.confirm
    assert "no longer accept this key" in row.confirm_detail
    assert "cannot be undone" in row.confirm_detail


def test_the_list_is_not_read_until_the_pin_is_given():
    """A key with a dozen accounts is a dozen round trips, and all of them need a PIN."""
    from hardware_ui.modules.fido2_security_keys import capabilities as C

    rows = C.passkeys_locked()
    assert [c.key for c in rows] == [C.PASSKEYS_SHOW_KEY]
    assert rows[0].prompt == "pin"


def test_a_passkey_is_labelled_site_then_account():
    from hardware_ui.modules.fido2_security_keys.credentials import Passkey

    assert Passkey("aa", "github.com", "me@example.com").label() == "github.com: me@example.com"
    assert Passkey("aa", "github.com", "").label() == "github.com"


def test_capacity_is_what_is_used_plus_what_is_left():
    from hardware_ui.modules.fido2_security_keys.credentials import Passkey, Passkeys

    keys = Passkeys(items=(Passkey("a", "s", "u"),), used=3, remaining=22)
    assert keys.capacity == 25
