"""Bounded torrent intake; Telegram remains the durable destination."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import os
import secrets
import shutil
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import aiohttp

from tgdrive.core.errors import InvalidNameError
from tgdrive.storage.store import _normalize_name
from tgdrive.transfers.remote import RemoteClient, address_is_forbidden
from tgdrive.transfers.uploads import ResumableUploads

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_FILES = 10_000
METADATA_TIMEOUT = 120
STALL_TIMEOUT = 300
_METADATA_DIRECTORY = ".tgdrive-torrent"
_worker = ResumableUploads._owned_worker


class TorrentError(ValueError):
    """The source cannot safely complete a torrent import."""


class TorrentCancelled(TorrentError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentFile:
    path: str
    size: int


@dataclass(frozen=True, slots=True)
class TorrentManifest:
    name: str
    files: tuple[TorrentFile, ...]
    total_size: int
    is_folder: bool
    info_hash: str = ""


def available() -> bool:
    return shutil.which("aria2c") is not None


def _decode(data: bytes):
    """Accept canonical bencode so re-encoding preserves the info hash."""
    if not data or len(data) > MAX_METADATA_BYTES:
        raise TorrentError("Torrent metadata exceeds the 8 MiB limit or is empty.")
    offset = nodes = 0

    def read(depth=0):
        nonlocal offset, nodes
        nodes += 1
        if depth > 128 or nodes > 100_000 or offset >= len(data):
            raise TorrentError("Torrent metadata is too complex or truncated.")
        marker = data[offset : offset + 1]
        offset += 1
        if marker == b"i":
            end = data.find(b"e", offset)
            raw = data[offset:end]
            if end < 0 or len(raw) > 20:
                raise TorrentError("Invalid torrent integer.")
            try:
                value = int(raw)
            except ValueError:
                raise TorrentError("Invalid torrent integer.") from None
            if str(value).encode() != raw:
                raise TorrentError("Non-canonical torrent integer.")
            offset = end + 1
            return value
        if marker in (b"l", b"d"):
            result = [] if marker == b"l" else {}
            previous = None
            while data[offset : offset + 1] != b"e":
                key = read(depth + 1)
                if marker == b"l":
                    result.append(key)
                else:
                    if not isinstance(key, bytes) or (
                        previous is not None and key <= previous
                    ):
                        raise TorrentError(
                            "Invalid or duplicate torrent dictionary key."
                        )
                    previous = key
                    result[key] = read(depth + 1)
            offset += 1
            return result
        offset -= 1
        end = data.find(b":", offset)
        raw = data[offset:end]
        if end < 0 or not raw.isdigit() or len(raw) > 8:
            raise TorrentError("Invalid torrent byte string.")
        size = int(raw)
        if str(size).encode() != raw or end + 1 + size > len(data):
            raise TorrentError("Invalid torrent byte string length.")
        offset = end + 1 + size
        return data[end + 1 : offset]

    value = read()
    if offset != len(data) or not isinstance(value, dict):
        raise TorrentError("Expected one torrent metadata dictionary.")
    return value


def _encode(value) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    return (
        b"d"
        + b"".join(_encode(key) + _encode(value[key]) for key in sorted(value))
        + b"e"
    )


def _name(value) -> str:
    try:
        if not isinstance(value, bytes):
            raise ValueError
        name = value.decode("utf-8")
        if _normalize_name(name) != name or name.endswith((".", " ")):
            raise ValueError
        return name
    except InvalidNameError, ValueError:
        raise TorrentError(
            "Torrent contains an unsafe or unsupported file name."
        ) from None


def _manifest(metadata: dict) -> TorrentManifest:
    info = metadata.get(b"info")
    if not isinstance(info, dict) or b"pieces" not in info:
        raise TorrentError(
            "Use a BitTorrent v1 torrent; v2-only torrents are unsupported."
        )
    if info.get(b"meta version") not in (None, 2):
        raise TorrentError("Unsupported torrent metadata version.")
    pieces, piece_length = info[b"pieces"], info.get(b"piece length")
    if (
        not isinstance(pieces, bytes)
        or not pieces
        or len(pieces) % 20
        or type(piece_length) is not int
        or piece_length <= 0
    ):
        raise TorrentError("Torrent piece checksums are invalid.")
    name = _name(info.get(b"name.utf-8", info.get(b"name")))
    if name.casefold() == _METADATA_DIRECTORY:
        raise TorrentError("Torrent name conflicts with TGDrive's staging metadata.")
    is_folder = b"files" in info
    entries = info.get(b"files") if is_folder else [info]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
        raise TorrentError("A torrent must contain between 1 and 10,000 files.")
    files, paths, folders = [], set(), {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TorrentError("Invalid torrent file entry.")
        attributes = entry.get(b"attr", b"")
        if (
            not isinstance(attributes, bytes)
            or b"l" in attributes
            or b"symlink path" in entry
        ):
            raise TorrentError("Torrent symlinks are unsupported.")
        size = entry.get(b"length")
        if type(size) is not int or size <= 0:
            raise TorrentError("TGDrive cannot import torrents containing empty files.")
        raw_parts = entry.get(b"path.utf-8", entry.get(b"path")) if is_folder else []
        if not isinstance(raw_parts, list) or (is_folder and not raw_parts):
            raise TorrentError("Invalid torrent file path.")
        parts = (name, *(_name(part) for part in raw_parts))
        if len(parts) > 65:
            raise TorrentError("Torrent folders cannot exceed 64 levels.")
        key = tuple(part.casefold() for part in parts)
        if key in paths:
            raise TorrentError("Torrent contains duplicate file names.")
        paths.add(key)
        for index in range(1, len(key)):
            folder_key, folder_name = key[:index], parts[:index]
            if folder_key in folders and folders[folder_key] != folder_name:
                raise TorrentError("Torrent has conflicting folder names.")
            folders[folder_key] = folder_name
        files.append(TorrentFile("/".join(parts), size))
    if paths.intersection(folders) or len(folders) > 1000:
        raise TorrentError("Torrent has conflicting paths or more than 1000 folders.")
    total = sum(item.size for item in files)
    if len(pieces) // 20 != (total + piece_length - 1) // piece_length:
        raise TorrentError("Torrent checksums do not match its declared size.")
    info_hash = hashlib.sha1(_encode(info), usedforsecurity=False).hexdigest()
    return TorrentManifest(name, tuple(files), total, is_folder, info_hash)


def _magnet(source: str) -> tuple[str, list[str]]:
    parsed = urlsplit(source)
    if parsed.scheme.lower() != "magnet" or parsed.netloc or parsed.path:
        raise TorrentError("Paste a magnet link or an HTTP(S) torrent file link.")
    try:
        query = parse_qs(parsed.query, max_num_fields=128)
    except ValueError:
        raise TorrentError("The magnet link contains too many parameters.") from None
    for value in query.get("xt", []):
        if not value.lower().startswith("urn:btih:"):
            continue
        raw = value[9:]
        try:
            digest = (
                bytes.fromhex(raw) if len(raw) == 40 else base64.b32decode(raw.upper())
            )
        except ValueError:
            continue
        if len(digest) == 20:
            return f"magnet:?xt=urn:btih:{digest.hex()}", query.get("tr", [])
    raise TorrentError("The magnet link must contain a valid BitTorrent v1 info hash.")


def validate_source(source: str) -> None:
    try:
        if not isinstance(source, str) or not source or len(source) > 16_384:
            raise ValueError
        if any(ord(character) < 32 for character in source):
            raise ValueError
        parsed = urlsplit(source)
        if parsed.scheme.lower() == "magnet":
            _magnet(source)
        elif (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port == 0
        ):
            raise ValueError
    except TorrentError:
        raise
    except ValueError:
        raise TorrentError(
            "Paste a valid magnet link or an HTTP(S) torrent file link."
        ) from None


async def _udp_trackers(values: list[str]) -> list[str]:
    result = []
    deadline = time.monotonic() + 10
    # shortcut: HTTP-only private trackers are unsupported; add a guarded tracker
    # transport before permitting aria2 to fetch arbitrary HTTP tracker URLs.
    for value in values[:32]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() != "udp":
                continue
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(ord(character) < 32 for character in value)
                or "," in value
            ):
                raise ValueError
            port = parsed.port or 80
            async with asyncio.timeout(remaining):
                answers = await asyncio.get_running_loop().getaddrinfo(
                    parsed.hostname, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
            if not answers or any(address_is_forbidden(item[4][0]) for item in answers):
                raise TorrentError("Torrent trackers must use public IP addresses.")
            address = str(ipaddress.ip_address(answers[0][4][0]))
            pinned = urlunsplit(
                ("udp", f"{address}:{port}", parsed.path, parsed.query, "")
            )
            if pinned not in result:
                result.append(pinned)
        except TorrentError:
            raise
        except ValueError, OSError, TimeoutError:
            continue
    return result


def _tracker_urls(metadata: dict) -> list[str]:
    raw = [metadata.get(b"announce")]
    tiers = metadata.get(b"announce-list", [])
    if isinstance(tiers, list):
        for tier in tiers[:32]:
            if isinstance(tier, list):
                raw.extend(tier[:32])
    return [
        value.decode("utf-8", errors="replace")
        for value in raw
        if isinstance(value, bytes)
    ]


class _Aria2:
    def __init__(self, directory: Path):
        self.directory = directory
        self.secret = secrets.token_urlsafe(32)
        self.process = None
        self.session = None

    async def __aenter__(self):
        executable = shutil.which("aria2c")
        if executable is None:
            raise TorrentError(
                "Torrent imports require aria2c. Install aria2 on the TGDrive host."
            )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self.process = await asyncio.create_subprocess_exec(
            executable,
            "--no-conf=true",
            "--no-netrc=true",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-allow-origin-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={self.secret}",
            "--rpc-max-request-size=16M",
            "--rpc-save-upload-metadata=false",
            f"--dir={self.directory}",
            "--file-allocation=none",
            "--auto-file-renaming=false",
            "--allow-overwrite=false",
            "--check-integrity=true",
            "--seed-time=0",
            "--bt-enable-lpd=false",
            "--enable-dht=true",
            "--enable-dht6=false",
            "--disable-ipv6=true",
            "--dht-entry-point=router.bittorrent.com:6881",
            f"--dht-file-path={self.directory / 'dht.dat'}",
            "--listen-port=49152-65535",
            "--dht-listen-port=49152-65535",
            "--bt-exclude-tracker=*",
            f"--bt-stop-timeout={STALL_TIMEOUT}",
            "--max-concurrent-downloads=1",
            "--follow-torrent=false",
            "--follow-metalink=false",
            "--quiet=true",
            "--download-result=hide",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5), trust_env=False
        )
        try:
            async with asyncio.timeout(10):
                while True:
                    if self.process.returncode is not None:
                        raise TorrentError("The torrent downloader could not start.")
                    try:
                        await self.call("getVersion")
                        return self
                    except aiohttp.ClientConnectionError:
                        await asyncio.sleep(0.1)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_args):
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                async with asyncio.timeout(5):
                    await self.process.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        if self.session is not None:
            await self.session.close()

    async def call(self, method, *params):
        async with self.session.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": f"aria2.{method}",
                "params": [f"token:{self.secret}", *params],
            },
        ) as response:
            response.raise_for_status()
            result = await response.json()
        if "error" in result:
            raise TorrentError("The torrent downloader rejected this operation.")
        return result["result"]

    async def wait(self, gid, *, progress=None):
        current = -1
        changed = time.monotonic()
        while True:
            state = await self.call("tellStatus", gid)
            received = int(state.get("completedLength", 0))
            if received != current:
                current = received
                changed = time.monotonic()
            if progress is not None:
                await progress(received, int(state.get("totalLength", 0)) or None)
            if state["status"] == "complete":
                return
            if state["status"] in {"error", "removed"}:
                raise TorrentError(
                    "Torrent download failed. Check available peers and retry."
                )
            if time.monotonic() - changed > STALL_TIMEOUT:
                raise TorrentError(
                    "Torrent made no progress for 5 minutes. Check peers and retry."
                )
            await asyncio.sleep(1)


async def download_torrent(
    source: str,
    output_dir: Path,
    *,
    remote: RemoteClient,
    cancel_event: asyncio.Event,
    progress: Callable[[int, int | None], Awaitable[None]],
    admit: Callable[[TorrentManifest], Awaitable[None]],
) -> TorrentManifest:
    """Validate metadata and reserve staging before any payload is downloaded."""
    if cancel_event.is_set():
        raise TorrentCancelled("Torrent import cancelled.")
    worker = asyncio.create_task(_download(source, output_dir, remote, progress, admit))
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        await asyncio.wait((worker, cancelled), return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            raise TorrentCancelled("Torrent import cancelled.")
        return await worker
    finally:
        worker.cancel()
        cancelled.cancel()
        await asyncio.gather(worker, cancelled, return_exceptions=True)


async def _download(source, output_dir, remote, progress, admit):
    validate_source(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cached = output_dir / _METADATA_DIRECTORY / "source.torrent"
    if cached.is_symlink() or cached.parent.is_symlink():
        raise TorrentError("Torrent staging metadata contains an unsafe symbolic link.")
    with tempfile.TemporaryDirectory(
        prefix=".torrent-", dir=output_dir.parent
    ) as temporary:
        metadata_dir = Path(temporary)
        async with _Aria2(metadata_dir) as engine:
            if cached.exists():
                metadata_path, trackers = cached, []
            elif source.lower().startswith("magnet:"):
                magnet, raw_trackers = _magnet(source)
                trackers = await _udp_trackers(raw_trackers)
                gid = await engine.call(
                    "addUri",
                    [magnet],
                    {
                        "bt-metadata-only": "true",
                        "bt-save-metadata": "true",
                        "bt-tracker": ",".join(trackers),
                    },
                )
                try:
                    async with asyncio.timeout(METADATA_TIMEOUT):
                        await engine.wait(gid)
                except TimeoutError:
                    raise TorrentError(
                        "No torrent metadata arrived within 2 minutes. "
                        "Check peers and retry."
                    ) from None
                sources = await _worker(lambda: list(metadata_dir.glob("*.torrent")))
                if len(sources) != 1:
                    raise TorrentError(
                        "The magnet link did not provide torrent metadata."
                    )
                metadata_path = sources[0]
                await engine.call("removeDownloadResult", gid)
            else:
                result = await remote.download(
                    source,
                    metadata_dir,
                    filename="source.torrent",
                    max_bytes=MAX_METADATA_BYTES,
                    single_threaded=True,
                )
                metadata_path = result.path
                trackers = []
            if metadata_path.stat().st_size > MAX_METADATA_BYTES:
                raise TorrentError("Torrent metadata exceeds the 8 MiB limit.")
            metadata = _decode(metadata_path.read_bytes())
            manifest = _manifest(metadata)
            trackers = list(
                dict.fromkeys(
                    [
                        *trackers,
                        *await _udp_trackers(_tracker_urls(metadata)),
                    ]
                )
            )[:32]
            if metadata[b"info"].get(b"private") and not trackers:
                raise TorrentError(
                    "Private torrents require a public UDP tracker; "
                    "HTTP-only trackers are unsupported."
                )
            await admit(manifest)
            # Keep only info: web seeds, DHT bootstrap nodes and tracker redirects
            # must never bypass the guarded HTTP source downloader.
            sanitized = _encode({b"info": metadata[b"info"]})
            cached_metadata = {b"info": metadata[b"info"]}
            if trackers:
                cached_metadata[b"announce-list"] = [
                    [value.encode()] for value in trackers
                ]

            def save_metadata():
                cached.parent.mkdir(exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=cached.parent, delete=False
                ) as saved:
                    saved_path = Path(saved.name)
                    try:
                        saved.write(_encode(cached_metadata))
                        saved.flush()
                        os.fsync(saved.fileno())
                    except BaseException:
                        saved_path.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(saved_path, cached)
                finally:
                    saved_path.unlink(missing_ok=True)

            await _worker(save_metadata)
            gid = await engine.call(
                "addTorrent",
                base64.b64encode(sanitized).decode(),
                [],
                {
                    "dir": str(output_dir.resolve()),
                    "pause": "true",
                    "bt-metadata-only": "false",
                    "bt-save-metadata": "false",
                    "bt-tracker": ",".join(trackers),
                },
            )
            actual = await engine.call("getFiles", gid)
            expected = {
                str((output_dir / item.path).resolve()): item.size
                for item in manifest.files
            }
            if {item["path"]: int(item["length"]) for item in actual} != expected:
                raise TorrentError(
                    "The downloader changed torrent paths; import stopped safely."
                )
            for item in manifest.files:
                path = output_dir / item.path
                if any(part.is_symlink() for part in (path, *path.parents)):
                    raise TorrentError(
                        "Torrent staging contains an unsafe symbolic link."
                    )
            await engine.call("unpause", gid)

            async def received(current, total):
                if shutil.disk_usage(output_dir).free < remote.min_free_bytes:
                    raise TorrentError(
                        "Temporary storage is full. Free disk space and retry."
                    )
                await progress(current, total)

            await engine.wait(gid, progress=received)
            for item in manifest.files:
                path = output_dir / item.path
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size != item.size
                ):
                    raise TorrentError(
                        "Torrent payload is incomplete. Retry the import."
                    )
            return manifest
