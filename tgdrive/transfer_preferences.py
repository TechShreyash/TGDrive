"""Durable owner upload scheduling preferences, applied without a restart."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from tgdrive.managed_settings import SettingsProblem


class UploadPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    uploads_per_bot: int = Field(ge=1, le=4)


class TransferPreferences:
    def __init__(self, settings, runtime):
        self.settings = settings
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self._loaded = False
        self._value = getattr(settings, "telegram_uploads_per_bot", 1)

    @property
    def path(self) -> Path:
        return self.settings.resolved_data_dir / "transfer-preferences.json"

    def _read(self) -> int:
        backend = getattr(self.runtime, "mongo", None)
        if backend is not None:
            row = backend.db.settings.find_one({"_id": "transfer-preferences"})
            raw = row["document"] if row else None
        else:
            try:
                raw = json.loads(self.path.read_text())
            except FileNotFoundError:
                raw = None
        return (
            UploadPreferences.model_validate(raw).uploads_per_bot
            if raw is not None
            else self._value
        )

    def _write(self, value: int) -> None:
        document = {"uploads_per_bot": value}
        backend = getattr(self.runtime, "mongo", None)
        if backend is not None:
            backend.write(
                backend.db.settings,
                "replace_one",
                {"_id": "transfer-preferences"},
                {"_id": "transfer-preferences", "document": document},
                upsert=True,
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(
            ".transfer-preferences-" + secrets.token_hex(12)
        )
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    async def initialize(self) -> None:
        async with self._lock:
            if not self._loaded:
                try:
                    value = await asyncio.to_thread(self._read)
                except Exception:
                    raise SettingsProblem(
                        "Could not read upload settings.", status=503
                    ) from None
                await self.runtime.pool.set_uploads_per_bot(value)
                self._value = value
                self._loaded = True

    async def snapshot(self) -> dict:
        await self.initialize()
        clients = await self.runtime.pool.status()
        bots = sum(
            client.kind == "bot" and client.started and client.healthy
            for client in clients
        )
        return {
            "uploads_per_bot": self._value,
            "connected_bots": bots,
            "bot_upload_slots": bots * self._value,
        }

    async def save(self, change: UploadPreferences) -> dict:
        await self.initialize()
        async with self._lock:
            try:
                await asyncio.to_thread(self._write, change.uploads_per_bot)
            except Exception:
                raise SettingsProblem(
                    "Could not save upload settings. Try again.", status=503
                ) from None
            await self.runtime.pool.set_uploads_per_bot(change.uploads_per_bot)
            self._value = change.uploads_per_bot
        return await self.snapshot()


def get_transfer_preferences(settings, runtime) -> TransferPreferences:
    service = getattr(runtime, "transfer_preferences", None)
    if service is None:
        service = TransferPreferences(settings, runtime)
        runtime.transfer_preferences = service
    return service
