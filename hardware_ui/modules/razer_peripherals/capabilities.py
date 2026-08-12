"""A Razer device, expressed in the shell's schema.

Everything is derived from what OpenRazer says the device supports. There is no per-model table,
which is what lets an untested Razer get a correct page: OpenRazer covers hundreds of devices and
answers ``has()`` for each one.

Scope is settings, not authoring. Choosing a built-in effect and the colour to run it in is a
setting; per-key colour mapping, effect layering and saved lighting profiles are an RGB editor,
and OpenRGB and Polychromatic already do that job well.
"""

from __future__ import annotations

from collections.abc import Sequence

from hardware_ui.core import Capability, CapabilitySet, Choice, Kind

GROUP_INFO = "Info"
GROUP_DEVICE = "Device"
GROUP_LIGHTING = "Lighting"
GROUP_MACROS = "Macros"

MACRO_STATUS_KEY = "macro.status"
MACRO_MODIFIER_KEY = "macro.mode_modifier"
MACRO_REFRESH_KEY = "macro.refresh"
MACRO_SAVE_KEY = "macro.save"
MACRO_RESTORE_KEY = "macro.restore"
MACRO_AUTORESTORE_KEY = "macro.autorestore"
MACRO_EXPORT_KEY = "macro.export"
MACRO_IMPORT_KEY = "macro.import"
MACRO_FILTER = "Macro files (*.json);;All files (*)"
MACRO_DELETE_PREFIX = "macro.delete."

#: What OpenRazer's macro feature actually is, in our own words. Recording is a keyboard gesture
#: handled by the daemon -- there is no D-Bus call for it, so no button here could start one.
NOTE_MACROS = (
    "Recording happens on the keyboard itself, not in this window — OpenRazer's daemon watches "
    "for the gesture and there is no way to start it from software.\n\n"
    "1. Press FN and the macro-mode key together (FN+M on most models, FN+F9 on some) to enter "
    "macro mode.\n"
    "2. Press the key to assign the macro to — only M1–M5 can hold one.\n"
    "3. Press the keys you want recorded, in order.\n"
    "4. Press FN and the macro-mode key again to save.\n\n"
    "Two things worth knowing: macros live only in the running daemon, so they are lost when it "
    "stops or the machine reboots; and they replay instantly, which some games and applications "
    "do not cope with."
)

DPI_KEY = "device.dpi"
DPI_X_KEY = "device.dpi_x"
DPI_Y_KEY = "device.dpi_y"
DPI_STAGE_KEY = "device.dpi_stage"
DPI_LOCK_KEY = "device.dpi_lock"
POLL_RATE_KEY = "device.poll_rate"
GAME_MODE_KEY = "device.game_mode"
MACRO_LED_KEY = "device.macro_led"
BATTERY_KEY = "info.battery"
CHARGING_KEY = "info.charging"
IDLE_TIME_KEY = "device.idle_time"
LOW_BATTERY_KEY = "device.low_battery_threshold"
GROUP_DPI = "DPI stages"
STAGE_PREFIX = "dpi.stage."
STAGE_APPLY_KEY = "dpi.apply"
STAGE_SYNC_KEY = "dpi.sync"
MAX_STAGES = 5

#: Polychromatic's table, kept identical so the two apps suggest the same stages for the same
#: hardware. Keyed on the device's maximum DPI.
DEFAULT_STAGES: dict[int, tuple[int, ...]] = {
    30000: (400, 800, 1600, 3200, 6400),
    16000: (800, 1800, 4500, 9000, 16000),
    8500: (800, 1600, 3200, 6400, 8500),
    8200: (800, 1800, 3200, 6400, 8200),
}

SCROLL_MODE_KEY = "device.scroll_free_spin"
SCROLL_ACCEL_KEY = "device.scroll_acceleration"
SMART_REEL_KEY = "device.scroll_smart_reel"

