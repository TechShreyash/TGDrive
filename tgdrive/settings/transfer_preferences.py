"""Durable owner upload scheduling preferences, applied without a restart."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from tgdrive.settings.managed_settings import SettingsProblem


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

    def _read(self) -> int:
        backend = self.runtime.mongo
        row = backend.db.settings.find_one({"_id": "transfer-preferences"})
        raw = row["document"] if row else None
        return (
            UploadPreferences.model_validate(raw).uploads_per_bot
            if raw is not None
            else self._value
        )

    def _write(self, value: int) -> None:
        document = {"uploads_per_bot": value}
        backend = self.runtime.mongo
        backend.write(
            backend.db.settings,
            "replace_one",
            {"_id": "transfer-preferences"},
            {"_id": "transfer-preferences", "document": document},
            upsert=True,
        )

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
