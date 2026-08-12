# Dell DDC/CI — behavioural specification

Extracted from `plasma-dell-monitor-support` v1.3.1, the hardware-verified reference
implementation: `gui.py` (2,837 lines), `features.py` (508), `ddcutil_backend.py` (445),
`calibration.py`, `profiles.py`, `input_names.py`, `app_settings.py`, `workers.py`, `cli.py`, and
all nine documentation files. Nine Dell models have been exercised against it on real hardware.

**Every rule below exists because it was found necessary on real hardware.** This file was written
*before* any adapter code, so the port could be checked against the original mechanically rather
than rediscovering rules one bug report at a time — which is exactly how the Sony port went. It is
now also the record of what was built: `hardware_ui/modules/dell_monitors/` implements it, and
`tests/test_dell_monitors.py` drives nine models' capability strings through it.

Status: ✅ ported · ⚠️ delivered differently (see [`PORT_DIVERGENCES`](PORT_DIVERGENCES.md)) ·
🚫 deliberately out of scope for this module · ❌ not ported

Mechanism: how the rule is delivered — **schema** (a `Capability` field), **module** (inside
`device.py`, invisible to the shell), **shell** (implemented once, inherited free), or **core+**
(needed a change to `hardware_ui/core` or the shell — all three landed, see §20).

---

## 1. Universal rules

| # | Rule | Why | Mechanism | Status |
|---|---|---|---|---|
| 1.1 | **Every write is `setvcp --noverify`, followed by an explicit `getvcp` read-back** | ddcutil's own verify is not enough, and exit code 0 does **not** mean applied — some panels ACK a write and ignore it (OSD-vs-DDC firmware split) | module | ✅ |
| 1.2 | **Wait `_SETTLE_SECONDS = 0.2` between the write and the read-back** | The panel needs a moment; reading immediately returns the old value | module | ✅ |
| 1.3 | **A snapped read-back is success, not failure** (`lenient`) — if the value changed from the previous one but is not what was asked, the write *took*, the panel quantised it | Sharpness moves in steps of 10 on the P2425D. Reporting "failed" for a change that visibly applied is worse than showing the snapped number | core+ (§20.1) | ✅ |
| 1.4 | **After calibration, leniency is switched off** — a calibrated control only ever sends valid steps, so the read-back must match exactly | Leniency would mask a genuine mismatch once the ranges are known | module | ✅ |
| 1.5 | **A control repaints to whatever the monitor reports**, never to what was requested | The monitor is the source of truth; `last_good` is only ever assigned from a read | core+ (§20.1) | ✅ |
| 1.6 | **All DDC I/O is serialised off the UI thread** | I²C is slow, and a bus is a single serial resource. (Two ddcutil *processes* are safe — 2.x flocks the `/dev/i2c` node by default and they queue. This rule is about our own threads and about not queueing behind ourselves.) | shell (`AsyncBridge`) + module lock | ✅ |
| 1.7 | **Continuous controls commit on slider *release* / spinbox `editingFinished`**, not on every intermediate value | One I²C write per pixel of drag would wedge the bus | shell (`CapabilityForm` already does this for `RANGE`) | ✅ |
| 1.8 | **Transient read failures are retried, not treated as unsupported** (`_TRANSIENT` in the backend) | A `getvcp` timeout is normal on a busy or MST bus | module | ✅ |
| 1.9 | **Read-only MCCS features are filtered out** (`is_read_only`, e.g. `0xAA` Screen Orientation) | They report state but cannot be set; a dead control is worse than none | schema (`Kind.READOUT`) | ✅ |
| 1.10 | **Opaque manufacturer opcodes with no known value map are never shown** | An "Option 0x10" dropdown is what shipped in the first Sony attempt; do not repeat it | module | ✅ |

## 2. Detection and enumeration