#: Boolean device settings beyond the headline ones, keyed by our capability key. The OpenRazer
#: capability name is the value; a device gets the control only if it advertises it.
EXTRA_TOGGLES: dict[str, str] = {
    SCROLL_MODE_KEY: "scroll_mode",
    SCROLL_ACCEL_KEY: "scroll_acceleration",
    SMART_REEL_KEY: "scroll_smart_reel",
    "device.keyswitch_optimization": "keyswitch_optimization",
    "device.profile_led_red": "lighting_profile_led_red",
    "device.profile_led_green": "lighting_profile_led_green",
    "device.profile_led_blue": "lighting_profile_led_blue",
}

#: The client's attribute is not always the capability name.
EXTRA_ATTRS: dict[str, str] = {
    "device.profile_led_red": "profile_led_red",
    "device.profile_led_green": "profile_led_green",
    "device.profile_led_blue": "profile_led_blue",
}

EXTRA_LABELS: dict[str, tuple[str, str, str]] = {
    SCROLL_MODE_KEY: ("Free-spin scroll wheel", "",
                      "Tactile clicks off, so the wheel spins freely."),
    SCROLL_ACCEL_KEY: ("Scroll acceleration", "", ""),
    SMART_REEL_KEY: ("Smart reel", "", ""),
    "device.keyswitch_optimization": (
        "Optimise key switches for gaming", "",
        "Trades a little typing feel for a faster actuation point.",
    ),
    "device.profile_led_red": ("Profile LED — red", "Profile indicators", ""),
    "device.profile_led_green": ("Profile LED — green", "Profile indicators", ""),
    "device.profile_led_blue": ("Profile LED — blue", "Profile indicators", ""),
}


class Effect:
    """One OpenRazer lighting effect and what it needs to be invoked.

    The argument lists are taken from the live API, not from documentation -- ``wave`` and
    ``reactive`` genuinely cannot be called without a direction and a speed, which is why those
    are controls rather than conveniences.
    """

    __slots__ = ("name", "label", "colours", "speed", "direction")

    def __init__(
        self, name: str, label: str, *, colours: int = 0, speed: bool = False,
        direction: bool = False,
    ) -> None:
        self.name = name
        self.label = label
        self.colours = colours
        self.speed = speed
        self.direction = direction


#: Declaration order is presentation order in the dropdown.
#: Every effect OpenRazer's client defines, with the exact argument list taken from its live
#: signatures. A device is offered only the ones it advertises, so this table being complete is
#: what makes an untested Razer get its real set rather than the subset two devices happened to
#: have.
EFFECTS: tuple[Effect, ...] = (
    Effect("none", "Off"),
    Effect("on", "On"),
    Effect("static", "Static", colours=1),
    Effect("blinking", "Blinking", colours=1),
    Effect("pulsate", "Pulsate", colours=1),
    Effect("spectrum", "Spectrum"),
    Effect("wave", "Wave", direction=True),
    Effect("wheel", "Wheel", direction=True),
    Effect("reactive", "Reactive", colours=1, speed=True),
    Effect("breath_single", "Breathing", colours=1),
    Effect("breath_dual", "Breathing (two colours)", colours=2),
    Effect("breath_triple", "Breathing (three colours)", colours=3),
    Effect("breath_mono", "Breathing (mono)"),
    Effect("breath_random", "Breathing (random)"),
    Effect("starlight_single", "Starlight", colours=1, speed=True),
    Effect("starlight_dual", "Starlight (two colours)", colours=2, speed=True),
    Effect("starlight_random", "Starlight (random)", speed=True),
    Effect("ripple", "Ripple", colours=1),
    Effect("ripple_random", "Ripple (random)"),
)
EFFECT_BY_NAME = {e.name: e for e in EFFECTS}

