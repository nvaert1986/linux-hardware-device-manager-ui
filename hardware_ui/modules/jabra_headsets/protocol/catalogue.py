"""The Jabra property catalogue.

Reads GN Audio's own definition of 423 device properties. The file is **not** redistributed with
this project — ``assets.py`` fetches it from GN Audio's own npm publication once the user
consents, and it lands in the module's vendor directory with its provenance beside it.

Each property declares up to four pipelines:

    read       steps ending in a value, driven by a gnpRead
    write      steps that encode a value, ending in a gnpWrite
    event      steps that decode an unsolicited gnpEvent
    subscribe  steps that arm the event (a read-modify-write on a bitmask register)

This module only *describes*; `process.py` executes. Keeping them apart means the catalogue can
be inspected, filtered and unit-tested without a device attached.
"""
from __future__ import annotations

import functools
import json
import pathlib
from dataclasses import dataclass
from typing import Any

#: Step types that actually talk to the device.
GNP_STEPS = frozenset({"gnpRead", "gnpWrite", "gnpEvent"})


@dataclass(frozen=True)
class ValueType:
    """The declared shape of a property's value — what the UI needs to pick a widget."""

    kind: str                       # boolean | integer | string | object | array | unknown
    enum: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None

    @property
    def is_enum(self) -> bool:
        return bool(self.enum)

    @property
    def is_ranged(self) -> bool:
        return self.kind == "integer" and None not in (self.minimum, self.maximum)

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> "ValueType":
        raw = raw or {}
        return cls(
            kind=raw.get("type", "unknown"),
            enum=tuple(raw.get("enum", ())),
            minimum=raw.get("minimum"),
            maximum=raw.get("maximum"),
        )


@dataclass(frozen=True)
class Property:
    name: str
    value_type: ValueType
    read: tuple[dict[str, Any], ...] = ()
    write: tuple[dict[str, Any], ...] = ()
    event: tuple[dict[str, Any], ...] = ()
    subscribe: tuple[dict[str, Any], ...] = ()
    autogen_name: str | None = None

    # -- capability shape ------------------------------------------------------------------

    @property
    def readable(self) -> bool:
        return bool(self.read)

    @property
    def writable(self) -> bool:
        return bool(self.write)

    @property
    def has_event(self) -> bool:
        return bool(self.event)

    @property
    def needs_subscription(self) -> bool:
        return bool(self.subscribe)

    @property
    def verifiable(self) -> bool:
        """Can a write be confirmed at all? 13 properties are writable but not readable."""
        return self.readable or self.has_event

    # -- addressing ------------------------------------------------------------------------

    def _first_gnp(self, steps: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
        return next((s for s in steps if s.get("typeName") in GNP_STEPS), None)

    @property
    def address(self) -> int | None:
        """Explicit target address, or None meaning the session's primary endpoint."""
        for steps in (self.read, self.write, self.event):
            step = self._first_gnp(steps)
            if step is not None:
                return step.get("address")
        return None

    def endpoint(self, kind: str = "read") -> tuple[int, int] | None:
        """(command, subcommand) for the named pipeline."""
        step = self._first_gnp(getattr(self, kind))
        if step is None:
            return None
        return step["command"], step["subcommand"]

    # -- introspection ---------------------------------------------------------------------

    @property
    def step_types(self) -> frozenset[str]:
        return frozenset(
            s.get("typeName", "")
            for steps in (self.read, self.write, self.event, self.subscribe)
            for s in steps
        )

    @property
    def is_bitmasked(self) -> bool:
        """True when the value shares a register with others, so writes must read first."""
        return "bitmaskInsert" in self.step_types

    def __str__(self) -> str:
        ops = "".join(c for c, on in
                      (("r", self.readable), ("w", self.writable),
                       ("e", self.has_event), ("s", self.needs_subscription)) if on)
        where = self.endpoint("read") or self.endpoint("write") or self.endpoint("event")
        loc = f"0x{where[0]:02X}/0x{where[1]:02X}" if where else "-"
        return f"{self.name} [{ops}] {loc} {self.value_type.kind}"


class Catalogue:
    """All properties, indexed by name."""

    def __init__(self, properties: dict[str, Property]):
        self._properties = properties

    def __len__(self) -> int:
        return len(self._properties)

    def __iter__(self):
        return iter(self._properties.values())

    def __contains__(self, name: object) -> bool:
        return name in self._properties

    def __getitem__(self, name: str) -> Property:
        try:
            return self._properties[name]
        except KeyError:
            raise KeyError(f"no property named {name!r}") from None

    def get(self, name: str) -> Property | None:
        return self._properties.get(name)

    def names(self) -> list[str]:
        return sorted(self._properties)

    def matching(self, *, readable: bool | None = None, writable: bool | None = None,
                 has_event: bool | None = None, address: int | None = ...,
                 command: int | None = None) -> list[Property]:
        """Filter helper. `address=...` means "don't care"; `address=None` means primary."""
        result = []
        for prop in self._properties.values():
            if readable is not None and prop.readable != readable:
                continue
            if writable is not None and prop.writable != writable:
                continue
            if has_event is not None and prop.has_event != has_event:
                continue
            if address is not ... and prop.address != address:
                continue
            if command is not None:
                where = prop.endpoint("read") or prop.endpoint("write")
                if where is None or where[0] != command:
                    continue
            result.append(prop)
        return result

    def by_subcommand(self, command: int, subcommand: int) -> list[Property]:
        """Properties served by a given (command, subcommand).

        Several names can share one subcommand — bitmask-packed settings do, and so do
        `powerFrequency2` and `cameraVideoFlicker`. `configChangeEvents` reports subcommands,
        not property names, so this is how its notifications get resolved.
        """
        return [
            p for p in self._properties.values()
            if any(p.endpoint(kind) == (command, subcommand)
                   for kind in ("read", "write", "event"))
        ]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Catalogue":
        properties: dict[str, Property] = {}
        for name, body in raw.items():
            event = body.get("event") or {}
            properties[name] = Property(
                name=name,
                value_type=ValueType.from_json(body.get("valueType")),
                read=tuple(body.get("read") or ()),
                write=tuple(body.get("write") or ()),
                event=tuple(event.get("process") or ()),
                subscribe=tuple(event.get("subscribe") or ()),
                autogen_name=body.get("autogenName"),
            )
        return cls(properties)


class CatalogueMissing(RuntimeError):
    """The property catalogue has not been obtained yet — see catalogue_source."""


@functools.lru_cache(maxsize=1)
def load(path: pathlib.Path | None = None) -> Catalogue:
    """The property catalogue. Cached — it is read-only and ~460 KB of JSON.

    The file is **not** shipped with this project; ``assets.py`` puts it in the module's vendor
    directory once the user consents to the download. Raising a named error rather than letting
    ``open()`` fail is what lets the shell offer to fetch it instead of showing a traceback.
    """
    if path is None:
        from .. import assets

        path = assets.find()
        if path is None:
            raise CatalogueMissing(
                "the Jabra property catalogue is not present; "
                "it can be downloaded from GN Audio's npm publication"
            )
    with open(path, encoding="utf-8") as fh:
        return Catalogue.from_json(json.load(fh))
