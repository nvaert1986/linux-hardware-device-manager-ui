# Creative Sound Blaster — behavioural specification

What this module does, and why each decision was made rather than the obvious alternative. Written
from the source project `plasma-creative-x4-protocol-soundcard-support`, which reverse-engineered
the protocol against a Sound Blaster X4 and verified it on that hardware.

A Sound Blaster X4 has since been attached and driven through this page, which produced the
corrections in §7a — five of them, all in what the page *offers* rather than in the protocol.

## 0. What makes this module different from the others

Every other module opens a node the kernel provides: hidraw, an RFCOMM socket, an I2C bus behind a
DRM connector. This one **claims a USB interface**. Creative's control protocol rides on a CDC-ACM
function, and there is no character device to open — libusb takes the interface directly.

That single fact produces most of what follows: a fourth transport in discovery (§2), the only
vendor-scoped line in the udev rules (§3), and a connect measured in seconds rather than
milliseconds (§4).

## 1. Why there are no per-model tables

The obvious design is a table of supported product ids. It would be wrong, and the vendor's own
library is what proves it.

`CTCDC.dll` contains exactly one per-model table: an eleven-entry `PID → display name` chain behind
a `mov eax, 0x041E ; cmp cx, ax` vendor check. **The Sound Blaster X4's own product id (`0x3278`) is
not in it** — and the vendor app drives an X4 perfectly well. So the table cannot be a gate; it is a
display-name lookup. The other six `0x041E` comparison sites are per-model special cases for four
specific devices, plus one generic "is this my device" check that reads vendor at struct offset
`+0xB8` and product at `+0xBA` at runtime.

The DLL talks to whatever it is handed. So this module matches on **vendor `041e` and nothing
narrower**, and the shape of the page comes from two masks the device answers with:

| Query | What it says |
|---|---|
| `FeatureControl` op 2 | 32-bit mask of the toggles *this unit* implements |
| `SubFeatureSupport` | 32-bit mask of what the *DSP* implements — equaliser, Crystalizer, surround |

A unit answering neither is shown everything. That is the source project's `DeviceState.supports`
rule kept intact: a control that turns out not to apply is a smaller failure than a working control
that was hidden.

For the record, the eleven names the DLL does carry — useful for recognising hardware, not for
gating it: Sound Blaster X3 (`3264`), G3 (`3267`), G4 (`3268`), SXFI Theater (`3253`), AIR (`3258`),
AIR C (`3262`), CARRIER (`3266`), GAMER (`3269`), AIR GAMER (`3e10`), SXFI AMP (`6007`), SXFI
Headphone Audio (`6100`).

## 2. The fourth transport, and why the filter is tight

`discovery.enumerate_usb()` walks `/sys/bus/usb/devices` and returns devices whose interfaces match
one of the signatures in `_CONTROL_INTERFACES`. Nothing is opened — interface classes are sysfs
attributes, read as files, exactly as `enumerate_hid` reads report descriptors.

That list is deliberately a set of exact `(class, subclass, protocol)` signatures rather than a
class test. Enumerating every USB device would put hubs, webcams, card readers and every composite
keyboard interface in the sidebar, and the sidebar is the product. Creative's entry is CDC-ACM
(`0x02/0x02`), a strong signal: a control channel that is not audio, not storage and not HID.
Communications-class interfaces that are *not* ACM — ethernet, ATM, OBEX — are skipped; a network
adapter is somebody else's business.

The 8BitDo module later added a second signature, GIP (`0xFF/0x47/0xD0`), matched as the full
triple because class `0xFF` alone is what every dongle in the world falls back to. Nothing about
Creative changed: a device qualifies if *any* signature matches, and the matched names are reported
in the `control_interfaces` property so a manifest can ask for the transport it actually speaks.

**One physical device is still one row.** A device may expose both a HID interface and a CDC
channel, and the two enumerators find it independently. `_one_row_per_usb_device` drops the raw-USB
duplicate, and **hidraw wins**: its row carries an openable node, a device kind read from the report
descriptor, and an icon. A module needing the CDC channel finds it from the USB path both rows
share, so nothing is lost by preferring the richer row.

**PCIe is out, and correctly so.** The manifest matches `transport = "usb"`. Discovery has no PCI
enumeration at all, so an AE-5 or a Z cannot be matched even by accident — and that is right rather
than lucky, because this protocol runs over a CDC-ACM function a PCIe card does not have. Do not add
a PCI rule without a transport that can reach one.

## 3. The one vendor-scoped udev rule

Every other rule in `packaging/70-hardware-ui.rules` matches a node *type*. This one names Creative
Technology, because there is no node type to match: claiming a USB interface needs access to the USB
device itself, and `SUBSYSTEM=="usb"` unqualified would hand every USB device on the machine to the
logged-in session.