| # | Rule | Why | Mechanism | Status |
|---|---|---|---|---|
| 2.1 | Only **Dell** panels (EDID manufacturer `DEL`) are controlled | The Dell value tables are reverse-engineered from DDPM and are wrong for other vendors | manifest `[[match]]` on EDID vendor | ✅ |
| 2.2 | A non-Dell monitor is **detected but shown read-only/unsupported**, never written to | Better than pretending it is absent | shell (unclaimed device already listed) | ✅ |
| 2.3 | **Laptop eDP panels and "invalid" displays are skipped** | eDP has no DDC/CI; docks/MST hubs frequently enumerate as invalid | module (`enumerate_displays` filter) | ✅ |
| 2.4 | **`ddcutil detect` is run once and sliced per bus**, never per monitor (`run_detect`) | `detect` takes no `-b`, and re-running it was ~76 % of the Information tab's cost | module | ✅ |
| 2.5 | **`capabilities` must be called with `--terse`** | Without it ddcutil's parsed output loses the raw `vcp(...)` string the whole design depends on | module | ✅ |
| 2.6 | **`vcpinfo` takes no `--bus`** | It is static MCCS metadata; passing `-b` makes it fail | module | ✅ |
| 2.7 | **Reads are batched: `ddcutil --bus N getvcp X Y Z …`** in one invocation | Measured 12 codes: 2.2 s separate vs 0.47 s batched (4.7×) direct-attached; 12.07 s vs 2.65 s (4.6×) over MST. Every ddcutil invocation re-probes the bus | core (`Device.get_many` override) | ✅ |
| 2.8 | **Enumeration uses DRM EDID, never `ddcutil detect`** | Already the project rule (ARCHITECTURE §"Enumeration is not probing"); `detect` takes seconds and can wedge a bus | shell (`discovery.enumerate_displays`) | ✅ |
| 2.9 | **Rescan** re-detects without restarting (toolbar / F5) | Hot-plug, `i2c-dev` loaded late, DDC/CI enabled in the OSD after launch | shell (Rescan exists) | ✅ |
| 2.10 | Rescan after monitors were already shown must **rebuild the loading screen safely** | 1.3.1 bugfix: it crashed with a deleted-widget error | shell | ✅ |

**Known scaling limit, inherited:** on a live 2× P2725HE MST daisy-chain full detection is 20.1 s
even batched (`detect_monitors` 3.29 + shared `run_detect` 3.36 + caps ~1.3 + `get_vcp_many` ~3.2
+ `get_monitor_info` ~2.1). The two full detect scans are 33 % and the last available lever; the
rest is inherent MST I²C latency (~0.3 s/transaction). Only a persistent `libddcutil` handle would
shave the residual — the Linux equivalent of the `dxva2` physical-monitor handle DDPM keeps open.

## 3. Tabs and ordering

`TAB_ORDER = ["Information", "Settings", "Color / Picture", "PIP / PBP", "MST", "KVM"]`.
These become `Capability.group` values verbatim — they were arrived at by someone using the
hardware, exactly as the Sony tab names were.

- `feature_category()` routes everything: `_SETTINGS_CODES = {0x60, 0xCC, 0xD6, 0x62, 0x8D}` →
  **Settings**; everything else image-related → **Color / Picture**. PIP/MST/KVM codes are routed
  explicitly and never reach it. ✅
- Within a tab, controls follow **`DISPLAY_ORDER`**, not capability-string order: ✅

  ```
  0x10 0x12 0x87              image basics (brightness, contrast, sharpness)
  0xE2 0xDC 0x14 0xF0 0xF4    presets / modes
  0x16 0x18 0x1A              RGB gain
  0x8A 0x62 0x8D              saturation / volume / mute
  0x6C 0x6E 0x70              black levels
  0x60 0xCC 0xAA 0xD6         input / OSD language / power
  ```

- **A tab is absent, not empty, when the monitor advertises nothing in it.** Same rule as Sony's
  `_apply_gating`. ✅

## 4. The merged Colour Preset

The single preset list in the OSD is split across opcodes, and the app merges them into one
Dell-style dropdown — this is the most intricate control in the project.

