"""Device identity, connection lifecycle, and the interface every module implements.

The split that keeps startup fast lives here:

``DeviceInfo``
    Produced by *enumeration* -- a sysfs walk or a BlueZ property read. Costs microseconds and
    never opens the device. Enough to match a module and paint a sidebar row.

``Device``
    Produced by *probing* -- opening hidraw, connecting RFCOMM, reading I2C. Costs anywhere from
    milliseconds to seconds and can hang. Only ever created when the user selects the device.

Nothing in the enumeration path may import a module's protocol code, and nothing may call
``connect()`` on the user's behalf at startup.
"""

from __future__ import annotations

import abc
import enum
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import connection
from . import interaction as interaction_module
from .capability import Advisory, CapabilitySet, CapabilityValue
from .connection import ConnectionLabel
from .diagram import Diagram


class Transport(enum.StrEnum):
    USB = "usb"
    HID = "hid"
    BLUETOOTH = "bluetooth"
    """Bluetooth Classic, i.e. RFCOMM. Not reachable from a browser, hence this project."""
    BLE = "ble"
    DISPLAY = "display"
    """DDC/CI over the I2C bus behind a DRM connector."""


class Category(enum.StrEnum):
    """Top-level grouping in the sidebar. Keep this list short.

    The value is both the manifest's ``category = "..."`` and, uppercased, the sidebar heading,
    so it is written the way it should read: ``security_keys`` becomes "SECURITY KEYS".
    """

    AUDIO = "audio"
    DISPLAY = "display"
    INPUT = "input"
    DOCKS = "docks"
    """Docking stations and port replicators. Not input: a dock is not something you type on, and
    filing one under INPUT put a Thunderbolt dock next to a keyboard."""

    SECURITY_KEYS = "security_keys"
    """FIDO2 / U2F authenticators and other security tokens."""

    OTHER = "other"

    @property
    def icon(self) -> str:
        return {
            Category.AUDIO: "audio-headset",
            Category.DISPLAY: "video-display",
            Category.INPUT: "input-gaming",
            Category.DOCKS: "preferences-desktop-thunderbolt",
            Category.SECURITY_KEYS: "application-pgp-keys",
            Category.OTHER: "preferences-desktop-peripherals",
        }[self]


class State(enum.StrEnum):
    """Connection lifecycle, as shown by the dot next to a sidebar row."""

    UNKNOWN = "unknown"
    """Enumerated but never probed. The normal state at startup."""

    ABSENT = "absent"
    """Known from cache but not present in this enumeration."""

    PRESENT = "present"
    """Enumerated and physically available, not yet opened.

    Correct for USB, hidraw and DRM, where the node exists only while the hardware does, and for a
    Bluetooth device BlueZ reports as connected -- that means switched on and linked to this
    machine, which is exactly "available, not yet opened". A paired headset that is switched off is
    :attr:`PAIRED` instead, because being in BlueZ's list says nothing about reachability."""

    PAIRED = "paired"
    """Known to BlueZ and paired, but not currently connected.

    The normal resting state of a headset that is switched off or out of range. Distinct from
    :attr:`PRESENT` because it is *not* openable, and distinct from :attr:`ABSENT` because it
    genuinely is in this enumeration."""

    CONNECTING = "connecting"

    CONNECTED = "connected"
    """**This application** has an open session with the device.

    Not "the operating system has a link to it". Enumeration must never set this: it is written by
    the shell for devices it currently holds open, and it is what the green dot reports. Conflating
    the two put a green dot on every switched-on Bluetooth headset before anyone had pressed
    Connect, which is not what the dot means anywhere else in the application."""

    FAILED = "failed"


