"""Discoverable credentials — passkeys — stored on the key itself.

The largest piece of ``kcm-fido-keys`` that had not been ported. Pure CTAP, so it belongs here in
the vendor-neutral module and a Nitrokey or a SoloKey gets it on the same terms as a YubiKey.

**A passkey is not a setting.** Everything else on this page describes what a key *can* do; these
are the accounts living on it, and deleting one is how you lose access to a website. Nothing here
writes without saying which site is affected.

**Two commands, one interface.** CTAP 2.1 defines ``credMgmt``; the 2.1 preview defines
``credentialMgmtPreview``, which does less. ``python-fido2`` accepts either for reading and
deleting, and gates renaming on the standard one — the YubiKey 5 this was developed against has
only the preview, so renaming genuinely cannot apply to it. That is a property of the key, and the
page says so rather than offering a control that fails.

**A PIN buys a token, and the token is what reads.** Listing needs a pin/uv token minted for
``CREDENTIAL_MGMT``; an unscoped one is refused on 2.1 keys. The PIN is asked for once, when the
list is opened, and not held.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from hardware_ui.core import DeviceError, NotSupported

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Passkey:
    """One credential, as the key describes it."""

    key: str
    """The credential id as hex -- opaque, and the only durable handle to it."""

    site: str
    """The relying party: the website or application it signs in to."""

    user: str
    """Whatever the site recorded as the account -- an email address, a username, sometimes both."""

    def label(self) -> str:
        return f"{self.site}: {self.user}" if self.user else self.site


@dataclass(frozen=True, slots=True)
class Passkeys:
    items: tuple[Passkey, ...] = ()
    used: int = 0
    remaining: int = 0

    @property
    def capacity(self) -> int:
        """What the key can hold in total, as far as it will say."""
        return self.used + self.remaining


def supported(info: Any) -> bool:
    """Whether this key can list its credentials at all, by either command."""
    from fido2.ctap2 import CredentialManagement

    return bool(info) and CredentialManagement.is_supported(info)


def rename_supported(info: Any) -> bool:
    """Renaming is CTAP 2.1 proper. The preview command cannot do it."""
    from fido2.ctap2 import CredentialManagement

    return bool(info) and CredentialManagement.is_update_supported(info)


def _manager(ctap: Any, pin: str) -> Any:
    """A ``CredentialManagement`` bound to a token minted for exactly that permission."""
    from fido2.ctap2 import ClientPin, CredentialManagement

    if not pin:
        raise NotSupported("locked")
    client = ClientPin(ctap)
    token = client.get_pin_token(pin, ClientPin.PERMISSION.CREDENTIAL_MGMT)
    return CredentialManagement(ctap, client.protocol, token)


def read(ctap: Any, pin: str) -> Passkeys:
    """Every credential on the key, grouped by the site that owns it.

    Two round trips per site: the relying parties, then the credentials for each. There is no
    command that returns everything at once, and a key with a dozen accounts is a dozen calls --
    which is why this happens when asked rather than on every connect.
    """
    from fido2.ctap2 import CredentialManagement as CM

    manager = _manager(ctap, pin)
    try:
        meta = manager.get_metadata()
        used = int(meta.get(CM.RESULT.EXISTING_CRED_COUNT, 0) or 0)
        remaining = int(meta.get(CM.RESULT.MAX_REMAINING_COUNT, 0) or 0)

        items: list[Passkey] = []
        for entry in manager.enumerate_rps():
            rp = entry.get(CM.RESULT.RP) or {}
            site = str(rp.get("id") or rp.get("name") or "unknown")
            rp_hash = entry.get(CM.RESULT.RP_ID_HASH)
            for cred in manager.enumerate_creds(rp_hash):
                user = cred.get(CM.RESULT.USER) or {}
                descriptor = cred.get(CM.RESULT.CREDENTIAL_ID)
                items.append(
                    Passkey(
                        key=bytes(getattr(descriptor, "id", b"")).hex(),
                        site=site,
                        user=str(user.get("displayName") or user.get("name") or ""),
                    )
                )
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(_explain(exc)) from exc

    return Passkeys(
        items=tuple(sorted(items, key=lambda p: (p.site.casefold(), p.user.casefold()))),
        used=used,
        remaining=remaining,
    )


def delete(ctap: Any, pin: str, credential_id: str) -> str:
    """Remove one credential. Irreversible, and the site will no longer accept this key."""
    from fido2.ctap2 import CredentialManagement as CM
    from fido2.webauthn import PublicKeyCredentialDescriptor

    manager = _manager(ctap, pin)
    wanted = bytes.fromhex(credential_id)
    try:
        for entry in manager.enumerate_rps():
            for cred in manager.enumerate_creds(entry.get(CM.RESULT.RP_ID_HASH)):
                descriptor = cred.get(CM.RESULT.CREDENTIAL_ID)
                if bytes(getattr(descriptor, "id", b"")) == wanted:
                    manager.delete_cred(
                        descriptor
                        if isinstance(descriptor, PublicKeyCredentialDescriptor)
                        else PublicKeyCredentialDescriptor("public-key", wanted)
                    )
                    return "The passkey was deleted from the key."
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(_explain(exc)) from exc
    raise DeviceError("That passkey is no longer on the key.")


def rename(ctap: Any, pin: str, credential_id: str, name: str) -> str:
    """Change the account name a credential carries.

    CTAP 2.1 only; see :func:`rename_supported`.
    """
    from fido2.ctap2 import CredentialManagement as CM
    from fido2.webauthn import PublicKeyCredentialUserEntity

    if not name.strip():
        raise DeviceError("A name is required.")
    manager = _manager(ctap, pin)
    wanted = bytes.fromhex(credential_id)
    try:
        for entry in manager.enumerate_rps():
            for cred in manager.enumerate_creds(entry.get(CM.RESULT.RP_ID_HASH)):
                descriptor = cred.get(CM.RESULT.CREDENTIAL_ID)
                if bytes(getattr(descriptor, "id", b"")) != wanted:
                    continue
                existing = cred.get(CM.RESULT.USER) or {}
                manager.update_user_info(
                    descriptor,
                    PublicKeyCredentialUserEntity(
                        name=str(existing.get("name") or name),
                        id=bytes(existing.get("id") or b""),
                        display_name=name.strip(),
                    ),
                )
                return "The passkey was renamed."
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(_explain(exc)) from exc
    raise DeviceError("That passkey is no longer on the key.")


def _explain(exc: Exception) -> str:
    """CTAP errors a person can act on, rather than a code."""
    from fido2.ctap import CtapError

    if isinstance(exc, CtapError):
        code = exc.code
        if code == CtapError.ERR.PIN_INVALID:
            return "That PIN is wrong."
        if code == CtapError.ERR.PIN_AUTH_BLOCKED:
            return "Too many wrong PINs in a row. Unplug the key and plug it back in."
        if code == CtapError.ERR.PIN_BLOCKED:
            return (
                "The PIN is blocked. The key can only be used again after a factory reset, which "
                "erases every credential on it."
            )
        if code == CtapError.ERR.NO_CREDENTIALS:
            return "There are no passkeys on this key."
        if code == CtapError.ERR.UNSUPPORTED_OPTION:
            return "This key does not support that — its firmware offers only the older command."
        return f"The key reported {code.name}."
    return str(exc)


__all__ = [
    "Passkey",
    "Passkeys",
    "delete",
    "read",
    "rename",
    "rename_supported",
    "supported",
]