#: The capability name is not the attribute name. A DeathAdder V2 advertises ``lighting_scroll_*``
#: while the object is ``fx.misc.scroll_wheel`` -- deriving one from the other by concatenation
#: raises AttributeError on a device that fully supports the feature.
ZONE_ATTRS: dict[str, str] = {
    "logo": "logo",
    "scroll": "scroll_wheel",
    "left": "left",
    "right": "right",
    "backlight": "backlight",
    # Battery-state indicators on wireless hardware. Real zones with their own effects, present
    # on `fx.misc` alongside the others.
    "charging": "charging",
    "fast_charging": "fast_charging",
    "fully_charged": "fully_charged",
}
ZONE_LABELS: dict[str, str] = {
    "main": "All",
    "logo": "Logo",
    "scroll": "Scroll wheel",
    "left": "Left",
    "right": "Right",
    "backlight": "Backlight",
    "charging": "While charging",
    "fast_charging": "While fast charging",
    "fully_charged": "When fully charged",
}

#: OpenRazer's own constants, so the labels and the wire values cannot drift apart.
WAVE_DIRECTIONS: tuple[tuple[int, str], ...] = ((1, "Right"), (2, "Left"))
SPEEDS: tuple[tuple[int, str], ...] = (
    (1, "0.5 s"), (2, "1 s"), (3, "1.5 s"), (4, "2 s"),
)
#: What OpenRazer assumed before 3.2.0 exposed a per-device list, and what Polychromatic still
#: falls back to. **Not** the full constant range: offering 8000 Hz on a mouse whose ceiling is
#: 1000 is offering a setting that cannot work.
POLL_RATES_FALLBACK: tuple[int, ...] = (125, 500, 1000)

#: Every rate the client defines. Only ever used to label a value the device itself reported.
POLL_RATES_ALL: tuple[int, ...] = (125, 250, 500, 1000, 2000, 4000, 8000)

NOTE_LIGHTING = (
    "Built-in effects only. Per-key colours, layered effects and saved lighting profiles are an "
    "RGB editor rather than a setting — OpenRGB and Polychromatic do that job."
)
NOTE_BATTERY_UNVERIFIED = (
    "Written from the OpenRazer API; no wireless Razer device has been available to test it on."
)


def macro_delete_key(bind_key: str) -> str:
    return f"{MACRO_DELETE_PREFIX}{bind_key}"


def macro_bind_key(key: str) -> str:
    return key[len(MACRO_DELETE_PREFIX):] if key.startswith(MACRO_DELETE_PREFIX) else ""


