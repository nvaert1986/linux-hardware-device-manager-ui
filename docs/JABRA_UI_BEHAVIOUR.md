# Jabra GNP — behavioural specification

What this module does, why, and — as importantly — what it deliberately refuses to do. The
protocol layers are a port of the standalone `plasma-jabra-headphone-support` project, whose rules
were found by running against a **Jabra Link 390** and an **Evolve2 85**. This document records
which of those rules survived the port and which parts have *never* been exercised.

## 0. What makes this module different from Poly and Sony

Sony has a fixed function list. Poly ships one JSON catalogue per product. Jabra publishes **one
catalogue for their entire range** — 423 properties, each declaring its command, subcommand and
byte converters — and says nothing about which device has which. So support is not read from data;
it is **discovered by asking the hardware**, one property at a time.

That single fact shapes everything below: the slow first connect, the cache, the short probe
timeout, and why an unsupported setting produces no control rather than a greyed-out one.

## 1. The catalogue is the protocol, not the labels

For Poly, missing vendor data costs the manufacturer's own wording. Here it costs everything: the
catalogue *is* the wire description. Without it a Jabra device can be identified and nothing else,
which is why `module.toml` marks the asset `required = true` and `connect()` raises
`DependencyMissing` rather than `Unreachable` — the headset is fine, the data is not here, and the
shell's import offer is the correct response.

**Why it is not shipped.** `@gnaudio/jabra-properties-definition` declares `"license": "ISC"` in
its `package.json` and contains **no licence text at all**. That declaration is how npm conveys
licences and is very probably a valid grant, but ISC's own terms require a copyright notice and a
permission notice to travel with every copy, and GN Audio published neither. Rather than ship GN
Audio's data under a notice this project would have had to write on their behalf, it is fetched
from their own publication, on consent, pinned to version `14.4.0`.

## 2. Connect is a probe, and the probe is the slow part

