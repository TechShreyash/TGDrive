"""Synthetic HTTP range responses; no external downloads or speed claims."""

from __future__ import annotations

import asyncio
import re
import threading
from itertools import pairwise

import pytest

from tests.transfers.test_remote import PUBLIC_DNS, FakeResolver, FakeResponse
from tgdrive.transfers.remote import RemoteCancelled, RemoteClient, UnsafeRemoteURL
from tgdrive.transfers.remote_ranges import RangeConcurrency, RangePolicy, _retry_after


class RangeSource:
    def __init__(self, *, failure=None):
        self.data = bytes(range(128))
        self.calls = []
        self.responses = []
        self.failure = failure
        self.full_requests = 0

    async def request(self, method, url, **kwargs):
        headers = kwargs["headers"]
        self.calls.append((url, headers))
        if "Range" not in headers:
            self.full_requests += 1
            response = FakeResponse(
                200,
                headers={"Content-Length": str(len(self.data)), "ETag": '"version-1"'},
                chunks=[self.data],
            )
        else:
            assert headers["If-Range"] == '"version-1"'
            start, last = map(
                int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups()
            )
            response = FakeResponse(
                206,
                headers={
                    "Content-Range": f"bytes {start}-{last}/{len(self.data)}",
                    "Content-Length": str(last - start + 1),
                    "ETag": '"version-1"',
                },
                chunks=[self.data[start : last + 1]],
            )
            if self.failure is not None:
                self.failure(response)
        self.responses.append(response)
        return response

    async def close(self):
        pass


def client_for(source, **kwargs):
    return RemoteClient(
        session=source,
        resolver=FakeResolver(PUBLIC_DNS),
        range_policy=RangePolicy(minimum_size=16, block_size=8, max_connections=4),
        **kwargs,
    )


async def test_parallel_ranges_assemble_exact_original_and_monotonic_progress(tmp_path):
    source = RangeSource()
    client = client_for(source)
    progress = []
    result = await client.download(
        "https://files.example/movie.mov",
        tmp_path,
        progress=lambda current, total: progress.append((current, total)),
    )
    assert result.path.read_bytes() == source.data
    assert source.full_requests == 1
    ranges = [headers["Range"] for _, headers in source.calls if "Range" in headers]
    assert ranges
    intervals = sorted(tuple(map(int, re.findall(r"\d+", value))) for value in ranges)
    assert intervals[0][0] == 8
    assert intervals[-1][1] == len(source.data) - 1
    assert all(left[1] + 1 == right[0] for left, right in pairwise(intervals))
    assert progress[0] == (0, len(source.data))
    assert progress[-1] == (len(source.data), len(source.data))
    assert [row[0] for row in progress] == sorted(row[0] for row in progress)
    assert all(response.released for response in source.responses)
    assert not list(tmp_path.glob("*.part"))


async def test_parallel_workers_overlap_instead_of_serializing_range_requests(tmp_path):
    source = RangeSource()
    second_range = asyncio.Event()
    original_request = source.request
    range_count = 0

    async def request(method, url, **kwargs):
        nonlocal range_count
        response = await original_request(method, url, **kwargs)
        if "Range" in kwargs["headers"]:
            range_count += 1
            if range_count == 1:
                original_read = response.content.read

                async def wait_for_peer(limit):
                    await second_range.wait()
                    return await original_read(limit)

                response.content.read = wait_for_peer
            else:
                second_range.set()
        return response

    source.request = request
    result = await asyncio.wait_for(
        client_for(source).download("https://files.example/movie.mov", tmp_path), 3
    )
    assert second_range.is_set()
    assert result.path.read_bytes() == source.data


@pytest.mark.parametrize(
    "failure",
    [
        "ignored",
        "rejected",
        "wrong_range",
        "different_total",
        "changed_etag",
        "missing_etag",
        "compressed",
        "short",
        "long",
    ],
)
async def test_unsuitable_ranges_fall_back_once_without_mixing_bytes(tmp_path, failure):
    def fail(response):
        if failure == "ignored":
            response.status = 200
        elif failure == "rejected":
            response.status = 416
        elif failure == "wrong_range":
            response.headers["Content-Range"] = "bytes 0-7/128"
        elif failure == "different_total":
            response.headers["Content-Range"] = response.headers[
                "Content-Range"
            ].replace("/128", "/129")
        elif failure == "changed_etag":
            response.headers["ETag"] = '"version-2"'
            source.data = b"n" * 128
        elif failure == "missing_etag":
            del response.headers["ETag"]
        elif failure == "compressed":
            response.headers["Content-Encoding"] = "gzip"
        elif failure == "short":
            response.content.chunks.clear()
        elif failure == "long":
            response.content.chunks.append(b"extra")

    source = RangeSource(failure=fail)
    result = await client_for(source).download("https://files.example/a", tmp_path)
    assert source.full_requests == 2
    assert result.path.read_bytes() == source.data
    assert all(response.released for response in source.responses)
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("etag", [None, 'W/"weak"'])
async def test_sources_without_strong_identity_keep_the_initial_full_stream(
    tmp_path, etag
):
    source = RangeSource()
    request = source.request

    async def without_validator(*args, **kwargs):
        response = await request(*args, **kwargs)
        if etag is None:
            response.headers.pop("ETag", None)
        else:
            response.headers["ETag"] = etag
        return response

    source.request = without_validator
    result = await client_for(source).download("https://files.example/a", tmp_path)
    assert result.path.read_bytes() == source.data
    assert len(source.calls) == 1


