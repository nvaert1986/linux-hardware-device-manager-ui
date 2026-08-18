# Project state — 2026-08-18

*A development record, not a manual.* It says what is done, what is verified against real hardware
and what is merely written, and it keeps the reasoning behind decisions that would otherwise look
arbitrary. For how to install and run the application, see [`README.md`](README.md) and
[`docs/INSTALL.md`](docs/INSTALL.md); for how a module behaves, the `docs/*_UI_BEHAVIOUR.md` files.

**Linux Hardware Device Manager UI (LHDMUI), version 0.10.1.** Renamed from the generic "Hardware"
2026-08-07; the package and desktop-entry id remain `hardware-ui` because they key the config,
cache and vendor-asset paths.

Where things stand and what to pick up next. Read this first to resume.

**Publication state.** `~/Projects/hardware-ui` is the working tree and is not a git repository;
`~/Projects/hardware-ui-prd` is the published mirror and holds the git history. `tools/publish.sh`
mirrors one into the other — it refuses to carry vendor data or personal identifiers, and it runs the
test suite *from inside the copy* before exiting. Publishing is a copy only: committing and pushing
are separate, deliberate acts. As of 2026-08-18 the mirror is current and **uncommitted**; the last
commit is `385d73f` ("0.10.1: Creative and 8BitDo modules, diagrams, tray icon").

**Session of 2026-08-18** — the camera module, and a documentation pass over everything:

- `uvc_cameras` built, verified on two cameras, and given writable streaming-mode controls (see
  below). A `v4l2` transport, a `CAMERAS` category and `video4linux` hotplug came with it.
- A Logitech BRIO stopped appearing as a gamepad. Discovery now publishes `hid_hidpp`.
- Docs: every module is in the README dependency table and the source tree (three were missing);
  `dev-python/pyusb`, `dev-python/cryptography` and the 8BitDo use of `dev-python/dbus-python` were
  **absent from `docs/INSTALL.md` entirely** and are now documented; the Jabra and Logitech behaviour
  specs were missing from the README index; the class-wide-claim exception is now correctly two
  modules; `docs/WRITING_A_MODULE.md` lists all six transports and all seven categories; a
  `uvc_cameras` section was added to `docs/PORT_DIVERGENCES.md`; test counts corrected across the
  tree. Checked mechanically afterwards: no broken relative links in any of the 18 markdown files,
  and every behaviour doc is linked from the README.
- One claim was **withdrawn** rather than kept: `docs/WRITING_A_MODULE.md` had said a guard test
  catches a forgotten `HOTPLUG_SUBSYSTEMS` entry. It does not — it pins the exact set, which makes
  changing it deliberate but catches no omission. The doc now says so, along with the matching trap
  that a new `Category` needs an icon that exists in the Breeze theme.

Backup of the pre-Dell tree: `~/Projects/hardware-ui-sony-only-backup-20260806`.
(Earlier QML backup: `~/Projects/hardware-ui-qml-backup-20260806-153858`.)

---

## Done

**12 modules, 6 transports, 7 categories, 1027 tests, `ruff` clean.**

| Module | State |
|---|---|
| Core, shell, discovery | working — 6 transports (`usb`, `hid`, `bluetooth`, `ble`, `display`, `v4l2`), hotplug over udev + BlueZ, tray icon |
| `sony_headsets` | complete, verified on XM3/XM4 |
| `dell_monitors` | complete, reads and writes verified on 2× P2425D |
| `poly_headsets` | complete, verified on a Voyager |
| `razer_peripherals` | complete, verified (via the OpenRazer daemon) |
| `dell_docks` | complete, read-only, verified on a WD22TB4 |
| `fido2_security_keys` | complete, verified against a real YubiKey |
| `yubikeys` | complete, vendor tab verified |
| `jabra_headsets` | complete, verified on a Link 390 + Evolve2 85 |
| `logitech_peripherals` | complete, verified on a Bolt receiver + MX Master 3S + MX Keys S |
| `creative_peripherals` | **experimental**, verified on a Sound Blaster X4 |
| `eightbitdo_controllers` | **experimental**, verified on an Ultimate Wired Controller for Xbox |
| `uvc_cameras` | **experimental**, verified on a Logitech BRIO and a Realtek integrated webcam |

Each module's behaviour spec is `docs/<NAME>_UI_BEHAVIOUR.md`; the authority on which devices are
tested rather than merely matched is the status table in [`README.md`](README.md).

The Dell module is `hardware_ui/modules/dell_monitors/`. Its behaviour spec —
every VCP opcode, gate and negative result, with an as-built status column — is
[`docs/DELL_UI_BEHAVIOUR.md`](docs/DELL_UI_BEHAVIOUR.md). Deliberate differences from the source
project are in [`docs/PORT_DIVERGENCES.md`](docs/PORT_DIVERGENCES.md).

### Verified on hardware 2026-08-06
2× P2425D, DisplayPort through a WD22TB4 dock, on `DPMST` buses (`/dev/i2c-15`, `/dev/i2c-16`).

- Enumeration from EDID, bus resolution, capability parse, full page in **3.3 s**
- Set-then-verify, exact: brightness 75 → 70 → 75
- **Set-then-verify, snapped**: sharpness 55 → panel landed on **60**, reported as success with
  the value the monitor actually holds. This is the case `Device.set()`'s return value exists for
- **Merged Colour Preset across all three opcodes**: Movie/Standard via `0xDC`, Warm/Custom Colour
  via `0x14`, `0xE2` tracked every one; RGB gains survived

### Verified on hardware 2026-08-07

**Multi-device, driven through the real widgets offscreen — 14/14.** Connect DP-3, connect DP-4,
first stays open; both rows read connected; returning to DP-3 repopulates its page rather than
blanking; disconnecting one leaves the other untouched; a Rescan with one open keeps it open and
still connected.

**Calibration, on DP-3 — 13/13.** The probe rediscovered this panel's documented firmware floors
from scratch: **contrast ≥ 25, RGB gain ≥ 30, sharpness in steps of 10**, brightness unrestricted
— exactly the numbers recorded in the source project's `TESTED-MONITORS-TECHNICAL.md`. The sliders
re-bounded live via `capabilities_revision`, and every setting came back to its original value.

**Leniency switches off once calibrated (rule 1.4).** With the step known, sharpness 60 confirms
exactly, and an off-step 55 is now reported as a mismatch instead of being excused as a snap —
the complement of the uncalibrated case, where the same write is success.

**Input renaming.** Editing the inline field relabels the Input Source dropdown, persists to
`input_names.json` keyed by serial, and clearing it restores the DDC name.

Both runs are reproducible: `tools/hw_multidevice.py` and `tools/hw_calibrate.py` drive the real
app offscreen against whatever monitors are attached. They need hardware, so they are not part of
`pytest`.

> **Left behind on purpose:** DP-3 (`3DMZZB4`) now has a saved calibration, so its sliders stop at
> the panel's real limits. Delete `~/.cache/hardware-ui/dell_monitors/calibration.json` to go back
> to 0–100 with step 1.

### Written but not exercised on hardware
No fault is known in any of these — there is simply no monitor here that has the feature.

- Factory reset *(the P2425D advertises it; deliberately not run — it discards settings this app
  does not manage, with no undo)*
- PIP/PBP, MST, USB KVM, monitor audio *(needs a P3424WE / P2725HE / a panel with speakers)*
- The new-spec `0xEF` MST toggle — no such monitor exists to test against, badged experimental

---

## Poly module — verified on hardware 2026-08-10

**One** Voyager 4310 with one BT700. **Every tab configures the headset**, confirmed on the test bench: Audio, Mute,
Calls & Prompts, Hearing Safety, General, Maintenance, plus battery and full identity. Both the
dongle and the charging-stand route now reach the same headset and offer the same settings.

**Three defects the hardware found, none of which any test could have:**

1. **A hardcoded report size.** The transport assumed the 503-byte output report a BT700 declares.
   A Voyager 4310 on its own USB connection declares **62**, so a 504-byte transfer stalled and
   surfaced as `SET_REPORT failed: [Errno 32] Broken pipe` — indistinguishable from an absent
   device. The size is now read from each device's own report descriptor.
