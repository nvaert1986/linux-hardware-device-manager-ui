"""Remember which properties a device model supports, so connecting is fast after the first time.

Probing is what makes the first connect slow: ~283 candidate reads, and an unsupported property
that *times out* rather than NACKing costs the full timeout. The set is a property of the model
and firmware, not of the individual unit, so it is cached under this application's own cache
directory, keyed by (product id, firmware).

Deliberately a cache, not config: deleting it only costs one slow connect. It is also versioned,
so a catalogue update invalidates stale entries instead of hiding newly-supported properties.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile

from hardware_ui.core import paths

log = logging.getLogger(__name__)

#: Bump when the probe's meaning changes, so old entries are ignored rather than trusted.
CACHE_VERSION = 1


def cache_dir() -> pathlib.Path:
    return paths.cache_dir() / "jabra_headsets"


def _path(product_id: int, firmware: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in firmware) or "unknown"
    return cache_dir() / f"caps-{product_id:04x}-{safe}.json"


def load(product_id: int | None, firmware: str, catalogue_size: int) -> list[str] | None:
    """Cached supported-property names, or None if there is no usable entry."""
    if product_id is None:
        return None
    path = _path(product_id, firmware)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get("version") != CACHE_VERSION:
        log.debug("%s: cache version %s ignored", path.name, data.get("version"))
        return None
    # A catalogue that has grown may describe properties this entry never probed.
    if data.get("catalogueSize") != catalogue_size:
        log.info("catalogue changed (%s -> %s) — re-probing %04x",
                 data.get("catalogueSize"), catalogue_size, product_id)
        return None
    names = data.get("supported")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    log.info("using cached capabilities for 0x%04X fw %s (%d properties)",
             product_id, firmware, len(names))
    return names


def save(product_id: int | None, firmware: str, catalogue_size: int,
         supported: list[str]) -> None:
    """Write the entry. Failure is logged and ignored — a cache must never break a session."""
    if product_id is None:
        return
    path = _path(product_id, firmware)
    payload = {
        "version": CACHE_VERSION,
        "productId": product_id,
        "firmware": firmware,
        "catalogueSize": catalogue_size,
        "supported": sorted(supported),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace, so a crash mid-write cannot leave a truncated file that then gets
        # trusted on the next run.
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                         encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=1)
            temp_name = tmp.name
        os.replace(temp_name, path)
        log.debug("cached %d capabilities to %s", len(supported), path)
    except OSError as exc:
        log.info("could not write the capability cache: %s", exc)


def forget(product_id: int | None, firmware: str) -> bool:
    """Drop one entry, e.g. after a firmware update. True if something was removed."""
    if product_id is None:
        return False
    try:
        _path(product_id, firmware).unlink()
        return True
    except OSError:
        return False


def entries_for(product_id: int | None) -> list[pathlib.Path]:
    """Cache entries that could serve a link with this USB product id.

    Used to decide whether a connect will be slow. Deliberately keyed on the *link's* id and any
    firmware: the entries are written per GNP endpoint, and the endpoint behind a dongle is not
    known until the link is open — by which point the warning would be too late to give.
    """
    if product_id is None:
        return []
    try:
        return sorted(cache_dir().glob(f"caps-{product_id:04x}-*.json"))
    except OSError:
        return []


#: Stored beside the supported-property list: what this model *refuses*, learned from writes.
#:
#: GN Audio's catalogue describes their whole range, so it offers values a given model does not
#: have -- an Evolve2 85 declares hearThroughLevel −12..6 and accepts −12..0. There is no query for
#: this; the only way to find out is to be refused. Keeping that per session means the slider
#: offers +6 again on every reconnect and the user hits the same wall every time, which is what
#: the source project did and what makes the application look broken rather than the firmware
#: limited. Deleting the cache re-learns it, so a wrong entry costs one refused click, not a
#: permanently missing setting.
LIMITS_VERSION = 1


def _limits_path(product_id: int, firmware: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in firmware) or "unknown"
    return cache_dir() / f"limits-{product_id:04x}-{safe}.json"


def load_limits(product_id: int | None, firmware: str) -> dict:
    """``{"rejected": {name: [values]}, "bounds": {name: [lo, hi]}, "locked": [names]}``."""
    empty: dict = {"rejected": {}, "bounds": {}, "locked": []}
    if product_id is None:
        return empty
    try:
        with open(_limits_path(product_id, firmware), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if data.get("version") != LIMITS_VERSION:
        return empty
    return {
        "rejected": data.get("rejected") or {},
        "bounds": data.get("bounds") or {},
        "locked": data.get("locked") or [],
    }


def save_limits(product_id: int | None, firmware: str, limits: dict) -> None:
    """Write the entry. Failure is logged and ignored — a cache must never break a session."""
    if product_id is None:
        return
    path = _limits_path(product_id, firmware)
    payload = {"version": LIMITS_VERSION, "productId": product_id, "firmware": firmware, **limits}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False,
                                         encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=1, sort_keys=True)
            temp_name = tmp.name
        os.replace(temp_name, path)
    except OSError as exc:
        log.info("could not write the limits cache: %s", exc)
