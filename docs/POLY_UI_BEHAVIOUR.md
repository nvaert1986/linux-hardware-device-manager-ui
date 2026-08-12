# Poly Deckard — behavioural specification

Extracted from `plasma-hp-poly-protocol-headphone-support`, the hardware-verified reference
implementation: `gui.py` (785 lines), `device.py` (488), `workers.py` (262), `transport/*` (762),
`protocol/*` (418), `cli.py` (154), plus `docs/PROJECT_STATUS.md` and the 621-line
`docs/POLY_DECKARD_PROTOCOL.md`.

**Every rule below exists because it was found necessary on real hardware.** Written before any
adapter code, so the port can be checked mechanically rather than rediscovering rules one bug
report at a time.

Status: ❌ not ported · ⚠️ delivered differently · ✅ ported · 🚫 out of scope for this module

---

## 0. What makes this port different from Sony and Dell

Sony's page is hand-declared per feature. Dell's is derived from a capability string. **Poly's is
derived from a vendor JSON catalogue, one per product, keyed by USB PID** — and there are 228 of
them. There is no per-model code at all, which is why the reference implementation claims support
for devices nobody has ever tested.

Three consequences shape everything below:

1. **The catalogue is the schema.** Each entry names its own `get` / `set` / `event` message ids
   and its values with typed payloads. Nothing is inferred.
2. **The catalogue is vendor property** and cannot ship — see §11. This is the case
   `core/assets.py` was written for and never exercised.
3. **The device pushes changes.** Press mute on the headset and an `EVENT` arrives unprompted.
   The reference implementation polls *nothing*; a Poly link sits silent for 25 s at a time.

---

## 1. Link discipline — the rule every stability bug traced back to

| # | Rule | Why | Status |
|---|---|---|---|
| 1.1 | **One request in flight at a time.** | The vendor's own link behaves this way; it is measured, not guessed | ❌ |
| 1.2 | **A write is confirmed by the device's change EVENT, not by re-reading.** | The event is authoritative and arrives *before* the command ack. Re-reading immediately returns the **old** value for a short while — that produced a user-visible "setting reverts, then applies my previous choice" bug | ❌ |
| 1.3 | **After the event, re-read that one setting** — Poly Lens does, on 43/43 observed writes, ~40 ms later. Cheap, and it catches a device that acked without committing | ❌ |
| 1.4 | **Never re-read everything after a write.** A full snapshot is ~3.6 s; a write is ~100 ms. A snapshot per click meant a second change landed *during* it and the UI published state that predated the click | ❌ |
| 1.5 | **Hold an in-flight value per control and ignore contradicting device state until confirmed.** This is what actually closes the window — the click happens before the write is even dispatched | ✅ shell (`set_pending`) |
| 1.6 | **Idle is silent.** No polling, no periodic re-read. A full re-read is a user action | ❌ |
| 1.7 | **Drain unsolicited events every 1 s.** Sends nothing — a non-blocking read of what the device already pushed. Poly does the same with a permanent `AsyncReadThread` | ❌ core+ (§13.1) |
| 1.8 | **Bound the event backlog** (`MAX_EVENT_BACKLOG = 32`). The device reports things that are not settings — `HEADSET_BUTTONS_PRESSED_REPORT` fires on every mute/volume press — and those never match a catalogue entry, so unbounded they accumulate for the session | ❌ |

## 2. Connection

| # | Rule | Why | Status |
|---|---|---|---|
| 2.1 | **Connect is manual.** The config channel is served to one host at a time | ✅ shell |
| 2.2 | **The RFCOMM channel is dynamic** — observed 14 ↔ 15 between consecutive connections. Resolve from SDP by service name every connect. Never hardcode, never sweep | ❌ |
| 2.3 | **BlueZ exposes no RFCOMM channel numbers**, so the reference implements its own SDP client over L2CAP PSM 1, including continuation state | ❌ |
| 2.4 | **`PltHeadsetDataService` UUID `82972387-294e-4d62-97b5-2668aa35f618`** is the match signal | ❌ manifest |
| 2.5 | **BlueZ `Modalias` gives the USB PID without connecting** (`bluetooth:v0055p016Ad0000` → 0x016A), so the catalogue can be chosen before the channel is opened | ❌ |
| 2.6 | **Non-blocking connect + select + a 2 s settle before the first send** — a Sony lesson that turned out to be needed here too | ❌ |
| 2.7 | Sony's rule that probing can power-cycle the device **does not apply**: an unsupported Poly setting answers `SETTING_UNKNOWN` cleanly. Capability probing is safe | ❌ |

