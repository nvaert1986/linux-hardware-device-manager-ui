# Razer peripherals — behavioural specification

Unlike the Sony, Dell and Poly modules, **nothing here is reverse-engineered.** OpenRazer already
owns the kernel drivers and the daemon; its Python client is a supported, documented API. This
module is a second consumer of that API alongside Polychromatic, not a reimplementation of it.

Sources: OpenRazer 3.12.3 (`openrazer.client`), Polychromatic 0.9.7's
`polychromatic/backends/openrazer.py` (1,718 lines) read for its gotchas, and live introspection of
a **Razer BlackWidow Chroma V2** keyboard and a **Razer DeathAdder V2** mouse.

Status: ✅ ported and verified on hardware · ⚠️ written, untested · 🚫 out of scope

Implemented as `hardware_ui/modules/razer_peripherals/`, with 19 tests that need neither hardware nor OpenRazer.

---

## 0. Scope

**In:** the settings that change how the hardware behaves — DPI, polling rate, brightness, game
mode, macro LED, battery and its power-management options, read-only identity, **and the device's
built-in lighting effects with a colour to apply them in.**

Picking *Static* and choosing a colour, or picking *Spectrum* and being done, is a setting. It is
one dropdown and one colour button per lighting zone, gated so the colour only appears for effects
that take one.

**Out, deliberately:** 🚫 an **RGB editor** — per-key colour mapping, effect layering, custom
matrices, saved profiles of the above. That is a different application and OpenRGB and
Polychromatic already do it well. The line is: *choose a built-in effect and its colour* is in,
*author your own* is out. 🚫 The tray icon; the shell has one window and that is the whole point
of it.

## 1. Licensing — checked, not assumed

| Component | Licence | Consequence |
|---|---|---|
| Polychromatic 0.9.7 | **GPL-3+** | Same licence as this project. Code reusable with attribution. |
| OpenRazer 3.12.3 | **GPL-2+** | The `+` is the whole answer: "or any later version" upgrades to GPLv3, so linking `openrazer.client` into a GPLv3 application is fine. |
| Solaar 1.1.18 (for a future Logitech module) | **GPL-2-or-later** | Compatible. Verified from the upstream source headers, *not* from packaging metadata — see below. |

**Solaar is a trap worth documenting**, because two prominent sources state it is GPL-2 and both
are artefacts of automated detection.

| Source | Says | Weight |
|---|---|---|
| GitHub's About sidebar / API | `GPL-2.0` | **None.** Produced by `licensee`, which matches the LICENSE *file text*. The GPLv2 text is byte-identical whether a project grants "only" or "or later", so this cannot distinguish them. GitHub also reports the deprecated SPDX id `GPL-2.0` rather than `GPL-2.0-only`/`GPL-2.0-or-later`. |
| Gentoo ebuild | `LICENSE="GPL-2"` | **None.** Gentoo's naming does mean v2-only, but the field is maintainer-assigned from the same LICENSE file. Compare OpenRazer, correctly tagged `GPL-2+`. |
| **`README.md`** | **"License: GPL v2+"** | **Decisive.** The project's own statement. |
| **Per-file headers** in `lib/logitech_receiver/device.py` and `lib/solaar/ui/window.py` | *"either version 2 of the License, or (at your option) any later version"* | **Decisive.** This is where the GPL says to put the version grant, and it is on the file we would import. |

⚠️ **Do not cite the "or later" phrase inside `LICENSE.txt` itself as evidence.** That file is the
stock GPLv2 text, and the phrase appears in its *"How to Apply These Terms to Your New Programs"*
appendix — sample boilerplate present in every copy of GPLv2 ever distributed, including projects
that are strictly v2-only. It says nothing about this project.

**Conclusion: Solaar is GPL-2.0-or-later**, upgradeable to GPLv3, so a Logitech module may import
`logitech_receiver` directly. No clean-room reimplementation is needed.

### How much of Solaar a Logitech module would need

Only the library — but "only the library" is smaller than what has to be *installed*.

