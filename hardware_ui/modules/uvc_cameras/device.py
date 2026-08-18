"""The shell's view of a UVC camera.

**Generic by construction.** The manifest claims any V4L2 capture device, and the page is built from
what the device reports rather than from a model table — so an unknown webcam produces a correct
page of standard controls. Vendor extras are additive on top, for the models in
:mod:`.protocol.extensions`.

**It does not take the camera.** Opening a video node read-write to change controls does not claim
the stream, so settings can be changed while a call is running. That is the advantage of doing this
through V4L2 instead of USB, and it is why this module needs no advisory about interrupting the
device — unlike every other module here.

**Nothing is saved to the camera**, with one exception. Standard V4L2 controls are volatile: they
reset when the camera loses power. Only two things in this module survive that — a Logitech
conference camera's stored pan/tilt positions, and a Razer Kiyo Pro's save command — because those
cameras keep them themselves. See :meth:`advisories`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from hardware_ui.core.capability import Advisory, CapabilitySet
from hardware_ui.core.connection import ConnectionLabel
from hardware_ui.core.device import Device, DeviceInfo

from . import capabilities as C
from .protocol import extensions as ext
from .protocol.ioctls import UVC_GET_MAX, UVC_GET_MIN
from .protocol.session import CameraError, Control, Session, unit_id, usb_ids

log = logging.getLogger(__name__)

#: Opening a video node and walking its controls is a few dozen ioctls on a local character device.
CONNECT_TIMEOUT = 10.0

VOLATILE = (
    "Camera settings are not stored in the camera: it returns to its defaults whenever it loses "
    "power. That is how UVC works rather than a limitation here — nothing on this page except a "
    "stored pan/tilt position is kept by the hardware."
)

#: Shown on the streaming-mode controls, because the obvious expectation of them is the wrong one.
#:
#: Measured 2026-08-18. The camera was set to 1280×720 MJPG at 30 fps, then each application was run
#: against it with no resolution requested:
#:
#: ===============  =========================  =========
#: application      left the camera at         verdict
#: ===============  =========================  =========
#: ffmpeg           1280×720 **YUYV** 30       overrode
#: VLC              **1920×1080** YUYV 30      overrode
#: GStreamer        **640×480** MJPG **120**   overrode
#: no ``S_FMT``     1280×720 MJPG 30           honoured
#: ===============  =========================  =========
#:
#: Three for three. The last row is a capture program written for this test that deliberately never
#: calls ``VIDIOC_S_FMT``; it received a real 1280×720 JPEG, which is what proves the setting is
#: genuine device state rather than decoration. But no application anybody actually uses behaves
#: that way, so the honest summary is that this changes what the camera is set to and changes
#: nothing about what a capture application shows.
NEGOTIATED = (
    "This sets the camera's current mode, but it will not change what a capture application "
    "shows. Every application tested asks for its own format when it opens the camera and gets "
    "it — ffmpeg, VLC and GStreamer all overrode a 1280×720 setting, each with a different mode "
    "of its own, and Kamoso does the same whether or not you restart it. Only a program that "
    "never asks keeps what is set here, and normal capture applications all ask. The image "
    "controls on the other tabs are unaffected by this and take effect live, even while "
    "something is streaming."
)


class UvcCamera(Device):
    """One camera, reached through its V4L2 node."""

    connect_timeout = CONNECT_TIMEOUT

    def __init__(self, info: DeviceInfo) -> None:
        super().__init__(info)
        self._session: Session | None = None
        self._capabilities = CapabilitySet([])
        self._controls: dict[str, Control] = {}
        self._vendor: dict[str, tuple[ext.Extension, ext.VendorControl, int, int]] = {}
        """key -> (extension, control, unit id, buffer length), resolved once at connect."""
        self._advisories: dict[str, Advisory] = {}
        self._identity: tuple[str, str] = ("", "")
        self._formats: list[str] = []
        self._streams: list[C.StreamFormat] = []
        self._mode: tuple[int, int, str, float] | None = None

    # ------------------------------------------------------------------ lifecycle

    def connect_notice(self) -> str:
        return ""       # local ioctls; there is nothing to wait for and nothing to warn about

    async def connect(self) -> None:
        session = Session(self.info.path or "")
        try:
            await asyncio.to_thread(session.open)
            await asyncio.to_thread(self._read_everything, session)
        except CameraError as exc:
            session.close()
            raise RuntimeError(str(exc)) from exc
        self._session = session
        self._describe()

    async def disconnect(self) -> None:
        if self._session is not None:
            await asyncio.to_thread(self._session.close)
            self._session = None

    def connection_label(self) -> ConnectionLabel:
        if self._session is None:
            return ConnectionLabel("Not connected", "")
        card, driver = self._identity
        return ConnectionLabel("V4L2", f"{driver} · {card}" if driver else card)

    # ------------------------------------------------------------------ reading the device

    def _read_everything(self, session: Session) -> None:
        card, driver, _bus = session.capability()
        self._identity = (card, driver)
        self._controls = {C.standard_key(c): c for c in session.controls()}
        self._formats = session.formats()
        self._streams = _streams(session, self._formats)
        self._mode = session.current_mode()
        self._vendor = self._resolve_vendor(session)

    def _resolve_vendor(self, session: Session) -> dict:
        """Which vendor controls this camera really has.

        Three gates, in cost order, and the point of the order is that a camera pays only for the
        checks that can still succeed: the model filter is a dictionary lookup, the GUID search
        reads one sysfs file, and only then is the device asked anything.
        """
        _vendor_id, product_id = usb_ids(session.node)
        found: dict = {}
        for extension in ext.for_product(product_id):
            unit = unit_id(session.node, extension.guid)
            if unit == 0:
                continue
            for control in extension.controls:
                length = session.xu_length(unit, control.selector)
                if length == 0:
                    continue        # the unit is there; this model does not implement the control
                key = C.vendor_key(extension, control)
                if key in found:
                    continue        # an earlier unit already published it; first wins
                found[key] = (extension, control, unit, length)
                log.debug("%s: %s on unit %d selector %#04x, %d bytes",
                          self.info.name, key, unit, control.selector, length)
        return found

    # ------------------------------------------------------------------ capabilities

    @property
    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    def _describe(self) -> None:
        session = self._require()
        vendor: list[tuple[ext.Extension, ext.VendorControl]] = []
        for extension, control, unit, length in self._vendor.values():
            resolved = control
            if control.kind == "range":
                # A vendor range does not declare its bounds in the table: the unit is asked,
                # exactly as a V4L2 control reports its own. GET_MIN and GET_MAX return the whole
                # buffer, so the byte at this control's offset is this control's bound.
                low = session.xu_read(unit, control.selector, length, request=UVC_GET_MIN)
                high = session.xu_read(unit, control.selector, length, request=UVC_GET_MAX)
                resolved = dataclasses.replace(
                    control,
                    minimum=low[control.offset] if low else 0,
                    maximum=high[control.offset] if high else 255,
                )
            vendor.append((extension, resolved))

        self._capabilities = CapabilitySet(C.build(
            controls=list(self._controls.values()),
            vendor=vendor,
            card=self._identity[0], driver=self._identity[1],
            node=self.info.path or "",
            formats=self._formats, streams=self._streams, mode=self._mode,
        ))
        self._advise()
        self._bump_capabilities()

    def _advise(self) -> None:
        """Lock what the driver says is inactive, and say that nothing is saved.

        ``V4L2_CTRL_FLAG_INACTIVE`` is the device telling us a control is meaningless right now --
        ``focus_absolute`` while continuous autofocus is on. That is a gate we get for free instead
        of encoding "focus depends on autofocus" ourselves, and it is re-read after every write,
        because turning autofocus off is what makes focus live.
        """
        self._advisories = {}
        for key, control in self._controls.items():
            if control.inactive:
                self._advisories[key] = Advisory(
                    f"{control.name} is not adjustable while the camera is controlling it "
                    f"automatically. Turn the matching automatic setting off to use it.",
                    locked=True,
                )
        first = next((c.key for c in self._capabilities if c.group == C.GROUP_CAMERA), None)
        if first is not None and first not in self._advisories:
            self._advisories[first] = Advisory(VOLATILE)
        # Not locked: the write genuinely works, and refusing it would be a different lie from the
        # one this prevents. The control is honest about its reach instead of pretending to have
        # more.
        if any(c.key == C.KEY_PIXELFORMAT for c in self._capabilities):
            self._advisories[C.KEY_PIXELFORMAT] = Advisory(NEGOTIATED)

    def advisories(self) -> dict[str, Advisory]:
        return dict(self._advisories)

    # ------------------------------------------------------------------ reads

    async def get(self, key: str) -> Any:
        return (await self.get_many([key])).get(key)

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_many, keys)

    def _read_many(self, keys: list[str]) -> dict[str, Any]:
        return {key: self._value(key) for key in keys}

    def _value(self, key: str) -> Any:
        session = self._require()
        control = self._controls.get(key)
        if control is not None:
            if control.kind == "button":
                return None
            live = session.get(control.ident)
            control.value = live
            return bool(live) if control.kind == "boolean" else live

        vendor = self._vendor.get(key)
        if vendor is not None:
            extension, spec, unit, length = vendor
            if not spec.readable:
                # A command, not a field. Nothing to report and nothing that could be reported:
                # these units answer SET_CUR and have no meaningful current value.
                return None
            raw = session.xu_read(unit, spec.selector, length)
            if not raw:
                return None
            byte = raw[spec.offset]
            if spec.kind == "choice":
                return next((label for label, value in spec.choices if value == byte), None)
            return byte

        return {
            C.KEY_CARD: self._identity[0],
            C.KEY_DRIVER: self._identity[1],
            C.KEY_NODE: self.info.path or "",
            C.KEY_MODE: _mode_text(self._mode),
            # The three writable mode controls read back from the same place the readout does: the
            # node's current format. There is no separate stored value to consult -- what the camera
            # is set to *is* the value, which is why a write that the driver substitutes shows the
            # substituted mode rather than the request.
            C.KEY_PIXELFORMAT: self._mode[2] if self._mode else None,
            C.KEY_RESOLUTION: f"{self._mode[0]}×{self._mode[1]}" if self._mode else None,
            C.KEY_FRAME_RATE: f"{self._mode[3]:g}" if self._mode and self._mode[3] else None,
            **{C.stream_key(s.pixelformat): s.summary() for s in self._streams},
        }.get(key)

    async def refresh(self) -> dict[str, Any]:
        """Re-read everything, including the inactive flags.

        Cheap: local ioctls on a character device, no protocol round trips. So unlike the headset
        modules this can be polled without thinking about traffic.
        """
        await asyncio.to_thread(self._read_everything, self._require())
        self._describe()
        return await self.get_many([c.key for c in self._capabilities])

    # ------------------------------------------------------------------ writes

    async def set(self, key: str, value: Any) -> Any | None:
        landed = await asyncio.to_thread(self._write, key, value)
        # An automatic setting going on or off changes which other controls are adjustable, and the
        # device reports that through its inactive flags rather than us predicting it.
        await asyncio.to_thread(self._refresh_flags)
        self._advise()
        return landed

    def _write(self, key: str, value: Any) -> Any:
        session = self._require()
        if key in (C.KEY_PIXELFORMAT, C.KEY_RESOLUTION, C.KEY_FRAME_RATE):
            return self._write_mode(session, key, value)

        control = self._controls.get(key)
        if control is not None:
            try:
                return session.set(control.ident, int(bool(value)) if control.kind == "boolean"
                                   else 0 if control.kind == "button" else int(value))
            except CameraError as exc:
                raise RuntimeError(f"{control.name}: {exc}") from exc

        base, _, action = key.rpartition(".")
        vendor = self._vendor.get(key) or self._vendor.get(base)
        if vendor is None:
            raise RuntimeError(f"{key} is not a control this camera has")
        extension, spec, unit, length = vendor
        label = self._label_for(spec, action, value)
        payload = next((v for text, v in spec.choices if text == label), None)
        try:
            if spec.write == ext.WRITE_BLOB:
                for prelude_label, prelude in spec.prelude:
                    if prelude_label == label:
                        session.xu_write_blob(unit, spec.selector, prelude)
                if payload is None:
                    raise RuntimeError(f"{spec.name}: no such setting {label!r}")
                session.xu_write_blob(unit, spec.selector, payload)  # type: ignore[arg-type]
            elif spec.kind == "range":
                session.xu_write_byte(unit, spec.selector, length, spec.offset, int(value))
            else:
                if payload is None:
                    raise RuntimeError(f"{spec.name}: no such setting {label!r}")
                session.xu_write_byte(unit, spec.selector, length, spec.offset, int(payload))
        except CameraError as exc:
            raise RuntimeError(f"{spec.name}: {exc}") from exc
        return label if spec.kind != "range" else int(value)

    def _write_mode(self, session: Session, key: str, value: Any) -> Any:
        """Change the pixel format, the frame size or the frame rate.

        Reopens the node afterwards, and re-reads the whole mode table. Both are needed and for
        different reasons: cameractrls marks all three of these controls ``reopener=True`` because
        changing them locks the device, and the *lists* change -- a different pixel format offers
        different sizes, and a different size offers different rates -- so the two dropdowns below
        the one that changed have to be rebuilt from the camera rather than left showing modes it no
        longer has.
        """
        try:
            if key == C.KEY_PIXELFORMAT:
                landed: Any = session.set_pixelformat(str(value))
            elif key == C.KEY_RESOLUTION:
                width, height = _parse_size(str(value))
                landed = "×".join(str(n) for n in session.set_resolution(width, height))
            else:
                landed = f"{session.set_frame_rate(float(str(value))):g}"
        except CameraError as exc:
            # Re-read regardless: a refused write can still have moved something, and showing a
            # stale page after a failure is how a user comes to believe a setting took when it did
            # not.
            self._reload_mode(session)
            raise RuntimeError(str(exc)) from exc
        self._reload_mode(session)
        return landed

    def _reload_mode(self, session: Session) -> None:
        session.reopen()
        self._formats = session.formats()
        self._streams = _streams(session, self._formats)
        self._mode = session.current_mode()
        # Rebuilds the page, which is unavoidable here and deliberate: the choices themselves
        # changed. This is the one write in this module that does it -- the image controls take the
        # cheaper path of updating values in place, because a brightness write cannot change which
        # controls exist.
        self._describe()

    @staticmethod
    def _label_for(spec: ext.VendorControl, action: str, value: Any) -> str:
        """Which of a vendor control's choices a write means.

        A CHOICE carries its own label as the value. An ACTION carries nothing -- the shell sends
        ``True`` -- so the label is recovered from the key's last segment, which is why
        :func:`capabilities._vendor_rows` gives every action its own key instead of hanging several
        buttons off one.
        """
        if spec.kind != "action":
            return str(value)
        wanted = action.replace("_", " ")
        return next((label for label, _ in spec.choices if label.lower() == wanted), wanted)

    def _refresh_flags(self) -> None:
        """Re-read the control list for its flags, keeping the same keys."""
        session = self._require()
        for control in session.controls():
            existing = self._controls.get(C.standard_key(control))
            if existing is not None:
                existing.inactive = control.inactive
                existing.readonly = control.readonly
                existing.value = control.value

    # ------------------------------------------------------------------ helpers

    def _require(self) -> Session:
        if self._session is None or not self._session.opened:
            raise RuntimeError(f"{self.info.name} is not connected")
        return self._session


def _streams(session: Session, formats: list[str]) -> list[C.StreamFormat]:
    """The full mode table: every format, every size, every rate.

    Replaces a number that was simply wrong -- taking the largest size of the *first* format
    reported a 4K BRIO as 1920x1080 because YUYV came before MJPG -- and the whole table is needed
    anyway now that the resolution and frame rate can be chosen. Measured at 0.4 ms for a BRIO's 43
    size/format pairs, so there is nothing to gain by enumerating less.
    """
    out: list[C.StreamFormat] = []
    for pixelformat in formats:
        sizes = tuple(session.resolutions(pixelformat))
        rates = {
            size: tuple(session.frame_rates(pixelformat, *size))
            for size in sizes
        }
        out.append(C.StreamFormat(pixelformat, sizes, rates))
    return out


def _parse_size(text: str) -> tuple[int, int]:
    """``"1920×1080"`` -> ``(1920, 1080)``. The label is the value for these choices."""
    width, _, height = text.partition("×")
    return int(width), int(height)


def _mode_text(mode: tuple[int, int, str, float] | None) -> str:
    if mode is None:
        return "unknown"
    width, height, pixelformat, fps = mode
    rate = f" at {fps:g} fps" if fps else ""
    return f"{width}×{height} {pixelformat}{rate}"


__all__ = ["UvcCamera"]
