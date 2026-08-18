# Logitech HID++ — behavioural specification

What this module does, why, and what it deliberately refuses to do. The protocol, the per-model
feature tables and every setting definition are **Solaar's**, vendored under
`hardware_ui/third_party` by `tools/vendor_solaar.py`. This module is the adapter and nothing else.

## 0. What makes this module different from the others

Every other module in this project either implements a protocol (Sony, Poly, Jabra) or drives a
daemon (Razer, via OpenRazer). This one links a **library**, and one that was not written to be
linked headlessly.

That single fact shapes the whole design: the vendoring, the `sys.path` discipline, the redirected
config file, and the four setting kinds that are deliberately not shown.

## 1. Why the library is vendored rather than depended on

`app-misc/solaar` is not split. Installing it pulls **GTK3, pygobject and python-xlib**, plus a
tray application and its autostart, for a library this Qt application only calls. Measured against
Solaar 1.1.19, exactly one file of the 22 in `logitech_receiver` hard-requires that stack:

| File | GUI import | Guarded? |
|---|---|---|
| `diversion.py` | `gi`, `Gdk`, `GLib`, `Xlib`, `psutil`, `evdev`, `keysyms` | **No** — bare, at module scope |
| `desktop_notifications.py` | `Gtk`, `Notify` | **Looks** guarded, is not — see below |
| `base.py` | `GLib` | Yes — under `typing.TYPE_CHECKING`, never runs |

`diversion.py` is the key-remapping rule engine. It is not device configuration, and it reaches the
settings shallowly: of 68 setting classes in `settings_templates.py` it is used in **one** place (a
mouse-gesture notification), with `desktop_notifications.show` in one more (a DPI-slide toast).
Both call sites are patched out. What remains needs only `pyudev` (already used here, for hotplug),
`PyYAML` and the bundled MIT `hid_parser`.

⚠️ **`desktop_notifications.py` is a trap.** Its `try` catches only `ValueError` — which is what
`gi.require_version` raises for a missing *typelib*. A missing pygobject raises `ImportError`,
which sails straight through. It looks defensive and is not, so it is dropped rather than carried.

**Solaar's own CLI has the same problem**: `solaar config`, a command-line tool, cannot import
without pygobject. That is worth knowing before assuming the headless path is supported upstream.

**Licensing.** GPL-2.0-**or-later**, established from Solaar's README and the per-file headers —
not from GitHub's badge or the Gentoo ebuild, both of which say plain GPL-2 and are artefacts of
matching the LICENSE *text*, which is identical for "only" and "or later". "Or later" is what
permits combining it into this GPL-3.0-or-later application. `LICENSE`, `COPYRIGHT` and every
per-file header travel with the copy; `PROVENANCE.md` records the release, its sha256 and every
patch.

**Re-syncing is a re-run, not a merge.** Bump `VERSION` in `tools/vendor_solaar.py` and run it. The
patch set is applied by name and **fails loudly** if upstream moved a line, which is the whole
point — a silently skipped patch would leave a GTK import in the tree and only fail on a user's
machine.

## 2. Import discipline — two traps, both structural

**The same module imported twice.** The library uses absolute imports (`from solaar import
configuration`), so its directory goes on `sys.path` rather than being reached as
`hardware_ui.third_party.solaar`. Reaching it both ways yields two module objects with separate
globals — and the config redirect would then apply to a copy nobody reads, with the symptom being
settings that silently fail to persist. Everything goes through `bootstrap.vendored()`.

**A real Solaar installed alongside.** Its `solaar` package is importable too. Ours is inserted at
the *front* of `sys.path` deliberately: the vendored copy is patched, and an unpatched one would
pull GTK in and fail differently on every machine. `bootstrap.loaded_from_vendor()` can answer
which one won, because every symptom of getting the wrong one is confusing.

## 3. The config file is ours, not Solaar's

Most HID++ settings do not survive a reconnect in hardware — the device forgets and the host writes
them back. `logitech_receiver` keeps that record, and upstream stores it at
`~/.config/solaar/config.yaml`.

This module **redirects it** to `~/.config/hardware-ui/logitech.yaml`. Writing into another
application's configuration is not ours to do: a user who installs Solaar later would find entries
it never wrote, and two processes with no locking between them would take turns clobbering each
other.

The cost is stated rather than hidden: **run both and each keeps its own idea of your settings.**
The hardware is the tiebreaker — whichever wrote last wins, and a read shows what the device holds.

