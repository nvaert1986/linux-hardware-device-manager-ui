"""Razer peripherals via the OpenRazer daemon.

Importing this package must stay cheap, and it must not import ``openrazer``: that dependency
belongs to this module alone, and an installation without it has to run normally. ``device.py``
imports the client, and is loaded only when a Razer device is opened.
"""

from __future__ import annotations

__all__ = ["MODULE_ID"]

MODULE_ID = "razer_peripherals"
