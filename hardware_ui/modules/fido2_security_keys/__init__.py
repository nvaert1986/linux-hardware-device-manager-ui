"""FIDO2 / U2F security keys, over the CTAP standard.

Importing this package must stay cheap and must not import ``fido2``: that dependency belongs to
this module alone, so an installation without it runs normally. ``device.py`` imports it inside
``connect()``.
"""

from __future__ import annotations

__all__ = ["MODULE_ID"]

MODULE_ID = "fido2_security_keys"
