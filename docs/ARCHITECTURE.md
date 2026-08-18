# Architecture

Sony headphones, Poly headsets, Jabra headsets and DDC/CI monitors are not really different
applications. They are all *read and write typed settings on a device that advertises what it
supports*. This project treats them that way: one shell, one renderer, and per-device modules that
contribute protocol code and a list of capabilities — never UI.

```
hardware_ui/
├── core/          device model, capability schema, discovery, vendor assets, photos
│                  no Qt import anywhere
├── shell/         PyQt6 Widgets UI and the controller
├── modules/       one subpackage per device family
└── cli.py         headless diagnostics; imports no Qt
```

The layering is load-bearing rather than decorative: when the UI was rewritten from QML to
QWidgets, `core/`, every module, the tests and the docs were untouched. `cli.py` exists partly as
a standing check that `core` has not grown a UI dependency.

---

## The capability schema

A module describes its device as a list of `Capability` values. The shell renders that list and
has no per-device knowledge.

```python
Capability(
    key="anc.mode",             # stable identifier: config, CLI, cache all key on it
    kind=Kind.CHOICE,           # how it is presented
    label="Noise control",
    group="Noise && Ambient",   # becomes a tab
    section="General",          # optional sub-heading within a tab
    choices=(...),
    requires="anc.mode",        # gate: another capability
    requires_value="ambient",   # ...and the value it must hold
    writes_with=(...),          # keys carried by the same protocol message
    confirm=False,              # ask first: disruptive, but the device stays connected
    reboots=False,              # applying this restarts the device
    timeout=0.0,                # override the shell's write timeout (0 = default)
    action_label="",            # button text for an ACTION
    file_dialog="",             # "open"/"save": the shell picks a file for this ACTION
    prompt="",                  # "pin"/"pin_change": the shell asks for a secret first
    secret=False,               # a TEXT value that is masked and never repainted
)
```

Seven kinds cover everything found across MDR, Deckard, GNP, DDC/CI and OpenRazer: `TOGGLE`,
`CHOICE`, `RANGE`, `ACTION`, `READOUT`, `METER`, `TEXT`, `COLOR`. Resist adding an eighth before checking an existing
one plus metadata will not do — each new kind is another branch in the renderer and another thing
that can look out of place.

Three fields exist because a real device forced them:

**`requires_value`** — truthiness is not enough. Sony's ambient level applies when noise control
*equals* `"ambient"`; `"anc"` is equally truthy and equally wrong.

**`writes_with`** — `set_stc` carries enable, sensitivity and timeout in one message, so writing
any of them re-sends the others. All three must be held pending together, or the untouched ones
get written from state captured mid-sequence. This is what made speak-to-chat switch itself off.

**`reboots`** — three Sony settings restart the headset. They need confirming before the write and
reconnecting after, and their writes can never be confirmed (below). **`confirm`** is the weaker
neighbour it kept being confused with: switching a monitor's input hands the picture to another
machine and a factory reset discards every setting, but neither drops the link, so neither should
promise a reconnect.

**`timeout`** — DDC/CI forced this. A range calibration writes and reads back thirty probe values,
and entering PIP/PBP polls a blanked panel for ten seconds. Both are correct, and both outlive a
timeout sized for an RFCOMM round-trip.

`COLOR` is the only kind added after the fact, for Razer lighting. It had to clear the bar above:
`TEXT` *could* carry `#00ff00` and ask the user to type it, which is what a form does when it has
given up. Values stay `#rrggbb` strings so `core` never imports a Qt type, and the renderer opens
the platform colour dialog and paints the swatch on the button.

**A secret belongs to the operation, not to the page.** `prompt` makes the shell ask for a PIN
when an action needs one and hand it over as that action's value. Fields on a form were the first
attempt and were worse in two ways: the PIN sat on screen for as long as the page did, and "test
this key" required filling in a box labelled for *changing* the PIN. `prompt = "pin_change"` also
asks for the new value twice and checks the two agree, because a mistyped PIN written to a
security key is recoverable only by a reset that wipes it.