| Piece | Needed? |
|---|---|
| `lib/logitech_receiver/` | **Yes** — the HID++ protocol library, the whole point |
| `lib/solaar/configuration.py` + `__init__.py` | **Yes**, unavoidably: `logitech_receiver/device.py` does `from solaar import configuration`. It imports only stdlib and yaml — no GTK |
| `lib/solaar/ui/`, tray, CLI | No |
| **`rules.d/42-logitech-unify-permissions.rules`** | **Yes** — and this is the one that decides the question |

The udev rules matter for a *Solaar* install: the ebuild lays them down with `udev_dorules` and
pulls `acct-group/plugdev` for the group they grant to, and without them a receiver's `/dev/hidraw*`
is not user-accessible.

⚠️ **Correction (2026-08-11).** This section previously concluded that vendoring was therefore
impossible — "a library with no permissions". That is wrong *for this project*, and the error was
reasoning about Solaar's packaging in isolation. `hardware-ui` already documents its own rule:

    SUBSYSTEM=="hidraw", MODE="0660", TAG+="uaccess"

which is broader than Solaar's device-specific rule and grants the receiver to whoever is logged in
at the seat. The permission argument against vendoring does not apply here. Verify a claim against
*this* project's install, not only against the upstream package.

**What the GTK dependency actually is.** Measured against Solaar 1.1.19, `logitech_receiver` is 22
files and 14,379 lines, and exactly **one** of them hard-requires a GUI stack:

| File | GUI import | Guarded? |
|---|---|---|
| `desktop_notifications.py` | Gtk, Notify | **Yes** — `try`/`except`, sets an `available` flag |
| `base.py` | GLib | **Yes** — under `if typing.TYPE_CHECKING`, never runs |
| `diversion.py` | Gdk, GLib, Xlib, psutil, evdev, keysyms | **No** — bare `import gi` at module scope |

`diversion.py` is Solaar's key-remapping rule engine, not device configuration. It reaches the
settings only through `settings_templates.py`, and shallowly: of 68 setting classes it is used in
**one** place (a mouse-gesture notification), with `desktop_notifications.show` used in one more (a
DPI-slide toast). Drop those two call sites and the whole GUI stack goes with them.

Without `diversion`, the library's third-party needs are **`pyudev`** (already an optional
dependency here, for hotplug), **`PyYAML`**, and **`hid_parser`**. No GTK, no X11, no psutil.

So both paths are open, and the trade is real rather than one-sided:

| | Depend on `app-misc/solaar` | Vendor the subset |
|---|---|---|
| Lines in this tree | 0 | ~13,600 (+`hidapi/udev_impl`, `solaar/configuration`, `i18n`) |
| Installed | GTK3, pygobject, python-xlib, plugdev | pyudev, PyYAML, hid_parser |
| Device support | tracked upstream | re-synced by hand |
| Licence | GPL-2-or-later, called not shipped | GPL-2-or-later, shipped — notices retained, compatible with GPLv3 |

The general rule this yields: a licence badge tells you which licence *text* is in the repository,
not which version the project granted. For that, read the README and the headers of the files you
actually link against.

Had OpenRazer been **GPL-2-only**, importing its client into this app would have been a licence
violation, and the module would have had to talk to the daemon over raw D-Bus instead. Worth
confirming rather than assuming — it is the kind of thing that sinks a project late.

## 2. The one rule that governs every control

| # | Rule | Why | Status |
|---|---|---|---|
| 2.1 | **Gate on `device.has(capability)` and nothing else.** | ✅ |
| 2.2 | **`hasattr()` is meaningless here.** Every property is declared on the base class and raises `NotImplementedError` when the device does not implement it. A probe written with `hasattr` reports every capability as present and then throws on access — that is exactly how the first live probe for this module failed. | ✅ |
| 2.3 | **Even a gated attribute can raise, in three different ways.** All three were hit while probing the two devices on this desk: | ✅ |
| 2.3a | `NotImplementedError` — `available_dpi` and `supported_poll_rates` raise it on a DeathAdder V2 that reports `has("dpi")` and `has("poll_rate")` true. | ✅ |
| 2.3b | **`dbus.exceptions.DBusException`** — reading `logo.active` raises `UnknownMethodException: getLogoActive is not a valid method`. This is *not* a Python-level error, so `except NotImplementedError` does not catch it. A bare `dir()` over a zone is enough to trigger it, because `active` is a property. | ✅ |
| 2.3c | `AttributeError` — an advertised zone may simply not exist on `fx.misc`; see 3b. | ✅ |
| 2.4 | Therefore every OpenRazer access is wrapped in `(NotImplementedError, AttributeError, dbus.exceptions.DBusException)`. Anything less leaves a traceback in front of the user. | ✅ |