Nothing is detached. On kernels without `cdc_acm` those interfaces have no driver bound, and ALSA
owns only the audio interfaces regardless — so the card keeps playing audio while it is configured.

## 4. Connecting takes seconds, and the user is told first

The device boots **locked** and silently discards every `5A` command until an ASCII
challenge/response handshake completes. The handshake alone retries five times at one-second
intervals, because the vendor's capture shows the first probe often goes unanswered; the initial
sync then issues nineteen reads.

So `connect_notice()` says so *before* the wait rather than after it, and `connect_timeout` is 45 s.
Same reasoning as the Jabra module's 30–60 second warning: several seconds of silence reads as a
hang.

The unlock survives until the device is power-cycled, and once in command mode `whoareyou` stops
being answered — so the transport probes with `MAX_PAYLOAD_SIZE` first and skips the handshake if
the device is already unlocked.

## 5. Reads come from held state, not from the wire

The device is **push-driven**. A write draws an Acknowledge, then the changed state, then any
dependent state — toggling Direct Mode pushes all twelve equaliser parameters. The controller
applies every frame as it arrives, so held state is current by construction.

Re-reading after a write would be both slower and *less* accurate: the card takes about half a
second to commit, so an immediate read returns the old value. `get_many` drains what has arrived and
answers from state. There is no polling anywhere, matching Creative's own app, which registers a
notification callback and has no polling timers. `refresh()` is a full re-read and a user action.

## 6. Two acknowledge landmines, carried over verbatim

Both were paid for once in the source project and are pinned by tests here with the same capture
bytes:

1. **A Super X-Fi mode write draws two acknowledges** — a failure for a *different* operation, then
   ours with status 0. Raising on the first was a spurious rejection, so a failure whose echoed
   operation is not ours must be ignored rather than raised.
2. **For `SetMalcolmParameter`, byte 0 is an entry count, not an operation.** Gating the *success*
   path on the operation comparison made every equaliser write time out. The comparison may only
   ever suppress a *failure*.

`UPGRADE` (83) and `FACTORY_RESET` (155) are refused unless explicitly permitted. Neither is exposed
as a capability, so the guard exists for a caller that has not been written yet — which is exactly
when it will matter.

## 6a. What the page offers, and five corrections from hardware

Every one of these was a control that read wrong to somebody holding the card, and every one came
from this port adding or tightening something the source project did not have.

**Output had three entries and the card has two.** `OutputTarget` carries a `POWER_AMP` bit, so all
three were offered; "Powered Speakers" is not an output the X4 has. The protocol has no query for
which targets a unit really has, so a third entry is this application inventing an output and then
routing audio into it — not cosmetic, since selecting it is silence. Two entries now, in the vendor
app's own order and wording.

**Direct Mode bypasses the whole DSP, not only the equaliser.** Super X-Fi and headphone
virtualisation are DSP processing too and were left live, offering switches that do nothing. All of
them are gated now, and output routing and headphone gain are deliberately *not* — those are
analogue and work either way, which the source project says in as many words.

**The equaliser was greyed out whichever way Direct Mode was set.** Two separate causes, both ours.
The rows were *also* gated on the Graphic Equalizer toggle, which greys the whole tab for anyone
whose card has the equaliser off — the factory state, and the one state from which a curve can never
then be built. And `requires` naming a capability in **another group** could not resolve at all: each
tab kept its own value map, so the Equalizer tab never learned Direct Mode's value and compared
against `None` for ever. That was a shell bug, fixed in the shell.

**Super X-Fi Mode was permanently unavailable.** It was gated on the Super X-Fi toggle, and the
initial sync never reads Super X-Fi — the vendor's own sync does not either; the device reports it
only when a `HardwareButton` frame arrives. Unknown is falsy, so the gate never opened.

**The sliders did not follow a preset.** Applying one writes eleven values in a single action; held
state was right immediately and the page was not, because the shell repaints the control that was
written and nothing else. Capabilities now declare `refreshes`, and the shell re-reads those rows
from held state after the write.

Three things the page was simply missing against the vendor app, now present: the card's **volume
and mute** readouts, its **four stored profiles** (recall and store), and a decode of the **feature
and DSP masks** — which is the honest answer to "why is setting X not here?".

### The equaliser is a list of modes, not a checkbox

This came out of a question asked twice: *why did enabling the equaliser put it in Movie mode?*

Because a stored profile was already live. A checkbox is a faithful model of the device operation —
`GRAPHIC_EQ_ENABLE` really is a boolean — and a poor model of the card: switching it on plays
whichever of the four stored curves is active, so the honest reading of "tick this box" was "turn on
Movie mode". The card's own button agrees with the list rather than the checkbox: it cycles off,
then each mode in turn, with a different colour for each.

