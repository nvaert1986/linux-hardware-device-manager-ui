"""Fast enumeration of what hardware is present, without opening any of it.

This module is the reason the app starts quickly. Everything here reads sysfs or asks BlueZ over
D-Bus for properties it already holds; nothing opens a device node, connects RFCOMM, or touches an
I2C bus. Budget for the whole sweep is tens of milliseconds.

The slow half -- probing -- lives in the modules and happens only when the user selects a device.
Keeping that boundary is what stops one wedged DDC bus or one unreachable headset from delaying
startup for everything else.

After the initial sweep, prefer :func:`watch` over calling :func:`enumerate_all` again. Rescanning
on a timer is the thing this design exists to avoid.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .device import Category, DeviceInfo, State, Transport
from .paths import cache_dir, ensure

log = logging.getLogger(__name__)

SYS_HIDRAW = Path("/sys/class/hidraw")
SYS_DRM = Path("/sys/class/drm")

#: Interface classes that make a USB device interesting to us even without a module match.
_AUDIO_CLASSES = {0x01, 0x03}


# --------------------------------------------------------------------------- HID / USB


def enumerate_hid() -> list[DeviceInfo]:
    """Walk ``/sys/class/hidraw``. No device node is opened.

    hidraw rather than raw USB because every headset worth configuring exposes a vendor-defined
    HID collection, and hidraw coexists with the kernel's ``usbhid`` driver -- WebUSB-style
    interface claiming would have to fight it.

    **One physical device is one row.** A peripheral exposes a hidraw node per HID interface, and
    gaming hardware exposes many: a Razer keyboard and mouse produced *seven* between them, which
    the sidebar showed as three keyboards and four mice. Nodes are grouped by the physical device
    they belong to -- see :func:`_group_key`.

    Two passes on purpose. Reading every node first and grouping afterwards keeps the grouping a
    pure function of the collected facts; interleaving them meant the key for one node was
    computed from the previous node's classification, which merged a keyboard into a dock.
    """
    if not SYS_HIDRAW.is_dir():
        return []

    scanned: list[dict] = []
    for node in sorted(SYS_HIDRAW.iterdir()):
        fields = _parse_uevent(node / "device" / "uevent")
        hid_id = fields.get("HID_ID", "")
        # HID_ID is "bus:vendor:product", all hex, zero-padded to 8.
        parts = hid_id.split(":")
        vid = pid = None
        if len(parts) == 3:
            with contextlib.suppress(ValueError):
                vid, pid = int(parts[1], 16), int(parts[2], 16)

        name = _tidy_name(fields.get("HID_NAME", node.name))
        usb = _usb_device_of(node)
        # `_descriptor_kind` last: the USB path is verified against the Razer seven-node
        # case, so it keeps precedence, and only a node that got nothing -- in practice a
        # Bluetooth one -- falls through to reading its own descriptor.
        kind = (_fido_kind(node) or _dock_kind(name) or _hid_kind(usb)
                or _descriptor_kind(node))
        scanned.append({
            "node": node,
            "name": name,
            "uniq": fields.get("HID_UNIQ", ""),
            "hid_id": hid_id,
            "vid": vid,
            "pid": pid,
            "usb": usb,
            "kind": kind,
            "fido": _fido_kind(node) is not None,
            "interfaces": _usb_interface_count(usb),
            "group": _group_key(usb, kind) if usb is not None else node.name,
        })

    grouped: dict[str, list[dict]] = {}
    for entry in scanned:
        grouped.setdefault(entry["group"], []).append(entry)

    out: list[DeviceInfo] = []
    for members in grouped.values():
        # The richest endpoint represents the group: a dock answers both as itself, with its model
        # name and a real serial, and as a bare "Dell dock". The model name is worth showing.
        # A composite device answers on several interfaces with different jobs -- a YubiKey
        # presents an OTP interface that looks like a keyboard alongside its FIDO one. What the
        # device *is* must win over which node happened to sort first, so a classification that
        # identifies the device beats one that merely describes an interface.
        kind = next(
            (e["kind"] for e in members if e["kind"] in _SPECIFIC_KINDS),
            next((e["kind"] for e in members if e["kind"]), None),
        )
        best = max(members, key=lambda e: (e["kind"] == kind, e["interfaces"], len(e["name"])))
        usb = best["usb"]
        stable = best["uniq"] or (usb.name if usb is not None else "") or best["hid_id"]
        out.append(
            DeviceInfo(
                uid=f"hid:{stable or best['node'].name}",
                name=best["name"],
                transport=Transport.HID,
                category=_KIND_CATEGORIES.get(kind) or _guess_category(best["name"]),
                vendor_id=best["vid"],
                product_id=best["pid"],
                address=best["uniq"],
                path=f"/dev/{best['node'].name}",
                state=State.PRESENT,
                properties={
                    "sysfs": str(best["node"]),
                    "usb": usb.name if usb is not None else "",
                    "hid_kind": kind or "",
                    # How many interfaces the parent USB device exposes. A primary control device
                    # usually offers more than one; a lone HID interface is often a secondary
                    # endpoint of the same product.
                    "usb_interfaces": best["interfaces"],
                    # Match rules compare exactly, so "more than one" is offered as a flag; a rule
                    # cannot express "greater than" against a count.
                    "usb_multi_interface": "yes" if best["interfaces"] > 1 else "no",
                    # Set on any interface advertising the FIDO usage page, so a match rule can
                    # claim every CTAP authenticator without naming a single vendor.
                    "hid_usage_page": "f1d0" if any(e["fido"] for e in members) else "",
                    # Whether any node of this device declares the HID++ input reports. Lets a
                    # match rule tell a Logitech mouse from a Logitech webcam, both of which carry
                    # vendor id 046d over hidraw and are otherwise indistinguishable without
                    # opening them. See `_hidpp_family`.
                    "hid_hidpp": _hidpp_family(members),
                    # Every node of this device, in case a module needs a specific one: the Poly
                    # Deckard tunnel lives on exactly one of several.
                    "nodes": [f"/dev/{e['node'].name}" for e in members],
                },
                icon_name=_HID_ICONS.get(kind or "", ""),
            )
        )
    return out


#: Boot-protocol values from the USB HID specification. Only meaningful on an interface
#: with class 3 and subclass 1; elsewhere the protocol byte is unspecified.
_BOOT_KEYBOARD, _BOOT_MOUSE = 1, 2
_HID_ICONS = {
    "mouse": "input-mouse",
    "keyboard": "input-keyboard",
    # Breeze ships no docking-station icon. KDE's own Thunderbolt preferences icon is the right
    # read for a Thunderbolt dock; a plain USB dock gets the device-tree icon, which is what a
    # dock actually is -- one connection fanning out into several. Both exist at 22 and 24 px,
    # the sizes the sidebar asks for.
    "dock": "preferences-desktop-thunderbolt",
    "dock_usb": "preferences-devices-tree",
    "security_key": "application-pgp-keys",
}

#: Which sidebar heading each classification belongs under. A dock is not an input device, and a
#: security key is not one either -- both were landing under INPUT because that was the only
#: category a classified HID device could get.
#: Classifications that identify the whole device rather than one of its interfaces.
_SPECIFIC_KINDS = frozenset({"security_key", "dock", "dock_usb"})

_KIND_CATEGORIES = {
    "keyboard": Category.INPUT,
    "mouse": Category.INPUT,
    "dock": Category.DOCKS,
    "dock_usb": Category.DOCKS,
    "security_key": Category.SECURITY_KEYS,
}

#: HID usage page 0xF1D0 is the FIDO alliance's, and every CTAP authenticator advertises it. In a
#: report descriptor it encodes as ``06 D0 F1`` (Usage Page, two-byte value, little-endian).
#: Reading it identifies a security key **vendor-neutrally and without opening the device**, which
#: is what lets one module serve YubiKey, Nitrokey, OnlyKey, SoloKey and anything else compliant.
FIDO_USAGE_PAGE = b"\x06\xd0\xf1"

#: Product names that mean "this is a dock or port replicator", whatever made it -- the icon
#: should be right before any module is loaded, and other vendors' docks will want it too.
_DOCK_WORDS = ("dock", "port replicator", "portreplicator")


def _usb_device_of(node: Path) -> Path | None:
    """The USB device a hidraw node belongs to, i.e. the physical peripheral.

    ``/sys/class/hidraw/hidrawN/device`` is the HID device; its parent is the USB *interface* and
    the grandparent is the USB *device*. The device is what a user would call "my mouse".
    """
    try:
        hid = (node / "device").resolve()
    except OSError:
        return None
    for candidate in (hid.parent.parent, hid.parent):
        if (candidate / "idVendor").exists():
            return candidate
    return None


def _group_key(usb: Path, kind: str | None) -> str:
    """Which physical device a USB node belongs to.

    Normally the USB device itself. **A dock is the exception**: it presents more than one control
    endpoint, on different branches of its own internal hubs -- a WD22TB4 answers at ``3-1.1.5``
    as itself and at ``3-1.1.3.5`` as a bare companion. Both descend from a hub whose product
    string is "Dell dock", and that hub is the dock, so it is the key.

    Only applied to devices already classified as docks. Everything plugged *into* the dock
    descends from the same hub -- a keyboard and a mouse do here -- and must not be merged with it.
    """
    if kind not in DOCK_KINDS:
        return usb.name
    ancestor = usb
    found = usb.name
    for _ in range(6):  # a dock nests its hubs a few deep; bound the walk
        ancestor = ancestor.parent
        if not (ancestor / "idVendor").exists():
            break
        if _dock_kind(_read_text(ancestor / "product")):
            found = ancestor.name
    return found


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _fido_kind(node: Path) -> str | None:
    """``security_key`` when this node advertises the FIDO usage page.

    Read from the sysfs ``report_descriptor``, which is a file: nothing is opened, so this stays
    inside the enumeration budget. Confirmed against a YubiKey, whose FIDO interface carries it
    while its OTP and CCID interfaces do not.
    """
    try:
        descriptor = (node / "device" / "report_descriptor").read_bytes()
    except OSError:
        return None
    return "security_key" if FIDO_USAGE_PAGE in descriptor else None


#: Top-level HID usages that say what a device is. Page 1 is Generic Desktop.
_USAGE_KINDS = {(0x01, 0x02): "mouse", (0x01, 0x06): "keyboard"}


def _descriptor_kind(node: Path) -> str | None:
    """What this device is, read from its own HID report descriptor.

    The fallback for **Bluetooth**. :func:`_hid_kind` classifies by the USB interface's protocol
    byte, and a Bluetooth HID device has no USB interface at all -- so an MX Master paired directly
    over Bluetooth got no classification, and therefore no icon and a category guessed from whether
    its name happened to contain the word "mouse". One did ("Logitech Wireless Mouse MX Master 2S",
    filed under INPUT) and one did not ("Logitech MX Master 3S", filed under OTHER with the generic
    peripherals icon), which is not a distinction anybody meant to draw.

    Only top-level usages count -- those outside any collection. A mouse's descriptor opens with
    Generic Desktop / Mouse and may then declare a vendor page for its private protocol; taking
    usages from inside collections would pick up pointers, buttons and wheels as well.

    Reads sysfs, like :func:`_fido_kind`; nothing is opened.
    """
    try:
        descriptor = (node / "device" / "report_descriptor").read_bytes()
    except OSError:
        return None

    offset = page = 0
    depth = 0
    while offset < len(descriptor):
        prefix = descriptor[offset]
        size = prefix & 0x03
        size = 4 if size == 3 else size
        tag, kind = prefix >> 4, (prefix >> 2) & 0x03
        data = int.from_bytes(descriptor[offset + 1:offset + 1 + size], "little") if size else 0

        if kind == 1 and tag == 0x0:            # Global: Usage Page
            page = data
        elif kind == 2 and tag == 0x0 and depth == 0:   # Local: Usage, outside any collection
            found = _USAGE_KINDS.get((page, data))
            if found:
                return found
        elif kind == 0 and tag == 0xA:          # Main: Collection
            depth += 1
        elif kind == 0 and tag == 0xC:          # Main: End Collection
            depth -= 1
        offset += 1 + size
    return None


#: HID++ declares two input reports and nothing else identifies it as reliably: a short one on
#: report id 0x10 carrying six payload bytes, and a long one on 0x11 carrying nineteen. Taken from
#: Solaar's own device filter (``third_party/hidapi/udev_impl.py::_match``), including its decision
#: *not* to also require usage page 0xFF00 -- the comment there marks that check as too strict, and
#: Solaar ships it disabled.
HIDPP_REPORTS = {0x10: 6, 0x11: 19}


def _input_report_sizes(descriptor: bytes) -> dict[int | None, int] | None:
    """Report id to total input payload size in bits. ``None`` if the descriptor is not modelled.

    ``None`` keys a descriptor that declares no report id at all, which is most of them.

    Cross-checked against the vendored ``hid_parser`` on every HID descriptor present on the
    development machine -- fourteen of them, from a Logitech BRIO to a Razer mouse with seven nodes
    -- and the two agree on every report id and every size. It is also checked against a HID++
    descriptor both parsers read as short=6 long=19. Three of those fourteen descriptors make
    ``hid_parser`` raise, where this walker still answers; being the more tolerant of the two is
    safe here, because the only question asked of the result is whether two specific report ids
    exist at two specific sizes.

    Bails on constructs it does not model rather than guessing: long items, and Push/Pop, which
    save and restore the global item state and so would silently corrupt the running report size.
    """
    sizes: dict[int | None, int] = {}
    report_id: int | None = None
    size = count = 0
    offset = 0
    while offset < len(descriptor):
        prefix = descriptor[offset]
        if prefix == 0xFE:                      # Long item
            return None
        length = prefix & 0x03
        length = 4 if length == 3 else length
        tag, kind = prefix >> 4, (prefix >> 2) & 0x03
        data = (
            int.from_bytes(descriptor[offset + 1:offset + 1 + length], "little") if length else 0
        )
        if kind == 1:                           # Global
            if tag == 0x7:                      # Report Size
                size = data
            elif tag == 0x8:                    # Report ID
                report_id = data
            elif tag == 0x9:                    # Report Count
                count = data
            elif tag in (0xA, 0xB):             # Push / Pop
                return None
        elif kind == 0 and tag == 0x8:          # Main: Input
            sizes[report_id] = sizes.get(report_id, 0) + size * count
        offset += 1 + length
    return sizes


def _speaks_hidpp(node: Path) -> bool | None:
    """Whether this node answers HID++, from its report descriptor. ``None`` if unreadable.

    Reads sysfs; the node is never opened. ``None`` matters and is not folded into ``False`` --
    "this is not a HID++ device" and "nobody could tell" call for different treatment, and quietly
    reporting the second as the first is how a working receiver would go missing.
    """
    try:
        descriptor = (node / "device" / "report_descriptor").read_bytes()
    except OSError:
        return None
    sizes = _input_report_sizes(descriptor)
    if sizes is None:
        return None
    return any(sizes.get(rid) == payload * 8 for rid, payload in HIDPP_REPORTS.items())


def _hidpp_family(members: Sequence[dict]) -> str:
    """``"yes"``, ``"no"`` or ``""`` for a group of nodes belonging to one physical device.

    Asked of **every** node, not of the one this group is represented by, because those are
    routinely not the same node. Measured on a Logi Bolt receiver: the group is represented by
    /dev/hidraw1, which declares no report ids whatsoever, while HID++ answers on /dev/hidraw3.
    Testing only the representative would report a working receiver as not speaking HID++.

    ``""`` when no node qualifies but at least one could not be read -- unknown, not absent.
    """
    verdicts = [_speaks_hidpp(entry["node"]) for entry in members]
    if any(v is True for v in verdicts):
        return "yes"
    return "" if any(v is None for v in verdicts) else "no"


def _dock_kind(name: str) -> str | None:
    """``dock``/``dock_usb`` when the product calls itself one, else ``None``.

    Name-based because nothing else identifies a dock before it is opened, and the transport is
    taken from the name too: a Thunderbolt dock says so, and anything else that calls itself a
    dock is treated as USB. Both are docks to a module -- see :func:`dock_kinds` -- the split
    exists only so the icon is right.
    """
    lowered = name.casefold()
    if not any(word in lowered for word in _DOCK_WORDS):
        return None
    return "dock" if "thunderbolt" in lowered else "dock_usb"


#: Every classification that means "this is a dock". A module matching docks wants both.
DOCK_KINDS = frozenset({"dock", "dock_usb"})


def _usb_interface_count(usb: Path | None) -> int:
    if usb is None:
        return 0
    try:
        return int((usb / "bNumInterfaces").read_text())
    except (OSError, ValueError):
        return 0


def _hid_kind(usb: Path | None) -> str | None:
    """``mouse``, ``keyboard`` or ``None``, from the USB boot protocols the device advertises.

    Neither of the obvious signals works on gaming hardware, both checked against a real Razer
    keyboard and mouse:

    * ``bInterfaceProtocol`` of the node's own interface -- the *mouse* has interfaces reporting
      the keyboard protocol, for its macro keys.
    * udev's ``ID_INPUT_MOUSE`` / ``ID_INPUT_KEYBOARD`` -- both devices carry both, because each
      presents pointer and key interfaces.

    Looking at the whole device rather than one interface does work: a boot-*mouse* interface
    (protocol 2) is present on the mouse and absent on the keyboard. Checked against hwdb, which
    independently names them "Gaming Mouse" and "Gaming Keyboard".

    Best effort by design -- it drives an icon. A module that knows better, because it asked a
    daemon with a device database, is the authority.
    """
    if usb is None:
        return None
    protocols: set[int] = set()
    for interface in sorted(usb.glob("*:*")):
        try:
            if int((interface / "bInterfaceClass").read_text(), 16) != 0x03:
                continue
            # The protocol field only means "keyboard" or "mouse" on a *boot* interface, i.e.
            # subclass 1. On subclass 0 it is unspecified, and reading it anyway is wrong: a
            # BlackWidow keyboard advertises 03:00:02, whose 2 is not "mouse" -- taking it as one
            # classified the keyboard as a mouse.
            if int((interface / "bInterfaceSubClass").read_text(), 16) != 0x01:
                continue
            protocols.add(int((interface / "bInterfaceProtocol").read_text(), 16))
        except (OSError, ValueError):
            continue
    if _BOOT_MOUSE in protocols:
        return "mouse"
    if _BOOT_KEYBOARD in protocols:
        return "keyboard"
    return None


def _tidy_name(name: str) -> str:
    """Drop a doubled vendor prefix: sysfs concatenates USB manufacturer and product strings, and
    some vendors put the brand in both -- "Razer Razer DeathAdder V2"."""
    words = name.split()
    while len(words) > 1 and words[0].casefold() == words[1].casefold():
        words.pop(0)
    return " ".join(words) or name


# --------------------------------------------------------------------------- raw USB


SYS_USB = Path("/sys/bus/usb/devices")

#: Interface signatures that make a USB device worth a sidebar row, as ``(class, subclass,
#: protocol)``. ``None`` matches any value in that position.
#:
#: **This list is the filter, and it stays a list of exact signatures rather than a class test.**
#: Enumerating every USB device would fill the sidebar with hubs, webcams, card readers and every
#: composite keyboard interface, and the sidebar is the product. Each entry below is a specific
#: protocol a module actually speaks, so a device qualifying here is a device something can talk
#: to. Add a signature when a real device needs it, never in anticipation.
_CONTROL_INTERFACES = (
    # CDC-ACM: the device presents a serial port and speaks a private protocol over it, which is
    # what Creative does. Subclass 0x02 specifically -- communications class also covers ethernet,
    # ATM and OBEX functions, which are somebody else's business.
    (0x02, 0x02, None),
    # GIP, the Xbox Game Input Protocol: a vendor-specific interface with a fixed subclass and
    # protocol. 8BitDo's Xbox controllers expose this and **no hidraw at all**, so without it they
    # cannot be discovered by any transport here. Matched as the full triple rather than on class
    # 0xFF alone, because 0xFF is what every dongle in the world falls back to.
    (0xFF, 0x47, 0xD0),
)


def enumerate_usb() -> list[DeviceInfo]:
    """Walk ``/sys/bus/usb/devices`` for devices exposing a control interface we can speak to.

    The fourth transport, and the only one that does not correspond to a kernel-provided character
    device this application can just open. hidraw, BlueZ and DRM all hand over a node; a vendor
    protocol tunnelled through CDC or GIP has to be claimed with libusb at connect time.

    Which interfaces qualify is :data:`_CONTROL_INTERFACES`, and that list is deliberately a set
    of exact signatures rather than a class test -- see the note there.

    Nothing is opened. Interface classes come from sysfs attributes, the same way
    :func:`enumerate_hid` reads report descriptors as files.
    """
    if not SYS_USB.is_dir():
        return []

    found: list[DeviceInfo] = []
    for entry in sorted(SYS_USB.iterdir()):
        # Interfaces are named `3-1.2:1.0`; devices are `3-1.2`. Only devices carry idVendor, so
        # the attribute is the test rather than the name shape.
        if not (entry / "idVendor").exists():
            continue
        control = _control_interfaces(entry)
        if not control:
            continue

        try:
            vendor_id = int(_read_text(entry / "idVendor"), 16)
            product_id = int(_read_text(entry / "idProduct"), 16)
        except ValueError:
            continue

        product = _read_text(entry / "product")
        manufacturer = _read_text(entry / "manufacturer")
        name = (_tidy_name(f"{manufacturer} {product}".strip())
                or f"USB {vendor_id:04x}:{product_id:04x}")
        serial = _read_text(entry / "serial")

        found.append(DeviceInfo(
            # The serial when there is one: a sysfs path changes the moment the device moves to
            # another port, and this uid keys the settings and the capability cache.
            uid=f"usb:{serial or entry.name}",
            name=name,
            transport=Transport.USB,
            category=_usb_category(entry, name, control),
            icon_name=_USB_ICONS.get(control[0] if control else "", ""),
            vendor_id=vendor_id,
            product_id=product_id,
            serial=serial,
            path=str(entry),
            state=State.PRESENT,
            properties={
                "busnum": _read_text(entry / "busnum"),
                "devnum": _read_text(entry / "devnum"),
                "interfaces": _usb_interface_count(entry),
                # Which signature matched, so a module's match rule can ask for the transport it
                # actually speaks instead of re-reading sysfs. A device may offer more than one.
                "control_interfaces": ",".join(control),
            },
        ))
    return found


#: Sidebar heading per control interface, where the interface says what the device *is*. GIP is
#: an Xbox input protocol and nothing else uses it, so a GIP device is a game controller; CDC-ACM
#: says nothing about the device, so those fall through to the audio sniff and then to OTHER.
_USB_CATEGORIES = {"gip": Category.INPUT}
_USB_ICONS = {"gip": "input-gaming"}


def _usb_category(usb: Path, name: str, control: list[str]) -> Category:
    for interface in control:
        if interface in _USB_CATEGORIES:
            return _USB_CATEGORIES[interface]
    return Category.AUDIO if _looks_like_audio(usb, name) else Category.OTHER


#: Names for the signatures, used as the ``control_interfaces`` property. Kept short and stable:
#: they are matchable strings in a module manifest, so renaming one breaks a rule somewhere.
_INTERFACE_NAMES = {(0x02, 0x02, None): "cdc-acm", (0xFF, 0x47, 0xD0): "gip"}


def _control_interfaces(usb: Path) -> list[str]:
    """Names of the control interfaces this device exposes, in :data:`_CONTROL_INTERFACES` order.

    Empty means nothing here can talk to it, which is the common case and the reason the sidebar
    stays short.
    """
    seen: list[tuple[int | None, int | None, int | None]] = []
    for interface in sorted(usb.glob(f"{usb.name}:*")):
        # Per attribute, not all-or-nothing: an unreadable one becomes None, which then only
        # matches a signature that does not care about that position. Losing a whole interface
        # because one file was missing would silently hide the device.
        triple = tuple(_hex_or_none(interface / attr) for attr in
                       ("bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol"))
        if triple[0] is not None:
            seen.append(triple)  # type: ignore[arg-type]

    matched = []
    for signature in _CONTROL_INTERFACES:
        if any(all(want is None or want == got
                   for want, got in zip(signature, triple, strict=True))
               for triple in seen):
            matched.append(_INTERFACE_NAMES[signature])
    return matched


def _hex_or_none(path: Path) -> int | None:
    try:
        return int(_read_text(path), 16)
    except ValueError:
        return None


def _looks_like_audio(usb: Path, name: str) -> bool:
    """A USB audio class interface, or a name that says so.

    The class is the reliable signal -- a sound card carries interface class 0x01 whatever it is
    called -- and the name check catches a headphone amplifier that presents only CDC and HID.
    """
    for interface in usb.glob(f"{usb.name}:*"):
        try:
            if int(_read_text(interface / "bInterfaceClass"), 16) == 0x01:
                return True
        except ValueError:
            continue
    return bool(_AUDIO_HINT.search(name))


# --------------------------------------------------------------------------- Bluetooth


def enumerate_bluetooth() -> list[DeviceInfo]:
    """Ask BlueZ for paired and known devices via ``GetManagedObjects``.

    One D-Bus round trip returns every device BlueZ knows, including name, address, advertised
    service UUIDs and current connection state. That is everything a match rule needs, so a Sony
    headset can be listed as "not connected" without any RFCOMM attempt.
    """
    try:
        from dbus_fast.aio import MessageBus  # noqa: F401
    except ImportError:
        return _enumerate_bluetooth_cli()
    # The async path is used by the running app via watch(); this synchronous entry point is for
    # the CLI and first paint, where a subprocess call is simpler than spinning an event loop.
    return _enumerate_bluetooth_cli()


def _enumerate_bluetooth_cli() -> list[DeviceInfo]:
    """Fallback using ``bluetoothctl``. Slower than D-Bus but has no Python dependency."""
    import subprocess

    try:
        listing = subprocess.run(  # noqa: S603
            ["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if listing.returncode != 0:
        return []

    out: list[DeviceInfo] = []
    for line in listing.stdout.splitlines():
        m = re.match(r"Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) (.+)", line.strip(), re.I)
        if not m:
            continue
        address, name = m.group(1), m.group(2)
        info = subprocess.run(  # noqa: S603
            ["bluetoothctl", "info", address],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        uuids = frozenset(re.findall(r"UUID:.*\(([0-9a-f-]{36})\)", info.stdout, re.I))
        connected = re.search(r"^\s*Connected:\s*yes", info.stdout, re.I | re.M) is not None
        # BlueZ publishes its own freedesktop icon name for a device, derived from the Class of
        # Device or the BLE Appearance characteristic. Better evidence than guessing from the name:
        # an MX Master reports `input-mouse` while "MX Master 3S" contains no word this could have
        # matched on. Headsets report `audio-headset`, which is what they were already getting.
        icon = re.search(r"^\s*Icon:\s*(\S+)", info.stdout, re.I | re.M)
        icon_name = icon.group(1) if icon else ""
        out.append(
            DeviceInfo(
                uid=f"bt:{address}",
                name=name,
                transport=Transport.BLUETOOTH,
                category=_icon_category(icon_name) or _guess_category(name),
                address=address,
                uuids=uuids,
                icon_name=icon_name,
                # **Never CONNECTED.** BlueZ's "connected" means the headset is switched on and
                # linked to this machine -- it says nothing about whether *this application* has
                # opened a configuration session, which is what State.CONNECTED is reserved for
                # and what the sidebar's green dot reports. Enumeration cannot know that.
                #
                # So a BlueZ-connected device is PRESENT: "physically available, not yet opened",
                # which is precisely what it is. Everything else is PAIRED -- in the list, not
                # openable -- whether BlueZ calls it paired or merely remembers seeing it.
                state=State.PRESENT if connected else State.PAIRED,
            )
        )
    return out


# --------------------------------------------------------------------------- Displays


def enumerate_displays() -> list[DeviceInfo]:
    """Read EDID from every connected DRM connector.

    Deliberately *not* ``ddcutil detect``, which takes seconds and can wedge on some buses. EDID
    from sysfs is instant and yields vendor, model and serial -- enough to match a module and
    paint a row. The I2C bus is touched only when the user opens the display's page.
    """
    out: list[DeviceInfo] = []
    if not SYS_DRM.is_dir():
        return out

    for conn in sorted(SYS_DRM.glob("card*-*")):
        try:
            if (conn / "status").read_text().strip() != "connected":
                continue
            edid = (conn / "edid").read_bytes()
        except OSError:
            continue
        if len(edid) < 128:
            continue

        # Internal panels are wired to the GPU without a DDC channel, so no module can ever
        # configure one. Dropping them here rather than in a module keeps every display module
        # from repeating the same filter, and stops a laptop screen appearing as a dead row.
        kind = _connector_type(conn.name)
        if kind in _INTERNAL_CONNECTORS:
            continue

        vendor, product, serial, name = _parse_edid(edid)
        out.append(
            DeviceInfo(
                uid=f"drm:{vendor}:{product:04x}:{serial or conn.name}",
                name=name or f"{vendor} {product:04X}",
                transport=Transport.DISPLAY,
                category=Category.DISPLAY,
                serial=serial,
                path=str(conn),
                state=State.PRESENT,
                properties={
                    "edid_vendor": vendor,
                    "edid_product": product,
                    "connector": conn.name,
                    "connector_type": kind,
                },
            )
        )
    return out


_INTERNAL_CONNECTORS = frozenset({"eDP", "LVDS", "DSI"})


def _connector_type(connector: str) -> str:
    """``card1-DP-3`` -> ``DP``. USB-C DP-Alt panels enumerate as an ordinary ``DP-n``."""
    parts = connector.split("-")
    return parts[1] if len(parts) > 2 else ""


def _parse_edid(edid: bytes) -> tuple[str, int, str, str]:
    """Return ``(vendor_pnp_id, product_code, serial, monitor_name)``.

    The vendor ID is three 5-bit letters packed big-endian into bytes 8-9, offset so that 1 == 'A'.
    """
    packed = (edid[8] << 8) | edid[9]
    vendor = "".join(chr(((packed >> shift) & 0x1F) + 0x40) for shift in (10, 5, 0))
    product = edid[10] | (edid[11] << 8)

    name = ""
    serial = ""
    for i in range(54, 126, 18):
        block = edid[i : i + 18]
        if block[0:3] != b"\x00\x00\x00":
            continue
        text = block[5:18].split(b"\n")[0].decode("ascii", "ignore").strip()
        if block[3] == 0xFC:
            name = text
        elif block[3] == 0xFF:
            serial = text
    return vendor, product, serial, name


# --------------------------------------------------------------------------- Aggregate


#: Which node a camera's settings are read through, when it publishes more than one.
#:
#: A camera with an infrared sensor -- anything supporting Windows Hello -- presents two capture
#: nodes. On a Logitech BRIO they are indistinguishable by USB ids, by name, and even by extension
#: unit: both report the same units and writing to either changes the one camera. What separates
#: them is what they can stream. The colour sensor offers several pixel formats and a long list of
#: resolutions; the infrared one offers ``GREY`` at a single square size.
#:
#: So the richer node wins, and the test is the count. Naming the formats instead would be guessing:
#: ``GREY`` is a perfectly ordinary format for a monochrome industrial camera that has no second
#: node to be confused with.
CAMERA_NODE_GLOB = "/dev/video*"


def enumerate_v4l2() -> list[DeviceInfo]:
    """Cameras, one row per physical device.

    Reads each ``/dev/videoN``'s capability with ``VIDIOC_QUERYCAP`` and keeps the capture ones. The
    ioctl needs the node opened, which is the one place this module opens anything -- but it opens
    read-only, performs one ioctl and closes, and does not touch the stream. A camera in use by a
    video call is enumerated without disturbing it.

    Nothing here imports the camera module: the shapes needed are three fields of one struct, and a
    module import during discovery is what the lazy-probe rule exists to prevent.
    """
    import ctypes
    import glob
    from fcntl import ioctl

    class _capability(ctypes.Structure):
        _fields_ = [
            ("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
            ("bus_info", ctypes.c_char * 32), ("version", ctypes.c_uint32),
            ("capabilities", ctypes.c_uint32), ("device_caps", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32 * 3),
        ]

    querycap = 0x80685600           # _IOR('V', 0, v4l2_capability)
    capture_bit = 0x00000001         # V4L2_CAP_VIDEO_CAPTURE

    found: list[dict[str, Any]] = []
    for node in sorted(glob.glob(CAMERA_NODE_GLOB), key=_node_number):
        cap = _capability()
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            log.debug("cannot open %s: %s", node, exc)
            continue
        try:
            ioctl(fd, querycap, cap)
        except OSError as exc:
            log.debug("%s answers no VIDIOC_QUERYCAP: %s", node, exc)
            os.close(fd)
            continue
        os.close(fd)
        # device_caps, not capabilities: the latter is what the *driver* can do across all its
        # nodes, so on a UVC camera it is set for the metadata node too.
        if not cap.device_caps & capture_bit:
            continue
        usb = _v4l2_usb_parent(node)
        found.append({
            "node": node,
            "card": cap.card.decode(errors="replace").strip(),
            "driver": cap.driver.decode(errors="replace").strip(),
            "usb": usb,
            "richness": _v4l2_richness(node),
        })

    return _one_row_per_camera(found)


def _node_number(node: str) -> tuple[int, str]:
    digits = "".join(ch for ch in node if ch.isdigit())
    return (int(digits) if digits else 0, node)


def _v4l2_richness(node: str) -> int:
    """How many formats this node offers. The colour sensor of a pair offers more than one.

    Counted with ``VIDIOC_ENUM_FMT`` rather than by looking for ``GREY``, so nothing depends on a
    format name. One ioctl per format, on a node already known to be a camera.
    """
    import ctypes
    from fcntl import ioctl

    class _fmtdesc(ctypes.Structure):
        _fields_ = [
            ("index", ctypes.c_uint32), ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
            ("description", ctypes.c_char * 32), ("pixelformat", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32 * 4),
        ]

    request = 0xC0405602            # _IOWR('V', 2, v4l2_fmtdesc)
    try:
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return 0
    count = 0
    try:
        desc = _fmtdesc()
        desc.type = 1               # V4L2_BUF_TYPE_VIDEO_CAPTURE
        while count < 32:
            try:
                ioctl(fd, request, desc)
            except OSError:
                break
            count += 1
            desc.index = count
    finally:
        os.close(fd)
    return count


def _v4l2_usb_parent(node: str) -> Path | None:
    """The USB device behind a video node, for its vendor and product ids."""
    real = Path(os.path.realpath(node))
    base = Path("/sys/class/video4linux") / real.name
    for _ in range(6):
        base = base / ".."
        candidate = base.resolve()
        if (candidate / "idVendor").is_file():
            return candidate
    return None


def _one_row_per_camera(found: list[dict[str, Any]]) -> list[DeviceInfo]:
    """Collapse a camera's several capture nodes into one row.

    Grouped by USB device, then the node offering the most pixel formats wins -- see
    :data:`CAMERA_NODE_GLOB`. A camera with no USB parent (a laptop's MIPI sensor, a loopback
    device) is grouped by its own node, which leaves it alone.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in found:
        usb = entry["usb"]
        groups.setdefault(str(usb) if usb is not None else entry["node"], []).append(entry)

    out: list[DeviceInfo] = []
    for members in groups.values():
        best = max(members, key=lambda e: (e["richness"], -_node_number(e["node"])[0]))
        usb = best["usb"]
        vendor = _hex_or_none(usb / "idVendor") if usb is not None else None
        product = _hex_or_none(usb / "idProduct") if usb is not None else None
        serial = ""
        if usb is not None:
            with contextlib.suppress(OSError):
                serial = (usb / "serial").read_text().strip()
        # Keyed on the USB path rather than the node number, so a camera keeps its identity across
        # a reboot that hands out /dev/video numbers in a different order.
        stable = str(usb).rsplit("/", 1)[-1] if usb is not None else Path(best["node"]).name
        out.append(DeviceInfo(
            uid=f"v4l2:{stable}",
            name=best["card"] or "Camera",
            transport=Transport.V4L2,
            category=Category.OTHER,
            vendor_id=vendor,
            product_id=product,
            serial=serial,
            path=best["node"],
            state=State.PRESENT,
            icon_name="camera-web",
            properties={
                "driver": best["driver"],
                "sysfs": str(usb) if usb is not None else "",
                # Every capture node this camera has, in case a module wants the infrared one.
                "nodes": [
                    entry["node"]
                    for entry in sorted(members, key=lambda e: _node_number(e["node"]))
                ],
                "formats": best["richness"],
            },
        ))
    return out


