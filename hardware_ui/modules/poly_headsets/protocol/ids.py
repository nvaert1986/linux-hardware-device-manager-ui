"""Deckard id tables, extracted from the Poly Lens app's bundled SDK enums.

`Setting` and `Command` are SEPARATE namespaces. 17 settings have a different id for reading than
for writing, and 13 numeric ids mean different things in each table — including 0x0E1C, which is
PARTITION_INFORMATION as a Setting but the destructive REMOVE_PARTITION_INFORMATION as a Command.

Never merge the tables. Resolve reads against `setting`, writes against `command`, by name.
This mirrors com.poly.devicesdk.DeckardIdKt.convertDeckardId, which dispatches on message type.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .framing import COMMAND_TYPES, SETTING_TYPES, MessageType

from hardware_ui.core.paths import vendor_dir

#: Written by the vendor-asset import, not shipped. Absent is survivable -- these names only
#: make log messages readable; every id actually sent comes from the catalogue.
DATA = vendor_dir("poly_headsets") / "deckard_ids.json"


@lru_cache(maxsize=1)
def _tables() -> dict[str, dict[int, str]]:
    """Id -> name tables. Absent or partial is survivable: names are for humans, not the wire.

    The catalogues carry the ids that actually get sent; this only makes output readable.
    """
    try:
        raw = json.loads(DATA.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    tables = {kind: {int(k): v for k, v in table.items()} for kind, table in raw.items()}
    for kind in ("setting", "command", "event", "exception", "message_type"):
        tables.setdefault(kind, {})
    return tables


def table_for_type(message_type: MessageType) -> str | None:
    """Which id table a message type indexes into, or None if the type carries no id."""
    if message_type in SETTING_TYPES:
        return "setting"
    if message_type in COMMAND_TYPES:
        return "command"
    if message_type is MessageType.EVENT:
        return "event"
    return None


def name_for(message_type: MessageType, message_id: int) -> str | None:
    """Human-readable name for an id, resolved in the table that message type uses."""
    table = table_for_type(message_type)
    if table is None:
        return None
    return _tables()[table].get(message_id)


def id_for(table: str, name: str) -> int | None:
    """Look up an id by name in one specific table ('setting', 'command', 'event')."""
    for value, candidate in _tables()[table].items():
        if candidate == name:
            return value
    return None


def exception_name(code: int) -> str | None:
    """Name of a SETTING_RESULT_EXCEPTION / PERFORM_COMMAND_RESULT_EXCEPTION payload code."""
    return _tables()["exception"].get(code)


#: Returned by PERFORM_COMMAND when the device will not write a setting it will happily read.
COMMAND_UNKNOWN = 16

#: Returned when the device does not implement a setting. Safe to probe for — the device answers
#: cleanly rather than resetting, so a query sweep is a valid capability check.
SETTING_UNKNOWN = 18
