"""The YubiKey's two OTP slots, over the HID keyboard interface.

The second of the two interfaces this module owns. Everything else a YubiKey can do lives on the
smartcard interface and is deliberately out of scope -- ``ykman`` opens that one *exclusively*, so
holding it locks out ``gpg-agent``, ``scdaemon`` and Kleopatra, and them holding it locks out us.
See ``docs/YUBIKEY_UI_BEHAVIOUR.md`` §11. The practical payoff is that this module depends on no
daemon at all: the CTAP handle the base module already owns, plus this, behind one udev rule.

**Bound to one key by its serial.** ``list_otp_devices()`` enumerates every attached YubiKey; the
serial read over CTAP is what says which one is *this* device. Nothing else joins the interfaces
reliably -- two keys of the same model have identical USB product strings.

**Opened per operation, never held.** A held handle is one no other tool can use, which is the
same courtesy the smartcard rule is built on.

Nothing here is imported at module scope: ``ykman`` stays optional, and a machine without it keeps
a working CTAP page.
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

SLOT_ONE = 1
SLOT_TWO = 2

SECRET_BYTES = 20
"""HMAC-SHA1 key length. ``ykman`` calls it ``HMAC_KEY_SIZE``; a secret is 40 hex characters."""

STATIC_MAX = 38
"""``SCAN_CODES_SIZE`` -- a static password is stored as scan codes, not as text."""

PRIVATE_ID_BYTES = 6
"""``UID_SIZE``. The private identity inside a Yubico OTP."""

AES_KEY_BYTES = 16
"""``KEY_SIZE``. The AES key a validation server needs to decrypt this key's OTPs."""

INSTALL_HINT = (
    "The OTP slots need app-crypt/yubikey-manager, which is not installed.\n\n"
    "On Gentoo:  emerge app-crypt/yubikey-manager"
)
PERMISSION_HINT = (
    "This YubiKey's OTP interface is not readable by your user.\n\n"
    "Install the udev rule from docs/INSTALL.md and re-plug the key:\n"
    '  SUBSYSTEM=="hidraw", MODE="0660", TAG+="uaccess"\n\n'
    "The OTP interface is a separate USB interface from the FIDO one, so a key whose security-key "
    "page works can still be unreadable here."
)


@dataclass(frozen=True, slots=True)
class SlotState:
    """What is in the two slots. The anchor for every decision on the page."""

    configured: bool = False
    touch_triggered: bool = False


def _ykman() -> Any:
    try:
        import yubikit.yubiotp as mod
    except ImportError as exc:
        raise DependencyMissing(INSTALL_HINT) from exc
    return mod


@contextmanager
def session(serial: int | None) -> Iterator[Any]:
    """A ``YubiOtpSession`` for the key with this *serial*, closed on the way out.

    ``Unreachable`` rather than an error for the two ordinary cases -- the OTP application switched
    off, and a key that has been unplugged. Neither is a fault.
    """
    _ykman()
    from ykman.device import list_otp_devices, read_info
    from yubikit.core.otp import OtpConnection
    from yubikit.yubiotp import YubiOtpSession

    if serial is None:
        raise NotSupported(
            "This key reports no serial number, so its OTP slots cannot be told apart from "
            "another key's."
        )

    try:
        devices = list_otp_devices()
    except Exception as exc:  # noqa: BLE001 - reported as unreachable, not as a traceback
        raise Unreachable(f"could not enumerate the OTP interface: {exc}") from exc

    for device in devices:
        try:
            with device.open_connection(OtpConnection) as connection:
                if read_info(connection, device.pid).serial != serial:
                    continue
                yield YubiOtpSession(connection)
                return
        except PermissionError as exc:
            raise Unreachable(PERMISSION_HINT) from exc
        except (Unreachable, NotSupported, DeviceError):
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("OTP device rejected", exc_info=True)
            last = exc
            del last

    raise Unreachable(
        "This key's OTP application is switched off, or the key is no longer plugged in. "
        "It can be switched back on from the Applications list."
    )


def read_all_states() -> dict[int, dict[int, SlotState]]:
    """Every attached key's slots, keyed by serial. **Call this before touching FIDO.**

    A YubiKey serves one USB interface at a time and takes about three seconds to hand over --
    Yubico call it *reclaim*, and ``ykman`` waits it out by retrying six times half a second apart
    (``OtpYubiKeyDevice.open_connection``). So reading the OTP slots after any FIDO traffic costs
    three seconds, and reading them first costs a few milliseconds. Measured on a YubiKey 5 NFC:
    **77 ms first, 3036 ms after, 25 ms once the window has passed.**

    Reading every key rather than one means the serial does not have to be known yet -- and the
    serial is only known after a management read, which is itself FIDO traffic.
    """
    _ykman()
    from ykman.device import list_otp_devices, read_info
    from yubikit.core.otp import OtpConnection
    from yubikit.yubiotp import SLOT, YubiOtpSession

    try:
        devices = list_otp_devices()
    except Exception as exc:  # noqa: BLE001
        raise Unreachable(f"could not enumerate the OTP interface: {exc}") from exc

    out: dict[int, dict[int, SlotState]] = {}
    for device in devices:
        try:
            with device.open_connection(OtpConnection) as connection:
                serial = read_info(connection, device.pid).serial
                if serial is None:
                    continue
                state = YubiOtpSession(connection).get_config_state()
                out[int(serial)] = {
                    int(slot): SlotState(
                        configured=bool(state.is_configured(slot)),
                        touch_triggered=bool(state.is_touch_triggered(slot)),
                    )
                    for slot in SLOT
                }
        except PermissionError as exc:
            raise Unreachable(PERMISSION_HINT) from exc
        except Exception:  # noqa: BLE001 - one unreadable key must not hide the others
            log.debug("OTP device unreadable", exc_info=True)
    return out


