"""Dell docking stations, read-only.

**This module never writes anything.** A dock's settings live in three different places, none of
which is the dock's own HID interface:

* **Firmware** belongs to `fwupd`, which already enumerates a WD22TB4 down to its individual
  hubs and controllers and can flash each of them. Duplicating that would be worse and riskier.
* **The dock's HID interfaces carry a bare 36-byte vendor descriptor** -- no usages, no structure.
  It is the channel fwupd updates through. Configuring by that route would be a reverse-engineering
  project sitting next to firmware flashing, where a wrong write is not merely a wrong setting.
* **The settings people actually want** -- MAC address pass-through, wake-on-dock, Thunderbolt
  security -- are BIOS attributes on the host, not dock state, and Linux exposes those through
  `dell-wmi-sysman` at ``/sys/class/firmware-attributes``. That is a different module.

What is left is worth having on its own: which dock is attached, how it is connected, what
firmware each of its parts is running, and how Thunderbolt has authorised it. All of it comes from
sysfs and, when it is installed, `fwupd` -- so nothing here opens the dock at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hardware_ui.core import (
    CapabilitySet,
    Device,
    DeviceInfo,
    NotSupported,
    Unreachable,
)

from . import capabilities as C

log = logging.getLogger(__name__)

MODULE_ID = "dell_docks"

SYS_USB = Path("/sys/bus/usb/devices")
SYS_THUNDERBOLT = Path("/sys/bus/thunderbolt/devices")

DELL_VENDOR_ID = 0x413C

#: Serial numbers that identify nothing. A WD22TB4's companion endpoint reports this one.
PLACEHOLDER_SERIALS = frozenset({"0123456789ABCDEF", "000000000000", "0"})

FWUPD_TIMEOUT = 20.0
"""`fwupdmgr` can be slow on first run. It is optional, so a timeout means "no firmware detail"
rather than a failure."""


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


class DellDock(Device):
    """One Dell dock. Everything is read; nothing is ever written."""

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._lock = asyncio.Lock()
        self._set = CapabilitySet()
        self._values: dict[str, Any] = {}

    @property
    def capabilities(self) -> CapabilitySet:
        return self._set

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._gather)

    async def disconnect(self) -> None:
        # Nothing was opened: every value came from sysfs or from fwupd's own output.
        return None

    def _usb_path(self) -> Path | None:
        """The dock's USB device directory, from the sysfs path enumeration recorded."""
        name = str(self.info.properties.get("usb", ""))
        candidate = SYS_USB / name if name else None
        return candidate if candidate is not None and candidate.is_dir() else None

    # ------------------------------------------------------------------ gathering

    def _gather(self) -> None:
        usb = self._usb_path()
        if usb is None:
            raise Unreachable("this dock is no longer attached")

        identity = self._identity(usb)
        thunderbolt = self._thunderbolt()
        firmware = self._firmware()

        self._values = {}
        self._values.update({f"{C.IDENTITY_PREFIX}{k}": v for k, v in identity})
        self._values.update({f"{C.LINK_PREFIX}{k}": v for k, v in thunderbolt})
        self._values.update({f"{C.FIRMWARE_PREFIX}{C.slug(k)}": v for k, v in firmware})
        self._set = C.build(
            identity=[k for k, _ in identity],
            link=[k for k, _ in thunderbolt],
            firmware=[k for k, _ in firmware],
        )
        self._bump_capabilities()

    def _identity(self, usb: Path) -> list[tuple[str, str]]:
        product = _read(usb / "product") or self.info.name
        vendor = _read(usb / "manufacturer") or "Dell"
        speed = _read(usb / "speed")
        rows: list[tuple[str, str]] = [("model", product), ("vendor", vendor)]
        vid, pid = _read(usb / "idVendor"), _read(usb / "idProduct")
        if vid and pid:
            rows.append(("usb_id", f"{vid}:{pid}"))
        if speed:
            # The dock's own control interface is a full-speed device; this is not the speed of
            # anything plugged into it, and saying so avoids a confusing "480 Mbps" on a
            # Thunderbolt dock.
            rows.append(("control_link", f"USB {speed} Mbps (dock control interface)"))
        serial = _read(usb / "serial")
        if serial and serial not in PLACEHOLDER_SERIALS:
            rows.append(("serial", serial))

        # A dock exposes more than one control endpoint -- a WD22TB4 presents the dock itself and
        # a bare companion -- and both are claimed, because gating on interface count would hide
        # any dock that has only one. Saying which endpoint this is beats two rows that look like
        # two docks.
        interfaces = int(self.info.properties.get("usb_interfaces") or 0)
        if interfaces <= 1 or (serial in PLACEHOLDER_SERIALS):
            rows.append((
                "endpoint",
                "Secondary control interface of this dock — the same hardware as the other entry",
            ))
        return rows

    def _thunderbolt(self) -> list[tuple[str, str]]:
        """Anything Thunderbolt knows about this dock. Empty for a plain USB dock."""
        rows: list[tuple[str, str]] = []
        if not SYS_THUNDERBOLT.is_dir():
            return rows
        for entry in sorted(SYS_THUNDERBOLT.iterdir()):
            vendor = _read(entry / "vendor_name")
            device = _read(entry / "device_name")
            if not device or "dell" not in vendor.casefold():
                continue
            rows.append(("connection", "Thunderbolt"))
            rows.append(("tb_device", f"{vendor} {device}"))
            generation = _read(entry / "generation")
            if generation:
                rows.append(("tb_generation", f"Thunderbolt {generation}"))
            nvm = _read(entry / "nvm_version")
            if nvm:
                rows.append(("tb_firmware", nvm))
            authorised = _read(entry / "authorized")
            if authorised:
                rows.append((
                    "tb_authorised",
                    "Yes" if authorised != "0" else "No — not approved for PCIe access",
                ))
            break

        domain = SYS_THUNDERBOLT / "domain0" / "security"
        level = _read(domain)
        if level:
            rows.append(("tb_security", f"{level} — {C.SECURITY_LEVELS.get(level, 'unknown')}"))
        if not rows:
            rows.append(("connection", "USB"))
        return rows

    def _firmware(self) -> list[tuple[str, str]]:
        """Per-component firmware versions, from fwupd if it is installed.

        fwupd owns dock firmware and enumerates a WD22TB4 down to its individual hubs and MST
        controller. Reading its report is far better than guessing, and it is strictly optional:
        without it the page simply has no firmware section.
        """
        if not shutil.which("fwupdmgr"):
            return []
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["fwupdmgr", "get-devices", "--json"],
                capture_output=True, text=True, timeout=FWUPD_TIMEOUT, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0 or not proc.stdout:
            return []
        try:
            devices = json.loads(proc.stdout).get("Devices") or []
        except ValueError:
            return []

        rows: list[tuple[str, str]] = []
        for entry in devices:
            name = str(entry.get("Name") or "")
            version = str(entry.get("Version") or "")
            vendor = str(entry.get("Vendor") or "")
            if not name or not version:
                continue
            # Keep the dock's own tree: Dell-made parts, or anything that says so in its name.
            haystack = f"{name} {vendor}".casefold()
            if "dock" not in haystack and not self._matches_model(name):
                continue
            rows.append((name, version))
        return rows

    def _matches_model(self, name: str) -> bool:
        """Whether an fwupd entry is this dock itself, e.g. ``WD22TB4``."""
        model = self.info.name.casefold()
        return bool(name) and name.casefold() in model

    # ------------------------------------------------------------------ reads

    async def get(self, key: str) -> Any:
        if key not in self._values:
            raise NotSupported(key)
        return self._values[key]

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        wanted = set(keys)
        return {k: v for k, v in self._values.items() if k in wanted}

    async def set(self, key: str, value: Any) -> Any:
        """Never. This module is informational, by design -- see the module docstring."""
        if key == C.REFRESH_KEY:
            async with self._lock:
                await asyncio.to_thread(self._gather)
            return None
        raise NotSupported(
            "Dell docks are shown for information only. Firmware updates belong to fwupd, and "
            "dock-related settings such as MAC address pass-through live in the system firmware."
        )


__all__ = ["DellDock"]
