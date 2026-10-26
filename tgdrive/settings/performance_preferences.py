"""Durable operator-owned resource policies, applied without a restart."""

from __future__ import annotations

import asyncio
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tgdrive.settings.managed_settings import SettingsProblem


class PerformancePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str = "Asia/Kolkata"
    cache_memory_bytes: int = Field(default=256 * 1024**2, ge=0, le=8 * 1024**3)
    cache_disk_bytes: int = Field(default=10 * 1024**3, ge=0, le=2 * 1024**4)
    cache_min_free_bytes: int = Field(default=1024**3, ge=0, le=2 * 1024**4)
    cache_ttl_seconds: int = Field(default=86400, ge=60, le=2678400)
    auto_preload: bool = True
    preload_max_jobs: int = Field(default=2, ge=1, le=8)
    remote_download_concurrency: int = Field(default=2, ge=1, le=8)
    telegram_upload_pipeline: int = Field(default=4, ge=1, le=16)
    telegram_request_limit: int = Field(default=8, ge=1, le=32)
    telegram_parts_per_second: int = Field(default=48, ge=1, le=128)
    telegram_media_per_bot: int = Field(default=3, ge=1, le=16)
    telegram_max_queue: int = Field(default=256, ge=1, le=4096)
    upload_split_mib: int = Field(default=2000, ge=0, le=2000)
    max_file_size: int = Field(default=16 * 1024**3, ge=1, le=2 * 1024**4)
    staging_budget_bytes: int = Field(default=16 * 1024**3, ge=1, le=2 * 1024**4)
    min_free_bytes: int = Field(default=1024**3, ge=0, le=2 * 1024**4)

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
        from tgdrive.media.media_policy import MediaCachePolicy

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
        initial["max_file_size"] = (
            settings.max_file_size or settings.staging_budget_bytes
        )
        self.value = PerformancePreferences.model_validate(initial)
        self._lock = asyncio.Lock()
        self._loaded = False

    def _read(self):
        backend = self.runtime.mongo
        row = backend.db.settings.find_one({"_id": "performance-preferences"})
        raw = row["document"] if row else None
        return (
            PerformancePreferences.model_validate(self.value.model_dump() | raw)
            if raw is not None
            else self.value
        )

    def _write(self, value):
        document = value.model_dump()
        backend = self.runtime.mongo
        backend.write(
            backend.db.settings,
            "replace_one",
            {"_id": "performance-preferences"},
            {"_id": "performance-preferences", "document": document},
            upsert=True,
        )

    async def _apply(self, value):
        for key, item in value.model_dump().items():
            setattr(self.settings, key, item)
        manager = getattr(self.runtime, "transfers", None)
        remote = getattr(manager, "remote", None) or getattr(
            self.runtime, "remote", None
        )
        if remote is not None:
            remote.max_bytes = self.settings.upload_limit
            remote.min_free_bytes = value.min_free_bytes
            scheduler = getattr(remote, "scheduler", None)
            if scheduler is not None:
                await scheduler.configure(value.remote_download_concurrency)
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
        from tgdrive.media.media_reader import MediaReadCache
        from tgdrive.media.preloading import PreloadManager

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
                        "check MongoDB access and saved settings, then retry.",
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
            "defaults": PerformancePreferences().model_dump(),
            "documentation_url": "https://github.com/TechShreyash/TGDrive/blob/main/docs/configuration.md",
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
                if (
                    {"cache_disk_bytes", "cache_min_free_bytes"}.intersection(changes)
                    and value.cache_disk_bytes > 0
                    and value.cache_min_free_bytes < 64 * 1024**2
                ):
                    raise SettingsProblem(
                        "Keep at least 64 MiB (0.0625 GiB) free "
                        "while disk caching is enabled. "
                        "Use the 1 GiB default for normal operation.",
                        status=422,
                    )
                upload_keys = {
                    "max_file_size",
                    "staging_budget_bytes",
                    "min_free_bytes",
                }
                if upload_keys.intersection(changes):
                    if value.max_file_size > value.staging_budget_bytes:
                        raise SettingsProblem(
                            "Maximum file size cannot exceed "
                            "the temporary upload budget. "
                            "Increase the budget or lower the file limit.",
                            status=422,
                        )
                    if value.min_free_bytes < 64 * 1024**2:
                        raise SettingsProblem(
                            "Keep at least 64 MiB free for upload storage. "
                            "The recommended default is 1 GiB.",
                            status=422,
                        )
                    from tgdrive.transfers.staging import staging_usage

                    manager = getattr(self.runtime, "transfers", None)
                    reserved, _ = staging_usage(manager)
                    if value.staging_budget_bytes < reserved:
                        raise SettingsProblem(
                            "The temporary budget is below space "
                            "already reserved by uploads. "
                            "Finish or cancel transfers first.",
                            status=409,
                        )
                await asyncio.to_thread(self._write, value)
            except SettingsProblem:
                raise
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