def enumerate_all() -> list[DeviceInfo]:
    """Every transport, in one sweep. Individual backends fail soft."""
    devices: list[DeviceInfo] = []
    for fn in (enumerate_hid, enumerate_usb, enumerate_bluetooth, enumerate_displays,
               enumerate_v4l2):
        try:
            devices.extend(fn())
        except Exception:
            log.exception("%s failed; continuing with other transports", fn.__name__)
    return _one_row_per_device(devices)


def _one_row_per_device(devices: list[DeviceInfo]) -> list[DeviceInfo]:
    """Drop a duplicate row when hidraw already produced one for the same physical device.

    Each enumerator is deliberately ignorant of the others -- that is what keeps them simple -- so a
    device reachable two ways gets found twice. The sidebar still has to show one row per thing you
    can hold, so the duplicate is resolved here.

    **hidraw wins**, both times, because its row carries strictly more: a node that can be opened,
    a device kind, and an icon. Losing the *duplicate* costs nothing; a module that needs the other
    channel can find it from the identifier the two rows share.

    Two ways a device gets found twice:

    *Raw USB.* It exposes a HID interface and a CDC control channel. Matched on the USB device the
    two rows share.

    *Bluetooth.* A directly-paired mouse or keyboard is both a BlueZ device and a hidraw node.
    Matched on the address: hidraw reports ``HID_UNIQ``, which is the Bluetooth MAC, and BlueZ
    reports the same MAC in a different case. **This one was doing real damage.** Every Logitech
    mouse paired over Bluetooth produced a working, claimed hidraw row *and* an unclaimed BlueZ row
    reading "no module" -- two entries for one mouse, one of them a dead end, with nothing to say
    which was which. It is the likeliest explanation for a report that Logitech configuration "does
    not work over Bluetooth" on a machine where it does.

    Bluetooth *audio* is untouched: a headset has no hidraw node, so nothing matches it and its
    BlueZ row is the only one. A Poly headset reached through a USB dongle keeps both of its rows
    too -- the dongle's node carries no ``HID_UNIQ``, so it never collides with the headset's MAC.
    """
    hid_rows = [info for info in devices if info.transport is Transport.HID]
    usb_parents = {str(info.properties.get("usb", "")) for info in hid_rows} - {""}
    addresses = {info.address.upper() for info in hid_rows} - {""}
    camera_parents = {
        Path(str(info.properties.get("sysfs", ""))).name
        for info in devices
        if info.transport is Transport.V4L2
    } - {""}
    if not usb_parents and not addresses and not camera_parents:
        return devices

    def is_duplicate(info: DeviceInfo) -> bool:
        if info.transport is Transport.USB:
            return Path(info.path).name in usb_parents
        if info.transport in (Transport.BLUETOOTH, Transport.BLE):
            return info.address.upper() in addresses
        if info.transport is Transport.HID:
            return str(info.properties.get("usb", "")) in camera_parents
        return False

    kept = [info for info in devices if not is_duplicate(info)]
    return [_with_hid_nodes(info, hid_rows) for info in kept]


