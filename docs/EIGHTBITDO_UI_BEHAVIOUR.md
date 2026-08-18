# 8BitDo Xbox controllers — behavioural specification

What this module does and why, written from `8bitdo-cfg`, which reverse-engineered the protocol
against an Ultimate Wired Controller for Xbox. This port has since been **verified on that hardware
over USB** — read, remap, save and read-back — which is where it stopped agreeing with its source:
see §2, the one thing `8bitdo-cfg`'s USB backend gets wrong and its own header warns about.

## 0. What makes this module different

Two things, and both shape everything below.

**It is one record, not a set of settings.** The controller stores its whole configuration as a
532-byte block with a single checksum. There is no such thing as writing one setting: every change
is a read-modify-write of the lot, followed by a commit. So the record is held in memory from
connect, a change patches the held copy, and the whole thing goes back.

**There are two ways in, and they are not equivalent.** These are *wired* controllers with a
Bluetooth radio inside, which exists because the vendor's configurator is an Android app. Holding
the config button makes the controller advertise as `82CE`. Both paths carry identical inner
messages, and the same save session; the difference is that only USB can read the record back. §3.

## 1. The record

```
[crc:2][curslot:1][mode:1]  then three 176-byte slots
```

Each slot begins with the marker `11 09 20 20`, which also recurs inside as a section separator,
and ends `55 aa`. Per-slot offsets shift by 176 per profile.

**Three slot states, not two.** Factory-fresh reads as all `0xff`; **deleted** reads as a zeroed
marker with *the old profile's bytes still in it*; written carries the marker. Deleting only zeroes
the four marker bytes, so anything deciding "is this a profile?" by looking for data would resurrect
a profile the user deleted. Tested against real dumps of all three states.

**Slots are edited in place**, never parsed and re-serialised. The record contains bytes nobody has
decoded, and a round-trip through a parsed form would zero them.

### The paddle offsets, and a bug found during the port

The source had `PADDLE_R` at 116 in one file and 124 in another. The captured record settles it: in
a profile documented as "paddles mapped to LT/RT", offset 116 holds `0x8000` (RT) and 120 holds
`0x4000` (LT), while **124 is the second copy of the record marker**. Writing a paddle at 124 would
have corrupted the record. 116/120 is right; the other was a latent bug, now fixed upstream too.

## 2. A save is a session, not a record — and this is what took longest

Sending the twelve or thirteen record chunks is **not a save**. The controller accepts all 532
bytes, reads them back correctly for as long as it stays powered, and comes back with the old
configuration at the next plug-in. That is the worst possible failure shape: it looks exactly like
success, from the page and from a read-back alike.

The vendor app's captured save session has six parts, and the order is theirs:

| | |
|---|---|
| `0x000B` control | opens a **save** session — offset `0x3434`, payload `aa 00 00 00` |
| `0x0007` setReportEnable | field `0x0501` |
| `0x0003` calibration | twelve bytes, replayed as captured |
| `0x0002` | a two-byte read the app makes here |
| `0x0001` × N | the record |
| `0x0006` finalize | field `0x005B` — **this is the commit** |

Both transports send exactly this, from one shared builder, because the inner bytes are
transport-independent and two copies would drift.

**Two traps, both of which this module fell into.** The finalize was named for what it looked like
— session bookkeeping — and left unsent. And the control packet is easy to mis-split: the request
header is seventeen bytes, so in `04 0b 00 | 0000 | 04000000 | 04000000 | 34340000 | aa000000` the
constant `34 34 00 00` sits in the **offset** field and the payload is the four bytes after it.
Read the other way round it never sets save mode.

`8bitdo-cfg`'s USB backend has both errors, which is why its own header says the USB path was
never validated against hardware. Its **Bluetooth** path replays the whole captured session, which
is why that one has always worked.

## 2a. The checksum is a chain from a fixed seed, not from the device

```
new = MCRF4XX(seed_lo, seed_hi, record[2:176])      # seed = 0xb6fb, every time
```

Reproduces four consecutive saves captured from the vendor app — `0x3b6b → 0x43a0 → 0x8781 →
0xa9fd` — so the *function* is certain. What was wrong for a long time here was the input.

Reading the controller's own header and chaining from that is the obvious design and it does not
work. Three consecutive reads of a real controller, with no write between them, returned headers
`fb 46 00 ff`, `a3 58 00 ff` and `79 02 00 ff`. Whatever those two bytes are, they are not a
checksum of the configuration, so chaining from them is chaining from noise.

The two non-checksum header bytes are replayed too, as `01 ff`. Measured to be safe: send `01` for
`curslot` and the controller reads back `00`, so it writes its own and a replayed value cannot move
the active profile.

One consequence worth having: nothing about a write depends on having read the device, so a
controller can be configured over Bluetooth without ever having been plugged in. An earlier version
refused that, on a premise that turned out to be false.

## 3. Why USB is still preferred

| | USB (GIP) | BLE (82CE) |
|---|---|---|
| Read | the whole 532-byte record | three 176-byte slots, one at a time |
| Header | read directly | not readable — rebuilt from the seed |
| Needs | a cable | the config button held, and a scan |
| Verified on hardware | **yes**, read and save | not yet through this application |

## 3a. BlueZ genuinely cannot do the BLE path

The controller **refuses the notify CCCD write** — the vendor's own Android code has an
`onNotifyFailure` handler that expects the refusal and carries on — and then notifies anyway. BlueZ
treats the refused write as fatal, so `StartNotify` and `AcquireNotify` both deliver nothing. A raw
ATT client on the L2CAP fixed channel receives Handle-Value-Notifications regardless, which is
effectively what Android does.

This is why the BLE transport opens an L2CAP socket itself instead of using BlueZ's GATT API.
Anyone tempted to "fix" that should try BlueZ first.

## 3b. Editing is local; **Sync** writes

Nothing reaches the controller until the Sync button. The configurator this module is ported from
works the same way, and the reason is the record: it is one indivisible block with one checksum, so
every change is a write of all 532 bytes. Saving per change turned remapping four buttons into four
full sessions — four kernel-driver detaches, four commits, four one-second gaps where the pad stops
being a gamepad — to express one intent. It also left a failed save with nothing to retry.

The button is on **every** tab, and an unsaved-changes advisory appears on every tab until it is
pressed. A write marks the capability `exclusive`, which disables the whole page while it runs: the
record is assembled from what is on screen, so a dropdown changed mid-write would produce a
controller holding neither configuration in full.

## 4. Discovery

GIP is a vendor-specific interface (`0xFF/0x47/0xD0`) and these controllers expose **no hidraw at
all**, so `enumerate_usb()` had to learn a second signature — matched as the full triple, because
class `0xFF` alone is what every dongle falls back to. A GIP device is filed under INPUT with a
gamepad icon: nothing else speaks GIP, so the interface says what the device is.

**The BLE side is not enumerated at startup.** A four-second LE scan is two orders of magnitude over
discovery's budget, and a controller only advertises while its config button is held — so there is
nothing to find unless the user has just done that. It is an action, like Logitech's pairing scan.

## 5. The page

47 rows: a profile selector, 19 remaps (17 inputs plus two paddles), 11 toggles, 8 sliders, reset
and delete, plus a copy of the Sync button on each of the five tabs.

**The profile selector chooses what is being *edited*, not what is *active*.** Which profile is live
is picked with the controller's own button, and moving it from software would change the device
under whoever is holding it. So the active profile is a readout, `SuperConfig` has no setter for it,
and a test asserts that.

**Switching profiles writes nothing.** It is a view control; saving there would burn a checksum step
for no change.

**Profile 1 cannot be deleted** — it is the base profile and deleting it leaves nothing to fall back
on. Reset is offered instead. Reset clones a profile the controller already has rather than
synthesising one, so nothing is invented that the device has not seen.

An empty slot makes every editing row unwritable, leaving the selector and the create action live
so there is a way out. Greyed sliders over a profile that does not exist read as breakage — and so
does a greyed page with no explanation, so the advisory appears on **every** tab, not only the one
carrying the button. The shell shows a tab the message belonging to the first of its own rows that
has one, so a single advisory on the Profile tab left four tabs silent.

The same action reads **"Create profile"** over an empty slot and **"Reset to default"** over a
written one, as the source configurator does. A button offering to reset something that does not
exist is most of why an empty profile read as breakage rather than as an empty profile.

## 6. Saving interrupts the gamepad

Over USB the configuration channel *is* the input interface, so claiming it stops input for about a
second and detaches `xpad`, which is put back afterwards. The page says so up front. Sliders commit
on release rather than per pixel — the shell already does this — so a drag is one save, not fifty.

## 7. Three drawings, because a controller has more than one side

The artwork is ours: `assets/controller-{front,top,back}.svg`, drawn for this project because
8BitDo's own renders cannot be redistributed. About 25 kB of line art against roughly 2 MB of vendor
PNGs, and it works on a dark theme, which theirs does not.

**One view was the original mistake, and it failed three ways.** From the face, LB and RB are a thin
sliver over the top edge and LT and RT are invisible behind them; drawn anyway, they became four
pills floating clear of the shell. The rear paddles sit directly behind the D-pad and the right
stick, so at their true position they collided with both, and anywhere else was simply wrong. Each
view now carries only what it can honestly show:

| view | controls | measured from |
|---|---|---|
| front | face buttons, sticks, D-pad, centre cluster | product photography |
| top | LB, RB, LT, RT | the vendor app's trigger screen |
| back | both paddles | the vendor app's back render |

The two vendor renders were dimensional references only and are not redistributed. Their trigger
screen conveniently highlights LT and RT, so those could be found by colour rather than by eye:
centres at render (201, 408) and (1069, 408), mirrored about a shell centre of 635.5, which is the
check that the measurement is sound. The back render puts the paddles at 0.250 and 0.750 of the body
width and 0.550 of its height.

