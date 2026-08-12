"""A YubiKey's vendor layer, expressed in the shell's schema.

Only what CTAP cannot say. Which YubiKey this is, what firmware it runs, and which of its
applications are switched on over which transport -- everything else on the page is inherited from
:mod:`hardware_ui.modules.fido2_security_keys`, unchanged, so a Nitrokey and a YubiKey still get
the same CTAP treatment.

Nothing here is written for one model. The application list comes from the ``CAPABILITY`` enum and
the labels from ``ykman`` itself, so a key with applications this file has never heard of shows
them correctly, and the YubiKey 5 that happened to be on the desk is not baked in anywhere.
"""

from __future__ import annotations

from collections.abc import Sequence

from hardware_ui.core import Advisory, Capability, Choice, Kind, PromptField

#: A tab of its own, and deliberately not called "YubiKey".
#:
#: The Information tab is the CTAP one: it says the same things about a Nitrokey, a Token2 and a
#: YubiKey, and it was already long before anything vendor-specific was added to it. Everything a
#: vendor knows beyond the standard goes here instead, under a name any vendor module can reuse --
#: so a reader learns once that this tab is "what the standard cannot tell you", rather than
#: learning a new tab name per manufacturer.
GROUP_VENDOR = "Vendor Specific Information"

GROUP_INFO = "Information"
SECTION_KEY = "YubiKey"

PREFIX = "yk."
APP_PREFIX = f"{PREFIX}app."
UNAVAILABLE_KEY = f"{PREFIX}unavailable"

#: Rows in the order they are worth reading. Each is emitted only when the key reports it, so a
#: Security Key with no serial and an unknown form factor simply has fewer rows.
LABELS: dict[str, str] = {
    "firmware": "Firmware version",
    "serial": "Serial number",
    "form_factor": "Form factor",
    "series": "Series",
    "interfaces": "USB interfaces",
    "lock": "Configuration lock",
    "pin_complexity": "PIN complexity",
    "nfc_restricted": "NFC restricted",
}

INSTALL_HINT = (
    "Everything this key does beyond the FIDO2 standard needs app-crypt/yubikey-manager, which "
    "is not installed. That is the model name, firmware version and serial number, and also "
    "enabling or disabling the USB and NFC interfaces, the application list, the OTP slots and "
    "OATH accounts \u2014 none of which the FIDO2 standard can express.\n\n"
    "On Gentoo:  emerge app-crypt/yubikey-manager\n\n"
    "Passkeys, the PIN and factory reset come from the standard and work without it."
)

NOTE_SERIAL = (
    "The serial number is what tells two otherwise identical keys apart. It identifies this "
    "specific key, so it is shown here and kept out of logs."
)
NOTE_LOCK = (
    "While a configuration lock is set, changing which applications are enabled needs the lock "
    "code. Reading is unaffected. A lock code cannot be recovered if it is lost."
)


def unavailable(reason: str) -> list[Capability]:
    """One row standing in for the whole vendor layer when ``ykman`` could not read the key.

    Not an error: the CTAP half of the page is untouched and fully usable, so the key stays
    workable and the row says what is missing rather than leaving a silent gap.
    """
    return [
        Capability(
            key=UNAVAILABLE_KEY, kind=Kind.READOUT, label="YubiKey details",
            group=GROUP_VENDOR, section=SECTION_KEY, writable=False, note=reason,
        )
    ]


def unavailable_advisory(reason: str) -> dict[str, Advisory]:
    return {UNAVAILABLE_KEY: Advisory(message=reason)}


def build(rows: Sequence[str]) -> list[Capability]:
    """*rows* are keys from :data:`LABELS`. Identity only -- the applications are their own tab."""
    return [
        Capability(
            key=f"{PREFIX}{row}", kind=Kind.READOUT, label=LABELS.get(row, row),
            group=GROUP_VENDOR, section=SECTION_KEY, writable=False,
            note=_note_for(row),
        )
        for row in rows
    ]


def _note_for(row: str) -> str:
    if row == "serial":
        return NOTE_SERIAL
    if row == "lock":
        return NOTE_LOCK
    return ""







# --------------------------------------------------------------------------- applications

#: A tab, where the Yubico Authenticator uses a dialog. Same content: every application the key
#: supports, once per transport, toggled independently.
GROUP_APPS = "Applications"

APP_KEY_PREFIX = "app."
USB, NFC = "usb", "nfc"

