"""Small BlueZ D-Bus helpers.

The WH-1000XM3/XM4 report battery over HFP, which BlueZ exposes as
``org.bluez.Battery1.Percentage`` — not over the MDR config protocol. Reading it
here avoids any MDR probing for battery.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _device_path(bus, mac: str) -> str | None:
    import dbus  # python-dbus

    mgr = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager",
    )
    suffix = "dev_" + mac.upper().replace(":", "_")
    for path, ifaces in mgr.GetManagedObjects().items():
        if str(path).endswith(suffix):
            return str(path)
    return None


# A2DP vendor codecs keyed by (vendor_id, codec_id), little-endian from the
# MediaTransport1 Configuration blob.
_VENDOR_CODECS = {
    (0x012D, 0x00AA): "LDAC",
    (0x004F, 0x0001): "aptX",
    (0x00D7, 0x0024): "aptX HD",
    (0x00D7, 0x0002): "aptX Low Latency",
    (0x00D7, 0x00AD): "aptX Adaptive",
    (0x053A, 0x4C33): "LHDC",
}


# MediaTransport1 UUIDs. Codec ids are only meaningful *within* a profile, so the transport's
# UUID has to be consulted before naming anything.
A2DP_SOURCE = "0000110a"
A2DP_SINK = "0000110b"
HFP_HF = "0000111e"
HFP_AG = "0000111f"

#: HFP codec ids. Note id 2 is mSBC here but AAC under A2DP -- reading the codec without
#: checking the profile reports a handsfree link as "AAC", which is how a headset that came back
#: from a reboot in HFP mode appeared to be using an A2DP codec.
_HFP_CODECS = {1: "CVSD", 2: "mSBC"}


def _hfp_codec_name(codec: int) -> str:
    return _HFP_CODECS.get(codec, f"HFP codec {codec}")


def _codec_name(codec: int, config: bytes) -> str:
    if codec == 0x00:
        return "SBC"
    if codec == 0x02:
        return "AAC"
    if codec == 0xFF and len(config) >= 6:
        vendor = int.from_bytes(config[0:4], "little")
        cid = int.from_bytes(config[4:6], "little")
        return _VENDOR_CODECS.get((vendor, cid), f"Vendor {vendor:#06x}/{cid:#06x}")
    return f"Codec {codec}"


def active_codec(mac: str) -> str | None:
    """Return the negotiated A2DP codec (SBC/AAC/aptX/LDAC/...) from BlueZ, or None."""
    try:
        import dbus

        bus = dbus.SystemBus()
        mgr = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        dev = "dev_" + mac.upper().replace(":", "_")
        best = None
        for path, ifaces in mgr.GetManagedObjects().items():
            props = ifaces.get("org.bluez.MediaTransport1")
            if props is None or dev not in str(path):
                continue
            codec = int(props.get("Codec", -1))
            config = bytes(bytearray(props.get("Configuration", b"")))
            state = str(props.get("State", ""))
            uuid = str(props.get("UUID", "")).lower()
            is_a2dp = uuid.startswith((A2DP_SOURCE, A2DP_SINK))
            # Prefer an A2DP transport over HFP, and an active stream over an idle endpoint.
            # A headset reconnecting after a reboot often exposes HFP first; reporting its codec
            # as the "active codec" is misleading when music will play over A2DP moments later.
            rank = (is_a2dp, state in ("active", "pending"))
            if best is None or rank > best[3]:
                best = (codec, config, uuid, rank)
        if best is None:
            return None
        codec, config, uuid, rank = best
        if not rank[0]:
            # Handsfree link. Say so, rather than naming it as if it were A2DP.
            return f"{_hfp_codec_name(codec)} (handsfree)"
        return _codec_name(codec, config)
    except Exception as exc:  # noqa: BLE001
        log.debug("BlueZ codec read failed for %s: %s", mac, exc)
        return None


def battery_percentage(mac: str) -> int | None:
    """Return the headset battery percentage from BlueZ, or None if unavailable."""
    try:
        import dbus  # python-dbus

        bus = dbus.SystemBus()
        path = _device_path(bus, mac)
        if path is None:
            return None
        props = dbus.Interface(
            bus.get_object("org.bluez", path),
            "org.freedesktop.DBus.Properties",
        )
        return int(props.Get("org.bluez.Battery1", "Percentage"))
    except Exception as exc:  # noqa: BLE001 - dbus/module absence is non-fatal
        log.debug("BlueZ battery read failed for %s: %s", mac, exc)
        return None
