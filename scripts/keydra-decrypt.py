#!/usr/bin/env python3
"""Read a Keydra backup: one written with a passphrase, or one written to a key.

An encrypted backup that only Keydra can read is not a backup, it is a hostage. This script
is the other half of that promise: it imports nothing from Keydra, needs no running server,
and is short enough to read before trusting. For a backup encrypted to a key it is the only
thing that can read it without handing the private half to a server — which is the point of
having taken it that way.

    pip install cryptography
    ./keydra-decrypt.py payments-20260820-031500-004.ndjson.gz.enc > payments.ndjson

The secret is read from KEYDRA_BACKUP_SECRET, or asked for. Give it the passphrase for a
backup written with one, or the `keydra-sk1:...` private key for one written to a key; the
file says which it needs. The output is one JSON object per line: a header saying which target
the backup came from, then one line per key with its name, its remaining life in milliseconds,
and its value as the store's own serialisation in base64.

The file format, so this can be rewritten in anything:

    header   "KEYDRA-BACKUP-1\\n"   16 bytes
             kdf                     1 byte, 1 = scrypt, 2 = one key, 3 = a list of keys

    kdf 1    N, r, p                 3 x uint32, big endian
             salt                   16 bytes
             base nonce              8 bytes
             key = scrypt(passphrase, salt, N, r, p, dklen=32)

    kdf 2    recipient id            8 bytes, identifies the key it was encrypted to
             ephemeral public key   32 bytes
             base nonce              8 bytes
             shared = X25519(private key, ephemeral public key)
             key    = HKDF-SHA256(shared,
                                  salt = ephemeral public || recipient public,
                                  info = b"keydra-backup-v1", 32)

    kdf 3    count                   1 byte, how many keys the file names
             then count times:
               recipient id          8 bytes
               ephemeral public key 32 bytes
               wrapped file key     48 bytes, AES-256-GCM over the 32-byte file key
             base nonce              8 bytes

             The wrapping key is derived exactly as in kdf 2, per recipient. The wrap uses a
             nonce of twelve zero bytes and the recipient id as its additional data: the
             wrapping key comes from a key pair made for this file and this recipient and used
             for nothing else, so there is exactly one message under it, and the aad stops a
             stanza being moved into somebody else's slot.

             key = the unwrapped file key. Written since phase 48, including for a single
             recipient; kdf 2 is still read so that every file already in a bucket opens.

    frames   final                   1 byte, 0 = more follow, 1 = last
             length                  uint32, ciphertext length including the 16-byte tag
             ciphertext              length bytes, AES-256-GCM

             nonce = base nonce || frame index as 4 bytes, big endian
             aad   = frame index as 4 bytes, big endian || the final byte

The recipient id is the first 8 bytes of SHA-256(b"keydra-recipient:" || public key), so a
file can say it is not for the key you gave it rather than failing as if it were corrupt.

Every frame authenticates its own position and whether it is the last, so a file that was
truncated, reordered or altered is refused rather than half-read. The plaintext of all the
frames concatenated is gzip, and inside that is the NDJSON.
"""

import base64
import getpass
import gzip
import hashlib
import os
import struct
import sys

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:  # pragma: no cover - the message is the point
    sys.exit("This needs the 'cryptography' package: pip install cryptography")

MAGIC = b"KEYDRA-BACKUP-1\n"
KDF_SCRYPT = 1
KDF_X25519 = 2
KDF_RECIPIENTS = 3
SALT_BYTES = 16
BASE_NONCE_BYTES = 8
RECIPIENT_ID_BYTES = 8
WRAPPED_KEY_BYTES = 48
WRAP_NONCE = bytes(12)
PRIVATE_PREFIX = "keydra-sk1:"


def _private_key(secret):
    """The X25519 key out of its text form, refusing anything that is not one."""
    if not secret.strip().startswith(PRIVATE_PREFIX):
        raise SystemExit(
            "This backup was encrypted to a key. Give it the private half, which starts"
            f" with {PRIVATE_PREFIX}."
        )
    raw = secret.strip()[len(PRIVATE_PREFIX):]
    padding = "=" * (-len(raw) % 4)
    return X25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(raw + padding))


def _recipient_id(public_bytes):
    digest = hashlib.sha256(b"keydra-recipient:" + public_bytes).digest()
    return digest[:RECIPIENT_ID_BYTES]


