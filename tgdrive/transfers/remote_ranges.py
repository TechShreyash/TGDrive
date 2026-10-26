"""Adaptive, validator-bound HTTP ranges written into one unpublished file."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from aiohttp import ClientConnectionError, ClientPayloadError, ClientSSLError

from tgdrive.transfers.remote import RemoteCancelled, RemoteError

if TYPE_CHECKING:
    from pathlib import Path

    from tgdrive.transfers.remote import ProgressCallback, RemoteClient

MIB = 1024 * 1024
_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)
_STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"')


@dataclass(frozen=True, slots=True)
class RangePolicy:
    minimum_size: int = 16 * MIB
    block_size: int = 8 * MIB
    max_connections: int = 8
    sample_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.block_size <= 0 or self.minimum_size < 2 * self.block_size:
            raise ValueError(
                "parallel threshold must cover at least two positive blocks"
            )
        if not 1 <= self.max_connections <= 8 or self.sample_seconds <= 0:
            raise ValueError("invalid remote range concurrency or sample interval")


class RangeFallback(RemoteError):
    """Drain all writers and restart once using an ordinary full response."""

    def __init__(self, size: int, *, retry_after: float = 0) -> None:
        super().__init__("source cannot safely continue parallel retrieval")
        self.size = size
        self.retry_after = retry_after


def range_validator(headers: Any) -> str | None:
    """An advertised range capability alone cannot identify immutable bytes."""
    from tgdrive.transfers.remote import RemoteClient

    if (RemoteClient._header(headers, "Accept-Ranges") or "").lower() == "none":
        return None
    etag = (RemoteClient._header(headers, "ETag") or "").strip()
    return etag if _STRONG_ETAG.fullmatch(etag) else None


class RangeConcurrency:
    """Probe 2 → 4 → 8, keeping a lower width when added streams do not help."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.target = min(2, maximum)
        self.accepted = self.target
        self.rate = 0.0
        self.probing = False
        self.settled = False
        self.slow_samples = 0

    def observe(self, rate: float) -> int:
        if rate <= 0:
            return self.target
        if self.probing:
            if rate < self.rate * 1.10:
                self.target = self.accepted
                self.settled = True
            else:
                self.accepted, self.rate = self.target, rate
            self.probing = False
        elif not self.rate:
            self.rate = rate
        elif self.settled or self.target == self.maximum:
            self.slow_samples = self.slow_samples + 1 if rate < self.rate * 0.7 else 0
            if self.slow_samples >= 2 and self.target > 2:
                self.target = self.accepted = max(2, self.target // 2)
                self.rate, self.slow_samples = rate, 0
            return self.target
        if not self.settled and self.target < self.maximum:
            self.target = min(self.maximum, self.target * 2)
            self.probing = True
        return self.target


def _retry_after(value: str | None) -> float:
    if value is not None:
        try:
            return max(0.0, float(int(value)))
        except ValueError, OverflowError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except ValueError, TypeError, OverflowError:
                pass
    return 1.0


async def download_ranges(
    client: RemoteClient,
    initial: Any,
    url: str,
    output: Any,
    directory: Path,
    size: int,
    validator: str,
    cancel_event: asyncio.Event | None,
    progress: ProgressCallback | None,
    *,
    initial_offset: int = 0,
) -> None:
    policy = client.range_policy
    controller = RangeConcurrency(policy.max_connections)
    prefix_end = initial_offset + policy.block_size
    next_offset = prefix_end
    block_size = policy.block_size
    written = initial_offset
    live_workers = 0
    progress_lock = asyncio.Lock()
    workers: set[asyncio.Task[None]] = set()
    all_workers: set[asyncio.Task[None]] = set()
    cancelled = asyncio.create_task(cancel_event.wait()) if cancel_event else None

    async def consume(response: Any, start: int, end: int, *, exact: bool) -> int:
        nonlocal written
        cursor = start
        while cursor < end:
            chunk = await client._read_chunk(
                response.content, cancel_event, min(client.chunk_size, end - cursor)
            )
            if not chunk or len(chunk) > end - cursor:
                raise RangeFallback(size)
            await client._disk_io(client._write_chunk, output, chunk, directory, cursor)
            cursor += len(chunk)
            async with progress_lock:
                written += len(chunk)
                await client._report_progress(progress, written, size)
        if exact and await client._read_chunk(response.content, cancel_event, 1):
            raise RangeFallback(size)
        return cursor

    async def fetch(start: int, end: int) -> None:
        # Failed ranges are discarded before a single-stream fallback. Never
        # retry a partial interval while counting the same bytes a second time.
        response = None
        try:
            async with client._range_slots:
                response, final_url = await client._request_following_redirects(
                    url,
                    headers={
                        "Accept-Encoding": "identity",
                        "Range": f"bytes={start}-{end - 1}",
                        "If-Range": validator,
                    },
                    cancel_event=cancel_event,
                )
                if response.status in {429, 503}:
                    raise RangeFallback(
                        size,
                        retry_after=_retry_after(
                            client._header(response.headers, "Retry-After")
                        ),
                    )
                if response.status != 206 or final_url != url:
                    raise RangeFallback(size)
                content_range = _RANGE.fullmatch(
                    (client._header(response.headers, "Content-Range") or "").strip()
                )
                if (
                    content_range is None
                    or tuple(map(int, content_range.groups())) != (start, end - 1, size)
                    or range_validator(response.headers) != validator
                    or (
                        client._header(response.headers, "Content-Encoding")
                        or "identity"
                    )
                    .strip()
                    .lower()
                    != "identity"
                ):
                    raise RangeFallback(size)
                length = client._header(response.headers, "Content-Length")
                if length is not None and length.strip() != str(end - start):
                    raise RangeFallback(size)
                await consume(response, start, end, exact=True)
        except ClientSSLError:
            raise
        except (ClientConnectionError, ClientPayloadError, TimeoutError) as error:
            raise RangeFallback(size) from error
        finally:
            await client._release_response(response)

    async def worker(*, prefix: bool = False) -> None:
        nonlocal next_offset, block_size, live_workers
        try:
            if prefix:
                try:
                    await consume(initial, initial_offset, prefix_end, exact=False)
                except ClientSSLError:
                    raise
                except (
                    ClientConnectionError,
                    ClientPayloadError,
                    TimeoutError,
                ) as error:
                    raise RangeFallback(size) from error
                finally:
                    await client._release_response(initial)
                return
            while next_offset < size and live_workers <= controller.target:
                client._raise_if_cancelled(cancel_event)
                start = next_offset
                fair_block = max(policy.block_size, (size - start) // controller.target)
                end = min(size, start + min(block_size, fair_block))
                next_offset = end
                began = time.monotonic()
                await fetch(start, end)
                elapsed = time.monotonic() - began
                if elapsed > 0:
                    # Aim for two seconds per request, avoiding both a swarm of
                    # tiny ranges and huge final intervals that strand peers.
                    block_size = min(
                        64 * MIB,
                        max(policy.block_size, int((end - start) / elapsed * 2)),
                    )
        finally:
            live_workers -= 1

    def spawn(*, prefix: bool = False) -> None:
        nonlocal live_workers
        live_workers += 1
        task = asyncio.create_task(worker(prefix=prefix))
        workers.add(task)
        all_workers.add(task)

    try:
        spawn(prefix=True)
        spawn()
        sampled_at, sampled_bytes = time.monotonic(), initial_offset
        while workers:
            waiting = workers | ({cancelled} if cancelled is not None else set())
            done, _ = await asyncio.wait(
                waiting,
                timeout=policy.sample_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            client._raise_if_cancelled(cancel_event)
            for task in done & workers:
                workers.remove(task)
                all_workers.discard(task)
                task.result()
            now = time.monotonic()
            if now - sampled_at >= policy.sample_seconds:
                controller.observe((written - sampled_bytes) / (now - sampled_at))
                sampled_at, sampled_bytes = now, written
            while live_workers < controller.target and next_offset < size:
                spawn()
        if written != size:
            raise RangeFallback(size)
    except RemoteCancelled:
        raise
    finally:
        pending = all_workers | ({cancelled} if cancelled is not None else set())
        for task in pending:
            if not task.done():
                task.cancel()
        # All positioned writes must finish before the caller can close/truncate
        # the file. A second shutdown cancellation cannot skip that drain.
        drain = asyncio.gather(*pending, return_exceptions=True)
        interrupted = False
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError
