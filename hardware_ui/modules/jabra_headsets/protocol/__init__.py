"""Headless GNP protocol implementation — no HID, Bluetooth or Qt dependencies."""

from .framing import (
    HEADER_SIZE,
    MAX_DATA_SIZE,
    MAX_PACKET_SIZE,
    FramingError,
    Packet,
    PacketType,
    SequenceCounter,
)
from .ids import (
    COMMAND_NACK,
    NACK_REFUSED,
    NACK_UNSUPPORTED,
    VENDOR_ID,
    Command,
    Nack,
    Target,
    command_name,
    nack_description,
    target_name,
)

__all__ = [
    "HEADER_SIZE", "MAX_DATA_SIZE", "MAX_PACKET_SIZE",
    "FramingError", "Packet", "PacketType", "SequenceCounter",
    "COMMAND_NACK", "NACK_REFUSED", "NACK_UNSUPPORTED", "VENDOR_ID",
    "Command", "Nack", "Target",
    "command_name", "nack_description", "target_name",
]
