"""Where a device is, in a few words, for the line under its name in the list.

Two identical devices are the normal case. Two P2425Ds have the same EDID name; two BT700 adapters
have the same USB product string; two YubiKeys of a model are indistinguishable until you read a
serial. The name alone cannot say which physical thing a row is, so the row carries a second line
that can -- ``Connection: DP-3``, ``Connection: via BT700 · S/NFH39CL``.

**When it can be answered differs by transport, which is the whole reason this is a class.** DRM
hands a display its connector before anything is opened, so that label exists at enumeration. A
USB descriptor says nothing about whether an adapter has a headset behind it, so that label only
exists once the device is open. Both end up on the same line, from opposite ends of the lifecycle.

A module composes one from :class:`ConnectionLabel`, whose parts are deliberately plain: a *route*
saying how the device is reached and an *identifier* telling it from its twin. Either may be
empty, and a module that supplies neither loses nothing -- the row is exactly as it was.
"""

from __future__ import annotations

from dataclasses import dataclass

SEPARATOR = " · "
"""Between route and identifier. A middle dot, because the row is narrow and a dash reads as a
hyphen inside model numbers like ``WH-1000XM4``."""

PREFIX = "Connection"
"""Named rather than bare: "DP-3" on its own reads as part of the model number."""


@dataclass(frozen=True, slots=True)
class ConnectionLabel:
    """One device's answer to "which one is this, and how is it attached?".

    Both parts are optional on purpose. A device that will not give a serial still gets a useful
    row from its route alone, which beats a row that says nothing and beats one that says ``None``.
    """

    route: str = ""
    """How the device is reached -- ``"Bluetooth"``, ``"USB"``, ``"via Poly BT700"``, ``"DP-3"``.

    Composed from what is structurally true, never from a table of models: whether a session had
    to hop through something, which transport is in use, what the thing it is plugged into calls
    itself. That is what makes it correct for hardware nobody has tested.
    """

    identifier: str = ""
    """What tells this device from an identical one -- usually a serial number."""

    def __bool__(self) -> bool:
        return bool(self.route or self.identifier)

    def __str__(self) -> str:
        if self.route and self.identifier:
            return f"{self.route}{SEPARATOR}{self.identifier}"
        return self.route or self.identifier

    def display(self) -> str:
        """The full line as the list shows it, or ``""`` when there is nothing to say."""
        return f"{PREFIX}: {self}" if self else ""


def from_connector(connector: str) -> ConnectionLabel:
    """``card1-DP-3`` -> a label reading ``DP-3``.

    The one case answerable before a device is opened: DRM names the connector, and for two
    identical panels it is the only thing that distinguishes the rows.
    """
    return ConnectionLabel(route=connector.split("-", 1)[1]) if "-" in connector else NONE


NONE = ConnectionLabel()
"""Nothing to say. The default for every device that does not override it."""


__all__ = ["NONE", "ConnectionLabel", "from_connector"]
