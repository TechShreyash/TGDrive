"""Bounded, SSRF-resistant remote file inspection and downloading."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

if TYPE_CHECKING:
    from tgdrive.transfers.remote_ranges import RangePolicy

DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 1024 * 1024
REMOTE_MAX_ATTEMPTS = 4
logger = logging.getLogger(__name__)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+\d+-\d+/(\d+|\*)$", re.IGNORECASE)
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class RemoteError(RuntimeError):
    """Base class for safe remote-transfer failures."""


class UnsafeRemoteURL(RemoteError):
    """A URL or resolved address violates the outbound network policy."""


class RemoteResolutionError(RemoteError):
    """DNS resolution failed or returned unusable answers."""


class TooManyRedirects(RemoteError):
    """The configured manual redirect limit was exceeded."""


class RemoteHTTPError(RemoteError):
    def __init__(
        self, status: int, url: str, *, retry_after: str | None = None
    ) -> None:
        self.status = status
        self.url = url
        self.retry_after = retry_after
        super().__init__(f"remote server returned HTTP {status}")


class RemoteProtocolError(RemoteError):
    """The response cannot safely represent the requested file."""


class RemoteIncompleteResponse(RemoteProtocolError):
    """A known-length response ended before all promised bytes arrived."""


class RemoteRetriesExhausted(RemoteError):
    """A bounded source-retrieval retry cycle has failed."""


class RemoteTooLarge(RemoteError):
    def __init__(self, limit: int, observed: int | None = None) -> None:
        self.limit = limit
        self.observed = observed
        detail = f" ({observed} bytes observed)" if observed is not None else ""
        super().__init__(f"remote file exceeds the {limit}-byte limit{detail}")


class RemoteCancelled(RemoteError):
    """The supplied cancellation event was set."""


class RemoteTimeout(RemoteError):
    """The connect/read/total timeout budget expired."""


class RemoteDestinationExists(RemoteError):
    """The safe destination name already exists and overwrite is disabled."""


type ProgressCallback = Callable[[int, int | None], Awaitable[None] | None]
type RetryCallback = Callable[[int, float, Exception], Awaitable[None] | None]


def source_retryable(error: Exception) -> bool:
    # Invalid URLs, unsafe redirects, certificate failures, size/storage errors
    # and permanent HTTP responses must not become automatic request loops.
    if isinstance(error, aiohttp.ClientSSLError):
        return False
    if isinstance(error, RemoteHTTPError):
        return error.status in {408, 429, 500, 502, 503, 504}
    return isinstance(
        error,
        (
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            ConnectionError,
            TimeoutError,
            RemoteResolutionError,
            RemoteTimeout,
            RemoteIncompleteResponse,
        ),
    )


class HTTPSessionLike(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Awaitable[Any]: ...

    def close(self) -> Awaitable[None] | None: ...


type SessionFactory = Callable[
    [AbstractResolver, aiohttp.ClientTimeout],
    HTTPSessionLike | Awaitable[HTTPSessionLike],
]


@dataclass(frozen=True, slots=True)
class RemoteTimeouts:
    connect: float = 10.0
    read: float = 30.0
    total: float = 60.0 * 60.0

    def __post_init__(self) -> None:
        if self.connect <= 0 or self.read <= 0 or self.total <= 0:
            raise ValueError("remote timeouts must be positive")

    def to_aiohttp(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=self.total,
            sock_connect=self.connect,
            sock_read=self.read,
        )


@dataclass(frozen=True, slots=True)
class RemoteFileInfo:
    url: str
    filename: str
    size: int | None
    content_type: str | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    url: str
    filename: str
    size: int
    content_type: str | None


def address_is_forbidden(address: str) -> bool:
    """Return whether an IP belongs to a non-public address category."""

    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as error:
        raise RemoteResolutionError("DNS returned a non-IP address") from error

    addresses = [parsed]
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        addresses.append(parsed.ipv4_mapped)

    return any(
        # Shared address space (100.64.0.0/10) is neither private nor reserved
        # in ipaddress, but can reach internal services and cloud metadata.
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
        for ip in addresses
    )


class ValidatingResolver(AbstractResolver):
    """Validate every DNS answer before aiohttp may connect to it.

    Installing this resolver in the connector is important: a separate DNS
    preflight alone would remain vulnerable to DNS rebinding between the check
    and the connection.
    """

    def __init__(
        self,
        resolver: AbstractResolver,
        *,
        allow_private: bool = False,
    ) -> None:
        self._resolver = resolver
        self.allow_private = allow_private
        self._closed = False

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        try:
            answers = await self._resolver.resolve(host, port, family)
        except UnsafeRemoteURL:
            raise
        except Exception as error:
            raise RemoteResolutionError(f"could not resolve host {host!r}") from error

        if not answers:
            raise RemoteResolutionError(f"host {host!r} returned no DNS answers")
        if not self.allow_private:
            for answer in answers:
                address = answer.get("host")
                if not isinstance(address, str) or address_is_forbidden(address):
                    raise UnsafeRemoteURL(
                        f"host {host!r} resolves to a non-public address"
                    )
        return answers

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        result = self._resolver.close()
        if inspect.isawaitable(result):
            await result


def safe_filename(
    content_disposition: str | None,
    url: str,
    content_type: str | None = None,
    *,
    fallback: str = "download.bin",
    max_length: int = 200,
) -> str:
    """Derive a cross-platform basename without trusting remote path data."""

    candidate: str | None = None
    if content_disposition:
        message = Message()
        message["content-disposition"] = content_disposition
        candidate = message.get_filename()

    if not candidate:
        candidate = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    candidate = unquote(candidate or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = _UNSAFE_FILENAME_CHARS.sub("_", candidate).strip().rstrip(". ")

    if candidate in ("", ".", ".."):
        extension = mimetypes.guess_extension((content_type or "").split(";", 1)[0])
        candidate = f"download{extension}" if extension else fallback

    stem = candidate.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        candidate = f"_{candidate}"

    if max_length <= 0:
        raise ValueError("max_length must be positive")
    candidate = candidate[:max_length].rstrip(". ")
    return candidate or fallback


class RemoteClient:
    """Inspect and adaptively retrieve remote files with strict network limits."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = 5,
        min_free_bytes: int = 0,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeouts: RemoteTimeouts | None = None,
        allow_private: bool = False,
        resolver: AbstractResolver | None = None,
        session: HTTPSessionLike | None = None,
        session_factory: SessionFactory | None = None,
        range_policy: RangePolicy | None = None,
        download_concurrency: int = 2,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if session is not None and session_factory is not None:
            raise ValueError("provide either session or session_factory, not both")

        self._resolver = (
            None
            if resolver is None
            else (
                resolver
                if isinstance(resolver, ValidatingResolver)
                else ValidatingResolver(resolver, allow_private=allow_private)
            )
        )
        self.allow_private = allow_private
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.max_redirects = max_redirects
        self.chunk_size = chunk_size
        self.timeouts = timeouts or RemoteTimeouts()
        self._session = session
        self._session_factory = session_factory
        self._owns_session = session is None
        self._owns_resolver = resolver is None
        self._closed = False
        self._session_lock = asyncio.Lock()
        from tgdrive.transfers.remote_ranges import RangePolicy

        self.range_policy = range_policy or RangePolicy()
        self._range_slots = asyncio.Semaphore(16)
        from tgdrive.transfers.remote_scheduler import URLDownloadScheduler

        self.scheduler = URLDownloadScheduler(download_concurrency)
        self._download_ticket: ContextVar[Any] = ContextVar(
            "url_download", default=None
        )

    async def __aenter__(self) -> RemoteClient:
        await self._get_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        async with self._session_lock:
            if self._closed:
                return
            self._closed = True
            session = self._session if self._owns_session else None
            resolver = self._resolver if self._owns_resolver else None
        try:
            if session is not None:
                result = session.close()
                if inspect.isawaitable(result):
                    await result
        finally:
            if resolver is not None:
                await resolver.close()

    async def inspect(
        self,
        url: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> RemoteFileInfo:
        """Inspect headers without downloading a response body."""

        try:
            async with asyncio.timeout(self.timeouts.total):
                return await self._inspect(url, cancel_event=cancel_event)
        except TimeoutError as error:
            raise RemoteTimeout("remote inspection timed out") from error

    async def download(
        self,
        url: str,
        output_dir: str | Path,
        *,
        filename: str | None = None,
        overwrite: bool = False,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressCallback | None = None,
        max_bytes: int | None = None,
        single_threaded: bool = False,
        can_promote: Callable[[], bool] | None = None,
        mode_changed: Callable[[str], Awaitable[None] | None] | None = None,
        retry: RetryCallback | None = None,
        retry_not_before: float = 0,
    ) -> DownloadResult:
        """Retry temporary source failures within one shared operation budget."""
        from tgdrive.transfers.remote_ranges import RangeFallback, _retry_after

        parallel = not single_threaded
        try:
            async with asyncio.timeout(self.timeouts.total):
                if retry_not_before:
                    await self._wait_for_retry(
                        max(0, retry_not_before - time.time()), cancel_event
                    )
                for attempt in range(1, REMOTE_MAX_ATTEMPTS + 1):
                    self._raise_if_cancelled(cancel_event)
                    failure = None
                    try:
                        async with self.scheduler.admit(
                            lambda wait: self._cancelable(wait, cancel_event),
                            can_promote,
                            mode_changed,
                        ) as ticket:
                            token = self._download_ticket.set(ticket)
                            try:
                                return await self._download(
                                    url,
                                    Path(output_dir),
                                    filename=filename,
                                    overwrite=overwrite,
                                    cancel_event=cancel_event,
                                    progress=progress,
                                    max_bytes=self.max_bytes
                                    if max_bytes is None
                                    else max_bytes,
                                    parallel=parallel,
                                )
                            finally:
                                self._download_ticket.reset(token)
                    except RangeFallback as error:
                        # The initial full response can also break while ranges
                        # are running. All remaining attempts stay single-stream.
                        parallel = False
                        failure = error
                        delay = error.retry_after
                        if delay or (
                            error.__cause__ is not None
                            and source_retryable(error.__cause__)
                        ):
                            delay = max(2.0**attempt, delay)
                    except Exception as error:
                        if not source_retryable(error):
                            raise
                        parallel = False
                        failure = error
                        delay = 2.0**attempt
                        if (
                            isinstance(error, RemoteHTTPError)
                            and error.retry_after is not None
                        ):
                            delay = max(delay, _retry_after(error.retry_after))
                    if attempt == REMOTE_MAX_ATTEMPTS:
                        raise RemoteRetriesExhausted(
                            "Source download failed after 4 attempts. "
                            "Try again later or use another source link."
                        ) from failure
                    self._raise_if_cancelled(cancel_event)
                    # The failed response/file and scheduler slot are released
                    # before callbacks or backoff can block another download.
                    if retry is not None:
                        result = retry(attempt + 1, delay, failure)
                        if inspect.isawaitable(result):
                            await result
                    else:
                        logger.info(
                            "Source retry scheduled attempt=%s/%s error_type=%s "
                            "retry_after_seconds=%.3f",
                            attempt + 1,
                            REMOTE_MAX_ATTEMPTS,
                            type(failure).__name__,
                            delay,
                        )
                    await self._wait_for_retry(delay, cancel_event)
        except TimeoutError as error:
            raise RemoteTimeout("remote download timed out") from error
        raise AssertionError("remote retry loop exhausted unexpectedly")

    async def _wait_for_retry(self, delay: float, cancel_event) -> None:
        await self._cancelable(asyncio.sleep(delay), cancel_event)

    async def _inspect(
        self,
        url: str,
        *,
        cancel_event: asyncio.Event | None,
    ) -> RemoteFileInfo:
        response = None
        final_url = url
        try:
            response, final_url = await self._request_following_redirects(
                url,
                headers={"Accept-Encoding": "identity", "Range": "bytes=0-0"},
                cancel_event=cancel_event,
            )
            if response.status not in (200, 206):
                raise RemoteHTTPError(
                    response.status,
                    final_url,
                    retry_after=self._header(response.headers, "Retry-After"),
                )
            self._require_identity_encoding(response.headers)
            size = self._declared_size(response.status, response.headers)
            self._enforce_size(size)
            content_type = self._header(response.headers, "Content-Type")
            return RemoteFileInfo(
                url=final_url,
                filename=safe_filename(
                    self._header(response.headers, "Content-Disposition"),
                    final_url,
                    content_type,
                ),
                size=size,
                content_type=(content_type.split(";", 1)[0] if content_type else None),
                etag=self._header(response.headers, "ETag"),
            )
        finally:
            await self._release_response(response)

    async def _download(
        self,
        url: str,
        output_dir: Path,
        *,
        filename: str | None,
        overwrite: bool,
        cancel_event: asyncio.Event | None,
        progress: ProgressCallback | None,
        max_bytes: int,
        parallel: bool,
    ) -> DownloadResult:
        response = None
        temp_path: Path | None = None
        temp_file: Any = None
        final_url = url
        try:
            response, final_url = await self._request_following_redirects(
                url,
                headers={"Accept-Encoding": "identity"},
                cancel_event=cancel_event,
            )
            if response.status != 200:
                raise RemoteHTTPError(
                    response.status,
                    final_url,
                    retry_after=self._header(response.headers, "Retry-After"),
                )
            self._require_identity_encoding(response.headers)
            declared_size = self._declared_size(response.status, response.headers)
            self._enforce_size(declared_size, max_bytes)

            content_type = self._header(response.headers, "Content-Type")
            chosen_name = safe_filename(
                None
                if filename is not None
                else self._header(response.headers, "Content-Disposition"),
                final_url if filename is None else filename,
                content_type,
            )
            output_dir = await asyncio.to_thread(
                self._prepare_output_dir,
                output_dir,
            )
            final_path = output_dir / chosen_name
            if final_path.exists() and not overwrite:
                raise RemoteDestinationExists(
                    f"destination {chosen_name!r} already exists"
                )

            temp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="wb",
                prefix=f".{chosen_name}.",
                suffix=".part",
                dir=output_dir,
                delete=False,
            )
            temp_path = Path(temp_file.name)
            downloaded = 0
            await self._report_progress(progress, downloaded, declared_size)

            from tgdrive.transfers.remote_ranges import download_ranges, range_validator

            validator = range_validator(response.headers)
            ticket = self._download_ticket.get()
            while True:
                if (
                    parallel
                    and self.range_policy.max_connections > 1
                    and declared_size is not None
                    and declared_size - downloaded >= self.range_policy.minimum_size
                    and validator is not None
                    and (ticket is None or ticket.full_speed)
                ):
                    # A waiting source can switch to parallel ranges in-place
                    # after promotion, keeping its already received prefix.
                    await download_ranges(
                        self,
                        response,
                        final_url,
                        temp_file,
                        output_dir,
                        declared_size,
                        validator,
                        cancel_event,
                        progress,
                        initial_offset=downloaded,
                    )
                    downloaded = declared_size
                    break
                chunk = await self._read_chunk(response.content, cancel_event)
                if not chunk:
                    break
                downloaded += len(chunk)
                self._enforce_size(downloaded, max_bytes)
                await self._disk_io(self._write_chunk, temp_file, chunk, output_dir)
                await self._report_progress(progress, downloaded, declared_size)

            if declared_size is not None and downloaded != declared_size:
                error_type = (
                    RemoteIncompleteResponse
                    if downloaded < declared_size
                    else RemoteProtocolError
                )
                raise error_type(
                    f"remote stream ended at {downloaded} of {declared_size} bytes"
                )

            await self._disk_io(temp_file.flush)
            await self._disk_io(os.fsync, temp_file.fileno())
            temp_file.close()
            temp_file = None
            await self._disk_io(
                self._publish_temp, temp_path, final_path, overwrite=overwrite
            )
            temp_path = None
            return DownloadResult(
                path=final_path,
                url=final_url,
                filename=chosen_name,
                size=downloaded,
                content_type=(content_type.split(";", 1)[0] if content_type else None),
            )
        except BaseException:
            if temp_file is not None:
                temp_file.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        finally:
            await self._release_response(response)

    async def _request_following_redirects(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        cancel_event: asyncio.Event | None,
    ) -> tuple[Any, str]:
        current = url
        session = await self._get_session()

        for redirect_count in range(self.max_redirects + 1):
            self._raise_if_cancelled(cancel_event)
            current = await self._cancelable(self._validate_url(current), cancel_event)
            response = await self._cancelable(
                session.request(
                    "GET",
                    current,
                    headers=dict(headers),
                    allow_redirects=False,
                    timeout=self.timeouts.to_aiohttp(),
                ),
                cancel_event,
                cleanup=self._release_response,
            )
            if response.status not in _REDIRECT_STATUSES:
                return response, current

            location = self._header(response.headers, "Location")
            await self._release_response(response)
            if not location:
                raise RemoteProtocolError("redirect response omitted Location")
            if redirect_count >= self.max_redirects:
                raise TooManyRedirects(
                    f"remote exceeded {self.max_redirects} redirect(s)"
                )
            current = urljoin(current, location)

        raise AssertionError("redirect loop exhausted unexpectedly")

    async def _validate_url(self, url: str) -> str:
        if not isinstance(url, str) or not url:
            raise UnsafeRemoteURL("remote URL must be a non-empty string")
        if any(ord(character) < 0x20 for character in url):
            raise UnsafeRemoteURL("remote URL contains control characters")

        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise UnsafeRemoteURL("only http and https URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeRemoteURL("userinfo is not allowed in remote URLs")
        if not parsed.hostname:
            raise UnsafeRemoteURL("remote URL must include a hostname")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as error:
            raise UnsafeRemoteURL("remote URL has an invalid port") from error

        host = parsed.hostname
        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            try:
                host.encode("idna")
            except UnicodeError as error:
                raise UnsafeRemoteURL("remote hostname is invalid") from error
            resolver = self._resolver
            if resolver is None:
                raise RuntimeError("remote resolver was not initialized") from None
            await resolver.resolve(host, port, socket.AF_UNSPEC)
        else:
            if not self.allow_private and address_is_forbidden(str(literal)):
                raise UnsafeRemoteURL("remote URL uses a non-public IP address")

        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )

    async def _get_session(self) -> HTTPSessionLike:
        async with self._session_lock:
            if self._closed:
                raise RemoteError("remote client is closed")
            if self._resolver is None:
                self._resolver = ValidatingResolver(
                    DefaultResolver(),
                    allow_private=self.allow_private,
                )
            resolver = self._resolver
            if self._session is not None:
                return self._session

            timeout = self.timeouts.to_aiohttp()
            if self._session_factory is not None:
                value = self._session_factory(resolver, timeout)
                self._session = await value if inspect.isawaitable(value) else value
            else:
                connector = aiohttp.TCPConnector(
                    resolver=resolver,
                    use_dns_cache=False,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                    auto_decompress=False,
                    trust_env=False,
                    read_bufsize=self.chunk_size,
                )
            return self._session

    def _write_chunk(
        self, output: Any, chunk: bytes, directory: Path, offset: int | None = None
    ) -> None:
        if self.min_free_bytes and shutil.disk_usage(directory).free < (
            self.min_free_bytes + len(chunk)
        ):
            raise RemoteError("Temporary storage is full. Free disk space and retry.")
        if offset is None:
            output.write(chunk)
        else:
            remaining = memoryview(chunk)
            while remaining:
                written = os.pwrite(output.fileno(), remaining, offset)
                if written <= 0:
                    raise OSError("remote staging write made no progress")
                remaining = remaining[written:]
                offset += written

    @staticmethod
    async def _disk_io(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # Cancelling an await does not stop its thread. Drain the write before
        # any worker/file cleanup can close or reuse its descriptor.
        worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise

    async def _read_chunk(
        self,
        content: Any,
        cancel_event: asyncio.Event | None,
        limit: int | None = None,
    ) -> bytes:
        from tgdrive.transfers.remote_scheduler import WARM_READ_SIZE

        ticket = self._download_ticket.get()
        if ticket is not None:
            await self._cancelable(ticket.pace(), cancel_event)
        size = self.chunk_size if limit is None else limit
        if ticket is not None and not ticket.full_speed:
            size = min(size, WARM_READ_SIZE)
        result = await self._cancelable(content.read(size), cancel_event)
        if ticket is not None:
            ticket.received(len(result))
        return result

    async def _cancelable(
        self,
        operation: Awaitable[Any],
        cancel_event: asyncio.Event | None,
        *,
        cleanup: Callable[[Any], Awaitable[None]] | None = None,
    ) -> Any:
        if cancel_event is None:
            return await operation
        read_task = asyncio.ensure_future(operation)
        cancel_task = asyncio.create_task(cancel_event.wait())
        delivered = False
        try:
            await asyncio.wait(
                (read_task, cancel_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_event.is_set():
                read_task.cancel()
                raise RemoteCancelled("remote download was cancelled")
            result = await read_task
            delivered = True
            return result
        finally:
            cancel_task.cancel()
            if not read_task.done():
                read_task.cancel()
            await asyncio.gather(read_task, cancel_task, return_exceptions=True)
            if (
                cleanup is not None
                and not delivered
                and not read_task.cancelled()
                and read_task.exception() is None
            ):
                await cleanup(read_task.result())

    @staticmethod
    async def _report_progress(
        callback: ProgressCallback | None,
        current: int,
        total: int | None,
    ) -> None:
        if callback is None:
            return
        value = callback(current, total)
        if inspect.isawaitable(value):
            await value

    @staticmethod
    def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RemoteCancelled("remote operation was cancelled")

    def _enforce_size(self, size: int | None, limit: int | None = None) -> None:
        maximum = self.max_bytes if limit is None else limit
        if size is not None and size > maximum:
            raise RemoteTooLarge(maximum, size)

    @classmethod
    def _declared_size(cls, status: int, headers: Mapping[str, str]) -> int | None:
        content_range = cls._header(headers, "Content-Range")
        if content_range:
            match = _CONTENT_RANGE_RE.fullmatch(content_range.strip())
            if match and match.group(1) != "*":
                return int(match.group(1))
        if status == 200:
            content_length = cls._header(headers, "Content-Length")
            if content_length is not None:
                try:
                    value = int(content_length)
                except ValueError as error:
                    raise RemoteProtocolError(
                        "invalid Content-Length header"
                    ) from error
                if value < 0:
                    raise RemoteProtocolError("negative Content-Length header")
                return value
        return None

    @classmethod
    def _require_identity_encoding(cls, headers: Mapping[str, str]) -> None:
        encoding = cls._header(headers, "Content-Encoding")
        if encoding and encoding.strip().lower() != "identity":
            raise RemoteProtocolError("remote ignored identity content encoding")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        direct = headers.get(name)
        if direct is not None:
            return str(direct)
        lowered = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
        return None

    @staticmethod
    async def _release_response(response: Any | None) -> None:
        if response is None:
            return
        release = getattr(response, "release", None)
        if callable(release):
            result = release()
            if inspect.isawaitable(result):
                await result
            return
        close = getattr(response, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _prepare_output_dir(output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir.resolve()

    @staticmethod
    def _publish_temp(temp_path: Path, final_path: Path, *, overwrite: bool) -> None:
        if overwrite:
            os.replace(temp_path, final_path)
        else:
            try:
                os.link(temp_path, final_path)
            except FileExistsError as error:
                raise RemoteDestinationExists(
                    f"destination {final_path.name!r} already exists"
                ) from error
            else:
                temp_path.unlink()
        descriptor = os.open(final_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