SECTION_FOR = {USB: "Over USB", NFC: "Over NFC"}

NOTE_APPS = (
    "Enable or disable applications over available transports. An application switched off here "
    "stops answering entirely — over that transport it is as if the key did not have it."
)
NOTE_APPS_REBOOT = (
    "Changing what is enabled over USB re-plugs the key: it disappears and comes back a moment "
    "later. Press Rescan afterwards."
)

#: Applications whose only way in is the smartcard interface. Kept together because the rule that
#: matters -- keep a non-smartcard way to reach the key -- is about exactly this set.
CCID_APPLICATIONS = frozenset({"OATH", "PIV", "OPENPGP", "HSMAUTH"})

CONSEQUENCE = {
    "FIDO2": (
        "Every site you have registered this key with as a passkey or security key will stop "
        "recognising it."
    ),
    "U2F": "Older two-factor sites that use this key as a security key will stop recognising it.",
    "OTP": "The key will stop typing a one-time password when you touch it.",
    "OATH": "Authenticator apps will no longer see the codes stored on this key.",
    "PIV": "Certificates on this key become unavailable, including to Kleopatra and GnuPG.",
    "OPENPGP": "GnuPG, Kleopatra and SSH will stop seeing this key.",
    "HSMAUTH": "YubiHSM Auth credentials on this key become unavailable.",
}

LAST_INTERFACE = (
    "At least one of Yubico OTP, FIDO U2F or FIDO2 has to stay enabled over USB. Those are the "
    "interfaces this application can talk to the key over — switching off all three would leave "
    "no way to switch anything back on, here or in ykman."
)


def app_key(transport: str, name: str) -> str:
    return f"{APP_KEY_PREFIX}{transport}.{name}"


def build_applications(
    rows: Sequence[tuple[str, str, str, bool]], *, locked: bool
) -> list[Capability]:
    """One toggle per application per transport.

    *rows* are ``(transport, application name, label, enabled)``, already filtered to what the key
    supports on that transport -- the Yubico Authenticator shows a chip only when
    ``capabilities & value != 0``, and an application the key does not have should be absent rather
    than greyed.
    """
    keys = tuple(app_key(transport, name) for transport, name, _, _ in rows)
    out: list[Capability] = []
    for index, (transport, name, label, _enabled) in enumerate(rows):
        out.append(
            Capability(
                key=app_key(transport, name), kind=Kind.TOGGLE, label=label,
                group=GROUP_APPS, section=SECTION_FOR.get(transport, transport),
                # Every toggle is one field of a single DeviceConfig written in one call. Held
                # together, or touching one re-sends the others from state captured mid-sequence.
                writes_with=tuple(k for k in keys if k != app_key(transport, name)),
                confirm=True,
                confirm_detail=_app_confirm(transport, name),
                # The key re-enumerates whenever the derived USB interface set changes. Declared
                # on every toggle rather than only the USB ones, because these are written as one
                # message: clicking an NFC toggle sends any staged USB change with it. An
                # unnecessary reconnect costs a second; a missed one leaves a dead handle and
                # reports an error for a change that applied.
                reboots=True,
                note=_app_note(index, transport, locked),
                description=CONSEQUENCE.get(name, ""),
            )
        )
    return out


def _app_confirm(transport: str, name: str) -> str:
    consequence = CONSEQUENCE.get(name, "")
    where = "over USB" if transport == USB else "over NFC"
    reboot = " The key will re-plug itself." if transport == USB else ""
    return f"Switching this off {where}: {consequence}{reboot}".strip()


def _app_note(index: int, transport: str, locked: bool) -> str:
    if index != 0:
        return ""
    note = NOTE_APPS if transport == NFC else f"{NOTE_APPS}\n\n{NOTE_APPS_REBOOT}"
    if locked:
        note = f"{note}\n\n{NOTE_LOCKED}"
    return note


NOTE_LOCKED = (
    "This key has a configuration lock code set, so these cannot be changed until it is entered."
)


# --------------------------------------------------------------------------- OTP slots

GROUP_SLOTS = "OTP Slots"

SLOT_PREFIX = "otp.slot."
CHALRESP_PREFIX = "otp.chalresp."
STATIC_PREFIX = "otp.static."
HOTP_PREFIX = "otp.hotp."
YUBIOTP_PREFIX = "otp.yubiotp."
DELETE_PREFIX = "otp.delete."
NDEF_SLOT_KEY = "otp.ndef.slot"
NDEF_KEY = "otp.ndef"
SWAP_KEY = "otp.swap"
READ_SLOTS_KEY = "otp.read"
SLOTS_UNAVAILABLE_KEY = "otp.unavailable"

