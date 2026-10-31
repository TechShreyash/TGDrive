"""Torrent intake tests use synthetic metadata and a fake downloader."""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgdrive.transfers import torrent


def metadata(files=None, **extra):
    info = {
        b"name": b"Collection",
        b"piece length": 16,
        b"pieces": b"x" * 20,
    }
    if files is None:
        info[b"length"] = 4
    else:
        info[b"files"] = files
    info.update(extra)
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
            remote=None,
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
    manifest = await torrent.download_torrent(
        "https://files.example/source.torrent",
        tmp_path,
        remote=remote,
        cancel_event=asyncio.Event(),
        progress=AsyncMock(),
        admit=admit,
    )

    assert manifest.total_size == 4
    assert calls == ["admit", "addTorrent", "getFiles", "unpause", "stopped"]
    assert not Path(metadata_dir).exists()

    retried = await torrent.download_torrent(
        "https://files.example/expired.torrent",
        tmp_path,
        remote=remote,
        cancel_event=asyncio.Event(),
        progress=AsyncMock(),
        admit=AsyncMock(),
    )
    assert retried.info_hash == manifest.info_hash
    remote.download.assert_awaited_once()


async def test_downloader_shutdown_waits_for_process_and_closes_rpc():
    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    process.terminate = lambda: setattr(process, "returncode", -15)
    engine = torrent._Aria2(Path("unused"))
    engine.process = process
    engine.session = SimpleNamespace(close=AsyncMock())

    await engine.__aexit__(None, None, None)

    process.wait.assert_awaited_once()
    engine.session.close.assert_awaited_once()


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
        remote=remote,
        cancel_event=asyncio.Event(),
        progress=AsyncMock(),
        admit=admit,
    )
    assert options["select-file"] == "1,3"


async def test_retry_progress_counts_verified_pieces_instead_of_restarting():
    engine = torrent._Aria2(Path("/tmp"))
    states = iter(
        [
            # Re-hashing pieces kept from the failed attempt.
            {"status": "active", "completedLength": "0", "verifiedLength": "600"},
            {"status": "complete", "completedLength": "1000", "totalLength": "1000"},
        ]
    )

    async def call(_method, *_params):
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