2. **`_attach_downstream` walked sideways.** Ported verbatim, and correct for its original
   assumption: a dongle with a headset behind it. Poly links are symmetric, so a headset in its
   stand also reports a downstream device — the dongle it is paired to. The stand's entry
   therefore configured the *dongle* and showed the dongle's serial as the headset's. It now
   refuses a downstream device whose USB product id belongs to something separately plugged in,
   and refuses a port that names no product at all (a dongle lists ports it *could* use).
3. **`<SettingUnsupported>` on the page.** The reference put the exception's class name in an
   identity field as a debugging aid. Absent now, as everywhere else in this project.

Two smaller ones: a raw errno reached the user inside "<device> is …", and the catch-all settings
tab was called "Other".

**Not to be forgotten:** the `GENES_GUID` field names the device at the *far end* of the link, not
the endpoint addressed. Two confident conclusions were drawn from matching it against USB serials
and both were wrong — it matched a device to its partner. Read identity through the link you mean
to use, and do not infer topology from it.

## Poly module — original notes, written before hardware

`hardware_ui/modules/poly_headsets/` (2,361 lines). Spec:
[`docs/POLY_UI_BEHAVIOUR.md`](docs/POLY_UI_BEHAVIOUR.md). 20 tests, no hardware or vendor data
needed.