# Field names inside the programming dialogs. Every modifier lives in the dialog of the thing it
# modifies -- on the page they sat beside four buttons and belonged to none of them visibly.
F_SECRET = "secret"
F_TOUCH = "touch"
F_DIGITS = "digits"
F_LAYOUT = "layout"
F_PASSWORD = "password"
F_ACCESS = "access"
F_PUBLIC_ID = "public_id"
F_PRIVATE_ID = "private_id"
F_KEY = "key"
F_VALUE = "value"

ACCESS_FIELD = PromptField(
    key=F_ACCESS, label="Access code", secret=True, optional=True, max_length=12,
    description="Only if this slot is locked with one. Twelve hex characters.",
)

#: Yubico's own words for the two slots, and the reason they beat "slot 1" and "slot 2": they say
#: what the user does rather than how the key numbers it.
SLOT_NAMES = {1: "Short touch", 2: "Long touch"}

#: `ykman.scancodes.KEYBOARD_LAYOUT`. A static password is stored as key presses, so the layout is
#: part of the data rather than a display preference.
LAYOUTS = ("US", "UK", "DE", "FR", "IT", "BEPO", "NORMAN", "MODHEX")

NOTE_SLOTS = (
    "A YubiKey has two OTP slots: slot 1 answers a short touch, slot 2 a long one. Slot 1 ships "
    "programmed from the factory with a Yubico OTP credential — that is the string the key types "
    "if you brush it."
)
NOTE_LOADING = (
    "A YubiKey serves one USB interface at a time and takes about three seconds to hand over "
    "between them, so this is fetched just after connecting rather than during it — which would "
    "have put those three seconds in front of every other tab."
)
NOTE_CHALRESP = (
    "Challenge-response is what yubikey-luks, ykfde and offline pam_yubico use. Leave the secret "
    "empty to have one generated.\n\n"
    "The secret cannot be read back off the key afterwards. If you want a second key as a backup, "
    "give both the same secret — that is the only way to make two keys interchangeable."
)
NOTE_HOTP = (
    "OATH-HOTP: the key types the next counter-based code when touched. Paste the base32 secret "
    "the service showed you, or leave it empty to generate one.\n\n"
    "This is one credential in one slot, typed as keystrokes — not the OATH application, which "
    "stores many and lives on the smartcard interface this module deliberately leaves alone."
)
NOTE_YUBIOTP = (
    "A Yubico OTP credential, the kind slot 1 ships with. All three values are generated here — "
    "the public identity from the key's serial, the rest at random — and shown once afterwards.\n\n"
    "Nothing is uploaded. To make the credential usable you register those values yourself, at "
    "Yubico's upload form or with your own validation server."
)
NOTE_STATIC = (
    "A fixed string the key types when touched. It is stored as key presses rather than as text, "
    "so the keyboard layout below has to match the machine it will be typed into."
)
NOTE_ACCESS = (
    "Only for a slot locked with an access code. Leave it empty otherwise — it is not a password "
    "for the key, and putting one here does not set one."
)
NOTE_SWAP = "Exchanges both slots at once, including which one answers a short touch."
NOTE_NDEF = (
    "What this key sends when tapped against a phone. A value containing “://” is sent as a URI, "
    "anything else as plain text."
)
EMPTY_SLOT = "Nothing to delete — this slot is empty."

CONFIRM_SLOT_ONE = (
    "Slot 1 normally holds the factory Yubico OTP credential — the string the key types when you "
    "brush it. Overwriting it cannot be undone, and anything registered against that credential, "
    "including Yubico's own service, stops recognising this key."
)
CONFIRM_OCCUPIED = (
    "This slot already holds a credential and it will be destroyed. If it is a challenge-response "
    "secret it exists nowhere else: any disk or login enrolled against it stops opening, and no "
    "backup of the secret can be made after the fact."
)
CONFIRM_EMPTY = "This slot is empty, so nothing is lost."


def slot_key(slot: int) -> str:
    return f"{SLOT_PREFIX}{slot}"


def delete_key(slot: int) -> str:
    return f"{DELETE_PREFIX}{slot}"


