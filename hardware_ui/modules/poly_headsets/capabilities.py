"""A Poly catalogue, expressed in the shell's schema.

There is no per-model code here and there must never be any. Poly ships one JSON catalogue per
product, each declaring its own message ids and typed values, so a device the author has never
seen gets a correct page from its own catalogue -- the same bet the Sony module makes on the
function list and the Dell module on the capability string.

Two rules from the reference implementation shape what appears:

**The catalogue is the UI-exposed subset, not the capability list.** The V4310 answers 33 ids but
its catalogue lists 26. Support comes from a live probe; the catalogue supplies labels and types.

**Write-only actions are never controls.** ``restoreDefaults`` and ``clearPairedDevices`` have no
get id, are destructive, and must not be reachable from a generic setter -- they live on their own
tab behind individual confirmations.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from hardware_ui.core import Capability, CapabilitySet, Choice, Kind

from . import labels
from .protocol import catalogue as cat

SETTING_PREFIX = "setting."
INFO_PREFIX = "info."
ACTION_PREFIX = "action."

BATTERY_KEY = "info.battery"
REFRESH_KEY = "action.refresh"
RECONNECT_KEY = "action.reconnect"

GROUP_INFO = "Info"
GROUP_MAINTENANCE = "Maintenance"

#: Identity fields worth showing, in display order, with the wording the reference implementation
#: used on its Info panel.
IDENTITY_ROWS: tuple[tuple[str, str], ...] = (
    ("TATTOO_SERIAL_NUMBER", "Serial number"),
    ("FIRMWARE_VERSION", "Firmware"),
    ("HARDWARE_REVISION_STRING", "Hardware"),
    ("STACK_VERSION", "Bluetooth stack"),
    ("GENES_GUID", "Device GUID"),
)

#: Shown when more than one Poly device is plugged in, because then the headset has two ways in.
GROUP_ADAPTER = "USB adapter"

ADAPTER_PREFIX = "adapter."

NOTE_ADAPTER = (
    "These belong to the USB adapter itself rather than to the headset — pairing it to a headset, "
    "and the dial tone it plays for softphones. Everything on the other tabs is the headset's."
)

NOTE_TWO_ROUTES = (
    "Your headset appears twice while it is both in its charging stand and paired to the BT700 "
    "adapter — one headset, two ways of reaching it. Either entry configures it, and both show "
    "the same settings.\n\n"
    "The stand entry disappears when you lift the headset out; the adapter entry stays as long "
    "as the headset is in range."
)

NOTE_MAINTENANCE = (
    "These actions cannot be undone and take effect immediately on the headset."
)
NOTE_REFRESH = (
    "Settings are read when you connect, and the headset reports its own changes as they happen — "
    "nothing is polled, so the link stays silent while you are not using it. Use this if you have "
    "changed something from another computer or phone."
)
CONFIRM_ACTION = "This cannot be undone and takes effect immediately on the headset."


def setting_key(name: str) -> str:
    return f"{SETTING_PREFIX}{name}"


def action_key(name: str) -> str:
    return f"{ACTION_PREFIX}{name}"


def setting_name(key: str) -> str:
    """The catalogue name behind a capability key, for either a setting or an action."""
    for prefix in (SETTING_PREFIX, ACTION_PREFIX):
        if key.startswith(prefix):
            return key[len(prefix) :]
    return ""


def boolean_names(setting: cat.Setting) -> tuple[str, str] | None:
    """``(on, off)`` when a setting is a plain switch, else ``None``.

    Two values named true/false, on/off or enabled/disabled read as a checkbox; anything else is a
    list. Straight from the reference implementation's ``BOOLEAN_CHOICES``.
    """
    names = [c.name for c in setting.choices]
    if len(names) != 2 or {n.lower() for n in names} not in labels.BOOLEAN_CHOICES:
        return None
    on = next(n for n in names if n.lower() in ("true", "on", "enabled"))
    return on, next(n for n in names if n != on)


def build(
    catalogue: cat.Catalogue | None,
    *,
    supported: Sequence[str] = (),
    identity: Mapping[str, str] | None = None,
    has_battery: bool = False,
    two_routes: bool = False,
    adapter: tuple[cat.Catalogue | None, Sequence[str]] = (None, ()),
) -> CapabilitySet:
    """The page for one headset.

    *supported* is what the device actually answered -- a catalogue entry the hardware reports as
    ``SETTING_UNKNOWN`` produces no control at all, rather than a dead one.
    """
    # Which entry this is, decided by what the device answered rather than by its name: an entry
    # that returns no settings cannot configure the headset, whatever it is called.
    out: list[Capability] = list(
        _info(identity or {}, has_battery, NOTE_TWO_ROUTES if two_routes else "")
    )
    # Built up front, because the adapter's tab does not depend on the headset having a
    # catalogue: a headset nobody has vendor data for still sits behind an adapter you can pair.
    adapter_rows = _adapter(*adapter)
    if catalogue is None:
        return CapabilitySet(out + adapter_rows)

    live = set(supported)
    grouped: dict[str, list[Capability]] = {}
    actions: list[Capability] = []
    for setting in catalogue.settings:
        if setting.is_action:
            actions.append(_action(setting))
            continue
        if not setting.choices or setting.name not in live:
            continue
        grouped.setdefault(labels.group_for(setting.name), []).append(_setting(setting))

    for group in labels.GROUP_ORDER:
        out.extend(grouped.get(group, ()))
    out.extend(actions)
    out.extend(adapter_rows)
    return CapabilitySet(out)


def adapter_key(name: str) -> str:
    """Adapter settings share names with the headset's, so they need their own keyspace."""
    return f"{ADAPTER_PREFIX}{setting_key(name)}"


