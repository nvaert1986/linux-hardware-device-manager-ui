"""The Poly adapter: capability keys in, Deckard exchanges out.

The session underneath (``protocol/session.py``) is the reference implementation's, verified on a
live V4310 over both Bluetooth and the BT700 dongle. This file does not reimplement any of it --
it wraps it. In particular these rules live down there and must not be duplicated or second-guessed
up here:

* a write is confirmed by the device's own change **EVENT**, which arrives *before* the ack;
  re-reading immediately returns the old value and produced a visible "setting reverts, then
  applies my previous choice" bug
* write-only actions are refused before anything is sent
* every BladeRunner address needs its own ``PROTOCOL_VERSION`` handshake
* the event backlog is bounded, because the headset reports button presses that are not settings

What this file adds is the parts the shell needs: one lock so a single thread owns the transport,
the capability mapping, and a change stream so a button pressed on the headset reaches the screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from hardware_ui.core import (
    Advisory,
    CapabilitySet,
    CapabilityValue,
    ConnectionLabel,
    Device,
    DeviceError,
    DeviceInfo,
    NotSupported,
    Transport,
    Unreachable,
)

from . import capabilities as C
from .protocol import catalogue as cat
from .protocol import ids
from .protocol.hid import HidTransport, HidTransportError, find_devices
from .protocol.rfcomm import RfcommTransport, TransportError
from .protocol.session import (
    CommandUnsupported,
    DeckardError,
    PolyHeadset,
    decode_string,
)

log = logging.getLogger(__name__)

MODULE_ID = "poly_headsets"

EVENT_DRAIN_INTERVAL = 1.0
"""How often unsolicited events are collected. This **sends nothing** -- it is a non-blocking read
of what the device already pushed. Poly Lens does the same with a permanent read thread. Idle
otherwise puts no traffic on the link at all, matching the vendor: a Poly link measured silent for
25 s at a time."""


class PolyHeadsetDevice(Device):
    """One Poly headset, over Bluetooth RFCOMM or a USB HID dongle."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        # One thread owns the transport. Deckard has no sequence layer -- replies are matched by
        # (type, id) -- so a second reader consumes the reply another call is waiting for. This is
        # the same rule that made Sony's speak-to-chat switch itself off.
        self._lock = asyncio.Lock()
        self._hp: PolyHeadset | None = None
        self._set = CapabilitySet()
        self._values: dict[str, Any] = {}
        self._read_only: set[str] = set()
        self._supported: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    @property
    def capabilities(self) -> CapabilitySet:
        return self._set

    def _make_transport(self) -> Any:
        """USB when the device came from a hidraw node, Bluetooth otherwise.

        Both satisfy the same connect/close/send/receive interface, which is what lets one session
        layer serve them -- Poly's own DLLs share their encoder between the two as well.
        """
        if self.info.transport is Transport.HID:
            return HidTransport(self._deckard_node())
        return RfcommTransport(self.info.address or self.info.uid)

    def _deckard_node(self) -> str | None:
        """The hidraw node that actually carries Deckard, not merely the first one enumerated.

        One USB device exposes several hidraw nodes -- measured on this machine, two Razer devices
        produce seven between them -- and only one of a Poly dongle's declares report 0x07. Opening
        the wrong one connects and then never answers. ``find_devices`` filters on the report
        descriptor, which is the only reliable signal.
        """
        wanted = self.info.path or ""
        try:
            devices = find_devices()
        except OSError:
            return wanted or None
        for device in devices:
            if device.path == wanted:
                return device.path
        # The enumerated node is not the Deckard one: prefer another node of the same product.
        for device in devices:
            if self.info.product_id is not None and device.product == self.info.product_id:
                log.info("%s does not carry Deckard; using %s", wanted, device.path)
                return device.path
        if devices:
            log.info("%s does not carry Deckard; using %s", wanted, devices[0].path)
            return devices[0].path
        return wanted or None

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        hp = PolyHeadset(self.info.address or self.info.uid, transport=self._make_transport())
        try:
            hp.connect()
        except (TransportError, HidTransportError, OSError) as exc:
            # The shell says "<device> is <this>. Switch it on, then Rescan.", so this has to read
            # as the middle of that sentence. A raw errno did not: "...Series is SET_REPORT
            # failed: [Errno 32] Broken pipe. Switch it on, then Rescan."
            raise Unreachable(_unreachable_reason(exc)) from exc
        except DeckardError as exc:
            raise Unreachable(f"no answer from the headset: {exc}") from exc
        self._hp = hp
        self._read_snapshot()

    async def disconnect(self) -> None:
        hp, self._hp = self._hp, None
        if hp is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(hp.close)

    # ------------------------------------------------------------------ reading

    def _read_adapter(self, hp: Any) -> tuple[Any, list[str], dict[str, Any]]:
        """The USB adapter's own settings, read at the local endpoint.

        Only when there is one to read: on a headset reached over its own cable the endpoint *is*
        the headset, and there is nothing separate to show. Failures here are silent -- the
        adapter's two settings are not worth failing a headset's page over.
        """
        from .protocol import catalogue as cat
        from .protocol.session import ADDRESS_LOCAL

        attached = hp.br_address
        if attached == ADDRESS_LOCAL:
            return None, [], {}
        try:
            hp.br_address = ADDRESS_LOCAL
            pid = hp.read_int("USB_PID", 0x0A02)
            catalogue = cat.load(pid)
            if catalogue is None:
                return None, [], {}
            supported: list[str] = []
            values: dict[str, Any] = {}
            for setting in catalogue.settings:
                if setting.is_action or not setting.choices:
                    continue
                try:
                    value = hp.read_choice(setting)
                except Exception:  # noqa: BLE001 - one unreadable setting is not a failure
                    continue
                if value is None:
                    continue
                supported.append(setting.name)
                values[C.adapter_key(setting.name)] = self._to_ui(setting, value)
            return catalogue, supported, values
        except Exception:  # noqa: BLE001
            log.debug("adapter settings unavailable", exc_info=True)
            return None, [], {}
        finally:
            hp.br_address = attached

    # ------------------------------------------------------------------ where this device is

    ROUTE_BLUETOOTH = "Bluetooth"
    ROUTE_USB = "USB"
    """A cable or a charging stand -- indistinguishable from here, so neither is claimed."""

    def connection_label(self) -> ConnectionLabel:
        return ConnectionLabel(self.connection_route(), self.connection_identifier())

    def connection_route(self) -> str:
        """Bluetooth, plain USB, or through whatever is plugged in.

        The hop is the signal: a session that had to address a downstream port is talking *through*
        something, and that something is the USB device whose node we opened. Its own product name
        is the honest label -- "via Poly BT700" stays right on a BT600 or a D200, where "adapter"
        would be vaguer and a hardcoded model name would be wrong.
        """
        hp = self._hp
        if hp is None:
            return ""
        if self.info.transport is Transport.BLUETOOTH:
            return self.ROUTE_BLUETOOTH
        if "port" not in (hp.endpoint or ""):
            return self.ROUTE_USB
        return f"via {_shorten(self.info.name)}"

    def connection_identifier(self) -> str:
        """The headset's serial. Absent on devices that will not give one, which is allowed."""
        return str(self._values.get(f"{C.INFO_PREFIX}tattoo_serial_number") or "")

    def _peer_devices(self) -> set[int]:
        transport = getattr(self._hp, "transport", None)
        getter = getattr(transport, "peer_product_ids", None)
        return set(getter()) if callable(getter) else set()

    def _read_snapshot(self) -> None:
        """Read identity, battery and every catalogue setting the device actually implements."""
        hp = self._hp
        if hp is None:
            raise Unreachable("not connected")
        state = hp.snapshot(name=self.info.name)
        self._supported = set(state.supported)

        values: dict[str, Any] = {}
        for name, value in state.settings.items():
            setting = hp.catalogue.by_name(name) if hp.catalogue else None
            values[C.setting_key(name)] = self._to_ui(setting, value)

        for field, _label in C.IDENTITY_ROWS:
            if field in state.identity:
                values[f"{C.INFO_PREFIX}{field.lower()}"] = state.identity[field]
        adapter = self._read_adapter(hp)
        values.update(adapter[2])
        values[f"{C.INFO_PREFIX}connection"] = state.endpoint or "—"
        if state.battery is not None:
            values[C.BATTERY_KEY] = state.battery.percent

        self._values = values
        self._set = C.build(
            hp.catalogue,
            supported=sorted(self._supported),
            identity=state.identity,
            has_battery=state.battery is not None,
            # Another Poly device on the bus means this headset has a second way in, and a
            # second row in the list. Saying so is the difference between "why is it twice?"
            # and "of course it is."
            two_routes=bool(self._peer_devices()),
            adapter=(adapter[0], adapter[1]),
        )
        self._bump_capabilities()

    @staticmethod
    def _to_ui(setting: cat.Setting | None, value: str | None) -> Any:
        """A catalogue value name, as the shell wants it: ``bool`` for a switch, else the name."""
        if setting is None or value is None:
            return value
        pair = C.boolean_names(setting)
        return value == pair[0] if pair is not None else value

    @staticmethod
    def _to_wire(setting: cat.Setting, value: Any) -> str:
        """The reverse: the catalogue value name to write."""
        pair = C.boolean_names(setting)
        if pair is None:
            return str(value)
        on, off = pair
        return on if bool(value) else off

    async def get(self, key: str) -> Any:
        if key not in self._values:
            raise NotSupported(key)
        return self._values[key]

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        # Served from the snapshot taken at connect. A full re-read is ~3.6 s and is a user
        # action, never something the shell does behind the user's back.
        wanted = set(keys)
        return {k: v for k, v in self._values.items() if k in wanted}

    def advisories(self) -> dict[str, Advisory]:
        """Settings the hardware reads but refuses to write.

        ``COMMAND_UNKNOWN`` is only discoverable by attempting a write -- the V4310 does this for
        ``linkQualityReporting``. Locking the control with an explanation is honest; leaving it
        live so the user can fail again is not.
        """
        return {
            C.setting_key(name): Advisory(
                message="This headset reports that setting as read-only — it can be read but not "
                "changed over the configuration channel.",
                locked=True,
            )
            for name in self._read_only
        }

    # ------------------------------------------------------------------ writing

    async def set(self, key: str, value: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._set_sync, key, value)

    def _set_sync(self, key: str, value: Any) -> Any:
        hp = self._hp
        if hp is None:
            raise Unreachable("not connected")
        if key == C.REFRESH_KEY:
            self._read_snapshot()
            return None
        if key == C.RECONNECT_KEY:
            self._reconnect()
            return None

        if key.startswith(C.ADAPTER_PREFIX):
            return self._set_on_adapter(hp, key, value)

        name = C.setting_name(key)
        setting = hp.catalogue.by_name(name) if hp.catalogue else None
        if setting is None:
            raise NotSupported(key)

        if key.startswith(C.ACTION_PREFIX):
            # A catalogue action carries a single value; the session refuses anything with a get
            # id, so a mistake here cannot turn into a generic write.
            choice = setting.choices[0].name if setting.choices else ""
            self._perform_action(setting, choice)
            return None

        wire = self._to_wire(setting, value)
        try:
            applied = hp.write_choice(setting, wire)
        except CommandUnsupported:
            self._read_only.add(name)
            raise NotSupported(f"{name} is read-only on this headset") from None
        except (TransportError, HidTransportError, OSError) as exc:
            raise Unreachable(str(exc)) from exc
        except DeckardError as exc:
            raise DeviceError(str(exc)) from exc
        landed = self._to_ui(setting, applied)
        self._values[key] = landed
        return landed

    def _set_on_adapter(self, hp: Any, key: str, value: Any) -> Any:
        """Write a setting that belongs to the USB adapter, not to the headset behind it.

        The address is switched for the one write and put back. Getting this wrong would send an
        adapter setting to the headset -- both catalogues use the same setting names, so it would
        be accepted somewhere and quietly do the wrong thing.
        """
        from .protocol import catalogue as cat
        from .protocol.session import ADDRESS_LOCAL

        name = C.setting_name(key.removeprefix(C.ADAPTER_PREFIX))
        attached = hp.br_address
        try:
            hp.br_address = ADDRESS_LOCAL
            catalogue = cat.load(hp.read_int("USB_PID", 0x0A02))
            setting = catalogue.by_name(name) if catalogue else None
            if setting is None:
                raise NotSupported(key)
            applied = hp.write_choice(setting, self._to_wire(setting, value))
        except CommandUnsupported:
            raise NotSupported(f"{name} is read-only on this adapter") from None
        except (TransportError, HidTransportError, OSError) as exc:
            raise Unreachable(str(exc)) from exc
        except DeckardError as exc:
            raise DeviceError(str(exc)) from exc
        finally:
            hp.br_address = attached
        landed = self._to_ui(setting, applied)
        self._values[key] = landed
        return landed

    def _perform_action(self, setting: cat.Setting, choice: str) -> None:
        """Issue a write-only catalogue action.

        Deliberately *not* routed through ``write_choice``: that refuses ``is_action`` outright,
        which is the correct guard for a generic setter and must stay. The reference
        implementation's Maintenance buttons call it anyway, so pressing one there raises
        "refusing to issue it" instead of doing anything -- a path its own handover lists as never
        click-tested. Actions therefore get their own explicit route, reachable only from a
        capability the shell rendered as an ACTION and confirmed.
        """
        hp = self._hp
        if hp is None or hp.catalogue is None:
            raise Unreachable("not connected")
        from .protocol.framing import Frame, MessageType

        if setting.set_id is None:
            raise NotSupported(f"{setting.name} has no set id")
        payload = setting.choice(choice).payload if setting.choice(choice) else b""
        try:
            hp._exchange(
                Frame(MessageType.PERFORM_COMMAND, setting.set_id, payload,
                      reserved=hp.br_address)
            )
        except DeckardError as exc:
            raise DeviceError(str(exc)) from exc

    def _reconnect(self) -> None:
        """Rebuild the session: handshake, downstream attach, catalogue, capability probe.

        Distinct from re-reading, and the distinction is worth keeping -- a refresh reuses the same
        session, so it cannot fix a stalled link, a headset that rebooted or moved host, or a
        dongle that re-enumerated.
        """
        if self._hp is not None:
            with contextlib.suppress(Exception):
                self._hp.close()
            self._hp = None
        self._read_only.clear()
        self._connect_sync()

    # ------------------------------------------------------------------ change stream

    def changes(self) -> AsyncIterator[CapabilityValue]:
        """Changes the headset made on its own.

        Press mute on the headset, or change something from a phone, and the device pushes an
        ``EVENT``. Nothing here polls -- this only collects what has already arrived -- so it is
        the sole route by which such a change reaches the screen.
        """

        async def stream() -> AsyncIterator[CapabilityValue]:
            while True:
                await asyncio.sleep(EVENT_DRAIN_INTERVAL)
                if self._hp is None:
                    continue
                async with self._lock:
                    hp = self._hp
                    if hp is None:
                        continue
                    try:
                        changed = await asyncio.to_thread(hp.drain_events)
                    except Exception:
                        log.debug("event drain failed", exc_info=True)
                        continue
                    catalogue = hp.catalogue
                # Everything below is pure mapping -- the session is not touched again, because a
                # disconnect between the drain and here would otherwise be an AttributeError.
                for name, value in changed.items():
                    setting = catalogue.by_name(name) if catalogue else None
                    key = C.setting_key(name)
                    ui_value = self._to_ui(setting, value)
                    self._values[key] = ui_value
                    yield CapabilityValue(key=key, value=ui_value)

        return stream()


__all__ = ["PolyHeadsetDevice", "decode_string", "ids"]


def _unreachable_reason(exc: Exception) -> str:
    """A transport failure, as the middle of "<device> is ___. Switch it on, then Rescan."

    A broken pipe on a Poly link means the far end is not there: the headset is off, asleep, or out
    of range of its dongle. The errno says the same thing in a language nobody wants at this
    moment, so it is kept for the log and not for the sentence.
    """
    text = str(exc)
    if "Broken pipe" in text or "No such device" in text or "not connected" in text.lower():
        return "not answering — it may be switched off, asleep, or out of range of its dongle"
    if "Permission denied" in text:
        return "not readable by your user — see the udev rule in docs/INSTALL.md"
    return f"not reachable ({text})"


def _shorten(name: str) -> str:
    """"Plantronics Poly BT700" -> "Poly BT700". The row is narrow and the vendor is a given."""
    for prefix in ("Plantronics Poly ", "Plantronics ", "Poly "):
        if name.startswith(prefix):
            return name[len(prefix):] if prefix != "Poly " else name
    return name
