# Linux Hardware Device Manager UI (LHDMUI)

**Version 0.10.1.** One uniform interface for configuring USB, Bluetooth and DDC/CI hardware on
Linux.

The distribution, the Python package and the desktop-entry id stay `hardware-ui`: those are
identifiers, and they key the config file, the device cache and every module's vendor-asset
directory. Only the title changed.

Sony headphones, Poly headsets, Jabra headsets and DDC/CI monitors are not really different
applications. They are all *read and write typed settings on a device that advertises what it
supports*. This project treats them that way: one shell, one renderer, and per-device modules that
contribute protocol code and a list of capabilities — never UI.

PyQt6 Widgets, so Breeze draws every control and it looks like the rest of your desktop. GPLv3.

---

## Read this before you run it

**Most of this code was written by an AI — Claude, by Anthropic — working under direction.** That
is stated plainly because you are entitled to know it before you point software at hardware you
own.

It was not unreviewed, but be precise about what the review was. **Nobody read every line of this
tree.** The review that did happen was at the level of behaviour and design: decisions were argued
over rather than accepted, output was checked against what the hardware actually did, bugs found on
the bench were traced and fixed, and the tree carries 974 tests that run with no hardware attached.
That applies to the code here and doubly to `hardware_ui/third_party/`, 15,590 lines of vendored
library that is pinned, patched at five named sites and smoke-tested rather than audited. Where the
reasoning behind something is not obvious from the code it is written down: `PROJECT_STATE.md` and
the `docs/*_UI_BEHAVIOUR.md` files exist for that, including the mistakes made and corrected along
the way.

