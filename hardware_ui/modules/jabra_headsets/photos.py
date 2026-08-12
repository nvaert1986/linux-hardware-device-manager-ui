"""The vendor's own product photo, fetched on request.

**Nothing is shipped.** Vendor product photography is Jabra's copyright and their trademark
position only permits nominative use, so this project bundles no images. A photo either comes from
a file the user points at -- which the shell already handles -- or from this opt-in download, and
either way it lands in the user's cache, never in the repository.

**The URL is not a guessable CDN pattern.** It is advertised by Jabra's device-configuration
service, reverse-engineered from ``libjabra.dll``'s
``CloudManager::FetchDeviceConfigurationFromCloud``::

    GET devicecapabilities.jabra.com/v4/DeviceConfiguration
        ?pid=<dec>&type=<dec>&firmwareVersion=<x.y.z>
    -> {"links": [{"key": "Image1280", "value": "…/v4/product/<pid>/image?type=<type>"}, …]}

All three parameters are required -- omitting ``type`` or ``firmwareVersion`` returns a 400 naming
the missing field. Following the advertised links rather than assuming a pattern is both more
robust and a far better position to be in than guessing at someone's asset host.

**An empty answer is a normal answer.** On an Evolve2 85 (pid 9403, also tried type 9401 from
``dfuProductId``, and the Link 390's 11857) the configuration answers 200 with an empty asset list
and every image URL 404s. That is not an error and must never be cached as a picture -- the service
answers those 404s with ``application/problem+json``, which is exactly what would otherwise be
stored as a corrupt image.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

BASE = "https://devicecapabilities.jabra.com/v4"
CONFIG_URL = f"{BASE}/DeviceConfiguration"

#: Which advertised asset to prefer, best first.
IMAGE_KEYS = ("Image1280", "Image", "Thumbnail")

TIMEOUT = 20

#: Refuse anything implausibly large for a product photo.
MAX_BYTES = 8 * 1024 * 1024


def device_configuration(
    product_id: int, firmware: str, type_id: int | None = None
) -> dict | None:
    """The device configuration, or ``None`` if it cannot be had.

    *type_id* defaults to the product id; some products use a separate value -- an Evolve2 85
    reports ``dfuProductId`` 9401 against pid 9403 -- so callers may pass it explicitly.
    """
    query = urllib.parse.urlencode(
        {
            "pid": product_id,
            "type": type_id if type_id is not None else product_id,
            "firmwareVersion": firmware or "0.0.0",
        }
    )
    url = f"{CONFIG_URL}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310 - fixed https
            return json.loads(response.read(MAX_BYTES).decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        log.info("device configuration unavailable: %s", exc)
        return None


def image_urls(config: dict) -> list[str]:
    """Advertised image URLs, best resolution first."""
    links = {
        entry.get("key"): entry.get("value")
        for entry in config.get("links", [])
        if isinstance(entry, dict)
    }
    return [links[key] for key in IMAGE_KEYS if links.get(key)]


def download(
    product_id: int | None, firmware: str, type_id: int | None = None
) -> bytes | None:
    """The vendor image bytes, or ``None`` when none is published for this model."""
    if product_id is None:
        return None
    config = device_configuration(product_id, firmware, type_id)
    if config is None:
        return None
    for url in image_urls(config):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310
                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    # A 404 for a product with no asset arrives as application/problem+json.
                    log.info("%s is not an image (%s)", url, content_type)
                    continue
                payload = response.read(MAX_BYTES)
        except (urllib.error.URLError, OSError) as exc:
            log.info("image fetch failed (%s): %s", url, exc)
            continue
        log.info("fetched %d bytes from %s", len(payload), url)
        return payload
    log.info("Jabra publishes no image for product 0x%04X", product_id)
    return None


__all__ = ["BASE", "IMAGE_KEYS", "device_configuration", "download", "image_urls"]
