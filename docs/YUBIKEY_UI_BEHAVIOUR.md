# YubiKey — behavioural specification

A `yubikeys` module that **`extends` [`fido2_security_keys`](FIDO2_UI_BEHAVIOUR.md)**, adding the
vendor functions that sit outside CTAP — the ones the base module names in its §1 as deliberately
excluded. The key still appears **once**.

Written before any code, as a decision record. **The information tab is now built and verified**;
everything that writes is still specification only.

Status: ✅ confirmed against the attached key · 📋 specified, not built · ⚠️ untestable here ·
🚫 out of scope

---

## 0. The reference implementation is on this machine

`~/Projects/yubikey-authenticator/yubico-authenticator-7.4.1-linux` — Yubico's own release, and
the authority for how any of this should behave. Two things fall out of it:

**Its `helper/` binary embeds `ykman`.** Confirmed by inspection: `ykman`, `ykman.device`,
`ykman.fido`, `ykman.diagnostics` and `smartcard` are all frozen into the PyInstaller executable.
So the Yubico Authenticator is a Flutter front end over the same Python library this module calls
directly. Nothing needs porting, and there is no licence question to answer — the app is
Apache-2.0, which GPL-3-or-later could absorb, but no code is copied.

**Its UI strings are readable in `lib/libapp.so`.** That is where the wording in §6 and §11b comes
from: *"Enable or disable applications over available transports"*, *"Configuration updated, remove
and reinsert your YubiKey"*, *"Short touch"* / *"Long touch"*, *"Slot is configured"*,
*"Keyboard layout (for static password)"*, *"Enter access code for slot"*. Where Yubico's phrasing
is better than ours, it is adopted rather than invented around.

Its theme is not adopted. Only its behaviour.

## 1. Which library, and why the other three are not it

| Candidate | What it actually is | |
|---|---|---|
| **`app-crypt/yubikey-manager` 5.9.1** | Yubico's current Python SDK (`ykman` + `yubikit`) and CLI. BSD-2 | ✅ **the one** |
| `sys-auth/ykpers` 1.20.0 | the C personalisation library behind `ykpersonalize` | **archived 20 Feb 2025** |
| `sys-auth/libyubikey` 1.13 | modhex, and AES **decryption of OTPs a key has emitted** | **archived 20 Feb 2025** |
| `ykclient` (yubico-c-client) | HTTP client for **YubiCloud** — asks Yubico's servers to validate an OTP | not in the Gentoo tree |

The names obscure the real division. **`libyubikey` and `ykclient` are validation-side**: they
belong in a *server* checking a one-time password some key already produced, one parsing it
locally and the other over the network. Neither can configure hardware at all, and `ykclient`
would mean sending data to an external service from a local settings panel.

`ykpers` was the only genuine alternative for configuration, and it is dead — archived upstream,
last Gentoo ebuild from 2022, **zero reverse-dependencies in the tree**.

**BSD-2 against this project's GPL-3.0-or-later** is a one-way permissive combination with no
friction. Underscore-prefixed names are the documented private API and are avoided.

## 2. One connection, already open ✅

The finding that shapes the whole module: `ManagementSession` accepts an `OtpConnection`, a
`SmartCardConnection` **or a `FidoConnection`** — and the CTAP backend implements `read_config`
*and* `write_config` as vendor commands, not just reads.

Confirmed on the attached YubiKey 5 NFC: the complete management surface — model name, serial,
form factor, per-transport application state, device flags, timeouts, lock state — read over
**the very `/dev/hidraw` node the base module already opens**.

So the management surface needs **no `pcscd`**, **no smartcard stack**, **no access to the OTP
keyboard interface**, and **no second device handle**. It reuses the base module's open CTAP
device. The OTP slots in §11b are the one part that reaches further, and even they stop short of
the smartcard.

Everything in §5, §6, §7 and §8 rides on that one connection. Only the OTP slots in §11b need a
second one, and nothing needs a third.

### Fallback, for keys where CTAP is not available

A YubiKey with FIDO switched off — see §4 — has no CTAP interface. Then `FidoConnection` →
`OtpConnection`, and **stop there**: the smartcard hop this originally specified is gone with §11,
because falling back to an interface `gpg-agent` may be holding is exactly the contention that
section exists to avoid. A key with neither FIDO nor OTP enabled is out of reach and says so.

