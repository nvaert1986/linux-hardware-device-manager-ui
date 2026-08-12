"""OATH accounts -- the TOTP and HOTP credentials a YubiKey stores, up to 32 of them.

This is the one thing here that lives on the **smartcard interface**, and it is allowed in for one
reason: it is what a YubiKey is most often bought for. Everything else on that interface -- PIV
certificates, OpenPGP keys -- stays out, because Kleopatra and GnuPG own it and do it better.

**Never held.** ``ykman`` opens the smartcard *exclusively* by default, so a connection kept open
locks ``gpg-agent``, ``scdaemon`` and Kleopatra out of the card entirely. Every function here opens
a connection, does one thing and closes it, so the card is claimed for milliseconds at a time and
free the rest of the time. That is stricter than the Yubico Authenticator, whose helper holds one
connection per open screen and closes it when you navigate away -- its ``RpcNode.get_child`` closes
the previous child before creating the next, and ``RpcNode.close`` cascades on exit.

The cost of touching this interface at all is the same USB reclaim the OTP slots pay: about three
seconds to hand over from FIDO and back again. So accounts are fetched the same way -- in the
background, after connecting, never during it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from hardware_ui.core import DependencyMissing, DeviceError, NotSupported, Unreachable

log = logging.getLogger(__name__)

MAX_ACCOUNTS = 32
"""What a YubiKey 5 holds. Reported for the user's sake; the key enforces it itself."""

PERIODS = (20, 30, 45, 60)
DIGITS = (6, 8)
ALGORITHMS = ("SHA1", "SHA256", "SHA512")

INSTALL_HINT = (
    "OATH accounts need app-crypt/yubikey-manager, which is not installed.\n\n"
    "On Gentoo:  emerge app-crypt/yubikey-manager"
)
PCSC_HINT = (
    "OATH accounts are stored on the key's smartcard interface, which needs the pcsc-lite daemon "
    "running.\n\n"
    "On Gentoo:  emerge sys-apps/pcsc-lite app-crypt/ccid\n"
    "            systemctl enable --now pcscd.socket"
)
BUSY_HINT = (
    "Another program is using this key's smartcard interface — usually gpg-agent, scdaemon or "
    "Kleopatra.\n\n"
    "They take it exclusively, as this application does for the moment it needs it. Close the "
    "other program, or run:  gpgconf --reload scdaemon"
)


@dataclass(frozen=True, slots=True)
class Account:
    """One credential, as the key reports it."""

    key: str
    issuer: str
    name: str
    oath_type: str
    touch: bool = False
    code: str = ""
    """Empty when the credential needs a touch, or is counter-based and has not been asked."""

    period: int = 30
    valid_to: int = 0
    """Unix time this code stops being valid. Zero when there is no code to expire."""


@dataclass(frozen=True, slots=True)
class Accounts:
    items: tuple[Account, ...] = ()
    has_password: bool = False
    locked: bool = False

    @property
    def expires_at(self) -> int:
        """When the soonest code goes stale, or ``0`` if none of them do.

        Read from the key rather than assumed: accounts can be 20, 30, 45 or 60 seconds, and they
        do not share a boundary. Counter-based and touch-required credentials never expire on
        their own, so they do not pull the next refresh forward.
        """
        times = [a.valid_to for a in self.items if a.valid_to]
        return min(times) if times else 0


def _ykman() -> Any:
    try:
        import yubikit.oath as mod
    except ImportError as exc:
        raise DependencyMissing(INSTALL_HINT) from exc
    return mod


