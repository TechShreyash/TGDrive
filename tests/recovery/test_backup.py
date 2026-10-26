"""Portable recovery JSON remains bounded, strict and independent of a backend."""

import json

import pytest

import tgdrive.recovery.backup as backup
from tgdrive.recovery.backup import BackupError, decode_catalog_snapshot


def test_portable_recovery_json_decodes_utf8_without_database_access():
    payload = {"name": chr(233), "revision": 2}
    assert (
        decode_catalog_snapshot(json.dumps(payload, ensure_ascii=False).encode())
        == payload
    )


@pytest.mark.parametrize(
    "content",
    [
        bytes([128, 4]),
        bytes([255]),
        b"{",
        b'{"revision":1,"revision":2}',
        b'{"root":{"id":1,"id":2}}',
    ],
)
def test_portable_recovery_json_rejects_invalid_input_and_duplicate_keys(content):
    with pytest.raises(BackupError):
        decode_catalog_snapshot(content)


def test_portable_recovery_json_enforces_the_shared_size_bound(monkeypatch):
    monkeypatch.setattr(backup, "MAX_STORE_BYTES", 2)
    assert decode_catalog_snapshot(b"{}") == {}
    with pytest.raises(BackupError, match="safe size limit"):
        decode_catalog_snapshot(b"{} ")
