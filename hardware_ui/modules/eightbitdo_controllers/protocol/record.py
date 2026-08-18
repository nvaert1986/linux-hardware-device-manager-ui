"""Reading and editing a controller's stored configuration.

A :class:`SlotConfig` is one 176-byte profile; a :class:`SuperConfig` is the 532-byte record the
device is programmed with, holding three of them plus a four-byte header. Both wrap the raw bytes
rather than parsing into fields and re-serialising, and that is deliberate: the record contains
bytes nobody has decoded, and a round-trip through a parsed representation would zero them.
Editing in place means an untouched setting is written back exactly as it was found.

**The checksum is a rolling chain**, which is the one genuinely surprising thing here. See
:func:`roll_crc`.
"""

from __future__ import annotations

import struct

from . import fieldmap as fm


def mcrf4xx(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX: reflected polynomial 0x8408, init 0xFFFF, no final xor.

    Recovered from ``libadvance-lib.so``'s ``writeUltimateWiredCustomConfig``, where the table is
    built at runtime -- which is why a search for a static CRC table in the binary found nothing.
    """
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def roll_crc(previous: int, super_record: bytes) -> int:
    """The next checksum in the chain.

    Not a checksum over the record: a **chain step**. The new value is computed over ``previous``'s
    two bytes followed by ``record[2:176]``.

    Proven in the source project across three consecutive saves captured from the vendor app:
    ``f(0x3b6b) = 0x43a0``, ``f(0x43a0) = 0x8781``, ``f(0x8781) = 0xa9fd``.

    It covers only ``[2:176]``, so it spans the two non-checksum header bytes and slot 0 -- **not**
    the mappings in slots 1 and 2, which sit outside it entirely.

    **What ``previous`` must be is settled by the hardware, not by this description.** See
    :data:`SEED_CRC`: the working implementation chains from a fixed seed every time, not from the
    value currently on the controller, and a port that chained from the device wrote records the
    controller accepted and then discarded at power-off.
    """
    return mcrf4xx(bytes([previous & 0xFF, (previous >> 8) & 0xFF]) + bytes(super_record[2:176]))


#: The two non-checksum header bytes, and the checksum every save chains from.
#:
#: **Both are constants, and neither comes from the controller.** That is the opposite of what this
#: port originally did, and reading them from the device is the mistake this comment exists to
#: prevent a second time. It is the obvious design -- the device is the authority on its own state
#: -- and it produced a controller that accepted every write and changed nothing.
#:
#: Two measurements, taken from a real Ultimate Wired over USB, are why:
#:
#: **The stored checksum changes on its own.** Three consecutive reads with no write between them
#: returned ``fb 46 00 ff``, ``a3 58 00 ff``, ``79 02 00 ff``. Whatever those two bytes are, they
#: are not a checksum of the configuration, so chaining the next save from them is chaining from
#: noise -- and a record whose checksum does not verify is discarded. ``8bitdo-cfg`` chains from a
#: fixed ``0xb6fb`` every time and saves successfully, which is now also what this does.
#:
#: **The controller writes its own ``curslot``.** Send ``01`` and read back ``00``. So replaying
#: the captured header's second byte cannot move the active profile, which was the obvious worry
#: about replaying it.
#:
#: Verified end to end afterwards: read, remap Y to X, write, re-read -- the mapping was there and
#: the header carried exactly the checksum we sent.
SEED_CRC = 0xB6FB
HEADER_TAIL = b"\x01\xff"


class SlotConfig:
    """One 176-byte profile, edited in place."""

    __slots__ = ("raw",)

    def __init__(self, raw: bytes | bytearray | None = None) -> None:
        if raw is None:
            self.raw = bytearray(fm.SLOT_LEN)
        else:
            if len(raw) != fm.SLOT_LEN:
                raise ValueError(f"a slot is {fm.SLOT_LEN} bytes, got {len(raw)}")
            self.raw = bytearray(raw)

    # ------------------------------------------------------------------ state

    @property
    def written(self) -> bool:
        """True when this slot holds a real profile.

        An unwritten slot reads as all ``0xff`` and a deleted one as zeros; only a real profile
        carries the marker. Checked rather than assumed because the UI must offer "create" for an
        empty slot and "reset" for a used one, and getting it backwards silently discards a
        profile.
        """
        return bytes(self.raw[0:4]) == fm.MARKER

    # ------------------------------------------------------------------ button maps

    def get_map(self, input_name: str) -> str:
        code = struct.unpack_from("<H", self.raw, fm.map_offset(input_name))[0]
        return fm.CODE_TO_NAME.get(code, "NONE")

    def set_map(self, input_name: str, output: str) -> None:
        struct.pack_into("<H", self.raw, fm.map_offset(input_name), fm.CODE[output])

    def get_paddle(self, paddle: str) -> str:
        code = struct.unpack_from("<H", self.raw, fm.REL_PADDLE[paddle])[0]
        return fm.CODE_TO_NAME.get(code, "NONE")

    def set_paddle(self, paddle: str, output: str) -> None:
        struct.pack_into("<H", self.raw, fm.REL_PADDLE[paddle], fm.CODE[output])

    # ------------------------------------------------------------------ toggles

    def get_toggle(self, name: str) -> bool:
        offset, bit = fm.TOGGLES[name]
        return bool(self.raw[offset] & bit)

    def set_toggle(self, name: str, on: bool) -> None:
        """Set a flag bit, plus the two pieces of bookkeeping the vendor app does.

        The companion bit and the exclusivity rule are both carried from the app's own behaviour
        rather than invented. Neither is a user-facing setting, and a record that disagrees with
        the app on either may simply not be honoured.
        """
        offset, bit = fm.TOGGLES[name]
        if on:
            self.raw[offset] |= bit
        else:
            self.raw[offset] &= (~bit) & 0xFF

        # Enabling one of a mutually exclusive pair clears the other, as the app does.
        if on:
            for first, second in fm.EXCLUSIVE:
                other = second if name == first else first if name == second else None
                if other is not None:
                    other_offset, other_bit = fm.TOGGLES[other]
                    self.raw[other_offset] &= (~other_bit) & 0xFF

        # FLAG2 bit 0x08 tracks "rumble or impulse is away from default".
        if name in ("NO_RUMBLE", "NO_IMPULSE"):
            if self.raw[fm.REL_FLAG2] & 0xC0:
                self.raw[fm.REL_FLAG2] |= fm.VIBRATION_COMPANION
            else:
                self.raw[fm.REL_FLAG2] &= (~fm.VIBRATION_COMPANION) & 0xFF

    # ------------------------------------------------------------------ value bytes

    def get_value(self, kind: str, side: str) -> int:
        """A 0-100 percentage for a vibration, dead-zone or trigger byte."""
        return fm.raw_to_pct(self.raw[_value_offset(kind, side)], kind)

    def set_value(self, kind: str, side: str, pct: float) -> None:
        self.raw[_value_offset(kind, side)] = fm.pct_to_raw(pct, kind)

    # ------------------------------------------------------------------ misc

    def clone(self) -> SlotConfig:
        return SlotConfig(bytes(self.raw))

    def to_dict(self) -> dict[str, object]:
        """A plain summary, for the CLI and for export."""
        return {
            "written": self.written,
            "maps": {name: self.get_map(name) for name in fm.REMAPPABLE},
            "paddles": {name: self.get_paddle(name) for name in fm.PADDLES},
            "toggles": {name: self.get_toggle(name) for name in fm.TOGGLES},
            "vib": {side: self.get_value("VIB", side) for side in fm.REL_VIB},
            "dz": {side: self.get_value("DZ", side) for side in fm.REL_DZ},
            "trig": {side: self.get_value("TRIG", side) for side in fm.REL_TRIG},
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SlotConfig) and self.raw == other.raw

    def __repr__(self) -> str:
        state = "profile" if self.written else "empty"
        return f"<SlotConfig {state} {bytes(self.raw[:4]).hex()}>"


_OFFSETS = {"VIB": fm.REL_VIB, "DZ": fm.REL_DZ, "TRIG": fm.REL_TRIG}


def _value_offset(kind: str, side: str) -> int:
    try:
        return _OFFSETS[kind][side]
    except KeyError as exc:
        raise KeyError(f"no {kind} value for side {side!r}") from exc


class SuperConfig:
    """The 532-byte record: a four-byte header and three slots.

    Held as raw bytes with the slots as views onto copies, so a byte nobody has decoded survives a
    read-modify-write untouched.
    """

    __slots__ = ("header", "slots")

    def __init__(self, raw: bytes | bytearray) -> None:
        if len(raw) != fm.SUPER_LEN:
            raise ValueError(f"a super-config is {fm.SUPER_LEN} bytes, got {len(raw)}")
        self.header = bytearray(raw[0:4])
        self.slots = [
            SlotConfig(raw[fm.slot_base(i):fm.slot_base(i) + fm.SLOT_LEN])
            for i in range(fm.SLOT_COUNT)
        ]

    # ------------------------------------------------------------------ header

    @property
    def crc(self) -> int:
        """The checksum currently in the record. The next write chains from this."""
        return struct.unpack_from("<H", self.header, 0)[0]

    @property
    def active_slot(self) -> int | None:
        """Which profile the device says is active, or None if the byte is out of range.

        Read-only here, and deliberately so: this application never writes it. Which profile is
        live is chosen with the controller's own button, and moving it from software would change
        the device out from under the person holding it.
        """
        value = self.header[2]
        return value if value < fm.SLOT_COUNT else None

    # ------------------------------------------------------------------ assembly

    def to_bytes(self, previous_crc: int | None = None, *, seed_header: bool = False) -> bytes:
        """Reassemble, rolling the checksum from ``previous_crc``.

        Passing None leaves the header's own checksum in place, which is only right when replaying
        a record verbatim -- a test, or a round-trip check.

        *seed_header* replaces the two non-checksum header bytes with :data:`HEADER_TAIL` before
        the checksum is computed, which is what a real save must do. It is a keyword and not the
        default so that a round-trip test can still assemble a record exactly as it was read.
        """
        out = bytearray(self.header)
        for slot in self.slots:
            out += slot.raw
        if len(out) != fm.SUPER_LEN:  # pragma: no cover - guards an edit to the field map
            raise ValueError(f"assembled {len(out)} bytes, expected {fm.SUPER_LEN}")
        if seed_header:
            out[2:4] = HEADER_TAIL
        if previous_crc is not None:
            struct.pack_into("<H", out, 0, roll_crc(previous_crc, out))
        return bytes(out)

    @classmethod
    def from_slots(cls, slots: list[SlotConfig], header: bytes) -> SuperConfig:
        """Build one from slots read individually, which is all BLE can do.

        The 532-byte record is not readable over BLE -- only the three 176-byte slots are -- so the
        four header bytes have to come from somewhere else: a USB read, or the last one this
        application wrote.
        """
        if len(header) != 4:
            raise ValueError(f"the header is 4 bytes, got {len(header)}")
        if len(slots) != fm.SLOT_COUNT:
            raise ValueError(f"expected {fm.SLOT_COUNT} slots, got {len(slots)}")
        raw = bytearray(header)
        for slot in slots:
            raw += slot.raw
        return cls(raw)

    def __repr__(self) -> str:
        return (f"<SuperConfig crc=0x{self.crc:04x} active={self.active_slot} "
                f"slots={[s.written for s in self.slots]}>")


def empty_slot() -> SlotConfig:
    """A deleted profile: a zeroed marker, which is what the vendor app writes to delete one.

    Note it is zeros rather than ``0xff``. An unwritten slot from the factory reads as ``0xff``,
    but the app deletes by zeroing, and matching the app is the safer of the two.
    """
    return SlotConfig(bytes(fm.SLOT_LEN))


__all__ = ["HEADER_TAIL", "SEED_CRC", "SlotConfig", "SuperConfig", "empty_slot", "mcrf4xx",
           "roll_crc"]
