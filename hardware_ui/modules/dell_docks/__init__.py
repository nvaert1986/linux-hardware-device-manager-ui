"""Dell docking stations — read-only.

Importing this package must stay cheap; ``device.py`` is loaded only when a dock is opened.
"""

from __future__ import annotations

__all__ = ["MODULE_ID"]

MODULE_ID = "dell_docks"
