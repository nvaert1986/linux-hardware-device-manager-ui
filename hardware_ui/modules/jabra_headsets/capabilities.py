"""Jabra's property catalogue, expressed in the shell's schema.

There is no per-model code here and there must never be any. GN Audio publishes one catalogue
describing 423 properties across their whole range, each declaring its own command, subcommand and
byte converters, so a headset the author has never seen produces a correct page from the
catalogue plus a live probe — the same bet the Poly module makes on its per-product catalogues and
the Dell module on the capability string.

Three rules shape what appears, each learned the hard way in the source project:

**The catalogue is the whole range, not this device.** A Link 390 answers 69 of the 283 readable
properties; the rest NACK or say nothing. Support comes from the probe, and an unsupported
property produces no control rather than a dead one.

**Destructive properties are never controls.** ``factoryReset``, ``firmwareUpdate`` and the DFU
entry points are writable like anything else, and a generic setter would happily fire them from a
stray click. ``labels.is_dangerous`` withholds them, and this module offers no route back —
unlike the Poly module's maintenance tab, these are not confirmable actions but device-bricking
operations behind a protocol nobody here has tested.

**Both endpoints, kept apart.** A Jabra link fronts several GNP devices — on a Link 390 the dongle
answers at 0x01 and the headset at 0x04 — and they answer *differently*: ``ancMode`` is readable at
the headset and "unknown sub-command" at the dongle, while ``radioPower`` is the dongle's own. Each
endpoint is therefore probed separately and gets its own section, with its own keyspace, because
the property names collide. The Poly module showed what happens when a page is vague about which
box a setting lands in; the section heading names the device.

**Language properties are not what the catalogue says they are.** ``currentLanguage``,
``currentLanguageCode`` and ``currentLanguageInConfigMode`` all decode to a raw Microsoft LCID
(1033 = en-US), even though the catalogue declares an 18-entry string enum for the second. Trusting
that enum shows the wrong entry; trusting the raw value shows "1033". Both are handled here: the
choices come from the device's own ``availableLanguages``, named through ``labels.LCID_NAMES``.

**An object-valued property becomes one row per field.** ``supportedEvents`` and
``versionExtended`` decode to dictionaries, and rendering one as a single readout puts its whole
``repr`` on one line -- which on real hardware stretched the window past the edge of the screen and
onto the next monitor. There is no widget that can edit a dict honestly, but there is an obvious
way to *show* one: a row per key, top to bottom, which is what the rest of the page already is.

**A value type decides the widget, not a name.** The catalogue declares ``boolean`` / ``integer``
with bounds / ``enum`` / ``string``, which maps cleanly onto TOGGLE / RANGE / CHOICE / TEXT. What
it cannot declare is that 13 properties are writable but not readable, so a write to those cannot
be confirmed; those carry a note saying so rather than implying verification that did not happen.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from hardware_ui.core import Capability, CapabilitySet, Choice, Kind

from . import categories, labels
from .protocol.catalogue import Catalogue, Property

log = logging.getLogger(__name__)

SETTING_PREFIX = "setting."
INFO_PREFIX = "info."
RELAY_PREFIX = "relay."
EQ_PREFIX = "eq.band"
EQ_FLAT_KEY = "action.eq.flat"

GROUP_INFO = "Info"
GROUP_EQ = "Equalizer"

BATTERY_KEY = "info.battery"
STATE_PREFIX = "state."

#: Live state the device pushes rather than answers: which earcup is on your head, where the boom
#: arm is, whether the microphone is muted, and whether a call is in progress. None of these are
#: settings, and none can be read on demand -- they are event-only, so they appear as readouts fed
#: by the change stream. Captions are the source app's ``StatePanel.CAPTIONS``.
STATE_ROWS: tuple[tuple[str, str], ...] = (
    ("onHeadDetectionStatus", "On your head"),
    ("boomArmPosition", "Boom arm"),
    ("microphoneMuteState", "Microphone"),
    ("_mode", "Mode"),
)

#: Shown until the first event arrives. Some models never send some of these at all, and a
#: permanent "waiting" reads as a hung load -- the source app says this outright after a grace
#: period rather than leaving the row blank.
STATE_UNREPORTED = "Not reported by this device"

#: dB either side of flat, in half-decibel steps -- the range the vendor's own UI offers.
#: A GNP payload is 58 bytes; the subcommand and the string's length prefix take one each, so a
#: longer string cannot reach the device however willing the widget is.
MAX_STRING_CHARS = 56

EQ_MIN_DB = -6.0
EQ_MAX_DB = 6.0
EQ_STEP_DB = 0.5

#: Worth stating once, on the first Info row, when the link fronts more than one GNP device.
NOTE_ENDPOINT = (
    "These settings belong to the headset. The adapter it connects through is a separate device "
    "on the same link, with its own section."
)

NOTE_RELAY = (
    "The adapter's own settings, read from the adapter rather than the headset. Property names "
    "overlap but the answers do not: the adapter refuses most headset settings and has some of "
    "its own, such as its radio power."
)

#: One drag is one write: the whole band table goes out in a single message, so every band shares
#: a ``writes_with`` group and the shell coalesces them.
NOTE_EQ = (
    "Bands are written together as one table. \"Flat\" sets every band to 0 dB, which is what the "
    "vendor's Restore does."
)

#: Writable but not readable — 13 properties in the catalogue. The write is acked and there is
#: nothing to read back, so a successful write means "the device did not refuse it", no more.
NOTE_UNVERIFIABLE = "This setting cannot be read back, so a change can only be reported as sent."

#: Identity rows, in display order, with the property that supplies each.
IDENTITY_ROWS: tuple[tuple[str, str], ...] = (
    ("name", "Model"),
    ("serialNumber", "Serial number"),
    ("firmwareVersion", "Firmware"),
    ("pid", "Product id"),
    ("skuNumber", "SKU"),
    ("variant", "Variant"),
)


def setting_key(name: str) -> str:
    return f"{SETTING_PREFIX}{name}"


def relay_key(name: str) -> str:
    """The adapter's properties share names with the headset's, so they need their own keyspace."""
    return f"{RELAY_PREFIX}{name}"


def property_name(key: str) -> str:
    """The catalogue property behind a capability key, at either endpoint."""
    for prefix in (RELAY_PREFIX, SETTING_PREFIX):
        if key.startswith(prefix):
            return key[len(prefix) :]
    return ""


def is_relay_key(key: str) -> bool:
    return key.startswith(RELAY_PREFIX)


def state_key(name: str) -> str:
    return f"{STATE_PREFIX}{name}"


def band_index(key: str) -> int | None:
    """The equalizer band a key addresses, or ``None`` if it is not a band."""
    if not key.startswith(EQ_PREFIX):
        return None
    try:
        return int(key[len(EQ_PREFIX) :])
    except ValueError:
        return None


def build(
    catalogue: Catalogue | None,
    *,
    supported: Sequence[str] = (),
    identity: Mapping[str, str] | None = None,
    values: Mapping[str, Any] | None = None,
    peers: Sequence[str] = (),
    relay: tuple[Sequence[str], str] = ((), ""),
    bands: Sequence[str] = (),
    has_battery: bool = False,
    states: Sequence[str] = (),
) -> CapabilitySet:
    """The page for one Jabra link.

    *supported* is what the headset answered during its probe; *relay* is the same for the adapter,
    with the adapter's name for the section heading. *bands* are the equalizer band labels the
    device reported, empty when it has no equalizer.
    """
    seen = dict(values or {})
    out: list[Capability] = list(_info(identity or {}, peers))
    if has_battery:
        out.append(_battery())
    out.extend(_states(states))
    if catalogue is not None:
        out.extend(_settings(catalogue, supported, setting_key, "", seen))
        out.extend(_equalizer(bands))
        out.extend(_relay(catalogue, relay[0], relay[1], seen))
    return CapabilitySet(out)


def _settings(
    catalogue: Catalogue,
    supported: Sequence[str],
    key_for,
    group_override: str,
    values: Mapping[str, Any] | None = None,
) -> list[Capability]:
    """Every supported, non-destructive property, bucketed into sections."""
    live = [n for n in supported if n in catalogue]
    grouped = categories.group(live)
    if grouped.withheld:
        log.info("withholding %d destructive properties: %s",
                 len(grouped.withheld), ", ".join(grouped.withheld))

    seen = values or {}
    languages = seen.get("availableLanguages")
    out: list[Capability] = []
    for section in grouped.categories():
        for name in grouped.by_category[section]:
            out.extend(
                _capabilities_for(
                    catalogue[name], key_for(name), group_override or section, seen.get(name),
                    languages if isinstance(languages, list) else None,
                )
            )
    return out


def _relay(catalogue: Catalogue, supported: Sequence[str], name: str,
           values: Mapping[str, Any] | None = None) -> list[Capability]:
    """The adapter's own settings, in one section.

    Not split by category: an adapter answers a handful of properties, and nine near-empty
    sections would read worse than one honest list.
    """
    if not supported:
        return []
    group = f"Adapter — {name}" if name else "Adapter"
    rows = _settings(catalogue, supported, relay_key, group, values)
    if rows:
        rows[0] = _with_note(rows[0], NOTE_RELAY)
    return rows


def _equalizer(bands: Sequence[str]) -> list[Capability]:
    """One slider per band, plus Flat.

    Every band carries the whole group in ``writes_with``: the device takes the entire table in one
    message, and the opaque per-band ``A`` field has to be written back unchanged, so a single-band
    write does not exist at the protocol level.
    """
    if not bands:
        return []
    group_keys = tuple(f"{EQ_PREFIX}{index}" for index in range(len(bands)))
    rows = [
        Capability(
            key=group_keys[index],
            kind=Kind.RANGE,
            label=label,
            group=GROUP_EQ,
            writes_with=group_keys,
            minimum=EQ_MIN_DB,
            maximum=EQ_MAX_DB,
            step=EQ_STEP_DB,
            unit="dB",
        )
        for index, label in enumerate(bands)
    ]
    rows[0] = _with_note(rows[0], NOTE_EQ)
    rows.append(
        Capability(
            key=EQ_FLAT_KEY,
            kind=Kind.ACTION,
            label="Flat",
            action_label="Flat",
            group=GROUP_EQ,
            description="Set every band to 0 dB, which is what the vendor's Restore does.",
        )
    )
    return rows


def field_key(key: str, field: str) -> str:
    """The key for one field of an object-valued property."""
    return f"{key}.{field}"


def _capabilities_for(
    prop: Property, key: str, group: str, value: Any = None,
    languages: Sequence[int] | None = None,
) -> list[Capability]:
    """The rows one property contributes -- usually one, but a dict contributes one per field."""
    if prop.name in labels.LANGUAGE_PROPERTIES:
        return [_language(prop, key, group, languages)]
    if _kind(prop) is Kind.READOUT and isinstance(value, dict) and value:
        return [
            Capability(
                key=field_key(key, field),
                kind=Kind.READOUT,
                label=f"{labels.label(prop.name)} — {categories.label_for(str(field))}",
                group=group,
                writable=False,
            )
            for field in value
        ]
    built = _capability(prop, key, group)
    return [built] if built is not None else []


def _language(prop: Property, key: str, group: str,
              languages: Sequence[int] | None) -> Capability:
    """A language property as a named dropdown.

    The value space is the device's own ``availableLanguages`` -- an Evolve2 85 reports exactly
    ``[1033]`` -- not the catalogue's enum, which for ``currentLanguageCode`` lists 18 string
    identifiers that the property never returns.
    """
    return Capability(
        key=key,
        kind=Kind.CHOICE,
        label=labels.label(prop.name),
        group=group,
        writable=prop.writable,
        description=labels.description(prop.name),
        choices=tuple(
            Choice(code, text) for text, code in labels.language_choices(list(languages or ()))
        ),
    )


def _capability(prop: Property, key: str, group: str) -> Capability | None:
    """One property as a control, or ``None`` when it cannot sensibly be one."""
    kind = _kind(prop)
    if kind is None:
        return None

    note = "" if prop.verifiable else NOTE_UNVERIFIABLE
    common = {
        "key": key,
        # Jabra's own wording, from their Android string pools, falling back to a generated label.
        "label": labels.label(prop.name),
        "group": group,
        "writable": prop.writable,
        "note": note,
        "description": labels.description(prop.name),
    }

    if kind is Kind.CHOICE:
        return Capability(
            **common,
            kind=kind,
            choices=tuple(
                Choice(value, labels.value_label(prop.name, str(value)))
                for value in prop.value_type.enum
            ),
        )
    if kind is Kind.RANGE:
        return Capability(
            **common,
            kind=kind,
            minimum=float(prop.value_type.minimum or 0),
            maximum=float(prop.value_type.maximum or 0),
            unit=labels.unit(prop.name).strip(),
        )
    if kind is Kind.TEXT:
        # The source puts the unit on every integer widget, bounded or not.
        return Capability(
            **common,
            kind=kind,
            # The source's UNITS carry a leading space because they are QSpinBox *suffixes*; this
            # shell spaces the unit itself, so keeping it gives "0  dB".
            unit=labels.unit(prop.name).strip(),
            max_length=MAX_STRING_CHARS if prop.value_type.kind == "string" else 0,
        )
    return Capability(**common, kind=kind)


def _kind(prop: Property) -> Kind | None:
    """The widget a property's declared value type calls for.

    A property that is neither readable nor writable is not a control at all — the catalogue
    contains event-only entries, which belong to the state panel rather than here.
    """
    value = prop.value_type
    if not prop.readable and not prop.writable:
        return None
    if not prop.writable:
        return Kind.READOUT
    if value.kind == "boolean":
        return Kind.TOGGLE
    if value.is_enum:
        return Kind.CHOICE
    if value.is_ranged:
        return Kind.RANGE
    if value.kind == "string":
        return Kind.TEXT
    if value.kind == "integer":
        # No declared bounds, so a slider would invent them. A free integer is safer as text.
        return Kind.TEXT
    # object / array / unknown: the interpreter can decode them but there is no widget that can
    # edit one honestly, so show what the device reports and refuse to pretend otherwise.
    return Kind.READOUT


def _info(identity: Mapping[str, str], peers: Sequence[str]) -> list[Capability]:
    rows: list[Capability] = []
    for name, label in IDENTITY_ROWS:
        value = identity.get(name)
        if not value:
            continue
        rows.append(
            Capability(
                key=f"{INFO_PREFIX}{name}",
                kind=Kind.READOUT,
                label=label,
                group=GROUP_INFO,
                writable=False,
                copyable=name in ("serialNumber", "firmwareVersion"),
            )
        )
    for index, peer in enumerate(peers):
        rows.append(
            Capability(
                key=f"{INFO_PREFIX}peer.{index}",
                kind=Kind.READOUT,
                label="Also on this link",
                group=GROUP_INFO,
                writable=False,
                description=peer,
            )
        )
    if rows and peers:
        rows[0] = _with_note(rows[0], NOTE_ENDPOINT)
    return rows


def _battery() -> Capability:
    """Charge, with charging and low flags folded into the reading.

    ``batteryLevelV2`` decodes as a jsonObject whose ``flags`` byte carries charging and low-battery
    bits beside the percentage, so the state comes free with the level -- no second read.
    """
    return Capability(
        key=BATTERY_KEY,
        kind=Kind.METER,
        label="Battery",
        group=GROUP_INFO,
        minimum=0,
        maximum=100,
        unit="%",
        writable=False,
    )


def _states(states: Sequence[str]) -> list[Capability]:
    """Event-only state, as readouts.

    Only the ones this device actually arms: an unarmed notification never fires, so a row for it
    would say "Not reported" forever. Which is also why the ones that *are* armed still start at
    that text -- the value arrives with the first event, not at connect.
    """
    live = set(states)
    return [
        Capability(
            key=state_key(name),
            kind=Kind.READOUT,
            label=caption,
            group=GROUP_INFO,
            writable=False,
        )
        for name, caption in STATE_ROWS
        if name in live
    ]


def _with_note(capability: Capability, note: str) -> Capability:
    import dataclasses

    return dataclasses.replace(capability, note=note)


__all__ = [
    "BATTERY_KEY",
    "EQ_FLAT_KEY",
    "EQ_PREFIX",
    "INFO_PREFIX",
    "RELAY_PREFIX",
    "SETTING_PREFIX",
    "band_index",
    "build",
    "field_key",
    "is_relay_key",
    "property_name",
    "relay_key",
    "setting_key",
    "state_key",
]
