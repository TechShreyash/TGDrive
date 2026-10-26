"""Container defaults that survive restarts without exposing deployment secrets."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
from pathlib import Path

SECRET_FIELDS = ("SESSION_SECRET", "MONGODB_BACKUP_KEY")


def installation_secrets(
    directory: Path, known: dict[str, str], *, existing_drive: bool
) -> dict[str, str]:
    """Reuse saved keys; generate missing keys only for an uninitialized drive."""
    if all(known.get(key) for key in SECRET_FIELDS):
        return known
    if directory in (Path(directory.anchor), Path.home()):
        raise ValueError("Use a dedicated DATA_DIR for generated deployment secrets")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(
        directory / ".deployment-secrets.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "deployment-secrets.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            saved = {}
        else:
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size > 4096
                    or info.st_mode & 0o077
                ):
                    raise ValueError
                with os.fdopen(descriptor, "r", closefd=False) as source:
                    saved = json.load(source)
                if not isinstance(saved, dict) or set(saved) != set(SECRET_FIELDS):
                    raise ValueError
                if (
                    not isinstance(saved["SESSION_SECRET"], str)
                    or len(saved["SESSION_SECRET"]) < 32
                    or not isinstance(saved["MONGODB_BACKUP_KEY"], str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", saved["MONGODB_BACKUP_KEY"])
                ):
                    raise ValueError
            except ValueError, TypeError:
                raise ValueError(
                    "Preserve deployment-secrets.json and repair its contents or "
                    "0600 permissions; saved keys were not replaced"
                ) from None
            finally:
                os.close(descriptor)
        values = {**saved, **known}
        missing = [key for key in SECRET_FIELDS if not values.get(key)]
        if not missing:
            return values
        if existing_drive:
            raise ValueError(
                "Existing drive is missing deployment keys. Supply the original "
                + ", ".join(missing)
                + "; existing keys were not regenerated"
            )
        for key in missing:
            values[key] = (
                secrets.token_hex(32)
                if key == "MONGODB_BACKUP_KEY"
                else secrets.token_urlsafe(48)
            )
        temporary = directory / (".deployment-secrets-" + secrets.token_hex(16))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as target:
                json.dump(values, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            temporary.unlink(missing_ok=True)
        return values
    finally:
        os.close(lock)
