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


def key_record(key) -> dict:
    return {"_id": "encryption", "document": {"fingerprint": fingerprint(key)}}


def check_drive_key(backend, key) -> None:
    """Open only drives created with this key; older TGDrive drives are refused."""
    row = backend.db.settings.find_one({"_id": "encryption"})
    if row is None:
        raise EncryptionKeyMismatch(
            "This database was created by an older TGDrive and cannot be opened. "
            "Use a new empty MongoDB database and a new storage channel."
        )
    if row["document"]["fingerprint"] != fingerprint(key):
        raise EncryptionKeyMismatch(
            "ENCRYPTION_KEY does not match this drive. Restore the original key; "
            "files encrypted with it cannot be read with another key."
        )


def backup_key() -> str:
    """Backup-file key derived from ENCRYPTION_KEY, separate from file encryption."""
    if _key is None:
        raise RuntimeError("ENCRYPTION_KEY is not configured")
    return hmac.new(_key, b"tgdrive-backup-file", hashlib.sha256).hexdigest()
