"""Small derivatives from staged uploads or complete verified local cache data."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import time
import weakref
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from tgdrive.media.thumbnail_worker import (
    MAX_POSTER_BYTES,
    VIDEO_FORMATS,
    VIDEO_IMAGES_HEADER,
    VIDEO_IMAGES_MAGIC,
)

MAX_BYTES = 64 * 1024
DISK_BYTES = 128 * 1024**2
MAX_ENTRIES = 4096
_slots = weakref.WeakKeyDictionary()
_background_slots = weakref.WeakKeyDictionary()
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
SUPPORTED = {
    ".cbr",
    ".docx",
    ".pptx",
    ".xlsx",
    ".odt",
    ".odp",
    ".ods",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".opus",
    ".wav",
    ".aac",
    ".epub",
    ".cbz",
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
REPAIR_SOURCE_BYTES = 128 * 1024**2
REPAIR_SECONDS = 8
NATIVE_ADMISSION_SECONDS = 2


class ThumbnailRendererBusy(Exception):
    """No native renderer was admitted; the source has not failed to decode."""


async def _finish_owned(task):
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except BaseException:
            break
    if interrupted:
        # Opening a source can create an fd/path even after cancellation. Return
        # its result to the owner so it can clean up before propagating the stop.
        with suppress(BaseException):
            return task.result(), True
        return None, True
    return task.result(), False


async def _owned_task(task):
    result, interrupted = await _finish_owned(task)
    if interrupted:
        raise asyncio.CancelledError
    return result


async def _owned_disk_call(function, *args, **kwargs):
    return await _owned_task(
        asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    )


class CachedThumbnailRepair:
    """One bounded local repair; missing bytes never initiate Telegram reads."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._slot = asyncio.Lock()
        self._attempts = weakref.WeakKeyDictionary()

    async def start(self):
        with suppress(OSError):
            await _owned_disk_call(self._cleanup_stale_sources)

    def _cleanup_stale_sources(self):
        if self.directory.is_symlink():
            return
        try:
            import fcntl
        except ImportError:
            fcntl = None
        for path in self.directory.glob(".repair-*.source"):
            token = path.name.removeprefix(".repair-").removesuffix(".source")
            if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
                continue
            try:
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "r+b") as previous:
                    info = os.fstat(previous.fileno())
                    if not stat.S_ISREG(info.st_mode) or (
                        hasattr(os, "getuid") and info.st_uid != os.getuid()
                    ):
                        continue
                    if fcntl is not None:
                        # An active renderer holds this lock through native
                        # decoding. A crash releases it without running cleanup.
                        try:
                            fcntl.flock(previous, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                    elif info.st_mtime > time.time() - 60:
                        continue
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _open_source(self, size, reserve):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise OSError("Thumbnail directory must not be a symbolic link")
        self._cleanup_stale_sources()
        if shutil.disk_usage(self.directory).free < size + reserve:
            raise OSError("Insufficient temporary thumbnail space")
        path = self.directory / (".repair-" + secrets.token_hex(16) + ".source")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        output = os.fdopen(descriptor, "w+b")
        try:
            import fcntl

            fcntl.flock(output, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except BaseException:
            self._cleanup_source(output, path)
            raise
        return output, path

    @staticmethod
    def _cleanup_source(output, path):
        try:
            output.close()
        finally:
            path.unlink(missing_ok=True)

    async def get(self, cache, *, source, name, version, authorize):
        from tgdrive.media.media_reader import CachedSourceUnavailable
        from tgdrive.media.thumbnails import ThumbnailBusy

        if (
            version is None
            or Path(name).suffix.lower() not in SUPPORTED
            or not 0 < source["size"] <= REPAIR_SOURCE_BYTES
        ):
            return None
        attempts = self._attempts.setdefault(cache, OrderedDict())
        if attempts.get(version.file_id) == version.revision:
            return None
        # Optional work never forms another long queue behind the sole renderer.
        deadline = asyncio.get_running_loop().time() + REPAIR_SECONDS
        try:
            await asyncio.wait_for(self._slot.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise ThumbnailBusy from exc
        output = path = None
        rendering = False
        rendered = False

        def remember_failure():
            attempts[version.file_id] = version.revision
            attempts.move_to_end(version.file_id)
            while len(attempts) > MAX_ENTRIES:
                attempts.popitem(last=False)

        def native_started():
            nonlocal rendering
            rendering = True

        try:
            if attempts.get(version.file_id) == version.revision:
                return None
            async with asyncio.timeout_at(deadline):
                written = 0
                async for block in cache.cached_file_blocks(
                    **source,
                    version=version,
                    max_bytes=REPAIR_SOURCE_BYTES,
                    authorize=authorize,
                ):
                    if output is None:
                        opening = asyncio.create_task(
                            asyncio.to_thread(
                                self._open_source,
                                source["size"],
                                max(64 * 1024**2, cache.policy.min_free_bytes),
                            )
                        )
                        opened, interrupted = await _finish_owned(opening)
                        if opened is not None:
                            output, path = opened
                        if interrupted:
                            raise asyncio.CancelledError
                    count = await _owned_disk_call(output.write, block)
                    if count != len(block):
                        raise CachedSourceUnavailable
                    written += count
                if output is None or written != source["size"]:
                    raise CachedSourceUnavailable
                await _owned_disk_call(output.flush)
                if await _owned_disk_call(lambda: path.stat().st_size) != written:
                    raise CachedSourceUnavailable
                authorize()
                cover = await generate(
                    path, name, raise_busy=True, on_start=native_started
                )
                rendered = True
                authorize()
                if cache.cached_source_version(**source) != version:
                    raise CachedSourceUnavailable
                if cover:
                    await _owned_disk_call(
                        save,
                        self.directory,
                        source["channel_id"],
                        source["message_id"],
                        cover,
                    )
                    authorize()
                    if cache.cached_source_version(**source) != version:
                        raise CachedSourceUnavailable
                elif rendering:
                    remember_failure()
                else:
                    # A transient spawn failure never tested this source.
                    raise ThumbnailRendererBusy
                return cover
        except asyncio.CancelledError:
            # A disconnected request can retry; an unsuccessful completed
            # repair stays suppressed until this file receives new cached data.
            if attempts.get(version.file_id) == version.revision:
                attempts.pop(version.file_id, None)
            raise
        except CachedSourceUnavailable as exc:
            if cache.cached_source_version(**source) != version:
                raise ThumbnailBusy from exc
            return None
        except ThumbnailRendererBusy as exc:
            raise ThumbnailBusy from exc
        except TimeoutError as exc:
            if rendering and not rendered:
                remember_failure()
                return None
            raise ThumbnailBusy from exc
        except OSError as exc:
            raise ThumbnailBusy from exc
        except ValueError:
            return None
        except BaseException:
            # Authorization/catalog failures belong to this request, not to
            # the immutable source's ability to render for another viewer.
            if attempts.get(version.file_id) == version.revision:
                attempts.pop(version.file_id, None)
            raise
        finally:
            try:
                if output is not None:
                    with suppress(OSError):
                        await _owned_disk_call(self._cleanup_source, output, path)
            finally:
                self._slot.release()


async def generate(
    path: Path,
    name: str,
    *,
    raise_busy=False,
    on_start=None,
    background=False,
    remote_url=None,
    source_size=None,
) -> bytes | None:
    return await _generate(
        path,
        name,
        raise_busy=raise_busy,
        on_start=on_start,
        background=background,
        remote_url=remote_url,
        source_size=source_size,
    )


async def generate_video_images(
    path: Path,
    name: str,
    *,
    raise_busy=False,
    on_start=None,
    background=False,
    remote_url=None,
    source_size=None,
) -> tuple[bytes | None, bytes | None]:
    """Render a video's two sizes from one picture; other files keep one cover."""
    suffix = Path(name).suffix.lower()
    images = suffix in VIDEO_FORMATS and suffix != ".avif"
    content = await _generate(
        path,
        name,
        raise_busy=raise_busy,
        on_start=on_start,
        background=background,
        remote_url=remote_url,
        source_size=source_size,
        images=images,
    )
    if not images:
        return content, None
    if not content or len(content) < VIDEO_IMAGES_HEADER.size:
        return None, None
    magic, small_size, poster_size = VIDEO_IMAGES_HEADER.unpack_from(content)
    if (
        magic != VIDEO_IMAGES_MAGIC
        or small_size > MAX_BYTES
        or poster_size > MAX_POSTER_BYTES
        or len(content) != VIDEO_IMAGES_HEADER.size + small_size + poster_size
    ):
        return None, None
    small_end = VIDEO_IMAGES_HEADER.size + small_size
    small = content[VIDEO_IMAGES_HEADER.size : small_end]
    poster = content[small_end:]
    if any(
        image
        and not (image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9"))
        for image in (small, poster)
    ):
        return None, None
    return small or None, poster or None


async def _generate(
    path: Path,
    name: str,
    *,
    raise_busy=False,
    on_start=None,
    background=False,
    remote_url=None,
    source_size=None,
    images=False,
) -> bytes | None:
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        return None
    slots = _background_slots if background else _slots
    slot = slots.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))
    try:
        await asyncio.wait_for(slot.acquire(), timeout=NATIVE_ADMISSION_SECONDS)
    except TimeoutError as exc:
        if raise_busy:
            raise ThumbnailRendererBusy from exc
        return None
    try:
        process = None
        try:
            async with asyncio.timeout(120 if remote_url else 12):
                opening = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "tgdrive.media.thumbnail_worker",
                        remote_url or str(await asyncio.to_thread(path.resolve)),
                        suffix,
                        *(
                            ["remote", str(source_size)]
                            if remote_url
                            else ["background"]
                            if background
                            else []
                        ),
                        *(["images"] if images else []),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=os.name == "posix",
                        env={
                            "PATH": os.defpath,
                            "LANG": "C.UTF-8",
                            "PYTHONPATH": PROJECT_ROOT,
                        },
                    )
                )
                process, interrupted = await _finish_owned(opening)
                if interrupted:
                    raise asyncio.CancelledError
                if on_start is not None:
                    on_start()
                content = bytearray()
                maximum = (
                    VIDEO_IMAGES_HEADER.size + MAX_BYTES + MAX_POSTER_BYTES
                    if images
                    else MAX_BYTES
                )
                while block := await process.stdout.read(8192):
                    content.extend(block)
                    if len(content) > maximum:
                        return None
                data = bytes(content)
                await process.wait()
                if process.returncode == 0 and (
                    images or data.startswith(b"\xff\xd8\xff")
                ):
                    return data
        except OSError, TimeoutError:
            pass
        finally:
            if process is not None:
                with suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.returncode is None:
                        process.kill()
                await _owned_task(asyncio.create_task(process.wait()))
    finally:
        slot.release()
    return None


def location(directory: Path, channel: int, message: int) -> Path:
    identity = hashlib.sha256(f"{channel}:{message}".encode()).hexdigest()
    return directory / (identity + ".jpg")


def read(
    directory: Path, channel: int, message: int, *, maximum=MAX_BYTES
) -> bytes | None:
    path = location(directory, channel, message)
    try:
        if path.is_symlink() or path.stat().st_size > maximum:
            return None
        with path.open("rb") as source:
            data = source.read(maximum + 1)
        return (
            data
            if 0 < len(data) <= maximum and data.startswith(b"\xff\xd8\xff")
            else None
        )
    except OSError:
        return None


def save(
    directory: Path, channel: int, message: int, data: bytes, *, maximum=MAX_BYTES
) -> None:
    if not 0 < len(data) <= maximum or not data.startswith(b"\xff\xd8\xff"):
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
