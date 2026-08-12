"""Device photos: a local cache, and an opt-in per-module fetch.

**No image is ever shipped with this project.** Vendor product photography is the manufacturer's
copyright, and their trademark position generally permits only nominative use. A photo therefore
comes from one of exactly two places:

1. a file the user points at, or
2. an opt-in download that a device module performs from the vendor's own advertised endpoint.

Either way it lands in the user's cache, never in the repository. That is the same rule the Jabra
project arrived at, and the reasoning has not changed.

Two constraints for any module implementing :meth:`~hardware_ui.core.device.Device.fetch_photo`:

**Follow advertised links; never guess a CDN pattern.** The Jabra work reverse-engineered
``CloudManager::FetchDeviceConfigurationFromCloud`` and discovered the device configuration
service *tells* you the asset URLs. Guessing a URL shape is both fragile and a much weaker
position than requesting what the vendor's own client requests.

**Absence is a normal answer.** On an Evolve2 85 the configuration endpoint answers 200 with an
empty asset list and every image URL 404s. There is simply no photo for that hardware, and a
module must return ``None`` rather than caching an error page as a picture.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from .paths import cache_dir, ensure

log = logging.getLogger(__name__)

#: Refuse anything implausibly large for a product photo.
MAX_BYTES = 8 * 1024 * 1024

#: Magic bytes -> file suffix. Used to verify a download really is an image and to pick the
#: extension, rather than trusting a Content-Type header or a URL.
_MAGIC: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",  # verified further below
    b"<svg": ".svg",
    b"<?xml": ".svg",
}


def _dir() -> Path:
    return ensure(cache_dir() / "photos")


def _key(uid: str) -> str:
    """Filename stem for a device.

    Hashed because a uid contains colons and slashes (``bt:AC:80:…``, a sysfs path), and a
    predictable stem is not worth the escaping rules.
    """
    return hashlib.sha256(uid.encode()).hexdigest()[:16]


def sniff(payload: bytes) -> str | None:
    """The file suffix for *payload*, or None if it is not an image we recognise.

    Checked by content, not by extension or Content-Type: the failure this prevents is caching a
    404 HTML page as a device photo, which then looks like a broken image forever.
    """
    for magic, suffix in _MAGIC.items():
        if payload.startswith(magic):
            if suffix == ".webp" and payload[8:12] != b"WEBP":
                continue
            return suffix
    return None


def cached(uid: str) -> Path | None:
    """An existing cached photo for this device, or None."""
    stem = _key(uid)
    for path in sorted(_dir().glob(f"{stem}.*")):
        if path.is_file() and path.stat().st_size:
            return path
    return None


def store_bytes(uid: str, payload: bytes) -> Path | None:
    """Cache *payload* as this device's photo. None if it is not a usable image."""
    if not payload or len(payload) > MAX_BYTES:
        log.debug("photo for %s rejected: %d bytes", uid, len(payload))
        return None
    suffix = sniff(payload)
    if suffix is None:
        log.debug("photo for %s rejected: not a recognised image", uid)
        return None

    remove(uid)
    target = _dir() / f"{_key(uid)}{suffix}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    except OSError:
        log.debug("could not write photo for %s", uid, exc_info=True)
        return None
    log.info("cached %d byte photo for %s", len(payload), uid)
    return target


def store_file(uid: str, source: Path) -> Path | None:
    """Cache a file the user chose. None if it is not a usable image."""
    try:
        payload = source.read_bytes()[: MAX_BYTES + 1]
    except OSError:
        log.debug("could not read %s", source, exc_info=True)
        return None
    return store_bytes(uid, payload)


def remove(uid: str) -> None:
    """Forget this device's photo."""
    for path in _dir().glob(f"{_key(uid)}.*"):
        try:
            path.unlink()
        except OSError:
            log.debug("could not remove %s", path, exc_info=True)


def clear() -> int:
    """Drop every cached photo. Returns how many were removed."""
    removed = 0
    for path in _dir().iterdir():
        if path.is_file():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def copy_into(uid: str, destination: Path) -> bool:
    """Export a cached photo, for a bug report or a backup."""
    source = cached(uid)
    if source is None:
        return False
    try:
        shutil.copy2(source, destination)
    except OSError:
        return False
    return True