The OTP fallback needs udev permission on the keyboard interface, so its absence is reported as
`DependencyMissing` naming that specific thing — never a bare failure. 📋

## 3. Enumeration stays cheap ✅ **built**

`ykman.device.list_all_devices()` **opens every interface of every attached YubiKey** to read its
info. That is exactly the probing this project keeps out of discovery.

Discovery therefore does what it already does: match `vendor_id = 1050` from sysfs, nothing
opened. Every `ykman` call happens in `connect()` or later — and a test asserts that neither
`ykman` nor `yubikit` appears among the module's top-level imports.

## 4. The match rule — the one real design decision ✅ **built**

**Match on the vendor id alone, *not* on vendor id plus the FIDO usage page.**

The tempting rule is `vendor_id = 1050` + `hid_usage_page = f1d0`, mirroring the base module. It
is a trap, and precisely because of what this module adds: **disabling FIDO over USB is one of the
operations offered here.** A user who does it would remove the interface the match rule depends
on, the key would vanish from the application, and the control needed to put it back would have
vanished with it. A setting must not be able to hide its own undo.

Vendor id alone survives that. The cost is that the **inherited CTAP half becomes conditional** —
`Fido2SecurityKey.connect()` cannot be assumed to succeed:

| Key | Behaviour |
|---|---|
| YubiKey 5, everything on | base CTAP page **plus** every section below |
| YubiKey 5, FIDO disabled | 📋 **not yet** — the key is claimed, but with no CTAP interface there is nothing to talk over until §2's fallback exists, so it reports itself unreachable. Harmless while nothing here can switch FIDO off; it must land before §6 does |
| Security Key by Yubico (FIDO-only, no management application) | **base module's page unchanged** — a missing management application degrades silently, it is not an error |
| Firmware < 5.0 | read-only, §9 |

That last row matters: `read_device_info` failing is a normal outcome, not a fault. The module
must not turn a key the base module handles perfectly into a broken one.

## 5. Vendor Specific Information — a tab of its own ✅ **built**

