"""Graphic EQ preset store.

Two sources, merged at load time:

  * a small **built-in** table (`catalogue.EQ_PRESETS`) recovered from the USB
    capture and cross-checked against the Android app — enough to be useful with
    no setup at all;
  * an optional **imported** table derived from your own Creative App install by
    `tools/import_vendor_data.py`, which adds the full factory set.

**What ships and what does not**, because the distinction is the whole reason this
file is split in two.

The eleven genre presets in `catalogue.EQ_PRESETS` *do* ship. They were recovered
from a USB capture of a real card -- observed on the wire, not copied out of a
vendor data file -- their names are generic words for kinds of music, and their
contents are ten gain figures each: functional settings, not expression.

Creative's own preset *files* never ship, and neither does their artwork. The
importer writes both into your XDG data directory, where they stay yours; the
repository's `.gitignore` and `tools/publish.sh` both refuse `presets.json` and
`device.png` so a stray copy cannot be committed or published by accident.

The per-game presets are a separate matter again: the installer contains no gains
for them at all, because the Creative App fetches them at runtime. There is
nothing to ship even if one wanted to.

Presets carry **two curves**: the vendor tunes Speaker and Headphone separately
and 43 of the 71 factory presets genuinely differ between the two. The active
curve follows the device's current output routing.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from dataclasses import dataclass

from .ids import EQ_BAND_COUNT, OutputTarget

log = logging.getLogger(__name__)

#: Centre frequencies of the ten bands, in Hz, in wire order (band 0 first).
#: Confirmed against the vendor preset files, which state them explicitly.
BAND_FREQUENCIES: tuple[int, ...] = (31, 62, 125, 250, 500, 1000, 2000, 4000,
                                     8000, 16000)

#: Filename the importer writes and this module reads.
CACHE_NAME = "presets.json"

#: Product photo, copied from the user's own Creative install by the importer.
#: Creative's artwork is theirs, so it is never committed to this repository —
#: the GUI shows it only if the user has imported one.
IMAGE_NAME = "device.png"

#: Format version of that file. Bumped if the schema changes incompatibly.
CACHE_VERSION = 1


def data_dir() -> pathlib.Path:
    """Where imported vendor data lives.

    **Redirected in the port.** The source project owns its own XDG directory; here every module's
    vendor assets live under one root so that ``vendor_dir()`` is the single answer to "where did
    that come from" -- the same redirection the Logitech module applies to Solaar's config. The
    directory is not created on read: a device with no imported presets is the normal case.
    """
    from hardware_ui.core import paths
    return paths.vendor_dir("creative_peripherals")


def cache_path() -> pathlib.Path:
    """Where imported preset data lives."""
    return data_dir() / CACHE_NAME


def image_path() -> pathlib.Path | None:
    """The imported product photo, or None if the user has not imported one.

    `$SBX4_DEVICE_IMAGE` overrides it, so anyone can point the app at their own
    picture without importing anything from Creative at all.
    """
    override = os.environ.get("SBX4_DEVICE_IMAGE", "").strip()
    if override:
        p = pathlib.Path(override).expanduser()
        return p if p.is_file() else None
    p = data_dir() / IMAGE_NAME
    return p if p.is_file() else None


@dataclass(frozen=True)
class EqPreset:
    """One preset: a headphone curve, a speaker curve, and where it belongs."""

    name: str
    headphone: tuple[float, ...]
    speaker: tuple[float, ...]
    headphone_preamp: float = 0.0
    speaker_preamp: float = 0.0
    #: Device profile slot (0-3) if the vendor assigns this preset to one.
    slot: int | None = None
    #: Vendor sort order; used to keep the list in the app's own sequence.
    order: int = 0
    #: False for presets that came from an import rather than the built-in table.
    builtin: bool = True

    def curve(self, output: int | None) -> tuple[tuple[float, ...], float]:
        """Return `(bands, preamp)` for the given OutputTarget mask.

        Anything that is not the headphone jack — line out, power amp, unknown —
        uses the speaker curve, which is what the Windows app does.
        """
        if output is not None and output & int(OutputTarget.HEADPHONES):
            return self.headphone, self.headphone_preamp
        return self.speaker, self.speaker_preamp

    def differs_by_output(self) -> bool:
        return self.headphone != self.speaker


def _coerce(bands) -> tuple[float, ...]:
    vals = tuple(float(b) for b in bands)
    if len(vals) != EQ_BAND_COUNT:
        raise ValueError(f"expected {EQ_BAND_COUNT} bands, got {len(vals)}")
    return vals


def _builtin() -> dict[str, EqPreset]:
    """The capture-derived table, as EqPreset objects.

    Those gains were recovered while the device was routed to headphones, and
    every one of them matches the vendor's headphone curve exactly. We therefore
    use them for both outputs rather than inventing a speaker variant.
    """
    from . import catalogue

    out = {}
    for i, (name, (bands, preamp)) in enumerate(catalogue.EQ_PRESETS.items()):
        curve = _coerce(bands)
        out[name] = EqPreset(name=name, headphone=curve, speaker=curve,
                             headphone_preamp=preamp, speaker_preamp=preamp,
                             order=i, builtin=True)
    return out


def _from_cache(path: pathlib.Path) -> dict[str, EqPreset]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable preset cache %s: %s", path, exc)
        return {}

    version = raw.get("version")
    if version != CACHE_VERSION:
        log.warning("preset cache %s has version %r, expected %d — ignoring; "
                    "re-run tools/import_vendor_data.py", path, version,
                    CACHE_VERSION)
        return {}

    out: dict[str, EqPreset] = {}
    for name, spec in (raw.get("eq") or {}).items():
        try:
            hp = _coerce(spec["headphone"])
            spk = _coerce(spec["speaker"])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping malformed preset %r: %s", name, exc)
            continue
        out[name] = EqPreset(
            name=name, headphone=hp, speaker=spk,
            headphone_preamp=float(spec.get("headphone_preamp", 0.0)),
            speaker_preamp=float(spec.get("speaker_preamp", 0.0)),
            slot=spec.get("slot"), order=int(spec.get("order", 0)),
            builtin=False)
    return out


def _dedupe_key(name: str) -> str:
    """Fold spelling differences so the same preset does not appear twice.

    Our capture-derived names and the vendor's display names disagree on
    punctuation and spacing — "Hip Hop" vs "HipHop", "R&B" vs "R & B" — which
    otherwise leaves near-identical pairs in the list.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def load(path: pathlib.Path | None = None) -> dict[str, EqPreset]:
    """All known presets, imported data taking precedence over the built-ins.

    Ordering is the vendor's own (`order`), then name, so the list reads the
    same way it does in the Windows app.
    """
    presets = _builtin()
    imported = _from_cache(path or cache_path())

    # An imported preset replaces the built-in it duplicates, even when the two
    # spell the name differently: the vendor's curve and title are the better
    # source once someone has imported them.
    if imported:
        taken = {_dedupe_key(n) for n in imported}
        presets = {n: p for n, p in presets.items()
                   if _dedupe_key(n) not in taken}
    presets.update(imported)
    return dict(sorted(presets.items(), key=lambda kv: (kv[1].order, kv[0])))


def slots(presets: dict[str, EqPreset]) -> dict[int, str]:
    """Device profile slot -> preset name, for presets the vendor pins to a slot.

    The factory data assigns exactly four: Music, Movie, FootstepEnhancer, Flat.
    """
    return {p.slot: name for name, p in presets.items() if p.slot is not None}