An unimplemented subcommand answers NACK `0xF6`/`0xFF`. Some properties answer *nothing at all*
(the Link 390's `voicePrompts`), so a timeout counts as absent too. Three measured decisions:

| Decision | Why |
|---|---|
| **One pass, not two** | Probing and then re-reading doubled the round trips. A read answers both questions at once: a value proves the property exists *and* is the value. |
| **`PROBE_TIMEOUT = 0.35`** | Measured: 0.6 s gave ~42 s per device, 0.35 s gives ~25 s, and no property changed its verdict. Lower starts risking a slow-but-present property being cached as absent. |
| **Capability cache** | Keyed by (product id, firmware), versioned against the catalogue's size. Warm connect reads only the ~69 properties that exist instead of probing 283 — about 8 s. |

The cache is a cache, not configuration: deleting it costs one slow connect. A catalogue bump
changes its size and therefore re-probes, rather than hiding newly-supported properties.

## 3. Nothing is polled

`configChangeEvents` is armed at connect. The device then pushes the list of CONFIG subcommands
that changed, which are resolved back to property names and re-read. That covers changes made with
the headset's own buttons without putting any traffic on an idle link.

**The register is always written, even when every wanted bit already reads as set.** This is not
an oversight and must not be "optimised". Skipping the write meant no on-head or boom-arm event
ever arrived, while a diagnostic script that asked for more bits — and therefore *had* to write —
saw them fine. The read-back reflects stored configuration; the device only starts streaming when
the register is written during the session.

Two more rules in the same area:

- **Bits are written together first, individually on refusal.** An Evolve2 85 answers NACK `0xFA`
  for bits it has no notification for (9, 10) and rejects the **whole** write when one bit is bad.
- **`microphoneMuteState` arms via its own pipeline**, not the shared register — its subscribe
  steps contain no `bitmaskInsert`. It is event-only, so an unarmed bit is not a stale value, it
  is no value ever.

## 4. Verification: two mechanisms, chosen per property

Of 307 writable properties only **10** emit a change event, so Sony-style echo verification cannot
cover Jabra. Verify-by-read alone would miss changes made on the device unless polled. So both,
picked from the catalogue:

1. the device's change event, if declared and armed;
2. otherwise a **settled re-read** — 4 attempts, 0.15 s apart, because a device can briefly report
   the old value after acking;
3. and if the property is not readable at all (13 of them), the absence of a NACK is all there is.
   Those carry a note saying so rather than implying a confirmation that never happened.

## 5. Safety — what this module will not offer

`labels.is_dangerous` withholds `factoryReset`, `firmwareUpdate`, the DFU entry points, pairing-list
clears, certificate and provisioning writes. Unlike the Poly module's maintenance tab, these are
**not** offered as confirmable actions: they are device-bricking operations behind a protocol
nobody here has tested.

The exclusion is applied to the **probe list**, not merely to the page. Probing only reads, so
probing them would be safe — but a name in the capability cache implies a control, and keeping the
two lists identical guarantees a factory reset can never reach the UI through a stale cache entry
written by an older version.

## 6. Two endpoints, probed separately, kept apart

A single HID interface fronts several GNP devices: on a Link 390 the dongle answers at `0x01` and
the headset at `0x04`, each with its own name, PID, firmware and settings. They answer
*differently*, which is the whole reason they cannot share a page:

| property | headset `0x04` | dongle `0x01` |
|---|---|---|
| `name` | `Jabra Evolve2 85` | `Jabra Link 390` |
| `serialNumber` | *(differs)* | *(differs)* |
| `firmwareVersion` | `1.5.7` | `3.0.13` |
| `ancMode` | readable | NACK — unknown sub-command |
| `radioPower` | — | `normal` |

(From `dump_baseline.py`, 2026-08-05.)

So **each endpoint gets its own probe, its own cache entry** — keyed by its own product id and
firmware, so one endpoint's verdict can never stand in for the other's — and its own section, whose
heading names the device. The keyspaces are separate (`setting.` and `relay.`) because the property
names collide while the values do not.

The adapter's set is discovered, not listed. The baseline above probed 19 properties by hand;
a full probe at `0x01` finds whatever that model actually answers, so a Link 380 or 400 needs no
code change.

An adapter that answers nothing gets no section, and a failed adapter probe costs its section
rather than the headset's page.

## 6a. The catalogue over-promises, and only a write finds out

GN Audio's catalogue describes the whole range, so it lists values a given model does not have.
There is no query for this: the only way to learn is to write and be refused. Measured on an
Evolve2 85 + Link 390:

| property | catalogue | actually accepted |
|---|---|---|
| `hearThroughLevel` | −12…6 | **−12…0** |
| `intellitoneLevel` | 6 levels | `peakStopOnly`, `level3`, `g616` — non-contiguous |
| `radioPower` | 4 ranges | all but `ultraLow` |
| `automaticAudioDetection` | 4 modes | `disabled`, `enabled` |
| `publicModeEnabled` | boolean | refused outright |
| `soundMode2` | normal/bass/treble | **`normal` only** |

**Two different refusals, one conclusion.** Most answer NACK `0xFA ILLEGAL_PARAM`. `soundMode2`
does not: it acknowledges the write and keeps its old value, which the settled re-read catches as
a verification failure. Both mean the catalogue lists something the hardware does not have, so both
narrow the control — a control that silently reverts is indistinguishable from a broken application.

**A refused range is searched, not stepped.** Creeping down one step per click would make a user
hit the wall six times before the slider told the truth, so a refusal binary-searches between the
last value known good and the rejected one, then restores the device to a value it accepts. Bounded
at ten probes; on the measured hardware it lands on 0 after two.

**Not a state lock.** Tested explicitly: `hearThroughLevel=3` is refused under *every* `ancMode`,
so this is not a cross-feature gate like the Sony equaliser's LDAC lock — the range is simply
narrower than the catalogue admits. Nor is there a sibling property that works: `ancHearThroughLevel`,
`hearThroughEnabled`, `hearThroughConfiguration`, `intellitoneSoundLevel` and
`intellitoneSoundLevel2` all exist in the catalogue and none of them exists on an Evolve2 85.

**Learned once, not once per session.** Findings are cached beside the supported-property list,
keyed by the same (product id, firmware) and versioned the same way, and applied at connect. The
source project dropped options at runtime only, so its slider offered +6 again on every reconnect
and the user hit the same wall every session — which reads as a broken application rather than
limited firmware. Keyed by *capability* key, not property name, because the adapter answers many
of the same names and refuses different values. Deleting the cache re-learns it, so a wrong entry
costs one refused click rather than a permanently missing setting.

## 7. Transport rules that are not obvious

- **Never trust `/dev/hidrawN` ordering.** After a re-enumeration this rig went from
  (15 = Link 390, 16 = deskstand) to (15 = deskstand, 16 = Link 390), silently pointing tools at
  the charging stand. Candidates are ordered by GNP report size, then non-accessory name.
- **Report id and size come from the report descriptor.** Link 390: report `0x05`, 63 bytes.
  Evolve2 85 deskstand: report `0x05`, **32 bytes** — so packets fragment there and must be
  reassembled.
- **Dongles re-enumerate spontaneously.** `EPIPE`/`ENODEV`/`ESHUTDOWN`/`EIO` mean the fd is dead,
  not that the request failed; the node is re-resolved **by product id**, and a freshly reopened
  node still needs 0.25 s to settle before the first write.
- **One conversation per device, across processes.** A `flock` under `XDG_RUNTIME_DIR`, taken
  non-blocking so a second instance says so immediately rather than appearing to freeze.
- **Replies are matched on sequence number.** A timed-out request's reply still arrives; accepting
  it leaves every later exchange off by one for the rest of the session. The symptom was identity
  strings decoding as `X` with product `0x000E`.
- **A global lock was tried and reverted.** It doubled first-connect time (~42 s to ~84 s) and the
  real cause was the sequence mismatch above. Serialisation is per session.

## 8. Labels — Jabra's own wording, with a fallback

Two layers, and the order matters:

1. **`labels.py`** — Jabra's own strings, lifted from the `resources.arsc` string pools of their
   Android apps (`com.jabra.moments`, `com.jabra.plus`): 71 property labels, 49 button-action
   names, 16 value maps, 20 LCID names, units and descriptions. This is why the page says
   *"Noise cancelling mode"* and *"Wireless range"* rather than the protocol's `ancMode` and
   `radioPower`.
2. **`categories.label_for`** — the generated fallback for anything that map does not cover:
   camelCase split into words with the catalogue's acronyms kept upper-case, so a property added
   by a future catalogue version reads as "ANC mode" rather than vanishing or reading "Anc mode".

Per-model wording is a third layer that does **not** exist here. Jabra Direct fetches it from
`…/deviceconfiguration/{pid}/localizedtext`, which is per-model, per-firmware, needs a network
round trip, and answers 404 on the test hardware. The hand-written map covers what a user actually
sees instead.

All three are wired, and that is worth stating because porting the file is not the same as calling
it: `label`, `description`, `unit`, `value_label`, `format_value`, `language_name` and
`language_choices` each have a call site here matching the one in the source's `gui.py`. Shipped
without them, the page showed `_0dB`, `1033` and `zh_CN` while the correct implementation sat
unused one file away.

`labels.format_value` also handles what the generic widget layer cannot infer — notably that
`currentLanguage` and `currentLanguageCode` both decode a raw Microsoft LCID (1033 = en-US) even
though the catalogue declares an 18-entry enum for the latter, so that enum must not be trusted as
the value space.

**Language properties are a trap.** `currentLanguage`, `currentLanguageCode` and
`currentLanguageInConfigMode` all return a raw Microsoft LCID, and the catalogue declares an
18-entry *string* enum for the second that the property never returns. Built from that enum, the
control silently displays item 0 — `zh_CN` — on every device. The value space is the device's own
`availableLanguages` (an Evolve2 85 reports exactly `[1033]`), named through `LCID_NAMES`.

## 8a. Battery, live state, and the photo

**Battery** is a meter. `batteryLevelV2` decodes as a jsonObject whose `flags` byte carries the
charging and low bits beside the percentage, so state comes free with the level — no second read.
The plain-integer form is accepted too.

**Live state** — on your head, boom arm, microphone, call-versus-media — are readouts, not
settings, because they are event-only: no read pipeline exists for any of them. Two consequences
the source project found:

- **Only armed state gets a row.** An unarmed notification never fires, so its row would read
  "Not reported by this device" forever.
- **On-head arrives one earcup at a time**, and both sides fire in the same instant when the
  headset is put on. The sides are accumulated rather than last-one-wins, which a plain dict
  keyed by property name would be.

**Mute and call state come from HID, not GNP.** The catalogue declares `offHook`/`gnOffHook` as
`hidInputReport` steps, so no GNP request returns them — but the interface carrying the GNP tunnel
also carries the standard Telephony page, and those reports are kept rather than discarded. This
works even though the kernel throws Hook Switch away (`hid-input.c` has no `case 0x20:` under
`HID_UP_TELEPHONY`), because the raw report still arrives over hidraw.

**The photo** is an opt-in download, following the links Jabra's device-configuration service
advertises rather than a guessed CDN pattern. An empty asset list is a normal answer — an Evolve2
85 answers 200 and then 404s every image URL, with `application/problem+json`, which is exactly
what must never be cached as a picture.

## 9. Vendor gating

Matching is on **USB vendor `0x0B0E`** (GN Netcom / GN Audio) only. Nothing in this module can
claim a Logitech or a Poly device.

Vendor id alone is deliberate, rather than vendor id plus the `0xFF00` usage page: matching on the
usage page would mean parsing every candidate's report descriptor during enumeration, which is the
transport's job at connect time. A Jabra HID node without the GNP tunnel fails to open with a
clear message, which is better than being invisible.

## 10. Status — what is and is not verified

**Verified in the source project, on a Link 390 + Evolve2 85 (2026-08-05):** framing, endpoint
discovery, capability probing via NACK, read/write with settled-read verification (~154 ms per ANC
write), the catalogue interpreter across all 423 properties, and `configChangeEvents` → targeted
re-read.

**Also verified in the source project:** the equalizer, read and written through its own tab, and
per-endpoint reads at both the headset and the dongle (`dump_baseline.py`, §6).

**Verified through this shell on 2026-08-11**, against a Link 390 with an Evolve2 85 behind it and
the deskstand alongside: endpoint discovery at `0x01` and `0x04`, a 68-property probe, the
capability cache cold (~30 s) and warm (~3 s), reads and writes with settled-read verification, the
equalizer read and written, the adapter's own 32 settings as controls, battery, the vendor-data
download, the cross-process lock refusing a second client, and the learned-limits cache surviving a
reconnect.

**Still untested:** a Jabra reached over a cable rather than a dongle or stand, any other model,
and anything with a camera.

**Port coverage.** Every source file is accounted for: 12 ported, 6 replaced by the shell
(`gui.py`, `status_widgets.py`, `workers.py`, `app.py`, `transport/hotplug.py`) or out of scope
(`keyfix.py`, §10). `catalogue_source.py` became `assets.py`.

**Known gaps, inherited:**

| Area | State |
|---|---|
| Per-model wording | Jabra's cloud `localizedtext` endpoint is not used; the hand-written map covers what a user sees. |
| Volume-key duplication | One press emits four events. The source project solved it with a separate uinput debouncer script; that is an input-remapping daemon, not device configuration, and does not belong in this application. |
| Adapter events | The change stream decodes events from the headset. An adapter setting changed by other means is seen on the next Refresh, not pushed. |

## 11. The equalizer

Five bands on an Evolve2 85, read from `CONFIG`/`0x7D`. Three facts decide how it is exposed:

- **The read takes an argument byte.** Jabra Direct sends `7d 00`; the bare subcommand returns ten
  useless bytes, which is what once made this look paginated and unreadable.
- **The read and write layouts differ.** The reply interleaves each band's opaque `A` field *before*
  the band, omits it for band 0, and adds a flag byte per later band. `decode_read` handles the
  reply; `encode` produces the compact write payload. Reading the reply with the write layout
  produces nonsense.
- **`A` must be written back unchanged.** It looks like a Q/bandwidth term and the vendor's UI never
  varies it. So a write is a read-modify-write over the last table read, and a single-band write
  does not exist on the wire.

Hence one `writes_with` group covering every band: the shell disables them together, and one drag
is one write. "Flat" writes every gain to 0 dB, which is exactly what the vendor's Restore does.
Range is ±6 dB in 0.5 dB steps, matching the vendor UI.

## 12. What was dropped from the source project

**Widgets, not features.** Its GUI — `gui.py`, `status_widgets.py`, `workers.py`, `app.py` — is
exactly what this application replaces, and `EqualizerPanel` goes with it. That is a rendering
decision and nothing else: the equalizer itself is fully present (§11), as are the battery bar and
the state panel, rebuilt as capabilities instead of hand-laid Qt.

Genuinely dropped, with reasons:

| Dropped | Why |
|---|---|
| `keyfix.py` | A uinput debouncer for the volume-key duplication. An input-remapping daemon, not device configuration — see §10. |
| `transport/hotplug.py` | This shell has its own udev and BlueZ hotplug. |
| `gui.py`, `status_widgets.py`, `workers.py`, `app.py` | Replaced by the shell and renderer. |

Nothing the device can *do* was dropped.
