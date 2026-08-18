"""The shell's view of an 8BitDo Xbox wired controller.

The whole configuration is one record with one checksum, so there is no such thing as writing a
single setting: every change is a read-modify-write of all 532 bytes, followed by a commit. That
shapes everything here.

**The record is held in memory from connect.** A write patches the held copy, rolls the checksum
and sends the lot. The alternative -- re-reading before each change -- would double an already
slow operation and, over BLE, is not even possible for the header.

**Every write briefly interrupts the gamepad.** Over USB the config channel *is* the input
interface, so claiming it stops input for about a second. Said in an advisory rather than
discovered by someone mid-game.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hardware_ui.core.capability import Advisory, CapabilitySet
from hardware_ui.core.connection import ConnectionLabel
from hardware_ui.core.device import DependencyMissing, Device, DeviceInfo
from hardware_ui.core.diagram import Diagram

from . import anchors, transport
from . import capabilities as C
from .protocol import empty_slot
from .protocol import fieldmap as fm

log = logging.getLogger(__name__)

#: A USB session detaches the kernel driver, waits out a possible re-enumeration and moves 532
#: bytes in 41-byte pieces; BLE adds a scan and a slower link. Neither is quick.
CONNECT_TIMEOUT = 40.0

PACKAGES = {"usb": "dev-python/pyusb", "dbus": "dev-python/dbus-python"}

INPUT_PAUSE = (
    "Saving takes about a second, and over USB the controller stops responding as a gamepad while "
    "it happens — the configuration channel and the input channel are the same interface."
)


def _dependency(exc: ImportError) -> DependencyMissing:
    missing = (getattr(exc, "name", "") or "").split(".")[0]
    package = PACKAGES.get(missing)
    if package:
        return DependencyMissing(
            f"8BitDo controllers need {package}, which is not installed. "
            f"The rest of the application is unaffected.")
    return DependencyMissing(f"8BitDo support is missing a dependency: {exc}")


class EightBitDoController(Device):
    """One controller, reached over USB or over its config radio."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._link = _link_for(info)
        self._config: Any = None
        self._checksum: int | None = None
        self._slot = 0
        self._capabilities = CapabilitySet([])
        self._advisories: dict[str, Advisory] = {}
        #: Edits made here that the controller has not been told about yet. See :meth:`set`.
        self._dirty = False

    # ------------------------------------------------------------------ lifecycle

    connect_timeout = CONNECT_TIMEOUT

    def connect_notice(self) -> str:
        if self._link.kind == "ble":
            return ("Hold the controller's config button until it advertises as 82CE, then "
                    "connecting takes a few seconds.")
        return "Reading the controller's configuration takes a few seconds."

    async def connect(self) -> None:
        try:
            config, checksum = await asyncio.to_thread(transport.read, self._link)
        except ImportError as exc:
            raise _dependency(exc) from exc
        except transport.TransportError as exc:
            raise RuntimeError(str(exc)) from exc
        self._config, self._checksum = config, checksum
        self._slot = config.active_slot or 0
        self._describe()

    async def disconnect(self) -> None:
        # Nothing is held open: each operation opens and closes its own session, because keeping
        # the USB interface claimed would keep the gamepad muted for as long as the page is shown.
        self._config = None

    def connection_label(self) -> ConnectionLabel:
        if self._config is None:
            return ConnectionLabel("Not connected", "")
        route = "USB cable" if self._link.kind == "usb" else "Bluetooth config radio"
        detail = ("checksum unknown" if self._checksum is None
                  else f"checksum {self._checksum:04x}")
        return ConnectionLabel(route, detail)

    # ------------------------------------------------------------------ capabilities

    @property
    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    def _slot_config(self):
        return self._require().slots[self._slot]

    def _describe(self) -> None:
        self._capabilities = CapabilitySet(C.build(profile_written=self._slot_config().written))
        self._advise()
        self._bump_capabilities()

    def _advise(self) -> None:
        self._advisories = {}
        if self._dirty:
            unsaved = Advisory(
                "Changed here but not written to the controller yet — press “Sync to controller”. "
                f"{INPUT_PAUSE}"
            )
            for members in self._capabilities.groups().values():
                first = next(iter(members), None)
                if first is not None:
                    self._advisories[first.key] = unsaved
        else:
            self._advisories[C.KEY_PROFILE] = Advisory(INPUT_PAUSE)

        if self.info.product_id not in anchors.MODEL_PRODUCT_IDS:
            # Reached over Bluetooth, where the advertisement is "82CE" for every 8BitDo controller
            # and says nothing about which one. The byte offsets on this page belong to the
            # Ultimate Wired for Xbox; another family's record is laid out differently.
            self._advisories[C.KEY_SYNC] = Advisory(
                "This controller could not be identified — over Bluetooth every 8BitDo controller "
                "advertises under the same name. These settings are laid out for the Ultimate "
                "Wired Controller for Xbox. If that is not what this is, do not sync: connect it "
                "by USB cable, which does identify the model."
            )

        if not self._slot_config().written:
            empty = Advisory(
                f"Profile {self._slot + 1} is empty — the controller has never had one written "
                f"here, so there is nothing to edit. Press “Create profile” on the Profile tab to "
                f"fill it with the controller's defaults, and everything below becomes editable."
            )
            # On **every** tab, not only the one carrying the button. The shell shows a tab the
            # message belonging to the first of its own rows that has one, so a single advisory on
            # the Profile tab left Buttons, Sticks, Triggers and Vibration greyed out with no
            # explanation anywhere. That is precisely how an empty profile came to look like a bug.
            for group, members in self._capabilities.groups().items():
                first = next(iter(members), None)
                if first is not None and group != C.GROUP_PROFILE:
                    self._advisories[first.key] = empty
            self._advisories[C.KEY_RESET] = empty

    def advisories(self) -> dict[str, Advisory]:
        return dict(self._advisories)

    def diagrams(self) -> dict[str, Diagram]:
        """One drawing per view, keyed by the section its rows already carry.

        This is what the three SVGs were drawn and anchored for. Until now they shipped and were
        never rendered: :mod:`.anchors` could read the coordinates and nothing asked it to, so the
        Buttons tab was nineteen dropdowns named after parts of a controller nobody could see.

        The mapping is derived from :data:`anchors.VIEWS` rather than written out again, so a view
        cannot end up with a drawing and a section heading that disagree -- and the anchors come
        out of the drawings themselves, so the picture and the coordinates cannot drift apart
        either. A view whose SVG will not load simply contributes nothing and its rows render as an
        ordinary form.

        **Only for the model the drawings are of.** The Bluetooth match rule is a name glob on
        ``82CE``, and that is what *every* 8BitDo controller advertises while its config button is
        held -- a Pro 2 or an Ultimate BT included, and those use different records. A row this
        module cannot positively identify gets a plain form rather than a confident picture of
        somebody else's controller, because the drawing is the part of the page a user trusts to
        say which button they are editing.
        """
        if self.info.product_id not in anchors.MODEL_PRODUCT_IDS:
            log.info("no drawings for %s: not a known model for %s",
                     self.info.name, anchors.MODEL)
            return {}
        out: dict[str, Diagram] = {}
        for view, (path, _) in anchors.VIEWS.items():
            found = anchors.anchors(view)
            if not found:
                continue
            out[anchors.VIEW_LABELS[view]] = Diagram(
                image=str(path),
                # Keyed by capability, not by the record's own field name: the panel is handed
                # capability keys and knows nothing about this device's vocabulary.
                anchors={C.map_key(name): point for name, point in found.items()},
                caption=anchors.VIEW_CAPTIONS.get(view, ""),
            )
        return out

    # ------------------------------------------------------------------ reads

    async def get(self, key: str) -> Any:
        return self._value(key)

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Served from the record held since connect.

        There is nothing to re-read: the controller does not change its stored configuration on
        its own, and a read costs a full session. ``refresh()`` is how a caller asks for one.
        """
        return {key: self._value(key) for key in keys}

    def _value(self, key: str) -> Any:
        config = self._require()
        if key == C.KEY_PROFILE:
            return self._slot
        if key == C.KEY_ACTIVE:
            active = config.active_slot
            return f"Profile {active + 1}" if active is not None else "Unknown"

        slot = self._slot_config()
        name = C.map_name(key)
        if name is not None:
            return (slot.get_paddle(name) if name in fm.REL_PADDLE else slot.get_map(name))
        toggle = C.toggle_name(key)
        if toggle is not None:
            return slot.get_toggle(toggle)
        spec = C.value_spec(key)
        if spec is not None:
            return slot.get_value(*spec)
        return None

    async def refresh(self) -> dict[str, Any]:
        await self.connect()
        return {c.key: self._value(c.key) for c in self._capabilities}

    # ------------------------------------------------------------------ writes

    async def set(self, key: str, value: Any) -> Any | None:
        """Edit the held record. **Nothing reaches the controller until Sync.**

        This is the shape ``8bitdo-cfg`` has, and copying it is the point rather than a
        simplification. Saving on every change looks tidier and is wrong here for two reasons that
        both come from the record being one indivisible block:

        *Every* change is a write of all 532 bytes, so remapping four buttons meant four full
        sessions -- four kernel-driver detaches, four commits, four one-second gaps where the pad
        stops being a gamepad -- to express one intent. And a save that does not take has nothing
        to retry: the next change simply writes again and fails again, with no button to press and
        nothing to see. Batching the edits gives the user exactly one moment where the write
        happens and one place to make it happen again.
        """
        config = self._require()

        # Switching which profile is being edited touches nothing, on the device or in the record.
        if key == C.KEY_PROFILE:
            slot = int(value)
            if not 0 <= slot < fm.SLOT_COUNT:
                raise RuntimeError(f"no profile {slot}")
            self._slot = slot
            self._describe()
            return slot

        # The one action that talks to the controller.
        if key == C.KEY_SYNC:
            await self._save()
            self._dirty = False
            self._advise()
            return "All three profiles written to the controller"

        if key == C.KEY_RESET:
            config.slots[self._slot] = transport.default_slot(config)
        elif key == C.KEY_DELETE:
            if self._slot == 0:
                raise RuntimeError("Profile 1 is the controller's base profile and cannot be "
                                   "deleted. Reset it to defaults instead.")
            config.slots[self._slot] = empty_slot()
        else:
            self._patch(key, value)
            # A plain edit changes a value, not the shape of the page. Rebuilding here would
            # bump the capability revision, which makes the shell repaint the whole device page --
            # losing the drawing the user was looking at, mid-edit.
            self._mark_dirty()
            return self._value(key)

        # Reset and delete do change the shape: an empty profile has nothing to edit.
        self._mark_dirty()
        self._describe()
        return self._value(key)

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._advise()

    def _patch(self, key: str, value: Any) -> None:
        slot = self._slot_config()
        name = C.map_name(key)
        if name is not None:
            if str(value) not in fm.CODE:
                raise RuntimeError(f"{value!r} is not a button this controller can emit")
            if name in fm.REL_PADDLE:
                slot.set_paddle(name, str(value))
            else:
                slot.set_map(name, str(value))
            return
        toggle = C.toggle_name(key)
        if toggle is not None:
            slot.set_toggle(toggle, bool(value))
            return
        spec = C.value_spec(key)
        if spec is not None:
            slot.set_value(spec[0], spec[1], float(value))
            return
        raise RuntimeError(f"{key} is not writable")

    async def _save(self) -> None:
        """Write the whole record and remember the checksum it now carries."""
        try:
            self._checksum = await asyncio.to_thread(
                transport.write, self._link, self._require(), self._checksum)
        except ImportError as exc:
            raise _dependency(exc) from exc
        except transport.TransportError as exc:
            raise RuntimeError(str(exc)) from exc

    # ------------------------------------------------------------------ helpers

    def _require(self):
        if self._config is None:
            raise RuntimeError(f"{self.info.name} is not connected")
        return self._config


def _link_for(info: DeviceInfo) -> transport.Link:
    """Which way in this row represents.

    USB rows carry a serial and a product id; a BLE row carries an address. Decided from the
    transport rather than sniffed, so a row always opens the link it advertises.
    """
    if info.transport in ("bluetooth", "ble"):
        return transport.Link("ble", info.address or info.uid.removeprefix("bt:"))
    return transport.Link("usb", info.serial, info.product_id or None)


__all__ = ["EightBitDoController"]