*(Identity only. The application list that used to live here became §6's tab of toggles.)*

**The vendor rows do not go on the Information tab.** That tab is the CTAP one: it says the same
things about a Nitrokey, a Token2 and a YubiKey, and between identity, capabilities and the full
option list it was already long before anything was added to it. Bolting six more rows and an
application table onto the end made it a page nobody reads to the bottom of.

So they get their own tab, named **Vendor Specific Information** and deliberately *not* "YubiKey".
Any vendor module can reuse the name — a Nitrokey or Logitech specialisation gets the same tab in
the same place — so a reader learns once that this is "what the standard cannot tell you", instead
of learning a new tab name per manufacturer. A test asserts the name carries no vendor in it.

Tab order is **Information → Vendor Specific Information → Configuration**: one form is built per
group in list order, so appending the vendor rows would otherwise have put the tab after
everything that writes.

Two sections, both confirmed live over CTAP:

| Row | Value here | Note |
|---|---|---|
| Model | **YubiKey 5 NFC** | `yubikit.support.get_name()` |
| Firmware version | 5.2.6 | gates most of §6 |
| Serial number | *(present)* | |
| Form factor | Keychain (USB-A) | |
| FIPS / SKY / PIN complexity | no / no / no | |
| Configuration lock | not locked | §7 |
| USB interfaces | OTP, FIDO, CCID | from the product id, dropped when unrecognised |

**Every row is conditional on the key reporting it.** A Security Key with no serial number and an
unknown form factor simply shows fewer rows; a NEO gets no configuration-lock row, because lock
codes arrived with the YubiKey 4 and the row would forever read "Not set"; `pin_complexity` and
NFC restriction are firmware 5.7 features and appear only where they are real. No product id,
model name or firmware version is hard-coded anywhere in the module.

**Ordering is a stable sort that moves the tab and nothing else.** Section order inside every tab,
and the relative order of all the other tabs, stays exactly as the base module built it — a page
with no vendor rows at all comes back untouched.

**This settles the base spec's §11 AAGUID problem.** `get_name()` returns the real marketing name
from the device itself — no community-maintained AAGUID list to download, nothing vendor-owned to
redistribute, and it works offline. The `RegistryFetch` plan for model names is dropped for
YubiKeys.

**The serial number is how two identical keys are told apart**, which is why it is a row rather
than a detail. It is device-identifying: shown on the page, never logged. The *sidebar* label
still comes from the USB product string — renaming a device from its module needs core support
that does not exist yet, so two YubiKeys currently read alike in the list and are told apart on
their pages.

## 6. Applications — a tab of toggles ✅ **built and verified on hardware**

Seven applications × two transports, from `info.supported_capabilities` and
`info.config.enabled_capabilities`:

|  | USB | NFC |
|---|---|---|
| Yubico OTP, FIDO U2F, FIDO2, OATH, PIV, OpenPGP, YubiHSM Auth | toggle | toggle |

Two sections, **Over USB** and **Over NFC**, one TOGGLE per application, each present only if the
key *supports* it on that transport — a YubiKey 5 NFC has no YubiHSM Auth at all, so it is absent
rather than greyed. That is the Yubico Authenticator's rule too: it draws a chip only when
`capabilities & value != 0`.

**A tab, where the Yubico Authenticator uses a dialog** ("Toggle applications — enable or disable
applications over available transports"). The content is identical; a tab is what this shell has.
The read-only application summary that used to sit on the vendor tab is gone: the same facts in
two places is how they end up disagreeing.

All of them, plus the timeouts, share one `writes_with` group: they are fields of a single
`DeviceConfig` written by one `write_device_config()` call. Without that, touching one toggle
re-sends the others from state captured mid-sequence and silently reverts them — the same failure
`writes_with` exists for on the Sony module.

### Applications are toggled; interfaces are a consequence

Worth stating because it is easy to conflate, and it changes what the tab can offer. There are
**seven applications** (OTP, U2F, FIDO2, OATH, PIV, OpenPGP, YubiHSM Auth) and **three USB
interfaces** (OTP, FIDO, CCID). On firmware 5 and later only the applications are settable; the
interface set is *derived* from them. Verified against `ykman`:

| Enable this application | and this interface appears |
|---|---|
| OTP | OTP |
| U2F, FIDO2 | FIDO |
| OATH, PIV, OpenPGP, YubiHSM Auth | CCID |

So "disable CCID" is not a control — it is what happens when the last CCID-backed application is
switched off. `ykman config mode`, which does set interfaces directly, exists for **pre-5 series**
keys and its own help says to use `config usb` on anything newer; §9 puts those keys read-only.

This has a consequence worth naming in the dialog: **the CCID interface is the one `gpg-agent`,
`scdaemon` and Kleopatra use.** Turning off OATH, PIV and OpenPGP together removes it, and those
tools stop seeing the key. §11 keeps this module off that interface, but a control here can still
switch it off — so it owes the user that sentence.

### Four rules that are not optional

1. **At least one of OTP, U2F or FIDO2 must stay enabled over USB.** Not "at least one
   application" — the Yubico Authenticator's actual rule is `(usbEnabled & ~ccid) != 0`, and it is
   the better one. Those three are the interfaces this application can talk over; leaving only
   smartcard applications is a one-way door, because §11 keeps us off the smartcard and `ykman`'s
   OTP and FIDO paths are shut too. The module refuses and says why rather than greying a box.
2. **Disabling FIDO2 or U2F kills our own connection.** The change needs `reboot=True`, the key
   re-enumerates, and the CTAP handle in use dies mid-operation. Expected, not an error — but the
   device list has to survive it, which is the unimplemented `discovery.watch()` all over again.
   Until hotplug exists, the honest behaviour is to say the key is re-plugging itself and to
   invite a Rescan.
3. **A confirm dialog naming the consequence**, for anything that disables a transport or an
   application. "FIDO2 will stop working for every site you have registered this key with"
   is the sentence a user needs before clicking, not afterwards.
4. **Read back after the reboot**, never paint the requested value. Same rule that caught the Dell
   monitor snapping sharpness 55 → 60.

`reboot=True` is set **exactly when the derived USB interface set changes** — switching OATH off
on a key that still has PIV keeps CCID, so nothing re-enumerates and nothing needs re-plugging.
NFC changes never reboot. Copied from `ykman` rather than reinvented, and the message after one is
Yubico's own: *"Configuration updated, remove and reinsert your YubiKey."*

Nothing is read back after a reboot, because the handle it would be read over has just died. The
result says the key is re-plugging itself and invites a Rescan, rather than reporting a value that
was never confirmed.

## 7. The configuration lock code ⚠️

`write_device_config` takes a 16-byte `cur_lock_code` / `new_lock_code`. Once set, **every**
configuration change needs it.

**There is no recovery.** A lost lock code does not lock a PIN that can be reset — it freezes the
key's configuration permanently. Nothing on the key, and nothing Yubico offers, undoes it.

Therefore:

- The lock state is **shown** on Information regardless — a locked key that refuses writes with no
  visible reason is the worst outcome.
- **Reading is unaffected**, so a locked key still shows everything in §5 and §6.
- Setting a lock code is `experimental=True`, behind a confirm dialog that says plainly that
  losing it is permanent and unrecoverable.
- Entering an existing lock code uses the same `prompt` mechanism as the FIDO PIN: asked for when
  needed, held for that operation only, never left on the page.
- ⚠️ Untestable here without accepting the risk on a key in daily use. It ships marked
  experimental or it does not ship in v1.

## 8. Timeouts and NFC restriction 📋

| Control | Field | Requires |
|---|---|---|
| Challenge-response timeout | `challenge_response_timeout` (15 s here) | — |
| Auto-eject timeout | `auto_eject_timeout`, implies touch-eject | CCID enabled |
| Touch eject | `DEVICE_FLAG.EJECT` | CCID enabled |
| NFC restricted | `nfc_restricted` — NFC off until next USB power | firmware **5.7+**, ⚠️ absent on 5.2.6 |

`requires` gating handles the CCID dependency: auto-eject is meaningless with the smartcard
interface disabled, so it disables itself when that toggle goes off, in the same window.

## 9. Firmware < 5.0 is read-only in v1 📋

`write_device_config` is gated at firmware 5.0. Older keys (YubiKey 4, NEO) use `set_mode`, a
coarser interface-combination API that cannot express per-application state and whose failure
modes cannot be checked here — no such key exists to test against.

They therefore get **§5's information and nothing that writes**, with a line saying so. Shipping
an untestable write path to a discontinued key is how devices get bricked by software that means
well. Revisit if such a key turns up.

## 10. What this module must *not* take from ykman 🚫

**Everything under `ykman fido`.** `access change-pin`, `config toggle-always-uv`,
`config set-min-length`, `credentials list/delete`, `fingerprints` — all of it is plain CTAP, all
of it is the base module's job, and routing it through `ykman` would mean two CTAP stacks arguing
over one device handle.

Two of those map onto work the base module has not done yet, and they belong **there**, in
`python-fido2`, not here:

- `fido credentials list/delete` → base §11 credential management
- `fido fingerprints` → CTAP `bioEnroll`, for Bio-series keys

The rule the base module states holds in both directions: **CTAP functionality stays vendor-neutral
in the base**, so a Nitrokey gets it too. Only what is genuinely YubiKey-specific lands here.

## 11. The smartcard interface: accounts in, certificates out 🚫/✅

**OATH accounts are in. PIV and OpenPGP stay out.** The line is not the interface after all — it
is whether something else already owns the job. Storing TOTP codes on the key is what a YubiKey is
most often bought for, and no other Linux tool does it in a settings panel. Certificates and GPG
keys belong to Kleopatra and GnuPG, which do them properly.

### How the Yubico Authenticator handles the contention, verified

`ykman` opens the smartcard **exclusively** by default, so whoever holds it locks the others out.
Their helper is a node tree — `RootNode → DevicesNode → UsbDeviceNode → ConnectionNode(ccid|otp|
fido) → oath|piv|management|ctap2` — and the rule is in `RpcNode.get_child`: **opening a different
child closes the current one first**, via `_close_child`. `RpcNode.close` cascades the same way on
exit. So the app holds **one connection at a time**, tied to the screen you are on, and releases it
when you navigate away or quit.

*(Read out of `helper/authenticator-helper` in the release the user supplied: the PyInstaller
archive unpacks to `helper.device`, `helper.oath`, `helper.base` and the rest, frozen `ykman`
included.)*

**This module is stricter.** Every OATH call opens a connection, does one thing and closes it, so
the card is claimed for milliseconds and free the rest of the time — `gpg-agent`, `scdaemon` and
Kleopatra keep working while the Accounts tab is on screen. When they hold it first, the error
names them and gives the command that fixes it, rather than reporting a bare failure.

**Quitting releases everything.** That turned out to be broken in a way unrelated to YubiKeys:
`_quit` submitted `controller.shutdown()` and then called `bridge.stop()`, which cancels every
pending task — including the shutdown it had just scheduled. Devices were never closed on exit.
It now waits for the shutdown, with a timeout so a stuck device cannot prevent quitting.

### Still out

| Not here | Where instead |
|---|---|
| PIV certificates, PIN and PUK | Kleopatra (GnuPG 2.3+ `scdaemon` speaks PIV) |
| OpenPGP keys, PINs and touch policy | Kleopatra / `gpg --card-edit` / `ykman openpgp` |

## 11-old. The original reasoning, kept for the record 🚫

**OATH, PIV and OpenPGP are not this module's business.** The line is drawn at the interface, not
at the feature, and it is a decision about ownership rather than effort.

`ykman` opens the smartcard **exclusively** by default, falling back to shared only when that is
refused. So while this application held a CCID connection, `gpg-agent`, `scdaemon` and Kleopatra
could not use the card — and when `scdaemon` gets there first, we cannot connect. A settings panel
has no business fighting the tools that legitimately own that interface, and there is no version
of "handle the contention gracefully" that is better than not creating it.

Those tools also already exist and are better: **Kleopatra and GnuPG** for OpenPGP and PIV,
**yubioath** for OATH.

Drawing the line here is what keeps the dependency story honest: **no `pcscd`, no `pyscard`, no
daemon of any kind.** The module needs the CTAP handle it already holds, plus the OTP interface
behind one udev rule. That is the whole of it.

| Not here | Where instead |
|---|---|
| OpenPGP keys, PINs, **touch policy (UIF)** | Kleopatra / `gpg --card-edit` / `ykman openpgp` |
| PIV certificates, PIN and PUK | Kleopatra (GnuPG 2.3+ `scdaemon` speaks PIV) |
| OATH accounts and TOTP codes | yubioath / `ykman oath` |

One honest caveat on the first row: **touch policy is a Yubico extension, not part of the OpenPGP
card specification**, so Kleopatra and `gpg --card-edit` genuinely do not expose it — `ykman` is
the only way to set it. It is the one thing in that table with no non-`ykman` home, and it stays
out anyway, because reaching it means taking the card away from the software that uses it.

*(Assessed and rejected, not overlooked: OATH was briefly rated hard to build. That was wrong —
the Razer macro tab already solves dynamic lists, one action per item rebuilt through
`_bump_capabilities()`, so per-account rows with delete buttons are a solved pattern here. It is
excluded for the exclusivity reason above, not the difficulty one.)*

## 11a. The OTP interface, and the three-second wall ✅ **built**

**A YubiKey serves one USB interface at a time.** Moving between the FIDO interface and the OTP
one costs about three seconds while the key hands over — Yubico call it *reclaim*, and `ykman`
waits it out by retrying six times half a second apart, in
`OtpYubiKeyDevice.open_connection`. Measured here on a YubiKey 5 NFC:

| | |
|---|---|
| OTP read, nothing else touched | **~130 ms** |
| OTP read straight after FIDO traffic | **3036 ms** |
| FIDO read straight after an OTP read | **2886 ms** |
| either, once the window has passed | ~25 ms |

**It is symmetric**, so no ordering avoids it — only not doing it. That single fact set the design
of this tab, after a first version that read the slots on every connect and cost:

| | before | after |
|---|---|---|
| Connect | 3046 ms | **194 ms** |
| Toggle an application | ~6 s | **146 ms** |
| Read the slots | (in the 3 s above) | 3067 ms, on request |
| Re-read, OTP already current | — | 50 ms |

Three rules came out of it:

1. **The slots are read just after connecting, in the background** — not during the handshake,
   and not behind a button. A button was the first attempt and it was wrong: the Yubico
   Authenticator has no such thing, and asking the user to fetch data the application could fetch
   itself reads as broken. Connect returns in ~200 ms, the tab says *Reading…*, and the rows
   arrive about three seconds later through `Device.changes()`.

   The wait happens **outside the device lock** — it is waiting for the key to finish its
   changeover, not for us — so a write started in the meantime is not stuck behind it. Once the
   window has passed the read itself costs ~130 ms.

   This needed one core change: `_watch_changes` now re-shows the page when a push changes the
   capability *set*, not just a value, using the same revision check the write path already had.
   Any module whose shape can change asynchronously gets that.
2. **A rebuild does no I/O.** `_read_slots` is a lookup into what was already read; `_repaint()`
   redraws from cache. A device read hidden inside a redraw costs a reclaim every time something
   unrelated changes.
3. **The base module reads the PIN retry counter once**, in `_connect_sync`, not from `_rebuild`.
   It used to be re-read on every redraw, which turned each one into a CTAP round trip — and
   straight after an OTP read, into a full three-second hand-over. That one cost 3 s of the 6 s a
   slot read took.

Straight after a slot write the OTP interface is already current, so the re-read that confirms the
write is free.

## 11a-i. Reaching it ✅ **built**

The other half of what this module owns. `YubiOtpSession` over an `OtpConnection` — the HID
keyboard interface, which needs a udev rule but **no daemon and no smartcard stack**.

**Binding to the right key: the serial.** `list_otp_devices()` enumerates every attached YubiKey,
and the serial read over CTAP is what says which one is *this* device. Verified: the same serial
comes back over CTAP, OTP and CCID on the attached key. Nothing else joins the interfaces reliably
— the USB product string is identical across two keys of the same model.

**Opened per operation, never held.** The same courtesy the smartcard rule is built on: a held OTP
handle is one no other tool can use. Enumeration stays in `connect()`; §3's rule is unchanged.

## 11b. OTP slots ✅ **built; slot 2 verified on hardware**

Two slots, and what is in them today, read live from the attached key:

| | |
|---|---|
| Slot 1 | **programmed, touch-triggered** |
| Slot 2 | empty |

**Slot 1 ships programmed from the factory with a Yubico OTP credential**, and it is what fires on
a short touch — during this project it typed an OTP into a chat window when the key was brushed.
It is live on essentially every YubiKey anyone owns.

### Laid out the way Yubico lays it out ✅ **built**

The first version put every action in one **Programming** section fed by a shared "Programme into"
dropdown, with the keyboard-layout picker sitting between two unrelated buttons. Shorter, and much
harder to trust: nothing on screen said which slot a button would write to.

The Yubico Authenticator gives each slot its own action list, and that is the fix. **Each slot owns
its actions**, so a button names the slot it writes to and its confirm dialog describes that slot's
contents. Their wording is adopted where it is better than ours — *Short touch* and *Long touch*
rather than slot numbers, *Slot is configured* / *Slot is empty*, *Delete credential*.

| Section | Contents |
|---|---|
| **Slot 1 · short touch** | status, then Yubico's four credential types — challenge-response, OATH-HOTP, Yubico OTP, static password — and Delete credential |
| **Slot 2 · long touch** | the same six |
| *(no Options section)* | every modifier lives in the dialog of the action it modifies — §11b-i |
| **NFC tag** | a slot picker and what a tap sends. Only on a key with NFC |
| **Both slots** | Re-read from the key, Swap short and long touch |

**The modifiers are not on the page at all.** Naming them *"Require touch (challenge-response)"*
was the first attempt and it was still wrong: a switch sitting beside four buttons belongs to none
of them visibly, whatever its label says. Yubico puts each one **inside the dialog of the thing it
changes**, and that is now what happens here. The Options section is gone.

**The challenge-response button says "challenge-response".** It said "Programme HMAC-SHA1", which
names the algorithm rather than the thing anyone is looking for, and led to it being reported as
missing while it was on screen.

### 11b-i. The dialogs ✅ **built**

Each programming action opens a dialog carrying exactly what that credential needs — the shape
Yubico's own dialogs have:

| Action | Fields |
|---|---|
| Challenge-response | Secret key *(masked, 0/40, generate)*, Require touch, Access code |
| OATH-HOTP | Secret key *(masked)*, Code length 6/8, Access code |
| Yubico OTP | Public ID *(0/12)*, Private ID *(masked, 0/12, generate)*, Secret key *(masked, 0/32, generate)*, Access code |
| Static password | Password *(masked, 0/38, generate)*, Keyboard layout, Access code |
| NFC tag | URI or text, Access code |

The access code moved in here too. It is needed by every write and by none of the reads, so a
permanent field on the page was both clutter and a secret left on screen.

This needed a core addition, **`Capability.prompt_fields`**: a tuple of `PromptField`, each with a
kind, a default, `secret`, `optional`, `max_length` and `generate`. The shell renders masked
fields with a reveal button, a live *x/n* counter, checkboxes and dropdowns, and hands the module a
`dict` of answers in place of the action's value. Any module gains it — the single-secret `prompt`
stays for the FIDO PIN, which genuinely is one field.

Only what is left blank is generated. A public identity typed in is used as given; empty derives it
from the serial. A malformed one is refused here, with the modhex alphabet named, rather than at
the key.

### The two generated credentials

Both hand back a secret that **cannot be read off the key afterwards**, so both show it once:

- **OATH-HOTP** takes the base32 secret a service issues, hex, or nothing — in which case one is
  generated and returned *as base32*, the form whatever checks the codes will want. Not to be
  confused with the OATH *application*, which holds many credentials on the smartcard interface
  and is out of scope; this is one credential in one slot, typed as keystrokes.
- **Yubico OTP** generates all three values: the public identity from the key's serial, exactly as
  `ykman otp yubiotp --serial-public-id` derives it, and the rest at random. **Nothing is
  uploaded.** The Yubico Authenticator offers to post the credential to YubiCloud; doing that from
  here would mean sending a freshly generated secret to a third party without being asked, so the
  three values are shown and registering them is left to the user.

### Rules that are not optional

1. **Never default to slot 1.** Every write defaults to slot 2 and slot 1 must be chosen
   deliberately, because overwriting it destroys the factory Yubico OTP credential and breaks
   YubiCloud and anything registered against it.
2. **Name what will be destroyed, per slot, from what is actually there.** "Slot 2 is empty" and
   "Slot 2 holds a credential" are different dialogs. A generic "are you sure" is worse than
   nothing here.
3. **A challenge-response secret cannot be recovered.** It exists only on the key and in whatever
   enrolled it — a LUKS header, a `pam_yubico` file. Overwrite the slot and that disk stops
   opening. This is the sharpest hazard anywhere in this project, sharper than the FIDO reset,
   because the reset at least announces itself as destroying everything.
4. **Access codes.** `put_configuration()` and `delete_slot()` take a `cur_acc_code`: a slot can be
   protected by a six-byte code, and without it the write is refused. That refusal must be
   explained as "this slot is protected by an access code", not reported as a failure.
5. **Touch-required is a real choice with a consequence.** A slot that needs a touch cannot unlock
   a disk unattended. The control says so where it is set, not in a manual.

### Untestable here, and how that is handled

Slot 1 is live on the only key available and slot 2 is empty. Writing to either to prove the code
works would mean either destroying a factory credential or leaving a secret on the user's key —
so the write paths ship **verified by tests against a stand-in session**, with the read path
verified on hardware, and say so.

## 12. Dependency and failure handling ✅ **built**

`yubikey-manager` is **optional**, and for the information tab that is stronger than the Razer
module's arrangement: `DependencyMissing` is the wrong answer here, because it would refuse the
connection outright and turn a key the base module handles perfectly into a broken one.

Instead the vendor rows collapse to **one row saying what is missing**, carrying an advisory with
the `emerge` line, and the CTAP half of the page is untouched and fully usable. Three cases reach
it, and all three are normal outcomes rather than faults:

| | |
|---|---|
| `ykman` not installed | the row names `app-crypt/yubikey-manager` and what it adds |
| the key has no management application | a Security Key by Yubico answers nothing — the base page is the correct page for it |
| the FIDO interface is not open | §4's second row |

`DependencyMissing` **is** right for the OTP slots in §11b, where udev permission on the keyboard
interface genuinely gates the operation rather than merely thinning a page. There is no `pcscd`
case to handle at all: §11 puts the whole smartcard interface out of scope, so this module depends
on no daemon.

## 12a. Vendor photographs 🚫 — there is no usable CDN

Checked, because the module framework supports fetching a product photo on an explicit click.
**Yubico publishes no addressable, model-keyed image source.** Product images on `yubico.com` are
WordPress uploads under dated paths — `wp-content/uploads/2021/02/5series-new-edited-1024x406.png`
— which cannot be derived from a model name, and the store is a single-page application with no
`products.json` and no image API behind it.

That is exactly the case the core's `fetch_photo` docstring warns about: follow the vendor's
advertised asset links, never guess a CDN pattern. There are no advertised links, so the module
implements no `fetch_photo` at all rather than one that guesses and 404s. Same conclusion as Dell.

## 13. Verified so far

**YubiKey 5 NFC**, firmware 5.2.6, `1050:0407`, form factor Keychain (USB-A), six applications
supported and enabled on both USB and NFC, unlocked, not FIPS, not SKY. The full information tab
rendered from a live read over CTAP, model name and all.

Generality is covered by tests rather than by hardware, since only one key exists here: a NEO, a
Security Key with no serial, a FIPS key with a part number, a key with no NFC, and a key with one
application disabled on one transport are all exercised against stand-in device info — as is
`ykman` being absent altogether.

### Written and confirmed on hardware

- **Application toggles** (§6) — enabling and disabling applications over USB and NFC.
- **OTP slot 2** (§11b) — programming and clearing the empty slot.
- **OATH accounts** (§11) — adding one, and its code refreshing by itself.

### Codes have to keep moving ✅ **built and verified**

A time-based code is right only for its period. Read once and left alone, the page shows a wrong
number a minute later — which is worse than showing none, because it still looks correct. The
first version did exactly that, and it took a user noticing to find it.

**The key says when each code dies**, so the next read is scheduled from `Code.valid_to` rather
than from a guessed thirty seconds: accounts may be 20, 30, 45 or 60 seconds and they do not share
a boundary. The refresh aims just *past* the expiry, because asking a moment early returns the
code that is about to die and the page would then be stale for a whole period. A key with nothing
time-based on it runs no loop at all.

Each pass takes the smartcard for a few tens of milliseconds and gives it straight back, so
`gpg-agent` and Kleopatra are not locked out between refreshes. When the account list is unchanged
the codes are pushed as values rather than rebuilding the page, so the tab does not flicker every
thirty seconds.

Measured on the key: codes arriving at t+14.8 s and t+44.9 s — 30.1 seconds apart, two different
values.

**A countdown, beside the code it belongs to.** The loop ticks once a second so the number moves,
and only talks to the key when a code has actually expired — a counter that jumps thirty seconds
at a time is not a counter.

It took three attempts to get right, and each was a real defect rather than a preference:

1. **A row of its own** — which re-entered the *Accounts* section after the delete buttons and so
   printed a **second "Accounts" heading**, the same ordering mistake already fixed once on the
   vendor tab. It also separated a qualifier from the thing it qualifies.
2. **Frozen.** Moved beside the code via `Capability.suffix_from`, it stopped counting: the shell
   routes a pushed value with `_form_for`, which finds the form owning a *row* with that key — and
   a suffix source has no row anywhere, so every per-second push was dropped. A pushed value is now
   offered to every form, each ignoring what it neither owns nor depends on.
3. **One clock for everything.** A single shared countdown showed the soonest expiry on the key,
   so a 60-second account sitting beside a 30-second one was told the wrong time. Each account now
   counts down on its own.

It ends up as `626180     12 s` with a small depleting bar, via `suffix_from` plus
`suffix_total` — a number says how long is left, a bar says it at a glance, and the total is
per-row because periods differ. Counter-based accounts get neither, because nothing expires.

**A copy button, as an icon.** A six-digit code replaced every thirty seconds cannot usefully be
selected with a mouse. `Capability.copyable` puts a Breeze `edit-copy` button after the value —
an icon, not the word, because it sits at the end of every code on the page and a five-letter
label is wider than what it copies. It copies the **value**, never the rendered string, so neither
a unit nor the countdown comes along with it. The status bar confirms it, since a copy leaves no
trace and a silent button reads as broken.

Each account also says which it is — *Time based, 30 s* or *Counter based* — since the period is
per-account and nothing else on screen admits a 60-second account can exist.

### Still not exercised, and why

| | |
|---|---|
| **Slot 1** | holds the factory Yubico OTP credential — the one that types when the key is brushed. Overwriting it is not undoable |
| **Configuration lock code** (§7) | no recovery exists if it is lost |
| **Firmware < 5.0** (§9) | no such key here; deliberately read-only |

The first two need a spare key rather than a decision.
