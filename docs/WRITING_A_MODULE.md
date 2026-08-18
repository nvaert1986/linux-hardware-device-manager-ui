# Writing a device module

A module contributes protocol code and a list of capabilities. It contributes **no UI**. If you
find yourself importing anything from `hardware_ui.shell`, something has gone wrong.

```
hardware_ui/modules/<name>/
├── module.toml        manifest: matching + metadata. Read at startup. No Python imported.
├── __init__.py        keep cheap; heavy imports belong in device.py
├── capabilities.py    what the device can do, and how it is presented
├── device.py          the Device implementation — the adapter
└── protocol/          ported protocol code, kept byte-identical where possible
```

Name the directory for the device *class*, not the vendor: `sony_headsets`, `dell_monitors`. The
id must equal the directory name — vendors make more than one kind of device, and the id keys
config, the device cache and vendor assets.

---

## Before writing any code

If you are porting from an existing project — and so far every module has been — read
[`feedback-port-carefully`](PORT_DIVERGENCES.md) first, then do this:

1. **Read the origin's GUI file *and* its worker/thread file in full.** Not skim. The Sony port
   missed `workers.py` on the first pass, and the 30-second refresh poll lived only there, so a
   whole behaviour was absent and not even recorded as missing.
2. **Write down the behaviour** as a spec with a status column, the way
   [`SONY_UI_BEHAVIOUR.md`](SONY_UI_BEHAVIOUR.md) does. Every rule that disables, hides, confirms,
   delays or refuses is there because someone found it necessary on real hardware.
3. **Grep the origin for `setEnabled`, `setVisible`, `pending`, `confirm`, `reboot`** and every
   `# Gate:` / `# only` / `# never` comment, and account for each one.
4. **Import the origin's tables. Never retype them.** Hand-writing EQ preset ids and
   auto-power-off labels from plausibility got them wrong and shipped an "Option 0x10" into the UI
   while the correct table sat in `messages.py`.
5. **Run `tools/audit_port.py`** and read its caveats — its composite-group check is still too
   loose to trust.

The protocol code ports cleanly. Everything that has gone wrong has been in the adapter written
around it.

---

## The manifest

```toml
id = "dell_monitors"                    # must equal the directory name
name = "Dell Monitors"
category = "display"                    # audio | display | input | docks
                                        # | security_keys | cameras | other
description = "DDC/CI control for Dell displays"
implementation = "hardware_ui.modules.dell_monitors.device:DellMonitor"

[[match]]
transport = "display"
name_glob = "DELL *"
status = "family"                       # protocol should apply, untested

[[match]]
transport = "display"
name_glob = "DELL P2425D"
status = "verified"                     # someone has this on a desk

[vendor_assets]                         # optional
provider = "extract_installer"
required = false
source_page = "https://…"
```

Match rules are tested against enumeration data only — nothing is opened. Prefer the most stable
signal available: a **service UUID** for Bluetooth (survives renaming), a **vendor id** for USB, the
**EDID vendor/model** for displays. Name globs are a weak last resort; use them to upgrade
`family` to `verified`, not as the primary matcher.

**Every rule must be gated on the vendor** — a vendor id, a service UUID, or a vendor-specific
property. Claim by maker, never by device class: a rule of `transport = "hid"` alone would hand
your module every mouse, keyboard and dock on the machine, and someone else's hardware would be
driven by your protocol. A test walks every shipped manifest and enforces this, so an ungated rule
fails the suite rather than reaching a user.

**The narrow exception, and what earns it.** Two shipped modules do claim a whole class —
`fido2_security_keys` by the FIDO usage page, and `uvc_cameras` by `transport = "v4l2"` — and both
are whitelisted by name in `tests/test_capability.py::CLASS_WIDE_MODULES`. What justifies it in both
cases is that the *class specification itself* reports what the device can do, so an unknown device
gets a correct page rather than a guessed one: CTAP answers which options a key supports, and V4L2
answers which controls a camera has, with ranges, defaults and menu items. If your protocol is a
published class specification with runtime capability discovery, make that argument and add
yourself to the whitelist. If it is a vendor protocol you happen to reach over a generic transport,
it is not this case — gate it.

**The `transport` values available** are `hid`, `usb`, `bluetooth`, `ble`, `display` and `v4l2`.
Adding a seventh means writing an enumerator in `hardware_ui/core/discovery.py`, adding the member
to `Transport`, and — if devices of that kind appear and disappear — adding its subsystem to
`HOTPLUG_SUBSYSTEMS`.