def _adapter(catalogue: cat.Catalogue | None, supported: Sequence[str]) -> list[Capability]:
    """The dongle's own settings, on their own tab.

    A BT700 answers two of them, and attaching downstream to the headset -- which is the whole
    point of a dongle -- put them out of reach. Pairing an adapter to a headset is precisely the
    thing you cannot do from the headset.
    """
    if catalogue is None:
        return []
    live = set(supported)
    out: list[Capability] = []
    for setting in catalogue.settings:
        if setting.name not in live or setting.is_action or not setting.choices:
            continue
        base = _setting(setting)
        out.append(
            dataclasses.replace(
                base,
                key=adapter_key(setting.name),
                group=GROUP_ADAPTER,
                section="",
                note=NOTE_ADAPTER if not out else "",
            )
        )
    return out


def _setting(setting: cat.Setting) -> Capability:
    group = labels.group_for(setting.name)
    common = {
        "key": setting_key(setting.name),
        "label": labels.setting_label(setting),
        "group": group,
        "description": labels.setting_description(setting),
    }
    pair = boolean_names(setting)
    if pair is not None:
        return Capability(**common, kind=Kind.TOGGLE)
    shown = labels.value_labels(setting)
    return Capability(
        **common,
        kind=Kind.CHOICE,
        choices=tuple(Choice(c.name, shown.get(c.name) or c.name) for c in setting.choices),
    )


def _action(setting: cat.Setting) -> Capability:
    return Capability(
        key=action_key(setting.name),
        kind=Kind.ACTION,
        label=labels.setting_label(setting),
        action_label=labels.setting_label(setting),
        group=GROUP_MAINTENANCE,
        confirm=True,
        confirm_detail=CONFIRM_ACTION,
        note=NOTE_MAINTENANCE,
    )


def _info(
    identity: Mapping[str, str], has_battery: bool, note: str = ""
) -> list[Capability]:
    out: list[Capability] = []
    if has_battery:
        out.append(
            Capability(
                key=BATTERY_KEY,
                kind=Kind.METER,
                label="Battery",
                group=GROUP_INFO,
                minimum=0,
                maximum=100,
                unit="%",
                writable=False,
                # The device reports a step out of a step count, not a percentage: a live V4310
                # answered 9 of 11. Showing "9 %" would be wrong by an order of magnitude.
                description="Reported by the headset in steps; shown as an approximate percentage.",
            )
        )
    out += [
        Capability(
            key=f"{INFO_PREFIX}{field.lower()}",
            kind=Kind.READOUT,
            label=label,
            group=GROUP_INFO,
            section="Identity",
            writable=False,
        )
        for field, label in IDENTITY_ROWS
        if field in identity
    ]
    out.append(
        Capability(
            key=f"{INFO_PREFIX}connection",
            kind=Kind.READOUT,
            label="Connection",
            note=note,
            group=GROUP_INFO,
            section="Identity",
            writable=False,
        )
    )
    out.append(
        Capability(
            key=REFRESH_KEY,
            kind=Kind.ACTION,
            label="Values",
            action_label="Re-read from headset",
            group=GROUP_INFO,
            section="Actions",
            note=NOTE_REFRESH,
        )
    )
    out.append(
        Capability(
            key=RECONNECT_KEY,
            kind=Kind.ACTION,
            label="Session",
            action_label="Reconnect",
            group=GROUP_INFO,
            section="Actions",
            description=(
                "Drop the connection and set it up again: redo the handshake, re-detect the "
                "headset behind a dongle, and reload its catalogue. Use this when the link "
                "itself has gone wrong; re-reading only refreshes values."
            ),
        )
    )
    return out