**The back view reuses the front's silhouette path unchanged** — it is the same shell from the other
side, so sharing it keeps the two the same size and shape by construction rather than by a second
round of measurement. A test asserts they stay identical.

**The page is sectioned by view, from the same table.** The nineteen remap rows are grouped and
ordered by `anchors.VIEWS`, so the Buttons tab reads Front, then Shoulders and triggers, then Back
paddles — matching the three drawings rather than describing a different arrangement. Each view
contributes one contiguous run, which the shell requires: it groups adjacent rows under a heading and
does not reorder them. Two tests hold that: every row's section must equal its drawing's label, and
the runs must appear once each.

**Anchors are read out of the drawings**, never measured against them, so the picture and the
coordinates cannot drift apart. That is what fixes the source project's overlapping dropdowns, which
were hand-measured fractions that had drifted from the render they were taken from.

**Each view is a sub-tab**, not a section stacked down the page. Three drawings plus their controls
is well over two screens: stacked, the tab opened on a scrollbar the size of a thumbnail and the
shoulder buttons had to be scrolled to before they could be discovered. Only one side of a
controller is being looked at anyway.

**Controls sit level with the part they point at.** Spreading them evenly down the column was the
first rule and it is wrong wherever a view has few controls — the top edge has two per side, which
went to the extreme top and bottom and drew two leader lines the height of the window to reach two
pads sitting together in the middle. Each control now starts at its anchor's own height and is
pushed only far enough to stop overlapping its neighbour.

**Labels on a drawing are short.** `short_label` carries "LB" where the row's own label is
"LB (left bumper)": beside an arrow touching the left bumper the parenthetical explains what the
reader can see, and it was clipped doing it. The dropdown gets the same treatment for the same
reason — a row labelled "LB (left bumper)" whose dropdown also read "LB (left bumper)" said it
twice and fitted neither column.

Three corrections came out of review, and all three were the same error — reading a measurement off
a magnified crop by eye instead of from the pixels:

* **The three centre buttons were half again too large** (radius 32 against a true 20), so they
  nearly touched. Re-measured from their rims in an intensity profile. The middle one sits directly
  below the guide button, which is the check that the row is placed right.
* **The guide button was an outline with a thin X through it**, meaning to suggest the Xbox logo
  without copying it. It read as a "close" symbol. It is now a filled dark disc and nothing else,
  which is what the button looks like; the logo is Microsoft's and is not reproduced.
* **The shoulders, triggers and paddles were on the front view at all**, which is what this section
  is about.
* **The top view named its own pads**, "LB" and "LT" in text at each pad's centre — which is exactly
  where a leader line ends, so every line was drawn straight through its own label. The lines name
  them now, so the text is gone.
* **The bumpers and triggers were drawn as two rounded rectangles.** They are not. Each side is one
  tombstone-shaped pad split across the middle: the bumper has rounded top corners and a flat lower
  edge, the trigger a flat top and a strongly rounded, slightly tapered bottom. They share that
  dividing line and are the same width as each other, which the rectangles were not.

## 8. Scope, and the one rule that is wider than it looks

Matched on **product id**, not on 8BitDo's vendor id — the opposite of the Creative and Jabra
modules, deliberately. Those talk to a device-generic vendor library. Here there is no capability
query at all: the byte offsets *are* the capability list, and 8BitDo's other families (Pro 2,
Ultimate BT, Micro) use different records and different checksums. A vendor-wide rule would offer a
confident page of wrong offsets and then write them.

**Over Bluetooth that guarantee does not hold.** The rule is a name glob on `82CE`, and `82CE` is
what *every* 8BitDo controller advertises while its config button is held. The advertisement carries
nothing that identifies the model, so the rule cannot be narrowed — a Pro 2 in config mode matches
it. Two things follow, and both are in the module rather than the manifest:

* **The drawings are withheld** for anything that is not a known product id. The picture is the part
  of the page a user trusts to say which button they are editing, and a plausible drawing of the
  wrong controller is worse than none.
* **The page says the model is unidentified**, on the Sync button, and points at the USB cable —
  which does identify it.

**Adding another model** means a match rule, a field map if the record differs, and drawings named
for it. The assets are `ultimate-wired-xbox-{front,top,back}.svg` rather than `controller-*.svg` for
exactly that reason: a generic name is fine for as long as there is one controller and misleading
ever after, and `anchors.MODEL` is the single place the prefix comes from.

## 9. Status

**Verified over USB against an Ultimate Wired Controller for Xbox.** Read, remap, save and read-back
all confirmed on hardware, including the save session of §2 — which is where the verification
earned its keep, since three earlier versions read and wrote correctly and saved nothing.

**Bluetooth is still unverified through this application.** It sends the same save session as USB
and its L2CAP path is carried from a project that was hardware-validated on it, but this module's
own BLE code has not opened a controller.

Not carried: profile import/export, and the source's bundled captured session — the BLE packet
sequence is built from constants instead, which the source's own `ble_rmw.py` had already shown was
possible.