def read_state(serial: int | None) -> dict[int, SlotState]:
    """What one key's slots hold. Pays the reclaim wait if FIDO has been used recently."""
    with session(serial) as otp:
        from yubikit.yubiotp import SLOT

        state = otp.get_config_state()
        out: dict[int, SlotState] = {}
        for slot in SLOT:
            try:
                out[int(slot)] = SlotState(
                    configured=bool(state.is_configured(slot)),
                    touch_triggered=bool(state.is_touch_triggered(slot)),
                )
            except Exception:  # noqa: BLE001 - older keys report less; absent beats invented
                log.debug("slot %s state unavailable", slot, exc_info=True)
        return out


# --------------------------------------------------------------------------- writing


def parse_secret(text: str) -> bytes:
    """A 40-character hex HMAC-SHA1 secret, or empty to generate one.

    Checked here rather than at the key, because a key accepts a *wrong* secret perfectly happily
    -- it has no idea what the other end expects. The failure would surface later, as a disk that
    no longer unlocks.
    """
    cleaned = re.sub(r"[\s:-]", "", text or "")
    if not cleaned:
        import os

        return os.urandom(SECRET_BYTES)
    if not re.fullmatch(r"[0-9a-fA-F]*", cleaned):
        raise DeviceError("A secret must be hexadecimal — digits and a–f only.")
    if len(cleaned) != SECRET_BYTES * 2:
        raise DeviceError(
            f"A secret must be exactly {SECRET_BYTES * 2} hex characters "
            f"({SECRET_BYTES} bytes); that one is {len(cleaned)}."
        )
    return bytes.fromhex(cleaned)


def program_chalresp(
    serial: int | None, slot: int, secret: bytes, *, require_touch: bool, access_code: bytes | None
) -> None:
    """HMAC-SHA1 challenge-response -- what ``yubikey-luks``, ``ykfde`` and offline
    ``pam_yubico`` use.

    ``lt64`` is set because every one of those callers sends a challenge shorter than 64 bytes and
    expects the variable-length behaviour; leaving it off is the classic reason a slot programmed
    by hand does not match what enrolled it.
    """
    mod = _ykman()
    config = mod.HmacSha1SlotConfiguration(secret).lt64(True).require_touch(require_touch)
    _put(serial, slot, config, access_code)


def parse_oath_secret(text: str) -> bytes:
    """An OATH secret: base32 as a service publishes it, hex, or empty to generate one.

    Base32 first, because that is what an authenticator service hands you and what
    ``ykman otp hotp`` accepts. Hex is allowed because the rest of this page speaks hex.
    """
    cleaned = re.sub(r"[\s:-]", "", text or "")
    if not cleaned:
        import os

        return os.urandom(SECRET_BYTES)
    if re.fullmatch(r"[0-9a-fA-F]+", cleaned) and len(cleaned) % 2 == 0:
        return bytes.fromhex(cleaned)
    try:
        from ykman.otp import parse_b32_key

        return parse_b32_key(cleaned)
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(
            "That is not a usable secret. Give the base32 string the service showed you, or an "
            "even number of hex characters, or leave it empty to generate one."
        ) from exc


def program_hotp(
    serial: int | None,
    slot: int,
    secret: bytes,
    *,
    digits8: bool,
    counter: int = 0,
    access_code: bytes | None,
) -> None:
    """OATH-HOTP: the key types the next counter-based code when touched.

    Not to be confused with the OATH *application*, which stores many credentials on the smartcard
    interface and is out of scope. This is one credential, in one OTP slot, typed as keystrokes.
    """
    mod = _ykman()
    config = mod.HotpSlotConfiguration(secret).digits8(digits8)
    if counter:
        try:
            config = config.imf(counter)
        except ValueError as exc:
            raise DeviceError(
                "The initial counter has to be a multiple of 16, between 0 and 1048560."
            ) from exc
    _put(serial, slot, config, access_code)


