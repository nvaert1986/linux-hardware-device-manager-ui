"""RFCOMM transport for the Deckard config channel.

Two hard-won details carried over from the Sony MDR work on this machine:

* The kernel marks an RFCOMM socket writable before the DLC/credit negotiation has finished, so
  writing immediately after connect() can be dropped or reset the link. Connect non-blocking,
  wait for writability, then settle briefly before the first send.
* Never sweep channels looking for the service — that exhausts a headset's RFCOMM slots. Resolve
  the channel via SDP (see sdp.find_channel), which is also required here because Poly allocates
  the channel dynamically.
"""
from __future__ import annotations

import errno
import select
import socket
import time

from .framing import Frame, FrameBuffer
from . import sdp

PLT_HEADSET_DATA_SERVICE = "82972387-294e-4d62-97b5-2668aa35f618"

DEFAULT_CONNECT_TIMEOUT = 10.0
#: Pause after the socket reports writable, before the first write. See module docstring.
DEFAULT_SETTLE = 2.0


class TransportError(RuntimeError):
    pass


class RfcommTransport:
    def __init__(
        self,
        address: str,
        channel: int | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        settle: float = DEFAULT_SETTLE,
    ):
        self.address = address
        self.channel = channel
        self.connect_timeout = connect_timeout
        self.settle = settle
        self._sock: socket.socket | None = None
        self._buf = FrameBuffer()

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def description(self) -> str:
        return f"RFCOMM channel {self.channel}" if self.channel else "RFCOMM"

    def connect(self) -> int:
        """Resolve the channel if needed, connect, and settle. Returns the channel used."""
        if self.channel is None:
            self.channel = sdp.find_channel(self.address, PLT_HEADSET_DATA_SERVICE)
            if self.channel is None:
                raise TransportError(
                    f"{self.address} does not advertise PltHeadsetDataService — "
                    "is it a Poly device, and is it connected?"
                )

        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.setblocking(False)
        try:
            sock.connect((self.address, self.channel))
        except BlockingIOError:
            pass
        except OSError as exc:
            sock.close()
            raise TransportError(f"connect to channel {self.channel} failed: {exc}") from exc

        _, writable, _ = select.select([], [sock], [], self.connect_timeout)
        if not writable:
            sock.close()
            raise TransportError(f"timed out connecting to channel {self.channel}")
        err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            sock.close()
            raise TransportError(
                f"connect to channel {self.channel} failed: {errno.errorcode.get(err, err)}"
            )

        sock.setblocking(True)
        self._sock = sock
        time.sleep(self.settle)
        return self.channel

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- io ----------------------------------------------------------------------------------

    def send(self, frame: Frame) -> None:
        if self._sock is None:
            raise TransportError("not connected")
        self._sock.sendall(frame.encode())

    def receive(self, timeout: float = 3.0) -> list[Frame]:
        """Read whatever is available within `timeout`; return any complete frames."""
        if self._sock is None:
            raise TransportError("not connected")
        readable, _, _ = select.select([self._sock], [], [], timeout)
        if not readable:
            return []
        data = self._sock.recv(4096)
        if not data:
            raise TransportError("device closed the connection")
        return self._buf.feed(data)
