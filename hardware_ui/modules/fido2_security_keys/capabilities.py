"""A CTAP authenticator, expressed in the shell's schema.

Two tabs, as designed: everything the key reports on **Information**, and everything that changes
it on **Configuration**.

Nothing here is vendor-specific and nothing should become so. The whole point of building on CTAP
is that one module serves YubiKey, Nitrokey, OnlyKey, SoloKey, Token2 and anything else compliant;
a vendor's own extras belong in a module that ``extends`` this one, so the key still appears once.

Every control is gated on what the key actually advertises. That is not decoration: a YubiKey 5
reports ``credentialMgmtPreview`` but not ``credMgmt``, and no ``authnrCfg`` at all, so its
credential management works by a different command and the CTAP 2.1 policy controls cannot apply
to it whatsoever.
"""

from __future__ import annotations

from collections.abc import Sequence

from hardware_ui.core import Capability, CapabilitySet, Kind

GROUP_INFO = "Information"
GROUP_CONFIG = "Configuration"

INFO_PREFIX = "info."
PIN_KEY = "config.pin"
NEW_PIN_KEY = "config.new_pin"
SET_PIN_KEY = "action.set_pin"
MIN_PIN_KEY = "config.min_pin_length"
ALWAYS_UV_KEY = "config.always_uv"
FORCE_PIN_CHANGE_KEY = "action.force_pin_change"
ENTERPRISE_ATTESTATION_KEY = "action.enterprise_attestation"
TEST_KEY = "action.test"
RESET_KEY = "action.reset"
REFRESH_KEY = "action.refresh"

#: Readable names for the information rows. The key is the identifier; the label is what a person
#: reads, and conflating the two put each row's *value* in its label column.
INFO_LABELS: dict[str, str] = {
    "model": "Model",
    "usb_id": "USB id",
    "node": "Device node",
    "aaguid": "AAGUID",
    "protocol": "Protocol",
    "versions": "CTAP versions",
    "extensions": "Extensions",
    "transports": "Transports",
    "algorithms": "Algorithms",
    "pin_protocols": "PIN protocols",
    "max_msg_size": "Maximum message size",
    "max_credentials_in_list": "Credentials per request",
    "max_credential_id_length": "Credential id length",
    "minimum_pin_length": "Minimum PIN length",
    "pin_state": "PIN",
    "pin_retries": "PIN attempts remaining",
}


def info_label(key: str) -> str:
    return INFO_LABELS.get(key, key.replace("_", " ").capitalize())


#: CTAP option names, in the order they are worth reading, with what they mean to a user. The
#: option list is the authoritative statement of what a key can do.
OPTION_LABELS: dict[str, str] = {
    "clientPin": "PIN set",
    "rk": "Discoverable credentials (resident keys)",
    "up": "User presence (touch)",
    "uv": "Built-in user verification",
    "credMgmt": "Credential management",
    "credentialMgmtPreview": "Credential management (preview)",
    "authnrCfg": "Authenticator configuration",
    "bioEnroll": "Fingerprint enrolment",
    "largeBlobs": "Large blobs",
    "pinUvAuthToken": "PIN/UV auth token",
    "makeCredUvNotRqd": "Credentials without user verification",
    "alwaysUv": "Always require user verification",
    "ep": "Enterprise attestation",
    "plat": "Platform (built into this computer)",
    "setMinPINLength": "Minimum PIN length can be set",
}

