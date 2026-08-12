# Sony MDR — behavioural specification

Extracted from `plasma-sony-v1-protocol-headphone-support/plasma_sony_headphones/gui.py`
(1,066 lines), the hardware-verified reference implementation.

**Every rule below exists because it was found necessary on real hardware.** None of it is
incidental UI polish. This file exists so the `hardware-ui` port can be checked against the
original mechanically, instead of rediscovering rules one bug report at a time — which is exactly
how the first attempt went.

Status column: ✅ ported · ⚠️ partial · ❌ missing

---

## 1. Universal rules (apply to every panel)

| # | Rule | Why | Status |
|---|---|---|---|
| 1.1 | **A write disables the controls it touches** until the device confirms | Prevents queuing a second write before the first is answered — the source of "Device did not confirm the change" cascades | ✅ |
| 1.2 | **A pending control ignores incoming state** | A refresh captured before the write lands would repaint the old value: the A→B→A flicker | ✅ |
| 1.3 | **Errors clear the in-flight set** (`clear_inflight`) | Otherwise a control stays disabled forever awaiting a confirmation that never comes | ✅ |
| 1.4 | **Programmatic updates must not trigger writes** (`_loading` guard) | Repainting a control from device state would otherwise echo a write back | ✅ |
| 1.5 | **A control is visible only if its state field is not `None`** | Older models (XM3, MDR-1000X) must not show dead controls | ✅ |
| 1.6 | **Composite writes mark *all* their fields pending**, not just the one touched | One message carries several fields; releasing only one lets the others be written from stale values | ✅ *(via `Capability.writes_with`; was wrongly marked done before it existed)* |
| 1.7 | **Periodic refresh every 30 s** (`BATTERY_POLL_MS`), via `refresh_status()` — **BlueZ only, no MDR traffic** | Battery and codec come from BlueZ and are never pushed, so nothing else would ever update them. Polling MDR instead flaps the codec on LDAC, which has almost no spare bandwidth on the config channel | ✅ |

## 2. Connection

| # | Rule | Why | Status |
|---|---|---|---|
| 2.1 | **Connect is manual — an explicit button** | *"opening the app or rescanning must never open/close the config channel by itself (that can make the headset power-cycle)"* | ✅ |
| 2.2 | Tabs are hidden per `_apply_gating`: Noise/EQ/STC/DSEE/Controls/Connectivity shown only if the matching state is not `None`. Info and Battery always shown | Older models get no dead tabs | ✅ |
| 2.3 | After a reboot-inducing write, auto-reconnect **15 s** later (`begin_reconnect`) | The link drops when the headset reboots | ✅ |
| 2.4 | Devices without the MDR config service (e.g. MDR-1000X) get a **read-only info view** | No control possible, but model/battery/codec are still worth showing | ❌ |

## 3. Noise & Ambient

- Single checkbox: **"Ambient sound (off = noise cancelling)"**. There is **no Off state** —
  `effect=OFF` is ignored by the hardware. ✅
- Level slider **1..ASM_MAX (19)**. Level 0 is the noise-cancelling end of the scale, not a valid
  ambient value; the headset pulls a sent 0 up to 1, and Sony's own app numbers ambient from 1. ✅
- Commit **on slider release**, not on every step. ✅
- Level and Focus-on-voice are **enabled only while ambient is on**. ✅
- All three fields go in **one** NC/ASM write and are marked pending together. ✅

## 4. Equalizer

- Presets from `EQ_PRESETS`; bands from `EQ_BAND_LABELS` (6: Clear Bass + 5), range
  `EQ_BAND_MIN..EQ_BAND_MAX`. ⚠️ *(labels were invented; must import)*
- Commit **on slider release**. ✅
- A preset change **or** a band change marks the preset **and all sliders** pending. ✅
- Bands editable **only** while the active preset is in `EQ_CUSTOM_PRESETS`. ✅
- **LDAC lock**: on `_EQ_LDAC_INCOMPATIBLE = {"WH-1000XM3"}`, while `codec == "LDAC"` the whole
  panel is disabled — engaging the EQ would drop A2DP to SBC (protocol doc §7.3.1). ✅
- Three distinct explanatory notes: ✅
  - LDAC-locked → *"The equalizer is only supported in SBC mode on this model. It is disabled
    while LDAC is active — set Sound Quality to 'Prioritize Stable Connection' (Connectivity tab)
    to use it."*
  - Editable → *"Drag a band and release to set your custom curve."*
  - Fixed preset → *"This preset's bands are fixed. Choose a Custom slot to edit them."*
- Band-count mismatch is tolerated (`zip(..., strict=False)`), never raised. ✅

