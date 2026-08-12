"""Per-device setting catalogues shipped by the Poly Lens app.

Lens bundles one JSON catalogue per device at `assets/<usb-pid-hex>.json`. Each setting declares
its `get` / `getResponse` / `set` / `event` deckard ids outright, plus named values with typed
payloads — vendor ground truth, so payload encodings need not be inferred.

Two caveats, both established from the V4310:

* The catalogue is the *UI-exposed* subset, not the device's full capability list (26 entries
  versus 33 ids the hardware answered). Use a live probe to decide what is supported and the
  catalogue for labels, value names and types.
* Some entries are write-only actions with no `get` — `clearPairedDevices`, `restoreDefaults`.
  Never issue these during a capability sweep.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from hardware_ui.core.paths import vendor_dir

#: Poly's per-device catalogues, unpacked from the user's own copy of Poly Studio. Never
#: shipped: they are Poly's. See docs/POLY_UI_BEHAVIOUR.md §11.
CATALOGUE_DIR = vendor_dir("poly_headsets") / "catalogues"

#: Wire width in bytes per declared payload type, big-endian.
#: BOOLEAN/BYTE/UNSIGNED_SHORT confirmed against live captures; UNSIGNED_INT is pinned by value
#: range (catalogues contain values up to 0x40000000, which cannot fit in 2 bytes).
#: The SDK's C struct field types are NOT reliable for width — use these.
TYPE_WIDTH = {
    "BOOLEAN": 1,
    "BYTE": 1,
    "UNSIGNED_SHORT": 2,
    "UNSIGNED_INT": 4,
}
#: Variable-length types, encoded as a u16 big-endian byte count followed by the bytes.
VARIABLE_TYPES = frozenset({"BYTE_ARRAY", "SHORT_ARRAY", "PAYLOAD"})


class CatalogueError(ValueError):
    pass


def encode_value(fields: list[dict]) -> bytes:
    """Encode a catalogue `payload` list into wire bytes."""
    out = bytearray()
    for f in fields:
        t = f["type"]
        width = TYPE_WIDTH.get(t)
        if width is None:
            raise CatalogueError(f"cannot encode variable-length type {t}")
        raw = f.get("value")
        if isinstance(raw, bool):
            n = int(raw)
        elif raw is None:
            raise CatalogueError(f"missing value for {t} field")
        else:
            n = int(str(raw), 0)
        out += n.to_bytes(width, "big")
    return bytes(out)


@dataclass(frozen=True)
class Choice:
    """One named, selectable value of a setting."""

    name: str
    fields: list[dict]

    @property
    def payload(self) -> bytes:
        return encode_value(self.fields)


@dataclass(frozen=True)
class Setting:
    name: str
    description: str
    get_id: int | None
    set_id: int | None
    event_id: int | None
    choices: tuple[Choice, ...]

    @property
    def is_action(self) -> bool:
        """Write-only entries (restoreDefaults, clearPairedDevices) — never probe these."""
        return self.get_id is None and self.set_id is not None

    @property
    def read_write_ids_differ(self) -> bool:
        return (
            self.get_id is not None and self.set_id is not None and self.get_id != self.set_id
        )

    def choice(self, name: str) -> Choice | None:
        for c in self.choices:
            if c.name == name:
                return c
        return None

    def decode(self, payload: bytes) -> str | None:
        """Name of the choice matching `payload`, if any."""
        for c in self.choices:
            try:
                if c.payload == payload:
                    return c.name
            except CatalogueError:
                continue
        return None


@dataclass(frozen=True)
class Catalogue:
    pid: int
    settings: tuple[Setting, ...]

    def by_name(self, name: str) -> Setting | None:
        for s in self.settings:
            if s.name == name:
                return s
        return None

    def by_get_id(self, get_id: int) -> Setting | None:
        for s in self.settings:
            if s.get_id == get_id:
                return s
        return None


def _block_id(setting: dict, block: str) -> int | None:
    did = (setting.get(block) or {}).get("deckardId")
    return int(did, 16) if did else None


def _parse(raw: dict) -> Catalogue:
    settings = []
    for s in raw.get("settings", []):
        # Prefer the set block's choices (what we write); fall back to getResponse.
        values = (s.get("set") or {}).get("possibleValues") or (
            s.get("getResponse") or {}
        ).get("possibleValues") or []
        settings.append(
            Setting(
                name=s.get("settingName") or "",
                description=s.get("description", ""),
                get_id=_block_id(s, "get"),
                set_id=_block_id(s, "set"),
                event_id=_block_id(s, "event"),
                choices=tuple(
                    Choice(name=v["name"], fields=list(v.get("payload") or []))
                    for v in values
                    if v.get("name") is not None
                ),
            )
        )
    return Catalogue(pid=int(str(raw.get("pid", "0x0")), 16), settings=tuple(settings))


@lru_cache(maxsize=1)
def _index() -> dict[int, Path]:
    """Map USB PID -> catalogue file.

    Most files are named after the PID in hex, but some carry a vendor prefix (`hp714a.json`,
    `p9212.json`), so the `pid` field inside the file is the authority.
    """
    out: dict[int, Path] = {}
    for path in sorted(CATALOGUE_DIR.glob("*.json")):
        try:
            pid = int(str(json.loads(path.read_text()).get("pid", "")), 16)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        out.setdefault(pid, path)
    return out


@lru_cache(maxsize=None)
def load(pid: int) -> Catalogue | None:
    """Load the catalogue for a USB PID (as reported by the USB_PID setting, 0x0A02)."""
    # Fast path: the common case is a plain hex filename.
    path = CATALOGUE_DIR / f"{pid:x}.json"
    if not path.exists():
        path = _index().get(pid)
    if path is None or not path.exists():
        return None
    return _parse(json.loads(path.read_text()))


def available_pids() -> list[int]:
    return sorted(_index())
