"""Offline browser fixture using the real application, auth, guard and routes.

Run with ``uv run --group e2e uvicorn tests.support.browser_transfer_app:app``.
Telegram, database and remote transports are synthetic. No .env or real storage
is loaded.
The fixture-only control routes let browser tests advance jobs deterministically.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import struct
import tempfile
import time
import wave
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import Request, Response, UploadFile
from telethon import types
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from tests.support.memory_mongo import FixtureMongoBackend
from tgdrive.core.config import Settings
from tgdrive.core.security import SESSION_COOKIE
from tgdrive.settings.management import ConnectionManager
from tgdrive.settings.premium_login import PremiumLoginService
from tgdrive.storage import mongo as fixture_mongo
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage
from tgdrive.transfers.manager import TransferCancelled, TransferEmptyFile
from tgdrive.transfers.remote import RemoteFileInfo
from tgdrive.transfers.tasks import TransferRecord, TransferRegistry, TransferStatus
from tgdrive.web.app import create_app

PASSWORD = "offline browser test password"
CONTROL_HEADER = "tgdrive-offline-browser-fixture"


class FixtureClient:
    name = "offline-browser-storage"
    display_name = "TGDrive Test Bot"
    username = "tgdrive_fixture_bot"

    def refresh_identity(self):
        self.display_name = "TGDrive Refreshed Bot"

    is_connected = False

    def __init__(self, payloads: dict[int, bytes]) -> None:
        self.payloads = payloads

    async def start(self) -> None:
        self.is_connected = True

    async def stop(self) -> None:
        self.is_connected = False

    async def get_messages(self, channel: int, *, message_ids: int) -> Any:
        content = self.payloads.get(message_ids)
        if content is None:
            return None
        # Real immutable Telegram document identities exercise production pooled
        # RAM/disk reads. A size-only namespace bypasses that path entirely.
        native = SimpleNamespace(
            id=message_ids,
            chat_id=channel,
            sender_id=1,
            raw_text="",
            photo=None,
            file=SimpleNamespace(size=len(content), name="fixture.bin"),
            document=types.Document(
                id=message_ids + 100000,
                access_hash=2,
                file_reference=b"offline-fixture",
                date=datetime(2026, 10, 18, tzinfo=UTC),
                mime_type="application/octet-stream",
                size=len(content),
                dc_id=2,
                attributes=[],
            ),
        )
        return TelegramMessage(self, native)

    async def download_thumbnail(
        self,
        message: Any,
        *,
        max_bytes: int,
        target_size: int,
        max_dimension: int,
    ) -> bytes | None:
        # Synthetic provider-supplied covers exercise the real bounded endpoint.
        # These never invoke the file stream, PDF renderer, or video decoder.
        content = self.payloads[message.id]
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return content if len(content) <= max_bytes else None
        if content.startswith(b"%PDF"):
            kind = "document"
        elif content.startswith(b"\x1aE\xdf\xa3") or content[4:8] == b"ftyp":
            kind = "video"
        else:
            return None
        cache = getattr(self, "_thumbnail_samples", {})
        if kind not in cache:
            cache[kind] = _thumbnail_png(kind)
            self._thumbnail_samples = cache
        return cache[kind]

    def describe_media(self, message: Any) -> dict[str, Any]:
        original = {
            "id": "original",
            "label": "Original",
            "is_original": True,
            "size": len(self.payloads[message.id]),
        }
        # This source-switch fixture deliberately reuses the real MKV bytes.
        # It exercises player replacement and HTTP Range, not Telegram encoding.
        variants = (
            [
                {
                    **original,
                    "id": "fixture-same-bytes",
                    "label": "Fixture alternate source",
                    "is_original": False,
                }
            ]
            if self.payloads[message.id].startswith(b"\x1aE\xdf\xa3")
            else []
        )
        return {"original": original, "variants": variants}

    def select_media_variant(self, message: Any, quality_id: str) -> Any:
        if quality_id == "fixture-same-bytes" and self.payloads[message.id].startswith(
            b"\x1aE\xdf\xa3"
        ):
            return message
        raise ValueError("The fixture media variant is unavailable")

    async def stream_media(self, message: Any, *, limit: int, offset: int):
        content = self.payloads[message.id]
        chunk = 1024 * 1024
        for index in range(offset, offset + limit):
            yield content[index * chunk : (index + 1) * chunk]


class FixtureTransfers:
    def __init__(self, store: DriveStore, payloads: dict[int, bytes]) -> None:
        self.registry = TransferRegistry(cancel_grace_seconds=0.2)
        self.store = store
        self.payloads = payloads
        self.started: list[dict[str, Any]] = []
        self.release: dict[str, asyncio.Event] = {}
        self.fail_deletions: set[str] = set()

    async def receive_browser_upload(
        self,
        upload: UploadFile,
        destination: str,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        on_created: Callable[[TransferRecord], Awaitable[None]] | None = None,
    ) -> TransferRecord:
        self.store.get_directory(destination)
        record = await self.registry.create("saving", total=upload.size or 0)
        record.status = TransferStatus.RUNNING
        try:
            if on_created:
                await on_created(record)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer cancelled")
            content = await upload.read()
            if not content:
                raise TransferEmptyFile("Empty files cannot be uploaded to Telegram")
            if is_disconnected is not None and await is_disconnected():
                raise TransferCancelled("Transfer cancelled")
            self._start(record, upload.filename or "upload.bin", destination, content)
            return record
        finally:
            await upload.close()

    async def enqueue_upload(
        self,
        local_path: Path,
        destination: str,
        file_name: str,
        size: int,
        *,
        transfer_id: str | None = None,
        folder_upload: dict[str, Any] | None = None,
    ) -> TransferRecord:
        record = await self.registry.create(
            "queued", total=size, transfer_id=transfer_id
        )
        if record.task is not None or record.terminal:
            return record
        record.context.update(kind="upload", name=file_name, destination=destination)
        if folder_upload is not None:
            record.context["folder_upload"] = dict(folder_upload)
        self._start(record, file_name, destination, local_path.read_bytes())
        return record

    async def cancel_job(self, job_id: str) -> TransferRecord:
        return await self.registry.cancel(job_id)

    async def start_deletion(self, path: str) -> TransferRecord:
        intent = self.store.begin_deletion(path)
        if intent["state"] == "failed":
            self.store.confirm_deletion_messages(intent["id"], [])
        record = await self.registry.create(
            "deleting", total=len(intent["message_ids"]), transfer_id=intent["id"]
        )
        record.context.update(
            kind="delete",
            name=self.store.get_directory(path, include_trashed=True)["name"],
            path=path,
        )
        self.release[record.id] = asyncio.Event()

        async def delete(item: TransferRecord) -> None:
            await self.release[item.id].wait()
            if item.id in self.fail_deletions:
                self.fail_deletions.remove(item.id)
                self.store.fail_deletion(
                    intent["id"], "Synthetic catalog deletion failed"
                )
                raise RuntimeError("Synthetic catalog deletion failed")
            self.store.finish_deletion(intent["id"])
            (
                await self.registry.update(
                    item.id, status=TransferStatus.COMPLETED, phase="completed"
                )
            )

        self.registry.spawn(record.id, delete)
        return record

    async def start_deletions(self, paths: list[str]) -> list[TransferRecord]:
        return [await self.start_deletion(path) for path in paths]

    async def retry_job(self, job_id: str) -> TransferRecord:
        record = self.registry.get(job_id)
        if record.context.get("showcase"):
            (await self.registry.retry(job_id))
            self._start(
                record,
                record.context["name"],
                record.context["destination"],
                b"Demo retry result",
            )

            async def finish():
                await asyncio.sleep(2)
                self.release[record.id].set()

            self.showcase_finish_tasks = getattr(self, "showcase_finish_tasks", set())
            task = asyncio.create_task(finish())
            self.showcase_finish_tasks.add(task)
            task.add_done_callback(self.showcase_finish_tasks.discard)
            return record
        if record.context.get("kind") != "delete":
            raise ValueError("Only fixture deletion retries are supported")
        (await self.registry.retry(job_id))
        return await self.start_deletion(record.context["path"])

    async def inspect_remote(self, url: str) -> RemoteFileInfo:
        await asyncio.sleep(0.05)
        return RemoteFileInfo(
            url=url,
            filename=unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "remote.bin",
            size=12,
            content_type="application/octet-stream",
            etag=None,
        )

    async def start_remote(
        self, *, url: str, destination: str, filename: str | None
    ) -> TransferRecord:
        self.store.get_directory(destination)
        record = await self.registry.create("downloading", total=12)
        name = filename or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        self._start(record, name, destination, b"remote-bytes")
        return record

    def _start(
        self, record: TransferRecord, name: str, destination: str, content: bytes
    ) -> None:
        self.release[record.id] = asyncio.Event()
        self.started.append({"id": record.id, "name": name, "destination": destination})
        self.registry.spawn(
            record.id,
            lambda transfer: self._simulate(transfer, name, destination, content),
        )

    async def _simulate(
        self, record: TransferRecord, name: str, destination: str, content: bytes
    ) -> None:
        total = len(content)
        for numerator in (0, 1, 2, 3):
            if record.cancel_event.is_set():
                return
            (
                await self.registry.update(
                    record.id,
                    status=TransferStatus.RUNNING,
                    phase="uploading",
                    current=total * numerator // 4,
                    total=total,
                )
            )
            await asyncio.sleep(0.15)
        released = asyncio.create_task(self.release[record.id].wait())
        cancelled = asyncio.create_task(record.cancel_event.wait())
        try:
            await asyncio.wait(
                {released, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            released.cancel()
            cancelled.cancel()
            await asyncio.gather(released, cancelled, return_exceptions=True)
        if record.cancel_event.is_set():
            return
        (
            await self.registry.update(
                record.id, phase="finalizing", current=total, total=total
            )
        )
        await asyncio.sleep(0.15)
        if record.cancel_event.is_set():
            return
        message_id = max(self.payloads, default=100) + 1
        self.payloads[message_id] = content
        self.store.add_file(
            destination, name, message_id, total, operation_id=record.id
        )
        (
            await self.registry.update(
                record.id,
                status=TransferStatus.COMPLETED,
                phase="completed",
                current=total,
                total=total,
            )
        )

    async def delete_telegram_messages(self, message_ids: list[int]) -> None:
        for message_id in message_ids:
            self.payloads.pop(message_id, None)


def _preview_pdf(page_count: int = 3) -> bytes:
    """Real portrait pages for continuous scrolling and bounded PDF rendering."""
    font_id = page_count + 3
    content_start = font_id + 1
    kids = " ".join(f"{number + 3} 0 R" for number in range(page_count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode(),
    ]
    for number in range(page_count):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 594] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_start + number} 0 R >>"
            ).encode()
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for number in range(1, page_count + 1):
        stream = (
            f"BT /F1 22 Tf 24 540 Td (TGDrive PDF preview page {number}) Tj ET".encode()
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    result = b"%PDF-1.4\n"
    offsets = [0]
    for identity, body in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(identity).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(result)
    size = len(objects) + 1
    result += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        result
        + f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
    )


def _large_preview_pdf(
    page_count: int = 48, *, shared_resources=False, filtered=False
) -> bytes:
    """A real 36 MiB image document, with distinct uncompressed page images."""
    result = io.BytesIO()
    result.write(b"%PDF-1.4\n")
    offsets = [0]
    font_id = 3 + page_count * 3
    resources_id = font_id + 1
    mask_id = resources_id + int(shared_resources)

    def obj(identity, body):
        offsets.append(result.tell())
        result.write(f"{identity} 0 obj\n".encode() + body + b"\nendobj\n")

    kids = " ".join(f"{3 + number * 3} 0 R" for number in range(page_count))
    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    for number in range(page_count):
        identity = 3 + number * 3
        obj(
            identity,
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 594] "
                + (
                    f"/Resources {resources_id} 0 R "
                    if shared_resources
                    else f"/Resources << /Font << /F1 {font_id} 0 R >> "
                    f"/XObject << /Im{number} {identity + 1} 0 R >> >> "
                )
                + f"/Contents {identity + 2} 0 R >>"
            ).encode(),
        )
        pixels = bytes(
            channel
            for y in range(512)
            for x in range(512)
            for channel in ((x + number * 7) % 256, y % 256, (x + y + number * 3) % 256)
        )
        if filtered:
            import zlib

            pixels = zlib.compress(pixels, level=0)
        filter_clause = "/Filter /FlateDecode " if filtered else ""
        mask_clause = f"/SMask {mask_id} 0 R " if filtered and number == 0 else ""
        obj(
            identity + 1,
            (
                f"<< /Type /XObject /Subtype /Image /Width 512 /Height 512 "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                f"{filter_clause}{mask_clause}"
                f"/Length {len(pixels)} >>\nstream\n"
            ).encode()
            + pixels
            + b"\nendstream",
        )
        content = (
            f"q 360 0 0 450 30 80 cm /Im{number} Do Q "
            f"BT /F1 18 Tf 24 550 Td (TGDrive large PDF page {number + 1}) Tj ET"
        ).encode()
        obj(
            identity + 2,
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream",
        )
    obj(font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if shared_resources:
        images = " ".join(f"/Im{i} {4 + i * 3} 0 R" for i in range(page_count))
        obj(
            resources_id,
            f"<< /Font << /F1 {font_id} 0 R >> /XObject << {images} >> >>".encode(),
        )
    if filtered:
        mask = bytes([200]) * (512 * 512)
        obj(
            mask_id,
            (
                "<< /Type /XObject /Subtype /Image /Width 512 /Height 512 "
                f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length {len(mask)} "
                ">>\nstream\n"
            ).encode()
            + mask
            + b"\nendstream",
        )
    start = result.tell()
    result.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.write(f"{offset:010d} 00000 n \n".encode())
    result.write(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF"
        ).encode()
    )
    return result.getvalue()


def _preview_png() -> bytes:
    """A generated RGB image without an external image or font dependency."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    pixels = b"".join(b"\x00" + bytes([40 + y * 5, 90, 150]) * 16 for y in range(16))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _thumbnail_png(kind: str) -> bytes:
    """Illustrative local-demo provider covers, small enough for a file tile."""
    width, height = 240, 144

    def pixel(x, y):
        if kind == "document":
            if 72 < x < 168 and 12 < y < 132:
                if 84 < x < 154 and (y - 38) % 15 < 3 and 38 < y < 112:
                    return (80, 104, 140)
                return (245, 247, 250)
            return (214, 225, 239)
        if y < 80:
            return (48 + y, 108 + y // 2, 169 + y // 3)
        if y > 138 - x // 3:
            return (45, 107, 86)
        return (78, 144, 116)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    pixels = b"".join(
        b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width))
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _preview_wav() -> bytes:
    """A short locally generated PCM tone for native audio controls."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(
            b"".join(
                struct.pack("<h", round(4000 * math.sin(index * math.tau * 220 / 8000)))
                for index in range(2000)
            )
        )
    return stream.getvalue()


class FixtureBackup:
    """Checkpoint transport state for owner UI tests; never contacts Telegram."""

    def __init__(self, store: DriveStore) -> None:
        self.protected_revision = store.revision
        self.error: str | None = None
        self.state_override: str | None = None
        self.fail_next = False
        self.calls = 0

    def history(self) -> list[dict[str, Any]]:
        return []

    def protection_status(self, store: DriveStore) -> dict[str, Any]:
        status = {
            "state": self.state_override
            or (
                "error"
                if self.error
                else "protected"
                if self.protected_revision >= store.revision
                else "pending"
            ),
            "metadata_revision": store.revision,
            "protected_revision": self.protected_revision,
            "checkpoint_message_id": 9000 + self.calls,
            "last_success_at": "2026-10-16T00:00:00+00:00",
        }
        if self.error:
            status["error"] = self.error
        return status

    async def ensure_revision(self, store: DriveStore, required_revision: int) -> int:
        self.calls += 1
        await asyncio.sleep(0.05)
        self.state_override = None
        if self.fail_next:
            self.fail_next = False
            self.error = "Synthetic Telegram checkpoint confirmation failed."
            raise RuntimeError(self.error)
        self.error = None
        self.protected_revision = max(store.revision, required_revision)
        return 9000 + self.calls

    async def snapshot_now(self, store: DriveStore) -> int:
        return await self.ensure_revision(store, store.revision)

    def set_scenario(self, scenario: str, store: DriveStore) -> None:
        self.error = None
        self.state_override = None
        self.fail_next = False
        self.protected_revision = store.revision
        if scenario in {"pending", "error", "stale", "fail-next"}:
            self.protected_revision = max(0, store.revision - 1)
        if scenario == "error":
            self.error = "Synthetic Telegram checkpoint confirmation failed."
        elif scenario == "stale":
            self.state_override = "protected"
        elif scenario == "fail-next":
            self.fail_next = True
        elif scenario not in {"protected", "pending"}:
            raise ValueError("Unknown fixture backup scenario")


class FixtureLoginClient:
    """Synthetic Telegram login transport; never connects to a real account."""

    def __init__(self) -> None:
        self.session = SimpleNamespace(
            save=lambda: "synthetic-premium-session-for-browser-tests"
        )

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def log_out(self) -> None:
        pass

    async def send_code_request(self, _phone: str) -> Any:
        return SimpleNamespace(phone_code_hash="synthetic-browser-code-hash")

    async def sign_in(self, *, code=None, password=None, **_kwargs) -> None:
        if code is not None:
            if code != "12345":
                raise PhoneCodeInvalidError(request=None)
            raise SessionPasswordNeededError(request=None)
        if password != "synthetic-two-step-password":
            raise PasswordHashInvalidError(request=None)

    async def get_me(self) -> Any:
        return SimpleNamespace(id=987654321, bot=False, premium=True)


async def fixture_verify_connection(_api_id, _api_hash, row, _channel):
    """Keep the real private persistence/API; replace only Telegram preflight."""
    if row.kind == "user":
        return "synthetic-premium-account"
    prefix = row.credential.get_secret_value().split(":", 1)[0]
    return (
        "offline-browser-storage" if prefix == "123456" else "synthetic-bot-" + prefix
    )


class FixtureReplica:
    """Deterministic offline contract; no Telegram access or durable secrets."""

    def __init__(self, owner):
        self.owner = owner
        self.control = asyncio.Lock()
        self._operation = asyncio.Lock()
        self._temporary = tempfile.TemporaryDirectory(
            prefix="tgdrive-recovery-fixture-"
        )
        self.directory = Path(self._temporary.name)
        self.state = {
            "enabled": False,
            "configured": False,
            "recovery_only": False,
            "phase": "disabled",
            "source_channel": -1001234567890,
            "backup_channel": None,
            "revision": 9,
            "protected_revision": 0,
            "pending_revision": 9,
            "last_success_at": None,
            "current_file": "",
            "total_files": 0,
            "done_files": 0,
            "total_parts": 0,
            "done_parts": 0,
            "total_bytes": 0,
            "done_bytes": 0,
            "error": None,
            "report": [],
            "backup_file_available": False,
            "file_restore_allowed": True,
        }
        self.destination = None
        self.catalog_only = False
        self.events = []
        self.main_unavailable = False
        self.access_revoked = False
        self.journal = SimpleNamespace(
            get=lambda name, default=None: {
                "restore_point": {"revision": 7, "message_id": 9001},
                "config": {
                    "channel": self.state["backup_channel"],
                    "enabled": self.state["enabled"],
                }
                if self.state["configured"]
                else {},
                "candidate": {
                    "complete": self.state["recovery_candidate"]["complete"],
                    "catalog": {
                        "storage_channel_id": self.state["recovery_candidate"][
                            "channel"
                        ]
                    },
                }
                if self.state.get("recovery_candidate")
                else None,
            }.get(name, default)
        )

    def status(self):
        import copy

        result = copy.deepcopy(self.state)
        result["recovery_only"] = self.owner.recovery_only
        return result

    def key(self):
        return "c" * 64

    def recovery_kit(self):
        return {
            "format": "tgdrive-replica-key-v1",
            "key": self.key(),
            "restore_point": self.journal.get("restore_point"),
            "next_backup_key": self.key(),
            "mongodb_backup_key": None,
            "instructions": "Synthetic key fixture. Never use it for a real drive.",
        }

    def start(self):
        pass

    async def close(self):
        self._temporary.cleanup()

    def recovery_file(self):
        if not self.state["backup_file_available"]:
            from tgdrive.settings.managed_settings import SettingsProblem

            raise SettingsProblem("Create and confirm a backup first.", status=404)
        return b"synthetic encrypted recovery file"

    async def inspect_file(self, path, key, source=None, token=None):
        if (
            key != self.key()
            or path.read_bytes() != b"synthetic encrypted recovery file"
        ):
            raise ValueError("Recovery key or file validation failed; nothing changed")
        if self.access_revoked and not token:
            raise ValueError(
                "No recorded channel could be accessed. "
                "Supply a working replacement bot token."
            )
        channel = source or (
            -1003333333333 if self.main_unavailable else self.state["source_channel"]
        )
        await self.inspect_recovery(channel, token, key, None)
        self.state["recovery_candidate"].update(
            has_database=True,
            source_is_backup=channel != self.state["source_channel"],
        )
        self.events.append(
            {
                "action": "inspect_file",
                "channel": channel,
                "replacement_access": bool(token),
            }
        )
        return self.status()

    async def _sync(self, config, *, force_snapshot=False):
        self.events.append({"action": "checkpoint", "forced": force_snapshot})
        self.scenario("protected")

    async def before_mutation(self):
        if self.blocked:
            from tgdrive.settings.managed_settings import SettingsProblem

            raise SettingsProblem("Recovery is in progress.", status=409)

    @property
    def blocked(self):
        return self.state["phase"] in {
            "recovering",
            "ready_to_activate",
            "restart_required",
        }

    def event(self, message):
        self.state["report"].append(
            {
                "at": "2026-10-22T05:00:00Z",
                "level": "info",
                "message": message,
            }
        )
        self.state["report"] = self.state["report"][-100:]

    async def configure(self, channel, token, independent):
        if not independent or channel == self.state["source_channel"]:
            raise ValueError("Use separate private channels and owner accounts.")
        self.events.append({"action": "configure", "channel": channel})
        self.state.update(
            enabled=True,
            configured=True,
            backup_channel=channel,
            phase="syncing",
            current_file="Project archive.zip",
            total_files=3,
            done_files=1,
            total_parts=4,
            done_parts=1,
            total_bytes=3 * 1024**2,
            done_bytes=1024**2,
        )
        self.event("Private channels verified. Initial synchronization started.")
        return self.status()

    async def action(self, action):
        self.events.append({"action": action})
        if action == "disable":
            self.state.update(enabled=False, phase="disabled")
        elif action == "pause":
            self.state["phase"] = "paused"
        elif action in {"resume", "retry", "sync"}:
            self.state.update(
                enabled=True,
                phase="recovering" if self.destination else "syncing",
                error=None,
            )
        else:
            raise ValueError("Unknown backup action")
        self.event("Operation " + action + " requested.")
        return self.status()

    async def inspect_recovery(self, channel, token, key, message_id):
        self.events.append(
            {"action": "inspect", "channel": channel, "message_id": message_id}
        )
        self.state.update(
            phase="paused",
            error=None,
            recovery_candidate={
                "revision": 7,
                "files": 3,
                "bytes": 3 * 1024**2,
                "channel": channel,
                "complete": False,
                "has_database": False,
                "source_is_backup": channel != self.state["source_channel"],
            },
        )
        self.event("Recovery copy authenticated. Nothing has been activated.")
        return self.status()

    async def recover_to(
        self, target, target_token, confirmation, *, catalog_only=False
    ):
        if confirmation != str(target):
            raise ValueError("Type the destination channel ID exactly.")
        self.events.append(
            {"action": "recover", "channel": target, "catalog_only": catalog_only}
        )
        self.destination = target
        self.catalog_only = catalog_only
        self.state.update(
            phase="recovering",
            error=None,
            total_files=3,
            done_files=1,
            total_parts=4,
            done_parts=1,
            total_bytes=3 * 1024**2,
            done_bytes=1024**2,
            current_file="Project archive.zip",
        )
        self.event(
            "File verification started." if catalog_only else "Recovery copy started."
        )
        return self.status()

    def activate(self):
        if not self.state.get("recovery_candidate", {}).get("complete"):
            raise ValueError("Complete recovery first.")
        self.events.append({"action": "activate"})
        self.state["phase"] = "restart_required"
        self.event("Recovery activation saved. Restart TGDrive.")
        return self.status()

    def scenario(self, scenario):
        if scenario == "recovery-only":
            self.owner.recovery_only = True
        elif scenario == "main-unavailable":
            self.main_unavailable = True
        elif scenario == "access-revoked":
            self.access_revoked = True
        elif scenario == "file-restore-disabled":
            self.state["file_restore_allowed"] = False
        elif scenario == "error":
            self.state.update(
                phase="error",
                error={
                    "code": "CHANNEL_ACCESS",
                    "detail": "Backup bot cannot read the target channel.",
                    "action": (
                        "Restore the bot's administrator permissions, then retry."
                    ),
                    "retry_at": 0,
                },
            )
        elif scenario in {"waiting", "waiting-mongodb"}:
            mongodb = scenario == "waiting-mongodb"
            self.state.update(
                phase="waiting",
                error={
                    "code": (
                        "REPLICA_MONGODB_UNAVAILABLE" if mongodb else "FLOOD_WAIT"
                    ),
                    "detail": (
                        "The backup database is temporarily unavailable."
                        if mongodb
                        else "Telegram asked this bot to wait."
                    ),
                    "action": "TGDrive will retry after the displayed time.",
                    "retry_at": 1792645260,
                },
            )
        elif scenario == "lagging":
            self.state.update(revision=10, pending_revision=1, phase="idle")
        elif scenario == "many-events":
            for number in range(100):
                self.event(f"File verification {number + 1} confirmed.")
        elif scenario == "protected":
            self.state.update(
                phase="protected",
                error=None,
                protected_revision=9,
                pending_revision=0,
                done_files=3,
                done_parts=4,
                done_bytes=3 * 1024**2,
                last_success_at="2026-10-22T05:00:00Z",
                backup_file_available=True,
            )
        elif scenario == "recovery-ready":
            self.state["recovery_candidate"].update(
                channel=self.destination, complete=True
            )
            self.state.update(
                phase="ready_to_activate",
                done_files=3,
                done_parts=4,
                done_bytes=3 * 1024**2,
                current_file="",
                error=None,
            )
        else:
            raise ValueError("Unknown replica fixture scenario")
        return self.status()


class FixtureRuntime:
    def __init__(self) -> None:
        self.mongo = FixtureMongoBackend()
        self.store = DriveStore.create()
        self.payloads = {101: b"public content", 102: b"private content"}
        folder = self.store.create_folder("/", "資料")
        private = self.store.create_folder("/", "Private folder")
        self.folder_path = f"/{folder['id']}"
        self.private_path = f"/{private['id']}"
        shared_file = self.store.add_file(self.folder_path, "résumé #1.txt", 101, 14)
        private_file = self.store.add_file(self.private_path, "private.txt", 102, 15)
        self.shared_file_path = f"{self.folder_path}/{shared_file['id']}"
        self.private_file_path = f"{self.private_path}/{private_file['id']}"
        preview = self.store.create_folder("/", "Preview samples")
        preview_path = f"/{preview['id']}"
        self.preview_path = preview_path
        samples = {
            "sample.json": b'{"message":"<img src=x onerror=alert(1)>","count":3}',
            "reports.csv": b"name,value\nalpha,3\nbeta,4\n",
            "source.html": b"<script>window.previewExecuted=true</script>",
            "sample.pdf": _preview_pdf(),
            "long.pdf": _preview_pdf(12),
            "sample.png": _preview_png(),
            "sample.svg": (
                b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
                b"<style>rect{fill:rgb(24,160,88)}</style>"
                b'<rect width="120" height="80"/>'
                b"<script>parent.svgPreviewExecuted=true</script>"
                b'<image href="https://svg-external.invalid/track.png" '
                b'width="1" height="1"/>'
                b"</svg>"
            ),
            "sample.wav": _preview_wav(),
            "archive.bin": bytes(range(256)),
            "setup.exe": b"Synthetic fixture bytes; never an executable.",
            "empty.exe": b"",
            "installer-"
            + "very-long-file-name-" * 5
            + "setup.exe": b"Long-name fixture.",
            # Self-generated FFmpeg testsrc2 ASF containers distinguish the
            # suffix from real decoder support: MPEG4 works; MSMPEG4v3 does not
            # exist in the pinned published WASM decoder build.
            "software-mpeg4.wmv": (
                Path(__file__).resolve().parents[1] / "fixtures" / "software-mpeg4.wmv"
            ).read_bytes(),
            "unsupported-msmpeg4.wmv": (
                Path(__file__).resolve().parents[1]
                / "fixtures"
                / "unsupported-msmpeg4.wmv"
            ).read_bytes(),
            # Self-generated 8s H264/AAC MP4 with English/Hindi mov_text samples;
            # testsrc2 + sine, FFmpeg libx264/crf30 and AAC32k, no third-party media.
            # One same-length subtitle payload patch preserves literal inert markup
            # (the SRT encoder otherwise strips tags); MP4 offsets stay unchanged.
            "movtext-subtitles.mp4": (
                Path(__file__).resolve().parents[1]
                / "fixtures"
                / "movtext-subtitles.mp4"
            ).read_bytes(),
            "multitrack.mkv": (
                Path(__file__).resolve().parents[1] / "fixtures" / "multitrack.mkv"
            ).read_bytes(),
        }
        for identity, (name, content) in enumerate(samples.items(), 110):
            self.payloads[identity] = content
            self.store.add_file(preview_path, name, identity, len(content))
        self.pool = ClientPool([FixtureClient(self.payloads)])
        self.transfers = FixtureTransfers(self.store, self.payloads)
        self.backup = FixtureBackup(self.store)
        self.recovery_only = False
        self.replica = FixtureReplica(self)
        self.ready = False

    async def start(self) -> None:
        from tgdrive.settings.performance_preferences import get_performance_settings

        await get_performance_settings(settings, self).initialize()
        await self.pool.start()
        if os.environ.get("TGDRIVE_UI_SHOWCASE") == "1":
            from tests.support.showcase_data import seed

            await seed(self, settings)
            seed_reading_samples(self)
            await seed_thumbnail_samples(self)
            self.store.set_starred(self.preview_path, True)
            self.store.set_starred(self.reading_path, True)
        self.ready = True

    async def stop(self) -> None:
        self.ready = False
        task = getattr(self, "showcase_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.transfers.registry.shutdown()
        await self.preloads.close()
        await self.pool.stop(drain_timeout=1)


_temporary = tempfile.TemporaryDirectory(prefix="tgdrive-browser-fixture-")
runtime = FixtureRuntime()
settings = Settings(
    _env_file=None,
    MONGODB_URI="mongodb://synthetic.invalid",
    API_ID=12345,
    API_HASH="a" * 32,
    BOT_TOKENS="123456:" + "x" * 30,
    STORAGE_CHANNEL=-1001234567890,
    ADMIN_PASSWORD=PASSWORD,
    SESSION_SECRET="synthetic-browser-test-secret-" * 2,
    STRING_SESSIONS="",
    MAIN_BOT_TOKEN="",
    TELEGRAM_ADMIN_IDS="",
    DATA_DIR=Path(_temporary.name) / "data",
    CACHE_DIR=Path(_temporary.name) / "cache",
    COOKIE_SECURE=False,
    MAX_FILE_SIZE=(
        16 * 1024**3
        if os.environ.get("TGDRIVE_UI_SHOWCASE") == "1"
        else 32 * 1024 * 1024
    ),
)
# Register only this private fixture directory. Production's required MongoDB
# interfaces remain active, backed by the explicit in-memory test double.
fixture_mongo._directories[settings.resolved_data_dir] = runtime.mongo
fixture_mongo._backends[
    (settings.mongodb_uri.get_secret_value(), settings.mongodb_database)
] = runtime.mongo

runtime.connection_manager = ConnectionManager(
    settings, runtime, verifier=fixture_verify_connection
)
app = create_app(settings, runtime=runtime)
if os.environ.get("TGDRIVE_UI_SHOWCASE") == "1":
    from tests.support.showcase_data import install

    install(app, runtime)
app.state.premium_login_service = PremiumLoginService(
    runtime.connection_manager.login_api,
    runtime.connection_manager.add_authenticated_session,
    client_factory=lambda _api_id, _api_hash: FixtureLoginClient(),
)


def _control(request: Request) -> None:
    from fastapi import HTTPException

    if request.headers.get("x-fixture-control") != CONTROL_HEADER:
        raise HTTPException(
            403, "Fixture controls are only available to the test runner"
        )


@app.get("/__test__/state")
async def state(request: Request) -> dict[str, Any]:
    _control(request)
    return {
        "started": runtime.transfers.started,
        "records": {
            record.id: record.as_dict()
            for record in runtime.transfers.registry._records.values()
        },
        "seed": {
            "folder": runtime.folder_path,
            "private_folder": runtime.private_path,
            "file": runtime.shared_file_path,
            "private_file": runtime.private_file_path,
            "preview_folder": runtime.preview_path,
        },
    }


@app.post("/__test__/backup/{scenario}")
async def backup_scenario(request: Request, scenario: str) -> dict[str, Any]:
    _control(request)
    runtime.backup.set_scenario(scenario, runtime.store)
    return {
        "recovery": runtime.backup.protection_status(runtime.store),
        "calls": runtime.backup.calls,
    }


@app.get("/__test__/backup")
async def backup_state(request: Request) -> dict[str, Any]:
    _control(request)
    return {
        "recovery": runtime.backup.protection_status(runtime.store),
        "calls": runtime.backup.calls,
    }


@app.post("/__test__/finish/{transfer_id}")
async def finish(request: Request, transfer_id: str) -> dict[str, str]:
    _control(request)
    runtime.transfers.release[transfer_id].set()
    return {"status": "ok"}


@app.post("/__test__/expire-session")
async def expire_session(request: Request, response: Response) -> dict[str, str]:
    _control(request)
    response.set_cookie(
        SESSION_COOKIE,
        app.state.sessions.create(
            now=int(time.time()) - settings.session_ttl_seconds - 2
        ),
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"status": "ok"}


@app.post("/__test__/fail-deletion/{transfer_id}")
async def fail_deletion(request: Request, transfer_id: str) -> dict[str, str]:
    _control(request)
    record = runtime.transfers.registry.get(transfer_id)
    if record.context.get("kind") != "delete":
        raise ValueError("The fixture failure must target a deletion")
    runtime.transfers.fail_deletions.add(transfer_id)
    runtime.transfers.release[transfer_id].set()
    return {"status": "ok"}


@app.post("/__test__/pdf/large")
async def seed_large_pdf(request: Request):
    """Opt-in heavy fixture; ordinary browser tests pay no large-PDF cost."""
    _control(request)
    content = _large_preview_pdf(
        shared_resources=request.query_params.get("shared") == "1",
        filtered=request.query_params.get("filtered") == "1",
    )
    runtime.payloads[900] = content
    single = runtime.store.add_file(
        runtime.preview_path, "large-images.pdf", 900, len(content)
    )
    boundary = len(content) // 2
    parts = []
    for index, data in enumerate((content[:boundary], content[boundary:])):
        message_id = 901 + index
        runtime.payloads[message_id] = data
        parts.append(
            {
                "index": index,
                "offset": 0 if index == 0 else boundary,
                "size": len(data),
                "message_id": message_id,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    split = runtime.store.add_file(
        runtime.preview_path, "split-images.pdf", 901, len(content), parts=parts
    )
    return {
        "size": len(content),
        "pages": 48,
        "single": runtime.preview_path + "/" + single["id"],
        "split": runtime.preview_path + "/" + split["id"],
    }


@app.post("/__test__/pdf/features")
async def seed_pdf_features(request: Request):
    """Tiny opt-in PDFs exercise password prompts and local rendering resources."""
    from tests.support.book_samples import encrypted_pdf, resource_pdf

    _control(request)
    font_data = (
        Path(__file__).resolve().parents[2]
        / "website/node_modules/pdfjs-dist/standard_fonts/LiberationSans-Regular.ttf"
    ).read_bytes()
    samples = {
        "password-protected.pdf": encrypted_pdf(),
        "local-resources.pdf": resource_pdf(font_data),
    }
    paths = {}
    for message_id, (name, content) in enumerate(samples.items(), 930):
        runtime.payloads[message_id] = content
        item = runtime.store.add_file(
            runtime.preview_path, name, message_id, len(content)
        )
        paths[name] = runtime.preview_path + "/" + item["id"]
    return {"files": paths}


def seed_reading_samples(target):
    from tests.support.book_samples import cbr, cbz, epub, manga_epub

    if hasattr(target, "reading_path"):
        return target.reading_path
    folder = target.store.create_folder("/", "Reading samples")
    target.reading_path = "/" + folder["id"]
    for message_id, (name, data) in enumerate(
        {
            "The little journey.epub": epub(unsafe=True),
            "Sample comic.cbz": cbz(),
            "Sample manga.cbr": cbr(),
            "Image-based manga.epub": manga_epub(),
        }.items(),
        980,
    ):
        target.payloads[message_id] = data
        target.store.add_file(target.reading_path, name, message_id, len(data))
    if os.environ.get("TGDRIVE_UI_SHOWCASE") == "1":
        from datetime import timedelta

        from tgdrive.web.share_links import token_factory

        for label, days in (("Book club", 30), ("Review copy", 7)):
            target.store.create_share(
                target.reading_path,
                label=label,
                expires_at=(datetime.now(UTC) + timedelta(days=days)).isoformat(),
                token_factory=token_factory(settings),
            )
    return target.reading_path


@app.post("/__test__/books")
async def seed_books(request: Request):
    _control(request)
    return {"path": seed_reading_samples(runtime)}


@app.post("/__test__/books/large-page")
async def seed_large_book_page(request: Request):
    """Opt-in encoded-page limit fixture; original PNG stays byte-for-byte intact."""
    _control(request)
    import struct
    import zlib
    from zipfile import ZipFile

    from tests.support.book_samples import png

    small = png(2)
    # A legal private ancillary chunk keeps decoded pixels small while exercising
    # a genuinely large original PNG through parsing, HTTP, CSP and image decode.
    payload = b"p" * (39 * 1024**2)
    chunk = b"tgDr" + payload
    image = (
        small[:-12]
        + struct.pack(">I", len(payload))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + small[-12:]
    )
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("unused.bin", b"x" * (48 * 1024**2))
        archive.writestr("page.png", image)
    data = output.getvalue()
    folder = seed_reading_samples(runtime)
    runtime.payloads[985] = data
    item = runtime.store.add_file(folder, "Large original page.cbz", 985, len(data))
    return {
        "path": folder,
        "file": folder + "/" + item["id"],
        "image_size": len(image),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "size": len(data),
    }


async def seed_thumbnail_samples(target):
    from tests.support.book_samples import png
    from tgdrive.media.generated_thumbnails import generate, save

    if hasattr(target, "thumbnail_sample_path"):
        return target.thumbnail_sample_path
    folder = target.store.create_folder("/", "Thumbnail samples")
    target.thumbnail_sample_path = "/" + folder["id"]
    samples = {
        "Illustration.png": png(2),
        "Getting started.pdf": _preview_pdf(),
        "Walkthrough.mp4": (
            Path(__file__).resolve().parents[1] / "fixtures" / "movtext-subtitles.mp4"
        ).read_bytes(),
    }
    for message_id, (name, data) in enumerate(samples.items(), 990):
        target.payloads[message_id] = data
        target.store.add_file(target.thumbnail_sample_path, name, message_id, len(data))
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory) / name
            staged.write_bytes(data)
            cover = await generate(staged, name)
        if cover:
            save(
                settings.resolved_cache_dir / "thumbnails",
                settings.storage_channel,
                message_id,
                cover,
            )
    return target.thumbnail_sample_path


@app.post("/__test__/thumbnails/generated")
async def seed_generated_thumbnails(request: Request):
    _control(request)
    return {"path": await seed_thumbnail_samples(runtime)}


@app.post("/__test__/replica/{scenario}")
async def replica_scenario(request: Request, scenario: str):
    _control(request)
    return runtime.replica.scenario(scenario)


@app.get("/__test__/replica")
async def replica_fixture_state(request: Request):
    _control(request)
    return {"state": runtime.replica.status(), "events": runtime.replica.events}