Both the YAML *and* the legacy JSON path are redirected. Leaving the JSON one pointed at Solaar's
directory would let a stray `config.json` there be picked up, quietly, and only on machines that
happen to have one.

## 4. One entry per device, and the receiver too

⚠️ **This section originally claimed the layout came free from `hid-logitech-dj`. That was wrong**,
and a kernel was rebuilt on the strength of it. The claim came from seeing `find_paired_node` exist
in Solaar, not from checking which receivers the kernel actually creates nodes for.

What is true: `hid-logitech-dj` creates a child node per paired device **for the receivers in its
device table** — Unifying `c52b`, the Lightspeed and Nano families. A **Logi Bolt** receiver is not
in that table, in any kernel version. `046d:c548` appears in mainline only in `hid-quirks.c`, as
`HID_QUIRK_ALWAYS_POLL`. Bolt is fully HID-compliant and pairs at hardware level, so it never
needed the DJ protocol that driver implements, and there is no kernel to upgrade to.

So the module gets its layout from two places, and asks the kernel first:

```
for each paired slot:
    find_paired_node(receiver, slot)  →  a node exists?  leave it to enumeration
                                      →  nothing?        emit a child (children.py)
```

On a Bolt receiver every slot falls through to `children.py`. On a Unifying receiver none do and the
expander is a no-op. Same sidebar either way, no duplicates, and no dependence on the kernel — and
if `c548` ever gains a driver entry, the expander stops firing for it by itself.

`children.py` is the one place the fast-discovery rule bends. It reads the receiver's *pairing
registers*, which took ~100 ms and answered even for a keyboard that would not reply to a ping,
because the receiver stores each paired device's name, kind and serial. Nothing is opened, woken or
probed. Walking the slots costs 2.13 s on a Bolt receiver — the library asks all six and the four
empty ones time out — so the result is cached against `count()`, which costs 4 ms and moves the
moment something is paired or unpaired.

**Resolution.** A child carries its slot, and that is checked *first* — its `path` is the
receiver's, so matching by path would return the receiver instead. A device that does have its own
node is matched by `find_paired_node`, and only then does a same-USB-device fallback apply, because
discovery hands over whichever node it picked and that is rarely the HID++ one (measured:
`/dev/hidraw1` offered, HID++ on `/dev/hidraw3`).

## 4a. Per-key maps, and the line between hardware and software

A `MAP_CHOICE` carries a value **per key** — "each button does this". Small maps expand to one
`CHOICE` row per key, under a section named after the setting; the row-per-key idea is the same one
the Jabra module uses for object-valued properties. Above `MAP_MAX_KEYS` (12) they are withheld and
logged, because a 17- or 117-row wall of dropdowns is worse than none and this window has already
been pushed off a screen once by page length.

The test is the *device's* map, never `keys_universe` — that is the 327 control ids Logitech defines
in total, not the handful any one device has. Measured: an MX Master 3S maps 7 buttons,
`dpi_extended` has 3 keys (X, Y, LOD), an MX KEYS S diverts 17, `per-key-lighting` can reach 117.

Writes go through `write_key_value`, so only the changed key is sent. Writing the whole map back
would rewrite every other button to whatever the page last read — which is how a stale row silently
undoes a change made elsewhere.

**`divert-keys` and `gesture2-divert` are read-only**, and this is the boundary of what this
application is. Diversion does not configure anything: it tells a key to stay quiet so a resident
program can decide what it should do instead. Solaar ships that program — a rule engine matching
conditions and firing `KeyPress`, `MouseClick`, `MouseScroll` or `Execute`. Without it, "Diverted",
"Mouse Gestures" and "Sliding DPI" all leave the key doing nothing at all, so three of four values
would break a physical button.

They are shown rather than hidden because the state is worth knowing — if Solaar has diverted a key,
this page should say so rather than implying the key is untouched — and every row carries the note,
not just the first. See `PROJECT_STATE.md` §5b for what building the engine would involve.

**Sliding DPI is not DPI stages.** It is a host state machine that walks the single `dpi` value and
remembers the other one in *Solaar's* config. Contrast Razer, where a DeathAdder Elite has five
genuine hardware stages. Logitech mice that store stages do it in `onboard_profiles`.

## 4b. What it costs, and where

Worth stating plainly because it is the module's one user-visible cost:

| | |
|---|---|
| Every discovery transport combined | 0.02 s |
| First expansion of a receiver | **2.28 s**, of which 2.13 s is the slot walk |
| ``count()``, the cache key | 0.004 s |
| Later rescans | 0.04 s |

