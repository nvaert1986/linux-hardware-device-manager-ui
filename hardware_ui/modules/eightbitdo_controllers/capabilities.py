"""What a controller offers, as capability rows.

Every row addresses the **currently selected profile**, chosen by :data:`KEY_PROFILE`. The
controller stores three, and which one is *live* is picked with its own button; this selector only
says which one is being edited. Those are different things, and conflating them would let a click
in this application change the controller under whoever is holding it -- so the active profile is
read and reported, never written.

The rows are fixed rather than discovered. Unlike Razer or Logitech, this device has no capability
query: the record's field map *is* the capability list, and it is the same for every controller
this module claims. A model whose record differs is a different module, not a different page.
"""

from __future__ import annotations

from hardware_ui.core.capability import Capability, Choice, Kind, Tier

from . import anchors
from .protocol import fieldmap as fm

KEY_PROFILE = "profile.slot"
KEY_ACTIVE = "profile.active"
KEY_RESET = "profile.reset"
KEY_DELETE = "profile.delete"
KEY_SYNC = "profile.sync"

MAP_PREFIX = "map."
TOGGLE_PREFIX = "toggle."
VALUE_PREFIX = "value."

GROUP_PROFILE = "Profile"
GROUP_BUTTONS = "Buttons"
GROUP_STICKS = "Sticks"
GROUP_TRIGGERS = "Triggers"
GROUP_VIBRATION = "Vibration"

#: Friendly names, for the row that says *which control is being remapped*. The record speaks in
#: enum names; a page should not.
LABEL = {
    "A": "A", "B": "B", "X": "X", "Y": "Y",
    "LB": "LB (left bumper)", "RB": "RB (right bumper)",
    "LT": "LT (left trigger)", "RT": "RT (right trigger)",
    "L3": "L3 (left stick click)", "R3": "R3 (right stick click)",
    "VIEW": "View", "MENU": "Menu", "STAR": "Star",
    "DPAD_UP": "D-pad Up", "DPAD_DOWN": "D-pad Down",
    "DPAD_LEFT": "D-pad Left", "DPAD_RIGHT": "D-pad Right",
    "PADDLE_L": "Left paddle", "PADDLE_R": "Right paddle",
    "NONE": "Not mapped",
}

#: The same controls named for the *dropdown*, where they are the value rather than the subject.
#:
#: Two different jobs that were being done by one table, and it showed: a row labelled
#: "LB (left bumper)" whose dropdown also read "LB (left bumper)" says the parenthetical twice and
#: fits neither column. In the list the reader already knows they are choosing a button, so the
#: button's own name is the whole message -- which is how the controller is silk-screened and how
#: the vendor's own configurator lists them.
OUTPUT_LABEL = {
    "NONE": "Not mapped",
    "VIEW": "View", "MENU": "Menu", "STAR": "Star",
    "DPAD_UP": "D-pad Up", "DPAD_DOWN": "D-pad Down",
    "DPAD_LEFT": "D-pad Left", "DPAD_RIGHT": "D-pad Right",
}

#: And the same names again for a control sitting **on the drawing**, where the picture is already
#: pointing at the part. "LB (left bumper)" beside an arrow touching the left bumper spends a whole
#: card explaining what the reader can see, and gets clipped doing it. Same table as the dropdown,
#: because the reasoning is the same one.
#:
#: The paddles are added back, because they are the one thing on a drawing that is *not* also an
#: output: nothing can be remapped *to* a paddle, so they are absent from the table above and fell
#: through to their raw enum names. "PADDLE_L" on a page is a leak, not a label.
SHORT_LABEL = dict(OUTPUT_LABEL) | {
    "PADDLE_L": "Left paddle",
    "PADDLE_R": "Right paddle",
}

TOGGLE_LABEL = {
    "INVERT_LX": "Invert left stick X", "INVERT_LY": "Invert left stick Y",
    "INVERT_RX": "Invert right stick X", "INVERT_RY": "Invert right stick Y",
    "SWAP_STICKS": "Swap sticks", "SWAP_DPAD_LS": "Swap D-pad and left stick",
    "SWAP_TRIGGERS": "Swap triggers", "NO_DEADZONE": "Disable dead zone",
    "FOURWAY_DPAD": "4-way D-pad only",
    "NO_IMPULSE": "Disable impulse triggers", "NO_RUMBLE": "Disable rumble",
}