An ACTION receives the answer as its value; anything else receives `(value, answer)`, so a slider
that also needs a PIN does not lose its number.

**An action says whether it worked.** A tick or a cross beside the button, the detail on hover, and
a line in the status bar. Actions are the one kind whose effect is often invisible -- a self-test,
a reset, a sync -- and until this existed "nothing happened" and "it worked" looked identical.
A module returns a sentence from `set()` and that sentence is what the user reads.

`Advisory` carries what the schema cannot: a state-dependent message, optionally locking the
control. The motivating case is the WH-1000XM3, where the equaliser is unusable while LDAC is
active — and the *message* is the valuable part, because it tells the user which other setting to
change.

---

## Two rules that make startup fast

### Enumeration is not probing

| Phase | Cost | What it does |
|---|---|---|
| **Enumerate** | ~30 ms for 20 devices | sysfs walk, BlueZ properties, DRM EDID. Opens nothing. |
| **Probe** | 100 ms – tens of seconds | Opens hidraw, connects RFCOMM, reads I²C. |

Enumeration yields VID/PID, Bluetooth names and UUIDs, and monitor vendor/model from EDID —
everything a match rule needs, without touching a device. Probing happens only when the user asks.

DDC/CI is the clearest case: `ddcutil detect` takes seconds and can wedge a bus, so monitors are
enumerated from `/sys/class/drm/*/edid` instead, and I²C is touched only when that display's page
is opened.

### A module's Python is not imported until one of its devices is opened

Manifests are TOML. Matching runs against enumeration data using declared rules. Modules
deliberately have **no `detect()` method** — if each could run code to decide whether its hardware
is present, startup time would be set by the least careful module, and one vendor library with a
slow import would tax everyone.

A test asserts that claiming a device imports no module code.

---

## Module discovery

`ModuleRegistry.discover()` scans for `module.toml`, in precedence order:

1. `hardware_ui/modules/*/module.toml` — built in; dropping a directory there is all it takes
2. `$XDG_DATA_HOME/hardware-ui/modules/*/module.toml` — user-supplied
3. the `hardware_ui.modules` entry point group — separately distributed modules

A module's id must equal its directory name; the registry warns and corrects otherwise. The id
keys the config file, the device cache and the vendor-asset directory, so a drifting id orphans a
user's settings. Ids are specific for the same reason: `sony_headsets`, not `sony` — vendors make
more than one kind of device.

Enablement is tri-state, because a boolean conflates two intentions:

| | |
|---|---|
| `auto` | active when a device matches (default) |
| `always` | active regardless — for development, or hardware that enumerates oddly |
| `off` | never matched, never imported |

---

## Matching

Rules live in the manifest and are tested against enumeration data. Prefer whatever the device
cannot be renamed out of: a **service UUID** for Bluetooth, a **vendor id** for USB, the **EDID
vendor** for a display.

```toml
[[match]]
transport = "bluetooth"
uuid = "96cc203e-5068-46ad-b32d-e316f5e069ba"
status = "family"                  # protocol should apply, untested

[[match]]
transport = "bluetooth"
name_glob = "WH-1000XM4"
status = "verified"                # someone has this on a desk
```

```toml
[[match]]                          # every Dell, whatever the panel calls itself
transport = "display"
properties = { edid_vendor = "DEL" }
status = "family"
```

The **service UUID is the real matcher**; name globs only upgrade the badge. A headset renamed to
anything at all is still recognised, because the UUID is what BlueZ caches from pairing. Both real
WH-1000XM headsets are claimed by UUID alone with their names hidden — which is what lets an
untested future model work with no manifest change.

`family` matches rely on **capability discovery over declaration**: the device is asked what it
supports rather than looked up in a per-model table.

### Every rule is gated on the vendor

A module claims hardware **by maker, never by device class**. Each rule carries a vendor id, a
service UUID, or a vendor-specific property such as the EDID vendor code — so a Logitech mouse can
never land in the Razer module, and a Corsair keyboard is simply unclaimed rather than
misconfigured by somebody else's protocol.