The library iterates slots 1–6 and constructs a Device for each; the four empty ones time out at
roughly half a second apiece. Nothing here is our own inefficiency and nothing is avoidable without
patching upstream — which the vendoring makes possible (``tools/vendor_solaar.py`` re-applies a
named patch set and fails loudly if upstream moves), but a patch is a maintenance liability and this
one has not earned its place.

What *is* done: the result is memoised against ``(receiver serial, connected count)``, so the walk
happens once per session and again only when something is paired or unpaired; and children are
persisted in the discovery cache, so a warm start paints them before any of this runs.

Three cheaper options exist if it ever matters more — asking the receiver which slots are occupied
rather than walking all six, making the expansion non-blocking so the list settles around it, or
shortening the library's per-request timeout. The last is the crude one and risks calling a
slow-but-present device absent, which is the trap the Jabra probe timeout already taught.

## 4c. On-board profiles are announced, not fought

A G-series mouse can store a profile on the device that, in Solaar's own words, "controls report
rate, sensitivity and button actions". While one is active, a live write to those settings may
simply be ignored: the write is accepted, the read-back disagrees, and the control looks like it
reverted on its own. That is indistinguishable from a bug in this application unless the page says
which — and upstream says so too, in `AdjustableDpi`'s description: *"May need Onboard Profiles set
to Disable to be effective."*

So when a device advertises `ONBOARD_PROFILES` (0x8100) **and** profile mode is on, every setting a
profile governs — `dpi`, `dpi_extended`, `report_rate`, `report_rate_extended`, the two
reprogrammable-key maps — carries an advisory saying so. Nothing is hidden and nothing is blocked;
the write still goes out, because the device is the authority on whether it takes.

Detection is two questions, both Solaar's: `OnboardProfiles.get_profile_headers()` for whether the
feature is there at all, and function `0x20` answering `0x01` for whether it is switched on. A
device without the feature never reaches the second question, which is every device tested here —
so the cost on tested hardware is one failed feature lookup at describe time.

**The control itself is not offered.** Switching profile mode calls `profile_change()` and moves
device state wholesale, and no G-series hardware exists here to run it against. Shipping a write
path nobody has executed is how a mouse ends up in a state its owner cannot undo. `PROJECT_STATE.md`
§5e tracks it.

## 4d. Bluetooth: two rows for one mouse, and which one wins

A mouse paired directly over Bluetooth — no receiver — appears **twice**, because two enumerators
find it independently and neither knows about the other:

| row | what it is | claimed by |
|---|---|---|
| hidraw | the node HID++ actually flows over | the `transport = "hid"` rules |
| BlueZ | the paired device BlueZ remembers | the `transport = "bluetooth"` rule |

`discovery._one_row_per_device` drops the BlueZ duplicate whenever the hidraw node exists, matching
on the address — hidraw reports it as `HID_UNIQ`, BlueZ in a different case. So in practice the
hidraw row is what you see and configure.

**The Bluetooth rule exists for the switched-off case.** Turn the mouse off and the kernel tears
down its hidraw node; only the BlueZ row survives. Without a rule to claim it that row is never
rendered, and the mouse does not move to "Disconnected devices" — it disappears from the list
altogether. Sony and Poly never had this because they match Bluetooth in the first place.

The rule is scoped to `00010000-0000-1000-8000-011f2000046d`, Logitech's own vendor GATT service,
whose last four hex digits are their vendor id. As specific as a vendor id, advertised by the device
itself, and not carried by a Logitech Bluetooth speaker — so it cannot claim something this module
has no business with. `family`, because a row that only appears while the device is unreachable is
not one anyone can test against.

Classification comes from the device rather than from its name. `_hid_kind` reads the USB interface's
protocol byte and a Bluetooth device has no USB interface, so the mouse used to get no kind at all
and its category fell back to whether the name contained the word "mouse" — which "Logitech MX
Master 3S" does not. The HID report descriptor answers properly, and BlueZ supplies its own icon
hint for its rows.

## 5. Four setting kinds are not shown

A setting's validator says what shape its value is. Four map onto controls; four describe a value
**per key** and need a table rather than a row:

