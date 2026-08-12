"""The devices paired to a receiver, which nothing else can see.

``hid-logitech-dj`` gives each paired device its own ``/dev/hidraw*`` node — and for a Logi Bolt
receiver no kernel version binds it, because Bolt is fully HID-compliant and does not speak the DJ
protocol that driver implements. ``046d:c548`` appears in mainline only in ``hid-quirks.c``, with
``HID_QUIRK_ALWAYS_POLL``. Checked against current master, not assumed.

The consequence, measured on a Bolt receiver with an MX Master 3S: the mouse has **nine
configurable settings** and enumeration cannot see it at all. Without this file the application
offers the dongle and hides the hardware.

**This is the one place the fast-discovery rule bends, and only just.** Reading the receiver's
pairing registers took ~100 ms and answered for both slots — including a keyboard that would not
reply to a ping — because the *receiver* stores each paired device's name, kind and serial. So no
device is opened, woken or probed here. Anything that talks to a peripheral belongs in ``connect``.

**The kernel gets first refusal, per slot.** Where ``hid-logitech-dj`` *does* bind — a Unifying
``c52b``, or a Bolt id somebody patches in — the paired device already has a node and enumeration
already found it. Emitting a child for it too would put the same mouse in the sidebar twice under
two different uids, which the registry's dedupe cannot catch. So each slot is asked
``find_paired_node`` first and skipped if the kernel answers. On a Bolt receiver every slot falls
through to this file; on a Unifying receiver none of them do and this becomes a no-op that costs one
sysfs walk. The day ``c548`` gains a driver entry, this stops firing for it on its own.

**Identity has to outlive the node.** ``/dev/hidrawN`` numbers are assigned in the order devices
appear and change on every reseat: this receiver moved from hidraw15-18 to hidraw1-4 across one
unplug. So a child's ``uid`` is built from the receiver's serial and the slot, which are stable, and
its address carries both so ``device.py`` can find it again without a node to match on.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from hardware_ui.core.device import Category, DeviceInfo, State, Transport

log = logging.getLogger(__name__)

#: ``receiver serial -> (occupied slots, children)``, for the lifetime of the process.
#:
#: Enumerating slots is the expensive part and by a wide margin. Measured on a Bolt receiver:
#: opening it 0.00 s, listing candidates 0.08 s, **walking the slots 2.13 s** -- because the library
#: asks all six and the four empty ones time out. That is the whole of the delay a user sees on a
#: rescan, and discovery is otherwise 0.02 s for every transport combined.
#:
#: ``count()`` costs 4 ms and changes the moment a device is paired or unpaired, so it is a sound
#: validity check: same receiver, same number of devices, same answer. Pair something and the count
#: moves and the walk happens again.
_MEMO: dict[str, tuple[int, list[DeviceInfo]]] = {}


def forget() -> None:
    """Drop the memo. For tests, and for a caller that knows pairing changed under it."""
    _MEMO.clear()

#: Marks a DeviceInfo as one of these children, and says where it lives. ``device.py`` reads both.
SLOT = "logitech_slot"
RECEIVER_PATH = "logitech_receiver_path"
RECEIVER_NAME = "logitech_receiver_name"

#: Logitech's own device kinds onto the shell's categories. Anything unrecognised stays INPUT --
#: these are peripherals on a receiver, and a wrong icon is better than a wrong section.
CATEGORIES = {
    "keyboard": Category.INPUT,
    "mouse": Category.INPUT,
    "touchpad": Category.INPUT,
    "trackball": Category.INPUT,
    "numpad": Category.INPUT,
    "presenter": Category.INPUT,
    "headset": Category.AUDIO,
}

#: freedesktop icon names, so a mouse and a keyboard behind one dongle are told apart at a glance.
ICONS = {
    "keyboard": "input-keyboard",
    "mouse": "input-mouse",
    "touchpad": "input-touchpad",
    "trackball": "input-mouse",
    "numpad": "input-keyboard",
    "presenter": "input-mouse",
    "headset": "audio-headset",
}


def child_uid(receiver_serial: str, slot: int) -> str:
    return f"hid:logitech:{receiver_serial or 'unknown'}:{slot}"


def discover(parent: DeviceInfo) -> list[DeviceInfo]:
    """Every device paired to the receiver *parent* describes.

    Returns an empty list for anything that is not a receiver, or when the receiver cannot be
    opened. Never raises: the registry logs and drops the children, and the receiver stays usable.
    """
    from .bootstrap import ensure_path, vendored
    from .device import _usb_parent

    try:
        ensure_path()
        base, _device_module, receiver_module = vendored()
    except Exception as exc:  # noqa: BLE001 - a missing dependency is not a scan failure
        log.debug("cannot expand %s: %s", parent.uid, exc)
        return []

    wanted = _usb_parent(parent.path or "")
    out: list[DeviceInfo] = []

    for info in base.receivers_and_devices():
        if info.isDevice:
            continue
        # The receiver's HID++ node is rarely the node discovery offered, so match the USB device
        # rather than the path -- see device.py's _resolve for the measurement behind this.
        if wanted is not None and _usb_parent(info.path) != wanted:
            continue
        receiver = receiver_module.create_receiver(base, info)
        if receiver is None:
            continue
        try:
            out.extend(_children_of(receiver, parent, info.path))
        finally:
            with contextlib.suppress(Exception):
                receiver.close()
        break

    if out:
        log.info("%s: %d paired device(s) enumeration could not see", parent.uid, len(out))
    return out


def _kernel_node(receiver_path: str, slot: int) -> str | None:
    """The node the kernel exposes for this slot, or ``None``.

    ``None`` on any failure, including the library not being importable, and that is the safe
    direction: unable to ask the kernel means we provide the child ourselves, so the device is
    configurable. The opposite default would hide it.
    """
    try:
        from hidapi.udev_impl import find_paired_node
    except ImportError:
        return None
    with contextlib.suppress(Exception):
        return find_paired_node(receiver_path, slot, 0)
    return None


def _children_of(receiver: Any, parent: DeviceInfo, path: str) -> list[DeviceInfo]:
    serial = _serial(receiver)

    occupied = None
    with contextlib.suppress(Exception):
        occupied = int(receiver.count())
    if occupied is not None and serial:
        remembered = _MEMO.get(serial)
        if remembered is not None and remembered[0] == occupied:
            log.debug("%s: reusing %d known children", serial, len(remembered[1]))
            return list(remembered[1])

    receiver_name = getattr(receiver, "name", "") or parent.name
    out: list[DeviceInfo] = []

    for paired in receiver:
        if paired is None:
            continue
        slot = int(paired.number)
        # The kernel first: a slot the driver already exposes is discovery's to report, not ours.
        node = _kernel_node(receiver.path, slot)
        if node:
            log.debug("slot %d already has %s; leaving it to enumeration", slot, node)
            continue
        kind = str(getattr(paired, "kind", "") or "").lower()
        name = getattr(paired, "name", "") or f"Paired device {slot}"
        out.append(
            DeviceInfo(
                uid=child_uid(serial, slot),
                name=name,
                transport=Transport.HID,
                category=CATEGORIES.get(kind, Category.INPUT),
                vendor_id=parent.vendor_id,
                # Deliberately not the receiver's product id: this is a different device, and
                # sharing the id would let one match rule speak for both.
                product_id=None,
                serial=str(getattr(paired, "serial", "") or ""),
                # The receiver's node, because that is the only way in. `_resolve` uses the slot
                # below to get from there to this device.
                path=path,
                icon_name=ICONS.get(kind, ""),
                # PRESENT, not the device's own online state: whether it is awake is a question for
                # connect, and a peripheral that is merely asleep is still there to be configured.
                state=State.PRESENT,
                properties={
                    SLOT: slot,
                    RECEIVER_PATH: path,
                    RECEIVER_NAME: receiver_name,
                    "logitech_kind": kind,
                },
            )
        )
    if occupied is not None and serial:
        _MEMO[serial] = (occupied, list(out))
    return out


def _serial(receiver: Any) -> str:
    """The receiver's serial, decoded, because it keys every child's uid.

    A Bolt receiver reports it double-encoded -- see ``device._readable_serial``. Using the raw
    form would work but would change the uid the day that is fixed, and a uid is what per-device
    settings and the photo cache are keyed on.
    """
    from .device import _readable_serial

    with contextlib.suppress(Exception):
        raw = receiver.serial
        return str(
            _readable_serial(raw, getattr(receiver, "receiver_kind", "") == "bolt") or ""
        )
    return ""


def slot_of(info: DeviceInfo) -> int | None:
    """The slot a child occupies, or ``None`` if *info* is not one of these children."""
    value = (info.properties or {}).get(SLOT)
    return int(value) if isinstance(value, int) else None


__all__ = [
    "RECEIVER_NAME",
    "RECEIVER_PATH",
    "SLOT",
    "child_uid",
    "discover",
    "slot_of",
]