def chalresp_key(slot: int) -> str:
    return f"{CHALRESP_PREFIX}{slot}"


def hotp_key(slot: int) -> str:
    return f"{HOTP_PREFIX}{slot}"


def yubiotp_key(slot: int) -> str:
    return f"{YUBIOTP_PREFIX}{slot}"


def static_key(slot: int) -> str:
    return f"{STATIC_PREFIX}{slot}"


def slot_label(slot: int) -> str:
    return f"Slot {slot} · {SLOT_NAMES.get(slot, '').lower()}".rstrip(" ·")


def describe_slot(configured: bool) -> str:
    """Yubico's wording. The key reports only that a slot is occupied, never what is in it."""
    return "Slot is configured" if configured else "Slot is empty"


def slots_unavailable(reason: str) -> list[Capability]:
    return [
        Capability(
            key=SLOTS_UNAVAILABLE_KEY, kind=Kind.READOUT, label="OTP slots",
            group=GROUP_SLOTS, section="Slots", writable=False, note=reason,
        )
    ]


def slots_loading() -> list[Capability]:
    """The tab for the moment between connecting and the background read landing."""
    return [
        Capability(
            key=SLOTS_UNAVAILABLE_KEY, kind=Kind.READOUT, label="Slots",
            group=GROUP_SLOTS, section="Slots", writable=False, note=NOTE_LOADING,
        )
    ]


def build_slots(states: dict[int, bool], *, has_nfc: bool) -> list[Capability]:
    """The OTP Slots tab. *states* maps slot number to whether it is configured.

    Laid out the way the Yubico Authenticator does it: **each slot owns its own actions**, so a
    button says which slot it writes to. The first version had one "Programme into" dropdown that
    every action silently read, which made the page shorter and much harder to trust.
    """
    out: list[Capability] = []
    for index, (slot, configured) in enumerate(sorted(states.items())):
        section = slot_label(slot)
        overwrite = _confirm_for(slot, configured)
        out.append(
            Capability(
                key=slot_key(slot), kind=Kind.READOUT, label="Status",
                group=GROUP_SLOTS, section=section, writable=False,
                note=NOTE_SLOTS if index == 0 else "",
            )
        )
        out.append(
            Capability(
                key=chalresp_key(slot), kind=Kind.ACTION, label="Challenge-response",
                action_label="Programme challenge-response…", group=GROUP_SLOTS, section=section,
                prompt_detail=NOTE_CHALRESP, confirm=True, confirm_detail=overwrite,
                description="Program a challenge-response credential.",
                prompt_fields=(
                    PromptField(
                        key=F_SECRET, label="Secret key", secret=True, optional=True,
                        max_length=40, generate=True,
                        description="40 hex characters, or leave empty to generate one.",
                    ),
                    PromptField(key=F_TOUCH, label="Require touch", kind=Kind.TOGGLE),
                    ACCESS_FIELD,
                ),
            )
        )
        out.append(
            Capability(
                key=hotp_key(slot), kind=Kind.ACTION, label="OATH-HOTP",
                action_label="Programme OATH-HOTP…", group=GROUP_SLOTS, section=section,
                prompt_detail=NOTE_HOTP, confirm=True, confirm_detail=overwrite,
                description="Program an HMAC-SHA1 based credential.",
                prompt_fields=(
                    PromptField(
                        key=F_SECRET, label="Secret key", secret=True, optional=True,
                        description="Base32, as the service showed it. Empty generates one.",
                    ),
                    PromptField(
                        key=F_DIGITS, label="Code length", kind=Kind.CHOICE, default=6,
                        choices=(Choice(6, "6 digits"), Choice(8, "8 digits")),
                    ),
                    ACCESS_FIELD,
                ),
            )
        )
        out.append(
            Capability(
                key=yubiotp_key(slot), kind=Kind.ACTION, label="Yubico OTP",
                action_label="Programme Yubico OTP…", group=GROUP_SLOTS, section=section,
                confirm=True, confirm_detail=overwrite, note=NOTE_YUBIOTP if index == 0 else "",
                description="Program a Yubico OTP credential.", prompt_detail=NOTE_YUBIOTP,
                prompt_fields=(
                    PromptField(
                        key=F_PUBLIC_ID, label="Public ID", optional=True, max_length=12,
                        description="Modhex. Empty derives it from the key's serial number.",
                    ),
                    PromptField(
                        key=F_PRIVATE_ID, label="Private ID", secret=True, optional=True,
                        max_length=12, generate=True, description="Empty generates one.",
                    ),
                    PromptField(
                        key=F_KEY, label="Secret key", secret=True, optional=True,
                        max_length=32, generate=True, description="Empty generates one.",
                    ),
                    ACCESS_FIELD,
                ),
            )
        )
        out.append(
            Capability(
                key=static_key(slot), kind=Kind.ACTION, label="Static password",
                action_label="Programme a password…", group=GROUP_SLOTS, section=section,
                prompt_detail=NOTE_STATIC, confirm=True, confirm_detail=overwrite,
                description="Configure a static password.",
                prompt_fields=(
                    PromptField(
                        key=F_PASSWORD, label="Password", secret=True, max_length=38,
                        generate=True,
                    ),
                    PromptField(
                        key=F_LAYOUT, label="Keyboard layout", kind=Kind.CHOICE, default="US",
                        choices=tuple(Choice(n, n) for n in LAYOUTS),
                        description="The key sends key presses, so this must match the machine.",
                    ),
                    ACCESS_FIELD,
                ),
            )
        )
        out.append(
            Capability(
                key=delete_key(slot), kind=Kind.ACTION, label="Delete credential",
                action_label="Delete credential", group=GROUP_SLOTS, section=section,
                confirm=True, confirm_detail=overwrite,
                description="Remove the credential in this slot.",
            )
        )

    if has_nfc:
        out.append(
            Capability(
                key=NDEF_SLOT_KEY, kind=Kind.CHOICE, label="Slot",
                choices=tuple(Choice(n, slot_label(n)) for n in sorted(states) or (1, 2)),
                group=GROUP_SLOTS, section="NFC tag",
            )
        )
        out.append(
            Capability(
                key=NDEF_KEY, kind=Kind.ACTION, label="What a tap sends",
                action_label="Set the NFC tag…", group=GROUP_SLOTS, section="NFC tag",
                prompt_detail=NOTE_NDEF, confirm=True, note=NOTE_NDEF,
                prompt_fields=(
                    PromptField(key=F_VALUE, label="URI or text"),
                    ACCESS_FIELD,
                ),
            )
        )

    out.append(
        Capability(
            key=READ_SLOTS_KEY, kind=Kind.ACTION, label="Slots",
            action_label="Re-read from the key", group=GROUP_SLOTS, section="Both slots",
            timeout=15.0,
        )
    )
    out.append(
        Capability(
            key=SWAP_KEY, kind=Kind.ACTION, label="Swap slots",
            action_label="Swap short and long touch", group=GROUP_SLOTS, section="Both slots",
            confirm=True, confirm_detail=NOTE_SWAP, note=NOTE_SWAP,
        )
    )
    return out


