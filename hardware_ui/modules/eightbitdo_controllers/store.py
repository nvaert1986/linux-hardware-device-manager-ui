"""Remembering each controller's checksum, because BLE cannot read it back.

The configuration record's checksum is a rolling chain: the next value is computed over the
*previous* one. Over USB that is a non-problem -- the whole record is readable, so the previous
value is right there. Over BLE only the three 176-byte slots can be read, never the four-byte
header that holds it, so it has to come from somewhere.

Two sources, in order of preference:

1. **A USB read.** Plug the controller in once and the value is known exactly.
2. **What this application last wrote.** After a successful write we know what we put there.

If neither has happened, a BLE write has nothing to chain from. That is reported plainly rather
than guessed at, because a wrong checksum hands the controller a record it cannot validate.

Kept beside the other per-module state under ``config_dir()``, one small JSON file keyed by the
controller's serial or Bluetooth address.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hardware_ui.core.paths import config_dir, ensure

log = logging.getLogger(__name__)

FILENAME = "eightbitdo-checksums.json"

#: Bumped if the file's shape changes incompatibly. A file from the future is ignored rather than
#: misread; a stale checksum is recoverable by plugging in over USB, so nothing is lost.
VERSION = 1


def path() -> Path:
    return config_dir() / FILENAME


def _load() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    entries = data.get("controllers")
    return entries if isinstance(entries, dict) else {}


def remembered(key: str) -> int | None:
    """The checksum last known to be on this controller, or None."""
    value = _load().get(key)
    return value if isinstance(value, int) and 0 <= value <= 0xFFFF else None


def remember(key: str, checksum: int) -> None:
    """Record the checksum now on the controller.

    Called after a USB read and after any successful write. Failure to save is logged and not
    raised: losing the cache costs one USB connect, whereas failing the write the user just made
    because a cache file could not be written is a worse trade.
    """
    entries = _load()
    entries[key] = int(checksum) & 0xFFFF
    try:
        ensure(config_dir())
        path().write_text(json.dumps({"version": VERSION, "controllers": entries}, indent=2))
    except OSError as exc:
        log.warning("could not save the checksum for %s: %s", key, exc)


def forget(key: str) -> None:
    entries = _load()
    if entries.pop(key, None) is None:
        return
    try:
        path().write_text(json.dumps({"version": VERSION, "controllers": entries}, indent=2))
    except OSError as exc:  # pragma: no cover - same reasoning as remember()
        log.warning("could not update the checksum store: %s", exc)


__all__ = ["FILENAME", "VERSION", "forget", "path", "remember", "remembered"]
