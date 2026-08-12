"""The contract every device module speaks.

A module describes what its device can do as a list of :class:`Capability` values. The shell
renders that list; it has no per-device knowledge and no per-device QML. Adding a device means
adding capabilities, never adding UI.

Seven kinds cover every setting found across the Sony MDR, Poly Deckard, Jabra GNP, DDC/CI and
OpenRazer protocols. Resist adding an eighth before checking that an existing one, plus metadata,
will not do -- each new kind is a new delegate in the shell and a new thing that can look out of
place. ``COLOR`` is the only one added after the fact, and its justification is recorded on it.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


class Kind(enum.StrEnum):
    """How a capability is presented and what shape its value takes."""

    TOGGLE = "toggle"
    """Boolean. Value is ``bool``."""

    CHOICE = "choice"
    """One of ``choices``. Value is the ``value`` of a :class:`Choice`."""

    RANGE = "range"
    """Numeric within ``minimum``..``maximum``. Value is ``int`` or ``float``."""

    ACTION = "action"
    """Write-only trigger with no value -- reset, identify, disconnect."""

    READOUT = "readout"
    """Read-only scalar, rendered as text with ``unit``. Battery, firmware, serial."""

    TEXT = "text"
    """Free-form string the user can edit, e.g. a device name."""

    COLOR = "color"
    """A single colour, as an ``#rrggbb`` string.

    The seventh kind, added against this module's own advice to resist adding one. The check it
    had to pass: can an existing kind plus metadata do the job? ``TEXT`` can -- by asking the user
    to type ``#00ff00``, which is what a settings form does when it has given up. Every
    lighting-capable device family wants this (Razer now; Logitech and OpenRGB-style devices
    later), and it costs the renderer one delegate that opens the platform colour dialog.

    The value is a string rather than a Qt colour so that ``core`` stays free of Qt and a module
    never imports a GUI type."""

    METER = "meter"
    """Read-only numeric shown as a filled bar plus its value, bounded by minimum/maximum.

    Battery is the motivating case and it appears on every headset module. A bar communicates
    "nearly flat" at a glance in a way that the string "18 %" does not."""


class Tier(enum.StrEnum):
    """Which disclosure level a capability belongs to.

    Devices such as the Jabra Evolve2 85 expose several hundred properties. Showing them all at
    once is unusable, so every capability declares where it belongs.
    """

    COMMON = "common"
    """Shown by default. Keep this to roughly 10-20 per device."""

    ALL = "all"
    """Behind "All settings", grouped by :attr:`Capability.group`."""

    ADVANCED = "advanced"
    """Behind an explicit opt-in. Anything that can confuse, degrade or brick."""


class _Any:
    """Sentinel: "any truthy value" for :attr:`Capability.requires_value`.

    A distinct object rather than ``None``, because ``None`` is a legitimate value a device may
    report and must not accidentally mean "unset".
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<any truthy>"


_ANY = _Any()


@dataclass(frozen=True, slots=True)
class Choice:
    """One option of a :attr:`Kind.CHOICE` capability."""

    value: Any
    label: str
    icon: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class PromptField:
    """One field of a multi-field prompt.

    A secret rarely arrives alone. Programming a YubiKey slot for challenge-response takes a key
    *and* whether it should need a touch; a static password takes the text *and* the keyboard
    layout it will be typed on; an OATH account takes seven things at once. Those modifiers belong
    **in the dialog with what they modify** -- put on the page instead, next to three other
    actions, nothing says which button they change. That was a real complaint about a real page.
    """

    key: str
    label: str
    kind: Kind = Kind.TEXT
    """``TEXT``, ``TOGGLE`` or ``CHOICE``. Anything else is meaningless in a dialog."""

    choices: tuple[Choice, ...] = ()
    default: Any = None
    secret: bool = False
    """Masked as typed, with a reveal button -- the dialog is the only place it is ever shown."""

    optional: bool = False
    """Empty is a real answer. Usually paired with "leave blank and one will be generated"."""

    max_length: int = 0
    """Shown as a counter and enforced, the way the vendor's own dialogs do it."""

    generate: bool = False
    """Offer a button that fills this field with ``max_length`` random hex characters."""

    description: str = ""


