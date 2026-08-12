"""Synchronous MDR session — sequence/ACK choreography over a byte transport.

The transport is any object exposing ``send(bytes)`` and ``recv(n) -> bytes``
(see transport/rfcomm.py). This class is Qt-free and blocking; the Qt layer runs
it on a dedicated thread.

⚠️ The alternating-bit ACK handshake below is ported from mos9527's `master`
client. The framing itself is verified; this choreography is the one runtime
behaviour to validate against a real headset (protocol reference §4.3, §9).
"""

from __future__ import annotations

import logging
import threading

from . import framing
from .enums import DataType

log = logging.getLogger(__name__)


class SessionError(RuntimeError):
    pass


class Session:
    def __init__(self, transport, *, ack_grace: float = 0.6,
                 response_timeout: float = 3.0) -> None:
        self._t = transport
        self._reader = framing.FrameReader()
        self._pending: list[framing.Frame] = []
        self._seq = 0
        self._ack_grace = ack_grace            # wait this long after the ACK for a RET
        self._response_timeout = response_timeout
        self._lock = threading.Lock()
        # frames the device pushed unsolicited (RET/NTFY) waiting to be drained
        self.inbox: list[framing.Frame] = []

    # -- low level ---------------------------------------------------------

    def _read_frame(self, timeout: float | None = None) -> framing.Frame | None:
        """Return the next frame, or None if ``timeout`` elapses with none."""
        if self._pending:
            return self._pending.pop(0)
        if timeout is not None and hasattr(self._t, "set_timeout"):
            self._t.set_timeout(timeout)
        while True:
            try:
                data = self._t.recv(4096)
            except TimeoutError:
                return None
            if not data:
                raise SessionError("connection closed by device")
            frames = self._reader.feed(data)
            if frames:
                first, *rest = frames
                self._pending.extend(rest)
                return first

    def _send_ack(self, received_seq: int) -> None:
        self._t.send(framing.encode(b"", DataType.ACK, 1 - received_seq))

    # -- public API --------------------------------------------------------

    def send_command(self, payload: bytes, data_type: int = DataType.DATA_MDR) -> None:
        """Send one command and block until the device answers.

        Confirmed XM4 behaviour from an HCI capture of the official app
        (protocol reference §4.3):

            TX  cmd            seq=S
            RX  ACK            seq=1-S      (device confirms receipt)
            RX  RET (DATA_MDR) seq=1-S      (the actual answer — for GETs)
            TX  ACK            seq=S        (we acknowledge the RET)

        So we skip the receipt ACK and wait for the RET, which we ACK and stash.
        Commands that return no data (e.g. SETs) produce only the receipt ACK; we
        return shortly after it rather than blocking for a RET that never comes.
        """
        import time
        with self._lock:
            sent_seq = self._seq
            self._seq ^= 1  # advance for the next command
            # The RET for a GET/SET has the command byte with its low bit set
            # (GET_x=0x_6 -> RET_x=0x_7, etc.). We wait for *that* frame and
            # stash anything else (unsolicited NTFYs like playback 0xA9).
            expected = (payload[0] | 0x01) if payload else None
            self._t.send(framing.encode(payload, data_type, sent_seq))

            deadline = time.monotonic() + self._response_timeout
            got_ack = False
            while time.monotonic() < deadline:
                f = self._read_frame(timeout=self._ack_grace)
                if f is None:
                    if got_ack:
                        return          # receipt ack seen, no RET (e.g. a SET) — done
                    continue            # still waiting for the receipt ack
                if f.data_type == DataType.ACK:
                    got_ack = True
                    continue
                # ALWAYS acknowledge every data frame so the headset does not
                # retransmit (unacked frames pile up and it resets/powers off).
                self._send_ack(f.seq)
                self.inbox.append(f)
                if expected is not None and f.command == expected:
                    return              # this is our reply
                # otherwise an unsolicited NTFY / stale RET — keep waiting

    def send_reboot_command(self, payload: bytes,
                            data_type: int = DataType.DATA_MDR) -> bool:
        """Send a change that the device gates behind a confirm dialog.

        Flow (confirmed from HCI capture):
            TX <setting>              → RX 99 01 <msgType> <action>  (alert)
            TX 98 01 <msgType> 01     → RX <RET>  (applied) → device reboots

        The confirm MUST be sent promptly (the device expires the alert in a
        couple of seconds), so we answer POSITIVE the instant the alert arrives,
        then read briefly so the device commits before the link is torn down.
        Returns True if an alert was seen and confirmed.
        """
        import time
        from .enums import (ALERT_ACTION_POSITIVE, ALERT_INQUIRED_FIXED_MESSAGE,
                            Command)
        with self._lock:
            self._t.send(framing.encode(payload, data_type, self._seq))
            self._seq ^= 1
            confirmed = False
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                f = self._read_frame(timeout=0.5)
                if f is None:
                    if confirmed:
                        return True
                    continue
                if f.data_type == DataType.ACK:
                    continue
                self._send_ack(f.seq)
                if (not confirmed and f.command == Command.ALERT_NTFY_PARAM
                        and len(f.payload) >= 3
                        and f.payload[1] == ALERT_INQUIRED_FIXED_MESSAGE):
                    msg_type = f.payload[2]
                    confirm = bytes((Command.ALERT_SET_PARAM,
                                     ALERT_INQUIRED_FIXED_MESSAGE, msg_type,
                                     ALERT_ACTION_POSITIVE))
                    self._t.send(framing.encode(confirm, data_type, self._seq))
                    self._seq ^= 1
                    confirmed = True
                    # keep reading ~1.5s so the device applies before we tear down
                    deadline = time.monotonic() + 1.5
                else:
                    self.inbox.append(f)
            return confirmed

    def pump(self) -> list[framing.Frame]:
        """Drain and return any queued/available inbound data frames.

        Blocks up to one recv for new traffic; used by the listen loop to catch
        NTFY pushes (battery/button changes) when we're not sending.
        """
        out = list(self.inbox)
        self.inbox.clear()
        try:
            data = self._t.recv(4096)
        except (TimeoutError, OSError):
            return out
        if data:
            for f in self._reader.feed(data):
                if f.data_type == DataType.ACK:
                    continue
                self._send_ack(f.seq)
                out.append(f)
        return out