| # | Rule | Status |
|---|---|---|
| 4.1 | Items come from **`0xDC`** (picture modes), **`0x14`** (colour temperatures) and **`0xF0`** (ComfortView / HDR), built by `build_preset_items` into `PresetItem(label, write_code, write_value, e2_value)` | ✅ |
| 4.2 | Each item **writes its own opcode** — the dropdown is one control over three registers | ✅ |
| 4.3 | **`0xE2` is read-only** and reports the active preset; it is used *only* to highlight the current selection. Writing it is rejected by the panel | ✅ |
| 4.4 | The merge is offered only when **`0xDC` + `0x14`** are both present | ✅ |
| 4.5 | **`0xF0 = 0x00` is a dead value and is dropped** — you leave ComfortView by choosing another preset, not by switching it off | ✅ |
| 4.6 | **`0xF0` counts only if advertised in the capability string**, even when it reads `0x00` (P2425H reads it but does not advertise it → no ComfortView) | ✅ |
| 4.7 | **A monitor with no `0xE2` still works** (U2412M). The preset writes and verifies against `0xDC`/`0x14`; it just cannot show which preset is active, so the dropdown keeps its own selection. Every `0xE2` access is guarded | ✅ |
| 4.8 | Read-back verification is against the **written opcode**, not `0xE2` | ✅ |

In our schema this is one `Capability(key="color.preset", kind=CHOICE)` whose choice ids carry the
`(code, value)` pair inside the module. No core change: a key does not have to be one opcode.

**Known gap, inherited:** on a higher-end Dell where `0xE2` *is* the writable master preset with no
`0xDC`/`0x14`, the app shows **no preset control at all**. Detecting that needs a capability dump
plus an `0xE2` write-probe from such a monitor.

## 5. Confirmations

`CONFIRM_CODES = {0x60, 0xD6}` — input source and power mode. Default button is **No**.

- **`0x60` Input Source** — *"Switch **Input Source** to **{label}**? This changes the monitor's
  active input; the picture may switch away from this machine."* ✅
- **`0xD6` Power Mode** — *"Set **Power Mode** to **{label}**? This can put the display into
  standby or turn it off."* ✅
- **Factory reset (`0x04`)** — its own confirmation; write-only, cannot be read back, so the panel
  is re-read **4 s** later (`QTimer.singleShot(4000, refresh_panel)`). Confirmed working on the
  U2412M. ✅
- **USB-C Prioritization (`0xEA`)** — confirmation because applying it re-negotiates the link (the
  screen may blank, USB devices reconnect). ✅
- **PIP/PBP mode**, **MST toggle**, **USB-upstream pairing** — each confirms before writing. ✅
- **Exit** confirms; **closing the window minimises to the tray** instead of quitting. 🚫 (shell
  policy, not per-module)

These map onto `Capability.confirm_detail`. Note the difference from Sony: none of these
*reboot* the device in the Sony sense (the link does not die), so they take the normal
`_write` path — except PIP/PBP, which is closer to a reboot write (§8).

## 6. Continuous controls (brightness, contrast, sharpness, RGB gain, volume)

| # | Rule | Status |
|---|---|---|
| 6.1 | Slider **plus** spinbox, mirrored, both snapped to the step while dragging | ✅ |
| 6.2 | Uncalibrated bounds are `0 … max(reading.maximum, 1)`, step 1, `lenient = True` | ✅ |
| 6.3 | Calibrated bounds come from the saved `Range`, `lenient = False` | ✅ |
| 6.4 | `_snap()` clamps **and** rounds to the nearest valid step before sending | ✅ |
| 6.5 | Page step is `max(step, span // 10)` | 🚫 |

Our `Kind.RANGE` already carries `minimum` / `maximum` / `step` / `unit`, so 6.2–6.4 are a
capability rebuild after calibration rather than widget code.

## 7. Range calibration

DDC/CI can report a **maximum** but never a minimum or a step, yet panels clamp and quantise:
the P2425D refuses contrast below 25 and gain below 30, and moves sharpness only in steps of 10.

| # | Rule | Status |
|---|---|---|
| 7.1 | Calibration is **optional and explicit** — a button, never automatic | ✅ *(verified through the GUI as well as `tools/hw_calibrate.py`)* |
| 7.2 | It **writes probe values** to discover the real min/max/step, then restores the originals. The screen visibly flashes for a second or two — the user must be warned before it runs | ✅ |
| 7.3 | The result is saved **per monitor, keyed by serial**, and reloaded on later launches | ✅ |
| 7.4 | After a run the affected sliders are **re-bounded live** and leniency is dropped — via `capabilities_revision` (§20.2) | ✅ |