## 3. Two transports, one protocol

Deckard framing is identical over Bluetooth and USB; only the transport and the address differ.

| # | Rule | Status |
|---|---|---|
| 3.1 | Frame: `0x1LLL` (high nibble = SOF, low 12 bits = remaining length), **BladeRunner address u16**, message type u16, message id u16, payload. No checksum, no escaping, no sequence layer | ❌ |
| 3.2 | **The "reserved" u16 is the BladeRunner address**, `(dest << 4) \| src`. Zero over Bluetooth; `0x2000` addresses a headset behind a dongle, replies return `0x0200` | ❌ |
| 3.3 | **Every address needs its own `PROTOCOL_VERSION` handshake.** A dongle will not route settings to the headset behind it until that headset has been greeted at its own address | ❌ |
| 3.4 | Downstream ports come from `CONNECTION_STATUS` 0x0C00 — `{downstream_port_ids, connected_port_ids, originating_port_id}`, each u16-count-prefixed | ❌ |
| 3.5 | **USB writes must use control `SET_REPORT` (`HIDIOCSOUTPUT`).** A plain `write()` to hidraw uses the interrupt OUT endpoint and is silently ignored | ❌ |
| 3.6 | USB wrapper: `SET_REPORT(Output, 0x13)=01` once to enable, TX on report `0x07` (503 B, never chunks), RX on interrupt IN (62 B, chunks up to 6). Both `[0x07][chunk idx][chunk count][frame][pad]` | ❌ |
| 3.7 | **Over USB the settings belong to the headset, not the dongle.** Discovery sees the dongle's PID (0x02E6); the catalogue must come from the *connected* device's PID (0x016A). Getting this wrong builds the wrong page | ❌ |

## 4. Ids — the trap

| # | Rule | Status |
|---|---|---|
| 4.1 | **`Setting` and `Command` are separate id namespaces.** 17 settings read and write on *different* ids; 13 numeric ids mean different things per table | ❌ |
| 4.2 | **`0x0E1C` is `PARTITION_INFORMATION` to read and `REMOVE_PARTITION_INFORMATION` (destructive) to write.** Resolve by name, per direction. Never merge the tables, never compute one id from the other — `WEARING_SENSOR_MODE` is +2, not −2 | ❌ |
| 4.3 | **Event ids follow the *set* id, not the get id** | ❌ |
| 4.4 | **The SDK's declared C types do not give wire widths** — `SIDE_TONE_LEVEL` is declared `int` but is one byte. Use the catalogue's own types: `BOOLEAN`/`BYTE` = 1, `UNSIGNED_SHORT` = 2, `UNSIGNED_INT` = 4, big-endian | ❌ |
| 4.5 | **`METADATA` carries a *count* in its id field, not an Event id.** It must never be name-resolved; keep it in its own list | ❌ |

## 5. Capability model

| # | Rule | Why | Status |
|---|---|---|---|
| 5.1 | **The catalogue is the UI-exposed subset, not the capability list** — 26 entries versus 33 ids the V4310 answers. Probe live for support; use the catalogue for labels and types | ❌ |
| 5.2 | **A setting the device answers with `SETTING_UNKNOWN` is hidden**, control and label both | ❌ |
| 5.3 | **A whole tab is hidden when nothing in it is supported.** The V4310 correctly shows no Ringtones tab | ✅ shell |
| 5.4 | **`COMMAND_UNKNOWN` (16) on a write means readable but not writable** — seen on the V4310's `linkQualityReporting`. Grey the control; it is not an error | ❌ core+ (§13.2) |
| 5.5 | Unknown settings fall into an **"Other"** group, so a new device never silently hides controls | ❌ |
| 5.6 | Two choices named true/false, on/off or enabled/disabled → a checkbox; anything else → a dropdown | ❌ schema (`TOGGLE` vs `CHOICE`) |

## 6. Actions