def _key_from_recipient(stream, secret):
    """Reads the X25519 half of the header and agrees on the symmetric key."""
    recipient_id = stream.read(RECIPIENT_ID_BYTES)
    ephemeral_public = stream.read(32)
    base_nonce = stream.read(BASE_NONCE_BYTES)

    private = _private_key(secret)
    mine = private.public_key().public_bytes_raw()
    if _recipient_id(mine) != recipient_id:
        raise SystemExit("This backup was encrypted to a different key than the one supplied.")

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral_public + mine,
        info=b"keydra-backup-v1",
    ).derive(shared)
    return key, base_nonce


def _key_from_recipients(stream, secret):
    """Reads the list of stanzas, finds the one this key opens, and unwraps the file key."""
    count = stream.read(1)[0]
    if count == 0:
        raise SystemExit("This backup names no keys at all.")

    stanzas = []
    for _ in range(count):
        recipient_id = stream.read(RECIPIENT_ID_BYTES)
        ephemeral_public = stream.read(32)
        wrapped = stream.read(WRAPPED_KEY_BYTES)
        stanzas.append((recipient_id, ephemeral_public, wrapped))
    # Behind the stanzas, so every one is read before any is tried.
    base_nonce = stream.read(BASE_NONCE_BYTES)

    private = _private_key(secret)
    mine = private.public_key().public_bytes_raw()
    mine_id = _recipient_id(mine)

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    for recipient_id, ephemeral_public, wrapped in stanzas:
        if recipient_id != mine_id:
            continue
        shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
        wrapping = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=ephemeral_public + mine,
            info=b"keydra-backup-v1",
        ).derive(shared)
        try:
            key = AESGCM(wrapping).decrypt(WRAP_NONCE, wrapped, recipient_id)
        except Exception:
            raise SystemExit(
                "This backup names that key, and the key does not open it. The file has"
                " been altered."
            )
        return key, base_nonce

    plural = "key" if count == 1 else "keys"
    raise SystemExit(
        f"This backup was encrypted to {count} {plural}, and the one supplied is not among them."
    )


def decrypt(stream, secret):
    """Yields the plaintext of each frame, refusing anything that does not add up."""
    magic = stream.read(len(MAGIC))
    if magic != MAGIC:
        raise SystemExit("This is not an encrypted Keydra backup.")

    kdf = stream.read(1)[0]
    if kdf == KDF_RECIPIENTS:
        key, base_nonce = _key_from_recipients(stream, secret)
    elif kdf == KDF_X25519:
        key, base_nonce = _key_from_recipient(stream, secret)
    elif kdf == KDF_SCRYPT:
        if secret.strip().startswith(PRIVATE_PREFIX):
            raise SystemExit("This backup was written with a passphrase, not to a key.")
        n, r, p = struct.unpack(">III", stream.read(12))
        salt = stream.read(SALT_BYTES)
        base_nonce = stream.read(BASE_NONCE_BYTES)
        # 128 * N * r bytes is what scrypt needs; ask for a little more so OpenSSL's own
        # default ceiling does not refuse the parameters the file was written with.
        key = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=32,
            maxmem=128 * n * r * 2,
        )
    else:
        raise SystemExit(
            f"Unknown key derivation ({kdf}); this script knows scrypt and X25519."
        )

    aesgcm = AESGCM(key)

    index = 0
    while True:
        head = stream.read(5)
        if len(head) < 5:
            raise SystemExit("This backup stops in the middle: it was truncated.")
        final = head[0] == 1
        (length,) = struct.unpack(">I", head[1:])
        sealed = stream.read(length)
        if len(sealed) < length:
            raise SystemExit("This backup stops in the middle: it was truncated.")

        nonce = base_nonce + struct.pack(">I", index)
        aad = struct.pack(">I", index) + bytes([1 if final else 0])
        try:
            yield aesgcm.decrypt(nonce, sealed, aad)
        except Exception:
            raise SystemExit(
                "This backup could not be decrypted. Either the secret is not the one it was"
                " written with, or the file has been altered."
            )

        index += 1
        if final:
            return


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <backup.ndjson.gz.enc>")

    secret = (
        os.environ.get("KEYDRA_BACKUP_SECRET")
        or os.environ.get("KEYDRA_BACKUP_PASSPHRASE")
        or getpass.getpass("Passphrase or private key: ")
    )

    with open(sys.argv[1], "rb") as handle:
        compressed = b"".join(decrypt(handle, secret))

    sys.stdout.buffer.write(gzip.decompress(compressed))


if __name__ == "__main__":
    main()
