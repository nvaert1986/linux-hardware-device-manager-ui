"""The equalizer band table, decoded.

Wire format, decoded from a capture of Jabra Direct moving each band. 34 bytes on
``CONFIG``/``0x7D``::

    [u8 band count = 5][u8 reserved]  5 x { u16 A  u16 freq*3  u16 gain }  [u16 trailer]

* frequency is stored as **Hz x 3** -- 180, 750, 3000, 12000, 22800 for 60/250/1000/4000/7600 Hz
* gain is **dB x 60**, which matches the catalogue's own ``scale`` converter (``by = 1/60``)
* field ``A`` is constant per band and looks like a Q/bandwidth term. Jabra's UI never varies it,
  so it is preserved verbatim rather than guessed at -- which makes a write a
  **read-modify-write** even though the property has no ``bitmaskInsert``.

"Restore" in the vendor UI simply writes every gain to 0, keeping A and the frequencies. So a
reset is a gain-only write, not a separate command.

The data model only. The source project's ``EqualizerPanel`` is a Qt widget and is not ported --
the shell renders from capabilities, which is the whole point of this application. The *feature* is
fully present: ``capabilities`` builds one slider per band plus Flat, and ``device`` reads and
writes the table.

**Verified on hardware 2026-08-11**, on an Evolve2 85: five bands at 60 Hz, 250 Hz, 1 kHz, 4 kHz
and 7.6 kHz, read and written. An earlier revision of this docstring said the codec was decoded but
not wired to any control, which was true for about an hour and then was not -- if it ever becomes
true again, say why here rather than leaving the reader to guess.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: dB × this = the stored gain. From the catalogue's scale converter, 1/60.
GAIN_SCALE = 60
#: Hz × this = the stored frequency field.
FREQ_SCALE = 3
#: The vendor UI offers ±6 dB in 0.5 dB steps; sliders work in half-decibels to stay integral.
GAIN_MIN_HALF_DB = -12
GAIN_MAX_HALF_DB = 12

BAND_BYTES = 6
HEADER_BYTES = 2
TRAILER_BYTES = 2


@dataclass(frozen=True)
class Band:
    """One equalizer band. `a` is opaque and must be written back unchanged."""

    a: int
    freq_field: int
    gain_raw: int

    @property
    def hz(self) -> int:
        return self.freq_field // FREQ_SCALE

    @property
    def db(self) -> float:
        return self.gain_raw / GAIN_SCALE

    @property
    def label(self) -> str:
        return f"{self.hz / 1000:g} kHz" if self.hz >= 1000 else f"{self.hz} Hz"

    def with_db(self, db: float) -> Band:
        return Band(self.a, self.freq_field, int(round(db * GAIN_SCALE)))


@dataclass(frozen=True)
class Equalizer:
    bands: tuple[Band, ...]
    reserved: int = 0
    trailer: int = 0x0828

    @classmethod
    def decode_read(cls, raw: bytes) -> Equalizer:
        """Decode the **read** reply, whose layout differs from the write payload.

        From the capture (`7d 00` -> 39 bytes incl. the echoed subcommand, 38 after it):

            [u8 count][u8 reserved][u16 band-bytes]
            [u16 freq][u16 gain]                      band 0 — its A is implicit
            ( [u16 A][u8 flag] [u16 freq][u16 gain] ) x (count - 1)
            [u16 trailer]

        So `A` arrives *before* the band it belongs to, band 0's `A` is absent (the write shows it
        as 0x0000), and each later band carries an extra flag byte (0x18, or 0x28 on the top band)
        that the write format does not have. Reading it with the write layout is what produced
        nonsense earlier.
        """
        if len(raw) < 8:
            raise ValueError(f"equalizer read too short: {len(raw)} bytes")
        count = raw[0]
        reserved = raw[1]
        offset = 4
        bands: list[Band] = []
        for index in range(count):
            a = 0
            if index:
                if offset + 3 > len(raw):
                    raise ValueError("equalizer read truncated before a band header")
                a = int.from_bytes(raw[offset:offset + 2], "big")
                offset += 3                      # A plus the flag byte
            if offset + 4 > len(raw):
                raise ValueError("equalizer read truncated inside a band")
            bands.append(Band(
                a=a,
                freq_field=int.from_bytes(raw[offset:offset + 2], "big"),
                gain_raw=int.from_bytes(raw[offset + 2:offset + 4], "big", signed=True),
            ))
            offset += 4
        trailer = (int.from_bytes(raw[offset:offset + 2], "big")
                   if offset + 2 <= len(raw) else 0x0828)
        return cls(tuple(bands), reserved=reserved, trailer=trailer)

    @classmethod
    def decode(cls, raw: bytes) -> Equalizer:
        if len(raw) < HEADER_BYTES + TRAILER_BYTES:
            raise ValueError(f"equalizer payload too short: {len(raw)} bytes")
        count = raw[0]
        expected = HEADER_BYTES + count * BAND_BYTES + TRAILER_BYTES
        if len(raw) < expected:
            raise ValueError(
                f"equalizer declares {count} bands, needing {expected} bytes, got {len(raw)}"
            )
        bands = []
        for index in range(count):
            offset = HEADER_BYTES + index * BAND_BYTES
            bands.append(Band(
                a=int.from_bytes(raw[offset:offset + 2], "big"),
                freq_field=int.from_bytes(raw[offset + 2:offset + 4], "big"),
                gain_raw=int.from_bytes(raw[offset + 4:offset + 6], "big", signed=True),
            ))
        trailer = int.from_bytes(raw[expected - TRAILER_BYTES:expected], "big")
        return cls(tuple(bands), reserved=raw[1], trailer=trailer)

    def encode(self) -> bytes:
        out = bytearray([len(self.bands), self.reserved])
        for band in self.bands:
            out += band.a.to_bytes(2, "big")
            out += band.freq_field.to_bytes(2, "big")
            out += int(band.gain_raw).to_bytes(2, "big", signed=True)
        out += self.trailer.to_bytes(2, "big")
        return bytes(out)

    def with_gains_db(self, values: list[float]) -> Equalizer:
        """Same bands and frequencies, new gains — the only thing a user may change."""
        bands = tuple(
            band.with_db(values[index]) if index < len(values) else band
            for index, band in enumerate(self.bands)
        )
        return Equalizer(bands, self.reserved, self.trailer)

    def flat(self) -> Equalizer:
        return self.with_gains_db([0.0] * len(self.bands))