`restoreDefaults` (0x0F13) and `clearPairedDevices` (0x1241) are **write-only, have no get id, and
are destructive**.

| # | Rule | Status |
|---|---|---|
| 6.1 | **Never issued during a capability sweep** | ❌ |
| 6.2 | **Never reachable from the generic setter** — `write_choice` refuses `is_action` before anything is sent | ❌ |
| 6.3 | Presented on their own **Maintenance** tab, each behind an individual confirmation | ❌ |
| 6.4 | Note: *"These actions cannot be undone and take effect immediately on the headset."* | ❌ |

## 7. Labels — Poly's own wording, never invented

The catalogue's `settingName` is an internal identifier (`enableOLI`, `G616`, `twa`). Poly's real
English wording was extracted from Poly Studio's Electron i18n bundle.

| # | Rule | Status |
|---|---|---|
| 7.1 | **Resolve per setting, all-or-nothing.** Mixing sources produced "1", "5 minutes", "6" in one dropdown | ❌ |
| 7.2 | Tier order: curated `SETTING_VALUE_LABELS` → minutes derived from `UNSIGNED_SHORT` seconds → `ui_labels.json` (matched on the *set* of option names) → `value_labels.json` → `humanize()` | ❌ |
| 7.3 | Curated names are Poly's: `enableOLI` = **Online Indicator**, `G616` = **Anti-Startle**, `scoTone` = **Active Audio Tone**, `twa` = **Noise Exposure**, `twaPeriod` = **Hours on Phone per Day** | ❌ |
| 7.4 | Boolean signatures are excluded from label matching — every boolean shares them, and a checkbox needs no per-value wording | ❌ |
| 7.5 | **Poly's catalogue descriptions are unreliable** — many settings carry a copy-pasted line about volume tones. Those are dropped rather than shown | ❌ |
| 7.6 | `humanize()`: sentence case per the KDE HIG, with an acronym table (VoIP, SCO, OLI, TWA, VP, ID, AAL, HD) and all-caps/digit passthrough so `G616` survives | ❌ |

## 8. Groups

`Audio · Mute · Calls & Prompts · Ringtones · Hearing Safety · Other`, plus `Info` first and
`Maintenance` last. Membership is a name table in the reference implementation; anything unlisted
lands in Other. ❌

## 9. Info and battery

Model, serial, firmware, hardware revision, battery, connection endpoint. ❌

**`BATTERY_INFO` (0x0A1A)** decodes to level, num_levels, charging, u16 talk minutes, high-estimate
flag. **The level is a step out of `num_levels`, not a percentage** — live V4310: `09 0b 00 03 61
01` = 9/11 ≈ 82 %, 865 minutes. Rendering it as "9 %" would be wrong by an order of magnitude. ❌

Strings are u16 big-endian length + bytes; `HARDWARE_REVISION_STRING` is UTF-16BE, zero-padded. ❌

## 10. Reconnect versus refresh

Two distinct user actions, and the distinction is worth keeping:

- **Refresh** re-reads every value over the existing session.
- **Reconnect** tears the session down and rebuilds it — handshake, downstream attach, PID lookup,
  capability probe. For when the session itself is wrong: link stalled, headset rebooted or moved
  host, dongle re-enumerated, or the device behind the dongle changed. ❌

## 11. Vendor data — the decision this module turns on

`plasma_poly_headset/data/` is **3.7 MB across 235 catalogue files** plus two label tables, all
derived from Poly's Android APK and Windows installer. The source project's own handover says it
plainly:

> It is Poly's, included for development reference, and **must not be redistributed** as part of
> any release. The bundled catalogues and label files under `plasma_poly_headset/data/` are
> derived from it — that should be settled before publishing.

This project's rule is already absolute: **nothing vendor-owned ships.** So the catalogues cannot
be copied into the module, and `core/assets.ExtractInstaller` — written for exactly this case and
never yet run against a real installer — is what unlocks them.

The shape that follows:

| | |
|---|---|
| **Ships** | A small hand-authored catalogue for the **verified V4310**, re-expressed in our schema, so first run is never a dead end |
| **Imported** | The other 227 devices, unpacked from the user's own copy of Poly Studio via `ExtractInstaller` |
| **Never copied** | Poly's JSON verbatim, the Electron i18n bundle, the decompiled APK |