So one control carries **Off**, **On — the curve below**, and the four stored profiles. Selecting a
profile enables the equaliser and recalls it, which is a single act on this hardware. Two device
operations behind one row, and the row is the one the user is actually making.

**They are modes, not numbered slots**, and they are named. A Sound Blaster X4's are Music, Movie,
Footstep Enhancer and EQ for Super X-Fi — read off the card by its owner, not decoded — and the
button on the front cycles exactly those, a colour each. "Profile 1" to "Profile 4" named the thing
without saying what it is. The table is keyed by product id and consulted only for a model that is
listed, because this module matches on vendor id alone and a confident wrong name is worse than an
honest number; imported vendor preset data overrides it per slot where it has an opinion.

Nothing about them is read from the card. There is a command to *name* a mode and one to select one,
and no captured reply to a name **read**. Which one is live cannot be asked either, so the row
reports the last one chosen here.

**The modes are split between the two Super X-Fi states, and the split is symmetric.** Measured on
an X4, both directions answering `status=128` on command 26:

| Super X-Fi | accepted | refused |
|---|---|---|
| on | EQ for Super X-Fi | Music, Movie, Footstep Enhancer |
| off | Music, Movie, Footstep Enhancer | EQ for Super X-Fi |

The card keeps two sets of equaliser modes, one per state, and will not enter the wrong set. Only
half of that was visible at first — the fourth mode needing Super X-Fi — which is why two attempts
to model it were wrong: the rule is not "one special mode", it is a mutual exclusion.

**It is enforced by the card, not by this page**, and reported here as an advisory that names the
modes currently available.

Two attempts to enforce it by hiding entries failed in different ways: the first ran off a value
that could not then be read, so it hid a mode that was available; the second rebuilt the page when
Super X-Fi changed, which bumps the capability revision, repaints every tab and makes the page jump
under the user's hands. An advisory carries the same information and updates in place, and the card
refuses the wrong mode regardless. What was actually wrong all along was the reporting: the refusal
reached the user as `command 26 rejected, status=128 (1a 80 00 ...)`, a protocol dump wearing an
error message.

The mode is written **before** the equaliser is switched on, so a refused mode cannot leave the
equaliser on in whatever mode happened to be live already.

Storing a curve into a mode stays a separate action with a dialog, because it takes a name as well
as a slot — and it replaces one of those four, which the dialog says.

## 6a2. The acknowledge is not the commit

The card acknowledges a write, commits it a while later, and only then reports the new value. The
gap is real and variable: routing measured at 200 ms on one attempt and 1.4 s on the next, and the
module's own notes had said "about half a second" all along without the code acting on it.

That produced a clean bug report — choosing Speakers put the box straight back on Headphones, while
the card really had switched — and it is worth writing down because the obvious fixes are each
wrong in an instructive way:

* **Return the value from held state after the acknowledge.** That is the value from *before* the
  write, because the state push has not arrived.
* **Wait a fixed time, then read.** Caught it two attempts out of three. The card is slow, not
  punctual.
* **Read it back immediately, then settle.** Worse: settling replays whatever is still queued,
  including a push describing the previous state, which puts the old value back *after* the read.

What works, for routing, is to flush and then read, repeatedly, until the card agrees or a deadline
passes -- and to do it **last**. Routing is one of the few things with a real getter, which is why
it can be settled rather than merely waited on. A card that never agrees is reported as it last
answered rather than retried for ever: the page showing what the device says is honest either way.

Left alone for four seconds the card reported the requested output three times out of three in both
directions. **It never reverts.** Every report of it doing so was this application reading too soon.

## 6b. The card answers on a different opcode than it was asked on

Super X-Fi, its mode and the live profile all read as unknown for a long time, and this module
documented them as write-only. That was wrong, and the mistake is worth keeping because it is easy
to repeat: **the device replies with a different operation code than the one that made the change**,
and both this module and the project it was ported from matched the code they had sent.

Captured from a Sound Blaster X4:

```
->  07 1e 00 01 00                       set button 30 (Super X-Fi) on
<-  08 ff ff 01 00 ...                   op 8, no button id, the new value at byte 3
->  0c 50 43 47 20                       set the mode to "PCG "
<-  0d 50 43 47 20 30 30 30 37 2e 31     op 0x0d, the mode, then a firmware version
<-  02 00                                ACTIVE_MALCOLM_PROFILE: type 2 (DEVICE), index 0
```

Matching op 7 on the way back never fires, because nothing arrives with op 7. The consequences ran a
long way: the Super X-Fi box sprang back to unticked on every click, Super X-Fi Mode was gated on it
and therefore permanently dead, and the mode selector could only report what this application had
itself chosen.