This is the one feature with no analogue anywhere in `hardware-ui`: an `ACTION` capability whose
effect is to **change other capabilities' bounds**. See §20.2.

## 8. PIP / PBP — verified on the P3424WE

Opcodes: `0xE9` mode/command, `0xE8` sub-window input, `0xE5` read-only status.

| # | Rule | Why | Status |
|---|---|---|---|
| 8.1 | Tab shown only when **`0xE9` is advertised** (`has_pip`) | | ✅ |
| 8.2 | Mode values from the advertised `0xE9` set **minus the command values `0x01`/`0x02`** — labelled Off (`0x00`) / PIP Small (`0x21`) / PIP Large (`0x22`) / PBP (`0x24`), others `PBP (0xNN)` | `0x01`/`0x02` are commands, not selectable modes | ✅ |
| 8.3 | **Entering or leaving PIP/PBP blanks and re-initialises the panel.** The immediate read-back errors or returns a transient value | This wrongly reported "failed" for a change that had applied — the same class of bug as Sony's reboot writes | ✅ |
| 8.4 | Mode apply therefore polls with **growing delays `1.0, 1.5, 2.0, 2.5, 3.0 s`**, ignoring read errors while the monitor is away; confirmed once the written mode comes back | | ✅ |
| 8.5 | If it never returns a clean read: **"applied (monitor re-initialised — could not confirm)"** — success, not failure | | ✅ |
| 8.6 | If it returns a clean read that *disagrees*: genuine mismatch, warn | | ✅ |
| 8.7 | Size/position toggles are **fire-and-forget** (`setvcp E9 0x01`/`0x02`), then re-read the mode with delays `1.0, 1.5, 2.0` and **no equality check** — `0xE9` reflects the mode, not the command | | ✅ |
| 8.8 | Toggle buttons shown only if `0x01`/`0x02` are advertised | | ✅ |
| 8.9 | Sub-window input `0xE8` is labelled with the **`0x60` input map** (`input_labels_for`), shown only if advertised | | ✅ |
| 8.10 | `0xE5` status shown read-only if the monitor returned one | | ✅ |
| 8.11 | Note in the tab: **PIP/PBP only shows a second image with two active inputs** — monitor behaviour, not a limitation of the app | | ✅ |

## 9. MST and USB-C Prioritization

| # | Rule | Why | Status |
|---|---|---|---|
| 9.1 | The DDC MST toggle is shown **only for "new-spec" `0xEF`** monitors — an advertised `0xEF` value **≥ `0x8000`** (`has_ddc_mst_control`) | DDPM's `GetEFSupport` tests bit 15 to choose between two incompatible specs | ✅ |
| 9.2 | New spec: `0xEF` is a bitmask — **bit 4 = MST enable**, bit 5 = iMST, bit 6 = Hybrid PBP; support flags bit 15/14/13/12. Written read-modify-write via `set_vcp_bit` | | ✅ |
| 9.3 | On **old-spec** monitors the tab shows *"MST is enabled from the OSD, not over DDC"* — **not a dead toggle** | Hardware-proven on the P2725HE: `0xEF` reads `0x00` whether MST is on or off; writing `0x10` is not a legal value and is ignored; `0x01` stores but does nothing. Exhaustive test wrote every advertised value plus `0x10` to both chain members — MST never dropped. DDPM offers no MST toggle on this model either, so the gate mirrors Dell's own behaviour | ✅ |
| 9.4 | Monitors without `0xEF` show *"not available"* | | ✅ |
| 9.5 | The new-spec write path is **UNVERIFIED** — no new-spec hardware exists here. Keep it, badged | | ✅ |
| 9.6 | **USB-C Prioritization `0xEA`** is a Dell **two-level 16-bit** control (sub-code `0xF8`): High Resolution `0xF800` / High Data Speed `0xF801`, via `set_vcp_word` | | ✅ |
| 9.7 | It is **write-only** — two-level `getvcp EA` returns `FFFF` until written — so the dropdown starts **non-committal** and each choice is sent but not verified | | ✅ |
| 9.8 | It is **tied to MST**: inert without it, but with MST enabled changing it visibly reconfigures the link (user-confirmed on the P2725HE) | | ✅ |
| 9.9 | Kept **out of the tray menu** | | 🚫 |

