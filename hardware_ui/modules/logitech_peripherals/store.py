"""Where this module persists Logitech settings, and why it is not where Solaar puts them.

Many HID++ settings do not survive a reconnect in hardware: the device forgets, and the host is
expected to write them back when it reappears. ``logitech_receiver`` therefore keeps a per-device
record and re-applies it, and upstream stores that at ``~/.config/solaar/config.yaml``.

**This module redirects it.** Writing into another application's configuration file is not ours to
do: a user who installs Solaar later would find entries it never wrote, and two processes with no
locking between them would take turns clobbering each other. The vendored library's module-level
path is repointed at this application's own config directory instead.

The cost is honest and worth stating: if you run both applications, each keeps its own idea of what
your mouse is set to, and they can disagree. The hardware is the tiebreaker -- whichever wrote last
wins, and a read shows what the device actually holds.

The redirect happens on import, before anything reads the file, because ``configuration._load()``
caches into a module global on first use and a later change would be ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hardware_ui.core import paths

log = logging.getLogger(__name__)

MODULE_ID = "logitech_peripherals"

#: Named for what it holds rather than after Solaar, because its *format* is upstream's business
#: and its *contents* are this application's.
FILENAME = "logitech.yaml"


def config_path() -> Path:
    return paths.config_dir() / FILENAME


def redirect() -> Path:
    """Point the vendored library at our file. Idempotent, and safe to call before every use.

    Both paths are set. Upstream reads YAML if present and falls back to a legacy JSON file, so
    leaving the JSON one aimed at Solaar's directory would let a stray ``config.json`` there be
    picked up -- quietly, and only on machines that happen to have one.
    """
    from .bootstrap import ensure_path

    ensure_path()
    # Imported as ``solaar.configuration``, the same name the library itself uses. Reaching it as
    # ``hardware_ui.third_party.solaar.configuration`` would create a *second* module object with
    # its own globals, and the redirect below would apply to a copy nobody reads.
    from solaar import configuration

    target = config_path()
    paths.ensure(target.parent)
    configuration._yaml_file_path = str(target)
    configuration._json_file_path = str(target.with_suffix(".json"))
    return target


__all__ = ["FILENAME", "MODULE_ID", "config_path", "redirect"]
