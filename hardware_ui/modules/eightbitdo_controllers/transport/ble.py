"""Configuration over the controller's hidden BLE config radio.

These are *wired* controllers with a BLE radio inside, which exists because the vendor's Android
app is the configurator. Holding the config-mode button makes the controller advertise as
``82CE``; this module then speaks the same inner messages the USB path does.

**BlueZ cannot do this, and that is not a bug in BlueZ.** The controller refuses the notify CCCD
write -- the vendor's own Android code has an ``onNotifyFailure`` handler that expects the refusal
and carries on -- and notifications arrive regardless. BlueZ treats the refused CCCD write as
fatal, so ``StartNotify`` and ``AcquireNotify`` both end up delivering nothing. A raw ATT client on
the L2CAP fixed channel receives Handle-Value-Notifications whatever the CCCD says, which is
effectively what Android does. Anyone tempted to "fix" this back to BlueZ should try it first.

**The super-config is not readable over BLE.** Only the three 176-byte slots are, via ``0x0d``. So
a write has to rebuild the 532-byte record from those slots plus a four-byte header, and the
header carries the rolling checksum's previous value -- which is why :mod:`.store` caches it and
why a controller wants to be seen once over USB. See :mod:`~..protocol.record.roll_crc`.

Ported from the source project's ``gui/bit8/ble.py``, with the bundled captured session dropped:
the packet sequence is built from constants here instead, which the source's own ``ble_rmw.py``
had already shown was possible.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import select
import socket
import struct
import time

from ..protocol import fieldmap as fm
from ..protocol import message

log = logging.getLogger(__name__)

#: What the controller advertises as once the config-mode button is held.
CONFIG_NAME = "82CE"

#: ATT value handle of characteristic 2B11, which the app both writes to and is notified on.
WRITE_HANDLE = 0x0015
#: Its client-configuration descriptor. Writing it is *expected to fail*; see the module docstring.
CCCD_HANDLE = 0x0016

#: Inner data bytes per BLE packet. Smaller than USB's because the ATT MTU is smaller.
CHUNK = 45

ATT_MTU = 67

#: ATT opcodes, only the handful this needs.
ATT_MTU_REQ, ATT_MTU_RSP = 0x02, 0x03
ATT_ERROR = 0x01
ATT_WRITE_REQ, ATT_WRITE_RSP = 0x12, 0x13
ATT_WRITE_CMD = 0x52
ATT_NOTIFY, ATT_INDICATE, ATT_CONFIRM = 0x1B, 0x1D, 0x1E

AF_BLUETOOTH, BTPROTO_L2CAP, SOCK_SEQPACKET = 31, 0, 5
ATT_CID = 4
BDADDR_LE_PUBLIC = 1


class TransportError(Exception):
    pass


class _SockaddrL2(ctypes.Structure):
    # Fully byte-packed, so the MSVC and gcc layouts coincide; naming one silences Python 3.14's
    # deprecation of the implicit choice. Older ctypes ignores the attribute.
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [
        ("l2_family", ctypes.c_ushort),
        ("l2_psm", ctypes.c_ushort),
        ("l2_bdaddr", ctypes.c_ubyte * 6),
        ("l2_cid", ctypes.c_ushort),
        ("l2_bdaddr_type", ctypes.c_ubyte),
    ]


def _sockaddr(address: str) -> _SockaddrL2:
    out = _SockaddrL2()
    out.l2_family = AF_BLUETOOTH
    out.l2_psm = 0
    out.l2_cid = ATT_CID
    out.l2_bdaddr = (ctypes.c_ubyte * 6)(
        *bytes(int(part, 16) for part in reversed(address.split(":"))))
    out.l2_bdaddr_type = BDADDR_LE_PUBLIC
    return out


# --------------------------------------------------------------------------- the packet sequence
#
# Built from constants rather than replayed from a captured app session. The source project
# shipped a captured session and rebuilt the data chunks inside it; carrying someone's captured
# traffic as a required asset is both a redistribution question and a thing that rots.

def _control(payload: bytes) -> bytes:
    return message.request(message.CMD_CONTROL, size=4, total=4,
                           offset=message.CONTROL_OFFSET, data=payload)


def _state(param: int) -> bytes:
    return message.request(message.CMD_STATE, field=param)


HANDSHAKE = (
    _control(message.CONTROL_READ),
    _state(1),
    _state(0),
    message.request(0x0002, size=4, total=4, data=bytes(8)),
)
"""Opens a session. `0x0b` then setConfigState 1 and 0, then a status read."""

#: The save block, shared with the USB path. It was reconstructed here from constants in a
#: plausible-looking order -- control, state, a read, then the chunks, then a commit -- and the
#: captured session says otherwise: the control that opens a save comes first, the report-enable
#: and calibration packets sit between it and the record, and the sequence ends with a finalize
#: that is what actually commits. Same bytes on both transports.
WRITE_PREFIX = message.save_prologue()
FINALIZE = message.finalize()


# --------------------------------------------------------------------------- discovery


def scan(timeout: float = 4.0) -> list[dict[str, str]]:
    """Controllers currently advertising in config mode.

    **Not called during startup enumeration.** A four-second LE scan is two orders of magnitude
    over discovery's budget, and a controller only advertises while its config-mode button has
    been held, so there is nothing to find unless the user has just done that. The shell offers
    this as an action instead.
    """
    try:
        import dbus
    except ImportError as exc:
        raise TransportError("scanning for controllers needs dev-python/dbus-python") from exc

    bus = dbus.SystemBus()
    adapter = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"), "org.bluez.Adapter1")
    with contextlib.suppress(Exception):
        adapter.SetDiscoveryFilter({"Transport": "le"})
    with contextlib.suppress(Exception):
        adapter.StartDiscovery()
    time.sleep(timeout)

    manager = dbus.Interface(bus.get_object("org.bluez", "/"),
                             "org.freedesktop.DBus.ObjectManager")
    found = {}
    for _, interfaces in manager.GetManagedObjects().items():
        device = interfaces.get("org.bluez.Device1")
        if not device:
            continue
        name = str(device.get("Name", device.get("Alias", "")))
        if name == CONFIG_NAME:
            found[str(device["Address"])] = {"address": str(device["Address"]), "name": name}
    with contextlib.suppress(Exception):
        adapter.StopDiscovery()
    return list(found.values())


def adapter_address() -> str:
    import dbus

    bus = dbus.SystemBus()
    properties = dbus.Interface(bus.get_object("org.bluez", "/org/bluez/hci0"),
                                "org.freedesktop.DBus.Properties")
    return str(properties.Get("org.bluez.Adapter1", "Address"))


# --------------------------------------------------------------------------- the link


class Session:
    """A raw ATT channel to one controller."""

    def __init__(self, address: str, adapter: str = "") -> None:
        self.address = address
        self.adapter = adapter or adapter_address()
        self._socket: socket.socket | None = None

    def __enter__(self) -> Session:
        # BlueZ holds the link if it has connected; take it back before opening our own channel.
        with contextlib.suppress(Exception):
            import dbus

            path = "/org/bluez/hci0/dev_" + self.address.replace(":", "_")
            dbus.Interface(dbus.SystemBus().get_object("org.bluez", path),
                           "org.bluez.Device1").Disconnect()
            time.sleep(1.0)

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        sock = socket.socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
        libc.bind(sock.fileno(), ctypes.byref(_sockaddr(self.adapter)),
                  ctypes.sizeof(_SockaddrL2))
        if libc.connect(sock.fileno(), ctypes.byref(_sockaddr(self.address)),
                        ctypes.sizeof(_SockaddrL2)) != 0:
            sock.close()
            raise TransportError(
                f"cannot reach {self.address} (errno {ctypes.get_errno()}). "
                "Is the controller in config mode, advertising as 82CE?")
        self._socket = sock

        sock.send(struct.pack("<BH", ATT_MTU_REQ, ATT_MTU))
        time.sleep(0.3)
        # Expected to be refused. The device notifies anyway; see the module docstring.
        with contextlib.suppress(OSError):
            sock.send(struct.pack("<BH", ATT_WRITE_REQ, CCCD_HANDLE) + b"\x01\x00")
        return self

    def __exit__(self, *_: object) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    # ------------------------------------------------------------------ io

    def _write(self, packet: bytes) -> None:
        assert self._socket is not None
        self._socket.send(struct.pack("<BH", ATT_WRITE_CMD, WRITE_HANDLE) + packet)

    def _pump(self, seconds: float, sink=None) -> None:
        """Service the channel for a while, handing config responses to *sink*."""
        assert self._socket is not None
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self._socket], [], [], 0.15)
            if not ready:
                continue
            pdu = self._socket.recv(512)
            if not pdu:
                continue
            opcode = pdu[0]
            if opcode == ATT_MTU_REQ:
                self._socket.send(struct.pack("<BH", ATT_MTU_RSP, ATT_MTU))
            elif opcode == ATT_ERROR:
                log.debug("ATT error: request 0x%02x error 0x%02x", pdu[1], pdu[4])
            elif opcode in (ATT_NOTIFY, ATT_INDICATE):
                if opcode == ATT_INDICATE:
                    self._socket.send(bytes([ATT_CONFIRM]))
                if sink is not None:
                    sink(pdu[3:])

    def _send_all(self, packets, per_packet: float = 0.25) -> None:
        for packet in packets:
            self._write(packet)
            self._pump(per_packet)

    # ------------------------------------------------------------------ config

    def read_slot(self, slot: int) -> bytes:
        """One 176-byte profile. The only read BLE offers."""
        buffer = bytearray(b"\xff" * fm.SLOT_LEN)
        seen: set[int] = set()

        def sink(value: bytes) -> None:
            parsed = message.parse_response(value)
            if parsed is None:
                return
            _, command, size, _, offset, data = parsed
            if command == message.CMD_SLOT and 0 <= offset < fm.SLOT_LEN and data:
                buffer[offset:offset + len(data)] = data
                seen.add(offset)

        self._send_all(HANDSHAKE, 0.4)
        for offset, size in message.chunks(fm.SLOT_LEN, CHUNK):
            self._write(message.request(message.CMD_SLOT, field=slot, size=size,
                                        total=fm.SLOT_LEN, offset=offset, data=bytes(size)))
            self._pump(0.6, sink)
        if not seen:
            raise TransportError(f"the controller did not answer any read for slot {slot}")
        return bytes(buffer)

    def read_slots(self) -> list[bytes]:
        return [self.read_slot(slot) for slot in range(fm.SLOT_COUNT)]

    def write_super(self, record: bytes) -> None:
        """Write the assembled 532-byte record, then commit and run the app's post-sequence."""
        if len(record) != fm.SUPER_LEN:
            raise TransportError(f"a super-config is {fm.SUPER_LEN} bytes, got {len(record)}")
        self._send_all(WRITE_PREFIX, 0.25)
        for offset, size in message.chunks(fm.SUPER_LEN, CHUNK):
            self._write(message.request(message.CMD_WRITE_SUPER, size=size, total=fm.SUPER_LEN,
                                        offset=offset, data=record[offset:offset + size]))
            self._pump(0.12)
        # Finalize is the last packet of the captured save and the one that commits, so nothing
        # follows it. The report-enable and calibration packets that used to be sent here belong
        # *before* the record, which is where the capture puts them and where they are now.
        self._write(FINALIZE)
        self._pump(1.2)


def read_slots(address: str, adapter: str = "") -> list[bytes]:
    with Session(address, adapter) as session:
        return session.read_slots()


def write_super(record: bytes, address: str, adapter: str = "") -> None:
    with Session(address, adapter) as session:
        session.write_super(record)


__all__ = ["CONFIG_NAME", "Session", "TransportError", "adapter_address", "read_slots", "scan",
           "write_super"]
