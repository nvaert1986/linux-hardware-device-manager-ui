"""One interface over two ways into the same controller.

USB is preferred wherever both are available, and not merely because it is faster: the 532-byte
record is readable over it, so the rolling checksum's previous value is known exactly rather than
remembered. BLE is what remains for a controller that is not plugged in.

The read paths differ in shape, which is the whole reason this layer exists:

============  ==========================================  ==============================
Transport     Read                                        Header / checksum
============  ==========================================  ==============================
USB (GIP)     the whole 532-byte record                   read from the record
BLE (82CE)    three 176-byte slots, one at a time         from :mod:`..store`, or unknown
============  ==========================================  ==============================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import store
from ..protocol import SEED_CRC, SlotConfig, SuperConfig
from ..protocol import fieldmap as fm
from . import ble, usb

log = logging.getLogger(__name__)


class TransportError(Exception):
    """Raised for anything a caller can act on. Wraps both backends' own errors."""


#: Kept as an empty string, and deliberately not deleted.
#:
#: This port refused to write over Bluetooth until the controller had been read once over USB, on
#: the reasoning that the checksum chain needs the value currently on the device and BLE cannot
#: read the header. The premise turned out to be wrong -- the chain starts from a fixed seed, not
#: from the device -- so the restriction was inventing a limitation neither the vendor app nor the
#: source project has. The name survives so an advisory keyed on it becomes silent rather than
#: raising, and so this note has somewhere to live.
UNKNOWN_CHECKSUM = ""


@dataclass(frozen=True, slots=True)
class Link:
    """How to reach one controller, and which of the two ways this is."""

    kind: str
    """``"usb"`` or ``"ble"``."""

    ident: str
    """A USB serial, or a Bluetooth address."""

    product_id: int | None = None

    @property
    def key(self) -> str:
        """Cache key for :mod:`..store`. Stable across transports for the same unit only when it
        has a USB serial, which is the common case and the one that matters."""
        return f"{self.kind}:{self.ident}"


def read(link: Link) -> tuple[SuperConfig, int | None]:
    """``(configuration, checksum currently on the device)``.

    The second value is None only on BLE for a controller never seen over USB. Callers must treat
    that as "cannot write yet" rather than substituting a guess.
    """
    if link.kind == "usb":
        try:
            raw = usb.read_super(link.product_id, link.ident)
        except (usb.TransportError, OSError) as exc:
            raise TransportError(str(exc)) from exc
        config = SuperConfig(raw)
        # A USB read is the authoritative source for the checksum, so record it for the BLE path.
        store.remember(link.key, config.crc)
        if link.ident:
            store.remember(f"ble:{link.ident}", config.crc)
        return config, config.crc

    try:
        slots = ble.read_slots(link.ident)
    except (ble.TransportError, OSError) as exc:
        raise TransportError(str(exc)) from exc

    known = store.remembered(link.key)
    # The header cannot be read over BLE. A remembered checksum goes in so the record round-trips
    # unchanged; without one the header is zeroed and `known` stays None, which is what stops a
    # write.
    header = bytes([(known or 0) & 0xFF, ((known or 0) >> 8) & 0xFF, 0, 0])
    config = SuperConfig.from_slots([SlotConfig(s) for s in slots], header)
    return config, known


def write(link: Link, config: SuperConfig, previous: int | None = None) -> int:
    """Write and commit, returning the checksum now on the controller.

    **The checksum chains from a fixed seed, not from the controller.** ``previous`` is accepted
    and ignored; it is kept so the caller and the cache still record what was written.

    This is the correction to the port's central mistake. Reading the controller's own header and
    chaining from it is the obviously right thing to do and it does not work: the controller takes
    the record, reports the new mapping for as long as it stays powered, and comes back with the
    old one. The vendor app and the hardware-validated project both replay the same two header
    bytes and the same seed on every save, and that is the only sequence known to survive a power
    cycle. See :data:`..protocol.record.SEED_CRC`.

    One consequence worth having: nothing about a write now depends on having read the device, so a
    controller can be configured over Bluetooth without ever having been plugged in -- which the
    source project could always do and this port had taken away.
    """
    record = config.to_bytes(previous_crc=SEED_CRC, seed_header=True)
    try:
        if link.kind == "usb":
            usb.write_super(record, link.product_id, link.ident)
        else:
            ble.write_super(record, link.ident)
    except (usb.TransportError, ble.TransportError, OSError) as exc:
        raise TransportError(str(exc)) from exc

    checksum = int.from_bytes(record[:2], "little")
    store.remember(link.key, checksum)
    if link.kind == "usb" and link.ident:
        store.remember(f"ble:{link.ident}", checksum)
    return checksum


def default_slot(config: SuperConfig) -> SlotConfig:
    """A fresh profile, cloned from whichever slot already holds one.

    Better than a synthetic default: the identity mapping is whatever this controller's own
    firmware wrote, and copying it means never inventing a record the device has not seen. Falls
    back to slot 0 if none is written, which then reads as empty and is handled upstream.
    """
    for slot in config.slots:
        if slot.written:
            return slot.clone()
    return config.slots[0].clone()


__all__ = ["Link", "TransportError", "UNKNOWN_CHECKSUM", "ble", "default_slot", "read", "usb",
           "write", "fm"]