Possible extra values once testable with MST: `F810` = FHD, `F811` = 4K.

## 10. USB KVM — verified on the P3424WE

| # | Rule | Status |
|---|---|---|
| 10.1 | KVM tab present when **`0xE7` is advertised** (`has_usb_kvm`) | ✅ |
| 10.2 | **Input switch is plain `0x60`** — the same write as Settings ▸ Input Source, re-framed. Labelled **"Switch active input"** (not "Switch keyboard/mouse to"): KB/M only visibly move when a *second* computer is on the other upstream; the monitor will not orphan KB/M to an empty upstream | ⚠️ |
| 10.3 | `0xE7` has **two mutually exclusive regimes**, chosen by the capability string | ✅ |
| 10.4 | **Bit-packed regime** (`0xE7` advertised *without* `0xFE`, values ≤ 3): 16-bit word, each input owns a 2-bit field at **`bit = 14 − 2×(index in advertised 0x60 order)`**; the 2-bit value is the USB-upstream index (`0` = USB-C, `1` = USB-B). Read `(w >> pos) & 3`; write = replace that field and `setvcp E7 <full word>` (RMW), then verify the field | ✅ |
| 10.5 | Inputs that carry USB natively (`_USB_NATIVE_INPUTS = {0x19,0x1A,0x1B,0x1C,0x1D,0x1E}` — USB-C DP-Alt / Thunderbolt) **self-pair** and get **no selector** — DP-Alt bundles video and USB on one cable, so that input's USB is physically the USB-C connection | ✅ |
| 10.6 | The advertised `0xE7` values **are** the valid upstream indices (P3424WE: `00 01` = two upstreams) | ✅ |
| 10.7 | **`0xFF0N` regime** (`0xE7` advertises `0xFE`, `isSupportedCurrentUSB`): `0xFF00` = Auto (USB follows the active input), `0xFF01…0xFF04` = pin to computer 1–4. **UNVERIFIED** — no such monitor | ✅ |
| 10.8 | Where neither regime applies, show an **OSD note**, no control | ✅ |
| 10.9 | KVM codes are excluded from copy / export / profiles / tray | 🚫 |
| 10.10 | Network KVM (`0xC6` = `01`/`8001`) is a **read-only support flag only** — the feature itself is a LAN service, not DDC | 🚫 |

P3424WE ground truth for regression: `0x60` = `{0x1B USB-C, 0x0F DP-1, 0x11 HDMI-1}`; `0xE7` caps
`00 01`; baseline `0x1400`; verified transitions `0x1400 → 0x1000 → 0x0400 → 0x1400`.

## 11. Monitor audio

`0x62` speaker volume (continuous) and `0x8D` mute (`01` = Muted, `02` = Unmuted), both on the
**Settings** tab, both capability-gated so they are invisible unless advertised. **Implemented but
never hardware-verified** — none of the nine tested monitors has speakers. Volume subrange
semantics and mute polarity are standard-MCCS but unproven. ✅ (ported as-is, badged)

## 12. Information tab

Read-only identity and status: model, serial, firmware, controller, panel technology, connection,
VCP version. Built by `get_monitor_info`, which after optimisation reads `B6 C8 C9 C0` in **one
non-terse `getvcp`** — split into per-code chunks so the decoded controller/firmware strings
survive — and reuses the shared `detect`. That took it from 1.9 s to 0.24 s per monitor. ✅

Extras: a **Copy** button beside the serial, and **Export information…** to
`Dell-<model>-<serial>.txt`. 🚫 (shell-level; `READOUT` values are already selectable text)

## 13. Features deliberately *not* controllable — do not re-add

All hardware-proven by OSD correlation with a full `0x00–0xFF` read diff, or by decompiling DDPM.
Each of these looked implementable and is not; the evidence is recorded so nobody re-opens them.