| Solaar kind | Shown as |
|---|---|
| `TOGGLE` | `Kind.TOGGLE` |
| `CHOICE` | `Kind.CHOICE` |
| `RANGE`, `PACKED_RANGE` | `Kind.RANGE` |
| `MAP_CHOICE` | **one row per key** when the device's map is small — see §4a |
| `MULTIPLE_TOGGLE`, `MULTIPLE_RANGE`, `HETERO` | **not offered** |

Flattening a per-key map into one row would write the wrong thing to the device. They are logged at
connect so it is visible what a given mouse has that this page does not show yet.

**Choices carry the int, not the `NamedInt`.** Solaar's choices are integers that print as names.
Carried through as-is, the value would stop comparing equal to the plain int a control hands back.

## 6. Wording is Solaar's; grouping is ours

Every setting has a `label` and usually a `description`, written for users and translated upstream
— "Sensitivity (DPI)", "Scroll Wheel Ratchet Speed". Regenerating those from internal names would
be strictly worse and would drift from what every other Linux tool calls the same setting.

Solaar's own window shows one flat list, which is fine for a small dialog and poor for a tabbed
page, so the sections here are this project's: Pointer, Scrolling, Keys & Buttons, Lighting, Sound
& Haptics, Power, Device. Keyword rules in order, so a setting added by a future Solaar lands
somewhere sensible rather than vanishing.

## 7. Pairing

Ported from Solaar's `cli/pair.py`. Two details there are load-bearing and easy to lose: the
WIRELESS notification flag must be on for the receiver to report the new device, and it is
**restored afterwards only if it was off to begin with** — clearing a flag a concurrently running
Solaar had set would stop that one working.

**Unpairing asks first**, and says why: unpairing the keyboard you are typing on leaves you without
one. **Pair is not offered when every slot is full**, because a button that cannot succeed is worse
than no button.

**Bolt pairing needed a channel that did not exist.** A Bolt receiver authenticates before it will
pair: it produces a passkey that has to be entered **on the device being paired**, while the lock is
still open. An action button that runs to completion cannot show that, so
`hardware_ui.core.interaction` was added — a deliberately narrow protocol letting a module replace
one line of text and ask whether the user cancelled, in the same spirit as `AcquireUI` for vendor
imports. The shell implements it as a modeless dialog; the CLI and the tests get a null one.

The passkey takes two forms, and the difference is bit 0 of `authentication`: a keyboard types
digits and presses Enter, while a mouse is given the same number as a **left/right click pattern**
— ten bits, most significant first — because it has no digits to type. That encoding is upstream's
and is not something to guess at.

Threading is the trap. `Device.set` runs under `asyncio.to_thread`, so a module calls the
interaction from a worker while Qt demands the GUI thread, and every call is therefore a signal
with an explicitly queued connection. Cancellation is advisory: the loop notices between reads, so
nothing is interrupted mid-write — a half-finished pairing is worse than a slow one.

## 8. Vendor gating

Matching is on **USB vendor `0x046D`** over `hid` only. Nothing here can claim another maker's
device, and a Bluetooth-only Logitech peripheral is left unclaimed rather than offered a page that
cannot open.

## 9. Status

**Verified on hardware 2026-08-11**, on a Logi Bolt receiver (`046d:c548`) with an MX Master 3S and
an MX KEYS S paired to it:

- the receiver resolves, reports its slots (2 of 6) and lists both devices by name
- both peripherals appear as their own sidebar entries with the right icons, via `children.py`
- the mouse produces **nine controls** from its own feature set — DPI, ratchet speed, scroll and
  thumb wheel, Change Host — and **writes work**, confirmed by the user toggling scroll-wheel
  settings through the application
- battery reads (60%), and an asleep peripheral says so instead of showing an empty page

Also verified: **per-key maps in both directions** — the mouse's Mouse Gesture Button (control 195)
remapped to Left Click, tested, and restored to its factory value, each write sent with
``write_key_value`` so only that control moved; and the keyboard's ``Disable keys`` expanded to five
switches (Caps Lock, Num Lock, Scroll Lock, Insert, Win).

**Still unverified:** Bolt pairing end to end (the flow is ported and the passkey formatting is
tested, but no device has been paired through it), unpairing, and the MX KEYS S's own settings —
it drops to standby quickly and has not been awake while connected.

**On standby.** The only related setting Solaar offers is `adc_power_management`, "Power off in
minutes (0 for never)", on the `ADC MEASUREMENT` feature — it appears under **Power** if the device
advertises it. That controls auto power-*off*; the radio idle that makes a keyboard stop answering
pings has no HID++ knob, so it may not be the same thing.
