"""Obtaining Jabra's property catalogue, with consent.

``properties.json`` is GN Audio's own definition of 423 Jabra properties, published on npm as
``@gnaudio/jabra-properties-definition`` under an ISC declaration. It is **not** redistributed
here, for a reason worth stating plainly: the published package contains no licence text at all —
only a ``"license": "ISC"`` field in its ``package.json``. That declaration is how the whole npm
ecosystem conveys licences and is very probably a valid grant, but ISC's own terms require a
copyright notice and permission notice to travel with every copy, and GN Audio published neither.
Rather than ship someone else's data under a notice this project would have had to write on their
behalf, the file is fetched from GN Audio's own publication, on request, and cached.

That also keeps this project's licence unambiguous: no third-party material in the tree.

**This module cannot work without it.** Unlike Poly, where a missing catalogue only costs the
manufacturer's own wording, the Jabra catalogue *is* the protocol description — it carries the
command and subcommand for every property and the byte converters that decode them. Without it a
device can be identified and nothing else, so ``required=True``.
"""

from __future__ import annotations

import logging
import pathlib

from hardware_ui.core import RegistryFetch
from hardware_ui.core.paths import vendor_dir

log = logging.getLogger(__name__)

MODULE_ID = "jabra_headsets"

PACKAGE = "@gnaudio/jabra-properties-definition"

#: Pinned so a surprise upstream change cannot alter behaviour silently. Bump deliberately: the
#: catalogue's size is part of the capability cache key, so a bump re-probes rather than trusting
#: entries recorded against a different property set.
VERSION = "14.4.0"

REGISTRY = "https://registry.npmjs.org"
TARBALL = f"{REGISTRY}/{PACKAGE}/-/jabra-properties-definition-{VERSION}.tgz"
MEMBER = "package/properties.json"

FILENAME = "properties.json"

#: The real file is ~461 KB; refuse anything wildly larger.
MAX_BYTES = 8 * 1024 * 1024

#: A catalogue with far fewer entries than this is not the file expected. 423 properties ship in
#: the pinned version, so this floor tolerates upstream shrinking without accepting a stub.
MIN_PROPERTIES = 100

SOURCE_PAGE = f"https://www.npmjs.com/package/{PACKAGE}"


def find() -> pathlib.Path | None:
    """The catalogue, or ``None`` if it has not been obtained yet.

    A packager may legitimately vendor the file themselves — the ISC declaration permits it — so
    an existing copy is used without asking. Only its absence triggers the offer to download.
    """
    candidate = vendor_dir(MODULE_ID) / FILENAME
    if candidate.is_file() and candidate.stat().st_size:
        return candidate
    return None


def consent_text() -> str:
    """What the user is agreeing to. Kept here so the dialog cannot understate it.

    The licensing position is the whole reason this download exists rather than a shipped file, and
    a generic "fetch this?" hides that. Also states plainly what declining costs, because for this
    module it is not a cosmetic loss -- the catalogue is the protocol description.
    """
    return (
        "This module needs Jabra's property catalogue to know what your headset can do.\n\n"
        "It is not included, because the package it comes from carries a licence declaration "
        "but no licence text, and shipping it would mean writing that notice on GN Audio's "
        "behalf.\n\n"
        f"  Package:  {PACKAGE} {VERSION}\n"
        f"  From:     {REGISTRY}\n"
        "  Licence:  ISC, declared by GN Audio A/S\n"
        "  Size:     about 460 KB\n\n"
        "Nothing about you or your device is sent — it is a plain file download. Without it your "
        "device can be identified, but not a single setting can be read or changed."
    )


def source() -> RegistryFetch:
    return RegistryFetch(
        MODULE_ID,
        url=TARBALL,
        version=VERSION,
        member=MEMBER,
        filename=FILENAME,
        max_bytes=MAX_BYTES,
        min_entries=MIN_PROPERTIES,
        source_page=SOURCE_PAGE,
        required=True,
        consent=consent_text(),
    )


__all__ = ["FILENAME", "MODULE_ID", "PACKAGE", "VERSION", "consent_text", "find", "source"]