| Feature | Finding |
|---|---|
| **Aspect ratio** | P3424WE: OSD 21:9 → 4:3 changed **no** DDC code; `0x86` returns ERR. DDPM uses the Windows `DISPLAYCONFIG_SCALING_*` GPU API. On Linux that is KWin's job |
| **Input colour format (RGB/YCbCr)** | P2425D: full 245-code scan before and after an OSD toggle — **zero** registers changed. DDPM uses `SetDisplayConfig` / `DISPLAYCONFIG_COLOR_ENCODING_*`. GPU-side, not DDC |
| **Response time / overdrive** | P2425D: full scan, zero changes. DDPM writes it through the **Gaming code `0xF4`** (`{Extreme 0, Super_Fast 1, Fast 2, Normal 3, Disable 0x0E}`), which the P2425D does not advertise. Would work on a gaming Dell that does |
| **Sharpness on some models** | P3424WE: `0x87` not advertised, `getvcp` = ERR, writes ACK'd then **ignored** — at native and scaled resolution. P2725HE and P2425H advertise it and it works. Per-model firmware |
| **Gamma (PC/Mac)** | U2412M: OSD-only. Beware `0xCA` — it *looks* like it tracks Gamma but actually stores the **last OSD menu cursor position**, so it latches whatever menu item you last visited. Ignore it for correlation |
| **OSD Language on the U2412M** | Readable (`0xCC`, standard MCCS values) but `setvcp` is rejected (`DDCRC_VERIFY`) and it is not advertised. Readout only |
| **Monitor-side input renaming** | P3424WE: renamed an input from the OSD → full `0x00–0xFF` diff **empty**, no Table-type feature advertised, name not in `0x60`. DDPM writes free text via `IDeviceManagerSA.SetInputSourcelist` — a Table Write / proprietary path unreachable by `ddcutil` |

**The general rule, and the one to apply to anything new: a feature in the OSD but not in the
`vcp(...)` capability list is not controllable over DDC.**

## 14. App-side input labels

Since the monitor cannot be renamed (§13), the app keeps its own labels — *DisplayPort-1 → "Work
Laptop"* — saved per monitor in `input_names.json`, appearing in the Input Source dropdown and the
sub-window input list. Blank restores the default. ✅ — as inline `Kind.TEXT` rows rather than a
modal dialog (`PORT_DIVERGENCES.md`); it is the substitute for a feature
that provably cannot exist.

## 15. Profiles, copy, export/import

| Feature | Behaviour | Status |
|---|---|---|
| **Profiles** | 10 numbered, labelled slots per monitor ("6. Gaming"); visual settings only (brightness, contrast, sharpness, RGB gain, colour preset) | 🚫 deferred |
| **Copy to other monitors** | `plan_bulk_copy` previews what applies and what is skipped; values **clamped to each target's calibrated range**; unsupported settings and unavailable presets skipped | 🚫 deferred |
| **Export / import** | JSON `Dell-<model>-<serial>.json`, format `plasma-dell-monitor-support/settings` v1; import offers *all settings* (image + OSD language) or *image only*, and warns (skippably) about anything the target lacks | 🚫 deferred |

All three are **the same idea**: apply a set of values to a device, skipping what it does not
support and clamping what it does. That generalises to every module — a headset preset is the
same operation — so it belongs in the shell, not in `dell_monitors`. Deferred out of the first
Dell module deliberately, and noted here so it is a decision rather than an omission.

## 16. Tray and D-Bus CLI

The reference app lives in the tray with per-monitor submenus offering
`_TRAY_QUICK = (preset, 0x60, 0xCC, 0xD6)` with the current value check-marked, plus **Re-read from
monitor** and **Calibrate ranges…**. Left-click opens the window (a Wayland client cannot anchor a
popup to the tray icon — that is a plasmoid privilege). `cli.py` talks to the **running GUI** over
D-Bus (`io.github.plasma_dell_monitor`, `/Control`, methods `ListMonitors` and `Adjust`) so hotkeys
are instant and the GUI stays the single owner of the I²C bus.

🚫 for the module — but the design point is worth recording: `hardware-ui`'s own CLI currently
opens the device itself. That is safe — ddcutil serialises across processes — but the two hold
independent state, so a change made by one is invisible to the other until a re-read. ARCHITECTURE already names a D-Bus
daemon as the eventual answer; Dell is the first module where the absence is a real conflict, not
a theoretical one.

## 17. Complete gating table

Every condition that hides or disables something, in one place.