#: Which tab each toggle belongs on.
TOGGLE_GROUP = {
    "INVERT_LX": GROUP_STICKS, "INVERT_LY": GROUP_STICKS,
    "INVERT_RX": GROUP_STICKS, "INVERT_RY": GROUP_STICKS,
    "SWAP_STICKS": GROUP_STICKS, "SWAP_DPAD_LS": GROUP_STICKS,
    "NO_DEADZONE": GROUP_STICKS, "FOURWAY_DPAD": GROUP_STICKS,
    "SWAP_TRIGGERS": GROUP_TRIGGERS,
    "NO_IMPULSE": GROUP_VIBRATION, "NO_RUMBLE": GROUP_VIBRATION,
}

#: Value sliders: key suffix -> (kind, side, group, label).
VALUES = {
    "dz_l": ("DZ", "L", GROUP_STICKS, "Left dead zone"),
    "dz_r": ("DZ", "R", GROUP_STICKS, "Right dead zone"),
    "trig_l": ("TRIG", "L", GROUP_TRIGGERS, "Left trigger travel"),
    "trig_r": ("TRIG", "R", GROUP_TRIGGERS, "Right trigger travel"),
    "vib_l": ("VIB", "L", GROUP_VIBRATION, "Left motor"),
    "vib_r": ("VIB", "R", GROUP_VIBRATION, "Right motor"),
    "vib_lt": ("VIB", "LT", GROUP_VIBRATION, "Left trigger motor"),
    "vib_rt": ("VIB", "RT", GROUP_VIBRATION, "Right trigger motor"),
}