class Support(enum.StrEnum):
    """How much confidence there is that a module actually works for this device."""

    VERIFIED = "verified"
    """Someone has this hardware and has exercised the module against it."""

    FAMILY = "family"
    """Matches a family rule. The protocol should apply, but nobody has tested this model."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Everything known about a device without opening it.

    ``uid`` must be stable across reboots and reconnects -- it keys the cache, per-device settings,
    pinned capabilities and the "don't ask again" flag. Prefer a MAC address or USB serial; fall
    back to a hash of the stable-looking parts of the sysfs path.
    """

    uid: str
    name: str
    transport: Transport
    category: Category = Category.OTHER

    vendor_id: int | None = None
    product_id: int | None = None
    address: str = ""
    """Bluetooth MAC, for ``Transport.BLUETOOTH`` and ``Transport.BLE``."""

    serial: str = ""
    path: str = ""
    """Device node or sysfs path, e.g. ``/dev/hidraw3`` or ``/sys/class/drm/card1-DP-1``."""

    uuids: frozenset[str] = frozenset()
    """Advertised Bluetooth service UUIDs. The strongest match signal for RFCOMM devices."""

    properties: Mapping[str, Any] = field(default_factory=dict)
    """Transport-specific extras: EDID fields, USB interface classes, BlueZ properties."""

    icon_name: str = ""
    """freedesktop icon overriding the category's, when the transport can tell what this is.

    A category icon is right for a family and wrong for a device: "input" covers a keyboard and a
    mouse, and showing the same picture for both is exactly the information a user needs. Empty
    falls back to the category, which is correct whenever the type is unknown.
    """

    module_id: str = ""
    """Filled in by the registry once a manifest claims this device. Empty means unsupported."""

    connection: ConnectionLabel = connection.NONE
    """Where this device is -- see :mod:`hardware_ui.core.connection`.

    Set at enumeration where the transport can say (a display's DRM connector) and replaced by
    :meth:`Device.connection_label` once the device is open, where it cannot.
    """

    support: Support = Support.FAMILY
    state: State = State.UNKNOWN

    @property
    def supported(self) -> bool:
        return bool(self.module_id)

    @property
    def ready(self) -> bool:
        """Whether the device can actually be opened right now.

        The distinction matters for Bluetooth and only for Bluetooth: BlueZ lists every *paired*
        device whether or not it is switched on, so "present in the enumeration" does not imply
        "reachable". A USB or hidraw node, by contrast, exists only while the hardware is plugged
        in, and a DRM connector reports its own connected status.

        Used to dim unreachable rows and sort them below live ones, so a powered-off headset no
        longer looks identical to one you can configure.

        One rule for every transport. It used to special-case Bluetooth as "state is CONNECTED",
        which worked only because enumeration was misusing CONNECTED to mean "BlueZ has a link".
        Encoding reachability where the transport's quirks are already known -- in the enumerator
        -- lets this stay a single comparison.
        """
        return self.state in (State.PRESENT, State.CONNECTED)

    @property
    def icon(self) -> str:
        return self.icon_name or self.category.icon


class DeviceError(Exception):
    """Base for module-raised failures the shell should surface rather than crash on."""


class Unreachable(DeviceError):
    """The device is known to the system but cannot be opened right now.

    Routine, not a defect: a paired headset that is switched off still appears in BlueZ, and
    connecting yields EHOSTDOWN. Modules should raise this instead of letting the transport error
    escape, so the shell can say "turn it on" rather than logging a traceback nobody can act on.
    """


class DependencyMissing(DeviceError):
    """A module needs software that is not installed, so this device cannot be opened.

    Distinct from :class:`Unreachable`, which means the hardware is not answering. The difference
    is what the user must do: plug something in and try again, versus install a package. It is
    also why the shell shows this message **verbatim** -- these carry install instructions, and
    wrapping them produced "…is OpenRazer is needed for Razer devices and is not installed. …
    Switch it on, then Rescan."

    Every module that needs optional software raises this from ``connect()``, so nothing outside
    that module ever imports it and an installation without it runs normally.
    """


class NotSupported(DeviceError):
    """The device rejected a capability that its catalogue claimed to have.

    Expected on ``Support.FAMILY`` hardware. The shell disables that single row and carries on;
    it must never abort the page.
    """