That is not a convention anybody has to remember. `MatchRule.matches` refuses a rule with no
fields at all, so an empty rule cannot claim everything by accident; and a test walks *every*
shipped manifest asserting each rule is gated on something vendor-specific. A new module cannot
add an ungated rule without the suite failing — which catches collisions nobody thought to write
an example for.

Vendor id alone is often too coarse, and the rules say so: `dell_docks` matches vid `0x413c`
**and** a name containing "dock", because a Dell keyboard shares that vendor id and is not a dock.

**Two deliberate exceptions**, and the guard test whitelists them by name so a third cannot be
added without someone deciding to:

**`fido2_security_keys`** claims a security key from any maker by its FIDO usage page `0xF1D0` and
mentions no vendor id at all. That is the point of it — the CTAP standard is the same on a YubiKey,
a Nitrokey and a SoloKey. A YubiKey still appears **once**, because `yubikeys` extends it and the
most specialised module wins.

**`uvc_cameras`** claims an entire transport: `transport = "v4l2"` with no vendor id. The
justification is the same in shape and stronger in degree — UVC is a class specification, and the
kernel's driver reports which controls a camera has, with their ranges, defaults and menu items. A
webcam nobody has ever seen therefore gets a *correct* page rather than a guessed one, which a
vendor-gated rule could never do. Per-model tables exist only for the vendor extras layered on top,
and each of those is gated three ways before it is offered — see
[UVC_CAMERAS_UI_BEHAVIOUR](UVC_CAMERAS_UI_BEHAVIOUR.md) §2.

The pattern to take from these two: claiming a class is legitimate when the *class itself* tells you
what the device can do. It is not a licence to claim broadly and guess.

---

### `suffix_from`

A value that qualifies a reading rather than being one — how long a one-time code is still good
for — is shown **after** that reading, lighter, on the same line. It is display only: `copyable`
still copies the value alone.

The source needs no row of its own, which is the point: giving it one put a second copy of the
section heading on the page and separated the qualifier from what it qualifies. Three consequences,
each of which was a bug first:

- `set_value` finds the rows that **depend on** a key even when that key has no row.
- The shell fetches suffix sources alongside the rows, so nothing is blank on first paint.
- A pushed change is offered to **every** form, because `_form_for` matches on rows and would
  never find one for a suffix source. Each form ignores what it neither owns nor depends on.

With `suffix_total` set the suffix is also drawn as a small depleting bar. Keep the total per-row:
two rows on one page can be counting down from different starting points.

### `copyable`

A readout that exists to be pasted somewhere else — a one-time code, a serial, an AAGUID — sets
`copyable = True` and gets a Copy button. Selecting label text with the mouse is technically
possible and useless in practice for a value that is replaced every thirty seconds. The button
copies the stored value rather than the rendered string, so a unit is never dragged along with it.

---

## Threading

Device I/O is asyncio on a dedicated thread; Qt owns the GUI thread.

```
GUI thread                    asyncio thread
──────────                    ──────────────
user clicks  ──spawn()──────▶ coroutine does device I/O
widget    ◀──call_on_ui()──── marshals the result back
```

`AsyncBridge` owns the loop. The rule is absolute: **coroutines never touch a widget**; they hand
a callable to `call_on_ui`, delivered on a queued signal.

qasync would normally unify the two loops, but it has no ebuild in any Gentoo repository. A
dedicated thread turned out better anyway: device I/O *cannot* block the UI, because it runs
somewhere else, and `core` stays a plain asyncio library testable with `asyncio.run` and no Qt.

**One thread owns a device transport.** A second reader racing writes on the same RFCOMM session
consumes the ACK `send_command` is waiting for and mutates state mid-write. That is not
hypothetical — it is what made speak-to-chat switch itself off.

---

## The write path

Every module inherits the same behaviour, implemented once:

0. **The device may answer with the value it actually landed on.** `set()` returns it, and the
   shell paints that rather than the request. A DDC panel quantises sharpness to steps of ten, so
   asking for 55 applies 50 — success, and showing 55 over a monitor at 50 is the same lie as
   reporting failure.
