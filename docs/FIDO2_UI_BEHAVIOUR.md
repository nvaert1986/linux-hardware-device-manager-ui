# FIDO2 security keys — behavioural specification

Converted from **`kcm-fido-keys`**, a KDE Plasma 6 System Settings module built on `libfido2` and
tested against real hardware. Its behaviour is the reference; this records what was kept, what
changed, and why.

Status: ✅ ported and verified on hardware · ⚠️ ported, untested · 🚫 out of scope

---

## 1. The two decisions inherited from the KCM

**Vendor-neutral, through CTAP.** One module serves YubiKey, Nitrokey, OnlyKey, SoloKey, Token2 and
anything else compliant. No `ykman`, no `nitropy`, no per-brand libraries. The trade-off is
deliberate and stated in the KCM's own README: vendor functions outside the FIDO standard — a
YubiKey's OTP/PIV/OATH/OpenPGP applets, its USB/NFC interface toggles — are **not** here. ✅

Those belong in a module that `extends` this one; see
[YUBIKEY_UI_BEHAVIOUR](YUBIKEY_UI_BEHAVIOUR.md). The boundary holds in both directions —
**anything CTAP stays here**, so a Nitrokey gets it too, and only what a vendor's own tooling can
reach goes there.

**Gated on what the key reports.** A control the hardware cannot honour is absent, not present and
failing. ✅

## 2. What changed, and why

| | |
|---|---|
| `libfido2` via C++ → **`python-fido2`** | Yubico's pure-Python CTAP2 library covers the same ground with no bindings to build. Installed from Portage as `dev-python/fido2`. |
| A per-brand module → **a base module others extend** | `extends = "fido2_security_keys"` lets a `yubikeys` module add vendor features while the key still appears once. §7. |
| PIN fields on the page → **a prompt per operation** | §5. |
| Credential management → **Passkeys** | ✅ ported, and renamed. The KCM called them credentials, which is accurate and tells a user nothing. §14. |

## 3. Identifying a key

**HID usage page `0xF1D0`** — the FIDO alliance's — read from the sysfs `report_descriptor`, where
it encodes as `06 d0 f1`. Nothing is opened, so it stays inside the enumeration budget, and no
vendor id appears in the match rule at all. That is what makes this a base module rather than a
YubiKey module. ✅

**A key answers on several interfaces and only one speaks CTAP.** A YubiKey presents an OTP applet
that types like a keyboard alongside its FIDO interface. Two consequences, both found on hardware:
the device is classified by **what it is** rather than by whichever interface sorted first — it was
appearing as a keyboard — and the module opens the node carrying `0xF1D0`, not merely the first. ✅

## 4. Information

Identity (model, USB id, node, AAGUID), then capabilities (CTAP versions, extensions, transports,
algorithms by COSE **name** rather than `-7`, PIN protocols, size limits, PIN state and attempts
remaining), then **every option the key advertises**, labelled. ✅

The option list is the authoritative statement of what a key can do, so it is shown rather than
summarised.

## 5. PIN handling

| # | Rule | Status |
|---|---|---|
| 5.1 | **The PIN is asked for when it is needed**, in a dialog, and kept only for that operation. Fields on the page were tried and were worse: the PIN sat on screen for as long as the page did, and "Test this key" required filling in a box labelled for *changing* the PIN. | ✅ |
| 5.2 | **A change asks for current, new and confirm-new**, and refuses to send unless the two new entries match and the new PIN meets the key's minimum. Straight from the KCM. A mistyped PIN written to a security key is recoverable only by a reset that wipes it. | ✅ |
| 5.3 | A key with no PIN yet gets **no current-PIN field**. The module knows which case it is and says so; the shell does not guess. | ✅ |
| 5.4 | A failed attempt reports **how many remain**. Once they run out the key can only be reset, so the count is the most useful thing to say. | ✅ |
| 5.5 | Setting a *first* PIN must not be reported as a change. The key reports a PIN either way once the write lands, so the wording is decided from what was true beforehand. | ✅ |

## 6. Configuration, and what a key will not let you do

**CTAP 2.1 authenticator configuration requires the `authnrCfg` option.** The test key here — a
YubiKey 5 NFC — does **not** have it, so minimum PIN length, always-require-verification and
enterprise attestation genuinely cannot apply to it. The tab shows an explanation saying so is a
property of the key, not a limitation of this application. ⚠️ *(the controls themselves are
untested: no key here has the option)*

The same key reports `credentialMgmtPreview` but not `credMgmt` — the preview command rather than
the standard one — which is why credential management is a separate piece of work rather than a
line of code.

**Every configuration change needs a PIN token minted for that permission.** `AUTHENTICATOR_CFG`
for policy, `MAKE_CREDENTIAL` for the self-test. An unscoped token is wrong on CTAP 2.1 keys.

## 6a. The self-test, and three bugs it hid

The KCM's self-test is a throwaway `makeCredential` that proves the touch and the PIN work and
stores nothing. Reproduced ✅ — and the first version failed with `PUAT_REQUIRED`, which turned out
to be masking three separate faults:

1. **It never asked for the PIN.** Once a key has one, `makeCredential` *requires* a pin/uv auth
   token; omitting it is precisely what `PUAT_REQUIRED` means.
2. **It signed the wrong hash.** The auth param authenticates the client data of the request it
   accompanies; a different random hash was being signed, so it would have failed even with a PIN.
3. **The token had no permission scope.**

