"""Bluetooth Classic RFCOMM transport (Linux, stdlib socket).

Opens the Sony configuration channel on an already-paired headset. The RFCOMM
channel number is dynamic and must be resolved via SDP for the Sony service
UUID; :func:`resolve_channel` tries a few strategies so PyBluez is optional.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import select
import shutil
import socket
import subprocess
import time

from ..protocol.enums import SERVICE_UUID

log = logging.getLogger(__name__)

# Prioritised RFCOMM channels to try when SDP tooling is unavailable. 9 is the
# real MDR config channel on a WH-1000XM4 (confirmed from an HCI capture of the
# official app); channel 11 is a DIFFERENT Sony service that answers with a
# fixed banner, so the probe validates the RET_PROTOCOL_INFO reply rather than
# accepting any frame. We deliberately do NOT sweep 1..30 — that exhausts the
# headset's limited RFCOMM slots and locks it out (learned the hard way).
_PROBE_CANDIDATES = (9, 10, 8, 12, 13, 15)

_CACHE_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "plasma-sony-headphones", "channels.json",
)


class TransportError(RuntimeError):
    pass


def _load_cache() -> dict:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except OSError as exc:
        log.debug("could not persist channel cache: %s", exc)


def resolve_channel(mac: str) -> int:
    """Resolve the RFCOMM channel for the Sony service on ``mac``.

    Strategy: cache -> PyBluez SDP -> ``sdptool`` -> gentle validated probe.
    """
    cache = _load_cache()
    if mac in cache:
        log.debug("using cached channel %s for %s", cache[mac], mac)
        return int(cache[mac])

    channel = _resolve_uncached(mac)
    cache[mac] = channel
    _save_cache(cache)
    return channel


def _resolve_uncached(mac: str) -> int:
    # 1) PyBluez (optional dependency)
    try:
        import bluetooth  # type: ignore

        services = bluetooth.find_service(uuid=SERVICE_UUID, address=mac)
        for svc in services:
            if svc.get("port"):
                log.debug("resolved channel %s via PyBluez", svc["port"])
                return int(svc["port"])
    except Exception as exc:  # noqa: BLE001
        log.debug("PyBluez SDP lookup unavailable: %s", exc)

    # 2) sdptool (deprecated; usually absent on modern BlueZ)
    if shutil.which("sdptool"):
        try:
            out = subprocess.run(
                ["sdptool", "records", mac],
                capture_output=True, text=True, timeout=15, check=False,
            ).stdout
            channel = _parse_sdptool_channel(out)
            if channel is not None:
                log.debug("resolved channel %s via sdptool", channel)
                return channel
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("sdptool lookup failed: %s", exc)

    # 3) gentle validated probe (no SDP tooling available)
    channel = _probe_channel(mac)
    if channel is not None:
        return channel

    raise TransportError(
        "Could not resolve the Sony RFCOMM channel. Install PyBluez, or ensure "
        "the headset is connected and not already in use by another app."
    )


def _probe_channel(mac: str) -> int | None:
    """Try a small candidate list; accept the channel that answers our
    GET_PROTOCOL_INFO with a genuine RET_PROTOCOL_INFO (command 0x01 on
    DATA_MDR). This rejects channel 11's fixed banner. One socket at a time."""
    from ..protocol import framing, messages
    from ..protocol.enums import Command, DataType

    probe = framing.encode(messages.build_get_protocol_info(), DataType.DATA_MDR, 0)
    for ch in _PROBE_CANDIDATES:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(3.0)
        try:
            sock.connect((mac, ch))
        except OSError:
            sock.close()
            continue
        try:
            sock.sendall(probe)
            reader = framing.FrameReader()
            for _ in range(3):  # read a few frames (ACK then RET)
                for f in reader.feed(sock.recv(4096)):
                    if (f.data_type == DataType.DATA_MDR
                            and f.command == Command.CONNECT_RET_PROTOCOL_INFO):
                        log.info("probe: channel %s is the MDR channel", ch)
                        return ch
        except OSError:
            pass
        finally:
            sock.close()
            time.sleep(0.3)  # let the DLC tear down before the next attempt
    return None


def _parse_sdptool_channel(text: str) -> int | None:
    """Very small parser: find the RFCOMM channel in the Sony service record."""
    uuid_short = SERVICE_UUID.replace("-", "").lower()
    in_sony = False
    channel = None
    for line in text.splitlines():
        low = line.lower()
        if "service name" in low or "service rechand" in low:
            in_sony = False
        if uuid_short[:8] in low.replace("0x", ""):
            in_sony = True
        if "channel" in low:
            for tok in low.replace(":", " ").split():
                if tok.isdigit():
                    if in_sony:
                        return int(tok)
                    channel = int(tok)  # remember last as a fallback
    return channel


class RfcommTransport:
    """Blocking RFCOMM socket wrapper."""

    def __init__(self, mac: str, channel: int | None = None, *, timeout: float = 10.0,
                 connect_timeout: float = 8.0, settle: float = 1.5) -> None:
        self.mac = mac
        self.channel = channel
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._settle = settle  # DLC/credit-negotiation settle before first send
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        if self.channel is None:
            self.channel = resolve_channel(self.mac)
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )
        # Require an authenticated + encrypted link, matching the reference C++
        # client. Constants: SOL_RFCOMM=18, RFCOMM_LM=0x03,
        # RFCOMM_LM_AUTH=0x0002 | RFCOMM_LM_ENCRYPT=0x0004.
        try:
            sock.setsockopt(18, 0x03, 0x0002 | 0x0004)
        except OSError as exc:
            log.debug("could not set RFCOMM link mode: %s", exc)

        # RFCOMM connects are non-blocking in the kernel: connect() reports
        # success before the DLC (incl. credit-based flow control) is actually
        # established. Sending into that window makes the headset reset the
        # channel (and, with no audio, power off). So: non-blocking connect,
        # wait for the socket to become ready, then let the DLC/credits settle
        # before anyone sends. Mirrors mos9527 issue #26.
        sock.setblocking(False)
        err = sock.connect_ex((self.mac, self.channel))
        if err not in (0, errno.EINPROGRESS):
            sock.close()
            raise TransportError(
                f"RFCOMM connect to {self.mac}:{self.channel} failed: {os.strerror(err)}")
        # wait for connect completion (writable) or an initial frame (readable)
        r, w, _ = select.select([sock], [sock], [], self._connect_timeout)
        if not (r or w):
            sock.close()
            raise TransportError(
                f"RFCOMM connect to {self.mac}:{self.channel} timed out")
        so_err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if so_err not in (0, errno.EINPROGRESS):
            sock.close()
            raise TransportError(
                f"RFCOMM connect to {self.mac}:{self.channel} failed: {os.strerror(so_err)}")
        # let the DLC parameter/credit negotiation finish before the first send
        time.sleep(self._settle)
        sock.settimeout(self._timeout)
        self._sock = sock
        log.info("connected to %s on RFCOMM channel %s", self.mac, self.channel)

    def send(self, data: bytes) -> int:
        if self._sock is None:
            raise TransportError("transport not connected")
        return self._sock.sendall(data) or len(data)

    def recv(self, n: int) -> bytes:
        if self._sock is None:
            raise TransportError("transport not connected")
        return self._sock.recv(n)

    def set_timeout(self, timeout: float) -> None:
        if self._sock is not None:
            self._sock.settimeout(timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None