def _confirm_for(slot: int, configured: bool) -> str:
    if not configured:
        return CONFIRM_EMPTY
    return CONFIRM_SLOT_ONE if slot == 1 else CONFIRM_OCCUPIED


__all__ = [
    "build",
    "build_timing",
    "build_accounts",
    "slots_loading",
    "build_applications",
    "build_slots",
    "describe_slot",
    "slots_unavailable",
    "unavailable",
    "unavailable_advisory",
]


# --------------------------------------------------------------------------- OATH accounts

GROUP_ACCOUNTS = "Accounts"

ACCOUNT_PREFIX = "oath.account."
ACCOUNT_DELETE_PREFIX = "oath.delete."
ACCOUNT_CODE_PREFIX = "oath.code."
ADD_ACCOUNT_KEY = "oath.add"
UNLOCK_KEY = "oath.unlock"
ACCOUNTS_REREAD_KEY = "oath.reread"
ACCOUNTS_STATUS_KEY = "oath.status"
EXPIRES_PREFIX = "oath.expires."

F_ISSUER = "issuer"
F_NAME = "name"
F_TYPE = "type"
F_ALGORITHM = "algorithm"
F_PERIOD = "period"
F_PASSWORD_OATH = "oath_password"

NOTE_ACCOUNTS = (
    "The TOTP and HOTP accounts stored on the key itself — the codes an authenticator app would "
    "otherwise keep on a phone. A YubiKey 5 holds 32.\n\n"
    "These live on the key's smartcard interface, which gpg-agent, scdaemon and Kleopatra also "
    "use, and which can only be held by one program at a time. This application takes it for the "
    "moment it needs it and releases it immediately, so those keep working."
)
NOTE_TOUCH_CODE = "This account needs a touch. Ask for its code, then press the key when it blinks."
NOTE_OATH_LOCKED = (
    "This key's accounts are protected by a password. Unlock them to see the codes; the password "
    "is kept only while the key stays connected."
)
CONFIRM_DELETE_ACCOUNT = (
    "The account is removed from the key. Unless the service's original secret was saved "
    "somewhere else, the only way back is to set it up again from scratch."
)


