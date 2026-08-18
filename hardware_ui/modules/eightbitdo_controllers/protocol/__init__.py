"""Byte layout and framing for 8BitDo Xbox wired controllers.

Transport-independent on purpose. The same 532-byte record, field map, rolling checksum and inner
message format are used whether they are carried over USB GIP or over the controller's hidden BLE
config radio, which is why the two backends are thin.
"""

from . import fieldmap, gip, message, record
from .fieldmap import CODE, CODE_TO_NAME, REMAPPABLE, SLOT_COUNT, SLOT_LEN, SUPER_LEN
from .record import (
    HEADER_TAIL,
    SEED_CRC,
    SlotConfig,
    SuperConfig,
    empty_slot,
    mcrf4xx,
    roll_crc,
)

__all__ = ["CODE", "CODE_TO_NAME", "REMAPPABLE", "SLOT_COUNT", "SLOT_LEN", "SUPER_LEN",
           "SlotConfig", "SuperConfig", "empty_slot", "fieldmap", "gip", "mcrf4xx", "message",
           "record", "roll_crc"]
