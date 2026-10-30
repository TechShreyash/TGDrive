"""Stored-document encryption: ranges, key binding and upload/read round trips."""

import secrets

import pytest

from tests.support.mongo_fixtures import FakeMongoBackend
from tgdrive.core import encryption

KEY = "e" * 64


@pytest.fixture(autouse=True)
def key():
    encryption.configure(KEY)


def test_any_aligned_range_decrypts_like_the_whole_document():
    nonce = encryption.new_nonce()
    plain = secrets.token_bytes(3 * 1024 * 1024 + 7)
    cipher = encryption.crypt(nonce, 0, plain)
    assert cipher != plain and len(cipher) == len(plain)
    for start in (0, 16, 512 * 1024, 1024 * 1024, 2 * 1024 * 1024):
        end = start + 1024 * 1024
        assert encryption.crypt(nonce, start, cipher[start:end]) == plain[start:end]
    assert encryption.crypt(None, 0, plain) is plain
    with pytest.raises(ValueError):
        encryption.crypt(nonce, 3, b"x")


def test_counter_wraps_instead_of_overflowing():
    assert len(encryption.crypt("f" * 32, 32, b"a" * 40)) == 40


def test_drive_records_its_first_key_and_rejects_another():
    backend = FakeMongoBackend()
    encryption.check_drive_key(backend, KEY)
    encryption.check_drive_key(backend, KEY)
    with pytest.raises(encryption.EncryptionKeyMismatch, match="does not match"):
        encryption.check_drive_key(backend, "d" * 64)


@pytest.mark.parametrize(
    "node",
    [
        {
            "_id": "a",
            "parts": [{"message_id": 1}, {"message_id": 2, "nonce": "0" * 32}],
        },
        {"_id": "b", "poster": {"message_id": 3, "nonce": "0" * 32}},
    ],
)
def test_encrypted_drive_without_key_record_is_not_rebound(node):
    backend = FakeMongoBackend()
    backend.db.nodes.insert_one(node)
    with pytest.raises(encryption.EncryptionKeyMismatch, match="no saved key"):
        encryption.check_drive_key(backend, KEY)
    assert backend.db.settings.find_one({"_id": "encryption"}) is None


def test_settings_require_a_valid_key(tmp_path):
    from pydantic import ValidationError

    from tests.support.app_fixtures import make_settings
    from tgdrive.core.config import Settings

    values = make_settings(tmp_path).model_dump(by_alias=True)
    values = {
        key: value.get_secret_value() if hasattr(value, "get_secret_value") else value
        for key, value in values.items()
        if value is not None and not key.startswith("managed_")
    }
    for invalid in ({"ENCRYPTION_KEY": "short"}, {}):
        current = {k: v for k, v in values.items() if k != "ENCRYPTION_KEY"}
        with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
            Settings(_env_file=None, **current, **invalid)