def _with_hid_nodes(info: DeviceInfo, hid_rows: Sequence[DeviceInfo]) -> DeviceInfo:
    """A camera row carrying the hidraw nodes of the row it displaced, as ``hid_nodes``.

    Nothing is lost by a camera winning: the buttons are still reachable, they are just no longer a
    separate entry pretending to be a separate device. A module that wants them asks for this
    property rather than re-deriving the USB topology.
    """
    if info.transport is not Transport.V4L2:
        return info
    parent = Path(str(info.properties.get("sysfs", ""))).name
    nodes = [
        node
        for row in hid_rows
        if parent and str(row.properties.get("usb", "")) == parent
        for node in row.properties.get("nodes", [])
    ]
    if not nodes:
        return info
    return dataclasses.replace(info, properties={**info.properties, "hid_nodes": nodes})


#: Subsystems worth waking up for. Anything else on the bus is somebody else's business, and the
#: filter is installed in the kernel, so an unrelated event never reaches this process at all.
HOTPLUG_SUBSYSTEMS = ("usb", "hidraw", "drm", "video4linux")

HOTPLUG_HINT = (
    "Hotplug needs dev-python/pyudev. Without it the list refreshes when you press Rescan."
)


class Hotplug:
    """A udev subscription: devices appearing and disappearing, as a readable file descriptor.

    Deliberately not a callback API. The shell owns an asyncio loop and knows how to wait on a
    descriptor; handing it one keeps the threading in one place, and keeps this module free of any
    opinion about how the application is structured.

    **Bluetooth is not here.** A headset switching on is BlueZ over D-Bus, not a uevent -- see
    ``PROJECT_STATE.md``. This covers USB, HID and DRM, which is a YubiKey re-plugging itself
    after a configuration change, a Poly headset going into its charging stand, and a monitor
    being switched to another input.
    """

    def __init__(self, subsystems: Sequence[str] = HOTPLUG_SUBSYSTEMS) -> None:
        import pyudev

        self._context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        for subsystem in subsystems:
            # Filtering here rather than in Python: libudev installs it as a kernel socket filter,
            # so an unrelated uevent never wakes this process.
            self._monitor.filter_by(subsystem)
        self._monitor.start()

    def fileno(self) -> int:
        """For ``loop.add_reader``. Readable means at least one event is waiting."""
        return self._monitor.fileno()

    def drain(self) -> set[str]:
        """Every subsystem with an event waiting, without blocking.

        Drained in full rather than one at a time: plugging in a single device emits a burst --
        the USB device, then one event per interface, then the hidraw nodes -- and the answer to
        all of them is the same single re-enumeration.
        """
        seen: set[str] = set()
        while True:
            device = self._monitor.poll(timeout=0)
            if device is None:
                return seen
            seen.add(device.subsystem or "")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._monitor = None  # type: ignore[assignment]

    def __enter__(self) -> Hotplug:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