class Device(abc.ABC):
    """A live connection to one device. Modules implement this.

    Instances are created only when the user opens the device, and everything here may block for
    a meaningful time, so it is all async. The shell always applies a timeout -- an implementation
    that hangs will be cancelled, not waited on.
    """

    interaction: Any = interaction_module.SILENT
    """How this device talks to the user *during* an operation -- see
    :mod:`hardware_ui.core.interaction`.

    Defaults to a null implementation so a module can call it unconditionally. The shell replaces
    it with something that draws; the CLI and the tests leave it alone.
    """

    def __init__(self, info: DeviceInfo) -> None:
        self.info = info
        self._caps_revision = 0

    @property
    def capabilities_revision(self) -> int:
        """Bumped by the module whenever :attr:`capabilities` has been rebuilt.

        Most devices never change shape once connected, so the default never moves. DDC/CI does:
        a range calibration discovers the real minimum and step of five controls at once, and the
        sliders must be re-bounded or they keep offering values the panel will refuse. The shell
        compares this after every write and rebuilds the page when it has changed -- cheaper and
        far less error-prone than a module reaching into the form itself.
        """
        return self._caps_revision

    def _bump_capabilities(self) -> None:
        self._caps_revision += 1

    @property
    @abc.abstractmethod
    def capabilities(self) -> CapabilitySet:
        """What this device can do.

        Discover this from the device where the protocol allows rather than hardcoding per model.
        Discovery is what lets a ``Support.FAMILY`` match work on hardware nobody has tested.
        Valid only after :meth:`connect`.
        """

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the transport and perform any session handshake."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close cleanly. Must be safe to call when never connected, and must not raise."""

    @abc.abstractmethod
    async def get(self, key: str) -> Any:
        """Read one capability. Raise :class:`NotSupported` if the device rejects it."""

    @abc.abstractmethod
    async def set(self, key: str, value: Any) -> Any | None:
        """Write one capability.

        Return once the device has confirmed, so the shell can resolve its optimistic update. If
        the protocol offers no confirmation, read back before returning.

        Return ``None`` when the device took the value as sent -- the common case. Return the
        **value the device actually landed on** when it differs and that still counts as success;
        the shell paints that instead of the requested value. DDC/CI forces this: a Dell panel
        quantises sharpness to steps of ten, so a request of 55 applies as 50. Painting 55 over a
        monitor sitting at 50 is the same class of lie as reporting failure for a change that took.
        """

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Read several capabilities.

        The default is sequential because most of these protocols are a single serialised channel.
        Override where the protocol supports batching -- it is the difference between a page that
        appears at once and one that fills in row by row.
        """
        out: dict[str, Any] = {}
        for key in keys:
            try:
                out[key] = await self.get(key)
            except NotSupported:
                continue
        return out

    #: Seconds the shell allows :meth:`connect` before giving up. Overridden by modules that must
    #: *discover* what a device supports rather than being told: a Jabra link is probed property by
    #: property at both of its endpoints, and a genuinely cold connect measured ~30 s per endpoint
    #: -- which the 60 s default would fail right at the finish line.
    connect_timeout: float = 60.0

    def connect_notice(self) -> str:
        """One line shown while this device opens, or "" for the usual silent connect.

        For devices where opening is slow *by design*. Waiting with no explanation reads as a hang:
        the first Jabra connect probes hundreds of properties and a user watching "Connecting…" for
        a minute will reasonably assume it has stuck and cancel it.
        """
        return ""

    def connection_label(self) -> ConnectionLabel:
        """Where this device is, for the line under its name in the list.

        Answered here, after connecting, because for most transports that is when it becomes
        knowable -- nothing in a USB descriptor says whether an adapter has a headset behind it.
        A display is the exception and fills the same line in at enumeration; see
        :mod:`hardware_ui.core.connection`.

        The default says nothing, which leaves the row exactly as it was.
        """
        return connection.NONE

    def advisories(self) -> dict[str, Advisory]:
        """State-dependent messages and locks, keyed by capability.

        Consulted after every read, so it may change as the device's state does. Cheap and
        synchronous: it must only inspect state already held, never touch the transport.
        """
        return {}

    def diagrams(self) -> dict[str, Diagram]:
        """Drawings of the hardware, keyed by the :attr:`Capability.section` each one covers.

        For settings that name a *place on the device*: "Left paddle", "D-pad Up", "Zone 2". A
        column of dropdowns is a list of names, and every vendor configurator for a controller, a
        mouse or a keyboard draws the device instead.

        Data only -- a file path and a table of fractions. The shell owns every decision about
        how it is drawn, and a section with no diagram renders as an ordinary form, so this is
        never load-bearing: a module whose drawing is missing, or a desktop without Qt's SVG
        support, loses the picture and keeps every control.

        Synchronous and cheap for the same reason as :meth:`advisories`. The default has none.
        """
        return {}

    async def fetch_photo(self) -> bytes | None:
        """Download this device's product photo from the vendor, if one is published.

        Called only when the user explicitly asks. Two rules, both learned the hard way in the
        Jabra work (see :mod:`hardware_ui.core.photos`):

        **Follow the vendor's advertised asset links.** Jabra's device-configuration service names
        the image URLs; guessing a CDN pattern is fragile and a far weaker position than asking
        for exactly what the vendor's own client asks for.

        **Return None when there is nothing.** An Evolve2 85 answers 200 with an empty asset list
        and 404s on every image URL. That is a normal answer, not an error, and must never be
        cached as a picture.

        The default publishes nothing, which is correct for any module without a known endpoint.
        """
        return None

    async def refresh(self) -> dict[str, Any]:
        """Cheap periodic re-read of values the device will never push.

        Some values do not arrive as notifications because they do not come from the device's
        own protocol at all -- the active A2DP codec and the battery level are read from BlueZ,
        so nothing will ever tell us they changed.

        **This must stay cheap and must not generate protocol traffic.** The Sony reference
        implementation is explicit: on LDAC the config channel has almost no spare bandwidth, and
        a periodic burst of MDR reads flaps the codec and the link. Poll the host-side sources;
        let the device push everything else.

        Returns only the keys it refreshed. The default polls nothing.
        """
        return {}

    def changes(self) -> AsyncIterator[CapabilityValue]:
        """Values the device pushed on its own.

        This is what keeps the view honest when someone changes a setting from the vendor's phone
        app, or presses a button on the headset, while we are connected. Modules whose protocol
        has no notifications may leave the default, which yields nothing.
        """

        async def _none() -> AsyncIterator[CapabilityValue]:
            return
            yield  # pragma: no cover -- makes this an async generator

        return _none()

    async def __aenter__(self) -> Device:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()