#: Outputs offered in a remap dropdown, in a sensible reading order rather than bit order.
OUTPUTS = ("NONE", "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "L3", "R3",
           "VIEW", "MENU", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")


def map_key(name: str) -> str:
    return f"{MAP_PREFIX}{name.lower()}"


def map_name(key: str) -> str | None:
    """The input a remap key addresses, or None. The inverse of :func:`map_key`."""
    if not key.startswith(MAP_PREFIX):
        return None
    name = key[len(MAP_PREFIX):].upper()
    return name if name in fm.INPUT_IDX or name in fm.REL_PADDLE else None


def toggle_key(name: str) -> str:
    return f"{TOGGLE_PREFIX}{name.lower()}"


def toggle_name(key: str) -> str | None:
    if not key.startswith(TOGGLE_PREFIX):
        return None
    name = key[len(TOGGLE_PREFIX):].upper()
    return name if name in fm.TOGGLES else None


def value_key(suffix: str) -> str:
    return f"{VALUE_PREFIX}{suffix}"


def value_spec(key: str) -> tuple[str, str] | None:
    """``(kind, side)`` for a slider key, or None."""
    if not key.startswith(VALUE_PREFIX):
        return None
    spec = VALUES.get(key[len(VALUE_PREFIX):])
    return (spec[0], spec[1]) if spec else None


def _sync(group: str) -> Capability:
    """The write button, which every tab gets a copy of.

    Nothing here reaches the controller until this is pressed, so a page where it is only on one
    tab is a page where finishing the job means remembering which tab that was. The vendor-derived
    configurator keeps Apply in a bar above every tab for the same reason; this application has no
    such bar, so the button is declared once per group instead.

    Deliberately the **same key** in each group. It is one action on one device, and giving each
    tab its own key would mean four capabilities to keep in step, four results to clear and four
    chances for them to disagree about whether the record has been written.
    """
    return Capability(
        key=KEY_SYNC, kind=Kind.ACTION, label="Configuration",
        action_label="Sync to controller", group=group, section="Save", tier=Tier.COMMON,
        # Holds the whole page while it runs. The record is one block with one checksum, so a
        # dropdown changed after the bytes were assembled and before they land would put a value
        # on screen that is not the one the controller was given -- and, if the change arrived
        # mid-write, neither configuration in full.
        exclusive=True,
        description=(
            "Write all three profiles to the controller and commit them. Everything on these tabs "
            "is edited here first and sent in one go, because the controller stores its whole "
            "configuration as a single block — there is no such thing as writing one button."
        ),
    )


def build(*, profile_written: bool) -> list[Capability]:
    """The rows for a controller.

    ``profile_written`` gates everything below the profile selector: an empty slot has nothing to
    edit, and offering greyed sliders over a profile that does not exist reads as breakage. The
    selector and the create action stay live so there is a way out.
    """
    out: list[Capability] = [
        _sync(GROUP_PROFILE),
        Capability(
            key=KEY_PROFILE, kind=Kind.CHOICE, label="Editing profile",
            group=GROUP_PROFILE, section="Profile", tier=Tier.COMMON,
            choices=tuple(Choice(i, f"Profile {i + 1}") for i in range(fm.SLOT_COUNT)),
            description=(
                "Which of the controller's three stored profiles is being edited here. Which one "
                "is *active* is chosen with the controller's own button."
            ),
        ),
        Capability(
            key=KEY_ACTIVE, kind=Kind.READOUT, label="Active on the controller",
            group=GROUP_PROFILE, section="Profile",
            description="Read only: switching profiles is done on the controller itself.",
        ),
        # One action, two jobs, exactly as the vendor-derived source has it: the button reads
        # "Create profile" over an empty slot and "Reset to default" over a written one. Both do
        # the same thing -- fill the slot with the controller's own defaults -- but a button
        # offering to *reset* something that does not exist is why an empty profile read as a
        # broken page rather than an empty one.
        Capability(
            key=KEY_RESET,
            kind=Kind.ACTION,
            label="This profile",
            action_label="Create profile…" if not profile_written else "Reset to default…",
            group=GROUP_PROFILE, section="Profile", tier=Tier.COMMON,
            confirm=profile_written,
            confirm_detail="The profile's mappings and settings are replaced with the defaults.",
            description=(
                "Fills this empty slot with the controller's default mapping, which is what makes "
                "it editable." if not profile_written else
                "Replaces this profile's mappings and settings with the controller's defaults."
            ),
        ),
        Capability(
            key=KEY_DELETE, kind=Kind.ACTION, label="Delete", action_label="Delete profile…",
            group=GROUP_PROFILE, section="Profile", confirm=True,
            confirm_detail="The profile is emptied on the controller.",
            # Two conditions, and the second is not decoration: deleting an already-empty slot
            # writes 532 bytes and burns a checksum step to achieve nothing.
            requires=KEY_PROFILE, requires_value=tuple(range(1, fm.SLOT_COUNT)),
            writable=profile_written,
        ),
    ]

    # Sectioned and ordered **by which drawing shows the control**, taken from `anchors.VIEWS` so
    # the page and the artwork cannot disagree. Three sections rather than two: the bumpers and
    # triggers used to sit among the face buttons, which no longer matches anything the user is
    # looking at now that they have their own view.
    #
    # Contiguous by construction, which the shell requires: it groups adjacent rows into a section
    # and does not reorder them, so interleaving views would render one heading several times.
    choices = tuple(Choice(name, OUTPUT_LABEL.get(name, name)) for name in OUTPUTS)
    remappable = (*fm.REMAPPABLE, *fm.PADDLES)
    for view, (_, keys) in anchors.VIEWS.items():
        for name in keys:
            if name not in remappable:
                continue                    # a control the drawing shows but the record cannot map
            out.append(Capability(
                key=map_key(name), kind=Kind.CHOICE, label=LABEL.get(name, name),
                short_label=SHORT_LABEL.get(name, name),
                group=GROUP_BUTTONS,
                section=anchors.VIEW_LABELS[view],
                tier=Tier.COMMON, choices=choices, writable=profile_written,
                description=f"What the controller reports when {LABEL.get(name, name)} is pressed.",
            ))

    for name, label in TOGGLE_LABEL.items():
        out.append(Capability(
            key=toggle_key(name), kind=Kind.TOGGLE, label=label,
            group=TOGGLE_GROUP[name], section="Options", writable=profile_written,
        ))

    for suffix, (kind, _side, group, label) in VALUES.items():
        low, high = fm.VALUE_RANGE[kind]
        out.append(Capability(
            key=value_key(suffix), kind=Kind.RANGE, label=label, group=group,
            section="Levels", minimum=0, maximum=100, step=1, unit="%",
            writable=profile_written,
            description=f"Raw range {low}–{high} on the device.",
        ))

    # A copy of the write button at the end of every other tab. Appended last so it lands under
    # its own heading below the controls, which is where a "done" button belongs.
    for group in (GROUP_BUTTONS, GROUP_STICKS, GROUP_TRIGGERS, GROUP_VIBRATION):
        out.append(_sync(group))
    return out


__all__ = [
    "GROUP_BUTTONS", "GROUP_PROFILE", "GROUP_STICKS", "GROUP_TRIGGERS", "GROUP_VIBRATION",
    "KEY_ACTIVE", "KEY_DELETE", "KEY_PROFILE", "KEY_RESET", "KEY_SYNC",
    "LABEL", "OUTPUTS", "OUTPUT_LABEL", "TOGGLE_LABEL",
    "VALUES", "build", "map_key", "map_name", "toggle_key", "toggle_name", "value_key",
    "value_spec",
]