def macros(
    bound: Sequence[tuple[str, int]], *, has_modifier: bool = False, has_led: bool = False,
    has_saved: bool = False,
) -> list[Capability]:
    """The Macros tab.

    *bound* is ``(key, number of recorded events)`` per macro the daemon holds.

    Deliberately has no Record button and no Test button. OpenRazer exposes exactly three macro
    calls to a client -- ``getMacros``, ``addMacro``, ``deleteMacro``. Recording is driven from the
    keyboard, and ``play_macro`` lives inside the daemon's key handling with no D-Bus endpoint, so
    a button for either would be a button that cannot work.
    """
    out: list[Capability] = [
        Capability(
            key=MACRO_STATUS_KEY,
            kind=Kind.READOUT,
            label="Recorded macros",
            group=GROUP_MACROS,
            writable=False,
            note=NOTE_MACROS,
        )
    ]
    if has_led:
        out.append(
            Capability(
                key=MACRO_LED_KEY, kind=Kind.TOGGLE, label="Macro key light",
                group=GROUP_MACROS, section="Behaviour",
            )
        )
    if has_modifier:
        out.append(
            Capability(
                key=MACRO_MODIFIER_KEY, kind=Kind.TOGGLE, label="Require FN for macro keys",
                group=GROUP_MACROS, section="Behaviour",
                description=(
                    "When on, a macro key only fires while FN is held, so the key keeps its "
                    "normal function otherwise."
                ),
            )
        )
    out += [
        Capability(
            key=macro_delete_key(bind),
            kind=Kind.ACTION,
            label=f"{bind} — {count} key{'s' if count != 1 else ''}",
            action_label="Delete",
            group=GROUP_MACROS,
            section="Recorded",
            confirm=True,
            confirm_detail=f"The macro on {bind} is removed. It cannot be recovered.",
        )
        for bind, count in bound
    ]
    # The daemon keeps macros in memory only -- its persistence file covers lighting zones, not
    # macros -- so they are lost when it stops or the machine reboots. Keeping a copy and feeding
    # it back is the one thing this application can add that OpenRazer does not do itself.
    out.append(
        Capability(
            key=MACRO_SAVE_KEY, kind=Kind.ACTION, label="Saved copy",
            action_label="Save macros to disk", group=GROUP_MACROS, section="Keeping them",
            note=(
                "The daemon holds macros in memory only, so they disappear when it stops or you "
                "reboot. Save them here and they can be put back."
            ),
        )
    )
    out.append(
        Capability(
            key=MACRO_RESTORE_KEY, kind=Kind.ACTION, label="Saved macros",
            action_label="Restore them to the keyboard", group=GROUP_MACROS,
            section="Keeping them", writable=has_saved,
            confirm=True,
            confirm_detail="Any macro currently on a saved key is replaced.",
        )
    )
    out.append(
        Capability(
            key=MACRO_AUTORESTORE_KEY, kind=Kind.TOGGLE, label="Restore on connect",
            group=GROUP_MACROS, section="Keeping them",
            description=(
                "Put the saved macros back automatically whenever this keyboard is opened. Off by "
                "default: it writes to the keyboard without asking, which should be your choice."
            ),
        )
    )
    out.append(
        Capability(
            key=MACRO_EXPORT_KEY, kind=Kind.ACTION, label="Export",
            action_label="Export to a file…", group=GROUP_MACROS, section="Moving them",
            file_dialog="save", file_filter=MACRO_FILTER, file_suffix=".json",
            writable=bool(bound),
            note=(
                "A plain JSON file holding the macros exactly as the daemon reports them, so it "
                "can be kept in a backup, copied to another machine, or edited by hand."
            ),
        )
    )
    out.append(
        Capability(
            key=MACRO_IMPORT_KEY, kind=Kind.ACTION, label="Import",
            action_label="Import from a file…", group=GROUP_MACROS, section="Moving them",
            file_dialog="open", file_filter=MACRO_FILTER,
            confirm=True,
            confirm_detail="Any macro currently on a key named in the file is replaced.",
        )
    )
    out.append(
        Capability(
            key=MACRO_REFRESH_KEY,
            kind=Kind.ACTION,
            label="List",
            action_label="Re-read from daemon",
            group=GROUP_MACROS,
            section="Recorded" if bound else "Behaviour",
            description="Macros recorded on the keyboard appear here after a re-read.",
        )
    )
    return out


def stage_key(index: int, axis: str) -> str:
    return f"{STAGE_PREFIX}{index}.{axis}"


def stage_of(key: str) -> tuple[int, str] | None:
    """``dpi.stage.2.x`` -> ``(2, "x")``."""
    if not key.startswith(STAGE_PREFIX):
        return None
    rest = key[len(STAGE_PREFIX):].split(".")
    if len(rest) != 2 or not rest[0].isdigit():
        return None
    return int(rest[0]), rest[1]


def default_dpi_stages(max_dpi: int) -> list[tuple[int, int]]:
    """The stages to offer before the user has saved any.

    Identical to Polychromatic's derivation, deliberately: a table for the maxima it knows, and
    otherwise ``max/10, max/8, max/4, max/2, max`` rounded to a valid step. A 20000 DPI mouse
    therefore gets 2000, 2500, 5000, 10000, 20000 in both applications.
    """
    listed = DEFAULT_STAGES.get(int(max_dpi))
    if listed is None:
        def snap(value: float) -> int:
            # Round to 100: the arithmetic can otherwise land on a DPI the sensor will not take.
            return max(100, round(value / 100) * 100)

        listed = (
            snap(max_dpi / 10), snap(max_dpi / 8), snap(max_dpi / 4), snap(max_dpi / 2),
            int(max_dpi),
        )
    return [(v, v) for v in listed]


