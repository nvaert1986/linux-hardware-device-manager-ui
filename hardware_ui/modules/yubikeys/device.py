"""YubiKey -- the vendor layer on top of the CTAP base module.

Information only, for now. What CTAP cannot tell you about a YubiKey: which model it is, what
firmware it runs, its serial number, and which of its applications answer over USB and over NFC.
Everything the FIDO2 standard covers is inherited from :class:`Fido2SecurityKey` unchanged.

**One connection, already open.** ``ManagementSession`` accepts an ``OtpConnection``, a
``SmartCardConnection`` *or a* ``FidoConnection`` -- and the CTAP backend implements both reads and
writes as vendor commands. So this rides on the CTAP handle the base module already holds: no
``pcscd``, no smartcard stack, no access to the OTP keyboard interface, no second device handle,
and no second set of permissions to explain. See ``docs/YUBIKEY_UI_BEHAVIOUR.md`` §2.

**Every YubiKey ``ykman`` knows, not the one on the desk.** Model names come from
``yubikit.support.get_name``, which handles NEO, Security Key, 4, 5 and Bio and guesses sensibly
when the hardware platform is unknown; application names come from the ``CAPABILITY`` enum. No
product id, model name or firmware version is hard-coded anywhere in this module, and every row is
emitted only if the key reports it -- a Security Key with no serial number and an unknown form
factor simply shows fewer rows.

**``ykman`` is optional.** Without it the vendor rows are replaced by one row saying so, and the
CTAP half of the page carries on working. A missing convenience must not turn a key the base
module handles perfectly into a broken one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator
from typing import Any

from hardware_ui.core import (
    Advisory,
    Capability,
    CapabilitySet,
    CapabilityValue,
    DeviceError,
    Kind,
    NotSupported,
)
from hardware_ui.modules.fido2_security_keys.device import Fido2SecurityKey

from . import capabilities as C
from . import oath as OATH
from . import otp as OTP

log = logging.getLogger(__name__)

MODULE_ID = "yubikeys"

#: One form is built per group, in list order, and each becomes a tab -- so a tab this module adds
#: would otherwise land after Configuration simply because its rows are appended last. The order
#: below is the reading order: what the key is, then what is on it, then what changes it.
_TAB_ORDER = (C.GROUP_INFO, C.GROUP_VENDOR, C.GROUP_APPS, C.GROUP_ACCOUNTS, C.GROUP_SLOTS)

DEFAULT_LAYOUT = "US"

CODE_MARGIN = 0.5
"""Seconds past a code's expiry before fetching the next one.

Aimed just *after* the boundary, not at it: asking a moment early returns the code that is about
to die, and the page would show a stale one for the whole of the next period.
"""

RECLAIM_WAIT = 3.2
"""Seconds to let the USB interface hand-over finish before reading the OTP slots.