def account_key(credential_id: str) -> str:
    return f"{ACCOUNT_PREFIX}{credential_id}"


def account_delete_key(credential_id: str) -> str:
    return f"{ACCOUNT_DELETE_PREFIX}{credential_id}"


def account_code_key(credential_id: str) -> str:
    return f"{ACCOUNT_CODE_PREFIX}{credential_id}"


def account_expires_key(credential_id: str) -> str:
    """Each account counts down on its own clock -- periods differ and do not share a boundary."""
    return f"{EXPIRES_PREFIX}{credential_id}"


def account_label(issuer: str, name: str) -> str:
    return f"{issuer}: {name}" if issuer else name


def accounts_loading() -> list[Capability]:
    return [
        Capability(
            key=ACCOUNTS_STATUS_KEY, kind=Kind.READOUT, label="Accounts",
            group=GROUP_ACCOUNTS, section="Accounts", writable=False, note=NOTE_ACCOUNTS,
        )
    ]


def accounts_locked() -> list[Capability]:
    return [
        Capability(
            key=UNLOCK_KEY, kind=Kind.ACTION, label="Accounts",
            action_label="Unlock…", group=GROUP_ACCOUNTS, section="Accounts", note=NOTE_OATH_LOCKED,
            prompt="secret", prompt_label="Password",
            prompt_detail=NOTE_OATH_LOCKED,
        )
    ]


def build_accounts(
    accounts: Sequence[tuple[str, str, str, bool, int]], *, periods: Sequence[int],
    algorithms: Sequence[str], digits: Sequence[int], capacity: int,
) -> list[Capability]:
    """*accounts* are ``(id, issuer, name, needs touch, seconds it is valid for)``.

    The period is zero for anything that does not expire on its own -- counter-based credentials
    and those waiting on a touch.
    """
    out: list[Capability] = []
    if not accounts:
        out.append(
            Capability(
                key=ACCOUNTS_STATUS_KEY, kind=Kind.READOUT, label="Accounts",
                group=GROUP_ACCOUNTS, section="Accounts", writable=False, note=NOTE_ACCOUNTS,
            )
        )
    for index, (cid, issuer, name, touch, period) in enumerate(accounts):
        out.append(
            Capability(
                key=account_key(cid), kind=Kind.READOUT, label=account_label(issuer, name),
                group=GROUP_ACCOUNTS, section="Accounts", writable=False, copyable=not touch,
                suffix_from=account_expires_key(cid) if period else "",
                suffix_total=period,
                note=NOTE_ACCOUNTS if index == 0 else "",
                description=(
                    NOTE_TOUCH_CODE if touch
                    else (f"Time based, {period} s." if period else "Counter based.")
                ),
            )
        )
    for cid, issuer, name, touch, _period in accounts:
        if touch:
            out.append(
                Capability(
                    key=account_code_key(cid), kind=Kind.ACTION,
                    label=account_label(issuer, name), action_label="Show the code",
                    group=GROUP_ACCOUNTS, section="Codes that need a touch", timeout=30.0,
                )
            )
    for cid, issuer, name, _touch, _period in accounts:
        out.append(
            Capability(
                key=account_delete_key(cid), kind=Kind.ACTION,
                label=account_label(issuer, name), action_label="Delete",
                group=GROUP_ACCOUNTS, section="Remove an account",
                confirm=True, confirm_detail=CONFIRM_DELETE_ACCOUNT,
            )
        )

    out.append(
        Capability(
            key=ADD_ACCOUNT_KEY, kind=Kind.ACTION, label="Add an account",
            action_label="Add an account…", group=GROUP_ACCOUNTS, section="Manage",
            description=f"{len(accounts)} of {capacity} used.",
            prompt_detail=(
                "The details the service shows beside its QR code. There is no camera here, so "
                "the secret is typed or pasted."
            ),
            prompt_fields=(
                PromptField(key=F_ISSUER, label="Issuer", optional=True, max_length=62),
                PromptField(key=F_NAME, label="Account name", max_length=64),
                PromptField(key=F_SECRET, label="Secret key", secret=True),
                PromptField(
                    key=F_TYPE, label="Type", kind=Kind.CHOICE, default="TOTP",
                    choices=(Choice("TOTP", "Time based"), Choice("HOTP", "Counter based")),
                ),
                PromptField(
                    key=F_ALGORITHM, label="Algorithm", kind=Kind.CHOICE, default="SHA1",
                    choices=tuple(Choice(a, a.replace("SHA", "SHA-")) for a in algorithms),
                ),
                PromptField(
                    key=F_PERIOD, label="Period", kind=Kind.CHOICE, default=30,
                    choices=tuple(Choice(p, f"{p} sec") for p in periods),
                ),
                PromptField(
                    key=F_DIGITS, label="Code length", kind=Kind.CHOICE, default=6,
                    choices=tuple(Choice(d, f"{d} digits") for d in digits),
                ),
                PromptField(key=F_TOUCH, label="Require touch", kind=Kind.TOGGLE),
            ),
        )
    )
    out.append(
        Capability(
            key=ACCOUNTS_REREAD_KEY, kind=Kind.ACTION, label="Codes",
            action_label="Re-read from the key", group=GROUP_ACCOUNTS, section="Manage",
            timeout=20.0,
        )
    )
    return out


