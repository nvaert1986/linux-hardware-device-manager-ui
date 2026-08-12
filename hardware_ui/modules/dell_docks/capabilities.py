"""A Dell dock, expressed in the shell's schema. Every row is read-only.

The page is built from whatever was actually found rather than from a fixed list: a plain USB dock
has no Thunderbolt section, and a machine without fwupd installed has no firmware section. Nothing
is shown as empty or unavailable, because a row that never has a value is noise.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from hardware_ui.core import Capability, CapabilitySet, Kind

GROUP_DOCK = "Dock"

IDENTITY_PREFIX = "identity."
LINK_PREFIX = "link."
FIRMWARE_PREFIX = "firmware."
REFRESH_KEY = "action.refresh"

#: Thunderbolt security levels, as the kernel reports them at
#: ``/sys/bus/thunderbolt/devices/domain0/security``.
SECURITY_LEVELS: dict[str, str] = {
    "none": "any device is connected automatically",
    "user": "you approve each device once",
    "secure": "approved devices are also cryptographically verified",
    "dponly": "display and USB only, no PCIe tunnelling",
    "usbonly": "USB only, no PCIe tunnelling",
    "nopcie": "PCIe tunnelling disabled",
}

LABELS: dict[str, str] = {
    "model": "Model",
    "vendor": "Made by",
    "usb_id": "USB id",
    "control_link": "Control interface",
    "serial": "Serial number",
    "endpoint": "Note",
    "connection": "Connected over",
    "tb_device": "Thunderbolt device",
    "tb_generation": "Thunderbolt generation",
    "tb_firmware": "Thunderbolt firmware",
    "tb_authorised": "Authorised",
    "tb_security": "Security level",
}

NOTE_READ_ONLY = (
    "Docks are shown for information only. Firmware updates are handled by fwupd, which "
    "enumerates this dock's parts individually and can update each one. Dock-related settings "
    "such as MAC address pass-through and wake-on-dock are system firmware settings on the "
    "computer rather than state held on the dock."
)
NOTE_FIRMWARE = (
    "Reported by fwupd, one entry per replaceable part. Use fwupd to update any of them."
)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def label_for(key: str) -> str:
    return LABELS.get(key, key.replace("_", " ").capitalize())


def build(
    *,
    identity: Sequence[str] = (),
    link: Sequence[str] = (),
    firmware: Sequence[str] = (),
) -> CapabilitySet:
    """One tab, three sections, every row a readout."""
    out: list[Capability] = [
        Capability(
            key=f"{IDENTITY_PREFIX}{key}",
            kind=Kind.READOUT,
            label=label_for(key),
            group=GROUP_DOCK,
            section="Identity",
            writable=False,
            note=NOTE_READ_ONLY if key == identity[0] else "",
        )
        for key in identity
    ]
    out += [
        Capability(
            key=f"{LINK_PREFIX}{key}",
            kind=Kind.READOUT,
            label=label_for(key),
            group=GROUP_DOCK,
            section="Connection",
            writable=False,
        )
        for key in link
    ]
    out += [
        Capability(
            key=f"{FIRMWARE_PREFIX}{slug(name)}",
            kind=Kind.READOUT,
            label=name,
            group=GROUP_DOCK,
            section="Firmware",
            writable=False,
            note=NOTE_FIRMWARE if name == firmware[0] else "",
        )
        for name in firmware
    ]
    out.append(
        Capability(
            key=REFRESH_KEY,
            kind=Kind.ACTION,
            label="Details",
            action_label="Re-read",
            group=GROUP_DOCK,
            section="Actions",
        )
    )
    return CapabilitySet(out)