@dataclass(frozen=True, slots=True)
class Capability:
    """A single readable and/or writable thing on a device.

    ``key`` is the stable identifier used everywhere -- config files, the CLI, pinned favourites,
    and change notifications. It must not change between releases once published, because users'
    pins and scripts reference it.
    """

    key: str
    kind: Kind
    label: str
    group: str = "General"
    tier: Tier = Tier.ALL

    section: str = ""
    """Optional sub-heading within a group, rendered as a header row above this capability.

    A tab of twenty unrelated rows reads as a list; splitting it into named sections makes it a
    form. Capabilities sharing a section must be declared contiguously -- the view groups
    adjacent rows, it does not reorder them.
    """

    icon: str = ""
    """freedesktop icon name. Prefer spec names (``audio-headphones``) so they resolve against
    Adwaita too when Breeze is not installed."""

    description: str = ""

    # Kind.CHOICE
    choices: Sequence[Choice] = ()

    # Kind.RANGE
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 1.0
    unit: str = ""

    writable: bool = True

    requires: str = ""
    """Key of another capability this one depends on. The shell renders a gated capability
    disabled rather than hiding it, so the dependency stays visible on screen."""

    requires_value: Any = _ANY
    """Value ``requires`` must hold. Defaults to "any truthy value".

    Truthiness alone is not enough, which the Sony MDR protocol demonstrates: ambient sound level
    applies only when noise control *equals* ``"ambient"`` -- it is meaningless in ``"anc"`` mode,
    which is equally truthy. Set this to the specific value, or to a tuple of acceptable values.
    """

    writes_with: tuple[str, ...] = ()
    """Other capability keys carried by the *same* protocol message.

    Sony's speak-to-chat sends enable, sensitivity and timeout in one ``set_stc``; noise control
    sends mode, focus-on-voice and level in one ``set_ncasm``. Writing any one of them re-sends
    the others, so all of them must be held pending together -- otherwise a control the user did
    not touch stays live, gets written from state captured mid-sequence, and reverts. That is
    what turned speak-to-chat off while its sensitivity was being changed.

    Declare the full group on every member; the shell takes the union.
    """

    action_label: str = ""
    """Button text for :attr:`Kind.ACTION`. Defaults to "Run", which is right for nothing.

    "Calibrate ranges…" and "Restore factory defaults" tell the user what the button does before
    they press it; the label column alone does not, because an action's label reads as a noun.
    """

    max_length: int = 0
    """Longest text this capability accepts, 0 for no limit. ``Kind.TEXT`` only.

    A protocol limit, not a style choice: a Jabra GNP payload holds 58 bytes, of which the
    subcommand and the string's length prefix take one each, so a longer name can never reach the
    device. Capping the field beats letting the write fail.
    """

    secret: bool = False
    """Hide this :attr:`Kind.TEXT` value as it is typed, and never repaint it from device state.

    A PIN is the motivating case and it is not merely cosmetic: a security key's PIN must be
    entered to manage credentials or change policy, and echoing it into a form -- or restoring it
    from a value map after a refresh -- would leave it on screen. Values for a secret capability
    are write-only from the shell's point of view.
    """

    prompt: str = ""
    """This :attr:`Kind.ACTION` needs a secret first, so the shell asks for it and passes the
    answer as the action's value.

    ``"pin"`` asks for one PIN and passes a string. ``"pin_change"`` asks for current, new and
    *confirm new*, checks the two new entries match, and passes ``(current, new)``. ``"pin_set"``
    is the same without the current field, for a key that has no PIN yet -- the module knows
    which, so it says so rather than the shell guessing.

    A dialog rather than fields on the form, which is how the reference KCM does it and is the
    better shape: a PIN belongs to the operation being performed, not to the page. Leaving one
    sitting in a form means it is on screen for as long as the page is, and made "Test this key"
    require filling in a field labelled for changing the PIN.

    For ``"pin_change"`` the minimum PIN length is taken from :attr:`minimum`, so the dialog can
    reject a too-short PIN without a round trip to the device.

    On an ``ACTION`` the answer becomes the value. On anything else -- a slider that also needs a
    PIN -- the value would otherwise be lost, so the module receives ``(value, answer)``.
    """

    prompt_detail: str = ""
    """Explanatory text shown in the prompt -- what is about to happen and what it will cost."""

    prompt_label: str = ""
    """What the prompt's field is called. Defaults to "PIN".

    Not every secret is a PIN. A YubiKey OTP slot takes an HMAC-SHA1 *secret* and a static
    *password*, and a dialog that asks for a "PIN" when it means a 40-character hex secret is
    telling the user the wrong thing about what to type.
    """

    prompt_fields: tuple[PromptField, ...] = ()
    """Ask for several things at once, instead of the single secret :attr:`prompt` asks for.

    The answer reaches the module as a ``dict`` keyed by :attr:`PromptField.key`, in place of the
    action's value. Setting this makes :attr:`prompt_label` and :attr:`prompt_optional` redundant --
    each field carries its own.
    """

    prompt_optional: bool = False
    """Accept an empty answer and let the module decide what it means.

    The prompt normally refuses to close on an empty field, which is right for a PIN -- there is no
    such thing as an empty one. It is wrong wherever blank is a real choice: leaving the secret
    field empty when programming challenge-response means "generate one for me", which is how
    ``ykman`` behaves and the option most people want.
    """

    file_dialog: str = ""
    """``"open"`` or ``"save"``: this :attr:`Kind.ACTION` needs a file, so the shell asks for one
    first and passes the chosen path as the action's value.

    A module runs on the asyncio thread and must never touch a widget, so it cannot raise a file
    chooser itself. Declaring the need here keeps that rule intact and makes the dialog the
    platform's own. Export and import want this in every module that has settings worth moving --
    Razer macros today, Dell monitor profiles next.
    """

    file_filter: str = ""
    """Qt name filter for :attr:`file_dialog`, e.g. ``"Macros (*.json);;All files (*)"``."""

    file_suffix: str = ""
    """Extension appended to a save filename when the user types none."""

    timeout: float = 0.0
    """Seconds this capability's write may take, overriding the shell's default. 0 means default.

    DDC/CI forces this. A range calibration writes six probe values to each of five features and
    reads every one of them back, and entering PIP/PBP polls a blanked panel for up to ten
    seconds. Both are correct behaviour, and both exceed a timeout sized for an RFCOMM round-trip.
    """

    confirm: bool = False
    """Ask before applying, for a change that is disruptive but does *not* restart the device.

    Distinct from :attr:`reboots`, which additionally means the write cannot be confirmed and the
    shell must reconnect afterwards. Switching a monitor's input moves the picture to another
    machine and a factory reset discards every setting -- neither drops the connection, so they
    take the normal write path, but neither should happen on a stray click.
    """

    reboots: bool = False
    """Applying this makes the device disconnect and restart.

    The shell must confirm with the user *before* writing, and reconnect afterwards. Sony's
    sound-quality, CUSTOM-button and multipoint settings all do this; writing one without warning
    drops the user's audio mid-track with no explanation.
    """

    confirm_detail: str = ""
    """Extra sentence shown in the confirmation, e.g. "Stable Connection disables LDAC."

    Consequences the user cannot infer from the setting's name, and the reason the original
    app's dialogs were worth more than a generic "are you sure".
    """

    note: str = ""
    """Static explanatory text shown under the control.

    For consequences the label cannot convey -- "Multipoint and Stable-Connection mode disable
    LDAC", "the XM4 offers only Auto or Off". Use :class:`Advisory` for text that changes with
    device state.
    """

    copyable: bool = False
    """Put a copy button beside this readout.

    Some values exist to be pasted somewhere else -- a one-time code, a serial number, an AAGUID.
    Selecting text with the mouse is technically possible and useless in practice for a six-digit
    code that is replaced every thirty seconds.
    """

    suffix_from: str = ""
    """Key of another value to show after this one, lighter, on the same line.

    For something that qualifies a reading rather than being one: how long a one-time code is
    still good for belongs *beside the code*, not in a row of its own under a second copy of the
    section heading. The suffix is display only -- :attr:`copyable` still copies the value.
    """

    suffix_total: int = 0
    """Draw the suffix as a depleting bar out of this many, as well as printing it.

    A number counting down says how long is left; a bar says it at a glance, which is what the
    value is actually for. Set only where the total is meaningful and per-row -- two accounts on
    the same page can have different periods.
    """

    experimental: bool = False
    """Derived from protocol documentation rather than observed on hardware. The shell marks it."""

    def __post_init__(self) -> None:
        if self.kind is Kind.CHOICE and not self.choices:
            raise ValueError(f"{self.key}: CHOICE requires choices")
        if self.kind in (Kind.RANGE, Kind.METER) and self.minimum >= self.maximum:
            raise ValueError(f"{self.key}: RANGE requires minimum < maximum")
        if self.kind in (Kind.READOUT, Kind.ACTION) and self.requires and not self.writable:
            pass  # legal; a readout may still be gated on a feature being enabled

    @property
    def readable(self) -> bool:
        return self.kind is not Kind.ACTION