def effect_key(zone: str) -> str:
    return f"light.{zone}.effect"


def colour_key(zone: str, index: int = 1) -> str:
    return f"light.{zone}.color" + ("" if index == 1 else str(index))


def speed_key(zone: str) -> str:
    return f"light.{zone}.speed"


def direction_key(zone: str) -> str:
    return f"light.{zone}.direction"


def brightness_key(zone: str) -> str:
    return f"light.{zone}.brightness"


def zone_of(key: str) -> str:
    """``light.logo.color`` -> ``logo``."""
    parts = key.split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "light" else ""


def effects_taking(n: int) -> tuple[str, ...]:
    """Effect names that take at least *n* colours -- the gate for a colour control."""
    return tuple(e.name for e in EFFECTS if e.colours >= n)


def build(
    *,
    zones: Sequence[tuple[str, Sequence[str], bool]] = (),
    dpi_max: int | None = None,
    dpi_fixed: Sequence[int] = (),
    dpi_stages: Sequence[tuple[int, int]] = (),
    poll_rates: Sequence[int] = (),
    game_mode: bool = False,
    macro_led: bool = False,
    battery: bool = False,
    stages: Sequence[tuple[int, int]] = (),
    can_sync: bool = False,
    extra: Sequence[str] = (),
    identity: Sequence[tuple[str, str]] = (),
) -> CapabilitySet:
    """Assemble a page.

    *zones* is ``(zone, supported effect names, has brightness)`` per lighting zone, already
    filtered by ``has()`` -- this function never talks to OpenRazer.
    """
    out: list[Capability] = []
    out += _info(identity, battery)
    out += _device(dpi_max, dpi_fixed, dpi_stages, poll_rates, game_mode, macro_led, battery,
                   extra)
    out += _dpi_stages(dpi_max, stages, can_sync)
    for zone, effects, has_brightness in zones:
        out += _zone(zone, effects, has_brightness)
    return CapabilitySet(out)


def _dpi_stages(
    dpi_max: int | None, stages: Sequence[tuple[int, int]], can_sync: bool
) -> list[Capability]:
    """Saved DPI stages, each with its own horizontal and vertical value.

    These are *ours*, kept per device, exactly as Polychromatic keeps its own list -- most mice,
    including a DeathAdder V2, store no stages in hardware at all. Where a mouse does
    (``dpi_stages``), Sync writes them onto the device so its own DPI buttons cycle them.
    """
    if not dpi_max or not stages:
        return []
    out: list[Capability] = []
    # A stage's current values are state, not schema: they arrive through the value map.
    for index in range(1, len(stages) + 1):
        for axis, label in (("x", "Horizontal"), ("y", "Vertical")):
            out.append(
                Capability(
                    key=stage_key(index, axis),
                    kind=Kind.RANGE,
                    label=label,
                    group=GROUP_DPI,
                    section=f"Stage {index}",
                    minimum=100,
                    maximum=dpi_max,
                    step=100,
                    writes_with=(stage_key(index, "x"), stage_key(index, "y")),
                )
            )
    out.append(
        Capability(
            key=STAGE_APPLY_KEY,
            kind=Kind.CHOICE,
            label="Use stage",
            group=GROUP_DPI,
            section="Apply",
            choices=tuple(
                Choice(i, f"Stage {i} — {x}" if x == y else f"Stage {i} — {x} × {y}")
                for i, (x, y) in enumerate(stages, start=1)
            ),
            description="Sets the mouse's current DPI to this stage.",
        )
    )
    out.append(
        Capability(
            key=STAGE_SYNC_KEY,
            kind=Kind.ACTION,
            label="Mouse DPI buttons",
            action_label="Send stages to the mouse",
            group=GROUP_DPI,
            section="Apply",
            writable=can_sync,
            note=(
                "Sends these stages to the mouse so its own DPI buttons cycle through them."
                if can_sync
                else "This mouse cannot store DPI stages, so its DPI buttons cannot be changed. "
                "The stages above are kept here and applied when you pick one."
            ),
        )
    )
    return out


