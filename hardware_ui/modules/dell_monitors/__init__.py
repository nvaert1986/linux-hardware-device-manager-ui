"""Dell DDC/CI monitor module.

Importing this package must stay cheap: the registry resolves ``module.toml`` beside this file
without importing anything here, and ``device.py`` -- which pulls in the ddcutil wrapper and the
Dell value tables -- is loaded only when a display is opened.
"""

from __future__ import annotations

__all__ = ["MODULE_ID"]

MODULE_ID = "dell_monitors"
