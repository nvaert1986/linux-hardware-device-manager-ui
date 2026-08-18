"""Sound Blaster X4 unlock-handshake responder.

The device refuses every `5A` command until the host completes an ASCII
challenge/response handshake on the same bulk endpoints:

    HOST -> whoareyou.<appname>\\r\\n
    DEV  -> whoareyou<36-byte challenge>\\r\\n
    HOST -> unlock<64-byte response>\\r\\n
    DEV  -> unlock_OK\\r\\n
    HOST -> SW_MODE1\\r\\n
    DEV  -> 5b ...            (mode confirmed; 5A protocol now live)

The response is **plain AES-256-GCM**:

    key       = BASE_KEY, with key[0:2] <- challenge[0:2]
                          and  key[30:32] <- challenge[2:4]
    nonce     = 12 random bytes
    plaintext = challenge[4:36]
    response  = nonce ++ 4 more random bytes ++ ciphertext(32) ++ tag(16)

That is 16 + 32 + 16 = 64 bytes. Note the response carries **16** random bytes
but only the first 12 are the GCM nonce; the remaining 4 are transmitted and
ignored. The device presumably reads the same 12.

No per-device secret is involved: `BASE_KEY` is hardcoded in Creative's
`CTCDC.dll` and is identical for every unit, and for all 21 CDC devices that
share that DLL. Only 4 bytes of the challenge enter the key, so the handshake is
not replayable — the other 32 bytes are the plaintext, and the device checks the
GCM tag.

### How this was determined

The algorithm was recovered by observing the vendor routine's behaviour rather
than by translating its code:

* Flipping one challenge byte changed exactly 17 output bytes — one body byte
  plus the whole 16-byte trailer. That gives the layout: a byte-for-byte XOR
  keystream over a 32-byte region, plus a 16-byte authenticator.
* Flipping one plaintext bit flipped exactly one ciphertext bit, so the cipher
  runs as a stream — a counter mode, not CBC (which earlier attempts had assumed,
  and which is why they failed).
* The one hot loop ran exactly 256 times and computed `x*2 ^ x` with a
  conditional `^ 0x1b` — GF(2^8) multiply-by-3 under the Rijndael polynomial,
  i.e. AES log/antilog table generation. The DLL *builds* its AES tables at
  runtime, which is why no static AES table or crypto import was ever found in it
  and the cipher looked proprietary.

Counter-mode AES + a 16-byte tag is AES-GCM, and the vendor's own cipher registry
lists `AES-256-GCM`. Verified: this module reproduces the response captured from
real hardware byte-for-byte, and agrees with a Unicorn emulation of the original
routine on randomly generated challenges.
"""

from __future__ import annotations

import os

#: Hardcoded in CTCDC.dll at 0x101DBA74/0x101DBA84. The same for every unit —
#: there is no per-device secret, so nothing here is unit-specific.
BASE_KEY = bytes.fromhex(
    "4f41d31a21279be346f0999d6ec4c3febe98901869c118fbb1256e0ce07b6f0a")

#: Bytes of the challenge that are patched into the key, and where they land.
KEY_PATCH = ((0, 2, 0), (2, 4, 30))     # (chal_start, chal_end, key_offset)

CHALLENGE_LEN = 36
RESPONSE_LEN = 64
#: Random bytes prepended to the response; only NONCE_LEN of them are the nonce.
RANDOM_LEN = 16
NONCE_LEN = 12


class UnlockError(Exception):
    pass


def derive_key(challenge: bytes) -> bytes:
    """Session key: the base key with four challenge bytes patched in."""
    key = bytearray(BASE_KEY)
    for start, end, offset in KEY_PATCH:
        key[offset:offset + (end - start)] = challenge[start:end]
    return bytes(key)


class UnlockResponder:
    """Computes unlock responses. Stateless; cheap to construct."""

    def respond(self, challenge: bytes, rand_bytes: bytes | None = None) -> bytes:
        """challenge -> the 64-byte unlock payload.

        `rand_bytes` exists for the self-test; leave it unset in real use so a
        fresh nonce is drawn. Reusing a nonce under a fixed key is exactly the
        thing GCM must never do.
        """
        if len(challenge) < CHALLENGE_LEN:
            raise UnlockError(
                f"challenge too short: {len(challenge)} bytes, "
                f"need {CHALLENGE_LEN}")
        rnd = rand_bytes if rand_bytes is not None else os.urandom(RANDOM_LEN)
        if len(rnd) != RANDOM_LEN:
            raise UnlockError(f"need {RANDOM_LEN} random bytes, got {len(rnd)}")

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise UnlockError(
                "the 'cryptography' package is required for the unlock "
                "handshake (pip install cryptography)") from exc

        sealed = AESGCM(derive_key(challenge)).encrypt(
            rnd[:NONCE_LEN], challenge[4:CHALLENGE_LEN], None)
        response = rnd + sealed
        if len(response) != RESPONSE_LEN:
            raise UnlockError(
                f"built a {len(response)}-byte response, expected {RESPONSE_LEN}")
        return response


#: Challenge/response pair captured from a real X4 over USB. Ground truth.
CAPTURED_CHALLENGE = bytes.fromhex(
    "1e0478324fb87e13dee9a59302b88fd2fda237257defde17517b4387a6a57649d5ca5641")
CAPTURED_RESPONSE = bytes.fromhex(
    "874c92ee8529afbbf49f4746933d3a42daaaa5bd3aeeb216dc2add2ee6f19d2c"
    "b24102e6ac9b85be844c5c4cb7ae6252fa7114e98004cfb0cf936aee2e0ee17f")


def self_test() -> bool:
    """Reproduce the captured hardware response from its captured challenge."""
    got = UnlockResponder().respond(
        CAPTURED_CHALLENGE, rand_bytes=CAPTURED_RESPONSE[:RANDOM_LEN])
    return got == CAPTURED_RESPONSE


if __name__ == "__main__":
    print("self test:", "PASS" if self_test() else "FAIL")
