"""Making the vendored Solaar importable, exactly once and unambiguously.

``logitech_receiver`` uses absolute imports throughout -- ``from solaar import configuration``,
``from logitech_receiver import common`` -- so its directory has to be on ``sys.path`` rather than
being reached as ``hardware_ui.third_party.logitech_receiver``. Rewriting those imports was the
alternative and was rejected: it would touch dozens of files, and every one of them would then
differ from upstream for no reason but our packaging, which is exactly what
``tools/vendor_solaar.py`` exists to avoid.

**Two traps, both handled here.**

*Importing the same module twice.* Reaching the tree both ways -- once as
``hardware_ui.third_party.solaar`` and once as ``solaar`` -- yields two module objects with
separate globals. Repointing the config path on one would leave the library reading the other, and
the symptom would be settings that silently fail to persist. So nothing outside this file may
import from ``hardware_ui.third_party`` directly; go through :func:`vendored`.

*A real Solaar installed alongside.* If ``app-misc/solaar`` is present its ``solaar`` package is
importable too, and which one wins would depend on ``sys.path`` order. Ours is inserted at the
front deliberately: the vendored copy is patched (no ``diversion``, no GUI stack) and an unpatched
one would pull GTK in and fail differently on every machine. Determinism beats politeness here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType

log = logging.getLogger(__name__)

VENDOR = Path(__file__).resolve().parent.parent.parent / "third_party"

#: Top-level names the vendored tree provides. Used to check what actually got imported.
PROVIDES = ("logitech_receiver", "hidapi", "hid_parser", "solaar")


class VendorMissing(RuntimeError):
    """The vendored Solaar subset is not in the tree -- see ``tools/vendor_solaar.py``."""


def ensure_path() -> Path:
    """Put the vendored tree first on ``sys.path``. Idempotent."""
    if not (VENDOR / "logitech_receiver").is_dir():
        raise VendorMissing(
            f"{VENDOR} does not contain logitech_receiver. "
            "Run: python3 tools/vendor_solaar.py"
        )
    entry = str(VENDOR)
    if sys.path[:1] != [entry]:
        # Remove before re-inserting rather than leaving a duplicate further down, where a later
        # path entry could still win for a name we have not imported yet.
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
    return VENDOR


def vendored() -> tuple[ModuleType, ModuleType, ModuleType]:
    """``(base, device, receiver)`` from the vendored library.

    The single entry point. Importing these anywhere else risks getting them under a different
    name, and the failure that causes is invisible until a setting quietly stops persisting.
    """
    ensure_path()
    from logitech_receiver import base, device, receiver

    return base, device, receiver


def loaded_from_vendor() -> bool:
    """Whether the modules currently imported are ours rather than a system Solaar.

    Worth being able to answer: a machine with ``app-misc/solaar`` installed can import a package
    of the same name, and every symptom of getting the wrong one is confusing.
    """
    import importlib

    for name in PROVIDES:
        module = sys.modules.get(name)
        if module is None:
            continue
        origin = getattr(module, "__file__", "") or ""
        if origin and not origin.startswith(str(VENDOR)):
            log.warning("%s was imported from %s, not the vendored copy", name, origin)
            return False
    importlib.invalidate_caches()
    return True


__all__ = ["PROVIDES", "VENDOR", "VendorMissing", "ensure_path", "loaded_from_vendor", "vendored"]
