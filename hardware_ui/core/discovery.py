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
import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path

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
        kind = _fido_kind(node) or _dock_kind(name) or _hid_kind(usb)
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
        paired = re.search(r"^\s*Paired:\s*yes", info.stdout, re.I | re.M) is not None
        out.append(
            DeviceInfo(
                uid=f"bt:{address}",
                name=name,
                transport=Transport.BLUETOOTH,
                category=_guess_category(name),
                address=address,
                uuids=uuids,
                # PAIRED, not PRESENT: BlueZ lists a paired headset whether or not it is
                # switched on, so being in this list does not mean it can be opened.
                state=(
                    State.CONNECTED if connected else State.PAIRED if paired else State.PRESENT
                ),
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


def enumerate_all() -> list[DeviceInfo]:
    """Every transport, in one sweep. Individual backends fail soft."""
    devices: list[DeviceInfo] = []
    for fn in (enumerate_hid, enumerate_bluetooth, enumerate_displays):
        try:
            devices.extend(fn())
        except Exception:
            log.exception("%s failed; continuing with other transports", fn.__name__)
    return devices


#: Subsystems worth waking up for. Anything else on the bus is somebody else's business, and the
#: filter is installed in the kernel, so an unrelated event never reaches this process at all.
HOTPLUG_SUBSYSTEMS = ("usb", "hidraw", "drm")

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
