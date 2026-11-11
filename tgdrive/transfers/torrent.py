"""Bounded torrent intake; Telegram remains the durable destination."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import aiohttp

from tgdrive.core.errors import InvalidNameError
from tgdrive.storage.capacity import measure_capacity, require_headroom
from tgdrive.storage.store import _normalize_name
from tgdrive.transfers.remote import RemoteClient, address_is_forbidden
from tgdrive.transfers.tasks import _journalled
from tgdrive.transfers.uploads import ResumableUploads

# Serialized input and decoded allocation have separate bounds.
MAX_METADATA_BYTES = 256 * 1024 * 1024
MAX_RPC_BYTES = 16 * 1024 * 1024
MAX_SANITIZED_BYTES = 12 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TOKENS = 100_000
MAX_CONTAINERS = 10_000
MAX_CONTAINER_ITEMS = 10_000
MAX_VALUE_BYTES = 16 * 1024 * 1024
MAX_FILES = 10_000
MAX_PATH_COMPONENTS = 64_000
METADATA_TIMEOUT = 120
STALL_TIMEOUT = 300
TORRENT_MAX_ATTEMPTS = 4
_METADATA_DIRECTORY = ".tgdrive-torrent"
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
_worker = ResumableUploads._owned_worker


class TorrentError(ValueError):
    """The source cannot safely complete a torrent import."""


class TorrentCancelled(TorrentError):
    pass


class TorrentTransportError(TorrentError):
    """A known transport interruption may retry without changing the source."""


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


def _decode(data):
    """Accept canonical bencode so re-encoding preserves the info hash."""
    if len(data) > MAX_METADATA_BYTES:
        raise TorrentError("Torrent metadata exceeds the 256 MiB input limit.")
    if not data:
        raise TorrentError("Torrent metadata is empty.")
    offset = tokens = containers = value_bytes = 0

    def read(depth=0):
        nonlocal offset, tokens, containers, value_bytes
        tokens += 1
        if tokens > MAX_TOKENS:
            raise TorrentError("Torrent metadata exceeds its decoded token limit.")
        # Folder depth is a flat path list in bencode, so nesting stays shallow;
        # this only stops runaway recursion on malformed input.
        if depth > 128 or offset >= len(data):
            raise TorrentError("Torrent metadata is malformed or truncated.")
        marker = data[offset : offset + 1]
        offset += 1
        if marker == b"i":
            end = data.find(b"e", offset, offset + 21)
            if end < 0:
                raise TorrentError("Invalid torrent integer.")
            raw = data[offset:end]
            if len(raw) > 20:
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
            containers += 1
            if containers > MAX_CONTAINERS:
                raise TorrentError(
                    "Torrent metadata exceeds its decoded container limit."
                )
            sequence = [] if marker == b"l" else None
            mapping = {} if marker == b"d" else None
            previous = None
            count = 0
            while data[offset : offset + 1] != b"e":
                count += 1
                if count > MAX_CONTAINER_ITEMS:
                    raise TorrentError(
                        "Torrent metadata has too many container entries."
                    )
                key = read(depth + 1)
                if marker == b"l":
                    assert sequence is not None
                    sequence.append(key)
                else:
                    if not isinstance(key, bytes) or (
                        previous is not None and key <= previous
                    ):
                        raise TorrentError(
                            "Invalid or duplicate torrent dictionary key."
                        )
                    previous = key
                    assert mapping is not None
                    mapping[key] = read(depth + 1)
            offset += 1
            return sequence if marker == b"l" else mapping
        offset -= 1
        end = data.find(b":", offset, offset + 9)
        if end < 0:
            raise TorrentError("Invalid torrent byte string.")
        raw = data[offset:end]
        if not raw.isdigit() or len(raw) > 8:
            raise TorrentError("Invalid torrent byte string.")
        size = int(raw)
        if str(size).encode() != raw or end + 1 + size > len(data):
            raise TorrentError("Invalid torrent byte string length.")
        value_bytes += size
        if size > MAX_SANITIZED_BYTES or value_bytes > MAX_VALUE_BYTES:
            raise TorrentError("Torrent metadata exceeds its decoded byte limit.")
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
    if not isinstance(entries, list) or not entries:
        raise TorrentError("A torrent must contain at least one file.")
    if len(entries) > MAX_FILES:
        raise TorrentError("Torrent contains too many files.")
    files, paths, folders = [], set(), {}
    components = path_bytes = 0
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
        components += len(raw_parts) + 1
        if len(raw_parts) >= 64 or components > MAX_PATH_COMPONENTS:
            raise TorrentError("Torrent paths exceed their component limit.")
        parts = (name, *(_name(part) for part in raw_parts))
        path = "/".join(parts)
        encoded_length = len(path.encode())
        path_bytes += encoded_length
        if encoded_length > 4096 or path_bytes > MAX_MANIFEST_BYTES:
            raise TorrentError("Torrent paths exceed their byte limit.")
        key = tuple(part.casefold() for part in parts)
        if key in paths:
            raise TorrentError("Torrent contains duplicate file names.")
        paths.add(key)
        for index in range(1, len(key)):
            folder_key, folder_name = key[:index], parts[:index]
            if folder_key in folders and folders[folder_key] != folder_name:
                raise TorrentError("Torrent has conflicting folder names.")
            folders[folder_key] = folder_name
        files.append(TorrentFile(path, size))
    if paths.intersection(folders):
        raise TorrentError("Torrent has conflicting file and folder paths.")
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
            if not answers or any(
                address_is_forbidden(str(item[4][0])) for item in answers
            ):
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
    values = [metadata.get(b"announce")]
    tiers = metadata.get(b"announce-list", [])
    if isinstance(tiers, list):
        for tier in tiers[:32]:
            if isinstance(tier, list):
                values.extend(tier[:32])
    return [
        item.decode("utf-8", "replace")
        for item in values
        if isinstance(item, bytes) and len(item) <= 2048
    ][:32]


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
            # Retry keeps the staged pieces: verify them and fetch only the rest.
            "--continue=true",
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
                    except TorrentTransportError:
                        await asyncio.sleep(0.1)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    @_journalled
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
        if self.session is None:
            raise TorrentError("The torrent service is not running.")
        payload = await _worker(
            lambda: json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": f"aria2.{method}",
                    "params": [f"token:{self.secret}", *params],
                },
                separators=(",", ":"),
            ).encode()
        )
        if len(payload) > MAX_RPC_BYTES:
            raise TorrentError("Torrent metadata exceeds the 16 MiB RPC request limit.")
        try:
            async with self.session.post(
                self.url, data=payload, headers={"Content-Type": "application/json"}
            ) as response:
                response.raise_for_status()
                result = await response.json()
        except aiohttp.ClientConnectionError, TimeoutError:
            raise TorrentTransportError(
                "The torrent downloader connection was interrupted."
            ) from None
        if "error" in result:
            raise TorrentError("The torrent downloader rejected this operation.")
        return result["result"]

    async def wait(self, gid, *, progress=None):
        current = -1
        changed = time.monotonic()
        while True:
            state = await self.call("tellStatus", gid)
            # On retry aria2c first re-hashes the pieces already on disk; it
            # reports them as verified, not completed, so progress would drop
            # to zero and look like a fresh download.
            received = max(
                int(state.get("completedLength", 0)),
                int(state.get("verifiedLength", 0) or 0),
            )
            if received != current:
                current = received
                changed = time.monotonic()
            if progress is not None:
                await progress(received, int(state.get("totalLength", 0)) or None)
            if state["status"] == "complete":
                return
            if state["status"] == "error" and state.get("errorCode") in {"2", "6"}:
                # aria2 errorCode uses its documented timeout/network exit codes.
                raise TorrentTransportError("Torrent transport was interrupted.")
            if state["status"] in {"error", "removed"}:
                raise TorrentError(
                    "Torrent download failed. Check available peers and retry."
                )
            if time.monotonic() - changed > STALL_TIMEOUT:
                raise TorrentError(
                    "Torrent made no progress for 5 minutes. Check peers and retry."
                )
            await asyncio.sleep(1)


@_journalled
async def _stop_parser(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    if process.stdout is not None:
        while await process.stdout.read(64 * 1024):
            pass
    await process.wait()


def _parsed_metadata(line, sanitized):
    try:
        plan = json.loads(line)
        if "error" in plan:
            raise TorrentError(plan["error"])
        files = plan["files"]
        if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
            raise ValueError
        manifest = TorrentManifest(
            plan["name"],
            tuple(TorrentFile(item["path"], item["size"]) for item in files),
            plan["total_size"],
            plan["is_folder"],
            plan["info_hash"],
        )
        with sanitized.open("rb") as source:
            details = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or not 0 < details.st_size <= MAX_SANITIZED_BYTES
            ):
                raise ValueError
            data = source.read(MAX_SANITIZED_BYTES + 1)
        if len(data) != details.st_size:
            raise ValueError
        return manifest, data, plan["trackers"], plan["private"]
    except (KeyError, TypeError, ValueError, OSError, RecursionError) as error:
        if isinstance(error, TorrentError):
            raise
        raise TorrentError(
            "The torrent processor returned invalid metadata."
        ) from error


async def _parse_metadata(source, sanitized):
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PYTHONPATH": _PACKAGE_ROOT,
    }
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tgdrive.transfers.torrent_worker",
            str(source),
            str(sanitized),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            limit=MAX_MANIFEST_BYTES + 1024,
        )
    )
    interrupted = False
    while True:
        try:
            process = await asyncio.shield(launch)
            break
        except asyncio.CancelledError:
            if launch.cancelled():
                raise
            interrupted = True
    try:
        if interrupted:
            raise asyncio.CancelledError
        assert process.stdout is not None
        async with asyncio.timeout(30):
            try:
                line = await process.stdout.readline()
            except ValueError:
                raise TorrentError(
                    "Torrent processor exceeded its response limit."
                ) from None
            if (
                not line
                or len(line) > MAX_MANIFEST_BYTES
                or await process.stdout.read(1)
            ):
                raise TorrentError("Torrent processor returned an invalid response.")
            await process.wait()
            if process.returncode:
                raise TorrentError("Torrent metadata could not be processed safely.")
            return await _worker(_parsed_metadata, line, sanitized)
    except TimeoutError:
        raise TorrentError("Torrent metadata processing timed out.") from None
    finally:
        await _stop_parser(process)


@_journalled
async def _stop_download(worker, cancelled):
    worker.cancel()
    cancelled.cancel()
    await asyncio.gather(worker, cancelled, return_exceptions=True)


async def download_torrent(
    source: str,
    output_dir: Path,
    *,
    remote: RemoteClient,
    cancel_event: asyncio.Event,
    progress: Callable[[int, int | None], Awaitable[None]],
    admit: Callable[[TorrentManifest], Awaitable[list[int] | None]],
    check_capacity: Callable[[int], Awaitable[None]] | None = None,
) -> TorrentManifest:
    """Validate metadata and reserve staging before any payload is downloaded.

    ``admit`` returns the 0-based file indexes to download, or None for all.
    ``check_capacity`` checks extra writes against live staging reservations.
    """
    if cancel_event.is_set():
        raise TorrentCancelled("Torrent import cancelled.")
    worker = asyncio.create_task(
        _download(source, output_dir, remote, progress, admit, check_capacity)
    )
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        await asyncio.wait((worker, cancelled), return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            raise TorrentCancelled("Torrent import cancelled.")
        return await worker
    finally:
        await _stop_download(worker, cancelled)


async def _download(source, output_dir, remote, progress, admit, check_capacity):
    validate_source(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cached = output_dir / _METADATA_DIRECTORY / "source.torrent"
    if cached.is_symlink() or cached.parent.is_symlink():
        raise TorrentError("Torrent staging metadata contains an unsafe symbolic link.")

    async def capacity(required_bytes):
        if check_capacity is not None:
            await check_capacity(required_bytes)
            return
        try:
            await _worker(
                lambda: require_headroom(
                    measure_capacity(output_dir),
                    required_bytes,
                    reserve_bytes=remote.min_free_bytes,
                )
            )
        except OSError:
            raise TorrentError(
                "Temporary storage is full or unavailable. "
                "Check free disk space and retry."
            ) from None

    if not cached.exists():
        await capacity(MAX_METADATA_BYTES)
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
                    check_capacity=capacity,
                )
                metadata_path = result.path
                trackers = []
            await capacity(MAX_SANITIZED_BYTES)
            manifest, sanitized, raw_trackers, private = await _parse_metadata(
                metadata_path, metadata_dir / "sanitized.torrent"
            )
            trackers = list(
                dict.fromkeys([*trackers, *await _udp_trackers(raw_trackers)])
            )[:32]
            if private and not trackers:
                raise TorrentError(
                    "Private torrents require a public UDP tracker; "
                    "HTTP-only trackers are unsupported."
                )
            # The child returns info-only bytes; never reconstruct its raw graph.
            cached_metadata = sanitized
            if trackers:
                cached_metadata = (
                    b"d13:announce-list"
                    + _encode([[value.encode()] for value in trackers])
                    + sanitized[1:]
                )

            def save_metadata():
                cached.parent.mkdir(exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=cached.parent, delete=False
                ) as saved:
                    saved_path = Path(saved.name)
                    try:
                        saved.write(cached_metadata)
                        saved.flush()
                        os.fsync(saved.fileno())
                    except BaseException:
                        saved_path.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(saved_path, cached)
                finally:
                    saved_path.unlink(missing_ok=True)

            # Saved before admission so a pause for file selection never has to
            # ask peers for the metadata again.
            await capacity(len(cached_metadata))
            await _worker(save_metadata)
            selected = await admit(manifest)
            wanted = [
                item
                for index, item in enumerate(manifest.files)
                if selected is None or index in selected
            ]
            options = {}
            if selected is not None:
                options["select-file"] = ",".join(str(i + 1) for i in sorted(selected))
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
                    **options,
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
                await progress(current, total)
                await capacity(0)

            await engine.wait(gid, progress=received)
            for item in wanted:
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
