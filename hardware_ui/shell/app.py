"""The controller: what the window calls into, and where the lazy-probe rules are enforced.

Startup order matters and is deliberate:

1. Window shows, populated from the cached device list -- 0 ms, no I/O.
2. Enumeration runs and reconciles the sidebar -- ~30 ms, still no device opened.
3. Nothing else happens until the user picks a device *and presses Connect*.

Only step 3 imports a module's Python or touches hardware. Every device operation is wrapped in a
timeout, because one wedged I2C bus or one unreachable headset must never freeze the UI.

Threading rule, applied without exception: coroutines run on the asyncio thread and never touch a
widget. Anything that does goes through :meth:`Controller._ui`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QUrl

from hardware_ui.core import (
    CapabilitySet,
    DependencyMissing,
    Device,
    DeviceInfo,
    Kind,
    ModuleRegistry,
    NotSupported,
    State,
    Support,
    Unreachable,
    discovery,
    photos,
)

from .asyncbridge import AsyncBridge
from .window import MainWindow

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 60.0
"""Opening a device: SDP lookup, RFCOMM connect, then the handshake (which ends with a full state
read). For Sony that is ~11 protocol round-trips, each allowed up to 3 s by ``session.py``."""

OP_TIMEOUT = 5.0
"""A single read. These come from cached state, so anything slower is a hung device."""

WRITE_TIMEOUT = 20.0
"""A single write, which can be several round-trips: ``set_stc`` alone sends four commands."""

POLL_INTERVAL = 30.0

HOTPLUG_SETTLE = 0.4
"""Seconds of quiet before re-enumerating after a hotplug event.

Plugging one device emits a burst -- the USB device, one event per interface, then the hidraw
nodes -- and a YubiKey re-configuring itself unplugs and replugs in the middle of that. One sweep
after the noise stops is both cheaper and more correct than one per event.
"""
"""Matches the reference implementation's ``BATTERY_POLL_MS``. With no notification listener this
is the only route by which a change made on the device itself reaches the UI."""

REBOOT_RECONNECT_TIMEOUT = 15.0
"""How long to keep waiting for a restarting device to come back before giving up.

Was a flat sleep of the same length, chosen for a Sony headset and a lifetime for a YubiKey that
re-enumerates in a second or two. Hotplug says precisely when a device returns, so this is now only
the point at which we stop hoping.
"""

REBOOT_POLL = 0.4
"""How often to look for it while waiting. Also the fallback when hotplug is unavailable."""

REBOOT_DROP_TIMEOUT = 4.0
"""How long to wait for a restarting device to actually disappear before giving up on seeing it go.

