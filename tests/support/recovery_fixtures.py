"""Synthetic deployment settings shared by supported channel recovery tests."""

from pathlib import Path

from tgdrive.core.config import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        MONGODB_URI="mongodb://synthetic.invalid",
        API_ID=12345,
        API_HASH="a" * 32,
        BOT_TOKENS="123456:" + "x" * 30,
        STORAGE_CHANNEL=-1001234567890,
        ADMIN_PASSWORD="correct horse battery staple",
        SESSION_SECRET="s" * 48,
        DATA_DIR=tmp_path / "data",
        CACHE_DIR=tmp_path / "cache",
        COOKIE_SECURE=False,
    )
