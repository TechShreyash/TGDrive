"""V2 streaming authentication precedes bounded decompression."""

import secrets
import stat
import zlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tgdrive.recovery import snapshot_stream
from tgdrive.recovery.backup import BackupError
from tgdrive.recovery.recovery_file import MAGIC

KEY = bytes.fromhex("a1" * 32)


def encrypted(compressed):
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(KEY).encrypt(nonce, compressed, MAGIC)


def stage(tmp_path, content, **limits):
    source = tmp_path / "input.tgdb"
    source.write_bytes(content)
    target = tmp_path / "output.json"
    size = snapshot_stream.decrypt_snapshot(
        source, target, KEY, SimpleNamespace(min_free_bytes=0), **limits
    )
    return target, size


@pytest.mark.parametrize("size", [0, 1, 65536, 65537, 1024 * 1024])
def test_roundtrip_and_private_output(tmp_path, size):
    raw = b"a" * size
    target, actual = stage(tmp_path, encrypted(zlib.compress(raw)))
    assert target.read_bytes() == raw
    assert actual == size
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "input.tgdb",
        "output.json",
    ]


def test_corruption_never_reaches_decompression(tmp_path, monkeypatch):
    content = bytearray(encrypted(zlib.compress(b"secret")))
    content[-1] ^= 1
    decoder = Mock(side_effect=AssertionError("unauthenticated input"))
    monkeypatch.setattr(snapshot_stream.zlib, "decompressobj", decoder)
    with pytest.raises(InvalidTag):
        stage(tmp_path, bytes(content))
    decoder.assert_not_called()
    assert [path.name for path in tmp_path.iterdir()] == ["input.tgdb"]


def test_expansion_limit_preserves_previous_output(tmp_path):
    output = tmp_path / "output.json"
    output.write_bytes(b"previous")
    with pytest.raises(ValueError, match="byte limit"):
        stage(tmp_path, encrypted(zlib.compress(b"a" * 1000000)), max_expanded_bytes=9)
    assert output.read_bytes() == b"previous"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "input.tgdb",
        "output.json",
    ]


@pytest.mark.parametrize(
    "compressed", [b"not zlib", zlib.compress(b"x")[:-1], zlib.compress(b"x") + b"x"]
)
def test_invalid_compression_cleans_temporary_files(tmp_path, compressed):
    with pytest.raises((ValueError, zlib.error)):
        stage(tmp_path, encrypted(compressed))
    assert [path.name for path in tmp_path.iterdir()] == ["input.tgdb"]


def test_ciphertext_limit_before_decryption(tmp_path):
    with pytest.raises(ValueError, match="Invalid recovery file"):
        stage(tmp_path, encrypted(zlib.compress(b"x")), max_recovery_bytes=1)
    assert [path.name for path in tmp_path.iterdir()] == ["input.tgdb"]


def test_disk_exhaustion_cleans_compressed_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(
        snapshot_stream.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0)
    )
    with pytest.raises(BackupError, match="temporary space"):
        stage(tmp_path, encrypted(zlib.compress(b"secret")))
    assert [path.name for path in tmp_path.iterdir()] == ["input.tgdb"]


def test_symlink_input_is_rejected(tmp_path):
    source = tmp_path / "input.tgdb"
    source.write_bytes(encrypted(zlib.compress(b"secret")))
    link = tmp_path / "link.tgdb"
    link.symlink_to(source)
    with pytest.raises(OSError):
        snapshot_stream.decrypt_snapshot(
            link,
            tmp_path / "output.json",
            KEY,
            SimpleNamespace(min_free_bytes=0),
        )
    assert not (tmp_path / "output.json").exists()