Do not rely on the suite to remind you of that last step. The test on `HOTPLUG_SUBSYSTEMS` pins the
exact set, which makes *changing* it a deliberate act, but nothing fails if you add a transport and
forget its subsystem — the symptom is a device that appears only after a manual Rescan, and no test
will tell you. The same is true of `Category`: adding a member is not enough, it needs an icon in
`Category.icon`, and the icon name has to exist in the Breeze theme.

Be wary of a vendor id being enough on its own. `dell_docks` needs vid `0x413c` **and** a name
containing "dock", because Dell also makes keyboards.

The one exception in this tree is `fido2_security_keys`, which claims by the FIDO usage page and
names no vendor, because CTAP genuinely is the same on every maker's key. If you think your module
is another such case, it probably belongs as a base module that vendor modules `extend` — see
[Specialising another module](#specialising-another-module).

`implementation` is imported **only when the user opens a matching device**. Keep `__init__.py`
cheap so that stays true.

---

## capabilities.py

Describe what the device can do. Build it from what the device *reports*, not from a per-model
table — that is what lets a `family` match work on hardware nobody has tested.

```python
BRIGHTNESS = Capability(
    key="image.brightness",
    kind=Kind.RANGE,
    label="Brightness",
    group="Image",
    minimum=0, maximum=100, step=1, unit="%",
)

def build(supported: set[int], state: Any = None) -> CapabilitySet:
    caps = [c for code, group in FEATURE_MAP.items() if code in supported for c in group]
    if state is not None:
        caps = [c for c in caps if observed(c.key, state)]   # hide what did not answer
    return CapabilitySet(caps)
```

Rules worth following:

- **`group` becomes a tab.** Use the origin application's tab names if there is one; they were
  arrived at by someone using the device.
- **`section` becomes a sub-heading.** Members must be declared contiguously.
- **Gate with `requires` + `requires_value`.** Truthiness alone is usually wrong.
- **Declare `writes_with`** for every key carried by the same protocol message — including a
  read-modify-write of one shared register, which is how the Dell USB-KVM pairings work.
- **Mark `reboots=True`** on anything that restarts the device, and give a `confirm_detail`
  explaining the consequence the label cannot convey. Use **`confirm=True`** for a change that is
  disruptive but keeps the connection — `reboots` additionally promises a reconnect.
- **Set `timeout`** where a write genuinely takes longer than a protocol round-trip, and
  **`action_label`** on every `ACTION`; the default button says "Run", which is right for nothing.
- **Need a secret? Declare it, do not put a field on the form.** `prompt="pin"` (or
  `"pin_change"` / `"pin_set"`, with `prompt_detail` and `minimum`) makes the shell ask and hand
  the answer over as the action's value. A PIN left in a form stays on screen.
- **Return a sentence from an action.** The shell shows it beside a tick, and "Done" is rarely the
  most useful thing you could have said.
- **Need a file? Declare it, do not open one.** Set `file_dialog="open"` or `"save"` on an
  `ACTION`, with `file_filter` and `file_suffix`; the shell raises the platform chooser and hands
  the path over as the action's value. A module runs on the asyncio thread and must never touch a
  widget.
- **Use `Kind.COLOR` for a colour**, valued as an `#rrggbb` string. Gate it with `requires` +
  `requires_value` so it appears only for the effects that actually take one.
- **A module may require a package the application does not.** Import it inside `connect()`, never
  at module scope, and turn `ImportError` into an `Unreachable` that says what to install —
  `razer_peripherals` needs OpenRazer and an installation without it must run normally. A test
  asserts the import stays out of module scope.
- **`description` is a tooltip; `note` is visible.** Put anything the user must see in `note`, or
  return an `Advisory` if it depends on state.

---

## device.py

```python
class DellMonitor(Device):
    @property
    def capabilities(self) -> CapabilitySet: ...

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...      # must be safe if never connected, must not raise
    async def get(self, key: str) -> Any: ...    # raise NotSupported if the device rejects it
    async def set(self, key: str, value: Any) -> None:  # return once confirmed

    # optional
    async def get_many(self, keys) -> dict: ...  # override where the protocol batches
    async def refresh(self) -> dict: ...         # cheap periodic poll; leave it out if a poll
                                                 # would cost real traffic (see dell_monitors)
    def advisories(self) -> dict[str, Advisory]: ...
    async def fetch_photo(self) -> bytes | None: ...  # only from a vendor-advertised URL
```

### Wrapping synchronous protocol code

Ported clients are usually blocking. Do not rewrite them — that is the one part known to work.
Dispatch with `asyncio.to_thread` and serialise with a lock:

```python
async def set(self, key: str, value: Any) -> None:
    async with self._lock:
        await asyncio.to_thread(self._write, key, value)
```

The lock is not optional. **One thread owns a transport.** A second reader on the same channel
consumes the reply another call is waiting for — that is what made Sony's speak-to-chat switch
itself off, and it applies equally to a shared I²C bus.

### Composite writes

Where one message carries several settings, read the siblings and send them together:

```python
def _write_group(self, key, value):
    a, b, c = self._current_values()
    ...                                   # substitute the one that changed
    self._client.set_all(a, b, c)
```

Declare the same keys in `writes_with` so the shell holds them all pending.

### Returning what actually landed

`set()` may return the value the device settled on. Return `None` when it took the request as
sent, and the real value when it did not but the change still applied — a DDC panel that
quantises sharpness to steps of ten lands 55 as 50, which is success. If the capability set itself
changed (new bounds, a wholesale reset), call `self._bump_capabilities()` and the shell repaints
the page.

### `UNREAD` vs `None`

Distinguish "not read yet" from "not supported". Returning `None` for both made the XM4's
equaliser show a permanent *unsupported* badge for a feature it fully supports. Omit unread keys
from `get_many`; raise `NotSupported` only when the device genuinely rejects the capability.

---

## Specialising another module

A module may build on another instead of duplicating it:

```toml
id = "yubikeys"
extends = "fido2_security_keys"
```

The registry claims a device with the **most specialised** module that matches, so hardware that
both modules claim appears **once**. The implementation is expected to subclass the base's, which
is how it inherits everything the base already does:

```python
class YubiKey(Fido2SecurityKey):
    def extra_capabilities(self): ...   # what this vendor adds
    def extra_values(self): ...
    def handle_set(self, key, value): ...
```

Design the base with those hooks from the start. Disabling a specialisation must leave its devices
working through the base rather than unsupported -- a test asserts that.

## Notes specific to DDC/CI

All of this is implemented in `hardware_ui/modules/dell_monitors/`; read that alongside
[`DELL_UI_BEHAVIOUR`](DELL_UI_BEHAVIOUR.md) before adding another display family.

- **Enumerate from EDID, never `ddcutil detect`.** The latter takes seconds and can wedge a bus.
  `discovery.enumerate_displays()` already provides vendor, model and connector from
  `/sys/class/drm/*/edid`. It is still needed to resolve the **I²C bus** when a display is opened —
  there is no reliable sysfs path, because a DP connector carries DDC over AUX and an MST bus has
  no link back to its connector. Run it once for the whole system, not once per monitor.
- **A write returning success is not a write that applied.** Read it back. Some panels acknowledge
  a feature they only implement in their own menu.
- **I²C is slow and not concurrency-safe.** Serialise every access, and cache the resolved bus
  number — rediscovering it per operation is most of the cost.
- **Reads can fail transiently.** A VCP read that times out is normal on some buses; retry rather
  than marking the capability unsupported.
- **Capability discovery exists**: the VCP capabilities string lists supported features. Use it in
  place of a per-model table, exactly as Sony's function list is used.

---

## Vendor data

Nothing vendor-owned may be committed. If a module needs data it cannot ship:

- **`RegistryFetch`** if the vendor publishes it at a stable URL — pin the version, verify a hash,
  cap the size, and check a sanity floor.
- **`ExtractInstaller`** if it only exists inside the vendor's installer — the user obtains it from
  the vendor and it is unpacked locally.

Ship a small hand-authored catalogue for verified devices so first run is never a dead end, and
let the import unlock the long tail. Anything derived from a vendor binary must be **re-expressed
in our schema**, not copied verbatim.

---

## Verifying

```bash
python3 -m pytest              # schema, matching, write-path behaviour
ruff check .
python3 tools/audit_port.py    # mechanical diff against the source project
python3 -m hardware_ui.cli --all   # what is detected, and what is unclaimed
./run.sh
```

`hardware-ui-cli <uid>` dumps capabilities, values and advisories — that output is the artefact to
attach to a bug report for an untested model.

Add a `[[match]]` with `status = "verified"` only for hardware someone has actually exercised.
Everything else is `family`, and the UI badges it accordingly.