NOTE_DESTRUCTIVE = (
    "These change the key itself. A reset erases every credential and the PIN, and cannot be "
    "undone — a key whose PIN is lost can only be recovered by resetting it, which wipes it. "
    "Keep a second enrolled key for anything that matters."
)
NOTE_PIN = (
    "The PIN is asked for when it is needed and kept only for that one operation — never stored, "
    "never shown, and never left sitting in this window."
)
NOTE_NO_CONFIG = (
    "This key does not support CTAP 2.1 authenticator configuration, so minimum PIN length, "
    "always-require-verification and enterprise attestation cannot be changed on it. That is a "
    "property of the key, not a limitation here."
)
CONFIRM_RESET = (
    "Every credential and the PIN are erased. Accounts that rely on this key alone will become "
    "unreachable. Most keys only accept a reset within a few seconds of being plugged in, and it "
    "needs a touch."
)
CONFIRM_ALWAYS_UV = (
    "The key will require user verification for every operation, including from software that "
    "does not expect it."
)
CONFIRM_FORCE_PIN = "You will be required to set a new PIN before the key can be used again."
CONFIRM_ENTERPRISE = (
    "Enterprise attestation lets a relying party identify this individual key. It cannot be "
    "switched off again on most keys."
)

RESET_TIMEOUT = 60.0
"""A reset waits for a touch, and keys give a generous window for it."""

TOUCH_TIMEOUT = 60.0
"""Anything that needs a touch: the user has to reach the key and press it."""


def option_rows(options: dict[str, bool]) -> list[tuple[str, str]]:
    """``(key, label)`` for each advertised option, known ones first and in a sensible order."""
    known = [(k, OPTION_LABELS[k]) for k in OPTION_LABELS if k in options]
    unknown = [(k, k) for k in sorted(options) if k not in OPTION_LABELS]
    return known + unknown


def build(
    *,
    identity: Sequence[str] = (),
    capabilities: Sequence[str] = (),
    options: Sequence[tuple[str, str]] = (),
    can_set_pin: bool = False,
    has_pin: bool = False,
    can_configure: bool = False,
    can_set_min_pin: bool = False,
    can_enterprise: bool = False,
    min_pin_length: int = 4,
) -> CapabilitySet:
    """The page for one authenticator.

    Every ``can_*`` argument comes from the key's own option list, so a control that the hardware
    cannot honour is absent rather than present and failing.
    """
    out: list[Capability] = []
    out += _information(identity, capabilities, options)
    out += _configuration(
        can_set_pin=can_set_pin,
        has_pin=has_pin,
        can_configure=can_configure,
        can_set_min_pin=can_set_min_pin,
        can_enterprise=can_enterprise,
        min_pin_length=min_pin_length,
    )
    return CapabilitySet(out)


def _information(
    identity: Sequence[str],
    capabilities: Sequence[str],
    options: Sequence[tuple[str, str]],
) -> list[Capability]:
    out: list[Capability] = [
        Capability(
            key=f"{INFO_PREFIX}{key}", kind=Kind.READOUT, label=info_label(key),
            group=GROUP_INFO, section="Identity", writable=False,
        )
        for key in identity
    ]
    out += [
        Capability(
            key=f"{INFO_PREFIX}{key}", kind=Kind.READOUT, label=info_label(key),
            group=GROUP_INFO, section="Capabilities", writable=False,
        )
        for key in capabilities
    ]
    out += [
        Capability(
            key=f"{INFO_PREFIX}option.{key}", kind=Kind.READOUT, label=label, group=GROUP_INFO,
            section="Reported by the key", writable=False,
            description=f"CTAP option “{key}”.",
        )
        for key, label in options
    ]
    out.append(
        Capability(
            key=REFRESH_KEY, kind=Kind.ACTION, label="Details",
            action_label="Re-read from key", group=GROUP_INFO, section="Actions",
        )
    )
    return out