# --------------------------------------------------------------------------- timeouts

TOUCH_EJECT_KEY = "cfg.touch_eject"
AUTO_EJECT_KEY = "cfg.auto_eject"
CHALRESP_TIMEOUT_KEY = "cfg.chalresp_timeout"

SECTION_TIMING = "Timing"

DEVICE_FLAG_EJECT = 0x80
"""``DEVICE_FLAG.EJECT``. Spelled out so this module does not import ykman to draw a page."""

NOTE_TIMING = (
    "How long the key waits, and what its button does with the smartcard. These live on the key "
    "itself, so they apply wherever it is plugged in."
)
NOTE_TOUCH_EJECT = (
    "The button ejects and re-inserts the smartcard instead of typing a one-time password. Useful "
    "where software will not release the card; confusing if you expected a password."
)
NOTE_AUTO_EJECT = (
    "Ejects the smartcard by itself after this long. Zero leaves it inserted. Setting any time at "
    "all turns touch-eject on as well — the key has no way to do one without the other."
)
NOTE_CHALRESP = (
    "How long the key waits for your touch during challenge-response before giving up. It only "
    "matters for a slot programmed to require a touch."
)


def build_timing(
    *, touch_eject: bool, auto_eject: int, chalresp: int, has_ccid: bool
) -> list[Capability]:
    """The three timing fields of ``DeviceConfig``.

    Written in the same message as the application toggles, so they join that ``writes_with``
    group -- sending one alone re-sends the others from whatever was captured mid-sequence.
    """
    keys = (TOUCH_EJECT_KEY, AUTO_EJECT_KEY, CHALRESP_TIMEOUT_KEY)
    together = {k: tuple(x for x in keys if x != k) for k in keys}

    out = [
        Capability(
            key=CHALRESP_TIMEOUT_KEY, kind=Kind.RANGE, label="Challenge-response timeout",
            group=GROUP_APPS, section=SECTION_TIMING, unit="s",
            minimum=0, maximum=255, step=1,
            writes_with=together[CHALRESP_TIMEOUT_KEY],
            note=NOTE_TIMING, description=NOTE_CHALRESP,
        )
    ]
    if not has_ccid:
        return out

    # Both eject controls are about the smartcard, so they say nothing on a key that has none.
    out.append(
        Capability(
            key=TOUCH_EJECT_KEY, kind=Kind.TOGGLE, label="Button ejects the smartcard",
            group=GROUP_APPS, section=SECTION_TIMING,
            writes_with=together[TOUCH_EJECT_KEY],
            description=NOTE_TOUCH_EJECT,
        )
    )
    out.append(
        Capability(
            key=AUTO_EJECT_KEY, kind=Kind.RANGE, label="Eject automatically after",
            group=GROUP_APPS, section=SECTION_TIMING, unit="s",
            minimum=0, maximum=32767, step=10,
            writes_with=together[AUTO_EJECT_KEY],
            description=NOTE_AUTO_EJECT,
        )
    )
    return out
