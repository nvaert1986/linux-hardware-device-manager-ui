"""A Logitech peripheral, or the receiver it is paired to, as the shell sees it.

Thin on purpose. The HID++ protocol, the per-model feature tables and the setting definitions are
Solaar's, vendored under ``hardware_ui/third_party`` (see ``tools/vendor_solaar.py``); this file is
the adapter between that blocking library and the shell's async
:class:`~hardware_ui.core.device.Device`.

**Each device is its own entry, and so is the receiver.** ``hid-logitech-dj`` gives every paired
device its own ``/dev/hidraw*`` node, so a mouse and a keyboard behind one receiver already arrive
as two devices with their own categories and icons — no shell change was needed to get the layout
right. The receiver appears alongside them because it has firmware and pairing slots of its own,
and pairing is precisely the thing that cannot be done from the peripheral.

**Resolution goes through the library, not through sysfs.** A paired device is reached by
iterating its receiver, not by opening its own node, so the node this module was handed has to be
matched back to a ``(receiver, index)`` pair. ``find_paired_node`` already answers exactly that
question and is upstream's own logic, so it is asked rather than reimplemented with ``HID_PHYS``
string surgery.

**Nothing is read to build the page.** Solaar's settings are lazy — ``read()`` talks to the device
— so the controls are constructed from the feature list and the values arrive afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
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
from hardware_ui.core.connection import ConnectionLabel

from . import capabilities as C
from . import children, labels, store
from .bootstrap import VendorMissing, ensure_path, vendored

log = logging.getLogger(__name__)

#: Third-party packages the vendored library needs, and what to install for each. Vendoring the
#: code does not vendor its dependencies: ``hidapi/udev_impl`` enumerates through ``pyudev`` and
#: ``solaar/configuration`` persists through ``PyYAML``.
PACKAGES = {
    "pyudev": "dev-python/pyudev",
    "yaml": "dev-python/pyyaml (PyYAML)",
}


def _as_dependency_error(exc: ImportError) -> DependencyMissing:
    """Turn a missing import into something a user can act on.

    Without this the shell shows a bare ``ImportError: No module named 'yaml'`` on Connect, which
    reads as a bug in this application rather than a package that was never installed.
    """
    missing = (getattr(exc, "name", "") or "").split(".")[0]
    package = PACKAGES.get(missing)
    if package:
        return DependencyMissing(
            f"Logitech devices need {package}, which is not installed. "
            f"The rest of the application is unaffected."
        )
    return DependencyMissing(f"Logitech support is missing a dependency: {exc}")


#: Settings an active on-board profile governs, in Solaar's own words: it "controls report rate,
#: sensitivity, and button actions". A live write to one of these may simply not take effect while
#: a profile is enabled -- upstream says as much in the DPI setting's description: "May need
#: Onboard Profiles set to Disable to be effective."
PROFILE_GOVERNED = ("dpi", "dpi_extended", "report_rate", "report_rate_extended",
                    "reprogrammable-keys", "persistent-remappable-keys")

NOTE_PROFILE_ACTIVE = (
    "An on-board profile is active on this device, and the profile controls report rate, "
    "sensitivity and button actions. Changing this here may have no effect until Onboard Profiles "
    "is set to Disabled — the profile is stored on the device and keeps overriding live changes."
)


#: How long a pairing scan stays open. Upstream's own value.
PAIR_TIMEOUT = 30.0

#: The lock-open notification can arrive slightly after the scan starts, so the loop waits this
#: long before believing an unopened lock. Upstream calls it "patience".
PAIR_PATIENCE = 5.0


class LogitechDevice(Device):
    """One Logitech peripheral or receiver."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._handle: Any = None
        """A ``logitech_receiver`` Device or Receiver."""
        self._receiver: Any = None
        """Set when this is a peripheral reached through a receiver."""
        self._is_receiver = False
        self._capabilities = CapabilitySet([])
        self._values: dict[str, Any] = {}
        self._settings: dict[str, Any] = {}
        self._advisories: dict[str, Advisory] = {}
        self._lock = asyncio.Lock()

    @property
    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            ensure_path()
        except VendorMissing as exc:
            raise DependencyMissing(str(exc)) from exc

        try:
            # Before anything reads it: the library caches the config on first use, so a later
            # redirect would be ignored and settings would persist into Solaar's file.
            store.redirect()
            handle, receiver, is_receiver = self._resolve()
        except ImportError as exc:
            # Vendoring the code did not vendor its dependencies.
            raise _as_dependency_error(exc) from exc
        except OSError as exc:
            raise Unreachable(f"cannot open {self.info.path}: {exc}") from exc
        if handle is None:
            raise Unreachable(
                "this device is not answering HID++ — unplug the receiver and plug it back in"
            )

        self._handle, self._receiver, self._is_receiver = handle, receiver, is_receiver
        self._announce_software()
        try:
            self._describe()
        except Exception:
            with contextlib.suppress(Exception):
                handle.close()
            self._handle = None
            raise

    def _announce_software(self) -> None:
        """Tell the receiver a configuration program is present, before asking it anything.

        Measured on a Bolt receiver with an MX Master 3S: without this the receiver answers a
        device ping with HID++ error **0x04 CONNECT_FAIL**, so every paired device reads as offline
        with zero settings -- while ``count()`` cheerfully reports two devices connected. Setting
        the flag turns the same ping into a reply and the same device into eleven settings.

        Solaar does this in ``enable_connection_notifications``, which sets SOFTWARE_PRESENT
        alongside WIRELESS. It is not optional and it is not about notifications: the receiver
        simply will not proxy HID++ to a device until a host has announced itself.

        The receiver, whether it is what was opened or the parent of what was opened. Failure is
        logged rather than raised -- an older receiver that does not implement the register still
        has settings worth showing.
        """
        receiver = self._handle if self._is_receiver else self._receiver
        if receiver is None:
            return
        try:
            flags = receiver.enable_connection_notifications()
            log.debug("%s: notification flags now %s", self.info.name, flags)
        except Exception as exc:  # noqa: BLE001
            log.info("%s: could not announce software presence: %s", self.info.name, exc)

    def _resolve(self) -> tuple[Any, Any, bool]:
        """Find the library object for the node this module was handed.

        Three shapes, in the order they are cheapest to rule out: the node is a receiver; the node
        is a device plugged in directly by cable or Bluetooth; or the node belongs to a device
        paired *through* a receiver, which is the common case and the only one that needs a search.
        """
        base, device_module, receiver_module = vendored()
        from hidapi.udev_impl import find_paired_node

        # A child from `children.discover`: it has no node of its own, so the slot is the address.
        # Checked before anything else, because its `path` is the *receiver's* and would otherwise
        # match the receiver and hand back the wrong device entirely.
        slot = children.slot_of(self.info)
        if slot is not None:
            return self._resolve_slot(base, receiver_module, slot)

        wanted = self.info.path or ""
        # Discovery hands over one node per USB device, chosen by its own heuristic, and it is
        # usually **not** the HID++ one: measured on a Bolt receiver, discovery offers
        # /dev/hidraw1 (no report ids at all) while HID++ 0x10/0x11 lives on /dev/hidraw3.
        # Demanding an exact match reported "not answering HID++" for a receiver that answers
        # perfectly -- the same fallback the Poly module needed, for the same reason.
        family = _usb_parent(wanted)
        receivers: list[Any] = []
        sibling: Any = None

        for info in base.receivers_and_devices():
            if info.isDevice:
                if info.path == wanted:
                    return device_module.create_device(base, info), None, False
                continue
            made = receiver_module.create_receiver(base, info)
            if made is None:
                continue
            if info.path == wanted:
                return made, None, True
            receivers.append(made)
            if sibling is None and family is not None and _usb_parent(info.path) == family:
                sibling = made

        # A paired device, *before* falling back to the sibling: with hid-logitech-dj bound, a
        # paired device's node sits under the same USB device as the receiver, so the sibling test
        # would happily claim the receiver for a path that is really a mouse.
        for receiver in receivers:
            for paired in receiver:
                if paired is None:
                    continue
                with contextlib.suppress(Exception):
                    if find_paired_node(receiver.path, paired.number, 0) == wanted:
                        _close_all(receivers, keep=receiver)
                        return paired, receiver, False

        if sibling is not None:
            log.info("%s is not the HID++ node; using %s", wanted, sibling.path)
            _close_all(receivers, keep=sibling)
            return sibling, None, True

        _close_all(receivers)
        return None, None, False

    def _resolve_slot(self, base: Any, receiver_module: Any, slot: int) -> tuple[Any, Any, bool]:
        """The paired device in *slot*, reached through its receiver.

        The receiver is found by USB device rather than by path, for the same reason ``_resolve``
        does: the node discovery recorded is not necessarily the HID++ one, and node numbers change
        on every reseat while the slot does not.
        """
        family = _usb_parent(self.info.properties.get(children.RECEIVER_PATH, "")
                             or self.info.path or "")
        for info in base.receivers_and_devices():
            if info.isDevice:
                continue
            if family is not None and _usb_parent(info.path) != family:
                continue
            receiver = receiver_module.create_receiver(base, info)
            if receiver is None:
                continue
            paired = next(
                (d for d in receiver if d is not None and int(d.number) == slot), None
            )
            if paired is not None:
                return paired, receiver, False
            with contextlib.suppress(Exception):
                receiver.close()
        return None, None, False

    async def disconnect(self) -> None:
        handle, self._handle = self._handle, None
        receiver, self._receiver = self._receiver, None
        for closeable in (handle, receiver):
            if closeable is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(closeable.close)

    # ------------------------------------------------------------------ the page

    def _describe(self) -> None:
        handle = self._require()
        self._settings = {}
        pairing: dict[str, Any] = {}

        if self._is_receiver:
            pairing = self._pairing_state(handle)
        else:
            for setting in getattr(handle, "settings", ()) or ():
                name = getattr(setting, "name", "")
                if name:
                    self._settings[name] = setting

        identity = self._identity(handle)
        self._values.update({f"{C.INFO_PREFIX}{k}": v for k, v in identity.items()})
        self._values.update({f"info.{k}": v for k, v in pairing.get("display", {}).items()})

        self._capabilities = C.build(
            list(self._settings.values()),
            identity=identity,
            online=bool(getattr(handle, "online", True)),
            battery=self._battery() is not None,
            pairing=pairing,
        )
        self._read_values()
        self._warn_about_profiles()
        self._bump_capabilities()

    def _warn_about_profiles(self) -> None:
        """Say so when an on-board profile is in charge, rather than letting writes look broken.

        The failure this prevents is the nastiest kind: the write is accepted, the read-back
        disagrees, and the control appears to revert on its own. That is not a bug in this
        application -- the profile stored on the device is overriding it -- but it is
        indistinguishable from one unless the page says which.

        Two cheap questions, both Solaar's: does the device have profiles at all
        (``get_profile_headers``), and is profile mode on (function ``0x20`` answering ``0x01``).
        A device without the feature never reaches the second, which is every device tested here.
        """
        handle = self._handle
        if handle is None or self._is_receiver:
            return
        try:
            from logitech_receiver import hidpp20
            from logitech_receiver.hidpp20_constants import SupportedFeature

            if not hidpp20.OnboardProfiles.get_profile_headers(handle):
                return
            enabled = handle.feature_request(SupportedFeature.ONBOARD_PROFILES, 0x20)
            if not enabled or enabled[0] != 0x01:
                return
        except Exception as exc:  # noqa: BLE001 - a device that will not answer simply has none
            log.debug("%s: no on-board profile state: %s", self.info.name, exc)
            return

        governed = {
            c.key for c in self._capabilities
            if (C.setting_name(c.key) or (C.map_entry(c.key) or ("", 0))[0]) in PROFILE_GOVERNED
        }
        for key in governed:
            self._advisories[key] = Advisory(NOTE_PROFILE_ACTIVE)
        if governed:
            log.info("%s: an on-board profile is active; %d settings may be overridden by it",
                     self.info.name, len(governed))

    def _identity(self, handle: Any) -> dict[str, str]:
        """Only what the object actually answers; a blank row is worse than no row."""
        out: dict[str, str] = {}

        def put(key: str, value: Any) -> None:
            if value not in (None, "", 0):
                out[key] = str(value)

        put("name", getattr(handle, "name", ""))
        put("codename", getattr(handle, "codename", ""))
        kind = getattr(handle, "kind", None)
        put("kind", "Receiver" if self._is_receiver else (kind or ""))
        with contextlib.suppress(Exception):
            put("serial", _readable_serial(handle.serial, self._is_receiver
                                           and getattr(handle, "receiver_kind", "") == "bolt"))
        with contextlib.suppress(Exception):
            put("unit_id", getattr(handle, "unitId", ""))
        with contextlib.suppress(Exception):
            firmware = handle.firmware or ()
            versions = [f"{f.kind}: {f.version}" for f in firmware if getattr(f, "version", "")]
            put("firmware", ", ".join(versions))
        put("wpid", getattr(handle, "wpid", ""))
        if self._receiver is not None:
            put("receiver", getattr(self._receiver, "name", ""))
        return out

    def _pairing_state(self, receiver: Any) -> dict[str, Any]:
        """A receiver's slots: what is in them, and how many are free."""
        devices: dict[int, str] = {}
        for paired in receiver:
            if paired is not None:
                devices[int(paired.number)] = getattr(paired, "name", f"device {paired.number}")

        total = int(getattr(receiver, "max_devices", 0) or 0)
        remaining = None
        with contextlib.suppress(Exception):
            remaining = receiver.remaining_pairings()
            if remaining is not None and remaining < 0:
                remaining = None            # the receiver does not limit pairings

        display = {"slots": f"{len(devices)} of {total}" if total else str(len(devices))}
        if remaining is not None:
            display["remaining_pairings"] = str(remaining)

        return {
            "used": len(devices),
            "total": total,
            "remaining": remaining,
            "devices": devices,
            "display": display,
            "may_unpair": bool(getattr(receiver, "may_unpair", False)),
            "kind": getattr(receiver, "receiver_kind", ""),
        }

    def _battery(self) -> int | None:
        handle = self._handle
        if handle is None or self._is_receiver:
            return None
        try:
            reading = handle.battery()
        except Exception:  # noqa: BLE001 - a device that will not answer simply has no meter
            return None
        level = getattr(reading, "level", None) if reading is not None else None
        if isinstance(level, (int, float)) and not isinstance(level, bool):
            return max(0, min(100, int(level)))
        return None

    def _read_values(self) -> None:
        """Read every setting once. Failures are per-setting and never fail the page."""
        for name, setting in self._settings.items():
            try:
                value = setting.read(cached=False)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: cannot read: %s", name, exc)
                continue
            if value is None:
                continue
            # A per-key map reads as ``{key: value}``, and the page has a row per key, so the
            # values have to be split the same way or every one of those rows shows nothing.
            if isinstance(value, dict) and getattr(
                getattr(setting, "kind", None), "name", ""
            ) in ("MAP_CHOICE", "MULTIPLE_TOGGLE"):
                for key, inner in value.items():
                    self._values[C.map_key(name, key)] = _plain(inner)
                continue
            self._values[C.setting_key(name)] = _plain(value)
        battery = self._battery()
        if battery is not None:
            self._values[C.BATTERY_KEY] = battery

    # ------------------------------------------------------------------ reading

    def _require(self) -> Any:
        if self._handle is None:
            raise Unreachable("not connected")
        return self._handle

    async def get(self, key: str) -> Any:
        return self._values.get(key)

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        return {k: self._values[k] for k in keys if k in self._values}

    async def refresh(self) -> dict[str, Any]:
        async with self._lock:
            await asyncio.to_thread(self._read_values)
        return dict(self._values)

    def advisories(self) -> dict[str, Advisory]:
        return dict(self._advisories)

    # ------------------------------------------------------------------ writing

    async def set(self, key: str, value: Any) -> Any:
        cap = self._capabilities.by_key(key)
        timeout = (cap.timeout if cap is not None and cap.timeout else 0.0) or 30.0
        async with self._lock:
            return await asyncio.wait_for(
                asyncio.to_thread(self._set_sync, key, value), timeout + 5
            )

    def _set_sync(self, key: str, value: Any) -> Any:
        if key == C.PAIR_KEY:
            return self._pair()
        index = C.unpair_index(key)
        if index is not None:
            return self._unpair(index)

        entry = C.map_entry(key)
        if entry is not None:
            return self._set_map_entry(key, *entry, value)

        name = C.setting_name(key)
        setting = self._settings.get(name)
        if setting is None:
            raise DeviceError(f"{key} is not a setting on this device")
        try:
            # save=True writes it to our own store as well, which is what re-applies it when the
            # device reconnects -- most HID++ settings do not survive that in hardware.
            landed = setting.write(value, save=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, who asked for the change
            raise DeviceError(f"{labels.label_for(setting)}: {exc}") from exc
        if landed is None:
            raise DeviceError(f"{labels.label_for(setting)}: the device did not accept that")
        self._values[key] = _plain(landed)
        return self._values[key]

    def _set_map_entry(self, key: str, name: str, map_key: int, value: Any) -> Any:
        """Write one entry of a per-key map.

        ``write_key_value`` is the library's own per-key accessor, so only the key being changed is
        sent. Writing the whole map back would rewrite every other button to whatever this page
        last read, which is how a stale row silently undoes a change made somewhere else.
        """
        setting = self._settings.get(name)
        if setting is None:
            raise DeviceError(f"{name} is not a setting on this device")
        try:
            landed = setting.write_key_value(map_key, value, save=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, who asked for the change
            raise DeviceError(f"{labels.label_for(setting)}: {exc}") from exc
        if landed is None:
            raise DeviceError(f"{labels.label_for(setting)}: the device did not accept that")
        # write_key_value may hand back the whole map; the row wants its own value.
        self._values[key] = _plain(value if isinstance(landed, dict) else landed)
        return self._values[key]

    # ------------------------------------------------------------------ pairing

    def _pair(self) -> str:
        """Open the receiver's pairing lock and wait for a device to appear.

        Ported from Solaar's ``cli/pair.py``. Two details there are load-bearing and easy to lose:
        the WIRELESS notification flag has to be on for the receiver to report the new device, and
        it is **restored** afterwards only if it was off to begin with -- clearing a flag another
        running Solaar had set would stop that one working.
        """
        receiver = self._require()
        if not self._is_receiver:
            raise NotSupported("pairing is done on the receiver, not on the device")
        base, _device_module, _receiver_module = vendored()
        from logitech_receiver import hidpp10_constants
        from logitech_receiver.hidpp10 import Hidpp10

        hidpp10 = Hidpp10()
        wireless = hidpp10_constants.NotificationFlag.WIRELESS
        previous = hidpp10.get_notification_flags(receiver)
        if not (previous & wireless):
            hidpp10.set_notification_flags(receiver, previous | wireless)

        known = [d.number for d in receiver if d is not None]

        class _Hooked(int):
            def notifications_hook(self, notification: Any) -> None:
                nonlocal known
                if notification.devnumber == 0xFF:
                    from logitech_receiver import notifications

                    notifications.process(receiver, notification)
                elif notification.sub_id == 0x41 and known is not None:
                    seen, known = known, None       # one connection notification only
                    if notification.devnumber not in seen:
                        receiver.pairing.new_device = receiver.register_new_device(
                            notification.devnumber, notification
                        )

        receiver.handle = _Hooked(receiver.handle)
        try:
            if getattr(receiver, "receiver_kind", "") == "bolt":
                self._pair_bolt(base, receiver)
            else:
                self.interaction.message(
                    "Pairing",
                    "Turn your device on, or press and release its channel button.\n\n"
                    f"Waiting up to {int(PAIR_TIMEOUT)} seconds.",
                )
                receiver.set_lock(False, timeout=int(PAIR_TIMEOUT))
                self._pump(base, receiver, until=lambda: not receiver.pairing.lock_open)
        finally:
            self.interaction.close()
            if not (previous & wireless):
                with contextlib.suppress(Exception):
                    hidpp10.set_notification_flags(receiver, previous)

        paired = receiver.pairing.new_device
        if paired is not None:
            self._describe()
            return f"Paired {paired.name}."
        error = receiver.pairing.error
        raise DeviceError(f"Pairing failed: {error}" if error else
                          "No device appeared. Turn it on, or press its channel button, and retry.")

    def _pump(self, base: Any, receiver: Any, *, until, done=None,
              patience: float = PAIR_PATIENCE) -> None:
        """Read notifications until the step finishes, or the user cancels.

        *until* is the step's own end condition; *done* is an early exit for when the thing being
        waited for has already arrived. The lock-open and discovery notifications can both land
        slightly after the request that caused them, so ``patience`` keeps the loop alive across
        that gap -- upstream's own word for it, and its own reason.
        """
        started = _now()
        while not until() or _now() - started < patience:
            if self.interaction.cancelled():
                raise DeviceError("Pairing cancelled.")
            if done is not None and done():
                return
            raw = base.read(receiver.handle)
            notification = base.make_notification(*raw) if raw else None
            if notification:
                receiver.handle.notifications_hook(notification)

    def _pair_bolt(self, base: Any, receiver: Any) -> None:
        """A Bolt receiver, which authenticates before it will pair.

        Ported from Solaar's ``cli/pair.py``. Three steps, and the middle one is why this needed a
        way to talk to the user mid-operation at all: the receiver produces a passkey that has to
        be entered **on the device being paired**, while the lock is still open.

        Two forms, and the difference is a bit in ``authentication``: a keyboard types digits and
        presses Enter; a mouse is told a left/right pattern to click, because it has no digits. The
        pattern is the passkey as ten bits, most significant first.
        """
        from logitech_receiver import hidpp10_constants

        pairing = receiver.pairing
        self.interaction.message(
            "Pairing",
            "Long-press the pairing button on the device you want to add.\n\n"
            f"Searching for up to {int(PAIR_TIMEOUT)} seconds.",
        )
        receiver.discover(timeout=int(PAIR_TIMEOUT))
        # Bolt announces discovery slightly late, so the loop is patient before believing that
        # nothing is there -- and it stops the moment a device is fully described rather than
        # waiting out the timeout.
        self._pump(
            base, receiver,
            until=lambda: not pairing.discovering,
            done=lambda: bool(
                pairing.device_address and pairing.device_authentication and pairing.device_name
            ),
        )

        if not (pairing.device_address and pairing.device_name):
            raise DeviceError("No device answered. Long-press its pairing button and try again.")

        name = pairing.device_name
        authentication = pairing.device_authentication
        self.interaction.message("Pairing", f"Found {name}. Authenticating\u2026")
        receiver.pair_device(
            address=pairing.device_address,
            authentication=authentication,
            # Entropy is the passkey length: a keyboard can type 20 bits, a mouse clicks 10.
            entropy=20 if pairing.device_kind == hidpp10_constants.DEVICE_KIND.keyboard else 10,
        )

        self._pump(
            base, receiver,
            until=lambda: not pairing.lock_open,
            done=lambda: bool(pairing.device_passkey),
        )
        self.interaction.message(
            "Pairing", _passkey_instructions(name, pairing.device_passkey, authentication)
        )
        # No patience here: the lock is already open, so its closing is the real completion signal.
        self._pump(base, receiver, until=lambda: not pairing.lock_open, patience=0.0)

    def _unpair(self, index: int) -> str:
        receiver = self._require()
        if not self._is_receiver:
            raise NotSupported("unpairing is done on the receiver")
        target = next((d for d in receiver if d is not None and int(d.number) == index), None)
        if target is None:
            raise DeviceError("That slot is already empty.")
        name = getattr(target, "name", f"device {index}")
        try:
            # Read the identity first: after unpairing there is nothing left to ask, which is why
            # upstream grabs it before the call rather than after.
            receiver._unpair_device(target.number, True)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"Could not unpair {name}: {exc}") from exc
        self._describe()
        return f"Unpaired {name}."

    # ------------------------------------------------------------------ where this device is

    def connection_label(self) -> ConnectionLabel:
        return ConnectionLabel(self._route(), self._identifier())

    def _route(self) -> str:
        if self._is_receiver:
            return "USB"
        receiver = self._receiver
        if receiver is None:
            return "USB"
        name = getattr(receiver, "name", "")
        return f"via {name}" if name else "wireless"

    def _identifier(self) -> str:
        return str(self._values.get(f"{C.INFO_PREFIX}serial") or "")


def _close_all(receivers: list[Any], keep: Any = None) -> None:
    """Close every receiver opened while searching, except the one being returned.

    The bug this exists to prevent: the search loop used to close each receiver as it finished
    with it, including the one it was about to hand back. The page then read "0 of 6" slots from a
    closed handle, while the log above it cheerfully announced both paired devices.
    """
    for receiver in receivers:
        if receiver is keep:
            continue
        with contextlib.suppress(Exception):
            receiver.close()


def _passkey_instructions(name: str, passkey: Any, authentication: int) -> str:
    """What the user must do on the device being paired.

    Bit 0 of ``authentication`` says the device can type: a keyboard is given digits, anything else
    is given the same number as a left/right click pattern, ten bits most significant first. That
    encoding is upstream's and is not obvious -- a mouse has no keys, so the passkey is entered by
    clicking it out.
    """
    if authentication & 0x01:
        return (
            f"On {name}, type this passkey and press Enter:\n\n"
            f"        {passkey}\n\n"
            "The window stays open until the device confirms."
        )
    pattern = ", ".join(
        "right" if bit == "1" else "left" for bit in f"{int(passkey):010b}"
    )
    return (
        f"On {name}, click this pattern:\n\n        {pattern}\n\n"
        "then press the left and right buttons together."
    )


def _usb_parent(node: str) -> str | None:
    """The USB device a hidraw node belongs to, as a sysfs path.

    Used to decide that two different ``/dev/hidraw*`` nodes are the same piece of hardware. A
    receiver presents several interfaces and only one of them carries HID++; which one discovery
    offers is not something this module gets to choose.
    """
    if not node:
        return None
    name = node.rsplit("/", 1)[-1]
    try:
        resolved = Path(f"/sys/class/hidraw/{name}/device").resolve()
    except OSError:
        return None
    for parent in [resolved, *resolved.parents]:
        if (parent / "idVendor").is_file():
            return str(parent)
    return None


def _readable_serial(serial: Any, is_bolt: bool) -> Any:
    """Undo the double encoding a Bolt receiver's serial arrives with.

    ``extract_serial`` hexlifies the raw register bytes, which is right for a Unifying receiver
    whose serial is four binary bytes. A Bolt receiver's ``BOLT_UNIQUE_ID`` register already
    contains printable ASCII, so hexlifying it yields 32 characters that are the hex *of the text* —
    measured on a Bolt receiver: ``30384334…`` for the serial ``08C4FD8D6992BC2C``. Shown raw it is
    twice as long as it should be and matches nothing printed on the hardware.

    Narrowed to Bolt receivers and to strings that really are hex of printable ASCII, so a binary
    serial that happens to decode is never mangled. An upstream fix belongs in ``extract_serial``;
    this only corrects what is displayed.
    """
    if not is_bolt or not isinstance(serial, str) or len(serial) != 32:
        return serial
    try:
        decoded = bytes.fromhex(serial).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return serial
    return decoded if decoded.isprintable() else serial


def _plain(value: Any) -> Any:
    """Strip Solaar's ``NamedInt`` down to something the shell can compare and store.

    A ``NamedInt`` is an ``int`` that prints as a name. Left as-is it would round-trip through the
    UI and stop comparing equal to the plain int a control hands back.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _now() -> float:
    import time

    return time.monotonic()


__all__ = ["LogitechDevice"]