| Gate | Condition | Effect |
|---|---|---|
| Dell only | EDID mfg == `DEL` | otherwise unsupported, read-only |
| Feature present | code in the advertised `vcp(...)` list | control exists |
| Read-only | `is_read_only(code)` (MCCS) | readout, not a control |
| Merged preset | `0xDC` **and** `0x14` present | preset dropdown exists |
| Preset highlight | `0xE2` present | dropdown reflects the active preset |
| ComfortView | `0xF0` **advertised** (not merely readable) | item added |
| Sharpness | `0x87` advertised **and** RW | control exists |
| OSD language | `0xCC` advertised **and** writable | control, else readout |
| Factory reset | `has_factory_reset` (`0x04` advertised) | button exists |
| PIP tab | `has_pip` (`0xE9` advertised) | tab exists |
| PIP toggles | `0x01`/`0x02` advertised | buttons exist |
| PIP sub-input | `0xE8` advertised | control exists |
| MST toggle | `has_ddc_mst_control` — advertised `0xEF` **≥ `0x8000`** | toggle, else OSD note |
| MST tab | `0xEF` present | tab exists (note if old-spec) |
| USB-C priority | `0xEA` advertised | write-only dropdown |
| KVM tab | `has_usb_kvm` (`0xE7`) | tab exists |
| KVM `0xFF0N` | `usb_kvm_upstream_controllable` (`0xE7` caps contain `0xFE`) | Auto / computer 1–4 |
| KVM bit-packed | `usb_kvm_bitpacked` (`0xE7`, no `0xFE`, values ≤ 3) | per-input dropdowns |
| KVM self-pair | input in `_USB_NATIVE_INPUTS` | **no** selector for that input |
| Audio | `0x62` / `0x8D` advertised | volume / mute |
| Calibrated | a saved `Range` for this serial | tighter bounds, strict verify |

## 18. Notes the user must see

Carried by `note` (static) or `Advisory` (state-dependent), exactly as Sony's LDAC lock is.

- PIP/PBP: *"Only shows a second image when two inputs are active."*
- PIP/PBP: *"Switching mode briefly blanks the screen while the panel re-initialises."*
- MST, old-spec: *"MST is enabled from this monitor's OSD, not over DDC."*
- MST, no `0xEF`: *"Not available on this monitor."*
- USB-C Prioritization: *"This setting cannot be read back, so it is sent without confirmation.
  Applying it re-negotiates the link — the screen may blank and USB devices may reconnect."*
- KVM: *"The keyboard and mouse only move when a second computer is connected to the other USB
  upstream."*
- KVM: *"Switching to another computer's input makes this machine lose the picture."*
- Calibration: *"Discovering the real limits writes test values — the screen will flash for a
  second or two, then your settings are restored."*
- Uncalibrated slider: *"Some panels clamp or quantise values over DDC. Calibrate ranges to make
  the slider match the hardware."*
- Monitor audio: *"Not yet verified on hardware."*
- Any `family` monitor: the existing untested badge.

## 19. Hard lessons (from `PROJECT_STATE.md`, kept verbatim in spirit)

1. **`setvcp` exit 0 ≠ applied.** Always read back.
2. **OSD ≠ DDC.** A feature in the menu may have no opcode, or an opcode the firmware ignores.
3. **`capabilities` needs `--terse`**; `vcpinfo` rejects `-b`.
4. **`0xE2` is read-only** on every tested panel, and absent on one.
5. **One ddcutil invocation per read is the dominant cost** — batch.
6. **A blanking panel is not a failed write** — poll, tolerate errors, treat silence as success.
7. **`0xCA` is a decoy** — it stores the OSD cursor position, not a setting.
8. **Per-model firmware differs more than per-model hardware** — P2425H has sharpness, P2222H does
   not; P2725HE has it, P3424WE does not.

## 20. What this module needed from the core, and what it got

Three gaps were identified before any adapter code was written. All three are closed, and each
change is general rather than Dell-shaped.

**20.1 — `Device.set()` can report the value that actually landed.** It returns `Any | None`;
non-`None` means "this is what the device holds now" and the shell paints that instead of the
request. Hardware-confirmed the day it was written: sharpness 55 landed as 60 on a P2425D, and
the old code would have shown 55 over a monitor sitting at 60.

