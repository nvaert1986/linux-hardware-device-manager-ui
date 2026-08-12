"""HP Poly / Plantronics headset module (Deckard protocol).

Importing this package must stay cheap: the registry reads ``module.toml`` beside this file
without importing anything here. ``device.py`` pulls in the session and both transports, and is
loaded only when a headset is opened.
"""

from __future__ import annotations

__all__ = ["MODULE_ID"]

MODULE_ID = "poly_headsets"
