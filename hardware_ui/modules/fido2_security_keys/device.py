"""FIDO2 / U2F security keys, over CTAP.

Converted from ``kcm-fido-keys``, a KDE System Settings module that was built and tested against
real hardware. Its two design decisions are kept because they are the right ones:

**Vendor-neutral.** Everything goes through the CTAP standard, so one module serves YubiKey,
Nitrokey, OnlyKey, SoloKey, Token2 and anything else compliant. No ``ykman``, no ``nitropy``, no
per-brand libraries. A vendor's own extras -- a YubiKey's OTP/PIV/OATH applets, its USB/NFC
interface toggles -- belong in a module that ``extends`` this one, which is why this class is
written to be subclassed.

**Gated on what the key reports.** A control the hardware cannot honour is absent, not present and
failing. The test key here makes the point: a YubiKey 5 advertises ``credentialMgmtPreview`` but
not ``credMgmt``, and no ``authnrCfg`` at all, so CTAP 2.1 policy simply does not apply to it.

The KCM used ``libfido2`` through C++. This uses ``python-fido2``, Yubico's pure-Python CTAP2
implementation, which covers the same ground with no bindings to build -- and it is a per-module
dependency, so an installation without it runs normally and only this family is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hardware_ui.core import (
    Advisory,
    CapabilitySet,
    DependencyMissing,
    Device,
    DeviceError,
    DeviceInfo,
    NotSupported,
    Unreachable,
)

from . import capabilities as C
from . import credentials as CRED

log = logging.getLogger(__name__)

MODULE_ID = "fido2_security_keys"

INSTALL_HINT = (
    "Security keys need the FIDO2 library, which is not installed.\n\n"
    "On Gentoo:  emerge dev-python/fido2\n\n"
    "Nothing else in this application needs it."
)
PERMISSION_HINT = (
    "This security key's device node is not readable by your user.\n\n"
    "Install the udev rule from docs/INSTALL.md and re-plug the key:\n"
    '  SUBSYSTEM=="hidraw", MODE="0660", TAG+="uaccess"'
)

#: Algorithm identifiers, as COSE registers them. Shown by name because "-7" tells nobody anything.
COSE_NAMES: dict[int, str] = {
    -7: "ES256", -8: "EdDSA", -35: "ES384", -36: "ES512",
    -37: "PS256", -38: "PS384", -39: "PS512",
    -257: "RS256", -258: "RS384", -259: "RS512",
}


def _cose(alg: int) -> str:
    return COSE_NAMES.get(alg, f"COSE {alg}")


class Fido2SecurityKey(Device):
    """One CTAP authenticator. Written to be subclassed by vendor modules.

    A specialising module overrides :meth:`extra_capabilities`, :meth:`extra_values` and
    :meth:`handle_set`; everything the standard provides is inherited. The registry claims a
    device with the most specialised module that matches, so the key still appears once.
    """

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        # One thread owns the key: CTAP is request/response over a single HID channel, and a
        # second reader would consume the reply another call is waiting for.
        self._lock = asyncio.Lock()
        self._dev: Any = None
        self._ctap: Any = None
        self._info: Any = None
        self._set = CapabilitySet()
        self._values: dict[str, Any] = {}
        self._pin: str = ""
        self._retries: int | None = None
        self._advisories: dict[str, Advisory] = {}
        self._passkeys: Any = None
        self._passkey_problem: str = ""

    @property
    def capabilities(self) -> CapabilitySet:
        return self._set

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            from fido2.ctap2 import Ctap2
            from fido2.hid import CtapHidDevice
        except ImportError as exc:
            raise DependencyMissing(INSTALL_HINT) from exc

        wanted = self.info.path
        nodes = {str(n) for n in (self.info.properties.get("nodes") or [])} | {wanted}
        try:
            devices = list(CtapHidDevice.list_devices())
        except PermissionError as exc:
            raise Unreachable(PERMISSION_HINT) from exc
        except Exception as exc:  # noqa: BLE001 - reported as unreachable, not as a traceback
            raise Unreachable(f"could not enumerate security keys: {exc}") from exc

        # Match by device node. A key answers on several interfaces -- a YubiKey's OTP applet
        # types like a keyboard -- and only the one carrying usage page 0xF1D0 speaks CTAP.
        self._dev = next((d for d in devices if str(d.descriptor.path) in nodes), None)
        if self._dev is None:
            raise Unreachable(
                "this key is no longer present, or its FIDO interface is disabled"
            )

        try:
            self._ctap = Ctap2(self._dev)
            self._info = self._ctap.get_info()
        except Exception as exc:  # noqa: BLE001
            # A U2F-only key answers no CTAP2 GetInfo. That is a fact about the key, not a fault.
            log.info("%s: no CTAP2 info (%s)", self.info.name, exc)
            self._ctap = None
            self._info = None
        # Read once here, not from _rebuild(). A rebuild happens for reasons that have nothing to
        # do with the key -- a vendor module redrawing a tab -- and a device read hidden inside it
        # makes every one of those cost a round trip. On a YubiKey it costs far more than that:
        # the key serves one USB interface at a time and takes ~3s to hand back over, so a stray
        # CTAP read after its OTP interface was used pays that in full.
        self._retries = self._read_pin_retries()
        self._rebuild()

    async def disconnect(self) -> None:
        dev, self._dev = self._dev, None
        self._ctap = self._info = None
        # The PIN is held only for the life of the connection, and not a moment longer.
        self._pin = ""
        if dev is not None:
            try:
                await asyncio.to_thread(dev.close)
            except Exception:  # noqa: BLE001
                log.debug("closing the key failed; dropping it anyway", exc_info=True)

    # ------------------------------------------------------------------ reading

    def _options(self) -> dict[str, bool]:
        return dict(getattr(self._info, "options", None) or {})

    def supports(self, option: str) -> bool:
        """Whether the key advertises a CTAP option. The only honest gate for any control."""
        return bool(self._options().get(option))

    def _rebuild(self) -> None:
        identity, capabilities = self._describe()
        options = C.option_rows(self._options())
        opts = self._options()

        self._set = C.build(
            identity=[k for k, _ in identity],
            capabilities=[k for k, _ in capabilities],
            options=options,
            can_set_pin=self._ctap is not None and "clientPin" in opts,
            has_pin=bool(opts.get("clientPin")),
            can_configure=bool(opts.get("authnrCfg")),
            can_set_min_pin=bool(opts.get("authnrCfg")) and bool(opts.get("setMinPINLength", True)),
            can_enterprise=bool(opts.get("authnrCfg")) and "ep" in opts,
            min_pin_length=int(getattr(self._info, "min_pin_length", 4) or 4),
        )
        rows = list(self._set) + self._passkey_capabilities()
        extra = self.extra_capabilities()
        if extra:
            rows += list(extra)
        # One form is built per group in list order, so group order is tab order. Passkeys are
        # something the key *holds*, not something you configure, so they belong with what is read
        # rather than after it -- appending left them past Configuration, at the end.
        order = {C.GROUP_INFO: 0, C.GROUP_PASSKEYS: 1, C.GROUP_CONFIG: 3}
        groups = list(dict.fromkeys(row.group for row in rows))
        self._set = CapabilitySet(
            sorted(rows, key=lambda row: (order.get(row.group, 2), groups.index(row.group)))
        )

        self._values = {f"{C.INFO_PREFIX}{k}": v for k, v in identity + capabilities}
        self._values.update(
            {f"{C.INFO_PREFIX}option.{k}": ("Yes" if opts.get(k) else "No") for k, _ in options}
        )
        if opts.get("authnrCfg"):
            self._values[C.ALWAYS_UV_KEY] = bool(opts.get("alwaysUv"))
            self._values[C.MIN_PIN_KEY] = int(getattr(self._info, "min_pin_length", 4) or 4)
        self._values.update(self._passkey_values())
        self._values.update(self.extra_values())
        self._bump_capabilities()

    def _describe(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """``(identity rows, capability rows)`` -- everything the key says about itself."""
        identity: list[tuple[str, str]] = [("model", self.info.name)]
        vid, pid = self.info.vendor_id, self.info.product_id
        if vid is not None and pid is not None:
            identity.append(("usb_id", f"{vid:04x}:{pid:04x}"))
        identity.append(("node", self.info.path))

        if self._info is None:
            identity.append(("protocol", "U2F only — this key does not speak CTAP2"))
            return identity, []

        aaguid = getattr(self._info, "aaguid", None)
        if aaguid:
            raw = bytes(aaguid).hex()
            identity.append((
                "aaguid",
                f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}",
            ))

        rows: list[tuple[str, str]] = []
        versions = list(getattr(self._info, "versions", None) or [])
        if versions:
            rows.append(("versions", ", ".join(versions)))
        for field, label in (("extensions", "extensions"), ("transports", "transports")):
            values = list(getattr(self._info, field, None) or [])
            if values:
                rows.append((label, ", ".join(values)))
        algorithms = [
            _cose(a["alg"]) for a in (getattr(self._info, "algorithms", None) or [])
            if isinstance(a, dict) and "alg" in a
        ]
        if algorithms:
            rows.append(("algorithms", ", ".join(algorithms)))
        protocols = list(getattr(self._info, "pin_uv_protocols", None) or [])
        if protocols:
            rows.append(("pin_protocols", ", ".join(str(p) for p in protocols)))
        for field, label in (
            ("max_msg_size", "max_msg_size"),
            ("max_creds_in_list", "max_credentials_in_list"),
            ("max_cred_id_length", "max_credential_id_length"),
            ("min_pin_length", "minimum_pin_length"),
        ):
            value = getattr(self._info, field, None)
            if value:
                rows.append((label, str(value)))
        rows.append(("pin_state", "Set" if self.supports("clientPin") else "Not set"))
        if self._retries is not None:
            rows.append(("pin_retries", str(self._retries)))
        return identity, rows

    def _pin_retries(self) -> int | None:
        """The count as last read. Refreshed by :meth:`_connect_sync`, including via `_reopen`."""
        return self._retries

    def _read_pin_retries(self) -> int | None:
        """Remaining PIN attempts, or ``None``. A read -- it needs no PIN and no touch."""
        if self._ctap is None or not self.supports("clientPin"):
            return None
        try:
            from fido2.ctap2 import ClientPin

            return int(ClientPin(self._ctap).get_pin_retries()[0])
        except Exception:  # noqa: BLE001 - informational only
            return None

    async def get(self, key: str) -> Any:
        if key not in self._values:
            raise NotSupported(key)
        return self._values[key]

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        wanted = set(keys)
        return {k: v for k, v in self._values.items() if k in wanted}

    def advisories(self) -> dict[str, Advisory]:
        return dict(self._advisories)

    # ------------------------------------------------------------------ passkeys

    def _passkey_capabilities(self) -> list[Any]:
        """The Passkeys tab, or nothing at all on a key that cannot list its credentials."""
        if not CRED.supported(self._info):
            return []
        if self._passkeys is None:
            return C.passkeys_locked()
        return C.build_passkeys(
            [(p.key, p.label()) for p in self._passkeys.items],
            used=self._passkeys.used,
            capacity=self._passkeys.capacity,
            can_rename=CRED.rename_supported(self._info),
        )

    def _passkey_values(self) -> dict[str, Any]:
        if self._passkeys is None:
            return {}
        out: dict[str, Any] = {C.PASSKEYS_STATUS_KEY: f"{self._passkeys.used} passkey(s)"}
        out.update({C.passkey_key(p.key): p.user or p.site for p in self._passkeys.items})
        return out

    def _read_passkeys(self, pin: str) -> str:
        self._passkeys = CRED.read(self._ctap, pin)
        self._rebuild()
        count = self._passkeys.used
        return f"{count} passkey{'s' if count != 1 else ''} on this key." if count else (
            "This key stores no passkeys."
        )

    # ------------------------------------------------------------------ hooks for subclasses

    def extra_capabilities(self) -> list[Any]:
        """Capabilities a vendor module adds on top of the standard ones. Empty here."""
        return []

    def extra_values(self) -> dict[str, Any]:
        """Values for :meth:`extra_capabilities`. Empty here."""
        return {}

    def handle_set(self, key: str, value: Any) -> Any:
        """A vendor module's writes. Raise :class:`NotSupported` for keys it does not own."""
        raise NotSupported(key)

    # ------------------------------------------------------------------ writing

    async def set(self, key: str, value: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._set_sync, key, value)

    def _set_sync(self, key: str, value: Any) -> Any:
        if self._dev is None:
            raise Unreachable("not connected")
        if key == C.REFRESH_KEY:
            self._reopen()
            return "Re-read from the key."
        if self._ctap is None:
            raise NotSupported("this key does not speak CTAP2")

        if key == C.SET_PIN_KEY:
            return self._set_pin(value)
        if key == C.TEST_KEY:
            return self._test(str(value or ""))
        if key == C.RESET_KEY:
            return self._reset()
        if key == C.PASSKEYS_SHOW_KEY:
            return self._read_passkeys(str(value or ""))
        if key.startswith(C.PASSKEY_DELETE_PREFIX):
            result = CRED.delete(
                self._ctap, str(value or ""), key.removeprefix(C.PASSKEY_DELETE_PREFIX)
            )
            self._passkeys = CRED.read(self._ctap, str(value or ""))
            self._rebuild()
            return result
        if key.startswith(C.PASSKEY_RENAME_PREFIX):
            name, pin = (value if isinstance(value, tuple) else (value, ""))
            result = CRED.rename(
                self._ctap, str(pin or ""), key.removeprefix(C.PASSKEY_RENAME_PREFIX), str(name)
            )
            self._passkeys = CRED.read(self._ctap, str(pin or ""))
            self._rebuild()
            return result
        if key in (C.MIN_PIN_KEY, C.ALWAYS_UV_KEY, C.FORCE_PIN_CHANGE_KEY,
                   C.ENTERPRISE_ATTESTATION_KEY):
            # A prompted action hands over the PIN; a RANGE hands over its number and the PIN
            # arrives the same way, so both are unpacked here.
            return self._configure(key, value)
        return self.handle_set(key, value)

    def _client_pin(self) -> Any:
        from fido2.ctap2 import ClientPin

        return ClientPin(self._ctap)

    def _set_pin(self, value: Any) -> str:
        """*value* is ``(current, new)`` from the shell's PIN-change prompt.

        The prompt has already checked that the two new entries match and that the new PIN is long
        enough, which is the point of asking there rather than here: a mistyped PIN written to a
        security key is recoverable only by a reset.
        """
        current, new = ("", "")
        if isinstance(value, (tuple, list)) and len(value) == 2:
            current, new = str(value[0] or ""), str(value[1] or "")
        if not new:
            raise DeviceError("No new PIN was given.")
        # Read before the write: re-opening the key afterwards reports a PIN either way, so
        # asking then would call a first-time set a "change".
        had_pin = self.supports("clientPin")
        client = self._client_pin()
        try:
            if had_pin:
                client.change_pin(current, new)
            else:
                client.set_pin(new)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(self._explain(exc)) from exc
        self._reopen()
        return (
            "The PIN was changed. The attempt counter is back to full."
            if had_pin
            else "A PIN was set on the key."
        )

    def _configure(self, key: str, value: Any) -> Any:
        from fido2.ctap2 import ClientPin
        from fido2.ctap2.config import Config

        # A prompted ACTION hands over the PIN itself; a prompted slider hands over
        # ``(number, pin)`` so its number is not lost.
        setting: Any = None
        if isinstance(value, tuple) and len(value) == 2:
            setting, pin = value[0], str(value[1] or "")
        else:
            pin = str(value or "")
        if not pin:
            raise DeviceError("The key's PIN is needed for that.")
        client = self._client_pin()
        try:
            # Authenticator configuration needs a token minted for exactly that permission.
            token = client.get_pin_token(pin, ClientPin.PERMISSION.AUTHENTICATOR_CFG)
            config = Config(self._ctap, client.protocol, token)
            if key == C.MIN_PIN_KEY:
                config.set_min_pin_length(min_pin_length=int(setting))
            elif key == C.ALWAYS_UV_KEY:
                config.toggle_always_uv()
            elif key == C.FORCE_PIN_CHANGE_KEY:
                config.set_min_pin_length(force_change_pin=True)
            else:
                config.enable_enterprise_attestation()
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(self._explain(exc)) from exc
        self._reopen()
        return self._values.get(key)

    def _test(self, pin: str = "") -> str:
        """A throwaway credential that proves the touch, and the PIN if one is set.

        Nothing is stored on the key: the credential is not discoverable, and the result is
        discarded. Straight from the KCM, where it exists so a user can confirm a key works
        without risking anything.

        Once a key has a PIN, ``makeCredential`` **requires** a pin/uv auth token -- omitting it
        answers ``PUAT_REQUIRED``. So the PIN is demanded up front rather than sending a request
        that cannot succeed.
        """
        import os

        from fido2.ctap2 import ClientPin

        rp_id = "hardware-ui.invalid"
        # The signature covers the *same* client data the request carries. Authenticating a
        # different random hash produces a token the key rejects.
        client_data_hash = os.urandom(32)
        pin_uv_param = pin_uv_protocol = None

        if self.supports("clientPin"):
            if not pin:
                raise DeviceError(
                    "This key has a PIN, so testing it needs that PIN. Enter it above and try "
                    "again."
                )
            client = ClientPin(self._ctap)
            try:
                token = client.get_pin_token(
                    pin, ClientPin.PERMISSION.MAKE_CREDENTIAL, rp_id
                )
            except Exception as exc:  # noqa: BLE001
                raise DeviceError(self._explain(exc)) from exc
            pin_uv_param = client.protocol.authenticate(token, client_data_hash)
            pin_uv_protocol = client.protocol.VERSION

        try:
            self._ctap.make_credential(
                client_data_hash=client_data_hash,
                rp={"id": rp_id, "name": "hardware-ui self test"},
                user={"id": b"\x00", "name": "test"},
                key_params=[{"type": "public-key", "alg": -7}],
                options={"rk": False},
                pin_uv_param=pin_uv_param,
                pin_uv_protocol=pin_uv_protocol,
            )
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(self._explain(exc)) from exc
        return (
            "The key answered and the touch was accepted"
            + (", and the PIN was correct." if pin_uv_param is not None else ".")
            + " Nothing was stored on it."
        )

    def _reset(self) -> str:
        """Erase everything. Irreversible, and most keys only accept it just after being plugged
        in -- which is a property of the key, so the message says so rather than pretending."""
        try:
            self._ctap.reset()
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(self._explain(exc)) from exc
        self._reopen()
        return "The key was reset. Every credential and the PIN have been erased."

    def _reopen(self) -> None:
        """Re-read the key. Its shape changes: setting a PIN flips ``clientPin``, a reset clears
        it, and both change what the Configuration tab should offer."""
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                log.debug("close before re-open failed", exc_info=True)
        self._connect_sync()

    def _explain(self, exc: Exception) -> str:
        """Turn a CTAP error into something a user can act on.

        The retry count matters most: a wrong PIN is recoverable until the attempts run out, and
        after that the key can only be reset, which wipes it.
        """
        from fido2.ctap import CtapError

        if isinstance(exc, CtapError):
            code = exc.code
            if code == CtapError.ERR.PUAT_REQUIRED:
                return (
                    "This key requires its PIN for that. Enter the PIN above and try again."
                )
            if code == CtapError.ERR.PIN_AUTH_INVALID:
                return "The key rejected the PIN authentication. Re-enter the PIN and try again."
            if code == CtapError.ERR.PIN_INVALID:
                retries = self._pin_retries()
                left = f" {retries} attempt{'s' if retries != 1 else ''} left." if retries else ""
                return f"That PIN is wrong.{left}"
            if code == CtapError.ERR.PIN_BLOCKED:
                return (
                    "The PIN is blocked. The key can only be used again after a factory reset, "
                    "which erases every credential on it."
                )
            if code == CtapError.ERR.PIN_AUTH_BLOCKED:
                return "Too many wrong PINs in a row. Unplug the key and plug it back in."
            if code == CtapError.ERR.PIN_POLICY_VIOLATION:
                return "That PIN does not meet the key's requirements — it is too short or reused."
            if code == CtapError.ERR.ACTION_TIMEOUT:
                return "The key was not touched in time. Try again and press it when it blinks."
            if code == CtapError.ERR.NOT_ALLOWED:
                return (
                    "The key refused. A factory reset is usually only accepted within a few "
                    "seconds of plugging the key in."
                )
            if code == CtapError.ERR.OPERATION_DENIED:
                return "Cancelled at the key."
            return f"The key reported {exc.code.name}."
        return str(exc)


__all__ = ["Fido2SecurityKey"]