**20.2 — `Device.capabilities_revision`.** A module bumps it when its capability set has been
rebuilt; the shell compares after every write and repaints the whole page. Calibration re-bounds
five sliders at once, and a factory reset changes every value on the panel — neither is expressible
as "one key changed".

**20.3 — `Capability.action_label`, `confirm` and `timeout`.**
`action_label` gives an action a real button ("Calibrate ranges…" rather than "Run").
`confirm` is disruption without a restart, distinct from `reboots`, which additionally means the
write cannot be confirmed and the shell must reconnect — a monitor never does that.
`timeout` lets one capability outlive the shell's default: a calibration writes and reads back
thirty values, and entering PIP polls a blanked panel for ten seconds.

Two more that fell out of building it:

**`MatchRule.properties`** — claim by EDID vendor rather than by name glob. Three bytes the panel
cannot be renamed out of, and some models publish no name descriptor at all.

**`Kind.TEXT` renders as a `QLineEdit`.** It was in the schema and fell through to a read-only
label, so the first module to use it would have found it silently broken. Input renaming is now an
inline row instead of the origin's modal dialog.

**Not gaps** — batched reads fit `get_many`; the merged preset fits one `CHOICE` key over three
opcodes; write-only `0xEA` fits a `CHOICE` whose value is simply never read; PIP's blank-tolerant
verify lives entirely inside `set()`; the per-monitor tabs are just `group`; multiple monitors are
just multiple devices in the sidebar.

## 21. Source audit

Every file in the reference implementation, and where its behaviour is accounted for.

| File | Lines | Status |
|---|---:|---|
| `gui.py` | 2,837 | Read in full. Controls §6–§11, gating §17, confirmations §5, notes §18. Tray/profiles/copy/export §15–§16 (deferred or out of scope). |
| `features.py` | 508 | Read in full. Becomes `capabilities.py`: `FEATURE_NAMES`, `CONTINUOUS`, `ENUM_LABELS`, `DISPLAY_ORDER`, `TAB_ORDER`, the preset merge, the PIP/MST/KVM helpers and the bit-packed `0xE7` helpers. **Import these tables; do not retype them.** |
| `ddcutil_backend.py` | 445 | Read in full. Ports near-verbatim into `protocol/`: `_run`, `_TRANSIENT` retry, `get_vcp`, `get_vcp_many`, `get_vcp_word`, `set_vcp`, `set_vcp_word`, `set_vcp_bit`, `get_capabilities`, `is_read_only`, `get_monitor_info`, `run_detect`. `detect_monitors` is replaced by `discovery.enumerate_displays`. |
| `calibration.py` | 101 | Read in full. Probing logic ports; persistence moves to `$XDG_CACHE_HOME/hardware-ui/dell_monitors/`. §7, §20.2. |
| `input_names.py` | 59 | Read in full. §14. |
| `profiles.py` | 64 | Read in full. §15 — deferred, generalises to the shell. |
| `app_settings.py` | 37 | Read in full. Window/tray preferences; superseded by the shell. |
| `workers.py` | 35 | Read in full. `QThreadPool` model replaced by `AsyncBridge`. |
| `app.py`, `main.py` | 35 | Entry points; superseded by the shell. |
| `cli.py` | 124 | Read in full. §16 — the D-Bus-to-running-GUI design is the answer to bus contention, not something to reimplement per module. |
| `collect-monitor-info.sh` | 182 | Read in full. Strictly read-only capability dumper with a per-code timeout (`DDC_PROBE_TIMEOUT`, default 8 s) and a `--full` `0x00–0xFF` sweep. Worth shipping as-is — it is how a new model gets added. |
| `PROJECT_STATE.md`, `DDC_ROADMAP.md`, `TESTED-MONITORS*.md`, `README*.md`, `REQUIREMENTS.md`, `ROADMAP.md`, `INSTALL.md` | 1,600+ | Read in full. The opcode knowledge and the negative results (§13) are the irreplaceable part — regenerating them would cost hardware sessions. |

## 22. Verified hardware to carry into the manifest

`verified`: P2425D, P2425H, P2222H, P2422H, U2412M, P2319H, P2317H, P3424WE, P2725HE.
`family`: every other `DEL` EDID — the app is capability-driven, so an untested Dell gets a
correct page from its own capability string, exactly as an untested Sony gets one from its
function list.