## 5. Speak-to-Chat

- Enabled + sensitivity (`STC_SENSITIVITY`) + timeout (`STC_TIMEOUT`), all sent in **one**
  `set_stc` call and marked pending together. ✅
- Sensitivity and timeout **enabled only while speak-to-chat is on**. ✅
- Note: `set_stc` sends *two* messages (enable, then ext), so a sensitivity change re-sends
  `enabled` — it must come from confirmed state, never a stale cache. ✅

## 6. DSEE

- Single checkbox, Auto/Off only on the XM4. ✅
- Note: *"Upscales compressed audio. The XM4 offers only Auto or Off; it may be unavailable while
  an equalizer preset is active."* ✅

## 7. Controls

| Control | Rule | Status |
|---|---|---|
| CUSTOM button | **Reboots.** Confirm first; on cancel restore the previous value. Deliberately **not** in the pending guard — its value is re-read on reconnect | ✅ |
| Touch sensor | Normal pending write | ✅ |
| Pause when removed | Normal pending write | ✅ |

## 8. Connectivity

| Control | Rule | Status |
|---|---|---|
| Sound quality | **Reboots.** Confirm with extra text *"Stable Connection disables LDAC."*; revert the combo on cancel | ✅ |
| Multipoint | **Reboots.** Confirm with extra text *"LDAC cannot be used while connected to 2 devices."*; revert the checkbox on cancel | ✅ |
| Note | *"Multipoint and Stable-Connection mode disable LDAC. Changing either reconnects the headphones."* | ✅ |

Reboot confirmation body: *"Changing '{feature}' makes the headphones disconnect and reboot.
{extra} The app will reconnect automatically after a few seconds. Continue?"* — default button is
**Cancel**.

## 9. Info (read-only)

Model, Model ID, Serial, Device ID, Battery, Codec, DSEE, Version fields, Codes
(identifiers 0x02/0x04), Protocol info (hex). ✅
Serial / Device ID / Codes are selectable text. ✅ *(all readouts are selectable)*

## 10. Battery

- Progress bar 0..100. ✅
- Label `{level}%` + `" (charging)"`, or `L {left}%  R {right}%` for true-wireless models. ✅
- Auto-power-off group hidden entirely unless the device advertised `apo_options`; the option
  list is **rebuilt from the device** and labelled via `APO_ELEMENT_LABELS`. ✅

---

## Gaps in `hardware-ui` as of this writing

Ordered by user impact:

1. ~~Manual connect (2.1)~~ — done; a Connect button, nothing opens on selection.
2. ~~Reboot confirmations (7, 8)~~ — done; Cancel-default dialog with the per-setting consequence
   text, plus the 15 s reconnect.
All 11 `set_*` methods are reachable, verified by diffing `_conn.set_*` call sites in the
reference implementation against the capability keys. Gating, locks, notes and the 30 s poll are
in place.

Remaining:

1. **Read-only fallback for models without the MDR config service (2.4)** — e.g. MDR-1000X. Needs
   such a device, or a deliberate simulation, to build against.
2. **The `_loading` guard (1.4)** — implemented in `CapabilityForm`, and `QComboBox.activated`
   fires only on user interaction, so the hazard is structurally covered. Not yet proven against a
   device that pushes state mid-interaction.

Notes and locks are carried by `Advisory` (message + `locked`), supplied by the module via
`Device.advisories()` and refreshed after every read, write and poll — so the equaliser unlocks
by itself when the codec leaves LDAC.

---

## Source audit

Every file in the reference implementation, and where its behaviour went.

| File | Lines | Status |
|---|---:|---|
| `gui.py` | 1,066 | Read in full. Behaviour captured in the sections above. |
| `workers.py` | 297 | Read in full. Threading model replaced by `AsyncBridge`; the 30 s poll, `settingApplied` vs `stateChanged`, and `_apply_reboot` (send → close → reconnect, never sync a dying link) are all reproduced. One deliberate divergence — see `PORT_DIVERGENCES.md`. |
| `cli.py` | 82 | Read in full. Reimplemented as `hardware_ui/cli.py`, which also dumps capabilities, values and advisories for bug reports on untested models. |
| `device.py` | 396 | Ported verbatim as `headphones.py`. |
| `protocol/*` | 1,011 | Ported verbatim. |
| `transport/*` | 450 | Ported verbatim, one bug fix — see `PORT_DIVERGENCES.md`. |
| `app.py`, `main.py`, `enums_ui.py` | 43 | Entry points and one constant; superseded by the shell. |

Writes were checked mechanically: every `_conn.set_*` call site in `gui.py` has a corresponding
capability key. All 11 are reachable.
