"""Discover connected Sony headsets via BlueZ.

Uses ``bluetoothctl`` (present on every BlueZ system, no extra Python deps) to
enumerate devices and identify Sony over-ear headsets — preferring ones that
advertise the Sony configuration UUID, falling back to the model-name prefix.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass

from ..protocol.enums import SERVICE_UUID, SONY_NAME_KEYWORDS, SONY_NAME_PREFIXES

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.IGNORECASE)


@dataclass
class DiscoveredDevice:
    mac: str
    name: str
    connected: bool
    has_service: bool  # advertises the Sony config UUID

    @property
    def label(self) -> str:
        state = "connected" if self.connected else "paired"
        return f"{self.name} ({self.mac}) — {state}"


def _bluetoothctl(*args: str) -> str:
    if not shutil.which("bluetoothctl"):
        raise RuntimeError("bluetoothctl not found — is BlueZ installed?")
    try:
        return subprocess.run(
            ["bluetoothctl", *args],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("bluetoothctl %s failed: %s", args, exc)
        return ""


def _device_macs() -> list[str]:
    out = _bluetoothctl("devices")
    macs = []
    for line in out.splitlines():
        m = _MAC_RE.search(line)
        if m:
            macs.append(m.group(1).upper())
    return macs


def _device_info(mac: str) -> tuple[str, bool, bool]:
    """Return (name, connected, has_service) for one device."""
    out = _bluetoothctl("info", mac)
    name = mac
    connected = False
    has_service = False
    uuid_needle = SERVICE_UUID.lower()
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Alias:") and name == mac:
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Connected:"):
            connected = stripped.split(":", 1)[1].strip().lower() == "yes"
        elif "uuid" in stripped.lower() and uuid_needle in stripped.lower():
            has_service = True
    return name, connected, has_service


def _looks_like_sony(name: str, has_service: bool) -> bool:
    """Gatekeep to Sony headphones only: either it advertises the Sony config
    service, or its name matches Sony's headphone naming conventions."""
    if has_service:
        return True
    upper = name.upper()
    return (upper.startswith(SONY_NAME_PREFIXES)
            or any(k in upper for k in SONY_NAME_KEYWORDS))


def scan(connected_only: bool = True) -> list[DiscoveredDevice]:
    """Return recognised Sony headsets known to BlueZ.

    By default only devices that are *actually connected* (BlueZ
    ``Connected: yes``) are returned — the paired-but-absent devices in the
    BlueZ list are filtered out. Pass ``connected_only=False`` to include paired
    devices too.
    """
    found: list[DiscoveredDevice] = []
    for mac in _device_macs():
        name, connected, has_service = _device_info(mac)
        if not _looks_like_sony(name, has_service):
            continue
        if connected_only and not connected:
            continue
        found.append(DiscoveredDevice(mac, name, connected, has_service))
    found.sort(key=lambda d: (not d.connected, d.name))
    return found