## 3. Capability mapping

| OpenRazer capability | Kind | Notes | Status |
|---|---|---|---|
| `dpi` **without** `available_dpi` | RANGE | Free slider, `0 … max_dpi`. **`device.dpi` is a `(x, y)` tuple, not a scalar** — a DeathAdder V2 reads `(1600, 1600)`. One slider writes both axes; independent X/Y is not offered, because it is a niche of a niche and the pair is what a user means by "DPI". | ✅ |
| `dpi` **with** `available_dpi` | CHOICE | Some devices expose fixed stages instead of a range. Mutually exclusive with the slider — Polychromatic branches the same way. | ⚠️ |
| `poll_rate` | CHOICE | From `supported_poll_rates` when it answers, else the client's own constants: 125, 250, 500, 1000, 2000, 4000, 8000 Hz. | ✅ |
| `brightness` | RANGE | 0–100, a float on the wire. Device-wide. | ✅ |
| `lighting_<zone>_brightness` | RANGE | Per zone (`logo`, `scroll`, `left`, `right`, `backlight`), reached through `device.fx.misc.<zone>`. **A mouse may have no device-wide brightness at all** — the DeathAdder V2 has none, only `logo` at 75. | ✅ |
| `game_mode_led` | TOGGLE | Keyboards. Disables the Windows key and friends. | ✅ |
| `macro_mode_led` | TOGGLE | Keyboards with a macro LED. | ✅ |
| `battery` | METER + READOUT | Percentage plus charging state. Wireless only. | ⚠️ |
| `scroll_mode`, `scroll_acceleration`, `scroll_smart_reel` | TOGGLE | Free-spin wheel, acceleration, smart reel. Neither test device has them. | ⚠️ |
| `battery` extras | RANGE | `get/set_idle_time` (seconds before sleep) and `get/set_low_battery_threshold` (percent). | ⚠️ |
| `keyboard_layout` | READOUT | Read-only; `en_US` on the test keyboard. | ✅ |
| `firmware_version`, `serial`, `name`, `type` | READOUT | Identity. | ✅ |
| `lighting_[<zone>_]<effect>` | CHOICE | One dropdown per zone, built from the effects the device advertises: None, Static, Spectrum, Wave, Reactive, Breath (single/dual/random), Starlight (single/dual/random), Ripple. Names come from the capability list, so a device offers exactly what it has. | ✅ |
| effect colour | **COLOR** | Gated on the selected effect actually taking one — `requires` + `requires_value` with the tuple of colour-taking effects. Static, Reactive, Breath-single, Starlight-single and Ripple do; Spectrum, None and the `*_random` variants do not, and their colour button stays hidden rather than lying. | ✅ |
| effect secondary colour | **COLOR** | Only for `breath_dual` / `starlight_dual`, which take two. | ✅ |
| wave direction | CHOICE | Left/right. Wave cannot be called without it. | ✅ |
| reactive / starlight speed | CHOICE | The API takes a speed argument for these; there is no way to invoke them without one. | ✅ |
| per-key matrix, effect layering, profiles | 🚫 | The editor. Out — see §0. |

### 3b. The zone name in a capability is not the zone attribute

`fx.misc` on a DeathAdder V2 provides `backlight, charging, fast_charging, fully_charged, left,
logo, right, **scroll_wheel**` — while its capability list advertises `lighting_scroll`,
`lighting_scroll_brightness`, `lighting_scroll_static`, `lighting_scroll_spectrum`. The capability
says **scroll**; the attribute is **scroll_wheel**. Deriving one from the other by string
concatenation — the obvious implementation — produces `AttributeError` on a device that plainly
supports the feature.

