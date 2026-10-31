"""Owner choices (timezone, preloading, split size) plus live-applied
resource values. The resource values are chosen by the governor
(tgdrive/core/governor.py) and are never saved or edited by the owner."""

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
    cache_memory_bytes: int = Field(default=256 * 1024**2, ge=0)
    cache_disk_bytes: int = Field(default=10 * 1024**3, ge=0)
    cache_min_free_bytes: int = Field(default=1024**3, ge=0)
    cache_ttl_seconds: int = Field(default=86400, ge=60)
    auto_preload: bool = True
    preload_max_jobs: int = Field(default=2, ge=1)
    remote_download_concurrency: int = Field(default=2, ge=1)
    telegram_upload_pipeline: int = Field(default=4, ge=1)
    telegram_request_limit: int = Field(default=8, ge=1)
    telegram_parts_per_second: int = Field(default=48, ge=1)
    telegram_media_per_bot: int = Field(default=3, ge=1)
    telegram_max_queue: int = Field(default=256, ge=1)
    upload_split_mib: int = Field(default=2000, ge=0, le=2000)
    max_file_size: int = Field(default=16 * 1024**3, ge=1)
    staging_budget_bytes: int = Field(default=16 * 1024**3, ge=1)
    min_free_bytes: int = Field(default=1024**3, ge=0)

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
        if raw is not None:
            from tgdrive.core.governor import MANAGED

            # Resource values are the governor's; ignore any saved copies.
            raw = {k: v for k, v in raw.items() if k not in MANAGED}
        return (
            PerformancePreferences.model_validate(self.value.model_dump() | raw)
            if raw is not None
            else self.value
        )

    def _write(self, value):
        from tgdrive.core.governor import MANAGED

        document = {k: v for k, v in value.model_dump().items() if k not in MANAGED}
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
        from tgdrive.core.governor import MANAGED

        return {
            key: value
            for key, value in self.value.model_dump().items()
            if key not in MANAGED
        } | {
            "revision": self.etag(),
            "defaults": {
                key: value
                for key, value in PerformancePreferences().model_dump().items()
                if key not in MANAGED
            },
            "documentation_url": "https://github.com/TechShreyash/TGDrive/blob/main/docs/configuration.md",
        }

    async def save(self, changes, *, expected=None):
        from tgdrive.core.governor import MANAGED

        if MANAGED.intersection(changes):
            raise SettingsProblem(
                "TGDrive tunes cache, storage and transfer limits automatically "
                "from server resources. To cap TGDrive, set Docker memory and "
                "CPU limits.",
                status=422,
            )
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
