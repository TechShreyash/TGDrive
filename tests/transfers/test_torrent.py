"""Torrent intake tests use synthetic metadata and a fake downloader."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import aiohttp
import pytest

from tgdrive.transfers import torrent
from tgdrive.transfers.remote import RemoteClient


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        ("error", "2", True),
        ("error", "6", True),
        ("error", "9", False),
        ("error", "1", False),
        ("removed", "6", False),
    ],
)
async def test_only_known_aria2_transport_outcomes_are_retryable(
    tmp_path, status, code, retryable
):
    engine = torrent._Aria2(tmp_path)
    engine.call = AsyncMock(return_value={"status": status, "errorCode": code})
    with pytest.raises(torrent.TorrentError) as caught:
        await engine.wait("gid")
    assert isinstance(caught.value, torrent.TorrentTransportError) is retryable


@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientConnectionError()])
async def test_rpc_transport_failure_has_a_safe_typed_error(tmp_path, error):
    engine = torrent._Aria2(tmp_path)
    engine.url = "http://127.0.0.1/jsonrpc"

    def failed(*_args, **_kwargs):
        raise error

    engine.session = cast(aiohttp.ClientSession, SimpleNamespace(post=failed))
    with pytest.raises(
        torrent.TorrentTransportError, match="connection was interrupted"
    ):
        await engine.call("tellStatus", "gid")


async def test_no_peer_stall_is_terminal(tmp_path, monkeypatch):
    engine = torrent._Aria2(tmp_path)
    engine.call = AsyncMock(return_value={"status": "active", "connections": "0"})
    clock = iter([0, 0, torrent.STALL_TIMEOUT + 1])
    monkeypatch.setattr(torrent, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(
        torrent.TorrentError, match="no progress for 5 minutes"
    ) as caught:
        await engine.wait("gid")
    assert not isinstance(caught.value, torrent.TorrentTransportError)


def metadata(files=None) -> dict[bytes, object]:
    info = {
        b"name": b"Collection",
        b"piece length": 16,
        b"pieces": b"x" * 20,
    }
    if files is None:
        info[b"length"] = 4
    else:
        info[b"files"] = files
    return {b"info": info}


def test_nested_folder_manifest_retains_relative_paths():
    source = metadata(
        [
            {b"length": 4, b"path": [b"one.txt"]},
            {b"length": 5, b"path": [b"nested", b"two.txt"]},
        ]
    )
    manifest = torrent._manifest(torrent._decode(torrent._encode(source)))

    assert manifest.name == "Collection"
    assert manifest.is_folder
    assert manifest.total_size == 9
    assert manifest.files == (
        torrent.TorrentFile("Collection/one.txt", 4),
        torrent.TorrentFile("Collection/nested/two.txt", 5),
    )


@pytest.mark.parametrize(
    "files",
    [
        [{b"length": 4, b"path": [b"..", b"escaped"]}],
        [{b"length": 4, b"path": [b"/absolute"]}],
        [{b"length": 4, b"path": [b"nested\\escape"]}],
        [{b"length": 4, b"path": [b"link"], b"attr": b"l"}],
        [{b"length": 0, b"path": [b"empty"]}],
        [
            {b"length": 4, b"path": [b"same"]},
            {b"length": 4, b"path": [b"SAME"]},
        ],
        [
            {b"length": 4, b"path": [b"same"]},
            {b"length": 4, b"path": [b"same", b"child"]},
        ],
        [
            {b"length": 4, b"path": [b"Folder", b"one"]},
            {b"length": 4, b"path": [b"folder", b"two"]},
        ],
    ],
)
def test_rejects_unsafe_or_unuploadable_files_before_download(files):
    with pytest.raises(torrent.TorrentError):
        torrent._manifest(metadata(files))


@pytest.mark.parametrize(
    "source",
    [
        b"degarbage",
        b"d1:ai1e1:ai2ee",
        b"d1:ai01ee",
        b"d1:al",
        b"d1:a99999:xe",
    ],
)
def test_rejects_malformed_bencode(source):
    with pytest.raises(torrent.TorrentError):
        torrent._decode(source)


def test_magnet_keeps_only_infohash_and_reports_trackers_separately():
    source = (
        "magnet:?xt=urn:btih:"
        + "ab" * 20
        + "&tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce"
        + "&xs=http%3A%2F%2F127.0.0.1%2Fsecret&ws=http%3A%2F%2Flocalhost%2F"
    )
    magnet, trackers = torrent._magnet(source)

    assert magnet == "magnet:?xt=urn:btih:" + "ab" * 20
    assert trackers == ["udp://tracker.example:80/announce"]


async def test_udp_tracker_pins_dns_and_rejects_private_addresses(monkeypatch):
    resolver = AsyncMock(return_value=[(2, 2, 17, "", ("93.184.216.34", 80))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)

    assert await torrent._udp_trackers(
        [
            "https://tracker.example/announce",
            "udp://tracker.example:80/announce",
        ]
    ) == ["udp://93.184.216.34:80/announce"]

    resolver.return_value = [(2, 2, 17, "", ("127.0.0.1", 80))]
    with pytest.raises(torrent.TorrentError, match="public"):
        await torrent._udp_trackers(["udp://tracker.example:80/announce"])


async def test_cancellation_drains_worker_before_return(monkeypatch, tmp_path):
    started, stopped, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(torrent, "_download", blocked)
    task = asyncio.create_task(
        torrent.download_torrent(
            "magnet:?xt=urn:btih:" + "ab" * 20,
            tmp_path,
            remote=cast(RemoteClient, None),
            cancel_event=cancel,
            progress=AsyncMock(),
            admit=AsyncMock(),
        )
    )
    await started.wait()
    cancel.set()

    with pytest.raises(torrent.TorrentCancelled):
        await task
    assert stopped.is_set()


async def test_validates_and_admits_before_unpausing_payload(monkeypatch, tmp_path):
    calls, metadata_dir = [], None
    source = metadata()
    source[b"url-list"] = b"http://127.0.0.1/private"

    class Engine:
        def __init__(self, directory):
            nonlocal metadata_dir
            metadata_dir = directory

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            calls.append("stopped")

        async def call(self, method, *params):
            calls.append(method)
            if method == "addTorrent":
                sanitized = torrent._decode(base64.b64decode(params[0]))
                assert sanitized == {b"info": source[b"info"]}
                assert params[2]["pause"] == "true"
                return "gid"
            if method == "getFiles":
                return [
                    {"path": str((tmp_path / "Collection").resolve()), "length": "4"}
                ]

        async def wait(self, _gid, *, progress):
            (tmp_path / "Collection").write_bytes(b"data")
            await progress(4, 4)

    async def download(_url, directory, **kwargs):
        assert kwargs["max_bytes"] == torrent.MAX_METADATA_BYTES
        path = directory / "source.torrent"
        path.write_bytes(torrent._encode(source))
        return SimpleNamespace(path=path)

    async def admit(manifest):
        calls.append("admit")
        assert not (tmp_path / manifest.files[0].path).exists()

    monkeypatch.setattr(torrent, "_Aria2", Engine)
    remote = SimpleNamespace(download=AsyncMock(side_effect=download), min_free_bytes=0)
    capacity = AsyncMock()
    manifest = await torrent.download_torrent(
        "https://files.example/source.torrent",
        tmp_path,
        remote=cast(RemoteClient, remote),
        cancel_event=asyncio.Event(),
        progress=AsyncMock(),
        admit=admit,
        check_capacity=capacity,
    )

    assert [call.args[0] for call in capacity.await_args_list] == [
        torrent.MAX_METADATA_BYTES,
        torrent.MAX_SANITIZED_BYTES,
        len(torrent._encode({b"info": source[b"info"]})),
        0,
    ]
    assert manifest.total_size == 4
    assert calls == ["admit", "addTorrent", "getFiles", "unpause", "stopped"]
    assert metadata_dir is not None
    assert not Path(metadata_dir).exists()

    capacity.reset_mock()
    retried = await torrent.download_torrent(
        "https://files.example/expired.torrent",
        tmp_path,
        remote=cast(RemoteClient, remote),
        cancel_event=asyncio.Event(),
        progress=AsyncMock(),
        admit=AsyncMock(),
        check_capacity=capacity,
    )
    assert [call.args[0] for call in capacity.await_args_list] == [
        torrent.MAX_SANITIZED_BYTES,
        len(torrent._encode({b"info": source[b"info"]})),
        0,
    ]
    assert retried.info_hash == manifest.info_hash
    remote.download.assert_awaited_once()


async def test_downloader_shutdown_waits_for_process_and_closes_rpc():
    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    process.terminate = lambda: setattr(process, "returncode", -15)
    engine = torrent._Aria2(Path("unused"))
    engine.process = cast(asyncio.subprocess.Process, process)
    engine.session = cast(aiohttp.ClientSession, SimpleNamespace(close=AsyncMock()))

    await engine.__aexit__(None, None, None)

    process.wait.assert_awaited_once()
    assert engine.session is not None
    cast(AsyncMock, engine.session.close).assert_awaited_once()


async def test_selected_files_are_the_only_ones_aria2c_downloads(monkeypatch, tmp_path):
    source = metadata(
        [
            {b"length": 4, b"path": [b"one.txt"]},
            {b"length": 5, b"path": [b"two.txt"]},
            {b"length": 6, b"path": [b"three.txt"]},
        ]
    )
    options = {}

    class Engine:
        def __init__(self, _directory):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, *params):
            if method == "addTorrent":
                options.update(params[2])
                return "gid"
            if method == "getFiles":
                root = (tmp_path / "Collection").resolve()
                return [
                    {"path": str(root / name), "length": str(size)}
                    for name, size in (("one.txt", 4), ("two.txt", 5), ("three.txt", 6))
                ]

        async def wait(self, _gid, *, progress):
            # aria2c leaves unselected files absent; only chosen ones are checked.
            (tmp_path / "Collection").mkdir()
            (tmp_path / "Collection/one.txt").write_bytes(b"1111")
            (tmp_path / "Collection/three.txt").write_bytes(b"333333")
            await progress(10, 10)

    async def download(_url, directory, **_kwargs):
        path = directory / "source.torrent"
        path.write_bytes(torrent._encode(source))
        return SimpleNamespace(path=path)

    async def admit(_manifest):
        return [2, 0]

    monkeypatch.setattr(torrent, "_Aria2", Engine)
    remote = SimpleNamespace(download=AsyncMock(side_effect=download), min_free_bytes=0)
    await torrent.download_torrent(
        "https://files.example/source.torrent",
        tmp_path,
        remote=cast(RemoteClient, remote),
        cancel_event=asyncio.Event(),
        progress=AsyncMock(),
        admit=admit,
    )
    assert options["select-file"] == "1,3"


async def test_retry_progress_counts_verified_pieces_instead_of_restarting(tmp_path):
    engine = torrent._Aria2(tmp_path)
    states = iter(
        [
            # Re-hashing pieces kept from the failed attempt.
            {"status": "active", "completedLength": "0", "verifiedLength": "600"},
            {"status": "complete", "completedLength": "1000", "totalLength": "1000"},
        ]
    )

    async def call(method, *params):
        return next(states)

    engine.call = call
    seen = []

    async def progress(current, total):
        seen.append(current)

    original = asyncio.sleep
    torrent.asyncio.sleep = AsyncMock()
    try:
        await engine.wait("gid", progress=progress)
    finally:
        torrent.asyncio.sleep = original
    assert seen == [600, 1000]


@pytest.mark.parametrize(
    "limit,value,source,match",
    [
        ("MAX_CONTAINERS", 3, b"d1:al" + b"le" * 3 + b"ee", "container limit"),
        ("MAX_TOKENS", 5, b"d1:ali1ei2ei3eee", "token limit"),
        ("MAX_CONTAINER_ITEMS", 2, b"d1:ali1ei2ei3eee", "container entries"),
        ("MAX_SANITIZED_BYTES", 4, b"d1:a5:12345e", "byte limit"),
        ("MAX_VALUE_BYTES", 9, b"d1:a4:12341:b4:5678e", "byte limit"),
    ],
)
def test_decoded_budgets_reject_before_next_allocation(
    monkeypatch, limit, value, source, match
):
    monkeypatch.setattr(torrent, limit, value)
    with pytest.raises(torrent.TorrentError, match=match):
        torrent._decode(source)


def test_torrent_path_components_are_bounded_before_folder_expansion(monkeypatch):
    monkeypatch.setattr(torrent, "MAX_PATH_COMPONENTS", 3)
    with pytest.raises(torrent.TorrentError, match="component limit"):
        torrent._manifest(
            metadata(
                [
                    {b"length": 4, b"path": [b"one"]},
                    {b"length": 5, b"path": [b"two"]},
                ]
            )
        )


async def test_child_returns_only_bounded_sanitized_metadata(tmp_path, monkeypatch):
    source = metadata()
    source[b"url-list"] = b"http://127.0.0.1/private"
    source[b"announce"] = b"udp://tracker.example:80/announce"
    raw = tmp_path / "source.torrent"
    raw.write_bytes(torrent._encode(source))
    expected = torrent._manifest(source)
    sanitized = torrent._encode({b"info": source[b"info"]})

    def no_parent_decode(_data):
        pytest.fail("The parent must never reconstruct the raw metadata graph.")

    monkeypatch.setattr(torrent, "_decode", no_parent_decode)
    actual, payload, trackers, private = await torrent._parse_metadata(
        raw, tmp_path / "sanitized.torrent"
    )
    assert actual == expected
    assert payload == sanitized
    assert trackers == ["udp://tracker.example:80/announce"]
    assert private is False
    assert raw.read_bytes() == torrent._encode(source)


async def test_child_rejects_container_expansion_without_producing_metadata(tmp_path):
    raw = tmp_path / "hostile.torrent"
    raw.write_bytes(b"d1:al" + b"le" * torrent.MAX_CONTAINERS + b"ee")
    output = tmp_path / "sanitized.torrent"
    with pytest.raises(torrent.TorrentError, match="container limit"):
        await torrent._parse_metadata(raw, output)
    assert not output.exists()


async def test_child_rechecks_serialized_limit_for_cached_or_magnet_metadata(tmp_path):
    raw = tmp_path / "source.torrent"
    with raw.open("wb") as source:
        source.truncate(torrent.MAX_METADATA_BYTES + 1)
    output = tmp_path / "sanitized.torrent"
    with pytest.raises(torrent.TorrentError, match="256 MiB"):
        await torrent._parse_metadata(raw, output)
    assert not output.exists()


async def test_rpc_size_bound_covers_exact_body_and_sends_checked_bytes(
    tmp_path, monkeypatch
):
    engine = torrent._Aria2(tmp_path)
    engine.url = "http://127.0.0.1/jsonrpc"
    sent = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return {"result": "gid"}

    def post(_url, **kwargs):
        sent.append(kwargs)
        return Response()

    engine.session = cast(aiohttp.ClientSession, SimpleNamespace(post=post))
    arguments = ["encoded", [], {"dir": "/é", "pause": "true"}]
    expected = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "aria2.addTorrent",
            "params": [f"token:{engine.secret}", *arguments],
        },
        separators=(",", ":"),
    ).encode()
    monkeypatch.setattr(torrent, "MAX_RPC_BYTES", len(expected))
    assert await engine.call("addTorrent", *arguments) == "gid"
    assert sent == [{"data": expected, "headers": {"Content-Type": "application/json"}}]
    monkeypatch.setattr(torrent, "MAX_RPC_BYTES", len(expected) - 1)
    with pytest.raises(torrent.TorrentError, match="RPC request limit"):
        await engine.call("addTorrent", *arguments)
    assert len(sent) == 1


async def test_parser_kills_and_reaps_child_after_repeated_cancellation(
    monkeypatch, tmp_path
):
    reading, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def readline():
        reading.set()
        await asyncio.Event().wait()

    async def wait():
        stopping.set()
        await release.wait()

    process = SimpleNamespace(
        stdout=SimpleNamespace(readline=readline, read=AsyncMock(return_value=b"")),
        returncode=None,
        wait=wait,
    )
    process.kill = lambda: setattr(process, "returncode", -9)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(
        torrent._parse_metadata(tmp_path / "in", tmp_path / "out")
    )
    await reading.wait()
    task.cancel()
    await stopping.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9


async def test_parser_owns_child_even_if_cancelled_during_spawn(monkeypatch, tmp_path):
    starting, release = asyncio.Event(), asyncio.Event()
    process = SimpleNamespace(returncode=None, stdout=None, wait=AsyncMock())
    process.kill = lambda: setattr(process, "returncode", -9)

    async def launch(*_args, **_kwargs):
        starting.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(
        torrent._parse_metadata(tmp_path / "in", tmp_path / "out")
    )
    await starting.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9
    process.wait.assert_awaited_once()


async def test_parser_rejects_oversized_child_response(monkeypatch, tmp_path):
    monkeypatch.setattr(torrent, "MAX_MANIFEST_BYTES", 64)
    output = asyncio.StreamReader()
    output.feed_data(b"x" * 65 + b"\n")
    output.feed_eof()
    process = SimpleNamespace(stdout=output, returncode=None, wait=AsyncMock())
    process.kill = lambda: setattr(process, "returncode", -9)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(torrent.TorrentError, match="invalid response"):
        await torrent._parse_metadata(tmp_path / "in", tmp_path / "out")
    assert process.returncode == -9
    process.wait.assert_awaited_once()


async def test_metadata_capacity_rejected_before_downloader_or_network(
    tmp_path, monkeypatch
):
    engine = AsyncMock()
    monkeypatch.setattr(torrent, "_Aria2", engine)
    remote = SimpleNamespace(download=AsyncMock(), min_free_bytes=0)
    capacity = AsyncMock(side_effect=torrent.TorrentError("Backing storage is full."))
    with pytest.raises(torrent.TorrentError, match="Backing storage"):
        await torrent.download_torrent(
            "https://files.example/source.torrent",
            tmp_path,
            remote=cast(RemoteClient, remote),
            cancel_event=asyncio.Event(),
            progress=AsyncMock(),
            admit=AsyncMock(),
            check_capacity=capacity,
        )
    capacity.assert_awaited_once_with(torrent.MAX_METADATA_BYTES)
    engine.assert_not_called()
    remote.download.assert_not_awaited()


async def test_child_bounds_sanitized_output_before_parent_reads_it(tmp_path):
    # One byte string fits its own budget; dictionary framing pushes info over
    # the independent sanitized-output limit.
    source = metadata()
    pieces = torrent.MAX_SANITIZED_BYTES // 20
    info = cast(dict[bytes, object], source[b"info"])
    info[b"pieces"] = b"x" * (pieces * 20)
    info[b"length"] = pieces
    info[b"piece length"] = 1
    raw = tmp_path / "source.torrent"
    raw.write_bytes(torrent._encode(source))
    output = tmp_path / "sanitized.torrent"
    with pytest.raises(torrent.TorrentError, match="Sanitized torrent metadata"):
        await torrent._parse_metadata(raw, output)
    assert not output.exists()
