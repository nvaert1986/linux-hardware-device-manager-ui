"""GNP command groups, NACK codes and target addresses.

Command names come from GnProtocol.GnpCommands (six of them). The other four groups are used
by @gnaudio/jabra-properties-definition but have no vendor-supplied name, so they are left
numeric — see docs/JABRA_GNP_PROTOCOL.md §5.
"""
from __future__ import annotations

import enum

#: GN Netcom / GN Audio USB vendor id.
VENDOR_ID = 0x0B0E

#: Command reserved for negative acknowledgements. A reply carrying this is never data.
COMMAND_NACK = 0xFE


class Command(enum.IntEnum):
    """Named groups from GnProtocol.GnpCommands."""

    IDENT = 0x02
    DFU = 0x07
    DEVICE = 0x0D
    FWU = 0x0F
    STATUS = 0x12
    CONFIG = 0x13


#: Groups the ISC property catalogue uses that GnProtocol.GnpCommands does not name.
#: Counts are distinct subcommands observed in properties.json.
UNNAMED_COMMANDS = {0x03: 1, 0x23: 4, 0x26: 25, 0x29: 4}


def command_name(value: int) -> str:
    try:
        return Command(value).name
    except ValueError:
        if value == COMMAND_NACK:
            return "NACK"
        return f"CMD_0x{value:02X}"


class Nack(enum.IntEnum):
    """NackSubcommand — the first payload byte of a NACK reply."""

    NO_ERROR_CODE = 0x00
    BAD_STATE = 0xF3
    TRANSMIT_FAILED = 0xF4
    ILLEGAL_ADDR = 0xF5
    UNKNOWN_CMD = 0xF6
    FILE_NOT_FOUND = 0xF7
    INTERNAL_ERROR = 0xF8
    REPLY_ERROR = 0xF9
    ILLEGAL_PARAM = 0xFA
    OUT_OF_SEQUENCE = 0xFB
    NO_SPACE = 0xFC
    ACCESS_DENIED = 0xFD
    NO_MORE_DATA = 0xFE
    UNKNOWN_SUB_CMD = 0xFF


NACK_DESCRIPTIONS = {
    Nack.NO_ERROR_CODE: "No error code",
    Nack.BAD_STATE: "Bad state",
    Nack.TRANSMIT_FAILED: "Transmit failed",
    Nack.ILLEGAL_ADDR: "Illegal address",
    Nack.UNKNOWN_CMD: "Unknown command",
    Nack.FILE_NOT_FOUND: "File not found",
    Nack.INTERNAL_ERROR: "Internal error",
    Nack.REPLY_ERROR: "Reply error",
    Nack.ILLEGAL_PARAM: "Illegal parameter",
    Nack.OUT_OF_SEQUENCE: "Out of sequence",
    Nack.NO_SPACE: "No space",
    Nack.ACCESS_DENIED: "Access denied",
    Nack.NO_MORE_DATA: "No more data",
    Nack.UNKNOWN_SUB_CMD: "Unknown sub-command",
}

#: A device that does not implement a subcommand should answer with one of these rather than
#: misbehaving, which is what makes capability probing survivable. Unverified on hardware.
NACK_UNSUPPORTED = frozenset({Nack.UNKNOWN_CMD, Nack.UNKNOWN_SUB_CMD})
#: Locked by device policy rather than absent — surface this to the user, do not retry.
NACK_REFUSED = frozenset({Nack.ACCESS_DENIED, Nack.BAD_STATE})


def nack_description(code: int) -> str:
    try:
        return NACK_DESCRIPTIONS[Nack(code)]
    except (ValueError, KeyError):
        return f"Unknown NACK subcommand 0x{code:02X}"


class Target(enum.IntEnum):
    """Values of the optional `address` field in the ISC property catalogue.

    Derived from which properties carry each value — `baseName` at 1, `camera*` at 2,
    `controller*` at 3, `secondaryDevice*` at 4. 636 of the 796 GNP steps omit the field
    entirely, meaning the primary device.

    Note this is *not* the SDK's `enumSubDevice` (which is 0-based and ordered differently);
    that is a higher-level abstraction, not the wire address.
    """

    BASE = 0x01
    CAMERA = 0x02
    CONTROLLER = 0x03
    SECONDARY = 0x04


def target_name(value: int | None) -> str:
    if value is None:
        return "primary"
    try:
        return Target(value).name.lower()
    except ValueError:
        return f"addr_0x{value:02X}"
