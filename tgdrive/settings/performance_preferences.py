"""Saved owner policies and ceilings, plus unsaved governor resource budgets."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.transfers.tasks import _journalled


class PerformancePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str = "Asia/Kolkata"
    cache_disk_limit_bytes: int | None = Field(default=None, ge=0, le=2**53 - 1)
    cache_memory_limit_bytes: int | None = Field(default=None, ge=0, le=2**53 - 1)
    staging_limit_bytes: int | None = Field(default=None, gt=0, le=2**53 - 1)
    stream_memory_limit_bytes: int | None = Field(
        default=None, ge=2 * 1024**2, le=2**53 - 1
    )
    protected_free_bytes: int | None = Field(default=None, ge=0, le=2**53 - 1)
    cache_memory_bytes: int = Field(default=256 * 1024**2, ge=0)
    cache_disk_bytes: int = Field(default=10 * 1024**3, ge=0)
    cache_min_free_bytes: int = Field(default=1024**3, ge=0)
    cache_ttl_seconds: int = Field(default=86400, ge=60, le=2**53 - 1)
    auto_preload: bool = True
    preload_max_jobs: int = Field(default=2, ge=1)
    remote_download_concurrency: int = Field(default=2, ge=1)
    telegram_upload_pipeline: int = Field(default=4, ge=1)
    telegram_request_limit: int = Field(default=8, ge=1)
    telegram_parts_per_second: int = Field(default=48, ge=1)
    telegram_media_per_bot: int = Field(default=3, ge=1)
    telegram_max_queue: int = Field(default=256, ge=1)
    upload_split_mib: int = Field(default=2000, ge=0, le=2000)
    upload_limit_bytes_per_second: int | None = Field(
        default=None, ge=1024, le=2**53 - 1
    )
    download_limit_bytes_per_second: int | None = Field(
        default=None, ge=1024, le=2**53 - 1
    )
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
        from tgdrive.media.media_policy import (
            AUTO_STREAM_MEMORY_LIMIT_BYTES,
            MediaCachePolicy,
        )

        return MediaCachePolicy(
            disk_max_bytes=self.cache_disk_bytes,
            memory_max_bytes=self.cache_memory_bytes,
            min_free_bytes=self.cache_min_free_bytes,
            ttl_seconds=self.cache_ttl_seconds,
            max_background_jobs=self.preload_max_jobs,
            auto_preload=self.auto_preload,
            scratch_max_bytes=self.stream_memory_limit_bytes
            if self.stream_memory_limit_bytes is not None
            else AUTO_STREAM_MEMORY_LIMIT_BYTES,
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
        from tgdrive.core.governor import constrain, measure

        values = value.model_dump()
        measurements = None
        if value.protected_free_bytes != self.value.protected_free_bytes and (
            value.protected_free_bytes is None
        ):
            pool = getattr(self.runtime, "pool", None)
            media = getattr(pool, "media_cache_dir", None) or (
                self.settings.resolved_data_dir / "media-cache"
            )
            directory = self.settings.resolved_cache_dir
            while not directory.exists():
                directory = directory.parent
            measurements = await asyncio.to_thread(measure, directory, media)
        values = constrain(values, values, measurements)
        value = PerformancePreferences.model_validate(values)
        changed = {
            key
            for key, item in values.items()
            if getattr(self.settings, key, None) != item
            or getattr(self.value, key) != item
        }
        for key, item in values.items():
            setattr(self.settings, key, item)
        bandwidth = getattr(self.runtime, "bandwidth", None)
        if bandwidth is not None:
            await bandwidth.configure(
                value.upload_limit_bytes_per_second,
                value.download_limit_bytes_per_second,
            )
        manager = getattr(self.runtime, "transfers", None)
        remote = getattr(manager, "remote", None) or getattr(
            self.runtime, "remote", None
        )
        if remote is not None:
            remote.max_bytes = self.settings.upload_limit
            remote.min_free_bytes = value.min_free_bytes
            scheduler = getattr(remote, "scheduler", None)
            if scheduler is not None and (
                not self._loaded or "remote_download_concurrency" in changed
            ):
                await scheduler.configure(value.remote_download_concurrency)
        pool = getattr(self.runtime, "pool", None)
        if pool is None or not hasattr(pool, "__dict__"):
            self.value = value
            return
        for method, key in (
            ("set_media_per_bot", "telegram_media_per_bot"),
            ("set_media_queue_limit", "telegram_max_queue"),
        ):
            setter = getattr(pool, method, None)
            if callable(setter) and (not self._loaded or key in changed):
                pending = setter(getattr(value, key))
                if not inspect.isawaitable(pending):
                    raise TypeError("Pool setters must be asynchronous")
                await pending
        if not self._loaded or changed.intersection(
            {
                "telegram_upload_pipeline",
                "telegram_request_limit",
                "telegram_parts_per_second",
            }
        ):
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
        elif not self._loaded or changed.intersection(
            {
                "cache_memory_bytes",
                "cache_disk_bytes",
                "cache_min_free_bytes",
                "cache_ttl_seconds",
                "stream_memory_limit_bytes",
                "preload_max_jobs",
                "auto_preload",
            }
        ):
            await manager.reconfigure(policy)
        self.value = value

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
                self._loaded = True

    def etag(self):
        from tgdrive.core.governor import MANAGED

        raw = json.dumps(
            {
                key: value
                for key, value in self.value.model_dump().items()
                if key not in MANAGED
            },
            sort_keys=True,
        ).encode()
        return '"' + hashlib.sha256(raw).hexdigest() + '"'

    async def snapshot(self):
        await self.initialize()
        from tgdrive.core.governor import (
            AUTO_FREE_SPACE_RESERVE,
            AUTO_RESOURCE_LIMITS,
            MANAGED,
        )
        from tgdrive.media.media_policy import AUTO_STREAM_MEMORY_LIMIT_BYTES

        return {
            key: value
            for key, value in self.value.model_dump().items()
            if key not in MANAGED
        } | {
            "revision": self.etag(),
            "auto_resource_limits": dict(AUTO_RESOURCE_LIMITS)
            | {
                "stream_memory_limit_bytes": AUTO_STREAM_MEMORY_LIMIT_BYTES,
            },
            "auto_free_space_reserve": dict(AUTO_FREE_SPACE_RESERVE),
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
                "TGDrive tunes effective budgets automatically. "
                "Save resource ceilings instead of effective budget fields.",
                status=422,
            )
        await self.initialize()
        async with self._lock:
            if expected is not None and expected != self.etag():
                raise SettingsProblem(
                    "Settings changed in another tab. Reload before saving.", status=409
                )
            value = PerformancePreferences.model_validate(
                self.value.model_dump() | changes
            )

            @_journalled
            async def commit_and_apply():
                try:
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

            await commit_and_apply()
        return await self.snapshot()


def get_performance_settings(settings, runtime):
    service = getattr(runtime, "performance_settings", None)
    if service is None:
        service = PerformanceSettings(settings, runtime)
        runtime.performance_settings = service
    return service
