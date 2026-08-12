"""XDG base directory helpers.

Everything the app writes goes under an XDG directory -- never ``~/.hardware-ui``. Vendor assets
imported from an installer live under the *data* dir because they are user data that must survive
a cache wipe; the device list lives under *cache* because losing it costs 30ms of re-enumeration.
"""

from __future__ import annotations

import os
import pathlib

APP = "hardware-ui"


def _xdg(var: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(var) or os.path.expanduser(default))


def config_dir() -> pathlib.Path:
    """``~/.config/hardware-ui`` -- modules.toml, per-device preferences, pins."""
    return _xdg("XDG_CONFIG_HOME", "~/.config") / APP


def data_dir() -> pathlib.Path:
    """``~/.local/share/hardware-ui`` -- imported vendor assets, user-supplied device images."""
    return _xdg("XDG_DATA_HOME", "~/.local/share") / APP


def cache_dir() -> pathlib.Path:
    """``~/.cache/hardware-ui`` -- the device list, resolved I2C bus numbers. Disposable."""
    return _xdg("XDG_CACHE_HOME", "~/.cache") / APP


def runtime_dir() -> pathlib.Path:
    """``$XDG_RUNTIME_DIR/hardware-ui``, falling back to the cache dir.

    Used for lock files. Falls back rather than failing, because ``XDG_RUNTIME_DIR`` is absent in
    containers and over plain ssh.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    return pathlib.Path(base) / APP if base else cache_dir()


def vendor_dir(module_id: str) -> pathlib.Path:
    """Where an :class:`~hardware_ui.core.assets.AssetSource` deposits a module's assets."""
    return data_dir() / "vendor" / module_id


def ensure(path: pathlib.Path) -> pathlib.Path:
    """Create *path* (mode 0700) and return it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path