`PUAT_REQUIRED` had no explanation of its own, so it fell through to the raw code — which is how
two of the three stayed hidden behind the first. Every CTAP error a user can hit is now explained
in terms of what to do about it.

## 7. Built to be extended

`Fido2SecurityKey` is written for subclassing: `extra_capabilities()`, `extra_values()` and
`handle_set()`, plus `supports(option)` for gating. A vendor module declares
`extends = "fido2_security_keys"`, inherits every CTAP capability, and adds its own. ✅

The registry claims a device with the **most specialised** matching module, so a YubiKey appears
**once**. Tested: a Nitrokey falls back to the base, manifest discovery order cannot change the
outcome, an `extends` cycle cannot hang, and **disabling the specialisation leaves the key working
through the base** rather than making it unsupported.

Several physical keys are several rows, automatically — each is its own USB device.

## 8. Destructive operations

The KCM's warning is worth repeating: a factory reset **erases every credential and the PIN**, and
cannot be undone. A key whose PIN is lost can only be recovered by resetting it, which wipes it.

| Operation | Guard | Status |
|---|---|---|
| Change / set PIN | prompt with confirmation field | ✅ |
| Policy changes | confirm dialog with the consequence, plus a PIN | ⚠️ no key here has `authnrCfg` |
| Self-test | none needed — stores nothing | ✅ |
| Factory reset | confirm dialog naming exactly what is destroyed | ⚠️ **deliberately untested** |

Most keys only accept a reset within seconds of being plugged in, and it needs a touch. The error
message says so rather than reporting a bare refusal.

## 9. Feedback

Every action reports its outcome — a tick or a cross beside the button, the detail on hover, a line
in the status bar. Actions are the one kind whose effect is invisible, and a successful self-test
previously looked exactly like nothing happening. ✅

## 10. Verified hardware

**YubiKey 5 NFC** (`1050:0407`, AAGUID `2fc0579f-8113-47ea-b116-bb5a8db9202a`) — CTAP
`U2F_V2 / FIDO_2_0 / FIDO_2_1_PRE`, ES256 + EdDSA, `credentialMgmtPreview` without `credMgmt`, no
`authnrCfg`. Information, self-test and PIN change all exercised on it. Factory reset was not, on
purpose: it is a key in daily use.

Everything else is `family` — claimed by usage page alone, with no vendor id involved.

## 11. Not yet ported

- **Fingerprint enrolment** — CTAP `bioEnroll`, for Bio-series keys. Also plain CTAP, also here.
- **AAGUID → model name.** The KCM shows "YubiKey 5 Series with NFC" instead of a raw AAGUID, from
  a community-maintained list it deliberately does not ship and asks the user to download. That is
  this project's `RegistryFetch` pattern exactly, and the same rule applies: nothing vendor-owned
  is redistributed.

  **For YubiKeys this is already solved without any list.** `yubikit.support.get_name()` returns
  the real model name from the device — offline, nothing downloaded, nothing redistributed. The
  fetched list is only needed for keys with no vendor module.
- **Hotplug** — the KCM refreshes its list when a key is inserted or removed. Project-wide,
  `discovery.watch()` is still unimplemented.

---

## 14. Passkeys ✅ **built**

The largest piece of the KCM that had not been ported. Pure CTAP, so it lives in this module and a
Nitrokey gets it on the same terms as a YubiKey.

**Called Passkeys, not credentials.** The same objects have three names — *discoverable
credentials* in the CTAP spec, *resident keys* in the older one, *passkeys* on every website that
asks you to make one. The KCM used "credentials", which is accurate and tells a user nothing. The
tab uses the word people meet in the wild and names the other two in its note.

| Section | Contents |
|---|---|
| **Passkeys** | how many are stored and of what capacity, then one row per account: *site: user* |
| **Rename an account** | only where the key can — see below |
| **Remove an account** | one Delete per passkey, each naming the site |
| **Manage** | Re-read from key |

### Behind a PIN, on purpose

Listing needs a pin/uv token minted for `CREDENTIAL_MGMT` — an unscoped one is refused on 2.1 keys
— and there is **no command that returns everything at once**: it is the relying parties, then the
credentials for each, so a key with a dozen accounts is a dozen round trips. Doing that on every
connect would ask for a PIN nobody wanted to give. The tab is a single *Show passkeys…* button
until asked, the same shape as the OATH accounts.

### The gate that mattered

`credMgmt` is CTAP 2.1; `credentialMgmtPreview` is the 2.1 preview and does less.
`python-fido2` accepts either for reading and deleting, and allows renaming **only** with the
standard command.

The YubiKey 5 NFC this was built against has **the preview and not the standard one** — verified
from its own option list. So renaming genuinely cannot apply to it, and the page says that is a
property of the key rather than hiding the control or offering one that fails. That is the common
case, not an edge: a preview-only key is what most people have.

### Deleting

A confirm dialog naming the site: *"The site will no longer accept this key for that account. It
cannot be undone, and unless you have another way in — a second key, a recovery code, a password —
you may lose access."*

Everything else on this page describes what a key *can* do. These are the accounts living on it,
and deleting one is how somebody loses a login.

### Verified on hardware 2026-08-11

Listed through the UI on the YubiKey 5 NFC: PIN prompt, then the accounts stored on the key.

**Deleting is still untested, deliberately** — it would mean destroying one of the user's real
logins. The confirm dialog and the delete path are covered by tests; nothing has been removed from
a key. A spare key would close this, along with the factory reset and slot 1.