@contextmanager
def session(serial: int | None, password: str = "") -> Iterator[Any]:
    """An ``OathSession`` for this key, **closed on the way out, every time**.

    The smartcard is taken exclusively while this is open, so the window is kept as small as it
    can be: one operation, then release.
    """
    _ykman()
    from ykman.device import list_ccid_devices, read_info
    from yubikit.core.smartcard import SmartCardConnection
    from yubikit.oath import OathSession

    if serial is None:
        raise NotSupported(
            "This key reports no serial number, so its accounts cannot be told apart from "
            "another key's."
        )
    try:
        devices = list(list_ccid_devices())
    except Exception as exc:  # noqa: BLE001
        raise Unreachable(PCSC_HINT) from exc
    if not devices:
        raise Unreachable(PCSC_HINT)

    for device in devices:
        try:
            with device.open_connection(SmartCardConnection) as connection:
                if read_info(connection, device.pid).serial != serial:
                    continue
                oath = OathSession(connection)
                if oath.locked:
                    if not password:
                        raise NotSupported("locked")
                    _unlock(oath, password)
                yield oath
                return
        except (Unreachable, NotSupported, DeviceError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise Unreachable(_explain(exc)) from exc

    raise Unreachable(
        "This key's OATH application is switched off, or the key is no longer plugged in. "
        "It can be switched back on from the Applications tab."
    )


def _unlock(oath: Any, password: str) -> None:
    try:
        oath.validate(oath.derive_key(password))
    except Exception as exc:  # noqa: BLE001
        raise DeviceError("That password is wrong.") from exc


def _explain(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "sharing violation" in lowered or "exclusive" in lowered or "in use" in lowered:
        return BUSY_HINT
    if "no smart card" in lowered or "no reader" in lowered or "service not available" in lowered:
        return PCSC_HINT
    return text


# --------------------------------------------------------------------------- reading


def read(serial: int | None, password: str = "") -> Accounts:
    """Every account, with a live code where one can be had without a touch.

    ``calculate_all`` is one round trip for the lot, which matters when the connection is opened
    and closed around it. Credentials that need a touch come back without a code -- asking for
    them all would make the key blink for every account on the page at once.
    """
    from yubikit.oath import OATH_TYPE

    try:
        with session(serial, password) as oath:
            entries = oath.calculate_all()
            items = []
            for credential, code in entries.items():
                items.append(
                    Account(
                        key=credential.id.hex(),
                        issuer=credential.issuer or "",
                        name=credential.name,
                        oath_type=OATH_TYPE(credential.oath_type).name,
                        touch=bool(credential.touch_required),
                        code=code.value if code is not None else "",
                        period=int(getattr(credential, "period", 30) or 30),
                        valid_to=int(getattr(code, "valid_to", 0) or 0) if code else 0,
                    )
                )
            return Accounts(
                items=tuple(sorted(items, key=lambda a: (a.issuer.lower(), a.name.lower()))),
                has_password=bool(oath.has_key),
                locked=False,
            )
    except NotSupported as exc:
        if str(exc) == "locked":
            return Accounts(has_password=True, locked=True)
        raise


def code_for(serial: int | None, credential_id: str, password: str = "") -> str:
    """One code, for a credential that needs a touch. The key blinks; the user presses it."""
    with session(serial, password) as oath:
        wanted = bytes.fromhex(credential_id)
        for credential in oath.list_credentials():
            if credential.id == wanted:
                return oath.calculate_code(credential).value
    raise DeviceError("That account is no longer on the key.")


# --------------------------------------------------------------------------- writing


def add(
    serial: int | None,
    *,
    issuer: str,
    name: str,
    secret: str,
    oath_type: str,
    algorithm: str,
    digits: int,
    period: int,
    touch: bool,
    password: str = "",
) -> None:
    """Add one account. Everything is checked here, because the key checks almost nothing."""
    from ykman.otp import parse_b32_key
    from yubikit.oath import HASH_ALGORITHM, OATH_TYPE, CredentialData

    if not name.strip():
        raise DeviceError("An account name is required.")
    cleaned = re.sub(r"[\s-]", "", secret or "")
    if not cleaned:
        raise DeviceError("A secret key is required — it comes from the service you are adding.")
    try:
        raw = parse_b32_key(cleaned)
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(
            "That secret is not valid base32. It is the string the service shows next to its QR "
            "code — letters A–Z and digits 2–7."
        ) from exc

    data = CredentialData(
        name=name.strip(),
        oath_type=OATH_TYPE[oath_type],
        hash_algorithm=HASH_ALGORITHM[algorithm],
        secret=raw,
        digits=int(digits),
        period=int(period),
        issuer=issuer.strip() or None,
    )
    with session(serial, password) as oath:
        if len(oath.list_credentials()) >= MAX_ACCOUNTS:
            raise DeviceError(
                f"This key already holds {MAX_ACCOUNTS} accounts, which is all it has room for. "
                "Delete one first."
            )
        oath.put_credential(data, touch_required=touch)


def delete(serial: int | None, credential_id: str, password: str = "") -> None:
    with session(serial, password) as oath:
        wanted = bytes.fromhex(credential_id)
        for credential in oath.list_credentials():
            if credential.id == wanted:
                oath.delete_credential(credential.id)
                return
    raise DeviceError("That account is no longer on the key.")


__all__ = [
    "ALGORITHMS",
    "DIGITS",
    "MAX_ACCOUNTS",
    "PERIODS",
    "Account",
    "Accounts",
    "add",
    "code_for",
    "delete",
    "read",
    "session",
]