def _configuration(
    *,
    can_set_pin: bool,
    has_pin: bool,
    can_configure: bool,
    can_set_min_pin: bool,
    can_enterprise: bool,
    min_pin_length: int,
) -> list[Capability]:
    out: list[Capability] = []
    if not can_set_pin:
        return out

    out.append(
        Capability(
            key=SET_PIN_KEY, kind=Kind.ACTION,
            label="Change PIN" if has_pin else "Set PIN",
            action_label="Change PIN…" if has_pin else "Set PIN…",
            group=GROUP_CONFIG, section="PIN", timeout=TOUCH_TIMEOUT,
            prompt="pin_change" if has_pin else "pin_set",
            minimum=min_pin_length,
            prompt_detail=(
                f"The new PIN must be at least {min_pin_length} characters. "
                "It cannot be recovered if you forget it — a key whose PIN is lost can only be "
                "used again after a factory reset, which erases every credential on it."
            ),
            note=NOTE_PIN,
        )
    )

    if can_set_min_pin:
        out.append(
            Capability(
                key=MIN_PIN_KEY,
                prompt="pin", prompt_detail=(
                    "Raising the minimum PIN length needs the key's PIN."
                ),
                kind=Kind.RANGE, label="Minimum PIN length",
                group=GROUP_CONFIG, section="Policy",
                minimum=4, maximum=63, step=1, confirm=True, timeout=TOUCH_TIMEOUT,
                confirm_detail=(
                    "The minimum can only ever be raised, never lowered, and a PIN shorter than "
                    "the new minimum must be changed before the key works again."
                ),
            )
        )
    if can_configure:
        out.append(
            Capability(
                key=ALWAYS_UV_KEY,
                prompt="pin", prompt_detail=(
                    "Changing this needs the key's PIN."
                ),
                kind=Kind.TOGGLE, label="Always require verification",
                group=GROUP_CONFIG, section="Policy",
                confirm=True, confirm_detail=CONFIRM_ALWAYS_UV, timeout=TOUCH_TIMEOUT,
            )
        )
        out.append(
            Capability(
                key=FORCE_PIN_CHANGE_KEY,
                prompt="pin", prompt_detail=(
                    "Requiring a new PIN needs the key's current PIN."
                ),
                kind=Kind.ACTION, label="PIN change",
                action_label="Require a new PIN", group=GROUP_CONFIG, section="Policy",
                confirm=True, confirm_detail=CONFIRM_FORCE_PIN, timeout=TOUCH_TIMEOUT,
            )
        )
    if can_enterprise:
        out.append(
            Capability(
                key=ENTERPRISE_ATTESTATION_KEY,
                prompt="pin", prompt_detail=(
                    "Enabling enterprise attestation needs the key's PIN."
                ),
                kind=Kind.ACTION, label="Enterprise attestation",
                action_label="Enable", group=GROUP_CONFIG, section="Policy",
                confirm=True, confirm_detail=CONFIRM_ENTERPRISE, timeout=TOUCH_TIMEOUT,
            )
        )
    if not (can_configure or can_set_min_pin or can_enterprise):
        out.append(
            Capability(
                key="config.no_policy", kind=Kind.READOUT, label="Policy",
                group=GROUP_CONFIG, section="Policy", writable=False, note=NOTE_NO_CONFIG,
            )
        )

    out.append(
        Capability(
            key=TEST_KEY, kind=Kind.ACTION, label="Test this key",
            action_label="Test this key…" if has_pin else "Test this key",
            group=GROUP_CONFIG, section="Maintenance", timeout=TOUCH_TIMEOUT,
            prompt="pin" if has_pin else "",
            prompt_detail=(
                "A throwaway sign-in that confirms the touch and the PIN work. Nothing is stored "
                "on the key. You will be asked to touch it."
            ),
            description=(
                "A throwaway sign-in that confirms the touch and the PIN work. Nothing is stored "
                "on the key."
            ),
        )
    )
    out.append(
        Capability(
            key=RESET_KEY, kind=Kind.ACTION, label="Factory reset",
            action_label="Erase everything on this key", group=GROUP_CONFIG,
            section="Maintenance", confirm=True, confirm_detail=CONFIRM_RESET,
            timeout=RESET_TIMEOUT, note=NOTE_DESTRUCTIVE,
        )
    )
    return out


__all__ = ["build", "build_passkeys", "option_rows", "passkeys_locked"]


# --------------------------------------------------------------------------- passkeys

GROUP_PASSKEYS = "Passkeys"