So the module carries an explicit capability-prefix → attribute map, and any zone that resolves to
nothing is skipped rather than crashing.

### 3c. The current effect *is* readable

Worth establishing, because it decides whether the effect dropdown can show truth or has to guess:
`fx.effect` returns the active effect by name — `'spectrum'` on the test keyboard, `'static'` on
the mouse's logo zone. So the control reflects the device rather than tracking state locally the
way an app with no read-back would have to.

`fx.active` is the trap next to it: absent on the keyboard's device-wide `fx`, and a D-Bus
`UnknownMethod` on the mouse's logo zone. Use `effect`, never `active`.

**The effect's arguments are readable too**, which was not obvious and makes every control honest
rather than blank on first paint:

* **`colors`** — a 9-byte blob, three RGB triplets. The test mouse returns
  `00ff00 00ffff 0000ff`, so colour 1 is `#00ff00` and colour 2 is `#00ffff`. The effects in scope
  use the first two.
* **`speed`** and **`wave_dir`** — plain integers matching the client's constants.

So a freshly opened page shows the device's real colour, speed and direction. Only if these were
unreadable would the module have to track state locally the way an app with no read-back does.

### 3d. Effect signatures and their arguments

Taken from the live API rather than documentation:

```
none()                                                  -> bool
spectrum()                                              -> bool
static(red, green, blue)                                -> bool
wave(direction)                                         -> bool     WAVE_RIGHT=1, WAVE_LEFT=2
reactive(red, green, blue, time)                        -> bool     500MS=1, 1000MS=2, 1500MS=3, 2000MS=4
breath_single(red, green, blue)                         -> bool
breath_dual(red, green, blue, red2, green2, blue2)      -> bool
breath_random()                                         -> bool
starlight_single(red, green, blue, time)                -> bool
starlight_dual(r, g, b, r2, g2, b2, time)               -> bool
starlight_random(time)                                  -> bool
ripple(red, green, blue, refreshrate=0.05)              -> bool
ripple_random(refreshrate=0.05)                         -> bool
```

This is why direction and speed are controls and not conveniences: `wave` and `reactive` cannot be
invoked at all without them.

The test keyboard advertises all thirteen; the mouse's logo zone advertises seven (no wave,
starlight or ripple). Building the dropdown from `has()` gives each zone exactly its own set.

### 3a. Why this needed a new capability kind

A colour is not a toggle, a number, a string or one of a fixed list, and the schema had no way to
say "this value is a colour". It was tempting to use `Kind.TEXT` and let the user type `#00ff00`,
which is what a settings form does when it has given up.

`Kind.COLOR` was added instead — the seventh kind, against the schema's own instruction to resist
adding one. The justification is that the alternative is strictly worse for the user, every
lighting-capable device family will want it (Razer today, Logitech and OpenRGB-style devices
later), and it costs the renderer one delegate that opens the platform colour dialog. Values are
`#rrggbb` strings, so a module never handles a Qt type and `core` stays Qt-free.

### 3e. DPI stages, and the two different things called "stages"

Confirmed against a live daemon: a DeathAdder V2's D-Bus object exposes **`maxDPI`, `getDPI`,
`setDPI` and nothing else**. There is no `getDPIStages`/`setDPIStages` on it. Polychromatic greys
its Sync button on the same device with "This device does not support synchronisation to hardware
DPI buttons". So two distinct features share the word:

| | |
|---|---|
| **Hardware stages** (`dpi_stages`) | Stored on the mouse; its own DPI button cycles them. Setter takes `(active, [(x, y), …])` — the whole list. Most mice do not have it. |
| **Saved stages** (ours) | A list we keep per device, applied on demand. This is what Polychromatic's editor edits, and what its quick-select buttons use. |

Both are implemented. The saved list is offered on any mouse with a DPI range; **Sync** writes it
to the hardware and is present-but-disabled where the mouse cannot store stages, with a note
saying so rather than a button that fails.