`PolyStudio-5.1.0.1111-x64.msi` is already on this machine, so the import path can be exercised
for real rather than written blind.

**Open question for the user:** whether the hand-authored V4310 catalogue counts as "re-expressed
in our schema" or as a derived copy. It is a table of message ids and payload byte values — facts
about a wire protocol, not creative expression — which is the same footing as the Dell VCP tables
that ship today. Recorded here rather than assumed.

## 12. Known gaps, inherited

- **41 of ~1370 catalogue settings across 18 devices** use `BYTE_ARRAY` / `SHORT_ARRAY` /
  `PAYLOAD`, which the encoder refuses. That blocks device rename and Custom EQ. Needs the
  length-prefix encoding.
- **Only the V4310 is hardware-tested.** ANC, wearing sensors and equalisers have never been on
  the wire — they exist only in vendor data.
- **`METADATA_TYPE` blob sectioning is unconfirmed.** Not needed; capability probing covers it.

## 13. What `hardware-ui` needs before this module can be written

**13.1 — the shell must consume `Device.changes()`.** It is declared in the core API and **nothing
subscribes to it.** Sony deliberately does not override it, so the gap has never shown. Poly is
entirely event-driven: a mute button press, or a change made from another host, arrives as an
unsolicited `EVENT` and is the *only* way the UI learns about it, because nothing polls. The
controller needs a task per open device that iterates `changes()` and feeds `form.set_value`,
alongside the existing poll loop.

**13.2 — a capability must be markable read-only at runtime.** `Capability.writable` is static, but
`COMMAND_UNKNOWN` is only discovered by attempting a write. `Advisory(locked=True)` already
disables a control and carries a message, so this likely needs no schema change — but the module
must be able to add an advisory *after* a failed write, and `advisories()` is currently only
consulted after reads, writes and polls. Verify it is re-read on the failure path too.

**13.3 — `ExtractInstaller` gets its first real run.** Written, never executed against an actual
installer. Expect it to need work: Poly Studio is an MSI containing nested MSIs containing
`disk1.cab`, and the catalogues live inside an Electron `app.asar` alongside them.

**Not gaps** — the two-transport split fits behind one `Device`; the catalogue maps cleanly onto
`CapabilitySet`; actions fit `Kind.ACTION` with `confirm`; battery fits `Kind.METER`; the
event-before-ack write fits inside `set()`.

## 14. Source audit

| File | Lines | Status |
|---|---:|---|
| `gui.py` | 785 | Read in full. Controls §5, labels §7, groups §8, info §9, actions §6. Tray, picker and hotplug are shell concerns. |
| `device.py` | 488 | Read in full. Becomes the adapter: session, handshake, addressing, reads, guarded writes, `drain_events`. |
| `workers.py` | 262 | Read in full. QThread model replaced by `AsyncBridge`; the 1 s event drain and the write-generation guard must survive. |
| `protocol/framing.py` | 146 | Ports verbatim. |
| `protocol/ids.py` | 74 | Ports verbatim; its data file is vendor-derived (§11). |
| `protocol/catalogue.py` | 187 | Ports verbatim; its data is vendor-derived (§11). |
| `transport/sdp.py` | 187 | Ports verbatim — own SDP client over L2CAP, because BlueZ exposes no channels. |
| `transport/rfcomm.py` | 122 | Ports verbatim. |
| `transport/hid.py` | 231 | Ports verbatim. |
| `transport/discovery.py` | 139 | Superseded by `core/discovery.py` plus a manifest match rule; the Modalias→PID trick is worth keeping. |
| `transport/hotplug.py` | 83 | Superseded — `discovery.watch()` is the project-wide answer, still unimplemented. |
| `cli.py` | 154 | Superseded by `hardware_ui/cli.py`. |
| `docs/POLY_DECKARD_PROTOCOL.md` | 621 | The protocol reference. Irreplaceable; regenerating it would cost capture sessions. |

## 15. Verified hardware

`verified`: **Poly V4310 (Voyager 4310 UC)** only, over both Bluetooth and the BT700 USB dongle.
Everything else in the 228 catalogues is `family` — the protocol should apply, nobody has tested it.
