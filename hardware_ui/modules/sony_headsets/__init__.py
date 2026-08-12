"""Sony WH-1000X module.

Importing this package must stay cheap: the registry resolves ``module.toml`` beside this file
without importing anything here, but a careless top-level import of the protocol stack would
defeat that for any code path that does touch the package. Keep heavy imports inside
``device.py``, which is only loaded when a device is opened.
"""

from __future__ import annotations

__all__ = ["MODULE_ID"]

MODULE_ID = "sony_headsets"
