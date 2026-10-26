from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from aiohttp.abc import AbstractResolver

from tgdrive.transfers.remote import (
    RemoteCancelled,
    RemoteClient,
    RemoteDestinationExists,
    RemoteError,
    RemoteTooLarge,
    TooManyRedirects,
    UnsafeRemoteURL,
    ValidatingResolver,
    safe_filename,
)


class FakeResolver(AbstractResolver):
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int, int]] = []
        self.closed = False

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        self.calls.append((host, port, family))
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
            for address in self.answers.get(host, [])
        ]

    async def close(self) -> None:
        self.closed = True


class FakeContent:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = deque(chunks or [])

    async def read(self, limit: int) -> bytes:
        await asyncio.sleep(0)
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > limit:
            self.chunks.appendleft(chunk[limit:])
            return chunk[:limit]
        return chunk


class FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.popleft()

    async def close(self) -> None:
        self.closed = True


PUBLIC_DNS = {"files.example": ["93.184.216.34"]}
NON_PUBLIC_REMOTE_ADDRESSES = (
    "100.64.0.1",
    "100.100.100.200",
    "100.127.255.254",
    "::ffff:100.100.100.200",
    "fec0::1",
)


class ConstructionTests(unittest.TestCase):
    def test_client_can_be_constructed_without_a_running_event_loop(self) -> None:
        client = RemoteClient()
        asyncio.run(client.close())


class ResolverPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_dns_answer_must_be_public(self) -> None:
        resolver = ValidatingResolver(
            FakeResolver({"mixed.example": ["93.184.216.34", "127.0.0.1"]})
        )

        with self.assertRaisesRegex(UnsafeRemoteURL, "non-public"):
            await resolver.resolve("mixed.example", 443, socket.AF_UNSPEC)

    async def test_shared_and_site_local_dns_answers_are_not_public(self) -> None:
        for address in NON_PUBLIC_REMOTE_ADDRESSES:
            with self.subTest(address=address):
                resolver = ValidatingResolver(
                    FakeResolver({"mixed.example": ["93.184.216.34", address]})
                )
                with self.assertRaisesRegex(UnsafeRemoteURL, "non-public"):
                    await resolver.resolve("mixed.example", 443, socket.AF_UNSPEC)

    async def test_explicit_allow_private_bypasses_address_policy(self) -> None:
        for address in ("127.0.0.1", *NON_PUBLIC_REMOTE_ADDRESSES):
            with self.subTest(address=address):
                session = FakeSession(
                    [
                        FakeResponse(200, headers={"Content-Length": "1"})
                        for _ in range(2)
                    ]
                )
                client = RemoteClient(
                    allow_private=True,
                    session=session,
                    resolver=FakeResolver({"internal.example": [address]}),
                )

                info = await client.inspect("http://internal.example/file.txt")
                self.assertEqual(info.size, 1)
                host = f"[{address}]" if ":" in address else address
                info = await client.inspect(f"http://{host}/file.txt")
                self.assertEqual(info.size, 1)
                self.assertEqual(len(session.calls), 2)


class RemoteInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_and_site_local_literals_are_rejected_before_request(
        self,
    ) -> None:
        session = FakeSession([])
        client = RemoteClient(session=session, resolver=FakeResolver({}))
        for address in NON_PUBLIC_REMOTE_ADDRESSES:
            host = f"[{address}]" if ":" in address else address
            with self.subTest(address=address), self.assertRaises(UnsafeRemoteURL):
                await client.inspect(f"http://{host}/file.txt")
        self.assertEqual(session.calls, [])

    async def test_rejects_non_http_and_url_userinfo_before_request(self) -> None:
        session = FakeSession([])
        client = RemoteClient(
            session=session,
            resolver=FakeResolver(PUBLIC_DNS),
        )

        for url in (
            "file:///etc/passwd",
            "ftp://files.example/a",
            "https://user:secret@files.example/a",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeRemoteURL):
                await client.inspect(url)
        self.assertEqual(session.calls, [])

    async def test_inspection_uses_identity_and_content_range_total(self) -> None:
        response = FakeResponse(
            206,
            headers={
                "Content-Range": "bytes 0-0/12",
                "Content-Disposition": 'attachment; filename="../../report.txt"',
                "Content-Type": "text/plain; charset=utf-8",
                "ETag": '"abc"',
            },
        )
        session = FakeSession([response])
        resolver = FakeResolver(PUBLIC_DNS)
        client = RemoteClient(session=session, resolver=resolver)

        info = await client.inspect("https://files.example/fallback.bin#fragment")

        self.assertEqual(info.filename, "report.txt")
        self.assertEqual(info.size, 12)
        self.assertEqual(info.content_type, "text/plain")
        self.assertEqual(info.url, "https://files.example/fallback.bin")
        self.assertTrue(response.released)
        request_headers = session.calls[0][2]["headers"]
        self.assertEqual(request_headers["Accept-Encoding"], "identity")
        self.assertEqual(request_headers["Range"], "bytes=0-0")
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertEqual(resolver.calls[0][0], "files.example")

    async def test_redirect_target_is_validated_before_following(self) -> None:
        redirect = FakeResponse(
            302,
            headers={"Location": "http://internal.example/secret"},
        )
        session = FakeSession([redirect])
        resolver = FakeResolver(
            {
                "files.example": ["93.184.216.34"],
                "internal.example": ["169.254.169.254"],
            }
        )
        client = RemoteClient(session=session, resolver=resolver)

        with self.assertRaises(UnsafeRemoteURL):
            await client.inspect("https://files.example/file")

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(redirect.released)

    async def test_redirect_cannot_reach_shared_or_site_local_dns_answers(self) -> None:
        for address in NON_PUBLIC_REMOTE_ADDRESSES:
            with self.subTest(address=address):
                redirect = FakeResponse(
                    302, headers={"Location": "http://internal.example/secret"}
                )
                session = FakeSession([redirect])
                client = RemoteClient(
                    session=session,
                    resolver=FakeResolver(
                        {**PUBLIC_DNS, "internal.example": [address]}
                    ),
                )

                with self.assertRaises(UnsafeRemoteURL):
                    await client.inspect("https://files.example/file")

                self.assertEqual(len(session.calls), 1)
                self.assertTrue(redirect.released)

    async def test_manual_redirect_limit_is_bounded(self) -> None:
        redirects = [
            FakeResponse(302, headers={"Location": "/next"}),
            FakeResponse(302, headers={"Location": "/again"}),
        ]
        client = RemoteClient(
            max_redirects=1,
            session=FakeSession(redirects),
            resolver=FakeResolver(PUBLIC_DNS),
        )

        with self.assertRaises(TooManyRedirects):
            await client.inspect("https://files.example/start")


class RemoteDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_space_checks_run_off_loop_and_reject_before_writing(self):
        response = FakeResponse(200, headers={"Content-Length": "3"}, chunks=[b"abc"])
        client = RemoteClient(
            min_free_bytes=10,
            session=FakeSession([response]),
            resolver=FakeResolver(PUBLIC_DNS),
        )
        loop_thread = threading.get_ident()

        def disk_usage(_directory):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return SimpleNamespace(free=12)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "tgdrive.transfers.remote.shutil.disk_usage", side_effect=disk_usage
                ),
                self.assertRaisesRegex(RemoteError, "Temporary storage is full"),
            ):
                await client.download("https://files.example/a", directory)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(response.released)

    async def test_successful_single_stream_download_and_progress(self) -> None:
        redirect = FakeResponse(307, headers={"Location": "/final/report.txt"})
        body = FakeResponse(
            200,
            headers={"Content-Length": "6", "Content-Type": "text/plain"},
            chunks=[b"abc", b"def"],
        )
        session = FakeSession([redirect, body])
        client = RemoteClient(
            max_bytes=10,
            chunk_size=3,
            session=session,
            resolver=FakeResolver(PUBLIC_DNS),
        )
        progress: list[tuple[int, int | None]] = []

        with tempfile.TemporaryDirectory() as directory:
            result = await client.download(
                "https://files.example/start",
                directory,
                progress=lambda current, total: progress.append((current, total)),
            )

            self.assertEqual(result.filename, "report.txt")
            self.assertEqual(result.size, 6)
            self.assertEqual(result.path.read_bytes(), b"abcdef")
            self.assertEqual(progress, [(0, 6), (3, 6), (6, 6)])
            names = await asyncio.to_thread(os.listdir, directory)
            self.assertFalse(any(name.endswith(".part") for name in names))
        self.assertTrue(redirect.released)
        self.assertTrue(body.released)

    async def test_declared_size_cap_rejects_before_writing(self) -> None:
        response = FakeResponse(200, headers={"Content-Length": "11"})
        client = RemoteClient(
            max_bytes=10,
            session=FakeSession([response]),
            resolver=FakeResolver(PUBLIC_DNS),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RemoteTooLarge):
                await client.download("https://files.example/large.bin", directory)
            self.assertEqual(await asyncio.to_thread(os.listdir, directory), [])

    async def test_streamed_size_cap_removes_partial_file(self) -> None:
        response = FakeResponse(200, chunks=[b"123456", b"789012"])
        client = RemoteClient(
            max_bytes=10,
            chunk_size=6,
            session=FakeSession([response]),
            resolver=FakeResolver(PUBLIC_DNS),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RemoteTooLarge):
                await client.download("https://files.example/unknown.bin", directory)
            self.assertEqual(await asyncio.to_thread(os.listdir, directory), [])

    async def test_cancellation_event_interrupts_and_cleans_partial(self) -> None:
        response = FakeResponse(200, chunks=[b"abc", b"def"])
        cancel = asyncio.Event()
        client = RemoteClient(
            max_bytes=10,
            chunk_size=3,
            session=FakeSession([response]),
            resolver=FakeResolver(PUBLIC_DNS),
        )

        def progress(current: int, _total: int | None) -> None:
            if current == 3:
                cancel.set()

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RemoteCancelled):
                await client.download(
                    "https://files.example/cancel.bin",
                    directory,
                    cancel_event=cancel,
                    progress=progress,
                )
            self.assertEqual(await asyncio.to_thread(os.listdir, directory), [])

    async def test_existing_destination_is_not_overwritten_by_default(self) -> None:
        response = FakeResponse(200, headers={"Content-Length": "1"}, chunks=[b"x"])
        client = RemoteClient(
            session=FakeSession([response]),
            resolver=FakeResolver(PUBLIC_DNS),
        )

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "file.bin"
            destination.write_bytes(b"original")
            with self.assertRaises(RemoteDestinationExists):
                await client.download("https://files.example/file.bin", directory)
            self.assertEqual(destination.read_bytes(), b"original")


class SafeFilenameTests(unittest.TestCase):
    def test_filename_strips_paths_controls_and_windows_devices(self) -> None:
        self.assertEqual(
            safe_filename('attachment; filename="..\\..\\safe.txt"', "https://x/a"),
            "safe.txt",
        )
        self.assertEqual(safe_filename(None, "https://x/%43ON"), "_CON")
        self.assertEqual(
            safe_filename(None, "https://x/", "application/pdf"),
            "download.pdf",
        )


if __name__ == "__main__":
    unittest.main()