def _info(identity: Sequence[tuple[str, str]], battery: bool) -> list[Capability]:
    out: list[Capability] = []
    if battery:
        out.append(
            Capability(
                key=BATTERY_KEY, kind=Kind.METER, label="Battery", group=GROUP_INFO,
                minimum=0, maximum=100, unit="%", writable=False, experimental=True,
                note=NOTE_BATTERY_UNVERIFIED,
            )
        )
        out.append(
            Capability(
                key=CHARGING_KEY, kind=Kind.READOUT, label="Charging", group=GROUP_INFO,
                writable=False, experimental=True,
            )
        )
    out += [
        Capability(
            key=f"info.{field}", kind=Kind.READOUT, label=label, group=GROUP_INFO,
            section="Identity", writable=False,
        )
        for field, label in identity
    ]
    return out


def _device(
    dpi_max: int | None,
    dpi_fixed: Sequence[int],
    dpi_stages: Sequence[tuple[int, int]],
    poll_rates: Sequence[int],
    game_mode: bool,
    macro_led: bool,
    battery: bool,
    extra: Sequence[str],
) -> list[Capability]:
    out: list[Capability] = []
    if dpi_fixed:
        # Some devices expose a fixed list instead of a free range; the two are mutually
        # exclusive, and Polychromatic branches the same way.
        out.append(
            Capability(
                key=DPI_KEY, kind=Kind.CHOICE, label="DPI", group=GROUP_DEVICE,
                choices=tuple(Choice(v, f"{v}") for v in dpi_fixed),
            )
        )
    elif dpi_max:
        # OpenRazer stores DPI as an (x, y) pair and both axes are independently settable, so both
        # are offered. They live in one property, hence writes_with: writing either re-sends the
        # pair, and holding only the touched one would write the other from stale state.
        # Locked by default, and Polychromatic does the same: dragging one axis alone from a
        # low value leaves the pointer barely controllable in that direction, which is a nasty
        # thing to do to someone mid-drag.
        out.append(
            Capability(
                key=DPI_LOCK_KEY, kind=Kind.TOGGLE, label="Lock horizontal and vertical",
                group=GROUP_DEVICE,
                description=(
                    "Keeps the two axes equal: moving either slider moves the other with it. "
                    "Unlock only if you deliberately want a different sensitivity per axis."
                ),
            )
        )
        out.append(
            Capability(
                key=DPI_X_KEY, kind=Kind.RANGE, label="DPI (horizontal)", group=GROUP_DEVICE,
                minimum=100, maximum=dpi_max, step=100,
                writes_with=(DPI_X_KEY, DPI_Y_KEY),
            )
        )
        out.append(
            Capability(
                key=DPI_Y_KEY, kind=Kind.RANGE, label="DPI (vertical)", group=GROUP_DEVICE,
                minimum=100, maximum=dpi_max, step=100,
                writes_with=(DPI_X_KEY, DPI_Y_KEY),
                description=(
                    "Set the same as horizontal unless you deliberately want a different "
                    "sensitivity per axis."
                ),
            )
        )
    if dpi_stages:
        out.append(
            Capability(
                key=DPI_STAGE_KEY, kind=Kind.CHOICE, label="Active DPI stage",
                group=GROUP_DEVICE,
                choices=tuple(
                    Choice(i + 1, f"Stage {i + 1} — {x} × {y}")
                    for i, (x, y) in enumerate(dpi_stages)
                ),
                experimental=True,
                note=(
                    "Stages stored on the mouse itself, cycled with its DPI button. No device "
                    "here reports the dpi_stages capability, so this is untested."
                ),
            )
        )
    if poll_rates:
        out.append(
            Capability(
                key=POLL_RATE_KEY, kind=Kind.CHOICE, label="Polling rate", group=GROUP_DEVICE,
                choices=tuple(Choice(v, f"{v} Hz") for v in poll_rates),
            )
        )
    if game_mode:
        out.append(
            Capability(
                key=GAME_MODE_KEY, kind=Kind.TOGGLE, label="Game mode", group=GROUP_DEVICE,
                description="Disables the Super key and other shortcuts that interrupt a game.",
            )
        )
    # Declared in EXTRA_LABELS order so the profile LEDs stay contiguous under their section.
    for key, (label, section, note) in EXTRA_LABELS.items():
        if key in extra:
            out.append(
                Capability(key=key, kind=Kind.TOGGLE, label=label, group=GROUP_DEVICE,
                           section=section, description=note)
            )
    if macro_led:
        out.append(
            Capability(
                key=MACRO_LED_KEY, kind=Kind.TOGGLE, label="Macro key light",
                group=GROUP_DEVICE,
            )
        )
    if battery:
        out.append(
            Capability(
                key=IDLE_TIME_KEY, kind=Kind.RANGE, label="Sleep after", group=GROUP_DEVICE,
                section="Power", minimum=1, maximum=15, step=1, unit="min", experimental=True,
            )
        )
        out.append(
            Capability(
                key=LOW_BATTERY_KEY, kind=Kind.RANGE, label="Low battery warning",
                group=GROUP_DEVICE, section="Power", minimum=5, maximum=50, step=5, unit="%",
                experimental=True, note=NOTE_BATTERY_UNVERIFIED,
            )
        )
    return out


