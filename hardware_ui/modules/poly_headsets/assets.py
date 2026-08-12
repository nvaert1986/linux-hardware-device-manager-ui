"""Unpacking Poly's device catalogues from the user's own copy of Poly Studio.

The catalogues carry the message ids and payload types this module needs, and they exist only
inside Poly's software. They are Poly's, so nothing is shipped: the user obtains the installer
from HP and we unpack their copy locally, exactly as Debian's ``ttf-mscorefonts-installer`` does.

The installer nests three deep, which is why ``ExtractInstaller`` had to learn about cabinets:

    PolyStudio-<version>-x64.msi          bootstrapper MSI, 315 MB
      └─ AI_ChainedPackageFile.PolyStudio.msi     chained MSI, 230 MB
           └─ disk1.cab                           the entire payload, 224 MB
                └─ DeviceSettings.zip             231 catalogues
                   app.asar                       Electron bundle, English UI strings

**One correction is applied during conversion, and it is not cosmetic.** Poly's Windows
catalogues name a setting's change-event id after its *get* id. A live capture proves that is
wrong where the two differ -- ``muteReminderFrequency`` reads on ``0x0A22``, writes on ``0x0A20``,
and its event arrives on ``0x0A20``, the **set** id. Measured across all 231 Windows catalogues:
2,515 settings declare all three ids, 418 have get ≠ set, and **107 of those name the event after
the get id**. Left uncorrected, every one of those writes would fall back to a re-read instead of
being confirmed by its event -- slower, and exactly the path that produced the "setting reverts,
then applies my previous choice" bug in the reference implementation.

The Android catalogues get this right, and the reference project merged the two sources with
Android winning. An Android APK is not something a user can reasonably be asked to supply, so the
rule is applied directly instead: **where get and set differ, the event follows the set id.**
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from hardware_ui.core import ExtractInstaller, safe_extract
from hardware_ui.core.paths import ensure

log = logging.getLogger(__name__)

MODULE_ID = "poly_headsets"

SOURCE_PAGE = "https://www.poly.com/us/en/support/downloads-apps/lens-desktop"

#: Searched in the unpacked tree. Globs, never fixed paths -- vendors move things between
#: releases, and a glob degrades into a clear error rather than a crash.
LOCATE = ("DeviceSettings.zip", "**/DeviceSettings.zip")

#: A catalogue with fewer settings than this is not a catalogue; refuse rather than import junk.
MIN_CATALOGUES = 20


def _block_id(setting: dict, block: str) -> int | None:
    raw = (setting.get(block) or {}).get("deckardId")
    try:
        return int(raw, 16) if raw else None
    except (TypeError, ValueError):
        return None


def correct_event_ids(catalogue: dict) -> int:
    """Point each event id at the set id where the two ids differ. Returns how many changed.

    See the module docstring: the Windows catalogues systematically name the event after the get
    id, and a live capture shows the device emits it on the set id.
    """
    fixed = 0
    for setting in catalogue.get("settings", []):
        get_id, set_id = _block_id(setting, "get"), _block_id(setting, "set")
        if get_id is None or set_id is None or get_id == set_id:
            continue
        event = setting.get("event")
        if not isinstance(event, dict) or _block_id(setting, "event") != get_id:
            continue
        event["deckardId"] = f"0x{set_id:x}"
        fixed += 1
    return fixed


def transform(found: Path, target: Path) -> int:
    """Convert the located installer contents into this module's vendor directory.

    ``found`` is the directory holding ``DeviceSettings.zip``; ``target`` is staging. Returns the
    number of catalogues written, which the importer treats as the entry count.
    """
    archive = found / "DeviceSettings.zip"
    if not archive.exists():
        matches = sorted(found.rglob("DeviceSettings.zip"))
        if not matches:
            raise FileNotFoundError("DeviceSettings.zip not found in the unpacked installer")
        archive = matches[0]

    catalogues = ensure(target / "catalogues")
    with zipfile.ZipFile(archive) as zf:
        safe_extract(zf, catalogues)

    # Flatten: the archive may nest the JSON under a directory.
    for path in list(catalogues.rglob("*.json")):
        if path.parent != catalogues:
            path.replace(catalogues / path.name)

    written = corrected = 0
    for path in sorted(catalogues.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if not data.get("settings"):
            path.unlink(missing_ok=True)
            continue
        corrected += correct_event_ids(data)
        path.write_text(json.dumps(data, indent=1))
        written += 1

    if written < MIN_CATALOGUES:
        raise ValueError(
            f"only {written} device catalogues found — this does not look like Poly Studio"
        )
    log.info("poly_headsets: %d catalogues, %d event ids corrected", written, corrected)
    return written


def source() -> ExtractInstaller:
    return ExtractInstaller(
        MODULE_ID,
        locate=LOCATE,
        transform=transform,
        source_page=SOURCE_PAGE,
        required=True,
    )


__all__ = ["correct_event_ids", "source", "transform"]