All three are parsed now, and the mode selector follows the button on the front of the card as well
as driving it. The `ff ff` where a button id would sit is not interpreted: only one button on this
card reports at all, and inventing a meaning for it would be guessing.

**They are still not read at connect** — the card volunteers them after a change and the initial
sync does not ask, so a freshly connected card shows Super X-Fi as unknown until something touches
it. Read forms for the three are untested; probing unlisted operations on somebody's sound card is
how you find out which of them writes.

## 7. What is actually verified here

| | |
|---|---|
| **Unlock handshake** | **Verified.** The port reproduces a challenge/response pair captured from real hardware, byte for byte. If the AES-256-GCM reconstruction had broken in the port, this fails. |
| Framing, command set, acknowledge handling | Ported from hardware-verified code; exercised against capture bytes, not against a device |
| Capability building, read/write mapping | Fakes, plus the page driven against an X4 |
| Transport claim, endpoints, unlock on the wire | **Run against a Sound Blaster X4.** It unlocks, reads and accepts writes |

The equaliser interlock is worth flagging as *ours*: Direct Mode bypasses the DSP, so the equaliser
does nothing while it is on. Creative's own Equalizer module never references Direct Mode — the
interlock was observed on hardware in the source project and is justified by what Direct Mode is.
Every row it bypasses is gated on it through `requires` — the equaliser, Super X-Fi and headphone
virtualisation — and an advisory on each affected tab explains why, because a control that moves and
changes nothing is the confusion `requires` exists to prevent.

## 8. Not carried: the host-side DSP

X-Bass and Dialog Plus are **not device state**. They are PipeWire filter chains hosted by child
`pipewire -c` processes, with their own install/route/level lifecycle and their own JSON state file,
because PipeWire offers no readback for live control values. That is roughly 1,400 lines in the
source project with no analogue anywhere in this application.

They are out of scope for now — a deliberate decision, not an oversight. Putting host-side effects
into a schema built around devices that answer back would need write-only capability support in
core, and the honest version of that is a separate piece of work. `PROJECT_STATE.md` tracks it.

`store_profile` and `select_profile` **are** carried now — the card's four stored slots are the one
thing this application cannot otherwise give a user, since a curve written there survives with no
software running. See §6a.

Still not carried: the `HardwareButton` writes for SBX and Scout Mode. The button ids come from the
vendor enum but the Windows app never sends them — toggling Scout there emits only "Super X-Fi off"
plus "graphic EQ off" — so they stay unexposed rather than guessed at.

## 9. Vendor data, and what is missing

Eleven equaliser presets recovered from the USB capture ship with the module — Flat, Acoustic,
Classical, Country, Dance, Hip Hop, Jazz, Pop, R&B, Rock, Vocal. Generic names for kinds of music,
and ten gain figures each: observed on the wire, not copied out of a vendor file. They are Python in
`protocol/catalogue.py`, so they need no setup and work as soon as a device connects.

Presets carry separate speaker and headphone curves — the vendor tunes them independently, and 43 of
the 71 factory presets genuinely differ — so the curve written follows the card's current output
routing.

**Creative's own preset files and artwork never ship**, and `.gitignore` and `tools/publish.sh` both
refuse `presets.json` and `device.png` so a stray copy cannot be committed or published by accident.

**There is no importer.** `protocol/presets.py` will merge a `presets.json` found in the module's
vendor directory, and `fetch_photo` will use a `device.png` there, so the reading side works — but
nothing in this application writes either file. The source project's `tools/import_vendor_data.py`
was not carried across. The module therefore declares **no** `[vendor_assets]` block: doing so would
put "can use vendor data, imported from the manufacturer's own files" on the Modules page and
describe a capability the code does not have. Restore the block in the same commit that adds the
importer; a test asserts the two stay together.

The per-game presets are a separate matter again. The installer contains no gains for them at all —
the Creative App fetches them at runtime — so there is nothing to import even with an importer.

## 10. Status

**Opened, read and written through this shell against a Sound Blaster X4** (`041e:3278`, firmware
`1.7.250324.0910`). The unlock completes, settings change, and the corrections in §6a, §6a2 and §6b
all came from those sessions.

That model is `status = "verified"`; every other Creative device stays `status = "family"`, since
the module matches on vendor id alone and one card has been driven.

**The verified rule is declared on both transports**, which is not redundancy. An X4 exposes a HID
interface as well as its CDC control channel, one physical device gives one row, and discovery
prefers the hidraw row — so the row carrying the label is the HID one. Marking only the USB rule
verified left the card reading "untested model" on screen while the registry cheerfully reported
`verified` when asked directly.