PASSKEY_PREFIX = "cred.item."
PASSKEY_DELETE_PREFIX = "cred.delete."
PASSKEY_RENAME_PREFIX = "cred.rename."
PASSKEYS_SHOW_KEY = "cred.show"
PASSKEYS_STATUS_KEY = "cred.status"

NOTE_PASSKEYS = (
    "The accounts stored on this key — passkeys, also called discoverable credentials or resident "
    "keys. These are what let a website sign you in with the key alone, without a username.\n\n"
    "Reading them needs the key's PIN, so it happens when you ask rather than every time the key "
    "is opened."
)
NOTE_NO_RENAME = (
    "This key's firmware offers only the older credential command, which can list and delete but "
    "not rename. That is a property of the key, not a limitation here."
)
CONFIRM_DELETE_PASSKEY = (
    "The site will no longer accept this key for that account. It cannot be undone, and unless "
    "you have another way in — a second key, a recovery code, a password — you may lose access."
)


def passkey_key(credential_id: str) -> str:
    return f"{PASSKEY_PREFIX}{credential_id}"


def passkey_delete_key(credential_id: str) -> str:
    return f"{PASSKEY_DELETE_PREFIX}{credential_id}"


def passkey_rename_key(credential_id: str) -> str:
    return f"{PASSKEY_RENAME_PREFIX}{credential_id}"


def passkeys_locked() -> list[Capability]:
    """Before the PIN has been given: one button, and why it is a button."""
    return [
        Capability(
            key=PASSKEYS_SHOW_KEY, kind=Kind.ACTION, label="Passkeys",
            action_label="Show passkeys…", group=GROUP_PASSKEYS, section="Passkeys",
            note=NOTE_PASSKEYS, prompt="pin", prompt_label="PIN",
            prompt_detail="Listing the accounts stored on this key needs its PIN.",
            timeout=30.0,
        )
    ]


def build_passkeys(
    items: Sequence[tuple[str, str]], *, used: int, capacity: int, can_rename: bool
) -> list[Capability]:
    """*items* are ``(credential id, label)``, already sorted."""
    out: list[Capability] = [
        Capability(
            key=PASSKEYS_STATUS_KEY, kind=Kind.READOUT, label="Stored",
            group=GROUP_PASSKEYS, section="Passkeys", writable=False,
            note=NOTE_PASSKEYS if not items else "",
            description=(
                f"{used} of {capacity} used." if capacity else f"{used} stored."
            ),
        )
    ]
    out += [
        Capability(
            key=passkey_key(cid), kind=Kind.READOUT, label=label,
            group=GROUP_PASSKEYS, section="Passkeys", writable=False, copyable=True,
        )
        for cid, label in items
    ]
    if can_rename:
        out += [
            Capability(
                key=passkey_rename_key(cid), kind=Kind.ACTION, label=label,
                action_label="Rename…", group=GROUP_PASSKEYS, section="Rename an account",
                prompt="secret", prompt_label="Account name",
                prompt_detail="What this account is called on the key.",
            )
            for cid, label in items
        ]
    elif items:
        out.append(
            Capability(
                key="cred.no_rename", kind=Kind.READOUT, label="Renaming",
                group=GROUP_PASSKEYS, section="Rename an account", writable=False,
                note=NOTE_NO_RENAME,
            )
        )
    out += [
        Capability(
            key=passkey_delete_key(cid), kind=Kind.ACTION, label=label,
            action_label="Delete", group=GROUP_PASSKEYS, section="Remove an account",
            confirm=True, confirm_detail=CONFIRM_DELETE_PASSKEY,
            prompt="pin", prompt_label="PIN",
            prompt_detail=f"Deleting “{label}” needs the key's PIN.",
        )
        for cid, label in items
    ]
    out.append(
        Capability(
            key=PASSKEYS_SHOW_KEY, kind=Kind.ACTION, label="List",
            action_label="Re-read from key", group=GROUP_PASSKEYS, section="Manage",
            prompt="pin", prompt_label="PIN",
            prompt_detail="Re-reading the accounts needs the key's PIN.",
            timeout=30.0,
        )
    )
    return out