A YubiKey serves one interface at a time; ``ykman`` waits the changeover out by retrying for three
seconds. Sitting out that window first turns a 3 s read into a 130 ms one -- which matters because
the read holds the device lock, and holding it for three seconds would block a write the user
started in the meantime.
"""


class YubiKey(Fido2SecurityKey):
    """One YubiKey: the CTAP base module, plus what ``ykman`` can add over the same handle."""

    def __init__(self, info: Any) -> None:
        super().__init__(info)
        self._yk: Any = None
        self._yk_rows: dict[str, str] = {}
        self._yk_problem: str = ""
        self._slots: dict[int, OTP.SlotState] = {}
        self._slots_by_serial: dict[int, dict[int, OTP.SlotState]] = {}
        self._slots_read = False
        self._slot_problem: str = ""
        self._accounts: OATH.Accounts | None = None
        self._accounts_problem: str = ""
        self._oath_password: str = ""
        self._sent_countdowns: dict[str, int] = {}
        self._slot_task: asyncio.Task[None] | None = None
        self._pushed: asyncio.Queue[CapabilityValue] = asyncio.Queue()
        self._apps: list[tuple[str, str, str, bool]] = []
        # The programming controls are this module's own state, not the key's: a YubiKey cannot be
        # asked which slot you are about to write to.
        # Only the NFC-tag slot picker is page state now. Everything else a write needs is asked
        # for in the dialog of the action that needs it.
        self._choices: dict[str, Any] = {C.NDEF_SLOT_KEY: OTP.SLOT_TWO}

    # ------------------------------------------------------------------ lifecycle

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        """Connect over FIDO, then fetch the OTP slots in the background.

        Not during the handshake, and not behind a button either. A YubiKey serves one USB
        interface at a time and takes about three seconds to hand over between them, so reading
        the slots inline made every connect cost three seconds -- but a button asking the user to
        do it themselves is not the answer the Yubico Authenticator gives, and it reads as broken.

        So it happens on its own, just afterwards. The three seconds are spent in a worker thread
        while the page is already up, the hand-over has usually expired by the time it runs, and
        the slots appear a moment later through the change stream.
        """
        await super().connect()
        self._slot_task = asyncio.create_task(self._load_deferred())

    async def disconnect(self) -> None:
        # Held only for the life of the connection, like the FIDO PIN one level down. The
        # smartcard itself is never held at all -- every OATH call opens and closes around one
        # operation, so gpg-agent and Kleopatra keep working throughout.
        self._oath_password = ""
        task, self._slot_task = self._slot_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await super().disconnect()

    async def _load_deferred(self) -> None:
        """Everything that lives on another USB interface, fetched after the page is already up.

        Both the OTP slots and the OATH accounts cost a ~3 s interface hand-over, so they are read
        here rather than during the handshake, one after the other, in a worker thread.
        """
        try:
            # Outside the lock: this is waiting for the *key* to be ready, not for us.
            await asyncio.sleep(RECLAIM_WAIT)
            async with self._lock:
                if self._dev is None:
                    return
                if not self._slots_read:
                    await asyncio.to_thread(self._refresh_slots)
                if self._accounts is None:
                    await asyncio.to_thread(self._refresh_accounts)
                self._repaint()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - each tab explains itself; the key stays usable
            log.debug("background read failed", exc_info=True)
        else:
            await self._wake()
            await self._code_loop()

    async def _code_loop(self) -> None:
        """Keep the TOTP codes current, until the key is disconnected.

        A time-based code is only right for its period, so a page that reads them once shows the
        wrong number a minute later -- which is worse than showing none, because it looks correct.

        The key says when each code dies, so the next read is scheduled from `valid_to` rather than
        from a guessed 30 seconds: accounts may be 20, 30, 45 or 60 and they do not share a
        boundary. Nothing time-based on the key means no loop at all.

        Each pass takes the smartcard for a few tens of milliseconds and gives it straight back,
        so `gpg-agent` and Kleopatra are not locked out between refreshes.
        """
        while True:
            accounts = self._accounts
            expires = accounts.expires_at if accounts else 0
            if not expires:
                return
            # Tick once a second so the countdown moves, and only talk to the key when a code has
            # actually expired. A countdown that jumps thirty seconds at a time is not a countdown.
            #
            # Send first, then wait. Sleeping first left the previous period's number on screen
            # for a second after every refresh, so a thirty-second code appeared to start at
            # twenty-eight.
            while True:
                if self._dev is None:
                    return
                await self._push_countdowns()
                due = expires + CODE_MARGIN
                if time.time() >= due:
                    break
                # Never sleep past the boundary. A flat one-second tick overshoots it by up to a
                # second, and the key is not asked for the next code until the sleep ends -- which
                # is why a thirty-second code was appearing to start at twenty-eight.
                await asyncio.sleep(min(1.0, max(0.05, due - time.time())))
            async with self._lock:
                if self._dev is None:
                    return
                before = [a.key for a in (self._accounts.items if self._accounts else ())]
                await asyncio.to_thread(self._refresh_accounts)
                after = [a.key for a in (self._accounts.items if self._accounts else ())]
                if before != after:
                    # Something was added or removed elsewhere: the page's shape changed, so it
                    # needs rebuilding rather than repainting.
                    self._repaint()
                else:
                    self._values.update(self._account_values())
            await self._push_codes()

    async def _push_codes(self) -> None:
        """Send the new codes to the screen without rebuilding the page.

        The countdowns go with them: a fresh code and a stale "1 s" beside it is worse than
        either, and waiting for the next tick to correct it is a visible second of nonsense.
        """
        for account in self._accounts.items if self._accounts else ():
            await self._pushed.put(
                CapabilityValue(C.account_key(account.key), account.code or "Needs a touch")
            )
        await self._push_countdowns()

    async def _push_countdowns(self) -> None:
        """Only what changed, measured against what was actually *sent*.

        Deduplicating against ``self._values`` looked equivalent and was not: a refresh writes the
        new countdown into that map before anything is pushed, so the first value of every period
        was suppressed and the display jumped from nought straight to twenty-nine. What matters is
        what the screen has been told.
        """
        for account in self._accounts.items if self._accounts else ():
            if not account.valid_to:
                continue
            key = C.account_expires_key(account.key)
            left = _seconds_left(account.valid_to)
            if self._sent_countdowns.get(key) == left:
                continue
            self._sent_countdowns[key] = left
            self._values[key] = left
            await self._pushed.put(CapabilityValue(key, left))

    async def _wake(self) -> None:
        """Nudge the shell, which notices the capability set changed shape and repaints."""
        key = C.slot_key(next(iter(sorted(self._slots)), 1))
        await self._pushed.put(CapabilityValue(key, self._values.get(key, "")))

    def _refresh_accounts(self) -> None:
        """Read the accounts over the smartcard interface, then let go of it immediately."""
        self._accounts_problem = ""
        serial = getattr(self._yk, "serial", None)
        if serial is None:
            self._accounts = OATH.Accounts()
            return
        try:
            self._accounts = OATH.read(serial, self._oath_password)
        except Exception as exc:  # noqa: BLE001 - explained on the tab, never a broken device
            log.info("%s: OATH unavailable (%s)", self.info.name, exc)
            self._accounts = OATH.Accounts()
            self._accounts_problem = str(exc)

    async def changes(self) -> AsyncIterator[CapabilityValue]:
        while True:
            yield await self._pushed.get()

    # ------------------------------------------------------------------ reading

    def _rebuild(self) -> None:
        # Before the base rebuilds the page: it calls extra_capabilities() and extra_values() at
        # the end, and both need this to have run. _reopen() comes back through here too, so
        # "Re-read from key" refreshes the vendor rows as well.
        self._read_management()
        self._read_slots()
        self._repaint()

    def _repaint(self) -> None:
        """Rebuild the page from what has already been read. Touches no hardware.

        Some of the page describes itself rather than the key: the programming dialogs name the
        slot they are about to overwrite. Changing that choice has to redraw them, and it must not
        cost two USB round trips to do it -- ``_bump_capabilities()`` alone only raises the
        revision, it does not rebuild anything.
        """
        super()._rebuild()
        self._set = CapabilitySet(self._in_reading_order(list(self._set)))

    def _in_reading_order(self, caps: list[Capability]) -> list[Capability]:
        """Put this module's tabs where they belong, without disturbing anything else.

        A stable sort, so section order within every tab -- and the relative order of the tabs the
        base module owns -- is exactly what it chose. A page with none of ours comes back
        untouched.
        """
        groups = list(dict.fromkeys(c.group for c in caps))
        anchor = C.GROUP_INFO
        if anchor not in groups:
            return list(caps)
        # Counted as they land, not by position in _TAB_ORDER: a key whose management
        # application is absent has no vendor tab, and a fixed offset would then drop the slots
        # tab a place too far -- behind Configuration.
        inserted = 0
        for group in _TAB_ORDER[1:]:
            if group in groups:
                groups.remove(group)
                inserted += 1
                groups.insert(groups.index(anchor) + inserted, group)
        return sorted(caps, key=lambda c: groups.index(c.group))

    def _read_management(self) -> None:
        """Read the management application over the already-open CTAP handle.

        Never closes anything. ``ManagementSession.close()`` closes the underlying CTAP device --
        which the base module owns and is still using -- so the session is simply dropped.
        """
        self._yk = None
        self._yk_rows = {}
        self._apps = []
        self._yk_problem = ""

        if self._dev is None:
            self._yk_problem = (
                "The key's FIDO interface is not open, so its details cannot be read."
            )
            return
        try:
            from yubikit.management import ManagementSession
        except ImportError:
            self._yk_problem = C.INSTALL_HINT
            return

        try:
            info = ManagementSession(self._dev).read_device_info()
        except Exception as exc:  # noqa: BLE001 - a normal outcome, not a fault; see below
            # Not every YubiKey has the management application: a Security Key by Yubico answers
            # nothing here. That degrades to the base module's page, which is correct for it.
            log.info("%s: no management application (%s)", self.info.name, exc)
            self._yk_problem = (
                "This key does not answer the YubiKey management application, so no model name, "
                "firmware version or application list is available. Everything below comes from "
                "the FIDO2 standard and is unaffected."
            )
            return

        self._yk = info
        self._yk_rows = self._identity_rows(info)
        self._apps = self._applications(info)

    def _read_slots(self) -> None:
        """Pick this key's slots out of the pre-read. **No I/O** -- see :meth:`_connect_sync`.

        Called on every rebuild, and rebuilds happen for reasons that have nothing to do with the
        OTP interface: a PIN change, an application toggle. Touching the hardware here would put a
        three-second reclaim wait behind each of them.
        """
        serial = getattr(self._yk, "serial", None)
        if serial is None:
            self._slots = {}
            if not self._slot_problem:
                self._slot_problem = (
                    "The OTP slots cannot be read: this key reports no serial number, so its OTP "
                    "interface cannot be told apart from another key's."
                )
            return
        self._slots = self._slots_by_serial.get(int(serial), {})

    def _refresh_slots(self) -> None:
        """Read this key's slots from the hardware. **The expensive one.**

        A YubiKey serves one USB interface at a time and takes about three seconds to hand over --
        Yubico call it *reclaim*, and ``ykman`` waits it out by retrying six times half a second
        apart (``OtpYubiKeyDevice.open_connection``). Measured here on a YubiKey 5 NFC, in both
        directions: **~130 ms cold, ~3000 ms straight after traffic on the other interface.**

        It is symmetric, so there is no ordering that avoids it -- only not doing it. Everything
        else this module shows lives on the FIDO interface, so reading the slots at connect made
        every connect cost three seconds and every application toggle cost six. Hence: read when
        the user asks for it, then cache. Straight after a slot write the OTP interface is already
        the current one, so the re-read that confirms the write is free.
        """
        self._slots_read = True
        serial = getattr(self._yk, "serial", None)
        if serial is None:
            self._read_slots()
            return
        try:
            self._slots_by_serial[int(serial)] = OTP.read_state(serial)
            self._slot_problem = ""
        except Exception as exc:  # noqa: BLE001
            log.info("%s: OTP slots unavailable (%s)", self.info.name, exc)
            self._slot_problem = str(exc)
        self._read_slots()

    def _identity_rows(self, info: Any) -> dict[str, str]:
        """The identity rows, each present only if the key actually reports it.

        Deliberately not called ``_describe``: the base class has a method of that name which
        builds the CTAP rows, and shadowing it silently replaces half the page.
        """
        rows: dict[str, str] = {}

        version = getattr(info, "version", None)
        if version:
            rows["firmware"] = str(version)
        if getattr(info, "serial", None):
            rows["serial"] = str(info.serial)

        form = getattr(info, "form_factor", None)
        if form is not None and int(form) != 0:
            rows["form_factor"] = str(form)

        series = self._series(info)
        if series:
            rows["series"] = series

        interfaces = self._interfaces()
        if interfaces:
            rows["interfaces"] = interfaces

        # A lock code only exists from YubiKey 4 onwards. Below that the row would always read
        # "Not set", which is true and useless.
        locked = bool(getattr(info, "is_locked", False))
        if locked or (version and tuple(version) >= (4, 0, 0)):
            rows["lock"] = "Set — configuration changes need the lock code" if locked else "Not set"

        # Firmware 5.7 and later. Absent on everything older, so report it only when it is real.
        if getattr(info, "pin_complexity", False):
            rows["pin_complexity"] = "Required"
        restricted = getattr(getattr(info, "config", None), "nfc_restricted", None)
        if restricted is not None and self._has_nfc(info):
            rows["nfc_restricted"] = (
                "Yes — NFC stays off until the key is next powered over USB" if restricted else "No"
            )
        return rows

    def _series(self, info: Any) -> str:
        """FIPS and Security Key are worth stating; an ordinary key needs no row saying so."""
        marks = []
        if getattr(info, "is_fips", False):
            marks.append("FIPS")
        elif getattr(info, "fips_capable", 0):
            marks.append("FIPS capable")
        if getattr(info, "is_sky", False):
            marks.append("Security Key series")
        part = getattr(info, "part_number", None)
        if part:
            marks.append(f"part {part}")
        return " · ".join(marks)

    def _interfaces(self) -> str:
        """Which USB interfaces this key presents, from its product id.

        Unknown product ids are normal -- ``ykman``'s table only covers the ones it knows -- so
        this is best-effort and the row is dropped rather than guessed at.
        """
        pid = self.info.product_id
        if pid is None:
            return ""
        try:
            from yubikit.core import PID

            return ", ".join(i.name for i in PID(pid).usb_interfaces)
        except Exception:  # noqa: BLE001 - an unrecognised product id is not an error
            return ""

    def _model_name(self, info: Any) -> str:
        """The marketing name, from the key itself.

        This is what retires the AAGUID-to-model lookup for YubiKeys: no community-maintained list
        to download, nothing vendor-owned to redistribute, and it works offline.
        """
        try:
            from yubikit.support import get_name

            return get_name(info, self._key_type())
        except Exception:  # noqa: BLE001 - fall back to whatever the USB descriptor said
            log.debug("get_name failed", exc_info=True)
            return ""

    def _key_type(self) -> Any:
        """The hardware platform, or ``None`` to let ``ykman`` work it out.

        ``get_name`` guesses from the firmware version and the capability set when this is
        ``None``, covering NEO, Security Key and the 4/5 families -- so an unrecognised product id
        costs nothing.
        """
        pid = self.info.product_id
        if pid is None:
            return None
        try:
            from yubikit.core import PID

            return PID(pid).yubikey_type
        except Exception:  # noqa: BLE001
            return None

    def _has_nfc(self, info: Any) -> bool:
        from yubikit.management import TRANSPORT

        return bool(info.supported_capabilities.get(TRANSPORT.NFC, 0))

    def _applications(self, info: Any) -> list[tuple[str, str, str, bool]]:
        """``(transport, name, label, enabled)`` for every application the key supports.

        Driven by the ``CAPABILITY`` enum rather than a list in this file, so an application added
        to a future ``ykman`` appears without a change here. An application the key does not have
        on a transport is absent rather than greyed -- the Yubico Authenticator shows a chip only
        when ``capabilities & value != 0``, and that is the right call.
        """
        from yubikit.management import CAPABILITY, TRANSPORT

        supported = info.supported_capabilities
        enabled = info.config.enabled_capabilities
        transports = [(C.USB, TRANSPORT.USB)]
        if self._has_nfc(info):
            transports.append((C.NFC, TRANSPORT.NFC))

        out: list[tuple[str, str, str, bool]] = []
        for name, transport in transports:
            for cap in CAPABILITY:
                if not supported.get(transport, 0) & cap:
                    continue
                label = getattr(cap, "display_name", None) or cap.name
                out.append((name, cap.name, str(label), bool(enabled.get(transport, 0) & cap)))
        return out

    # ------------------------------------------------------------------ hooks

    def extra_capabilities(self) -> list[Capability]:
        if self._yk is None:
            return C.unavailable(self._yk_problem)
        out = C.build(rows=list(self._yk_rows))
        out += C.build_applications(self._apps, locked=bool(getattr(self._yk, "is_locked", False)))
        out += self._timing_capabilities()
        return out + self._account_capabilities() + self._slot_capabilities()

    def _timing_capabilities(self) -> list[Capability]:
        config = getattr(self._yk, "config", None)
        if config is None:
            return []
        return C.build_timing(
            touch_eject=bool((config.device_flags or 0) & C.DEVICE_FLAG_EJECT),
            auto_eject=int(config.auto_eject_timeout or 0),
            chalresp=int(config.challenge_response_timeout or 0),
            has_ccid=self._has_ccid(),
        )

    def _has_ccid(self) -> bool:
        """Whether the smartcard interface is enabled. Both eject controls are about that card."""
        from yubikit.management import CAPABILITY, TRANSPORT

        enabled = getattr(getattr(self._yk, "config", None), "enabled_capabilities", {}) or {}
        usb = enabled.get(TRANSPORT.USB, 0)
        return any(usb & CAPABILITY[name] for name in ("OATH", "PIV", "OPENPGP", "HSMAUTH"))

    def _timing_values(self) -> dict[str, Any]:
        config = getattr(self._yk, "config", None)
        if config is None:
            return {}
        return {
            C.CHALRESP_TIMEOUT_KEY: int(config.challenge_response_timeout or 0),
            C.TOUCH_EJECT_KEY: bool((config.device_flags or 0) & C.DEVICE_FLAG_EJECT),
            C.AUTO_EJECT_KEY: int(config.auto_eject_timeout or 0),
        }

    def _account_capabilities(self) -> list[Capability]:
        if self._accounts is None:
            return C.accounts_loading()
        if self._accounts_problem:
            return [
                Capability(
                    key=C.ACCOUNTS_STATUS_KEY, kind=Kind.READOUT, label="Accounts",
                    group=C.GROUP_ACCOUNTS, section="Accounts", writable=False,
                    note=self._accounts_problem,
                )
            ]
        if self._accounts.locked:
            return C.accounts_locked()
        return C.build_accounts(
            [
                (a.key, a.issuer, a.name, a.touch, a.period if a.valid_to else 0)
                for a in self._accounts.items
            ],
            periods=OATH.PERIODS, algorithms=OATH.ALGORITHMS, digits=OATH.DIGITS,
            capacity=OATH.MAX_ACCOUNTS,
        )

    def _slot_capabilities(self) -> list[Capability]:
        # Not read yet, and deliberately so -- see _refresh_slots. The tab is one button until the
        # user asks, rather than putting a three-second interface hand-over in front of every
        # other tab on the page.
        if not self._slots_read:
            return C.slots_loading()
        if not self._slots:
            return C.slots_unavailable(self._slot_problem) if self._slot_problem else []
        return C.build_slots(
            {n: st.configured for n, st in self._slots.items()},
            has_nfc=self._has_nfc(self._yk),
        )

    def extra_values(self) -> dict[str, Any]:
        if self._yk is None:
            return {C.UNAVAILABLE_KEY: "Not available"}
        values: dict[str, Any] = {f"{C.PREFIX}{k}": v for k, v in self._yk_rows.items()}
        values.update(
            {C.app_key(transport, name): on for transport, name, _, on in self._apps}
        )
        # The base module fills the Model row from the USB product string, which on a YubiKey is
        # the interface list -- "Yubico YubiKey OTP+FIDO+CCID". The key knows its own name.
        name = self._model_name(self._yk)
        if name:
            values["info.model"] = name
        values.update(self._timing_values())
        values.update(self._slot_values())
        values.update(self._account_values())
        return values

    def _account_values(self) -> dict[str, Any]:
        if self._accounts is None:
            return {C.ACCOUNTS_STATUS_KEY: "Reading…"}
        if self._accounts_problem:
            return {C.ACCOUNTS_STATUS_KEY: "Not available"}
        if self._accounts.locked:
            return {}
        if not self._accounts.items:
            return {C.ACCOUNTS_STATUS_KEY: "No accounts stored"}
        out: dict[str, Any] = {
            C.account_key(a.key): (a.code or "Needs a touch") for a in self._accounts.items
        }
        # No rows of their own: each rides after its account's code via `suffix_from`. Per account
        # rather than one shared number, because a 60-second account beside a 30-second one would
        # otherwise be told the wrong time.
        out.update(
            {
                C.account_expires_key(a.key): _seconds_left(a.valid_to)
                for a in self._accounts.items
                if a.valid_to
            }
        )
        return out

    def _slot_values(self) -> dict[str, Any]:
        if not self._slots_read:
            return {C.SLOTS_UNAVAILABLE_KEY: "Reading…"}
        if not self._slots:
            return {C.SLOTS_UNAVAILABLE_KEY: "Not available"} if self._slot_problem else {}
        out: dict[str, Any] = {
            C.slot_key(n): C.describe_slot(st.configured) for n, st in self._slots.items()
        }
        out.update(self._choices)
        return out

    def advisories(self) -> dict[str, Advisory]:
        out = super().advisories()
        if self._yk is None and self._yk_problem:
            out.update(C.unavailable_advisory(self._yk_problem))
        if not self._slots and self._slot_problem:
            out[C.SLOTS_UNAVAILABLE_KEY] = Advisory(message=self._slot_problem)
        # An Erase button for a slot with nothing in it is locked rather than hidden, so the slot
        # keeps its row and the reason is on screen instead of implied by an absence.
        for slot, state in self._slots.items():
            if not state.configured:
                out[C.delete_key(slot)] = Advisory(message=C.EMPTY_SLOT, locked=True)
        return out

    # ------------------------------------------------------------------ writing

    def handle_set(self, key: str, value: Any) -> Any:
        """This module's writes. Everything CTAP is the base class's and never reaches here."""
        if key in self._choices:
            self._choices[key] = value
            # The programming dialogs name the slot they are about to overwrite, so changing the
            # target has to rebuild them -- otherwise picking slot 1 leaves a warning about slot 2.
            return value

        if key.startswith(C.ACCOUNT_DELETE_PREFIX):
            return self._delete_account(key.removeprefix(C.ACCOUNT_DELETE_PREFIX))
        if key.startswith(C.ACCOUNT_CODE_PREFIX):
            return self._account_code(key.removeprefix(C.ACCOUNT_CODE_PREFIX))
        if key == C.ADD_ACCOUNT_KEY:
            return self._add_account(value if isinstance(value, dict) else {})
        if key == C.UNLOCK_KEY:
            return self._unlock_accounts(str(value or ""))
        if key == C.ACCOUNTS_REREAD_KEY:
            self._refresh_accounts()
            self._repaint()
            return self._accounts_problem or "Read from the key."
        if key == C.READ_SLOTS_KEY:
            self._refresh_slots()
            self._repaint()
            return "Read from the key." if self._slots else self._slot_problem
        if key.startswith(C.APP_KEY_PREFIX):
            return self._toggle_applications(key, value)
        if key in (C.TOUCH_EJECT_KEY, C.AUTO_EJECT_KEY, C.CHALRESP_TIMEOUT_KEY):
            return self._write_timing(key, value)
        if key.startswith(C.DELETE_PREFIX):
            return self._delete(int(key.removeprefix(C.DELETE_PREFIX)))
        answers = value if isinstance(value, dict) else {}
        if key.startswith(C.CHALRESP_PREFIX):
            return self._programme_chalresp(int(key.removeprefix(C.CHALRESP_PREFIX)), answers)
        if key.startswith(C.HOTP_PREFIX):
            return self._programme_hotp(int(key.removeprefix(C.HOTP_PREFIX)), answers)
        if key.startswith(C.YUBIOTP_PREFIX):
            return self._programme_yubiotp(int(key.removeprefix(C.YUBIOTP_PREFIX)), answers)
        if key.startswith(C.STATIC_PREFIX):
            return self._programme_static(int(key.removeprefix(C.STATIC_PREFIX)), answers)
        if key == C.NDEF_KEY:
            return self._programme_ndef(answers)
        if key == C.SWAP_KEY:
            return self._swap()
        raise NotSupported(key)

    def _serial(self) -> int | None:
        return getattr(self._yk, "serial", None)

    def _access_code(self, answers: dict[str, Any]) -> Any:
        return OTP.parse_access_code(str(answers.get(C.F_ACCESS) or ""))

    def _programme_chalresp(self, slot: int, answers: dict[str, Any]) -> str:
        answer = str(answers.get(C.F_SECRET) or "")
        supplied = bool(answer.strip())
        secret = OTP.parse_secret(answer)
        OTP.program_chalresp(
            self._serial(), slot, secret,
            require_touch=bool(answers.get(C.F_TOUCH)),
            access_code=self._access_code(answers),
        )
        self._after_write()
        if supplied:
            return f"Slot {slot} now answers challenges with the secret you supplied."
        # The only moment this can be shown: it cannot be read back off the key, and a second key
        # is only interchangeable with this one if it is given the same secret.
        return (
            f"Slot {slot} was programmed with a generated secret:\n\n{secret.hex()}\n\n"
            "Write it down now if you want a backup key — it cannot be read back afterwards."
        )

    def _programme_hotp(self, slot: int, answers: dict[str, Any]) -> str:
        answer = str(answers.get(C.F_SECRET) or "")
        supplied = bool(answer.strip())
        secret = OTP.parse_oath_secret(answer)
        digits = int(answers.get(C.F_DIGITS) or 6)
        OTP.program_hotp(
            self._serial(), slot, secret, digits8=digits == 8,
            access_code=self._access_code(answers),
        )
        self._after_write()
        if supplied:
            return f"Slot {slot} now types a {digits}-digit OATH-HOTP code."
        import base64

        # Shown once, for the same reason as a generated challenge-response secret: the service
        # that will check these codes needs the identical secret, and the key will not give it up.
        b32 = base64.b32encode(secret).decode().rstrip("=")
        return (
            f"Slot {slot} now types a {digits}-digit OATH-HOTP code, from a generated secret:"
            f"\n\n{b32}\n\n"
            "Register that with whatever will be checking the codes — it cannot be read back."
        )

    def _programme_yubiotp(self, slot: int, answers: dict[str, Any]) -> str:
        public_id, private_id, key = OTP.program_yubiotp(
            self._serial(), slot,
            public_id=str(answers.get(C.F_PUBLIC_ID) or ""),
            private_id=str(answers.get(C.F_PRIVATE_ID) or ""),
            key=str(answers.get(C.F_KEY) or ""),
            access_code=self._access_code(answers),
        )
        self._after_write()
        return (
            f"Slot {slot} now holds a Yubico OTP credential.\n\n"
            f"Public identity:  {public_id}\n"
            f"Private identity: {private_id}\n"
            f"Secret key:       {key}\n\n"
            "Nothing was uploaded. Register these three with Yubico's upload form or your own "
            "validation server, or the codes this key types will not verify anywhere."
        )

    def _programme_static(self, slot: int, answers: dict[str, Any]) -> str:
        OTP.program_static(
            self._serial(), slot, str(answers.get(C.F_PASSWORD) or ""),
            layout=str(answers.get(C.F_LAYOUT) or DEFAULT_LAYOUT),
            access_code=self._access_code(answers),
        )
        self._after_write()
        return f"Slot {slot} now types that password."

    def _programme_ndef(self, answers: dict[str, Any]) -> str:
        slot = int(self._choices.get(C.NDEF_SLOT_KEY) or OTP.SLOT_TWO)
        OTP.program_ndef(
            self._serial(), slot, str(answers.get(C.F_VALUE) or ""), self._access_code(answers)
        )
        self._after_write()
        return f"Slot {slot} now sends that when the key is tapped to a phone."

    def _toggle_applications(self, key: str, value: Any) -> Any:
        """Write the whole application matrix in one ``write_device_config``.

        Every toggle declares the others in ``writes_with``, so the shell holds them together and
        this is called once with the complete set -- the composite write exists because they are
        fields of a single message, and sending one alone re-sends the rest from whatever was
        captured mid-sequence.
        """
        from yubikit.management import CAPABILITY, TRANSPORT

        wanted = dict(self._pending_applications(key, value))
        transports = {C.USB: TRANSPORT.USB, C.NFC: TRANSPORT.NFC}

        enabled: dict[Any, int] = {}
        for transport, name, _label, _was in self._apps:
            if wanted.get((transport, name)):
                enabled[transports[transport]] = (
                    enabled.get(transports[transport], 0) | CAPABILITY[name]
                )
            else:
                enabled.setdefault(transports[transport], 0)

        self._refuse_if_unreachable(enabled.get(TRANSPORT.USB, 0))

        config = self._yk.config
        before = config.enabled_capabilities.get(TRANSPORT.USB, 0)
        # ykman reboots exactly when the derived USB interface set changes -- enabling OATH on a
        # key that already has PIV adds no interface, so it needs no re-plug.
        reboot = _interfaces(before) != _interfaces(enabled.get(TRANSPORT.USB, 0))

        import dataclasses

        try:
            from yubikit.management import ManagementSession

            ManagementSession(self._dev).write_device_config(
                dataclasses.replace(config, enabled_capabilities=enabled),
                reboot,
                None,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            if not reboot:
                raise DeviceError(self._explain_config(exc)) from exc
            # We asked the key to restart. It restarting *is* the reply, so the transport
            # failing here is the expected outcome and not a failure to report -- calling it one
            # showed the user an error for a change that had applied, and stopped the shell
            # reopening the key afterwards.
            log.info("%s: link dropped as the key restarted (expected)", self.info.name)

        if reboot:
            # The key re-enumerates and our CTAP handle dies with it, so nothing can be read back.
            # The shell owns what happens next: these capabilities declare `reboots`, so it
            # tolerates the dropped link and reopens the device once it returns.
            return "Configuration updated — the key is restarting."
        self._reopen()
        return value

    def _write_timing(self, key: str, value: Any) -> Any:
        """Write the timing fields, which share a message with everything else in DeviceConfig.

        Never with ``reboot``: none of these changes the USB interface set, so the key stays put
        and the value can be read back.
        """
        import dataclasses

        from yubikit.management import ManagementSession

        pending = value if isinstance(value, dict) else {key: value}
        config = self._yk.config
        flags = int(config.device_flags or 0)
        auto = int(pending.get(C.AUTO_EJECT_KEY, config.auto_eject_timeout or 0) or 0)
        touch = bool(pending.get(C.TOUCH_EJECT_KEY, flags & C.DEVICE_FLAG_EJECT))
        # ykman's own rule: a time to eject after is meaningless unless the button ejects at all,
        # so asking for one asks for both. Doing it silently here beats a write the key ignores.
        if auto:
            touch = True
        flags = (flags | C.DEVICE_FLAG_EJECT) if touch else (flags & ~C.DEVICE_FLAG_EJECT)

        updated = dataclasses.replace(
            config,
            device_flags=flags,
            auto_eject_timeout=auto,
            challenge_response_timeout=int(
                pending.get(C.CHALRESP_TIMEOUT_KEY, config.challenge_response_timeout or 0) or 0
            ),
        )
        try:
            ManagementSession(self._dev).write_device_config(updated, False, None, None)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(self._explain_config(exc)) from exc
        self._reopen()
        return self._values.get(key)

    def _pending_applications(self, key: str, value: Any) -> list[tuple[tuple[str, str], bool]]:
        """The full matrix as it should be after this write.

        A composite write hands over every member's value; anything missing keeps what the key
        currently reports rather than being invented.
        """
        pending = value if isinstance(value, dict) else {key: value}
        out: list[tuple[tuple[str, str], bool]] = []
        for transport, name, _label, was in self._apps:
            k = C.app_key(transport, name)
            out.append(((transport, name), bool(pending.get(k, was))))
        return out

    def _refuse_if_unreachable(self, usb_enabled: int) -> None:
        """Keep at least one interface this application can talk over.

        The Yubico Authenticator's rule, and for the same reason: with only smartcard applications
        left over USB there is no way back in -- not from here, which speaks CTAP and OTP, and not
        from ykman either.
        """
        from yubikit.management import CAPABILITY

        reachable = [n for n in ("OTP", "U2F", "FIDO2") if usb_enabled & CAPABILITY[n]]
        if not reachable:
            raise DeviceError(C.LAST_INTERFACE)

    def _explain_config(self, exc: Exception) -> str:
        text = str(exc)
        if "lock" in text.lower() or getattr(self._yk, "is_locked", False):
            return (
                "This key's configuration is locked, so applications cannot be switched on or off "
                "until the lock code is entered."
            )
        return text

    def _delete(self, slot: int) -> str:
        state = self._slots.get(slot)
        if state is not None and not state.configured:
            raise DeviceError(f"Slot {slot} is already empty.")
        OTP.delete(self._serial(), slot, None)
        self._after_write()
        return f"Slot {slot} was erased."

    def _swap(self) -> str:
        OTP.swap(self._serial())
        self._after_write()
        return "The two slots were exchanged."

    def _add_account(self, answers: dict[str, Any]) -> str:
        OATH.add(
            self._serial(),
            issuer=str(answers.get(C.F_ISSUER) or ""),
            name=str(answers.get(C.F_NAME) or ""),
            secret=str(answers.get(C.F_SECRET) or ""),
            oath_type=str(answers.get(C.F_TYPE) or "TOTP"),
            algorithm=str(answers.get(C.F_ALGORITHM) or "SHA1"),
            digits=int(answers.get(C.F_DIGITS) or 6),
            period=int(answers.get(C.F_PERIOD) or 30),
            touch=bool(answers.get(C.F_TOUCH)),
            password=self._oath_password,
        )
        self._refresh_accounts()
        self._repaint()
        return "The account was added."

    def _delete_account(self, credential_id: str) -> str:
        OATH.delete(self._serial(), credential_id, self._oath_password)
        self._refresh_accounts()
        self._repaint()
        return "The account was removed from the key."

    def _account_code(self, credential_id: str) -> str:
        return OATH.code_for(self._serial(), credential_id, self._oath_password)

    def _unlock_accounts(self, password: str) -> str:
        if not password:
            raise DeviceError("No password was given.")
        self._oath_password = password
        self._refresh_accounts()
        self._repaint()
        if self._accounts_problem:
            self._oath_password = ""
            raise DeviceError(self._accounts_problem)
        return "Unlocked. The password is kept only while this key stays connected."

    def _after_write(self) -> None:
        """Re-read the slots rather than painting what was asked for.

        The confirm dialogs and the Delete buttons are both driven by what each slot holds, so a
        stale read leaves a button offering to delete something that is no longer there.
        """
        self._refresh_slots()
        self._repaint()


def _seconds_left(expires_at: int) -> int:
    """Whole seconds a code still has, rounded up. Never negative.

    ``ceil``, not ``int(...) + 1``. ``int`` truncates *towards zero*, so it rounds the wrong way
    once the boundary has passed and negative remainders appear: it held "1 s" on screen from a
    second before expiry until a second after, a two-second stall that looked exactly like the
    refresh being slow. It is not -- the whole read takes about 25 ms.
    """
    return max(0, math.ceil(expires_at - time.time()))


def _interfaces(capabilities: int) -> int:
    """The USB interface set a capability set implies.

    On firmware 5 and later interfaces are not settable: enabling OATH brings CCID with it,
    enabling FIDO2 brings FIDO. ``ykman`` derives it exactly this way.
    """
    from yubikit.management import CAPABILITY

    return int(CAPABILITY(capabilities).usb_interfaces)


__all__ = ["YubiKey"]