A device that never drops -- because it applied the change without restarting after all -- must
not hold the reconnect hostage for the full timeout.
"""


class Controller(QObject):
    """Owns device lifecycle and mediates between the window and the modules."""

    def __init__(self, registry: ModuleRegistry, bridge: AsyncBridge, window: MainWindow) -> None:
        super().__init__()
        self._registry = registry
        self._bridge = bridge
        self._window = window

        self._devices: list[DeviceInfo] = []
        self._selected: DeviceInfo | None = None
        # Open devices, keyed by uid. Several may be open at once: connecting to a second monitor
        # used to silently close the first, which is nonsense for DDC/CI -- there is no session
        # to hold, every operation is its own ddcutil invocation. It was equally arbitrary for
        # anything else; two headsets are two independent sockets.
        self._open: dict[str, Device] = {}
        self._polls: dict[str, asyncio.Task[None]] = {}
        self._hotplug: Any = None
        self._settle: asyncio.TimerHandle | None = None
        self._bluetooth: Any = None
        self._watches: dict[str, asyncio.Task[None]] = {}
        self._painted_open: frozenset[str] = frozenset()
        # *Which* devices are opening or restarting -- sets, not single values, and not booleans.
        #
        # A boolean followed the selection instead of the device: opening one and then clicking
        # another greyed out the second device's button. Replacing it with a single uid fixed that
        # and left a narrower race: two devices can be opening at once, and whichever finished
        # first cleared the other's state, freeing a button for a device still connecting.
        self._busy_uids: set[str] = set()
        self._reconnecting_uids: set[str] = set()
        self._idle_status = ""

        page, sidebar = window.page, window.sidebar
        sidebar.selected.connect(self.select)
        sidebar.rescanRequested.connect(self.rescan)
        sidebar.modulesRequested.connect(self.show_modules)
        page.connectRequested.connect(self.connect_device)
        page.disconnectRequested.connect(self.disconnect_device)
        page.changed.connect(self._on_changed)
        page.triggered.connect(lambda key: self._on_changed(key, True))
        # A copy leaves no trace on screen, so say it happened. A one-time code is the case that
        # matters: pressing the button and seeing nothing change reads as a broken button.
        page.copied.connect(self._on_copied)
        page.photoRequested.connect(self._choose_photo)
        page.photoFetchRequested.connect(self._fetch_photo)
        page.photoCleared.connect(self._clear_photo)
        #: Built **here**, on the GUI thread, and never lazily.
        #:
        #: A QObject's thread affinity is whichever thread constructed it, and its queued signals
        #: are delivered to that thread's event loop. Built on first use it would be constructed
        #: inside ``_connect``, which runs on the asyncio thread -- which has no Qt event loop, so
        #: every ``message()`` would be queued and never delivered. The symptom is precise and
        #: baffling: the pairing dialog simply never appears, nothing raises, and the action sits
        #: there looking stuck.
        from .interaction import QtInteraction

        self.__interaction = QtInteraction(window)

    def _ui(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        """Marshal a widget update onto the GUI thread."""
        self._bridge.call_on_ui(fn, *args, **kwargs)

    # ---------------------------------------------------------------- startup

    def paint_from_cache(self) -> None:
        """Step 1, before the window is shown. Cached devices render normally, not greyed."""
        cached = [self._registry.claim(d) for d in discovery.load_cache()]
        if cached:
            self._devices = cached
            self._window.sidebar.reconcile(self._visible())

    async def enumerate(self) -> None:
        """Step 2. Sweep every transport, then reconcile on the GUI thread."""
        self._ui(self._window.sidebar.set_status, "Looking for devices…")
        found = await asyncio.to_thread(discovery.enumerate_all)
        # Expanded after claiming: a module that can see devices enumeration cannot -- a mouse
        # paired to a receiver with no node of its own -- gets to add them here. See
        # ModuleRegistry.expand.
        self._devices = self._registry.expand(
            [self._registry.claim(d) for d in found]
        )
        self._ui(self._window.sidebar.reconcile, self._visible())
        await asyncio.to_thread(discovery.save_cache, self._devices)

        await self._drop_stale()
        self._ui(self._window.sidebar.reconcile, self._visible())

        supported = sum(1 for d in self._devices if d.supported)
        self._idle_status = f"{supported} configurable device{'s' if supported != 1 else ''}"
        self._ui(self._window.sidebar.set_status, self._idle_status)
        log.info("enumerated %d devices, %d supported", len(self._devices), supported)
        self._start_hotplug()

    async def _drop_stale(self) -> None:
        """Close devices we hold a handle to that is no longer good.

        Without this a device shows a green dot forever after being unplugged: ``_visible`` marks
        a row connected because its uid is in ``_open``, and nothing was removing it. Two ways for
        a handle to go bad, and both happen in practice:

        **The device is gone.** Straightforward, and now noticed because hotplug re-enumerates.

        **The device came back somewhere else.** A YubiKey pulled and re-inserted keeps its uid --
        it is derived from the USB port path, or from a serial number, precisely so that settings
        follow a device across reboots -- but its ``/dev/hidraw`` node is a new one. The old
        descriptor still opens, reads nothing, and reports success at doing so.
        """
        present = {d.uid: d for d in self._devices}
        stale = [
            uid
            for uid, device in self._open.items()
            if uid not in present or present[uid].path != device.info.path
        ]
        for uid in stale:
            log.info("%s: handle no longer valid, closing", uid)
            await self._teardown(uid)

    # ---------------------------------------------------------------- hotplug

    def _start_hotplug(self) -> None:
        """Subscribe once, after the first sweep. Whatever is unavailable just means Rescan.

        Two sources, because a device is on one bus or the other and neither hears about the
        other's: udev for USB, HID and DRM, BlueZ for anything Bluetooth.
        """
        if self._hotplug is None:
            watcher = discovery.watch()
            if watcher is not None:
                self._hotplug = watcher
                asyncio.get_running_loop().add_reader(watcher.fileno(), self._on_hotplug)
                log.info("hotplug watching %s", ", ".join(discovery.HOTPLUG_SUBSYSTEMS))
        if self._bluetooth is None:
            self._ui(self._start_bluetooth_hotplug)

    def _start_bluetooth_hotplug(self) -> None:
        """Called on the Qt thread: QtDBus subscribes there and delivers signals there."""
        from .bluetooth import BluetoothWatcher

        watcher = BluetoothWatcher(self._window)
        if not watcher.start():
            return
        # The settle timer is on the Qt side, so this fires once per burst. Handing the sweep to
        # the asyncio thread is safe from here -- that is what the bridge is for.
        watcher.changed.connect(
            lambda: self._bridge.spawn(self.enumerate(), label="hotplug-bluetooth")
        )
        self._bluetooth = watcher

    def _on_hotplug(self) -> None:
        """Called on the asyncio thread when udev has something. Drains, then waits for quiet."""
        if self._hotplug is None:
            return
        subsystems = self._hotplug.drain()
        if not subsystems:
            return
        log.debug("hotplug: %s", ", ".join(sorted(subsystems)))
        loop = asyncio.get_running_loop()
        if self._settle is not None:
            self._settle.cancel()
        self._settle = loop.call_later(
            HOTPLUG_SETTLE, lambda: self._bridge.spawn(self.enumerate(), label="hotplug")
        )

    def _stop_hotplug(self) -> None:
        bluetooth, self._bluetooth = self._bluetooth, None
        if bluetooth is not None:
            self._ui(bluetooth.stop)
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None
        watcher, self._hotplug = self._hotplug, None
        if watcher is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(watcher.fileno())
        watcher.close()

    def _visible(self) -> list[DeviceInfo]:
        """The sidebar's view: enumerated devices, with the ones we have open shown as connected.

        The override is computed here rather than written back into ``_devices``, because the two
        meanings of "connected" are not the same and conflating them broke the sidebar. BlueZ
        reports a headset as connected when its *audio* is up, which is what makes the row
        reachable in the first place; whether *we* hold its config channel is our own business.
        Writing our state over the enumerated one would have demoted a live headset to
        "disconnected" the moment the user closed its page.
        """
        import dataclasses

        return [
            dataclasses.replace(
                d,
                state=State.CONNECTED,
                connection=self._open[d.uid].connection_label() or d.connection,
            )
            if d.uid in self._open
            else d
            for d in self._devices
            if d.supported
        ]

    def _by_uid(self, uid: str) -> DeviceInfo | None:
        return next((d for d in self._devices if d.uid == uid), None)

    def rescan(self) -> None:
        self._bridge.spawn(self.enumerate(), label="rescan")

    def show_modules(self) -> None:
        """Which device families to look for. Modal, on the Qt thread, touching no device.

        Everything it shows comes from manifests the registry read at startup, so opening it
        imports nothing -- which matters, because switching a module off is exactly what you want
        when its dependency is broken.
        """
        from .modules_page import ModulesDialog

        dialog = ModulesDialog(self._registry, self._window)
        # Re-scan rather than re-discover: enablement filters matching, and the manifests on disk
        # have not changed.
        dialog.changed.connect(self.rescan)
        dialog.exec()

    # ---------------------------------------------------------------- selection

    @property
    def _device(self) -> Device | None:
        """The open device the page is currently showing, if any."""
        return self._open.get(self._selected.uid) if self._selected else None

    @property
    def connected(self) -> bool:
        """Whether the *selected* device is open."""
        return self._device is not None

    def select(self, uid: str) -> None:
        """Change which device is shown. Opens and closes nothing.

        The reference implementation is explicit: opening the config channel by itself can make
        the headset power-cycle, so this must never do it.
        """
        info = self._by_uid(uid)
        if info is None or (self._selected and self._selected.uid == uid):
            return
        self._selected = info
        self._window.page.set_device(info.name, info.support is Support.FAMILY)
        self._refresh_connection_ui()
        self._refresh_photo()

        if self.connected and self._device is not None:
            # Returning to the still-open device: put its page back.
            self._bridge.spawn(self._show_connected(self._device), label="reshow")
        else:
            self._window.page.show_capabilities(CapabilitySet())
            if not info.supported:
                self._window.notify("No module claims this device")

    @property
    def _interaction(self):  # noqa: ANN202 - a QtInteraction
        """The session's dialog, with any earlier cancellation forgotten."""
        self.__interaction.reset()
        return self.__interaction

    def connect_device(self) -> None:
        """Pressed Connect. Runs on the Qt thread, which is where a dialog belongs.

        The vendor-data offer happens here rather than inside ``_connect`` for that reason: asking
        a question needs the GUI thread, and the answer decides nothing about *whether* to open the
        device -- only whether its settings will carry the manufacturer's names.
        """
        if not self._selected or self.connected:
            return
        module_id = self._selected.module_id
        if module_id:
            from .vendor_data import ensure_vendor_data

            manifest = self._registry.get(module_id)
            name = getattr(manifest, "name", "") or module_id
            ensure_vendor_data(module_id, name, self._window)
        self._bridge.spawn(self._connect(self._selected.uid), label="connect")

    def disconnect_device(self) -> None:
        if self._selected:
            self._bridge.spawn(self._teardown(self._selected.uid), label="disconnect")

    async def _connect(self, uid: str) -> None:
        info = self._by_uid(uid)
        if info is None or not info.supported or uid in self._open:
            return
        manifest = self._registry.get(info.module_id)
        if manifest is None:
            self._ui(self._window.notify, "Module not installed", "error")
            return
        self._busy(uid, True)
        # `finally`, because `except Exception` does not catch `CancelledError` -- it is a
        # BaseException. A connect cancelled at shutdown, or by a teardown racing it, would leave
        # the uid marked busy for the rest of the session.
        try:
            cls = await asyncio.to_thread(manifest.load)  # the expensive import, deferred
            device: Device = cls(info)
            # How a module talks to the user *during* an operation -- a Bolt passkey that has to be
            # typed while the pairing lock is open. Silent by default; only modules that need it
            # ever call it.
            device.interaction = self._interaction
            notice = device.connect_notice()
            if notice:
                self._ui(self._window.notify, notice)
            await asyncio.wait_for(
                device.connect(), device.connect_timeout or CONNECT_TIMEOUT
            )
        except DependencyMissing as exc:
            # Shown verbatim: these carry install instructions, and wrapping them in
            # "<device> is … Switch it on, then Rescan" made nonsense of both.
            log.info("%s: %s", info.name, exc)
            self._fail(str(exc), uid)
            return
        except asyncio.CancelledError:
            self._busy(uid, False)
            raise
        except Unreachable as exc:
            log.info("%s: %s", info.name, exc)
            self._fail(f"{info.name} is {exc}. Switch it on, then Rescan.", uid)
            return
        except TimeoutError:
            self._fail(f"{info.name} did not respond in time", uid)
            return
        except Exception as exc:
            log.exception("connect failed for %s", uid)
            self._fail(f"Could not connect: {exc}", uid)
            return

        self._busy(uid, False)
        self._open[uid] = device
        self._ui(self._window.sidebar.set_status, self._idle_status)
        if self._selected is not None and self._selected.uid == uid:
            await self._show_connected(device)
        self._ui(self._refresh_photo)
        loop = asyncio.current_task().get_loop()
        self._polls[uid] = loop.create_task(self._poll_loop(device))
        self._watches[uid] = loop.create_task(self._watch_changes(device))

    async def _show_connected(self, device: Device) -> None:
        """Populate the page from an open device. Values come from state cached by the handshake,
        so this costs no protocol traffic and is safe on every re-selection."""
        caps = device.capabilities
        # Suffix sources have no row of their own -- a countdown shown after a one-time code --
        # so asking only for the rows would leave every one of them blank until the next push.
        wanted = list(dict.fromkeys(
            [c.key for c in caps] + [c.suffix_from for c in caps if c.suffix_from]
        ))
        try:
            values = await device.get_many(wanted)
        except Exception:
            log.exception("read failed for %s", device.info.uid)
            values = {}
        advisories = device.advisories()

        def paint() -> None:
            self._window.page.show_capabilities(caps)
            for form in self._window.page.forms().values():
                for key, value in values.items():
                    form.set_value(key, value, confirmed=True)
                form.set_advisories(advisories)
            self._refresh_connection_ui()

        self._ui(paint)

    def _fail(self, message: str, uid: str = "") -> None:
        """Report a failed open. *uid* is whose, so it releases only that device's button."""
        self._ui(self._window.notify, message, "error")
        self._ui(self._window.sidebar.set_status, self._idle_status)
        self._busy(uid, False)

    async def _teardown(self, uid: str) -> None:
        """Close one open device. Safe to call for a uid that is not open."""
        for tasks in (self._polls, self._watches):
            task = tasks.pop(uid, None)
            if task is not None:
                task.cancel()
        # Everything keyed by this uid, not only the handle. A device torn down while it was
        # opening -- unplugged mid-connect, say -- otherwise stays in the busy set, and its
        # Connect button is disabled for good if it comes back under the same uid.
        self._busy_uids.discard(uid)
        self._reconnecting_uids.discard(uid)
        device = self._open.pop(uid, None)
        if device is not None:
            try:
                await asyncio.wait_for(device.disconnect(), OP_TIMEOUT)
            except Exception:
                log.debug("disconnect failed; dropping anyway", exc_info=True)
        if self._selected is not None and self._selected.uid == uid:
            self._ui(self._window.page.show_capabilities, CapabilitySet())
        self._ui(self._refresh_connection_ui)

    # ---------------------------------------------------------------- writes

    def _form_for(self, key: str) -> Any:
        for form in self._window.page.forms().values():
            if key in form.keys():  # noqa: SIM118 - CapabilityForm.keys() is our own method
                return form
        return None

    def _on_copied(self, key: str) -> None:
        cap = self._device.capabilities.by_key(key) if self._device else None
        self._window.notify(f"{cap.label if cap else 'Value'} copied to the clipboard")

    def _on_changed(self, key: str, value: Any) -> None:
        if self._device is None:
            return
        cap = self._device.capabilities.by_key(key)
        if cap is not None and cap.prompt_fields:
            # Several things at once, and the modifiers travel with what they modify rather than
            # sitting on the page beside three other buttons that ignore them.
            answers = self._window.ask_fields(cap.label, cap.prompt_detail, cap.prompt_fields)
            if answers is None:
                return
            value = answers if cap.kind is Kind.ACTION else (value, answers)
        elif cap is not None and cap.prompt:
            # A secret belongs to the operation, not to the page: asked for here and handed over
            # as the action's value, so it is never left sitting in a form.
            answer = self._window.ask_pin(
                cap.label,
                cap.prompt_detail,
                change=cap.prompt in ("pin_change", "pin_set"),
                has_pin=cap.prompt == "pin_change",
                minimum=int(cap.minimum or 4),
                field_label=cap.prompt_label or "PIN",
                allow_empty=cap.prompt_optional,
            )
            if answer is None:
                return
            # An ACTION has no value of its own, so the answer *is* the value. Anything else does
            # -- a slider's number -- so the answer travels alongside it rather than replacing it.
            value = answer if cap.kind is Kind.ACTION else (value, answer)
        if cap is not None and cap.file_dialog:
            # The module cannot raise a dialog -- it runs on the asyncio thread -- so the shell
            # asks here and hands the path over as the action's value.
            chosen = self._window.choose_path(
                cap.file_dialog, cap.label, cap.file_filter, cap.file_suffix
            )
            if not chosen:
                return
            value = chosen
        if cap is not None and (cap.reboots or cap.confirm):
            accepted = (
                self._window.confirm_reboot(
                    cap.label, cap.confirm_detail, self._selected.name if self._selected else ""
                )
                if cap.reboots
                else self._window.confirm_change(cap.label, cap.confirm_detail)
            )
            if not accepted:
                # Rejected: repaint from the device's real value rather than leaving the
                # control showing what the user picked.
                form = self._form_for(key)
                if form is not None:
                    form.set_value(key, form.value_of(key), confirmed=True)
                return
        if cap is not None and cap.reboots:
            self._bridge.spawn(self._write_rebooting(key, value), label=f"write({key})")
            return
        self._bridge.spawn(self._write(key, value), label=f"write({key})")

    def _group(self, key: str) -> list[str]:
        device = self._device
        cap = device.capabilities.by_key(key) if device else None
        if cap is None or not cap.writes_with:
            return [key]
        return list(dict.fromkeys((key, *cap.writes_with)))

    async def _write(self, key: str, value: Any) -> None:
        """Optimistic write, then confirm or revert.

        The ``finally`` is load-bearing: releasing the pending flag on each branch separately
        means any path not thought of leaves the control disabled forever.
        """
        form = self._form_for(key)
        if self._device is None or form is None:
            return
        cap = self._device.capabilities.by_key(key)
        previous = form.value_of(key)
        group = self._group(key)
        revision = self._device.capabilities_revision
        self._ui(form.clear_result, key)
        self._ui(form.set_pending, group, key, value)
        try:
            timeout = (cap.timeout if cap is not None and cap.timeout else 0.0) or WRITE_TIMEOUT
            landed = await asyncio.wait_for(self._device.set(key, value), timeout)
        except NotSupported as exc:
            self._ui(form.set_result, key, False, str(exc))
            self._ui(form.mark_failed, key)
            self._ui(form.set_value, key, previous, confirmed=True)
            # Re-read advisories here too: a module can only discover "readable but not writable"
            # by attempting the write, so this failure is when the explanation becomes available.
            # Publishing it only on the success path left the control silently dead.
            self._ui(form.set_advisories, self._device.advisories())
            self._ui(self._window.notify, str(exc) or "This device does not support that setting",
                     "warning")
        except TimeoutError:
            self._ui(form.set_result, key, False, "The device did not answer in time")
            self._ui(form.set_value, key, previous, confirmed=True)
            self._ui(self._window.notify, "Device did not confirm the change", "error")
        except asyncio.CancelledError:
            self._ui(form.set_value, key, previous, confirmed=True)
            raise
        except Exception as exc:
            log.exception("write failed for %s", key)
            self._ui(form.set_result, key, False, str(exc))
            self._ui(form.set_value, key, previous, confirmed=True)
            self._ui(self._window.notify, f"Could not apply: {exc}", "error")
        else:
            # A device may report the value it actually landed on -- a DDC panel that quantises
            # the request still applied the change, and the monitor is the source of truth.
            self._ui(form.set_value, key, value if landed is None else landed, confirmed=True)
            self._ui(form.set_advisories, self._device.advisories())
            if cap is not None and cap.kind is Kind.ACTION:
                # An action's effect is often invisible, so say plainly that it worked. A module
                # may return a sentence explaining what happened; otherwise the label will do.
                done = str(landed) if isinstance(landed, str) and landed else f"{cap.label}: done"
                self._ui(form.set_result, key, True, done)
                self._ui(self._window.notify, done)
            if self._device.capabilities_revision != revision:
                # The write changed the device's shape, not just a value -- a calibration run
                # re-bounds every slider it probed. Repaint the whole page from the new set.
                await self._show_connected(self._device)
        finally:
            self._ui(form.clear_pending, group)

    async def _write_rebooting(self, key: str, value: Any) -> None:
        """Write a setting that restarts the device.

        These can never be confirmed: the link drops *as* the device reboots, so the reply we
        would wait for is exactly what does not arrive. Treating that as failure reported an
        error for a change that had applied, and skipped the reconnect.
        """
        form = self._form_for(key)
        if self._device is None or form is None:
            return
        uid = self._device.info.uid
        group = self._group(key)
        self._ui(form.set_pending, group, key, value)
        failed: Exception | None = None
        try:
            await asyncio.wait_for(self._device.set(key, value), WRITE_TIMEOUT)
        except (TimeoutError, Unreachable, OSError) as exc:
            log.info("%s: link dropped applying %s (expected on reboot): %s", uid, key, exc)
        except Exception as exc:  # noqa: BLE001 - reported below, after the device is back
            log.exception("reboot write failed for %s", key)
            failed = exc

        self._ui(form.clear_pending, group)
        if failed is None:
            self._ui(form.set_value, key, value, confirmed=True)
        else:
            self._ui(self._window.notify, f"Could not apply: {failed}", "error")

        # Reconnect either way. A write that restarts a device cannot report whether it applied --
        # the reply and the link go together -- so an error here means "we do not know", not "it
        # did not happen". Returning early on it left the device closed and the user pressing
        # Connect by hand, which is exactly the bug this path exists to avoid. Reopening also
        # re-reads the real values, which is what settles the question.
        await self._reconnect_after_reboot(uid)

    async def _reconnect_after_reboot(self, uid: str) -> None:
        """Close the dead handle, wait for the device to come back, then reopen it.

        Waiting for the device rather than for the clock. A YubiKey re-enumerates in a second or
        two and a headset takes longer; sleeping the worst case made the quick one feel broken.
        """
        self._reconnecting_uids.add(uid)
        self._ui(self._refresh_connection_ui)
        name = self._selected.name if self._selected else "Device"
        self._ui(self._window.sidebar.set_status, f"{name} restarting…")
        await self._teardown(uid)
        try:
            returned = await self._await_return(uid)
        finally:
            self._reconnecting_uids.discard(uid)
            self._ui(self._refresh_connection_ui)

        if not returned:
            self._ui(self._window.sidebar.set_status, self._idle_status)
            self._ui(self._window.notify, f"{name} has not come back yet", "warning")
            return
        # Only if it is still the device on screen. Reopening something the user has navigated
        # away from would take a channel they did not ask for.
        if self._selected is not None and self._selected.uid == uid:
            log.info("reconnecting to %s after restart", uid)
            await self._connect(uid)
        else:
            self._ui(self._window.sidebar.set_status, self._idle_status)

    async def _await_return(self, uid: str) -> bool:
        """Wait for *uid* to be enumerable again.

        Hotplug normally re-enumerates first and this returns on the next look. The explicit sweep
        is the fallback for a machine without ``pyudev``, and costs about 30 ms.

        A device put back in a *different* USB port comes back under a different uid -- the uid is
        the port path for anything with no serial number, deliberately, so settings follow a
        socket. It is then a new row and will not be reopened automatically. Guessing that a device
        which appeared elsewhere is the one we were configuring is how you write to the wrong key.
        """
        loop = asyncio.get_running_loop()

        # Wait for it to go before waiting for it to come back. ``self._devices`` holds whatever
        # the last sweep saw, which still lists the device we have just told to restart -- so
        # asking "is it back?" straight away answers yes, against stale data, and reopens a device
        # mid-reset. That fails with ENODEV and leaves the user pressing Connect by hand.
        dropped = loop.time() + REBOOT_DROP_TIMEOUT
        while loop.time() < dropped:
            await self.enumerate()
            if not self._is_back(uid):
                break
            await asyncio.sleep(REBOOT_POLL)
        else:
            log.info("%s: never left the bus; treating it as still present", uid)
            return self._is_back(uid)

        deadline = loop.time() + REBOOT_RECONNECT_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(REBOOT_POLL)
            await self.enumerate()
            if self._is_back(uid):
                return True
        return False

    def _is_back(self, uid: str) -> bool:
        """Present *and* reachable.

        Presence alone is not the signal, and assuming it was would have broken Sony while fixing
        the YubiKey: BlueZ lists every paired device whether it is switched on or not, so a
        restarting headset never leaves the enumeration. Waiting for presence would return
        immediately and reconnect to something still rebooting -- which is what the flat
        fifteen-second sleep this replaced was avoiding.

        For USB, HID and DRM the node exists only while the hardware does, so ``ready`` is true
        whenever the device is found and this behaves exactly as presence would.
        """
        device = self._by_uid(uid)
        return device is not None and device.ready

    # ---------------------------------------------------------------- polling

    async def _poll_loop(self, device: Device) -> None:
        """Re-read the device periodically. Values with a write in flight are dropped by the
        form, so a poll can never repaint a control mid-change."""
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                values = await asyncio.wait_for(device.refresh(), WRITE_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("periodic refresh failed", exc_info=True)
                continue
            advisories = device.advisories()

            def apply(values: dict[str, Any] = values, advisories: Any = advisories) -> None:
                for form in self._window.page.forms().values():
                    for key, value in values.items():
                        form.set_value(key, value)
                    form.set_advisories(advisories)

            self._ui(apply)

    async def _watch_changes(self, device: Device) -> None:
        """Apply values the device pushed on its own.

        Without this, ``Device.changes()`` is an API nothing consumes. Sony does not override it,
        so the gap never showed -- but a Poly headset polls nothing at all and reports a mute
        button press as an unsolicited event, which makes this the *only* route by which a change
        made on the device reaches the screen.

        Values for a key with a write in flight are dropped by the form, exactly as poll results
        are, so an event cannot repaint a control mid-change.

        A push may also change the device's *shape*, not just a value -- a YubiKey reads its OTP
        slots in the background after connecting, because doing it during the handshake would put
        a three-second USB interface hand-over in front of the whole page. The same revision check
        the write path uses applies here, so a set that grew or shrank repaints rather than
        leaving controls that no longer match the device.
        """
        revision = device.capabilities_revision
        try:
            async for change in device.changes():
                if self._device is not device:
                    continue  # a different device is on screen; its own form will catch up

                if device.capabilities_revision != revision:
                    revision = device.capabilities_revision
                    await self._show_connected(device)

                def apply(change: Any = change) -> None:
                    # Offered to every form, not only the one that owns a row with this key: a
                    # value can be a *suffix source*, shown after somebody else's reading and
                    # having no row of its own. `set_value` ignores a key a form neither owns nor
                    # depends on, so this stays cheap and cannot paint the wrong tab.
                    for form in self._window.page.forms().values():
                        form.set_value(change.key, change.value)
                        form.set_advisories(device.advisories())

                self._ui(apply)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: change stream stopped", device.info.uid)

    # ---------------------------------------------------------------- photos

    def _refresh_photo(self) -> None:
        path = photos.cached(self._selected.uid) if self._selected else None
        fetchable = self._device is not None and (
            type(self._device).fetch_photo is not Device.fetch_photo
        )
        self._window.page.set_photo(str(path) if path else None, fetchable)

    def _choose_photo(self) -> None:
        if self._selected is None:
            return
        chosen = self._window.choose_image()
        if not chosen:
            return
        path = Path(QUrl(chosen).toLocalFile() or chosen)
        if photos.store_file(self._selected.uid, path) is None:
            self._window.notify("That file is not a usable image", "warning")
            return
        self._refresh_photo()

    def _clear_photo(self) -> None:
        if self._selected is not None:
            photos.remove(self._selected.uid)
            self._refresh_photo()

    def _fetch_photo(self) -> None:
        self._bridge.spawn(self._fetch_photo_async(), label="fetch-photo")

    async def _fetch_photo_async(self) -> None:
        if self._device is None or self._selected is None:
            return
        uid = self._selected.uid
        try:
            payload = await asyncio.wait_for(self._device.fetch_photo(), OP_TIMEOUT * 4)
        except Exception as exc:
            log.debug("photo fetch failed for %s: %s", uid, exc)
            self._ui(self._window.notify, "Could not reach the vendor's image service", "warning")
            return
        if payload is None:
            # A normal answer: plenty of models publish no photo.
            self._ui(self._window.notify, "The vendor publishes no photo for this model")
            return
        if photos.store_bytes(uid, payload) is None:
            self._ui(self._window.notify, "The vendor returned something that is not an image",
                     "warning")
            return
        self._ui(self._refresh_photo)

    # ---------------------------------------------------------------- helpers

    def _refresh_connection_ui(self) -> None:
        # Both states belong to a device, and the page only ever shows one. A device opening in
        # the background must not disable the button of the device on screen.
        self._window.page.set_connection(
            connected=self.connected,
            busy=self._selected_in(self._busy_uids),
            reconnecting=self._selected_in(self._reconnecting_uids),
        )
        # Every open device shows as connected, not just the one on screen -- with several
        # monitors open at once the sidebar dot is the only thing saying which.
        open_now = frozenset(self._open)
        if open_now != self._painted_open:
            self._painted_open = open_now
            self._window.sidebar.reconcile(self._visible())

    def _busy(self, uid: str, opening: bool) -> None:
        """Mark one device as opening, or no longer opening. Others are untouched."""
        if not uid:
            return
        if opening:
            self._busy_uids.add(uid)
        else:
            self._busy_uids.discard(uid)
        self._ui(self._refresh_connection_ui)

    def _selected_in(self, uids: set[str]) -> bool:
        return self._selected is not None and self._selected.uid in uids

    async def shutdown(self) -> None:
        self._stop_hotplug()
        for uid in list(self._open):
            await self._teardown(uid)