def _zone(zone: str, effects: Sequence[str], has_brightness: bool) -> list[Capability]:
    """One lighting zone: effect, its arguments, and brightness.

    Every argument control is gated on the selected effect actually taking it, so a colour button
    never appears for Spectrum and a direction never appears for anything but Wave.
    """
    out: list[Capability] = []
    section = ZONE_LABELS.get(zone, zone.replace("_", " ").title())
    available = [EFFECT_BY_NAME[n] for n in effects if n in EFFECT_BY_NAME]
    ordered = [e for e in EFFECTS if e in available]

    if ordered:
        out.append(
            Capability(
                key=effect_key(zone), kind=Kind.CHOICE, label="Effect", group=GROUP_LIGHTING,
                section=section,
                choices=tuple(Choice(e.name, e.label) for e in ordered),
                note=NOTE_LIGHTING if zone in ("main", "logo") else "",
            )
        )
        for index in (1, 2, 3):
            takers = tuple(e.name for e in ordered if e.colours >= index)
            if takers:
                out.append(
                    Capability(
                        key=colour_key(zone, index),
                        kind=Kind.COLOR,
                        label={1: "Colour", 2: "Second colour", 3: "Third colour"}[index],
                        group=GROUP_LIGHTING,
                        section=section,
                        requires=effect_key(zone),
                        requires_value=takers,
                    )
                )
        if any(e.speed for e in ordered):
            out.append(
                Capability(
                    key=speed_key(zone), kind=Kind.CHOICE, label="Speed",
                    group=GROUP_LIGHTING, section=section,
                    choices=tuple(Choice(v, label) for v, label in SPEEDS),
                    requires=effect_key(zone),
                    requires_value=tuple(e.name for e in ordered if e.speed),
                )
            )
        if any(e.direction for e in ordered):
            out.append(
                Capability(
                    key=direction_key(zone), kind=Kind.CHOICE, label="Direction",
                    group=GROUP_LIGHTING, section=section,
                    choices=tuple(Choice(v, label) for v, label in WAVE_DIRECTIONS),
                    requires=effect_key(zone),
                    requires_value=tuple(e.name for e in ordered if e.direction),
                )
            )
    if has_brightness:
        out.append(
            Capability(
                key=brightness_key(zone), kind=Kind.RANGE, label="Brightness",
                group=GROUP_LIGHTING, section=section,
                minimum=0, maximum=100, step=1, unit="%",
            )
        )
    return out
