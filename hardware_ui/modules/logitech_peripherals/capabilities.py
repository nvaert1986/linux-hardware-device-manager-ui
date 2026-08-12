"""Solaar's settings, expressed in the shell's schema.

There is no per-device code here and there must never be any. ``logitech_receiver`` builds a
device's setting list by asking the hardware which HID++ features it implements, so a mouse nobody
here has seen produces a correct page from its own feature set -- the same bet every other module
in this project makes.

**A validator decides the widget.** Each setting carries a validator whose ``kind`` says what shape
its value is. Four of the eight map onto controls directly; the other four describe per-key maps
that need a table rather than a row, and are deliberately not offered yet rather than flattened
into something misleading:

======================  =====================  ==========================================
Solaar kind             Shown as               Note
======================  =====================  ==========================================
``TOGGLE``              ``Kind.TOGGLE``
``CHOICE``              ``Kind.CHOICE``        ``setting.choices``
``RANGE``               ``Kind.RANGE``         ``setting.range``
``PACKED_RANGE``        ``Kind.RANGE``         one value, packed on the wire
``MAP_CHOICE``          --                     per-key choice; key remapping
``MULTIPLE_TOGGLE``     --                     a switch per key
``MULTIPLE_RANGE``      --                     a range per key
``HETERO``              --                     mixed fields, no common shape
======================  =====================  ==========================================

**Nothing is read to build the page.** Solaar's settings are lazy: ``read()`` talks to the device.
Constructing controls from ``device.settings`` costs no I/O, so the page appears immediately and
values arrive after.

**A receiver is a device too.** It has firmware and pairing slots of its own, and pairing is the
one thing that cannot be done from the peripheral. It gets its own entry rather than a tab, because
a receiver hosting a mouse and a keyboard is not "the mouse's dongle".
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from hardware_ui.core import Capability, CapabilitySet, Choice, Kind

from . import labels

log = logging.getLogger(__name__)

SETTING_PREFIX = "setting."
INFO_PREFIX = "info."

#: Above this many keys a per-key map becomes a wall of dropdowns, and the page-length problem is
#: real rather than theoretical -- a 40-row tab has already pushed this window off the bottom of a
#: screen once. Measured on real hardware: an MX Master 3S maps 7 buttons and ``dpi_extended`` has
#: 3 keys, while a keyboard's ``divert-keys`` has 17 and ``per-key-lighting`` can reach 117. The
#: small ones expand; the large ones wait for a compact key-and-value control.
#:
#: The test is the *device's* map, never ``keys_universe`` -- that is the 327 control ids Logitech
#: defines in total, not the handful any one device has.
MAP_MAX_KEYS = 12

GROUP_INFO = "Info"
GROUP_PAIRING = "Pairing"

PAIR_KEY = "action.pair"
UNPAIR_PREFIX = "action.unpair."

#: Identity rows, in display order.
IDENTITY_ROWS: tuple[tuple[str, str], ...] = (
    ("name", "Model"),
    ("codename", "Short name"),
    ("kind", "Type"),
    ("serial", "Serial number"),
    ("unit_id", "Unit id"),
    ("firmware", "Firmware"),
    ("wpid", "Wireless id"),
    ("receiver", "Paired to"),
)

#: Battery is reported through a feature rather than a setting, so it gets its own row.
BATTERY_KEY = "info.battery"

NOTE_OFFLINE = (
    "This device is paired but not currently reachable. Settings are shown from the last known "
    "values and cannot be changed until it wakes up."
)

NOTE_UNPAIR = (
    "Unpairing frees the slot and the device stops working until it is paired again. If this is "
    "the keyboard or mouse you are using, make sure you have another way to control this machine."
)


def setting_key(name: str) -> str:
    return f"{SETTING_PREFIX}{name}"


def setting_name(key: str) -> str:
    return key[len(SETTING_PREFIX) :] if key.startswith(SETTING_PREFIX) else ""


def unpair_key(index: int) -> str:
    return f"{UNPAIR_PREFIX}{index}"


def unpair_index(key: str) -> int | None:
    if not key.startswith(UNPAIR_PREFIX):
        return None
    try:
        return int(key[len(UNPAIR_PREFIX) :])
    except ValueError:
        return None


def build(
    settings: Sequence[Any] = (),
    *,
    identity: Mapping[str, str] | None = None,
    online: bool = True,
    battery: bool = False,
    pairing: Mapping[str, Any] | None = None,
) -> CapabilitySet:
    """The page for one peripheral, or for a receiver.

    *settings* are ``logitech_receiver`` setting objects. *pairing* is present only for a receiver
    and carries its slot state.
    """
    out: list[Capability] = list(_info(identity or {}, online, battery))
    out.extend(_settings(settings))
    out.extend(_pairing(pairing or {}))
    return CapabilitySet(out)


def _settings(settings: Sequence[Any]) -> list[Capability]:
    grouped: dict[str, list[Capability]] = {}
    skipped: list[str] = []
    for setting in settings:
        built = _capabilities_for(setting)
        if not built:
            skipped.append(getattr(setting, "name", "?"))
            continue
        for one in built:
            grouped.setdefault(one.group, []).append(one)

    if skipped:
        log.info("%d settings need a per-key editor and are not shown: %s",
                 len(skipped), ", ".join(sorted(skipped)))

    out: list[Capability] = []
    for group in labels.GROUP_ORDER:
        out.extend(grouped.pop(group, []))
    for remaining in grouped.values():          # a group the order does not name yet
        out.extend(remaining)
    return out


def map_key(name: str, key: Any) -> str:
    """The key for one entry of a per-key map. The int is the wire value, so it is what is kept."""
    return f"{SETTING_PREFIX}{name}#{int(key)}"


def map_entry(key: str) -> tuple[str, int] | None:
    """``(setting name, key)`` for a per-key row, or ``None`` if *key* is not one."""
    if not key.startswith(SETTING_PREFIX) or "#" not in key:
        return None
    name, _, index = key[len(SETTING_PREFIX) :].partition("#")
    try:
        return name, int(index)
    except ValueError:
        return None


def _capabilities_for(setting: Any) -> list[Capability]:
    """The rows one setting contributes.

    Usually one. A per-key map contributes one **per key** -- the same shape the Jabra module uses
    to expand an object-valued property, and the only honest way to show "each button does this"
    without inventing a table widget.
    """
    kind_name = getattr(getattr(setting, "kind", None), "name", "")
    if kind_name == "MAP_CHOICE":
        return _per_key(setting)
    if kind_name == "MULTIPLE_TOGGLE":
        return _per_key_toggles(setting)
    built = _capability(setting)
    return [built] if built is not None else []


def _per_key(setting: Any) -> list[Capability]:
    """A MAP_CHOICE as one CHOICE row per key.

    Each key carries its *own* option list -- measured on an MX Master 3S, Left Button offers 2
    actions and Middle Button offers 7 -- so the rows genuinely differ and cannot share one control.
    """
    name = getattr(setting, "name", "")
    choices = getattr(setting, "choices", None)
    if not name or not choices:
        return []
    try:
        keys = list(choices.keys())
    except AttributeError:
        return []
    if not keys or len(keys) > MAP_MAX_KEYS:
        log.info("%s maps %d keys, too many to expand into rows", name, len(keys))
        return []

    group = labels.group_for(name)
    section = labels.label_for(setting)
    note = labels.map_note(name)
    # Some maps are readable and meaningful but not ours to write -- see READ_ONLY_MAPS. Offering
    # a dropdown where three of four values silently disable a physical button is worse than
    # offering nothing, and a note on the first row does not protect the four rows below it.
    writable = name not in labels.READ_ONLY_MAPS
    out: list[Capability] = []
    for index, key in enumerate(keys):
        options = tuple(
            Choice(_choice_value(option), labels.choice_label(option))
            for option in (choices[key] or ())
        )
        if not options:
            continue
        out.append(
            Capability(
                key=map_key(name, key),
                kind=Kind.CHOICE,
                label=labels.key_label(name, key),
                group=group,
                section=section,
                choices=options,
                writable=writable,
                # On every row, not just the first: a reader who scrolls to the fourth button
                # should not have to scroll back to learn why it cannot be changed.
                note=note if (index == 0 or not writable) else "",
            )
        )
    return out


def _per_key_toggles(setting: Any) -> list[Capability]:
    """A MULTIPLE_TOGGLE as one switch per key.

    Same idea as :func:`_per_key`, one shape simpler: every key is a boolean, so the rows need no
    per-key option list. Used by ``disable-keyboard-keys`` -- switching off Caps Lock or Insert --
    and ``m-key-leds``.

    The keys come from the *validator*, not from ``choices``: a bitfield setting reports
    ``choices`` as ``None`` and keeps the device's real key list in ``_validator.options``.
    ``choices_universe`` is the wrong source for the same reason as in :func:`_per_key` -- it is
    every key the protocol defines, not the ones this keyboard has.
    """
    name = getattr(setting, "name", "")
    options = getattr(getattr(setting, "_validator", None), "options", None)
    if not name or not options:
        return []
    keys = list(options)
    if len(keys) > MAP_MAX_KEYS:
        log.info("%s toggles %d keys, too many to expand into rows", name, len(keys))
        return []

    note = labels.map_note(name)
    writable = name not in labels.READ_ONLY_MAPS
    return [
        Capability(
            key=map_key(name, key),
            kind=Kind.TOGGLE,
            label=labels.key_label(name, key),
            group=labels.group_for(name),
            section=labels.label_for(setting),
            writable=writable,
            note=note if (index == 0 or not writable) else "",
        )
        for index, key in enumerate(keys)
    ]


def _capability(setting: Any) -> Capability | None:
    """One setting as a control, or ``None`` when its shape needs more than a row."""
    kind = getattr(setting, "kind", None)
    name = getattr(setting, "name", "")
    if kind is None or not name:
        return None

    common = {
        "key": setting_key(name),
        # Solaar's own label and description, which are written for users and translated. Falling
        # back to the internal name only when it has neither.
        "label": labels.label_for(setting),
        "group": labels.group_for(name),
        "description": labels.description_for(setting),
    }

    kind_name = getattr(kind, "name", str(kind))
    if kind_name == "TOGGLE":
        return Capability(**common, kind=Kind.TOGGLE)
    if kind_name == "CHOICE":
        choices = tuple(
            Choice(_choice_value(c), labels.choice_label(c)) for c in (setting.choices or ())
        )
        return Capability(**common, kind=Kind.CHOICE, choices=choices) if choices else None
    if kind_name in ("RANGE", "PACKED_RANGE"):
        bounds = getattr(setting, "range", None)
        if not bounds:
            return None
        low, high = bounds
        # The validator is private (``_validator``) and has no public step, so ask for one and fall
        # back to 1 rather than reaching inside. Measured: a real SmartShift setting has no
        # ``validator`` attribute at all, which is what an invented one in a fake concealed.
        step = getattr(getattr(setting, "_validator", None), "step", None)
        return Capability(
            **common,
            kind=Kind.RANGE,
            minimum=float(low),
            maximum=float(high),
            step=float(step or 1),
        )
    return None


def _choice_value(choice: Any) -> Any:
    """The value to send back for a choice.

    Solaar's choices are ``NamedInt``: an int that prints as a name. The *int* is what the device
    wants, and carrying the NamedInt through would make equality against a plain int fail on the
    way back in.
    """
    return int(choice) if isinstance(choice, int) else str(choice)


def _info(identity: Mapping[str, str], online: bool, battery: bool) -> list[Capability]:
    rows: list[Capability] = []
    for name, label in IDENTITY_ROWS:
        if not identity.get(name):
            continue
        rows.append(
            Capability(
                key=f"{INFO_PREFIX}{name}",
                kind=Kind.READOUT,
                label=label,
                group=GROUP_INFO,
                writable=False,
                copyable=name in ("serial", "unit_id"),
            )
        )
    if battery:
        rows.append(
            Capability(
                key=BATTERY_KEY,
                kind=Kind.METER,
                label="Battery",
                group=GROUP_INFO,
                minimum=0,
                maximum=100,
                unit="%",
                writable=False,
                description="Reported by the device; some models report a level rather than a "
                            "percentage, in which case this is approximate.",
            )
        )
    if rows and not online:
        import dataclasses

        rows[0] = dataclasses.replace(rows[0], note=NOTE_OFFLINE)
    return rows


def _pairing(pairing: Mapping[str, Any]) -> list[Capability]:
    """A receiver's slots, and the two actions that change them."""
    if not pairing:
        return []

    used = int(pairing.get("used", 0))
    total = int(pairing.get("total", 0))
    remaining = pairing.get("remaining")

    rows: list[Capability] = [
        Capability(
            key="info.slots",
            kind=Kind.READOUT,
            label="Paired devices",
            group=GROUP_PAIRING,
            writable=False,
        )
    ]
    if remaining is not None:
        rows.append(
            Capability(
                key="info.remaining_pairings",
                kind=Kind.READOUT,
                label="Pairings left",
                group=GROUP_PAIRING,
                writable=False,
                description="Some receivers permit a limited number of pairings in total.",
            )
        )

    if used < total:
        rows.append(
            Capability(
                key=PAIR_KEY,
                kind=Kind.ACTION,
                label="Pair a new device",
                action_label="Pair…",
                group=GROUP_PAIRING,
                description="Put the device in pairing mode first, then start this.",
                timeout=45.0,
            )
        )

    for index, name in sorted((pairing.get("devices") or {}).items()):
        rows.append(
            Capability(
                key=unpair_key(int(index)),
                kind=Kind.ACTION,
                label=f"Unpair {name}",
                action_label="Unpair",
                group=GROUP_PAIRING,
                confirm=True,
                confirm_detail=NOTE_UNPAIR,
                description=f"Slot {index}.",
            )
        )
    return rows


__all__ = [
    "BATTERY_KEY",
    "GROUP_INFO",
    "GROUP_PAIRING",
    "INFO_PREFIX",
    "PAIR_KEY",
    "SETTING_PREFIX",
    "build",
    "map_entry",
    "map_key",
    "setting_key",
    "setting_name",
    "unpair_index",
    "unpair_key",
]
