"""AES-256-CTR for stored Telegram documents, keyed by ENCRYPTION_KEY.

Each document part has its own random 128-bit nonce, saved in the catalog.
CTR keeps sizes and offsets unchanged, so ranges and seeking still work.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_key: bytes | None = None


def configure(key) -> None:
    global _key
    raw = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    _key = bytes.fromhex(raw)


def new_nonce() -> str:
    return secrets.token_hex(16)


def fingerprint(key) -> str:
    raw = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    return hmac.new(
        bytes.fromhex(raw), b"tgdrive-encryption-key-check", hashlib.sha256
    ).hexdigest()


def crypt(nonce: str | None, offset: int, data: bytes) -> bytes:
    """Encrypt or decrypt ``data`` starting at ``offset`` bytes into a document."""
    if nonce is None:
        return data
    if _key is None:
        raise RuntimeError("ENCRYPTION_KEY is not configured")
    if offset % 16:
        raise ValueError("Encrypted reads must start on a 16-byte boundary")
    counter = (int(nonce, 16) + offset // 16) % (1 << 128)
    context = Cipher(
        algorithms.AES(_key), modes.CTR(counter.to_bytes(16, "big"))
    ).encryptor()
    return context.update(data) + context.finalize()


class EncryptionKeyMismatch(RuntimeError):
    pass


def check_drive_key(backend, key) -> None:
    """Bind the drive to its first key; a different key would read garbage."""
    expected = fingerprint(key)
    row = backend.db.settings.find_one({"_id": "encryption"})
    if row is None and backend.db.nodes.find_one(
        {
            "$or": [
                {"parts.nonce": {"$exists": True}},
                {"poster.nonce": {"$exists": True}},
            ]
        }
    ):
        raise EncryptionKeyMismatch(
            "This drive has encrypted files but no saved key check. Restore its "
            "settings from a backup, or start with the original ENCRYPTION_KEY."
        )
    if row is None:
        backend.write(
            backend.db.settings,
            "update_one",
            {"_id": "encryption"},
            {"$setOnInsert": {"document": {"fingerprint": expected}}},
            upsert=True,
        )
        row = backend.db.settings.find_one({"_id": "encryption"})
    if row is not None and row["document"]["fingerprint"] != expected:
        raise EncryptionKeyMismatch(
            "ENCRYPTION_KEY does not match this drive. Restore the original key; "
            "files encrypted with it cannot be read with another key."
        )