Defaults match Polychromatic's derivation exactly, so the two applications suggest the same stages:
a table for the maxima it knows, otherwise `max/10, max/8, max/4, max/2, max` rounded to 100. A
20000 DPI mouse gets **2000, 2500, 5000, 10000, 20000** in both.

Each stage carries its own horizontal and vertical value, and they are `writes_with` siblings
because one property holds the pair.

### 3f. The axis lock

Locked by default, and Polychromatic does the same. Dragging one axis alone down to 100 leaves the
pointer barely controllable in that direction — a nasty thing to do to someone mid-drag. Locked,
moving either slider moves the other; the lock starts on when the axes already match.

### 3g. Never trust a DPI read-back of zero

The daemon can answer `(0, 0)` immediately after a write. Zero is not a DPI any mouse holds, so it
is a transient read rather than a value — publishing it puts a 0 in front of the user for a
setting that applied correctly. Observed once during live testing; the device itself was at 1600
throughout.

## 3h. Macros — what OpenRazer actually offers

The client gets **three** macro calls: `getMacros`, `addMacro`, `deleteMacro`. That decides the
whole tab, and it rules out two of the three things one would expect:

| Wanted | Possible? |
|---|---|
| **Delete** a macro | **Yes** — `deleteMacro(bind_key)`, one button per recorded key |
| **List** what is recorded | **Yes** — `getMacros()` returns JSON |
| **Record** | **No.** Recording is a keyboard gesture the daemon handles itself: FN + the macro-mode key, then the M-key to bind, then the sequence, then FN + the key again. There is no D-Bus call to start it, so a Record button could not work. The tab explains the gesture instead. |
| **Test / play** | **No.** `play_macro` exists in `key_event_management.py` but is internal to the daemon's key handling — it has no `@endpoint`, so no client can call it. |

The exact key combination is **device-dependent**: the daemon triggers on a key named `MACROMODE`
in a per-model key map, which is why its own source comment says FN+F9 while Polychromatic tells
users FN+M. The hint here names both rather than picking one.

### 3i. Macros do not survive the daemon

**The daemon keeps macros in memory only.** Its persistence file covers lighting zones
(`set_persistence(zone, key, value)`); macros are not in it, and Polychromatic warns users of the
same thing. Stop the daemon or reboot and every recorded macro is gone.

That is the one place this application can add something OpenRazer does not do itself, so it does:
**Save macros to disk**, and **Restore them to the keyboard**. The stored form is the raw
`getMacros` JSON, because `addMacro` takes JSON — the round trip is lossless and no macro type can
be lost in translation.

**Restore on connect is opt-in and off by default.** It writes to the keyboard, and that must not
happen merely because someone opened a page.

Stored at `$XDG_CONFIG_HOME/hardware-ui/razer_peripherals/macros.json`, keyed by serial so the
copy follows the keyboard rather than the USB port. **Export** and **import** move them to and
from a file of the user's choosing — for a backup, another machine, or hand-editing. The import
accepts both an exported file and a bare `{bind_key: [...]}` object, so a hand-written one works.

Neither the module nor any other may open a file chooser itself: it runs on the asyncio thread.
`Capability.file_dialog` declares the need and the shell raises the platform dialog, passing the
path as the action's value — a mechanism Dell profiles and Poly settings export will reuse.

## 3j. Product photo — the vendor's own advertised link

**OpenRazer stores no images; it carries a URL per device** (`device_image`, with the deprecated
`razer_urls["top_img"]` as a fallback on ≤ 2.8). Polychromatic uses the same field. Live values on
the test hardware point at Razer's own asset host:

```
BlackWidow Chroma V2  https://assets.razerzone.com/eeimages/support/products/1179/…
DeathAdder V2         https://assets.razerzone.com/eeimages/support/products/1612/…
```

That is exactly the case this project's photo rule describes: **follow the vendor's own advertised
link, on explicit request.** Nothing is shipped, no CDN pattern is guessed, and the download only
happens when the user presses *Download from vendor* — never on connect and never on rescan.

