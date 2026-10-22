"""TGDrive-owned small derivatives generated from an already staged upload."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import sys
import tempfile
import weakref
from contextlib import suppress
from pathlib import Path

MAX_BYTES = 64 * 1024
DISK_BYTES = 128 * 1024**2
MAX_ENTRIES = 4096
_slots = weakref.WeakKeyDictionary()
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
SUPPORTED = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".mp4",
    ".m4v",
    ".mov",
    ".avif",
    ".mkv",
    ".webm",
    ".avi",
    ".wmv",
    ".asf",
    ".mpeg",
    ".mpg",
    ".pdf",
}


async def generate(path: Path, name: str) -> bytes | None:
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        return None
    slot = _slots.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))
    try:
        await asyncio.wait_for(slot.acquire(), timeout=2)
    except TimeoutError:
        return None
    try:
        process = None
        try:
            async with asyncio.timeout(12):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tgdrive.thumbnail_worker",
                    str(await asyncio.to_thread(path.resolve)),
                    suffix,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                    env={
                        "PATH": os.defpath,
                        "LANG": "C.UTF-8",
                        "PYTHONPATH": PROJECT_ROOT,
                    },
                )
                content = bytearray()
                while block := await process.stdout.read(8192):
                    content.extend(block)
                    if len(content) > MAX_BYTES:
                        return None
                data = bytes(content)
                await process.wait()
                if process.returncode == 0 and data.startswith(b"\xff\xd8\xff"):
                    return data
        except (OSError, TimeoutError):
            pass
        finally:
            if process is not None:
                with suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.returncode is None:
                        process.kill()
                await asyncio.shield(process.wait())
    finally:
        slot.release()
    return None


def location(directory: Path, channel: int, message: int) -> Path:
    identity = hashlib.sha256(f"{channel}:{message}".encode()).hexdigest()
    return directory / (identity + ".jpg")


def read(directory: Path, channel: int, message: int) -> bytes | None:
    path = location(directory, channel, message)
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return None
        with path.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
        return (
            data
            if 0 < len(data) <= MAX_BYTES and data.startswith(b"\xff\xd8\xff")
            else None
        )
    except OSError:
        return None


def save(directory: Path, channel: int, message: int, data: bytes) -> None:
    if not 0 < len(data) <= MAX_BYTES or not data.startswith(b"\xff\xd8\xff"):
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(directory).free < 64 * 1024**2:
        return
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".thumbnail-")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary, location(directory, channel, message))
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    # Disposable cache: bounded independently of original media budgets.
    entries = []
    for path in directory.glob("*.jpg"):
        try:
            if len(path.stem) == 64 and not path.is_symlink():
                entries.append((path.stat().st_mtime, path, path.stat().st_size))
        except FileNotFoundError:
            continue
    total = sum(row[2] for row in entries)
    for index, (_, path, size) in enumerate(sorted(entries)):
        if total <= DISK_BYTES and len(entries) - index <= MAX_ENTRIES:
            break
        with suppress(FileNotFoundError):
            path.unlink()
        total -= size