def program_yubiotp(
    serial: int | None,
    slot: int,
    *,
    public_id: str = "",
    private_id: str = "",
    key: str = "",
    access_code: bytes | None,
) -> tuple[str, str, str]:
    """A Yubico OTP credential, generated here. Returns ``(public id, private id, AES key)``.

    All three are generated rather than asked for: the public id is derived from the key's serial
    the way ``ykman otp yubiotp --serial-public-id`` does it, and the other two are random. There
    is nothing useful a person can type into those fields that is not either random or already
    determined.

    **Nothing is uploaded.** The Yubico Authenticator offers to send the credential to YubiCloud;
    doing that from here would mean posting a freshly generated secret to a third party, which is
    not this application's decision to make. The three values are returned so they can be
    registered wherever they belong -- Yubico's upload form, or a self-hosted validation server.
    """
    import struct

    mod = _ykman()
    from yubikit.core.otp import modhex_decode, modhex_encode

    if not public_id:
        if serial is None:
            raise NotSupported(
                "A Yubico OTP credential needs a public identity. Give one, or use a key that "
                "reports a serial number so it can be derived."
            )
        public_id = modhex_encode(b"\xff\x00" + struct.pack(b">I", int(serial)))
    try:
        fixed = modhex_decode(public_id)
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(
            "A public identity is modhex — only the letters c b d e f g h i j k l n r t u v."
        ) from exc

    private = _bytes_or_random(private_id, PRIVATE_ID_BYTES, "private identity")
    secret = _bytes_or_random(key, AES_KEY_BYTES, "secret key")
    _put(serial, slot, mod.YubiOtpSlotConfiguration(fixed, private, secret), access_code)
    return public_id, private.hex(), secret.hex()


def _bytes_or_random(text: str, size: int, what: str) -> bytes:
    import os

    cleaned = re.sub(r"[\s:-]", "", text or "")
    if not cleaned:
        return os.urandom(size)
    if not re.fullmatch(r"[0-9a-fA-F]*", cleaned) or len(cleaned) != size * 2:
        raise DeviceError(
            f"A {what} is exactly {size * 2} hex characters; that one is {len(cleaned)}."
        )
    return bytes.fromhex(cleaned)


def program_static(
    serial: int | None, slot: int, password: str, *, layout: str, access_code: bytes | None
) -> None:
    """A fixed string the key types. Stored as scan codes, so the keyboard layout matters."""
    mod = _ykman()
    from ykman.scancodes import KEYBOARD_LAYOUT, encode

    if not password:
        raise DeviceError("No password was given.")
    try:
        codes = encode(password, KEYBOARD_LAYOUT[layout])
    except KeyError as exc:
        raise DeviceError(
            f"“{password}” cannot be typed on a {layout} keyboard. Every character has to exist "
            "on that layout, because the key sends key presses rather than text."
        ) from exc
    if len(codes) > STATIC_MAX:
        raise DeviceError(
            f"A static password can be at most {STATIC_MAX} key presses; that one is {len(codes)}."
        )
    _put(serial, slot, mod.StaticPasswordSlotConfiguration(codes), access_code)


def program_ndef(serial: int | None, slot: int, value: str, access_code: bytes | None) -> None:
    """What an NFC key emits when tapped to a phone: a URI, or plain text."""
    mod = _ykman()
    if not value:
        raise DeviceError("No URI or text was given.")
    kind = mod.NDEF_TYPE.URI if "://" in value else mod.NDEF_TYPE.TEXT
    with session(serial) as otp:
        _guard(
            lambda: otp.set_ndef_configuration(
                _slot(slot), uri=value, cur_acc_code=access_code, ndef_type=kind
            )
        )


def swap(serial: int | None) -> None:
    """Exchange the two slots. Affects both at once, which is why it is worded as one action."""
    with session(serial) as otp:
        _guard(otp.swap_slots)


def delete(serial: int | None, slot: int, access_code: bytes | None) -> None:
    with session(serial) as otp:
        _guard(lambda: otp.delete_slot(_slot(slot), cur_acc_code=access_code))


def parse_access_code(text: str) -> bytes | None:
    """Six bytes as twelve hex characters, or nothing at all."""
    cleaned = re.sub(r"[\s:-]", "", text or "")
    if not cleaned:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{12}", cleaned):
        raise DeviceError("An access code is exactly 12 hex characters (6 bytes).")
    return bytes.fromhex(cleaned)


def _slot(slot: int) -> Any:
    from yubikit.yubiotp import SLOT

    return SLOT(slot)


def _put(serial: int | None, slot: int, config: Any, access_code: bytes | None) -> None:
    with session(serial) as otp:
        _guard(lambda: otp.put_configuration(_slot(slot), config, cur_acc_code=access_code))


def _guard(call: Any) -> None:
    """Turn the two refusals a slot actually produces into something a user can act on."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001
        raise DeviceError(explain(exc)) from exc


def explain(exc: Exception) -> str:
    try:
        from yubikit.core import NotSupportedError
        from yubikit.core.otp import CommandRejectedError
    except ImportError:  # pragma: no cover - ykman is present by the time anything writes
        return str(exc)

    if isinstance(exc, CommandRejectedError):
        return (
            "The key refused the change. That normally means the slot is protected by an access "
            "code — enter it and try again."
        )
    if isinstance(exc, NotSupportedError):
        return f"This key does not support that: {exc}"
    return str(exc)


__all__ = [
    "SlotState",
    "delete",
    "explain",
    "parse_access_code",
    "parse_secret",
    "program_chalresp",
    "program_ndef",
    "program_static",
    "read_state",
    "session",
    "swap",
]