def watch(subsystems: Sequence[str] = HOTPLUG_SUBSYSTEMS) -> Hotplug | None:
    """A :class:`Hotplug` subscription, or ``None`` if this machine cannot provide one.

    ``None`` is an ordinary answer, not a failure: without ``pyudev`` the application works exactly
    as it did before, refreshing when the user presses Rescan. Nothing else should be degraded by
    its absence, so the import lives here rather than at module scope.
    """
    try:
        return Hotplug(subsystems)
    except ImportError:
        log.info("%s", HOTPLUG_HINT)
        return None
    except Exception:  # noqa: BLE001 - a machine without udev is not a broken installation
        log.warning("hotplug unavailable; Rescan still works", exc_info=True)
        return None


# --------------------------------------------------------------------------- Cache


_CACHE = "devices.json"


def load_cache() -> list[DeviceInfo]:
    """Last known devices, for painting the sidebar before enumeration finishes.

    Rendered normally, not greyed out: the user's headset was there last time and almost certainly
    still is. Reconciliation happens quietly ~30 ms later rather than behind a progress bar.
    """
    try:
        raw = json.loads((cache_dir() / _CACHE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for item in raw:
        try:
            out.append(
                DeviceInfo(
                    uid=item["uid"],
                    name=item["name"],
                    transport=Transport(item["transport"]),
                    category=Category(item.get("category", "other")),
                    vendor_id=item.get("vendor_id"),
                    product_id=item.get("product_id"),
                    address=item.get("address", ""),
                    module_id=item.get("module_id", ""),
                    # Without this a cached keyboard falls back to Category.INPUT's icon, which is
                    # `input-gaming` -- so a keyboard and a mouse both came up as gamepads for the
                    # second before the live scan replaced them.
                    icon_name=item.get("icon_name", ""),
                    properties=item.get("properties") or {},
                    state=State.UNKNOWN,
                )
            )
        except (KeyError, ValueError):
            continue
    return out


def save_cache(devices: list[DeviceInfo]) -> None:
    ensure(cache_dir())
    payload = [
        {
            "uid": d.uid,
            "name": d.name,
            "transport": d.transport.value,
            "category": d.category.value,
            "vendor_id": d.vendor_id,
            "product_id": d.product_id,
            "address": d.address,
            "module_id": d.module_id,
            "icon_name": d.icon_name,
            # Only what a module needs to find the device again -- a paired device's slot, say.
            # Everything else is re-derived by the live scan a moment later.
            "properties": {
                k: v for k, v in (d.properties or {}).items()
                if isinstance(v, (str, int, float, bool)) and not isinstance(v, bytes)
            },
        }
        for d in devices
    ]
    tmp = cache_dir() / f"{_CACHE}.tmp"
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(cache_dir() / _CACHE)


# --------------------------------------------------------------------------- helpers


def _parse_uevent(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if value:
            out[key] = value
    return out


_AUDIO_HINT = re.compile(r"head(set|phone)|audio|speak|evolve|voyager|wh-|jabra|poly", re.I)
_INPUT_HINT = re.compile(r"gamepad|controller|8bitdo|joystick|keyboard|mouse", re.I)


#: What BlueZ's icon name says a device is. Only the prefixes worth acting on; anything else falls
#: through to the name guess.
_ICON_CATEGORIES = {"input": Category.INPUT, "audio": Category.AUDIO}


def _icon_category(icon_name: str) -> Category | None:
    """Category from BlueZ's own icon hint, or None if it says nothing useful."""
    return _ICON_CATEGORIES.get(icon_name.split("-", 1)[0]) if icon_name else None


def _guess_category(name: str) -> Category:
    """Best-effort grouping for the sidebar before a module claims the device.

    Only affects which heading an unclaimed device appears under; a matched module's manifest
    always wins.
    """
    if _AUDIO_HINT.search(name):
        return Category.AUDIO
    if _INPUT_HINT.search(name):
        return Category.INPUT
    return Category.OTHER