What it also does not mean is *proven*. Anything marked **verified** in the
[Verified hardware](#verified-hardware) table was exercised against the physical device, and that
table is deliberately specific about what "exercised" meant in each case — writes where the module
writes at all, reads only where it does not (the Dell dock), and a note where the verification was
done in the source project a module was ported from rather than here. But it is a handful of
devices. Everything outside that table is inference from a protocol, a vendor catalogue or another
project's code, and it can be wrong.

**Two modules are experimental even by that standard**, and are called out here rather than only in
the table so it cannot be missed:

* **Creative Sound Blaster**
* **8BitDo Xbox wired controllers**

Both are new, both are reverse-engineered rather than built on a documented protocol, and **both
will contain bugs.** Every session against real hardware so far has found one — including, in the
8BitDo case, a save that reported success, read back correctly, and was gone the moment the
controller was unplugged. They are usable and worth having; they are not settled. If you use them,
expect to find something, and know what your device was set to before you start.

**Use it at your own risk.** The GPL's disclaimer of warranty is not a formality here; it is the
accurate description. If your device is not in the table below, treat the first write as an
experiment.

---

## Status

| Area | State |
|---|---|
| Capability schema, device model | working, 64 tests |
| Module manifests, matching, folder scan | working |
| Discovery (hidraw / BlueZ / DRM+EDID) | working — 27 ms for 20 devices |
| Hotplug | **working** — udev for USB/HID/DRM, BlueZ over QtDBus for Bluetooth; the latter verified on hardware |
| QWidgets shell | working |
| **Sony WH-1000X module** | **complete, verified on XM3/XM4 hardware** |
| **Dell monitor module** | **complete; reads and writes verified on 2× P2425D** |
| **Poly headset module** | **complete; verified on one Voyager 4310 + BT700.** Several headsets at once is untested |
| **Razer module** | **complete; reads and writes verified on a keyboard and two mice.** No per-model code — every control comes from what OpenRazer reports, so an unseen model gets a correct page |
| **Dell dock module** | **complete; read-only, verified on a WD22TB4** |
| **FIDO2 security keys** | **complete, including passkeys; verified on a YubiKey 5 NFC.** Factory reset and passkey deletion untested by choice |
| **YubiKey module** | **verified on a YubiKey 5 NFC**: information, interface and application toggles, OTP slot 2, OATH accounts with live codes. Slot 1 and the lock code untested. **Needs `yubikey-manager`** — interfaces, OTP and OATH are not expressible in CTAP |
| Device photos, discovery cache | working |
| Vendor assets (`RegistryFetch`, `ExtractInstaller`) | working — offered in-app on Connect, and `cli --import-vendor`; verified on the real Poly Studio MSI |
| **Jabra module** | **complete; verified on a Link 390 + Evolve2 85 + deskstand** — 68 headset settings, the adapter's own 32, equalizer, battery, live state. Needs the vendor catalogue (ISC, fetched on consent) |
| **Logitech module** | **complete; verified on a Logi Bolt receiver, MX Master 3S and MX Keys S** — reads and writes, including per-key button remapping. Paired devices get their own sidebar entries even where no kernel driver exposes them. Solaar's library vendored GUI-free; no runtime dependency. **Adds a few seconds to the first scan** — see below. Pairing implemented but unexercised |
| **Creative module** | **EXPERIMENTAL — expect bugs.** Newest module here, and by some margin the least settled: most of what it does was worked out against one card over a single evening, and every session against real hardware so far has found something. Exercised on a Sound Blaster X4 — the CDC-ACM transport, the AES-256-GCM unlock, feature toggles, output routing, Super X-Fi, the 10-band equaliser with presets, and the card's four stored modes. Other Creative models are untested: the module matches on vendor id alone. Audio Balance is missing. Host-side DSP effects (X-Bass, Dialog Plus) are deliberately not carried — see [CREATIVE_UI_BEHAVIOUR](docs/CREATIVE_UI_BEHAVIOUR.md) §6a, §8 |
| **8BitDo module** | **EXPERIMENTAL — expect bugs.** Xbox wired controllers only, and every layer of it has had a correction found on hardware, including one that let a save look successful and survive nothing. Exercised over USB on an Ultimate Wired Controller for Xbox — read, remap, save and read-back. Three profiles, 19 remaps including the back paddles, 11 toggles, 8 sliders, written by an explicit Sync button. The BLE config radio sends the same save session but has not been run through this shell, and in config mode every 8BitDo controller advertises under one name, so an unrecognised model gets no drawings and a warning. Original controller artwork, drawn on the page with the controls around it — see [EIGHTBITDO_UI_BEHAVIOUR](docs/EIGHTBITDO_UI_BEHAVIOUR.md) §2, §7 |
| ASUS module | not started |
| Profiles / copy settings between devices | not built — belongs in the shell, not a module |
| System tray icon | **working** — Open and Quit on right-click, click to toggle. Closing the window keeps it running in the tray; Quit exits. Skipped entirely where the desktop has no tray, so it can never leave the app unquittable |
| Modules page (enable/disable UI) | **working** — Modules… beside Rescan; three states, written through to `modules.toml` |

> **Logitech receivers make the first scan slow.** With a Logitech receiver attached and the module
> enabled, the first device sweep of a session takes roughly **2–3 seconds** longer before the list
> settles. Every other transport combined takes about 0.02 s; the cost is entirely the receiver's
> slot enumeration, because the library asks all six slots and the four empty ones time out.
>
> It is paid **once**. The result is cached against the receiver's connected-device count, so later
> rescans take about 0.04 s, and the previous session's devices are painted from the discovery
> cache immediately at startup. Pairing or unpairing something changes that count and the walk
> happens again. Disabling the module on the Modules page removes the delay entirely.

## Quick start

```
emerge -av dev-python/pyqt6 kde-frameworks/breeze-icons
cp packaging/70-hardware-ui.rules /etc/udev/rules.d/   # device access, see docs/INSTALL.md
udevadm control --reload-rules && udevadm trigger
./run.sh
```

That is the whole installation. **Every device family's dependency is optional and its own**, so
the application runs with none of them — a module whose dependency is missing says so, with the
command to install it, when you press Connect on one of its devices. Nothing is silently disabled
and nothing fails with a traceback.

```
python3 -m hardware_ui.cli --all    # headless: what is detected, and what nothing claims
python3 -m hardware_ui.cli <uid>    # dump one device's capabilities, values and advisories
```

## Requirements

Everything comes from Portage. **No virtualenv, no pip, no uv** — a pip-installed module is
invisible to Portage, never rebuilt on a Python upgrade, and would stop this shipping as an ebuild.
[INSTALL](docs/INSTALL.md) has the desktop entry, udev rules and the packages deliberately *not*
used.

### Global — needed whatever hardware you own

| Package | Version | Why |
|---|---|---|
| `dev-lang/python` | **≥ 3.13** | `requires-python = ">=3.13"` |
| `dev-python/pyqt6` | ≥ 6.7 | the UI: `gui`, `widgets`, `network`. **QtDBus** comes with it and is what listens for Bluetooth hotplug |
| `kde-frameworks/breeze-icons` | — | every device and action icon |
| `dev-qt/qtbase` | — | pulled in by pyqt6; Breeze's QStyle is what makes it look native |

Nothing else. No `qasync` (no ebuild anywhere — asyncio runs on its own thread instead), no
`dbus-fast` or `dbus-next` (`PyQt6.QtDBus` ships with pyqt6), no compiled bindings of any kind.

**The application never needs root.** Access comes from udev — `uaccess` grants the device to
whoever is logged in at the seat, which is the right semantics and avoids a setuid binary or a
root daemon.

### Per module

Each row is what that one module needs. Miss it and only that family is unavailable.

| Module | Package | Also needs | Without it |
|---|---|---|---|
| `dell_monitors` | `app-misc/ddcutil` **2.x** (tested on 2.2.6) | the `i2c-dev` kernel module, and `ddcutil` working **without root** — group `i2c` plus the `uaccess` rule in [INSTALL](docs/INSTALL.md) | monitors are still *listed* — enumeration reads EDID from sysfs, never `ddcutil detect` — but nothing can be read or changed. 2.x specifically, because it takes a cross-instance lock so the CLI and this app cannot collide on a bus |
| `razer_peripherals` | `sys-apps/openrazer-daemon` | the OpenRazer kernel modules, your user in `plugdev`, and the daemon running | devices are listed but Connect explains that OpenRazer is needed. **Not mandatory** — decline it and you simply cannot use this module |
| `poly_headsets` | — (BlueZ, in the base system) | the headset **paired**, and nothing more — SDP and RFCOMM go over ordinary `AF_BLUETOOTH` sockets, no root. Vendor labels come from a one-off import of a Poly installer, needing `app-arch/msitools` (or `app-arch/7zip`) | without the import the page still builds: labels fall back to generated names and the headset keeps Reconnect and Re-read |
| `sony_headsets` | — (BlueZ, in the base system) | the headset **paired**, and nothing more | — |
| `fido2_security_keys` | `dev-python/fido2` | read/write on the key's `/dev/hidraw*` — the `uaccess` udev rule in [INSTALL](docs/INSTALL.md) | keys are listed but Connect explains the library is missing. Pure Python: no compiler, no `libfido2` bindings |
| `yubikeys` | `app-crypt/yubikey-manager` — **optional even here** | nothing further: the management application is read over the FIDO interface already open, so **no `pcscd` and no smartcard stack** | the key still works as a CTAP key. Only the vendor tab collapses to one row naming the package |
| `dell_docks` | — | nothing; reads sysfs and Thunderbolt directly | `fwupdmgr` is consulted when present for firmware detail and skipped when not |
| `eightbitdo_controllers` | `dev-python/pyusb`; `dev-python/dbus-python` for the Bluetooth path | the udev rule in [INSTALL](docs/INSTALL.md). USB is preferred and is the only way to read the configuration checksum, so connect by cable once even if you intend to use Bluetooth | devices are listed but Connect explains which package is missing |
| `creative_peripherals` | `dev-python/pyusb` and `dev-python/cryptography` | the udev rule in [INSTALL](docs/INSTALL.md) — this is the one module that *claims* a USB interface rather than opening a node. `cryptography` is not optional: the card discards every command until an AES-256-GCM handshake unlocks it | devices are listed but Connect explains which package is missing |

**Modules claim hardware by vendor, not by device class**, so a Logitech mouse can never land in
the Razer module: every rule is gated on a vendor id, a service UUID or a vendor-specific property
such as the EDID vendor code, and a test asserts it for every shipped module. The one deliberate
exception is `fido2_security_keys`, which claims a security key from *any* maker by its FIDO usage
page — that is the point of it. A YubiKey still appears once, because `yubikeys` extends it and
the more specialised module wins.

### Development

| Package | Why |
|---|---|
| `dev-python/pytest` | the test suite — 974 tests, no hardware and no vendor dependency needed |
| `dev-python/pyudev` | **hotplug** — the list updates itself as USB, HID and DRM devices come and go. A thin binding to libudev, which does the work. Without it nothing is lost but the automatic refresh: Rescan behaves exactly as before |
| `app-arch/msitools`, `app-arch/7zip` | unpacking vendor installers for `ExtractInstaller` |
| `dev-python/hatchling` | building a wheel |
| `dev-python/ruff` | the linter this tree is kept clean against |

```
emerge -av dev-python/pyqt6 kde-frameworks/breeze-icons   # everyone
emerge -av app-misc/ddcutil                               # monitors
emerge -av sys-apps/openrazer-daemon                      # Razer keyboards and mice
emerge -av dev-python/fido2                               # FIDO2 / U2F security keys
emerge -av dev-python/pyusb dev-python/cryptography       # Creative Sound Blaster
emerge -av dev-python/pyusb dev-python/dbus-python        # 8BitDo Xbox controllers
emerge -av app-crypt/yubikey-manager                      # YubiKey model, firmware, applications
emerge -av dev-python/pytest dev-python/ruff              # development
```

## Layout

```
hardware_ui/
├── core/                 device model, capability schema, discovery, assets, photos
│                         no Qt import anywhere
├── shell/                PyQt6 Widgets UI and the controller
│   ├── window.py         sidebar, tabs, connection bar
│   ├── form.py           CapabilitySet -> widgets
│   ├── app.py            device lifecycle, the write path
│   └── asyncbridge.py    asyncio on its own thread
├── modules/
│   ├── sony_headsets/    ported MDR protocol + capability declarations, no UI
│   ├── dell_monitors/    a ddcutil wrapper and a capability builder
│   ├── poly_headsets/    ported Deckard session, two transports, vendor catalogues
│   ├── razer_peripherals/  a client of the OpenRazer daemon
│   ├── dell_docks/       read-only: sysfs, Thunderbolt and fwupd
│   ├── fido2_security_keys/  vendor-neutral CTAP; a base for per-vendor modules
│   ├── yubikeys/         extends the above with ykman; the worked example of `extends`
│   ├── jabra_headsets/   ported GNP over hidraw, vendor catalogue, learned per-model limits
│   ├── logitech_peripherals/  HID++ through the vendored library; receivers expand to children
│   ├── creative_peripherals/  CDC-ACM control channel behind an AES-256-GCM unlock
│   └── eightbitdo_controllers/  one 532-byte record over USB GIP or a hidden BLE radio
├── third_party/          Solaar, vendored GUI-free and reproducibly (see `tools/vendor_solaar.py`)
└── cli.py                headless diagnostics; imports no Qt

tools/
├── vendor_solaar.py      re-vendors the subset; fails loudly if upstream moved
├── publish.sh            mirrors the tree into a clean, publishable copy
├── backup.sh             timestamped tarball, verified by restoring it
└── extract_ui_labels.py  pulls vendor wording out of a shipped bundle
```

Adding a device family means dropping a directory into `modules/` with a `module.toml`. The
registry iterates the folder; there is no registration step. Out-of-tree modules work identically
via the `hardware_ui.modules` entry point group, and users can drop their own into
`~/.local/share/hardware-ui/modules/`.

## The rules that hold it together

**Enumeration is not probing.** Startup reads sysfs and asks BlueZ for properties it already
holds — it never opens a device, connects RFCOMM or touches an I²C bus. That happens when you
press Connect.

**A module's Python is not imported until one of its devices is opened.** Manifests are TOML;
matching runs against enumeration data. Modules have no `detect()`, so no module can tax startup.

**Connection is manual.** Opening a device's config channel by itself can make a headset
power-cycle, so selecting a device never opens anything.

**No setting value is ever cached.** The vendor's phone app can change settings while this app is
closed, so values are read from the device on every connect. Identity and capability data *is*
cached, keyed on the device's advertised function list.

## Documentation

| | |
|---|---|
| [CHANGELOG](CHANGELOG.md) | what changed in each release, and whether it affects you |
| [PROJECT_STATE](PROJECT_STATE.md) | where things stand and what to pick up next |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | how it fits together and why |
| [WRITING_A_MODULE](docs/WRITING_A_MODULE.md) | adding a device family |
| [INSTALL](docs/INSTALL.md) | dependencies, desktop entry, udev, packaging |
| [SONY_UI_BEHAVIOUR](docs/SONY_UI_BEHAVIOUR.md) | the Sony reference implementation's behaviour, rule by rule |
| [DELL_UI_BEHAVIOUR](docs/DELL_UI_BEHAVIOUR.md) | the same for Dell DDC/CI — every VCP opcode, gate and negative result |
| [POLY_UI_BEHAVIOUR](docs/POLY_UI_BEHAVIOUR.md) | the same for Poly Deckard — link discipline, id namespaces, the vendor-data decision |
| [RAZER_UI_BEHAVIOUR](docs/RAZER_UI_BEHAVIOUR.md) | the same for Razer via OpenRazer — capability gating, DPI stages, macros, licensing |
| [FIDO2_UI_BEHAVIOUR](docs/FIDO2_UI_BEHAVIOUR.md) | the same for CTAP security keys — PIN handling, gating, and the base-module design |
| [EIGHTBITDO_UI_BEHAVIOUR](docs/EIGHTBITDO_UI_BEHAVIOUR.md) | the same for 8BitDo — why a save is a session rather than a record, the checksum seed, why BlueZ cannot do the Bluetooth path, and where the artwork came from |
| [CREATIVE_UI_BEHAVIOUR](docs/CREATIVE_UI_BEHAVIOUR.md) | the same for Creative — the fourth transport, why there are no per-model tables, the unlock, and what is actually verified |
| [YUBIKEY_UI_BEHAVIOUR](docs/YUBIKEY_UI_BEHAVIOUR.md) | the YubiKey specialisation — library choice, the match-rule trap, USB interface reclaim, application toggles, OTP slots, OATH accounts |
| [PORT_DIVERGENCES](docs/PORT_DIVERGENCES.md) | where a port differs from its source, and why |

`tools/audit_port.py` checks a port against its source project mechanically. It is honest about
its limits — the composite-group check is still too loose to trust.

## Vendor data

**Nothing vendor-owned is redistributed.** Two modules need data they cannot ship, for different
reasons: Jabra's property definition is ISC but published without the licence text ISC requires,
so it is fetched from GN Audio's own publication on consent; Poly's catalogue exists only inside
Poly Studio, so the user obtains HP's installer and it is unpacked locally. Device photos follow
the same rule — user-supplied, or an opt-in download from the vendor's own advertised endpoint.

## Verified hardware

This table is the whole basis for any claim this project makes about working. "Verified" means the
listed operations were performed against the physical device — not that the code compiles, not that
tests pass, and not that the protocol documentation says it should work. Everything absent from
this table is untested, and the UI badges it as such rather than pretending otherwise.

| Device | Status |
|---|---|
| Sony WH-1000XM3 / XM4 | verified |
| Logi Bolt receiver + MX Master 3S + MX Keys S | **verified** — all three. Receiver slots; both peripherals as their own sidebar entries; per-key button remapping written and reverted on the mouse; per-key `Disable keys` switches on the keyboard; several settings exercised on both. No kernel driver claims `c548`, so the paired devices reach the sidebar only through the module's expander. Pairing is implemented but has not been run |
| Razer BlackWidow Chroma V2, DeathAdder V2, DeathAdder Elite | **verified** — the Elite had never been seen by the module and produced a correct page from OpenRazer's own report: 5 DPI stages, 2 lighting zones, its own polling rates |
| Poly Voyager 4310 + BT700 | **verified** — every settings tab writes to the headset, over both the BT700 adapter and the charging stand; adapter's own settings on their own tab. **One headset only**; two headsets or two adapters at once has never been tried |
| Jabra Evolve2 85 + Link 390 | **verified** — 68 headset settings through the dongle, the dongle's own 32 on their own tab, equalizer read and written, battery, live state, and per-model limits learned and cached. The **deskstand** is verified as its own device (9 settings); it is *not* a passthrough to the headset. A cabled Evolve2 85 has not been tried |
| YubiKey 5 NFC (`ykman` layer) | **verified** — model name, firmware, serial, form factor; enabling and disabling applications over USB and NFC, including the key restarting and the application reopening it by itself; programming and clearing OTP slot 2; adding an OATH account and its code refreshing on the key's own schedule |
| Razer BlackWidow Chroma V2 | verified — lighting, brightness, game mode, macros (record, delete, save, restore) |
| Razer DeathAdder V2 | verified — DPI (both axes), poll rate, per-zone lighting |
| Poly V4310 | module complete, not yet on the wire |
| Dell P2425D (×2) | verified here — reads, set-then-verify incl. a snapped value, merged preset |
| Dell WD22TB4 Thunderbolt dock | verified — read-only |
| YubiKey 5 NFC | **verified** — information, self-test, PIN change, and listing the passkeys stored on it |
| Any FIDO2 / U2F key | `family` — claimed by HID usage page 0xF1D0, no vendor id involved |
| Creative Sound Blaster X4 | **EXPERIMENTAL** — exercised, not settled. Unlock, every read the page makes, and writes to routing, feature toggles, Super X-Fi and its mode, the equaliser and the card's four stored modes. The module is new and every session against it has found a bug; Audio Balance is missing entirely. Treat writes as an experiment |
| 8BitDo Ultimate Wired Controller for Xbox | **EXPERIMENTAL** — exercised over USB, not settled. Read, remap, save and read-back, with the save confirmed to survive the cable coming out. Bluetooth sends the same save session but has never been run through this application. The module is new and every layer of it has had a correction found on hardware |
| Dell P2425H, P2222H, P2422H, U2412M, P2319H, P2317H, P3424WE, P2725HE | verified in the source project |
| Everything else in these families | `family` — badged as untested in the UI |

A `family` match gets capability *discovery* rather than a hardcoded table, so an untested model
reports its own feature set and gets a correct page. Both real WH-1000XM headsets are claimed by
the MDR service UUID alone, with no name rule; every Dell is claimed by its EDID vendor id. Neither
depends on the device's name, which is what makes a renamed or future model work unattended.

## Credit where it is owed

Several of the modules here are thin. That is the point, and it is only possible because other
people did the hard part first.

**[OpenRazer](https://openrazer.github.io/)** — GPL-2.0-or-later. Its authors wrote and maintain
the kernel drivers, the device database and the daemon that make Razer hardware configurable on
Linux at all. Every capability this project's Razer module offers is one OpenRazer already
decoded, implemented and keeps working across hundreds of devices and years of firmware. There is
no reverse engineering left in `razer_peripherals` because OpenRazer had already done it; the
module is a client of their API and nothing more. Without them there would be no Razer support
here, and no realistic prospect of writing any.

**[Polychromatic](https://polychromatic.app/)** — GPL-3.0-or-later, by Luke Horwell and
contributors. Its OpenRazer backend is where the awkward parts are written down: that
`supported_poll_rates` must be capability-gated before it is read, that fixed DPI lists and free
ranges are mutually exclusive branches, how default DPI stages are derived from a device's
maximum, and that most mice cannot store stages in hardware at all. Several of those were read
straight out of its source after this project got them wrong. Its DPI-stage behaviour and default
values are deliberately matched so the two applications agree.

**[GN Audio / Jabra](https://www.jabra.com/), for `@gnaudio/jabra-properties-definition`** — ISC,
published on npm. It is not a library but a specification: 423 device properties, each with the
GNP command and subcommand that carries it and the byte converters that encode and decode its
value. `jabra_headsets` implements one interpreter for that format and gets every property, on
every model, rather than hand-coding a getter and setter per setting — so a headset nobody here has
seen produces a correct page from GN Audio's own description of it. Publishing that at all was a
choice they did not have to make.

The file is **not** redistributed with this project, and the reason is narrow rather than
disapproving: the package declares `"license": "ISC"` in its `package.json` and contains no licence
text, while ISC's own terms require a copyright notice and permission notice to travel with every
copy. Shipping it would mean writing that notice on GN Audio's behalf. So it is fetched from their
own publication instead, once, after the user is shown what is being downloaded and from where —
and the module says plainly that without it a Jabra device can be identified but not configured.
The protocol underneath it was reverse engineered from `GnProtocol.dll`; that part is not theirs to
credit.

**[Yubico](https://www.yubico.com/), for `yubikey-manager` and `python-fido2`** — BSD-2-Clause
and Apache-2.0 respectively. The security-key modules are a user interface over their libraries and
almost nothing else. `python-fido2` is a complete pure-Python CTAP2 implementation, so
`fido2_security_keys` needed no bindings, no compiler and no protocol work. `yubikey-manager` is
the whole of `yubikeys`: the management application, the OTP slots, the OATH accounts, the
`CAPABILITY` and `KEYBOARD_LAYOUT` tables, the model names, the modhex encoder, and the derivation
of a public identity from a serial number. Its own retry loop is where the three-second USB
interface reclaim was found — a behaviour that shaped this entire module and that no amount of
guessing would have produced.

**The [Yubico Authenticator](https://github.com/Yubico/yubioath-flutter)** — Apache-2.0 — is the
reference for *how these pages should behave*, and several of its decisions are adopted here
because they are better than the first attempt: one action list per OTP slot instead of a shared
target dropdown, each modifier inside the dialog of the thing it modifies, and its wording where
its wording is clearer — "Short touch" and "Long touch" rather than slot numbers, "Slot is
configured", "Enable or disable applications over available transports". Its rule that at least
one non-smartcard application must stay enabled over USB is copied outright, because it prevents a
one-way door. Its theme is not copied.

**[Solaar](https://pwr-solaar.github.io/Solaar/)** — GPL-2.0-or-later, by Daniel Pavel and
contributors. `logitech_peripherals` is a user interface over their `logitech_receiver` and almost
nothing else: the HID++ 1.0 and 2.0 protocols, 113 features, 60 setting definitions with their
wording and translations, the per-model tables, and the pairing sequence — all of it decoded,
implemented and kept working across years of Logitech firmware. There is no reverse engineering in
this module because Solaar had already done every bit of it.

Uniquely among the projects here, their code is **shipped** rather than called — see the note
below — so their licence and copyright travel with this application.

These are separate projects with their own maintainers. Bugs found here are this project's, not
theirs — please report them here rather than upstream unless the fault is genuinely in their code.

**Almost everything above is a dependency, not a copy.** OpenRazer, Yubico's libraries and PyQt6
are resolved by Portage at install time; calling a library is not redistributing it, and it raises
no licence question beyond this project's own GPL-3.0-or-later.

**One exception, and it is deliberate:** `hardware_ui/third_party/` carries a subset of
[Solaar](https://pwr-solaar.github.io/Solaar/)'s `logitech_receiver`, GPL-2.0-or-later, with its
licence text, its `COPYRIGHT` file and every per-file header intact. The reason is narrow.
`app-misc/solaar` is not split, so depending on it installs **GTK3, pygobject and python-xlib** —
an entire GUI stack, for a library this Qt application calls and a tray app it never runs. Of the
22 files in that library, exactly one (`diversion.py`, Solaar's key-remapping rule engine) requires
that stack, and it touches device configuration in two call sites out of 68 setting classes.
Dropping it leaves a library needing only `pyudev`, `PyYAML` and the bundled MIT `hid_parser`.

Nothing about that copy is hand-made: `tools/vendor_solaar.py` fetches a pinned release, takes the
subset, applies a named patch set that **fails loudly if upstream moves**, proves the result imports
with `gi`, `Xlib`, `psutil` and `evdev` blocked, and writes `PROVENANCE.md`. Re-syncing is one
command. `--check` re-runs the proof against the tree already there.

## Licence

GPL-3.0-or-later. PyQt6 is GPLv3, so this is the honest declaration rather than a permissive
licence the distributed combination would contradict.

**The application icon is not ours either.** It is Breeze's `devices/64/audio-card.svg`, copied
verbatim, **LGPL-3.0-or-later**, © 2014 Uri Herrera and the KDE community — a different licence
from the rest of this project, and one that stays with the artwork. Its text and upstream notice
sit beside it in `packaging/icons/` and `hardware_ui/shell/icon/`.

`hardware_ui/third_party/` is not ours. It is a subset of [Solaar](https://github.com/pwr-Solaar/Solaar)
by Daniel Pavel and contributors, GPL-2.0-**or-later** — the "or later" is what permits combining it
into a GPL-3 application, and it comes from the per-file headers rather than from the bundled
`COPYRIGHT`, which says plain "version 2". Inside it, `hid_parser` is MIT by Filipe Laíns and
carries its own notice. `hardware_ui/third_party/PROVENANCE.md` records the pinned version, its
checksum, every file omitted, every patch applied and why — and `tools/vendor_solaar.py` regenerates
all of it, refusing to run if a patch no longer applies.