1. **Optimistic update** — the control moves immediately
2. **The whole `writes_with` group is disabled** until the device confirms
3. **Incoming values for pending keys are dropped**, unless they are that write's confirmation —
   otherwise a refresh captured before the write lands repaints the old value
4. **`finally` always releases the pending flag.** Releasing it per-branch means any unanticipated
   path leaves the control disabled forever
5. **A rejection is not permanent** — a later successful read retires it

**A device can also change shape.** `Device.capabilities_revision` is bumped by a module that has
rebuilt its capability set, and the shell repaints the whole page. Calibrating a monitor re-bounds
five sliders at once and a factory reset changes every value on the panel; neither is expressible
as "one key changed".

**Reboot writes take a separate path.** They cannot be confirmed: the link drops *as* the device
restarts, so the reply is exactly what does not arrive. Treating that silence as failure reported
an error for a change that had applied, and skipped the reconnect. The link dying is the success
signal; the app then waits 15 s and reopens.

---

## Vendor data

Nothing vendor-owned is redistributed. Two acquisition modes exist, for two different reasons:

**`RegistryFetch`** — Jabra's property definition is ISC and freely redistributable, but the
published package contains no licence text, only a `"license": "ISC"` field. ISC requires the
notice to travel with every copy. Rather than author someone else's notice, it is fetched from GN
Audio's own publication on consent, pinned by version and hash.

**`ExtractInstaller`** — Poly's device catalogue exists only inside Poly Studio. The user obtains
HP's installer from HP; we unpack their copy locally and convert it into our schema. Same model as
Debian's `ttf-mscorefonts-installer`.

Both are optional: a hand-authored catalogue covers verified devices so first run is never a dead
end, and importing unlocks the long tail.

**Device photos have one supported source, and only one: a URL the vendor advertises.** Nothing
ships. `Device.fetch_photo()` follows a link the device or its daemon hands us — OpenRazer carries
`device_image` per device, Jabra's configuration service names its asset URLs — and the download
happens only when the user presses the button, never on connect or rescan.

Everything else is out of scope, deliberately:

* **`https` only.** The download is unattended and the result is written to disk and rendered, so
  plain `http` is refused: a fetch that can be rewritten in transit is not worth a picture. No
  vendor seen so far advertises anything but `https`; if one ever does, that is a decision to make
  deliberately rather than a default to inherit.
* **No guessed CDN patterns.** Constructing a URL from a model string is fragile, breaks silently
  when the vendor reorganises, and is a far weaker position than asking for exactly what the
  vendor's own client asks for.
* **No unpacking installers for artwork.** `ExtractInstaller` exists to obtain data a module
  *cannot work without* — Poly's message ids and payload types. Making someone download a 568 MB
  application for a picture is not that trade. Dell is the case that settled it: DDC/CI advertises
  no URL, and DDPM appears to embed its images as resources.

A device with no advertised URL simply keeps the user-supplied photo, which every module already
supports.

---

## Caching

| Data | Cached? | Why |
|---|---|---|
| Device presence | yes, `$XDG_CACHE_HOME` | paints the sidebar before enumeration finishes |
| Discovery (identity, function list, APO options) | yes, keyed on the function list | ~14 of 27 round-trips on connect |
| **Setting values** | **never** | the vendor's phone app can change them behind us |

That last row is the rule the others exist around. A test asserts the discovery cache blob
contains no setting keys.

---

## What this deliberately does not do

**No auto-connect.** Opening a device's config channel by itself can make a headset power-cycle,
so selection never opens anything — the user presses Connect. This is the reference
implementation's rule and it is not negotiable.

**No hotplug yet.** `discovery.watch()` raises `NotImplementedError`; a device paired while the app
is running needs Rescan. udev, BlueZ `InterfacesAdded` and DRM uevents are the intended
implementation.

**No daemon.** A D-Bus service would let the CLI, the GUI and a future KCM share one connection.
It is easy to add behind the core API and premature now.
