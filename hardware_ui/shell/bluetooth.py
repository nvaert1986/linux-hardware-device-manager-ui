"""Bluetooth hotplug: BlueZ telling us a device appeared, vanished, or came on.

The other half of :func:`hardware_ui.core.discovery.watch`, which covers USB, HID and DRM through
udev. A Bluetooth device emits no uevent -- it lives on the system bus -- so this listens there
instead.

**Why it is in the shell and not in ``core``.** ``core`` imports no Qt, and ``hardware_ui.cli``
depends on that: a headless diagnostic run must not drag in a GUI toolkit. Subscribing is a
shell concern anyway -- the shell owns the event loop that the subscription needs -- so this lives
here and ``core`` stays clean. Enumeration is untouched and still uses ``dbus_fast`` or
``bluetoothctl``.

**Why QtDBus and not QtBluetooth.** ``QBluetoothLocalDevice`` has ``deviceConnected`` and
``deviceDisconnected``, which sounds like exactly this, and it works: verified on a live adapter
once ``pyqt6-6.11.0-r1`` fixed a signature mismatch that had made the module unimportable.

It is still the wrong tool, for the same reason it would have been before. On Linux it is
implemented over these very BlueZ signals and exposes a strict subset: two signals plus
``pairingFinished``, with no ``InterfacesAdded`` or ``InterfacesRemoved``, so pairing a new device
or removing one would not refresh the list -- and no way to filter on which property changed, which
is what keeps RSSI updates from re-sweeping every transport several times a second.

Neither costs a dependency; both ship with the pyqt6 this application already requires. The choice
is only about how much BlueZ says.

Three signals matter, and the third is the one people actually notice:

``InterfacesAdded``    a device became known -- newly paired, or back in range
``InterfacesRemoved``  unpaired or forgotten
``PropertiesChanged``  ``Connected`` flipped -- a headset switched on or off

That last one is why this is not simply "udev for D-Bus". A paired headset is in the list whether
it is on or off; what changes is whether it can be reached. It is a state change on a row that was
already there, not an appearance.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage

log = logging.getLogger(__name__)

SERVICE = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
DEVICE_INTERFACE = "org.bluez.Device1"

SETTLE_MS = 400
"""Quiet period before re-enumerating, in milliseconds.

Matches the udev side, and matters more here: BlueZ emits a flurry of property changes while a
link is being set up, and a headset at the edge of range can bounce.
"""

#: Properties worth waking up for. BlueZ also reports RSSI, transmit power and service data as
#: they change, none of which alters what the list shows.
INTERESTING = frozenset({"Connected", "Paired", "Trusted", "Alias", "Name"})


class BluetoothWatcher(QObject):
    """Emits :attr:`changed` once a burst of BlueZ activity has settled.

    Emitted on the Qt thread, so a slot may hand work to the asyncio side however it likes.
    """

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self.changed)
        self._subscribed = False

    def start(self) -> bool:
        """Subscribe. ``False`` means no BlueZ, which is an ordinary answer on a desktop.

        Failure is not reported as an error anywhere: without it the list refreshes on Rescan,
        exactly as it did before any of this existed.
        """
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            log.info("no system bus; Bluetooth hotplug unavailable")
            return False

        ok = all(
            (
                bus.connect(SERVICE, "/", OBJECT_MANAGER, "InterfacesAdded", self._on_signal),
                bus.connect(SERVICE, "/", OBJECT_MANAGER, "InterfacesRemoved", self._on_signal),
                # Empty path: a device's properties are announced on its own object, and there is
                # one object per device. Subscribing per device would mean re-subscribing every
                # time one appeared -- which is the event we are trying to hear about.
                bus.connect(SERVICE, "", PROPERTIES, "PropertiesChanged", self._on_properties),
            )
        )
        if not ok:
            log.info("BlueZ not answering on the system bus; Bluetooth hotplug unavailable")
            return False
        self._subscribed = True
        log.info("Bluetooth hotplug watching org.bluez")
        return True

    def stop(self) -> None:
        self._settle.stop()
        if not self._subscribed:
            return
        bus = QDBusConnection.systemBus()
        bus.disconnect(SERVICE, "/", OBJECT_MANAGER, "InterfacesAdded", self._on_signal)
        bus.disconnect(SERVICE, "/", OBJECT_MANAGER, "InterfacesRemoved", self._on_signal)
        bus.disconnect(SERVICE, "", PROPERTIES, "PropertiesChanged", self._on_properties)
        self._subscribed = False

    # ---------------------------------------------------------------- signals

    # QtDBus will only deliver to a decorated slot -- the signature is how it decides what to
    # unmarshal into.
    @pyqtSlot(QDBusMessage)
    def _on_signal(self, _message: QDBusMessage) -> None:
        self._settle.start()

    @pyqtSlot(QDBusMessage)
    def _on_properties(self, message: QDBusMessage) -> None:
        """Only for a device, and only for a property that changes what the list shows.

        BlueZ reports RSSI on every advertisement it hears. Re-enumerating for those would mean
        sweeping every transport several times a second for a number nothing displays.
        """
        args = message.arguments()
        if len(args) < 2 or args[0] != DEVICE_INTERFACE:
            return
        changed = args[1] if isinstance(args[1], dict) else {}
        if INTERESTING.isdisjoint(changed):
            return
        log.debug("bluez: %s", ", ".join(sorted(set(changed) & INTERESTING)))
        self._settle.start()


__all__ = ["BluetoothWatcher"]
