"""Durable operator-owned resource policies, applied without a restart."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tgdrive.managed_settings import SettingsProblem


class PerformancePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str = "Asia/Kolkata"
    cache_memory_bytes: int = Field(default=256 * 1024**2, ge=0, le=8 * 1024**3)
    cache_disk_bytes: int = Field(default=10 * 1024**3, ge=0, le=2 * 1024**4)
    cache_min_free_bytes: int = Field(default=1024**3, ge=0, le=2 * 1024**4)
    cache_ttl_seconds: int = Field(default=86400, ge=60, le=2678400)
    auto_preload: bool = True
    preload_max_jobs: int = Field(default=2, ge=1, le=8)
    telegram_upload_pipeline: int = Field(default=4, ge=1, le=16)
    telegram_request_limit: int = Field(default=8, ge=1, le=32)
    telegram_parts_per_second: int = Field(default=48, ge=1, le=128)
    telegram_media_per_bot: int = Field(default=3, ge=1, le=16)
    telegram_max_queue: int = Field(default=256, ge=1, le=4096)
    upload_split_mib: int = Field(default=2000, ge=0, le=2000)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(
                "Choose a valid IANA timezone, such as Asia/Kolkata"
            ) from error
        return value

    def cache_policy(self):
        from tgdrive.media_policy import MediaCachePolicy

        return MediaCachePolicy(
            disk_max_bytes=self.cache_disk_bytes,
            memory_max_bytes=self.cache_memory_bytes,
            min_free_bytes=self.cache_min_free_bytes,
            ttl_seconds=self.cache_ttl_seconds,
            max_background_jobs=self.preload_max_jobs,
            auto_preload=self.auto_preload,
        )


class PerformanceSettings:
    def __init__(self, settings, runtime):
        self.settings, self.runtime = settings, runtime
        initial = PerformancePreferences().model_dump()
        initial.update(
            {key: getattr(settings, key) for key in initial if hasattr(settings, key)}
        )
        self.value = PerformancePreferences.model_validate(initial)
        self._lock = asyncio.Lock()
        self._loaded = False

    @property
    def path(self) -> Path:
        return self.settings.resolved_data_dir / "performance-preferences.json"

    def _read(self):
        backend = getattr(self.runtime, "mongo", None)
        if backend is not None:
            row = backend.db.settings.find_one({"_id": "performance-preferences"})
            raw = row["document"] if row else None
        else:
            try:
                raw = json.loads(self.path.read_text())
            except FileNotFoundError:
                raw = None
        return (
            PerformancePreferences.model_validate(raw)
            if raw is not None
            else self.value
        )

    def _write(self, value):
        document = value.model_dump()
        backend = getattr(self.runtime, "mongo", None)
        if backend is not None:
            backend.write(
                backend.db.settings,
                "replace_one",
                {"_id": "performance-preferences"},
                {"_id": "performance-preferences", "document": document},
                upsert=True,
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(".performance-" + secrets.token_hex(12))
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

    async def _apply(self, value):
        for key, item in value.model_dump().items():
            setattr(self.settings, key, item)
        pool = getattr(self.runtime, "pool", None)
        if pool is None or not hasattr(pool, "__dict__"):
            return
        for method, argument in (
            ("set_media_per_bot", value.telegram_media_per_bot),
            ("set_media_queue_limit", value.telegram_max_queue),
        ):
            setter = getattr(pool, method, None)
            if callable(setter):
                await setter(argument)
        for slot in getattr(pool, "_slots", ()):
            client = getattr(slot, "client", None)
            configure = getattr(client, "configure_transport", None)
            if callable(configure):
                configure(
                    upload_pipeline=value.telegram_upload_pipeline,
                    request_limit=value.telegram_request_limit,
                    parts_per_second=value.telegram_parts_per_second,
                )
        from tgdrive.media_reader import MediaReadCache
        from tgdrive.preloading import PreloadManager

        policy = value.cache_policy()
        cache = getattr(pool, "_media_read_cache", None)
        manager = getattr(self.runtime, "preloads", None)
        if manager is None:
            if cache is None:
                directory = getattr(pool, "media_cache_dir", None)
                if directory is None:
                    directory = self.settings.resolved_data_dir / "media-cache"
                    pool.media_cache_dir = directory
                pool._media_read_cache = MediaReadCache(directory, policy)
            else:
                await cache.reconfigure(policy)
            pool._media_read_cache.deferred_writes = True
            self.runtime.preloads = PreloadManager(pool, policy=policy)
        else:
            await manager.reconfigure(policy)

    async def initialize(self):
        async with self._lock:
            if not self._loaded:
                try:
                    value = await asyncio.to_thread(self._read)
                    await self._apply(value)
                except Exception:
                    raise SettingsProblem(
                        "Could not load performance settings; "
                        "preserve the settings file and retry.",
                        status=503,
                    ) from None
                self.value, self._loaded = value, True

    def etag(self):
        raw = json.dumps(self.value.model_dump(), sort_keys=True).encode()
        return '"' + hashlib.sha256(raw).hexdigest() + '"'

    async def snapshot(self):
        await self.initialize()
        return self.value.model_dump() | {
            "revision": self.etag(),
            "documentation_url": "https://github.com/TechShreyash/TGDrive/blob/dev/docs/performance.md",
        }

    async def save(self, changes, *, expected=None):
        await self.initialize()
        async with self._lock:
            if expected is not None and expected != self.etag():
                raise SettingsProblem(
                    "Settings changed in another tab. Reload before saving.", status=409
                )
            try:
                value = PerformancePreferences.model_validate(
                    self.value.model_dump() | changes
                )
                await asyncio.to_thread(self._write, value)
            except ValueError:
                raise
            except Exception:
                raise SettingsProblem(
                    "Could not save performance settings. No changes were applied.",
                    status=503,
                ) from None
            try:
                await self._apply(value)
            except Exception:
                restored = True
                try:
                    await asyncio.to_thread(self._write, self.value)
                    await self._apply(self.value)
                except Exception:
                    restored = False
                detail = (
                    "Could not apply performance settings. "
                    "The previous policy was restored."
                    if restored
                    else "Could not apply performance settings. "
                    "Restart the service to reconcile the saved policy."
                )
                raise SettingsProblem(detail, status=503) from None
            self.value = value
        return await self.snapshot()


def get_performance_settings(settings, runtime):
    service = getattr(runtime, "performance_settings", None)
    if service is None:
        service = PerformanceSettings(settings, runtime)
        runtime.performance_settings = service
    return service