The tested session layer is ported **verbatim** as `protocol/session.py` (was the reference
project's `device.py`), together with `framing`, `sdp`, `rfcomm`, `hid`, `ids` and `catalogue`.
The adapter wraps it and adds only what the shell needs: one lock per transport, the capability
mapping, and the change stream. Poly's own label and grouping tables are ported verbatim as
`labels.py`.

**Verified without the headset:**
- The import runs end to end against the real `PolyStudio-5.1.0.1111-x64.msi`: **231 catalogues**.
- **107 wrong event ids are corrected on import.** Poly's Windows catalogues name a setting's
  change event after its *get* id; a live capture proves it arrives on the *set* id. Measured over
  all 231 catalogues: 2,515 settings declare all three ids, 418 have get ≠ set, 107 of those are
  wrong. Left alone, each would fall back to a re-read instead of event confirmation — the exact
  path that produced the "setting reverts, then applies my previous choice" bug.
- Everything degrades with no vendor data: labels fall back to `humanize()`, and a headset with no
  catalogue still gets Reconnect and Re-read rather than a blank page.

**The vendor import is done on this machine** — `231` catalogues, `225` PIDs, provenance recorded.
Re-run with:

```
python3 -m hardware_ui.cli --import-vendor poly_headsets ~/Projects/PolyStudio-5.1.0.1111-x64.msi
```

The V4310 page builds from it: 31 controls across Info · Audio · Mute · Calls & Prompts ·
Hearing Safety · Other · Maintenance, with Poly's own labels ("Sidetone", "Mute On/Off Alerts").

**Untested:** everything on the wire. The V4310 is not paired and the BT700 is not plugged in.

**Four defects found and fixed on review** — worth recording, because they are what a single
writing pass costs:
1. *One USB device exposes several hidraw nodes* (seven for two Razer devices here) and only one
   carries the Deckard report. Opening the enumerated node would connect and then never answer.
   Now filtered on the report descriptor.
2. *The change stream touched the session outside its lock* — a disconnect between the drain and
   the mapping was an `AttributeError`.
3. *A refused write did not publish its advisory.* `COMMAND_UNKNOWN` is only discoverable by
   attempting the write, so that failure is exactly when the "read-only" explanation becomes
   available; publishing it only on the success path left the control silently dead.
4. *No import path existed at all.* `assets.py` was unreachable — added
   `hardware_ui.cli --import-vendor`.

**Also found: a bug in the reference project.** Its Maintenance buttons call `write_choice`,
which refuses `is_action` outright — so pressing "Restore Defaults" there raises "refusing to
issue it" rather than doing anything. Its own handover lists GUI click-testing as never done.
This module routes actions explicitly instead, keeping the guard on the generic setter.

**In-app import — built 2026-08-11.** `shell/vendor_data.py` implements `AcquireUI`, which had
been written for exactly this and which nothing implemented. Pressing Connect on a device whose
module wants vendor data it does not have now offers to import it, from a copy of the vendor's own
installer the user chooses. Declining still opens the device, with generated names; a failed
import is reported and still opens it. Nothing is fetched or read without consent, and nothing is
redistributed — the unpack lands in the user's own data directory with provenance beside it.

### Core changes this needed
- **The shell now consumes `Device.changes()`** — it was declared and nothing subscribed. Sony
  does not override it so the gap never showed; Poly polls nothing at all, so an unsolicited
  `EVENT` is the only way a headset button press reaches the screen.
- **`ExtractInstaller` learned about cabinets.** Poly Studio is a bootstrapper MSI containing a
  chained MSI whose entire payload is one 224 MB `disk1.cab`. Stopping at the MSI layer found
  nothing; it now sniffs `MSCF` and recurses.

---

## Razer module — written and hardware-verified

`hardware_ui/modules/razer_peripherals/`. Spec:
[`docs/RAZER_UI_BEHAVIOUR.md`](docs/RAZER_UI_BEHAVIOUR.md). 19 tests, none needing OpenRazer.

**OpenRazer is a requirement of the module, not of the application.** It is imported inside
`connect()` only, so an installation without it runs normally and simply cannot use this module;
opening a Razer device then explains what to install. A test asserts nothing imports it at module
scope.

**Verified live on a BlackWidow Chroma V2 and a DeathAdder V2** — reads *and writes*:
brightness 75→40→75, effect spectrum→static `#ff00ff`→spectrum (the device reported both back),
DPI 1600→800→1600, poll rate 1000→500→1000.

Scope: settings, not authoring. Effects and their colours are in; per-key mapping, layering and
lighting profiles are out — that is an RGB editor, and OpenRGB and Polychromatic do it well.

### Four things the live probe caught that would each have been a bug
1. **`hasattr` is meaningless.** Every property is on the base class and raises
   `NotImplementedError` when unsupported; the first probe reported every capability present and
   then threw. `has()` is the only valid gate.
2. **Three exception types**, including `dbus.exceptions.DBusException` — reading `logo.active`
   answers `UnknownMethod`, and that is not caught by `except NotImplementedError`. Merely calling
   `dir()` on a zone triggers it.
3. **The capability name is not the attribute name.** `lighting_scroll_*` is advertised; the
   object is `fx.misc.scroll_wheel`. Concatenation raises `AttributeError` on a working feature.
4. **Matching had to be on USB product id.** The hidraw serial is empty, and sysfs says
   `Razer Razer DeathAdder V2` where the daemon says `Razer DeathAdder V2` — the vendor prefix is
   doubled. Seven hidraw nodes resolve onto two devices.

Good news that shaped the UI: `effect`, `colors` (a 9-byte blob of three RGB triplets), `speed`
and `wave_dir` are **all readable**, so the controls show what the device is really doing instead
of starting blank.

### Core changes this needed
**One hidraw row per physical device.** A peripheral exposes a node per HID interface, and the
sidebar showed *three keyboards and four mice* for one of each. Nodes are now grouped by their
parent **USB device** — the right key, because two identical mice sit at different USB paths and
stay two rows, where grouping on vendor/product id would have merged them. This mirrors what the
OpenRazer daemon does for Polychromatic, which never enumerates hidraw at all.

**`DeviceInfo.icon_name`,** so a keyboard gets a keyboard and a mouse gets a mouse instead of both
getting the category's generic "input" icon. Classified from the USB **boot** protocol, after two
obvious signals were checked and rejected on real hardware: `bInterfaceProtocol` of the node's own
interface (the *mouse* reports the keyboard protocol, for its macro keys) and udev's
`ID_INPUT_MOUSE`/`ID_INPUT_KEYBOARD` (both devices carry both). The boot-mouse interface is present
on the mouse and absent on the keyboard, and hwdb independently agrees.

⚠️ The first version of that check read the protocol byte on *any* HID interface and classified the
keyboard as a mouse — the byte is only meaningful on subclass 1. Fixed, with a test.

**`Capability.file_dialog`** — an `ACTION` can declare that it needs a file (`"open"`/`"save"`,
with a filter and a suffix) and the shell raises the platform chooser, handing the path over as
the action's value. A module runs on the asyncio thread and must never touch a widget, so it
cannot raise one itself. Razer macro export/import uses it; Dell profiles and Poly settings export
will reuse it.

**`Kind.COLOR`** — the seventh kind, added against the schema's own advice. The alternative was
`Kind.TEXT` and asking the user to type `#00ff00`. Values are `#rrggbb` strings so `core` stays
Qt-free; the renderer opens the platform colour dialog and paints the swatch on the button.

---

## Dell docks — written, read-only, verified

`hardware_ui/modules/dell_docks/`. 9 tests. Verified on a WD22TB4: model, USB id, serial,
Thunderbolt generation/firmware/authorisation, the domain security level with an explanation of
what it means, and six firmware components read from fwupd.

**Informational by design**, and the investigation is why:

- **Firmware is fwupd's.** It already enumerates a WD22TB4 down to each hub and controller.
- **The dock's HID interfaces are a bare 36-byte vendor descriptor** — no usages, no structure.
  It is the channel fwupd updates through, so configuring by that route is a reverse-engineering
  project sitting next to firmware flashing.
- **The settings people want are not on the dock.** MAC pass-through, wake-on-dock and
  Thunderbolt security are BIOS attributes on the host. Linux exposes those through
  `dell-wmi-sysman` at `/sys/class/firmware-attributes` — a separate, worthwhile module (see
  below).

A write is refused at the module with an explanation rather than silently absent.

**One dock is one row**, and getting there took three attempts worth recording:

1. *Gate on interface count.* Rejected: a guess from one model, and any dock exposing a single
   interface would have vanished from the list entirely. Hiding hardware is worse than a duplicate.
2. *Claim both and label the companion.* Honest but still two rows, which is what the duplicate
   Razer entries had already shown to be wrong.
3. *Group them.* A WD22TB4 answers at `3-1.1.5` as itself and at `3-1.1.3.5` as a bare companion,
   and **both descend from a hub whose product string is "Dell dock"** — that hub is the dock, so
   it is the group key. Applied only to devices already classified as docks, because everything
   plugged *into* the dock descends from the same hub and must stay separate; a Razer keyboard
   does, here.

That rewrite also fixed a real bug: the grouping key was computed from `kind` **before `kind` was
assigned**, so each node was grouped using the *previous* node's classification — which briefly
produced a Thunderbolt-branded DeathAdder. `enumerate_hid` is now two passes, collect then group,
so the key is a pure function of the collected facts.

Icons: Thunderbolt docks get KDE's Thunderbolt preferences icon, USB docks the device-tree icon —
Breeze ships no docking-station icon, and a dock is one connection fanning out into several.

---

## FIDO2 security keys — written, read path verified

`hardware_ui/modules/fido2_security_keys/`. Converted from `kcm-fido-keys`, which was built and
tested against real hardware. Two tabs: **Information** (identity, CTAP capabilities, every
advertised option) and **Configuration** (PIN, CTAP 2.1 policy, test, factory reset).

Vendor-neutral through CTAP, as the KCM intended — one module for YubiKey, Nitrokey, OnlyKey,
SoloKey, Token2 and anything compliant. Uses **`dev-python/fido2`** (Yubico's pure-Python CTAP2
library) rather than the KCM's C++ `libfido2`: same coverage, no bindings to build, and it is a
per-module dependency so an installation without it runs normally.

**Read path verified on a live YubiKey 5 NFC** — AAGUID `2fc0579f-…`, CTAP `U2F_V2 / FIDO_2_0 /
FIDO_2_1_PRE`, ES256+EdDSA, PIN set with 8 attempts remaining. The gating proves itself on this
key: it reports `credentialMgmtPreview` but **not** `credMgmt`, and **no `authnrCfg`**, so the
Policy section correctly shows an explanation instead of controls that could not work.

**Verified on a live YubiKey 5 NFC:** information, the self-test, and a PIN change — all confirmed
working by the user. Spec: [`docs/FIDO2_UI_BEHAVIOUR.md`](docs/FIDO2_UI_BEHAVIOUR.md).

**Still untested:** the **factory reset**, deliberately — it erases every credential and the PIN on
a key in daily use, so it needs a spare. And the CTAP 2.1 policy controls, which no key here can
run: the YubiKey 5 has no `authnrCfg` at all.

**Not ported yet:** credential management (list, delete, rename resident credentials — the largest
remaining piece of the KCM), and the AAGUID→model-name list, which is community-maintained and
which the KCM deliberately does not ship. That is this project's `RegistryFetch` pattern exactly.

### Built to be extended
`Fido2SecurityKey` is written for subclassing: `extra_capabilities()`, `extra_values()` and
`handle_set()` are the hooks. A `yubikeys` module adding `extends = "fido2_security_keys"` inherits
every CTAP capability and adds the vendor's own — and the registry claims a device with the **most
specialised** matching module, so the key still appears exactly **once**.

### Core changes this needed
- **`Capability.prompt`** — an action declares that it needs a secret, and the shell asks for it
  and hands it over as the action's value. `"pin_change"` asks twice and checks the entries agree.
  An ACTION gets the answer as its value; anything else gets `(value, answer)`, so a slider that
  also needs a PIN keeps its number.
- **Actions report their outcome** — a tick or a cross beside the button, the detail on hover, and
  a line in the status bar, from a sentence the module returns. Every module gains this: a Dell
  calibration and a Razer macro sync were equally silent.
- **`ModuleManifest.extends` and specificity-ordered claiming.** Tested: a YubiKey goes to a
  `yubikeys` module, a Nitrokey falls back to the base, manifest discovery order cannot change the
  outcome, an `extends` cycle cannot hang, and **disabling a specialisation falls back to the base
  rather than making the device unsupported**.
- **`DependencyMissing`**, shown verbatim. `Unreachable` means the hardware is not answering and
  the shell appends "Switch it on, then Rescan" — for a missing package that advice is wrong, and
  the wrapping produced "…is OpenRazer is needed… Switch it on, then Rescan."
- **`Capability.secret`** — a `TEXT` value hidden as typed and never repainted from state, so a
  PIN cannot end up on screen after a refresh.
- **`Category.DOCKS` and `Category.SECURITY_KEYS`.** `INPUT` had been the only category a
  classified HID device could get, so a dock and a security key both landed beside the keyboard.
- **Composite devices classify by what they are.** A YubiKey's OTP interface types like a keyboard;
  whichever node sorted first used to win, so the key showed up as a keyboard.
- **Vendor gating is now enforced, not just intended.** Every shipped manifest is walked by a test
  asserting each rule carries a vendor id, a service UUID or a vendor-specific property, so a new
  module cannot claim by device class alone — a Logitech mouse landing in the Razer module fails
  the suite instead of reaching a user. Two modules are whitelisted by name:
  `fido2_security_keys`, because claiming any maker's key by FIDO usage page is the point of it, and
  `uvc_cameras`, because UVC is a class specification whose driver reports each camera's own
  controls, ranges and menu items — so an unknown webcam gets a correct page rather than a guessed
  one. The test whitelist is what stops a third being added without a decision. Documented in
  `docs/ARCHITECTURE.md` (Matching) and `docs/WRITING_A_MODULE.md` (The manifest).
  Worth keeping in mind: **a vendor id alone is often too coarse** — `dell_docks` needs the vendor
  id *and* a name containing "dock", because Dell also makes keyboards.

---

## YubiKeys — Vendor Specific Information tab built and verified

`hardware_ui/modules/yubikeys/`, the first module to use `extends`. Spec and every decision behind
it: [`docs/YUBIKEY_UI_BEHAVIOUR.md`](docs/YUBIKEY_UI_BEHAVIOUR.md). 28 tests, none needing `ykman`
or a key.

**Read live from the YubiKey 5 NFC**, over the CTAP handle the base module already holds: model
name *YubiKey 5 NFC*, firmware 5.2.6, serial, form factor Keychain (USB-A), USB interfaces
OTP/FIDO/CCID, configuration lock not set, NFC not restricted — and six application rows each
reading "Enabled over USB and NFC".

**They live on a tab of their own, `Vendor Specific Information`**, between Information and
Configuration. The Information tab is the CTAP one — the same rows for a Nitrokey or a Token2 —
and it was already long; the vendor rows made it a page nobody reads to the bottom of. The name
is deliberately vendor-neutral so a future Nitrokey or Logitech specialisation reuses it and a
reader learns the meaning once. Tab placement is a stable sort that moves that one group and
leaves every other tab, and every section inside them, exactly as the base module built it.

**Three findings worth keeping:**

1. **`ManagementSession` accepts a `FidoConnection`**, and its CTAP backend implements
   `write_config` as well as `read_config`. The whole vendor layer therefore rides on the base
   module's open `/dev/hidraw` node: no `pcscd`, no smartcard stack, no OTP keyboard interface,
   no second handle, no second permission story. Never call `ManagementSession.close()` — it
   closes the CTAP device the base module owns.
2. **`yubikit.support.get_name()` returns the real model name from the device**, offline. That
   retires the AAGUID→model `RegistryFetch` for YubiKeys; the fetched list is only needed for keys
   with no vendor module. The base module's Model row previously showed the USB product string,
   which on a YubiKey is an interface list — "Yubico YubiKey OTP+FIDO+CCID".
3. **No Yubico image CDN exists.** Product photos are WordPress uploads under dated paths that
   cannot be derived from a model name, and the store is an SPA with no `products.json`. So the
   module implements no `fetch_photo` rather than one that guesses. Same answer as Dell.

**Two defects found while building, both from subclassing:**

- `_describe()` **shadowed the base method of the same name**, silently replacing half the page —
  it failed loudly here only because the signatures differed. Renamed `_identity_rows`.
- **Appending capabilities appends sections.** The form emits a heading whenever the section
  changes as it walks the list, so the vendor rows first landed *below* the "Re-read from key"
  button, with the model name further down the page than the AAGUID. Fixed by ordering — and then
  made moot by moving them to their own tab.

**Generality is by test, not by hardware** — only one key exists here. A NEO, a Security Key with
no serial, a FIPS key with a part number, a key without NFC, a key with one application disabled
on one transport, and `ykman` missing altogether are all exercised against stand-in device info.
Nothing in the module hard-codes a product id, model name or firmware version; applications come
from `ykman`'s `CAPABILITY` enum and its own labels.

### Verified on hardware 2026-08-11

**Restart-and-reconnect on the YubiKey.** Disabling an application over USB restarts the key and
the application reopens it by itself — confirmed on the test bench. Three attempts, and the first two
theories were wrong, so the sequence is worth keeping:

1. *Suspected the dispatch* — that `writes_with` was routing past `_write_rebooting`. It was not;
   `cap.reboots` is checked first and the composite path never intercepts it.
2. **`DeviceError` skipped the reconnect.** `_write_rebooting` tolerated `TimeoutError`,
   `Unreachable` and `OSError` as "the link dropped, expected", and the module wrapped *every*
   failure in `DeviceError` — including the key restarting, which is what it had been asked to do.
   Fixed twice over: the module no longer calls an expected reboot a fault, and the shell now
   reconnects whatever the outcome, because a write that restarts a device cannot report whether
   it applied. An error there means "we do not know", not "it did not happen".
3. **The wait trusted a stale sweep.** `_await_return` asked "is it back?" against `self._devices`,
   which still listed the device we had just told to restart — so it answered yes immediately and
   reopened it mid-reset (`[Errno 19] No such device`). It now waits for the device to *leave*
   before waiting for it to return, re-enumerating each poll, and gives up on the leaving phase
   after 4 s so a device that never restarts does not hold the reconnect hostage.

**Measured on the key: back in 3–4 seconds.** That sets the constants. `REBOOT_DROP_TIMEOUT` at
4 s is the wait for it to *go*, which happens almost at once; `REBOOT_RECONNECT_TIMEOUT` at 15 s
is roughly four times what the key needs, which is the right side to err on — a slower machine or
a hub in the way costs nothing, and the only price of a generous ceiling is how long a genuinely
dead device takes to be reported.

### Verified on hardware 2026-08-10

- **Application toggles** — switching applications on and off over USB and NFC.
- **OTP slot 2** — programming and clearing it.
- **OATH accounts** — adding one, and its code refreshing on the key's own schedule.
- **Dell calibration through the GUI**, not only through `tools/hw_calibrate.py`.

**Still unwritten to:** slot 1 (destroys the factory Yubico OTP credential) and the configuration
lock code (unrecoverable if lost). Both want a second key rather than a decision.

**Core additions this needed:** `Capability.prompt_fields` (multi-field dialogs, so a modifier
lives with what it modifies) and `Capability.copyable` (a Copy button on a readout). Both are
general; the YubiKey happened to need them first.

**A defect worth remembering:** the OATH codes were read once and never again. Everything looked
right — the code was correct at the moment it appeared — and it took a user asking "shouldn't that
be a 30-second timer?" to find it. A displayed value that expires needs something scheduling its
replacement, and the schedule has to come from the device rather than from a constant.

---

## Creative module — written, not verified on hardware

Ported from `plasma-creative-x4-protocol-soundcard-support`, which reverse-engineered the protocol
against a Sound Blaster X4 and verified it there. **No Creative device has been attached to this
machine**, so every match rule is `status = "family"` and stays that way until one has been opened
through this shell. Spec: [`docs/CREATIVE_UI_BEHAVIOUR.md`](docs/CREATIVE_UI_BEHAVIOUR.md).

26 capabilities on an X4-like device: eight feature toggles, output routing, Super X-Fi and its
mode, the ten-band equaliser with preamp and presets, firmware and serial.

**The one thing that is genuinely verified here** is the unlock. The port reproduces a
challenge/response pair captured from real hardware, byte for byte — so the AES-256-GCM
reconstruction (key patching, nonce placement, plaintext range) survived the port intact. Nothing
else in the module has touched a device.

### What the port kept, deliberately

- **Both acknowledge landmines.** A Super X-Fi mode write draws two acknowledges — a failure for a
  different op, then ours with status 0 — so a failure whose op is not ours must be ignored. And
  for `SetMalcolmParameter` byte 0 is an entry count rather than an op, so that comparison must
  never gate the *success* path; doing so made every equaliser write time out. Both pinned by tests
  with the same capture bytes.
- **`UPGRADE` (83) and `FACTORY_RESET` (155) refused** unless explicitly permitted. Neither is
  exposed as a capability, so the guard is for a caller that does not exist yet.
- **The Direct Mode interlock**, which is *ours* rather than Creative's — the vendor's Equalizer
  module never references Direct Mode. Every equaliser row is gated through `requires` and an
  advisory says why.

### What changed in the port, and why

- **No hardcoded ids.** The source targets one card and names its interfaces (1, 2) and endpoints
  (`0x03`/`0x82`). Here the CDC-ACM function and its bulk endpoints are read from the descriptors,
  because the module matches the whole vendor. Justified by `CTCDC.dll`, whose only per-model table
  is a display-name lookup that does not contain the X4's own product id — the library talks to
  whatever it is handed.
- **`sync()` skips equaliser reads** when the device's `SubFeature` mask says it has none. On the
  X4 that costs nothing; on a device without one it saves twelve reads timing out at 0.5 s each.
- **The preset store is redirected** to `vendor_dir("creative_peripherals")`, the same redirection
  the Logitech module applies to Solaar's config.
- **`DependencyMissing` instead of `ImportError`.** The responder wraps a missing `cryptography` in
  an `UnlockError` to keep the source's contract; the transport unwraps exactly that case, so a
  missing package is never reported as a broken card. Narrow on purpose — a rejected challenge is
  still a transport error.

### Core changes this needed

- **`discovery.enumerate_usb()` — the fourth transport.** Walks `/sys/bus/usb/devices` for devices
  matching a signature in `_CONTROL_INTERFACES`; opens nothing. The filter is a list of exact
  `(class, subclass, protocol)` triples rather than a class test, because enumerating every USB
  device would fill the sidebar with hubs and webcams. Creative's entry is CDC-ACM; communications
  interfaces that are not ACM (ethernet, ATM, OBEX) are skipped. The 8BitDo module later added GIP
  as a second signature — see that module's section.
- **`_one_row_per_usb_device()`** — a device exposing both a HID interface and a CDC channel is
  found by both enumerators; hidraw wins, because its row carries an openable node, a device kind
  and an icon, and the CDC channel is still reachable from the USB path both rows share.
- **The first vendor-scoped udev rule.** Every other rule matches a node *type*; this one has to
  name Creative (`041e`), because claiming a USB interface needs access to the USB device and an
  unqualified `SUBSYSTEM=="usb"` rule would hand every USB device to the logged-in session.

### Open

1. **Everything above the unlock is untested on hardware.** Claiming the interfaces, the handshake
   on the wire, whether a real device answers the feature and subfeature queries at all.
2. **Does any Creative device expose hidraw?** The source project's udev rule carries a `hidraw`
   clause alongside the USB one, which is a hint and not an answer. The manifest matches both
   transports and the device opens CDC either way, so both outcomes work — but only one is real.
3. **The host-side DSP is deliberately not carried.** X-Bass and Dialog Plus are PipeWire filter
   chains hosted by child processes with their own JSON state file, because PipeWire has no
   readback — roughly 1,400 lines with no analogue here. Carrying them would need write-only
   capability support in core. Decided with the user: device-only for now, with the page saying so.
4. **Not carried either:** `store_profile`/`select_profile` (writing curves into the card's own
   slots) and the SBX / Scout Mode button writes. The button ids are in the vendor enum but the
   Windows app never sends them, so they stay unexposed rather than guessed at.
5. **Product codes still wanted** for importing presets on anything other than an X4: the folder
   listing of `C:\ProgramData\Creative\CreativeApp\Product\`, whose names are the product codes
   (`SB1815` is the X4).

## 8BitDo Xbox controllers — ported, not yet run through this shell

Ported from `~/Projects/8bitdo-cfg`, which decoded the protocol against an Ultimate Wired Controller
for Xbox and was **hardware-validated over both transports** — USB (GIP) and the controller's hidden
BLE config radio. Unlike Creative, the source work is proven; what is unproven is this port of it.
Spec: [`docs/EIGHTBITDO_UI_BEHAVIOUR.md`](docs/EIGHTBITDO_UI_BEHAVIOUR.md).

42 capability rows: profile selector, 19 remaps (17 inputs plus both back paddles), 11 toggles,
8 sliders, reset and delete.

### Verified here, with no controller attached

- **The rolling checksum chain**, against four consecutive saves captured from the vendor app:
  `0x3b6b → 0x43a0 → 0x8781 → 0xa9fd`, all three steps exact. The strongest evidence in the module.
- **Record decoding**, against a captured profile and against real slot dumps of all three slot
  states (unwritten, deleted, written).

### Two bugs found during the port

1. **`configure.py` had `PADDLE_R` at offset 124**, where `fieldmap.py` had 116. The captured record
   settles it: 116 holds `0x8000` (RT), 120 holds `0x4000` (LT), and **124 is the second copy of the
   `11 09 20 20` marker** — writing a paddle there corrupts the record. Fixed in the source project
   as well as in the port.
2. **My own `message.HEADER_LEN` was one byte wrong.** A request header is 17 bytes and a response
   header 16, because the response's `u8` status replaces the request's `u16` field. The packing was
   right and the constant was not; a test caught it.

### Core changes this needed

- **`enumerate_usb()` learned a second interface signature.** GIP is `0xFF/0x47/0xD0` and these
  controllers expose no hidraw at all, so they were undiscoverable. Matched as the full triple —
  class `0xFF` alone is what every dongle falls back to — and the matched names are reported in a
  `control_interfaces` property. A GIP device is filed under INPUT with a gamepad icon.
- **A second vendor-scoped udev rule** (`2dc8`), for the same reason as Creative's.

### Decisions worth not re-litigating

- **Matched on product id, not vendor id** — the opposite of Creative and Jabra, and deliberate.
  There is no capability query: the byte offsets *are* the capability list, and 8BitDo's other
  families use different records. A vendor-wide rule would write confident wrong offsets.
- **The active profile is a readout.** Which profile is live is chosen with the controller's own
  button; moving it from software changes the device under whoever is holding it.
- **A write with no known checksum is refused, not guessed.** Over BLE the header is unreadable, so
  the previous value comes from a USB read or from what this application last wrote, cached in
  `store.py`. Plug in once, then use Bluetooth indefinitely.
- **Raw ATT rather than BlueZ** for the BLE path. The controller refuses the notify CCCD write and
  notifies anyway; BlueZ treats the refusal as fatal and delivers nothing. Not a bug in BlueZ, and
  not fixable from its API.
- **Original artwork.** 8BitDo's renders cannot be redistributed, so the controller drawing is ours,
  measured from product photos rather than traced. Anchors are read out of the SVG, which is what
  fixes the source's overlapping dropdowns.

### Open

1. **Nothing in this port has touched a controller.** Both transports, the profile switching, the
   commit — all unexercised through this shell.
2. **The BLE scan action is not wired into the shell yet.** `transport.ble.scan()` exists and is
   deliberately not called during enumeration; the sidebar has no button for it.
3. **Only `2dc8:2002` is claimed.** Other Xbox wired models in the family very likely share the
   record, but "very likely" is not evidence — `GUI_TODO.md` §6 in the source sketches a guided
   capture-diff script for adding one.

## Cameras — written and verified on two cameras, 2026-08-18

`hardware_ui/modules/uvc_cameras/`. Spec:
[`docs/UVC_CAMERAS_UI_BEHAVIOUR.md`](docs/UVC_CAMERAS_UI_BEHAVIOUR.md). Divergences from the source
that taught it: [`docs/PORT_DIVERGENCES.md`](docs/PORT_DIVERGENCES.md).

**The first module that claims a whole transport.** `transport = "v4l2"` with no vendor id, which
required adding a member to `Transport`, a fifth enumerator to discovery, `video4linux` to
`HOTPLUG_SUBSYSTEMS`, a `Category.CAMERAS`, and `uvc_cameras` to the guard test's whitelist. The
justification is that UVC is a class specification: the kernel's driver reports which controls a
camera has with ranges, defaults and menu items, so the page is *read from the device* rather than
declared. Per-model tables cover only vendor extras.

**No source code copied, and stdlib-only.** `cameractrls` is LGPL-3.0-or-later. It was read to
learn which extension unit GUIDs, selectors, offsets and payload bytes exist — device-interface data
describing third-party firmware, not its author's expression — and those values were transcribed.
Verified mechanically rather than claimed: the only source lines the two projects share are kernel
constants from `linux/videodev2.h` and `asm-generic/ioctl.h`. Note also that LGPL-3.0-or-later may be
conveyed under GPL-3.0, so the licences are compatible in this direction regardless, and the standing
obligation is attribution — see README, "Credit where it is owed".

**Licence position re-checked 2026-08-18**, because it is the kind of claim that should not rest on
an assertion. `app-misc/cameractrls-0.6.10-r1` declares `LICENSE="LGPL-3+"` in its ebuild (the
sources carry no header and the install ships no LICENSE file, so the ebuild is the authority). A
line-by-line comparison of `uvc_cameras` against `cameractrls.py` finds exactly **14 shared lines**,
every one a constant from `linux/videodev2.h` or `asm-generic/ioctl.h` — each confirmed present in
those headers on this machine — and `videodev2.h` is dual-licensed `(GPL-2.0+ WITH
Linux-syscall-note) OR BSD-3-Clause`. Note finally that LGPL-3.0-or-later may be conveyed under
GPL-3.0, so the licences are compatible in this direction whatever view one takes of the value
tables, and "clean room" was the wrong term for this and has been removed everywhere: the source was
read in full and deliberately. The module imports `ctypes`, `fcntl`, `errno`, `os`,
`asyncio`, `dataclasses`, `logging`, `pathlib` and `typing` — nothing third-party, asserted by
inspection of every import in the package. So cameras add **no** dependency and **no** udev rule:
`systemd`'s own `70-uaccess.rules` already tags `video4linux`.

### Verified on hardware

- **Logitech BRIO** (`046d:085e`) — standard controls, plus both Logitech extension controls: field
  of view on unit 10 across all three values, status light on unit 11, each written and read back.
- **Realtek Integrated_Webcam_FHD** (`0bda:5570`) — a laptop camera with no extension units, which
  is the point of including it: the plain-UVC case, proving the standard half stands alone.
- Streaming mode exercised across every pixel format on both, 4096×2160 MJPG and 7.5 fps set and
  read back, and the `EBUSY` path provoked by streaming from the camera.

### Three findings worth keeping

1. **A camera row displaces the HID row for the same USB device.** A BRIO exposes two media-button
   bits on hidraw, which `logitech_peripherals` claimed on vendor id alone — so a webcam appeared
   under INPUT with a gamepad icon. This reverses the rule that holds everywhere else in
   `_one_row_per_device`, and it is right here because the video row carries every setting and the
   hidraw row carries none. Discovery also publishes `hid_hidpp` now, from the report descriptor,
   using Solaar's own test.
2. **The streaming-mode controls change the camera and change nothing about what applications show.**
   Measured against `ffmpeg`, VLC and GStreamer: three for three overrode a 1280×720 setting, each
   substituting a different mode. Only a capture that never calls `VIDIOC_S_FMT` keeps it, and no
   real application behaves that way. The controls stay, with the measurement stated on the control
   itself. Whether they are worth having on that basis is an open question, and the *reporting* —
   which formats exist and what each can do — is the part carrying real value.

   The chain is worth knowing for the next person who asks whether it can be fixed from outside.
   Kamoso's config holds `deviceObjectId=138`, and PipeWire object 138 is the node
   `v4l2:/dev/video5` — so it is Kamoso → `pipewiresrc` → the PipeWire daemon → `uvcvideo`.
   GStreamer itself is a library inside the application's process, with no socket and no bus name,
   so there is nothing to talk to there. PipeWire *is* a daemon and does carry
   `default.video.width` / `height` / `rate` (640/480/25, which is suspiciously what Kamoso showed),
   but those are read at daemon start rather than from the runtime `settings` metadata, and whether
   they would override this path is untested — camera access goes through the desktop portal, so
   `pipewiresrc` would not preroll from a terminal to check.
3. **Two of this module's design decisions were argued from wrong premises and had to be corrected
   on measurement.** Both are recorded in
   [`docs/UVC_CAMERAS_UI_BEHAVIOUR.md`](docs/UVC_CAMERAS_UI_BEHAVIOUR.md) §6 rather than quietly
   fixed, because the corrections are the useful part:

   - The streaming mode was first made **read-only** on the reasoning that setting a format needs
     the descriptor reopened and exclusive access. Measured: it succeeds on an idle node and
     *persists* across closing and reopening the descriptor. The real limits are narrower — `EBUSY`
     while in use, and applications renegotiating — and neither justified withholding the control.
   - The frame rate was called unreliable because a request for 15 fps came back as 30. That was the
     driver clamping to an *enumerated* rate, not general unreliability. Offering only enumerated
     values makes the substitution check an assertion rather than a hope.

   The pattern behind both: a plausible mechanism was accepted without testing it, and the test was
   cheap. The module's docs now lead with measurements.

### Open

1. **The mechanical pan/tilt half is unexercised**: relative nudges, recentre, the eight stored
   positions, and the QuickCam focus motor. Payloads are verified against `cameractrls`' constants
   by a test, which is transcription and not hardware. Needs a PTZ Pro, Group, MeetUp or Rally. The
   BRIO cannot test them — no motor, and its peripheral unit correctly declines both selectors.
2. **Razer, Dell and AnkerWork extras are carried unverified** and marked experimental in the UI.
3. **`logitech_peripherals` still claims by vendor id alone.** `hid_hidpp` exists so the rule can
   require it, but narrowing it needs a Bolt receiver attached to test the positive case, and the
   cost of getting it wrong is a receiver that vanishes rather than one reporting a clear error.

## Settled — kept for the reasoning

**Modules page — built and verified 2026-08-11.** `shell/modules_page.py`, opened from a button
beside Rescan. Three states because the registry always had three; writes through to
`modules.toml`; re-scans on close only if something changed. Draws entirely from manifests, so it
imports no module code — which is the point, since disabling a module is what you want when its
dependency is broken.

**A state audit came with it**, prompted by the question of what else was unwired. Two real gaps,
both the shape of every other bug this shell has had — per-device state held globally or not
cleaned up:

- `_teardown` cleared the handle, the poll and the watch, but not `_busy_uids` or
  `_reconnecting_uids`. A device unplugged mid-connect kept its Connect button disabled for the
  rest of the session if it returned under the same uid.
- `_connect` released the busy flag per-branch rather than in `finally`. `except Exception` does
  not catch `CancelledError` — it is a `BaseException` — so a cancelled connect leaked the uid.

Clean on audit: every `pyqtSignal` is connected, every task and timer is cancelled on teardown or
shutdown, and `shutdown` covers hotplug plus every open device.

1. **The CLI and the GUI hold separate state.** Corrected 2026-08-07 — an earlier note here
   claimed two processes on one I²C bus corrupt each other's transactions. They do not: ddcutil
   2.x locks across instances by default (`--enable-cross-instance-locks`, an flock on the
   `/dev/i2c` node). Measured: six concurrent reads on one bus all return the correct value, and
   take the same wall-clock as six sequential ones — they serialise on the lock.

   What is actually left:

   - **Stale values, the only real symptom.** The GUI reads once on connect and never polls (a
     timer over I²C is worse than the staleness). Change something from the CLI, from `ddcutil`
     by hand, or from the monitor's own menu, and the GUI shows the old value until *Re-read from
     monitor*. This is not a bus problem, which is why "contention" was the wrong name for it.
   - **Latency.** The lock makes one process wait for the other; a CLI read behind a 20-call
     snapshot blocks. Never wrong, just slow.
   - **A narrow write-verify race.** The lock is per *invocation*, and set-then-verify is two of
     them (`setvcp`, settle 0.2 s, `getvcp`). Another writer inside that window would make the
     GUI report a false mismatch for a write that landed. Needs two processes writing the same
     opcode within 200 ms.

   A D-Bus service — the source project's answer, a CLI that talks to the running GUI — fixes all
   three by making one process the owner. But only the first is a symptom anyone would notice, and
   it has cheaper fixes: re-read on window focus, or have the CLI detect a running GUI and say so.
   Decide which before building a daemon.
4. **Hotplug — done, both halves, 2026-08-10.** udev (`dev-python/pyudev`) for USB, HID and DRM;
   BlueZ over **QtDBus** for Bluetooth, which costs no new dependency because pyqt6 already ships
   it. Both debounce 400 ms before one sweep, because a single plug or a single link coming up
   emits a burst.

   `QtBluetooth` was considered and rejected on merit: on Linux `QBluetoothLocalDevice` is
   implemented over these same BlueZ signals and exposes a strict subset — connections and
   pairing-finished only, no `InterfacesAdded`/`InterfacesRemoved`, no property filtering.

   It was *also* unimportable at the time, which `pyqt6-6.11.0-r1` has since fixed — verified
   working on a live adapter 2026-08-11. That changes nothing here: the rejection was never about
   the bug.

   The subscription lives in `shell/bluetooth.py`, not in `core`: `core` imports no Qt and
   `hardware_ui.cli` depends on that. Enumeration is untouched and still uses `dbus_fast` or
   `bluetoothctl`.

   **Bluetooth verified on hardware 2026-08-10** — a headset switching on and off updates the
   list on its own, with no Rescan, and the latency was reported on the bench as "fast". The 400 ms
   settle is not noticeable in use.

   Still to confirm: a USB device being replugged. The re-enumeration itself was observed working
   before the stale-handle fix (that bug was *only* the dot staying green); what has not been
   watched since is the row going grey and coming back cleanly.

---

## Open — next session

*Updated 2026-08-18.* The numbered list further down is the older roadmap, kept because the
reasoning in it is still worth reading; entries struck through are finished.

**Needs hardware nobody here has:**

1. **Creative Audio Balance.** The only feature of the X4 the module does not carry. It is set with
   the card's own buttons, so it needs the hardware present to capture what those buttons send.
   Check ALSA first — it may simply be a mixer control — then the `AudioLevel` (35) domain byte.
2. **Logitech PTZ cameras.** The mechanical half of `uvc_cameras` — relative pan and tilt, recentre
   and the eight stored positions — plus the QuickCam focus motor, are transcribed from
   `cameractrls` and verified only against its own constants by a test. That is a transcription
   guarantee, not a hardware one. A BRIO cannot test them: no motor, and its peripheral unit
   correctly declines both selectors. Needs a PTZ Pro, Group, MeetUp or Rally.
3. **Narrowing the Logitech match rule to `hid_hidpp = "yes"`.** Discovery publishes that property
   now, read from the report descriptor using Solaar's own test — input report `0x10` at six bytes
   or `0x11` at nineteen, asked of *every* node of the device because on a Bolt receiver the node
   discovery represents the group with is not the node HID++ answers on. The manifest change is two
   lines. It is not made yet because the positive case cannot be tested without HID++ hardware
   attached, and the cost of getting it wrong is a receiver that vanishes from the sidebar rather
   than one that reports a clear error. Needs a Bolt receiver or an MX device.

**Needs somebody else's log:**

4. **The tray reappearing by itself.** A colleague reports the window coming back 30–60 seconds
   after being closed to the tray, with a Logitech BRIO selected. `Tray.show_window()` is the only
   thing in the application that can re-show a hidden window, and it now logs every call with the
   reason, as does every tray activation including the ones that are ignored. Waiting on
   `./run.sh -v` output from that machine. Note there is also **no single-instance guard**, so a
   second launch is a candidate explanation that the log will distinguish.

**A decision, not a task:**

5. **Whether the streaming-mode dropdowns should exist.** Pixel format, resolution and frame rate
   are writable in `uvc_cameras` and they genuinely change the device — proven by a capture written
   to omit `VIDIOC_S_FMT`, which received a real 1280×720 frame. But no application anybody uses
   honours them: `ffmpeg`, VLC and GStreamer each overrode a 1280×720 setting with a different mode
   of their own, three for three, and Kamoso does the same whether or not it is restarted. The
   controls carry an advisory saying exactly that. The *reporting* beside them — which formats
   exist, what each can reach — is the part with real value. Removing the three dropdowns and
   keeping the reporting is a defensible call and the user's to make.

---

### The older roadmap, kept for its reasoning

Numbered separately from the list above. Some of these are finished — struck through, with the
argument left intact where it is still the reason something is shaped the way it is.

1. **Profiles / copy-settings / export-import.** Deliberately not in the Dell module: all three
   are the same operation — apply a value set, skip what the target does not support, clamp the
   rest — which generalises to every module and belongs in the shell.
2. **Sony leftovers**, both needing hardware: a read-only fallback for MDR-1000X-class devices
   with no config service, and proving the `_loading` guard against a device that pushes state
   mid-interaction.
3. **`dell-wmi-sysman` — the real Dell settings module.** Every WMI GUID the driver needs is
   present on this Precision 3581 (`A80593CE…`, `F1DDEE52…`, `8A42EA14…`, `70FE8229…`,
   `0894B8D6…`) and `CONFIG_DELL_WMI_SYSMAN=y` is built into the kernel — yet
   `/sys/class/firmware-attributes/` is empty, so the driver has not bound. Find out why. If it
   can be made to bind, BIOS attributes become readable and writable through a documented,
   kernel-maintained sysfs interface — no reverse engineering — covering MAC address
   pass-through, wake-on-dock and Thunderbolt security.
4. **The `yubikeys` module's write half.** The information tab is **built and verified on the
   YubiKey 5 NFC** — model name, firmware, serial, form factor, USB interfaces, configuration
   lock, and one row per application showing which transports it answers on. Spec and decisions:
   [`docs/YUBIKEY_UI_BEHAVIOUR.md`](docs/YUBIKEY_UI_BEHAVIOUR.md).

   Still to do, in order: the **connection fallback** for a key whose FIDO interface is off
   (§2 — it must land *before* the toggles, or switching FIDO off strands the key), then the
   **application toggles** (§6), then the lock code (§7) and timeouts (§8). After that the
   **OTP slots** (§11a–§11b): status, challenge-response HMAC-SHA1, static password, swap, delete
   and NDEF.

   **Scope settled: anything on the smartcard interface is out** — OATH, PIV and OpenPGP, including
   OpenPGP touch policy. `ykman` opens the smartcard *exclusively* by default, so holding it locks
   out `gpg-agent`/`scdaemon`/Kleopatra and `scdaemon` holding it first locks out us. Kleopatra,
   GnuPG and yubioath already own that interface and do it better. The payoff is that this module
   depends on **no daemon at all**: the CTAP handle it already holds, plus the OTP interface behind
   one udev rule.

   Read live from the key while scoping: **slot 1 programmed and touch-triggered, slot 2 empty**.
   Slot 1 is the factory Yubico OTP credential — it typed an OTP into a chat window during this
   project when the key was brushed. So: never default to slot 1, and a challenge-response secret
   overwritten is gone, taking any LUKS volume enrolled against it. The **serial joins the
   interfaces** — verified identical over CTAP, OTP and CCID — and is the only reliable way to bind
   the OTP interface to the same physical key when several are attached.

   Background on the library choice: **`ykman` only:**
   `ykpers` and `libyubikey` were both archived by Yubico on 20 Feb 2025, and `libyubikey` and
   `ykclient` are validation-side anyway — they parse OTPs a key emitted, they cannot configure
   one. `app-crypt/yubikey-manager` is installed, BSD-2, and already wraps `python-fido2`.

   The finding that makes it cheap: **`ManagementSession` accepts a `FidoConnection`** and can
   both read *and* write config over it, so the whole application matrix works on the CTAP handle
   the base module already opens — no `pcscd`, no keyboard interface, no second handle. Confirmed
   read-only on the attached key. `yubikit.support.get_name()` also returns the real model name
   offline, which retires the AAGUID-list fetch for YubiKeys.

   The one decision to get right is the match rule: **vendor id alone, not vendor id + FIDO usage
   page** — disabling FIDO is an operation this module offers, and a usage-page rule would let
   that setting hide its own undo.
5. ~~A Jabra module.~~ **DONE — complete and verified on a Link 390 + Evolve2 85.** Ported
   2026-08-11. `hardware_ui/modules/jabra_headsets/`, from the
   standalone `~/Projects/plasma-jabra-headphone-support`. The protocol, transport, interpreter and
   catalogue layers came across near-verbatim — they encode hardware findings, not design choices,
   and the source project's own protocol tests came with them (53 pass, 18 skip without the
   catalogue). New here: `assets.py` (RegistryFetch for the ISC catalogue), `capabilities.py`
   (property -> control), `device.py` (the async ABC adapter), 26 tests.

   The whole module came across: protocol, interpreter, transport, capability cache, the vendor
   label layer (`labels.py`, from Jabra's Android string pools), equalizer, adapter settings,
   battery, event-only live state, telephony-derived mute/call state, and the opt-in photo. Only
   the GUI, `keyfix.py` and the source's own hotplug are dropped, all replaced by the shell. An earlier pass dropped
   both on the strength of `docs/STATUS.md` saying the equalizer was "not yet wired to a widget" —
   wrong, and the same stale file that claimed the GUI did not exist. **Read the app, not its
   status doc**; `gui.py` builds an equalizer tab and `dump_baseline.py` records per-endpoint reads
   at both `0x01` and `0x04`. The controller gained an optional `address=` on its read/write
   surface so either endpoint can be addressed, with per-endpoint capability caching.

   **Verified on hardware 2026-08-11** — Link 390 + Evolve2 85 + deskstand, all settings working.
   Nine bugs came out of that session and every one was in glue written from reasoning rather than
   read from the source: two `_route` methods shadowing each other, `format_value`/`unit`/
   `language_name`/`language_choices` ported but never called, dict values rendered as one
   enormous row, Flat not repainting, `CONNECT_TIMEOUT` shorter than a cold connect, and `&` eaten
   as a Qt mnemonic in tab labels.

   The lasting one: **GN Audio's catalogue over-promises.** It describes their whole range, so it
   offers values a given model refuses — an Evolve2 85 declares `hearThroughLevel` −12..6 and takes
   −12..0. Only a write finds out, and the source project relearned it every session. Findings are
   now cached per (product id, firmware) beside the capability cache. See
   `docs/JABRA_UI_BEHAVIOUR.md` §6a.

5e. **Logitech on-board profiles — advised, not controlled; needs G-series hardware.** A profile
   stored on the device "controls report rate, sensitivity and button actions" (Solaar's words), so
   while one is active a live write to those settings can be accepted and then ignored, which reads
   as this application reverting the control by itself. Every governed setting therefore carries an
   advisory when the device advertises `ONBOARD_PROFILES` (0x8100) *and* function `0x20` reports
   profile mode on — see `docs/LOGITECH_UI_BEHAVIOUR.md` §4c. Nothing is hidden and nothing blocked.

   What is missing is the `onboard_profiles` **control**, which would let the profile be switched
   off from here. It writes through `profile_change()` and moves device state wholesale, and no
   G-series device exists here to run it against; the advisory path is exercised by fakes only,
   because neither tested peripheral advertises the feature. Wanted, once there is hardware:
   surface the control, then verify that disabling the profile actually makes a DPI write stick.

   Rejected along the way: gating the module to non-G-series by name. The module's whole premise is
   that there is no per-model code — capabilities come from what the device answers — and a name
   gate would also refuse G-series devices whose profiles are already disabled.

5d. **Logitech's slot walk is 2.13 s and nobody's fault.** Enumerating a receiver's six pairing
   slots dominates discovery — every transport combined is 0.02 s. The library asks all six and the
   four empty ones time out. Memoised against ``(receiver serial, connected count)`` so it happens
   once per session, and children are in the discovery cache so a warm start paints instantly, but
   the first sweep of a session is still visibly slower with a receiver attached. Documented in the
   README, `docs/INSTALL.md` and `docs/LOGITECH_UI_BEHAVIOUR.md` §4b rather than hidden.

   If it ever needs fixing, in order of preference: ask the receiver which slots are occupied
   instead of walking all six (needs checking whether HID++ register 0x02 carries an occupancy
   bitfield); make the expansion non-blocking; or patch the library's per-request timeout — crude,
   and it risks calling a slow-but-present device absent, which the Jabra probe timeout already
   taught. A patch would survive re-import (that is what the patch set is for) but is a maintenance
   liability.

5c. **Logitech RGB, to Razer's standard — wanted, needs hardware.** Every lighting setting is
   already rendered, but as Solaar shapes them, which is thinner than the Razer page: `led_control`
   and `rgb_control` are a CHOICE of effect *by name*, `led_zone_` and `rgb_zone_` are TOGGLEs, and
   there is **no `Kind.COLOR` anywhere**. Razer gets swatches, a second colour, speed and per-zone
   brightness because OpenRazer exposes them as structured values; Solaar keeps the colour inside
   the effect's parameters and edits it with a dedicated widget.

   So this is a missing *mapping*, not a missing setting: read Solaar's effect-parameter model and
   project it onto `Kind.COLOR` plus speed and brightness, the way `capabilities.py` already does
   for Razer. `per-key-lighting` is a MAP_CHOICE of up to 117 keys and needs the compact control
   before it can be shown at all. Completely untestable without an RGB Logitech device — none is
   available here yet.

5b. **A diversion rule engine — deliberately not built, revisit later.** Logitech's
   `divert-keys` and `gesture2-divert` tell a key to stop doing its normal job so that *software*
   can decide what it does. Solaar ships that software: `diversion.py` matches conditions (`Key`,
   `KeyIsDown`, `Modifiers`, `Process`, `MouseGesture`, `Feature`) and fires actions (`KeyPress`,
   `MouseClick`, `MouseScroll`, `Execute`). Mouse Gestures and Sliding DPI are just built-in rules
   of that kind — Sliding DPI is a host state machine rewriting the single `dpi` value, **not**
   hardware DPI stages.

   Those settings are therefore **read-only** here, with a note on every row pointing at Solaar.
   Shown rather than hidden because the state is worth seeing; not writable because three of the
   four values would leave a physical button doing nothing.

   Building it would mean a **resident daemon**: holding the receiver open continuously, `/dev/uinput`
   for event injection with its own permissions, a rule store and editor, and exclusive ownership of
   a request/reply channel that corrupts if two processes share it. Difficulty: **hard**, and almost
   all of it is daemon and input plumbing rather than device work.

   ⚠️ **The precedent says no for now.** Jabra's `keyfix.py` was dropped for exactly this reason —
   "an input-remapping daemon, not device configuration". If it is ever wanted, the honest shape is
   a **separate daemon that hardware-ui configures**, the same relationship the Razer module has
   with OpenRazer — a project, not a feature.

   Open question worth testing first: whether running Solaar's daemon *alongside* an open Logitech
   page corrupts either, since both issue HID++ requests on one channel with no arbitration. The
   Jabra module needed a cross-process `flock` for precisely this.

6. ~~Creative Sound Blaster~~ **DONE — shipped as `creative_peripherals`, experimental, verified
   on a Sound Blaster X4.** The naming argument below was not followed: the module covers the
   card's headphone amplifier and its DSP as well, so `peripherals` was the honest word. Kept for
   the reasoning. Name it **`creative_soundcards`**: the convention is
   vendor plus device *class*, and "Sound Blaster" is a brand while "audio" is not a class. Leaves
   room for `creative_headsets` and `creative_speakers` without collision.
7. ~~A Logitech module via Solaar~~ **DONE — complete and verified on a Bolt receiver, MX Master
   3S and MX Keys S.** Vendored 2026-08-11.
   `hardware_ui/third_party/` holds a 28-file subset of `logitech_receiver` (GPL-2.0-or-later, with
   `LICENSE`, `COPYRIGHT` and every per-file header), produced by `tools/vendor_solaar.py` — pinned
   release, named patch set that fails loudly if upstream moves, provenance written, and a proof
   that it imports with `gi`/`Xlib`/`psutil`/`evdev` blocked. `--check` re-runs that proof.

   **Why vendored rather than depended on.** `app-misc/solaar` is not split, so depending on it
   installs GTK3, pygobject and python-xlib plus a tray app, for a library this Qt application only
   calls. Exactly one file needs that stack — `diversion.py`, the key-remapping rule engine — and it
   touches configuration in two call sites out of 68 setting classes.

   ⚠️ **Two earlier claims here were wrong.** (a) "Vendoring yields a library with no permissions,
   because it needs Solaar's udev rules" — false for this project, which already documents a blanket
   `SUBSYSTEM=="hidraw" TAG+="uaccess"` rule that is broader than Solaar's. The error was reasoning
   about upstream packaging in isolation instead of against this install. (b)
   `desktop_notifications.py` was described as safely guarded; its `try` catches only `ValueError`,
   so a missing pygobject raises `ImportError` straight through it. It is now dropped.

   What remains needs only `pyudev` (already used), `PyYAML` and the bundled MIT `hid_parser`.

   **Module written 2026-08-11**, `hardware_ui/modules/logitech_peripherals/`, 33 tests, not yet
   hardware-verified. Decisions taken during development: config isolated to
   `~/.config/hardware-ui/logitech.yaml` rather than written into Solaar's; one sidebar entry per
   device *plus* the receiver (free, because `hid-logitech-dj` already gives each paired device its
   own hidraw node); pair and unpair both offered, unpair behind a confirmation.

   **Bolt receivers refuse to pair from the page** and say so: Bolt shows a passkey that must be
   typed on the new keyboard *while the operation runs*, and an action button that runs to
   completion cannot show it. Lifting that needs a mid-action prompt channel in the shell.
   See `docs/LOGITECH_UI_BEHAVIOUR.md` §7.

---

## Core changes made for Dell (all general, none Dell-shaped)

| Change | Why |
|---|---|
| `Device.set()` may return the landed value | A DDC panel quantises the request and still applies it |
| `Device.capabilities_revision` | Calibration re-bounds five sliders at once; a factory reset changes every value |
| `Capability.confirm` | Disruptive but no restart — distinct from `reboots`, which promises a reconnect |
| `Capability.timeout` | A calibration writes and reads back thirty values |
| `Capability.action_label` | The default button said "Run" |
| `MatchRule.properties` | Claim displays by EDID vendor, not by a name glob |
| `Kind.TEXT` renders a line edit | It was in the schema and silently fell through to a read-only label |
| Sidebar sections | Unreachable devices under `DISCONNECTED DEVICES`, so no category heading repeats |
| Device-neutral copy | The shell said "the headphones" on a monitor's page |