async def test_compatibility_mode_does_not_probe_ranges(tmp_path):
    source = RangeSource()
    result = await client_for(source).download(
        "https://files.example/a", tmp_path, single_threaded=True
    )
    assert result.path.read_bytes() == source.data
    assert len(source.calls) == 1


async def test_range_redirects_cannot_reach_private_networks(tmp_path):
    def redirect(response):
        response.status = 302
        response.headers["Location"] = "http://127.0.0.1/private"

    source = RangeSource(failure=redirect)
    with pytest.raises(UnsafeRemoteURL):
        await client_for(source).download("https://files.example/a", tmp_path)
    assert all("127.0.0.1" not in url for url, _ in source.calls)
    assert not list(tmp_path.iterdir())


async def test_retry_after_wait_is_cancellable_and_does_not_hammer_source(tmp_path):
    cancel = asyncio.Event()

    def throttle(response):
        response.status = 429
        response.headers["Retry-After"] = "3600"
        release = response.release

        def cancelled_release():
            release()
            cancel.set()

        response.release = cancelled_release

    source = RangeSource(failure=throttle)
    with pytest.raises(RemoteCancelled):
        await asyncio.wait_for(
            client_for(source).download(
                "https://files.example/a", tmp_path, cancel_event=cancel
            ),
            3,
        )
    assert source.full_requests == 1
    assert not list(tmp_path.iterdir())


async def test_cancellation_drains_disk_workers_before_closing_staging(
    tmp_path, monkeypatch
):
    source = RangeSource()
    client = client_for(source)
    entered, release = threading.Event(), threading.Event()
    original_write = client._write_chunk
    closed_early = []

    def blocked_write(output, *args):
        entered.set()
        release.wait(3)
        closed_early.append(output.closed)
        original_write(output, *args)

    monkeypatch.setattr(client, "_write_chunk", blocked_write)
    download = asyncio.create_task(client.download("https://files.example/a", tmp_path))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        download.cancel()
        await asyncio.sleep(0)
        assert not download.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await download
    assert closed_early and not any(closed_early)
    assert not list(tmp_path.iterdir())


def test_adaptive_width_retains_only_useful_extra_connections():
    controller = RangeConcurrency(8)
    assert controller.target == 2
    assert controller.observe(10) == 4
    assert controller.observe(18) == 8
    assert controller.observe(19) == 4
    assert controller.observe(18) == 4
    assert controller.observe(5) == 4
    assert controller.observe(5) == 2


def test_adaptive_width_and_retry_after_are_bounded_and_validated():
    controller = RangeConcurrency(8)
    assert controller.observe(10) == 4
    assert controller.observe(20) == 8
    assert controller.observe(40) == 8
    assert _retry_after("120") == 120
    with pytest.raises(ValueError):
        RangePolicy(max_connections=0)
    with pytest.raises(ValueError):
        RangePolicy(block_size=0)


async def test_promoted_stream_keeps_its_prefix_when_switching_to_ranges(tmp_path):
    source = RangeSource()
    client = client_for(source, chunk_size=3)
    ready = False
    modes = []

    async def progress(current, _total):
        nonlocal ready
        if current >= 3:
            ready = True
            await client.scheduler.refresh()

    result = await client.download(
        "https://files.example/movie.mov",
        tmp_path,
        can_promote=lambda: ready,
        mode_changed=modes.append,
        progress=progress,
    )
    assert result.path.read_bytes() == source.data
    assert modes == ["queued", "warm", "full"]
    assert source.full_requests == 1
    intervals = [headers["Range"] for _, headers in source.calls if "Range" in headers]
    assert intervals[0].startswith("bytes=11-")