@dataclass(frozen=True, slots=True)
class Advisory:
    """A state-dependent message about one capability, optionally locking it.

    Some controls are unavailable for reasons no static schema can express, and the reason is
    the useful part. The Sony equaliser is the motivating case: on a WH-1000XM3 it cannot be used
    while LDAC is active, and the message tells the user exactly how to proceed -- switch Sound
    Quality to "Prioritize Stable Connection". A greyed-out control with no explanation is a
    support ticket.
    """

    message: str = ""
    locked: bool = False
    """Disable the control regardless of ``writable`` and any ``requires`` gate."""


@dataclass(frozen=True, slots=True)
class CapabilityValue:
    """A capability's current value, as reported by the device."""

    key: str
    value: Any
    stale: bool = False
    """True when shown from an optimistic local write not yet confirmed by the device."""


@dataclass(slots=True)
class CapabilitySet:
    """The capabilities of one device, with lookup and grouping helpers."""

    capabilities: list[Capability] = field(default_factory=list)

    def __iter__(self):
        return iter(self.capabilities)

    def __len__(self) -> int:
        return len(self.capabilities)

    def by_key(self, key: str) -> Capability | None:
        return next((c for c in self.capabilities if c.key == key), None)

    def tier(self, tier: Tier) -> list[Capability]:
        return [c for c in self.capabilities if c.tier is tier]

    def groups(self) -> dict[str, list[Capability]]:
        """Capabilities grouped by :attr:`Capability.group`, insertion-ordered.

        The shell turns these groups into sections; group names are therefore user-visible.
        """
        out: dict[str, list[Capability]] = {}
        for cap in self.capabilities:
            out.setdefault(cap.group, []).append(cap)
        return out

    def search(self, needle: str) -> list[Capability]:
        """Case-insensitive match over label, key, group and description.

        Search is the primary navigation for large sets, not a convenience -- for a device with
        400+ properties it is how anyone finds anything.
        """
        n = needle.casefold().strip()
        if not n:
            return list(self.capabilities)
        return [
            c
            for c in self.capabilities
            if n in c.label.casefold()
            or n in c.key.casefold()
            or n in c.group.casefold()
            or n in c.description.casefold()
        ]


def gate_satisfied(cap: Capability, value_of: Any) -> bool:
    """Whether *cap*'s ``requires`` dependency is currently met.

    ``value_of`` is a callable mapping a capability key to its current value. Lives here rather
    than in the Qt model so the CLI and tests apply identical gating rules.
    """
    if not cap.requires:
        return True
    current = value_of(cap.requires)
    if cap.requires_value is _ANY:
        return bool(current)
    if isinstance(cap.requires_value, tuple):
        return current in cap.requires_value
    return current == cap.requires_value