Guards: https only (an unattended fetch of a remote file), a 15 s timeout, and a read capped at
`core.photos.MAX_BYTES`. A missing or unusable URL returns `None`, which the shell reports as "the
vendor publishes no photo" rather than an error.

### 3k. Dell has no equivalent, and that is not an oversight

Asked whether the Dell module could do the same. It cannot, for a structural reason rather than a
missing feature: **nothing advertises a URL.** DDC/CI has no field for one, and unlike OpenRazer
there is no daemon curating links. Two independent checks agree. DDPM's core libraries (`DdmLibrary.dll`, `DCF.Common.dll`,
`DCF.Agent.dll`) reference **no Dell asset host** — only code-signing certificate URLs. And the
568 MB install contains **zero loose image files**, so its artwork is embedded in the .NET
resource assemblies: Dell's to license, not ours to redistribute, and not reachable by a URL in
any case.

That leaves two options, both rejected. Guessing a `dell.com` URL from the model string is what
this project rules out on principle, and it would rot. Unpacking DDPM to lift its embedded artwork
means making the user download a 568 MB Windows application for a picture — **decided out of
scope**: the `ExtractInstaller` path exists to obtain *data the module cannot work without*, like
Poly's message ids, not decoration.

Dell monitors therefore keep the user-supplied photo, which already works. 🚫

## 4. Enumeration

| # | Rule | Status |
|---|---|---|
| 4.1 | Devices are claimed from our own HID sweep by **vendor id `0x1532`**, so no module code is imported until one is opened — the project's standing rule. | ✅ |
| 4.2 | **One USB device exposes several hidraw nodes.** Two Razer devices produced seven here. The module must reconcile the daemon's device list to the opened device by **serial**, not by node. | ✅ |
| 4.3 | The daemon, not the module, owns the hardware. If `openrazer-daemon` is not running or the client is not installed, the failure must read as "install/start OpenRazer", not as a traceback. | ✅ |

## 5. Writes

| # | Rule | Status |
|---|---|---|
| 5.1 | Writes go to the daemon over D-Bus and are effectively immediate; there is no confirmation protocol and no reboot. Read back after writing and return what the daemon reports. | ✅ |
| 5.2 | `dpi` must be written as a tuple: `device.dpi = (x, y)`. | ✅ |
| 5.3 | Nothing here is destructive and nothing needs a confirmation dialog — the most disruptive act available is dimming a keyboard. | ✅ |

## 5a. Verified on hardware, 2026-08-07

Reads and writes, on a BlackWidow Chroma V2 and a DeathAdder V2, each restored afterwards:

| Write | Result |
|---|---|
| keyboard brightness 75 → 40 → 75 | confirmed by read-back |
| keyboard effect spectrum → static `#ff00ff` → spectrum | device reported both effect and colour back |
| mouse DPI 1600 → 800 → 1600 | tuple write, confirmed |
| mouse poll rate 1000 → 500 → 1000 | confirmed |

⚠️ **One residual change was left behind and is worth recording as a process failure, not a code
one.** Setting Static wrote colour 1 to `#ff00ff`; restoring the *effect* to Spectrum does not
restore the *colour*, because the device stores the two independently. Spectrum does not display a
stored colour, so nothing looked wrong — but the next Static would have been magenta rather than
the user's green. **Restoring a device means restoring every field a test touched, not the one the
test was about.**

## 6. What cannot be tested here

Both available devices are **wired**, so `battery`, `is_charging`, idle time and the low-battery
threshold are written from the API contract and Polychromatic's usage, never exercised. They are
badged accordingly. `available_dpi` and fixed DPI stages are likewise untested — neither device
reports them.

## 7. Verified hardware

`verified`: **Razer BlackWidow Chroma V2** (keyboard, fw v1.1) and **Razer DeathAdder V2**
(mouse, fw v1.2), both matched by product id in the manifest. Reads and writes exercised live:
brightness, effect and colour, DPI on both axes, poll rate, and the macro page.
Everything else Razer is `family` — OpenRazer supports hundreds of devices and the capability
gating is generic, so an untested model gets a correct page from its own `has()` answers.
