"""Application lifecycle orchestration and graceful shutdown."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import time
from pathlib import Path

from tgdrive.core.config import Settings
from tgdrive.recovery.backup import RecoveryConflict
from tgdrive.recovery.mongo_backup import MongoProtection
from tgdrive.settings.bot_settings import get_bot_settings
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.store import DriveStore
from tgdrive.telegram.bot_mode import BotMode
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import build_bot_mode_client, build_client_pool
from tgdrive.transfers.manager import TransferManager, restore_telegram_cooldowns
from tgdrive.transfers.remote import RemoteClient, RemoteTimeouts
from tgdrive.transfers.tasks import TransferRegistry

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        pool: ClientPool | None = None,
    ) -> None:
        self.settings = settings
        from tgdrive.storage.mongo import for_settings

        self.mongo = for_settings(settings)
        self.pool = pool or build_client_pool(settings)
        self.backup = MongoProtection(settings, self.pool, self.mongo)
        self.remote = RemoteClient(
            download_concurrency=settings.remote_download_concurrency,
            max_bytes=settings.upload_limit,
            min_free_bytes=settings.min_free_bytes,
            max_redirects=settings.remote_max_redirects,
            allow_private=settings.allow_private_remote_urls,
            timeouts=RemoteTimeouts(
                connect=settings.remote_connect_timeout,
                read=settings.remote_read_timeout,
                total=settings.remote_total_timeout,
            ),
        )
        self.registry = TransferRegistry(ttl_seconds=settings.transfer_ttl_seconds)
        self.store: DriveStore | None = None
        self.transfers: TransferManager | None = None
        self.bot_mode: BotMode | None = None
        self.ready = False
        self._owner_lock = None
        self._mongo_acquired = False
        self._reconnector = None
        self._channel_check_lock = asyncio.Lock()
        self._channel_verified_until = 0.0
        self.storage_channel_error: str | None = None

    async def start(self) -> None:
        if self.ready:
            return
        self._prepare_runtime_directories()
        try:
            self._owner_lock = (
                self.settings.resolved_data_dir / ".instance.lock"
            ).open("a")
            try:
                fcntl.flock(self._owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._owner_lock.close()
                self._owner_lock = None
                raise RuntimeError(
                    "Another TGDrive instance owns this data directory"
                ) from exc
            from tgdrive.core.errors import CorruptStoreError
            from tgdrive.recovery.backup import BackupError
            from tgdrive.replication.replica import ChannelReplica
            from tgdrive.replication.replica_activation import (
                ActivationError,
                apply_activation,
            )
            from tgdrive.storage.mongo import (
                initialize_if_empty,
                record_drive_open,
            )

            # Database configuration, connectivity and ownership are startup
            # requirements, including when recovery metadata is available.
            logger.info("Checking MongoDB connectivity")
            await asyncio.to_thread(self.mongo.ping)
            logger.info("Acquiring MongoDB drive ownership")
            await asyncio.to_thread(self.mongo.acquire)
            self._mongo_acquired = True
            logger.info("Checking MongoDB transaction support and write permissions")
            await asyncio.to_thread(self.mongo.transaction, self.mongo.fence)

            try:
                if getattr(self, "_activation_unavailable", False):
                    raise ActivationError("Recovery activation requires review")
                await asyncio.to_thread(apply_activation, self.settings, self.mongo)
                created = await asyncio.to_thread(
                    initialize_if_empty, self.settings, self.mongo
                )
                logger.info(
                    "Created a new MongoDB drive from deployment configuration"
                    if created
                    else "Opening the existing MongoDB drive"
                )
                self.store = await self.backup.restore()
                await asyncio.to_thread(record_drive_open, self.settings, self.store)
            except (
                ActivationError,
                BackupError,
                CorruptStoreError,
                ValueError,
            ):
                self.recovery_only = True
                self.replica = ChannelReplica(self.settings, self)
                self.replica.report(
                    "error",
                    "The MongoDB catalog is unavailable or requires explicit recovery. "
                    "Existing data has been preserved; normal file operations "
                    "are stopped.",
                )
                self.replica.start()
                logger.warning(
                    "Catalog unavailable: owner web recovery is available; "
                    "file writers are stopped"
                )
                return
            self.recovery_only = False
            self.replica = ChannelReplica(self.settings, self)
            self.backup.replica = self.replica
            if self.replica.journal.get("activation"):
                candidate = self.replica.journal.get("candidate", {})
                backup_config = candidate.get("backup_config")
                transition = {
                    "activation": None,
                    "recovery": None,
                    "candidate": None,
                    "job": None,
                    "config": dict(backup_config, enabled=True)
                    if backup_config
                    else {},
                    "status": self.replica.journal.get("status", {})
                    | {
                        "phase": "idle" if backup_config else "disabled",
                        "protected_revision": 0,
                        "error": None,
                    },
                }
                if "database" in candidate:
                    transition.update(
                        key=candidate["key"], mongodb_key=candidate["key"]
                    )
                self.replica.journal.set_many(transition)
            try:
                await asyncio.to_thread(self.replica.key)
            except Exception as error:
                self.replica._record_failure(error)
                logger.warning(
                    "Recovery key persistence needs attention: %s", type(error).__name__
                )
            original_lineage = getattr(self.backup, "verify_lineage", None)

            async def protected_lineage(store):
                if self.replica.blocked:
                    from tgdrive.settings.managed_settings import SettingsProblem

                    raise SettingsProblem(
                        "Channel recovery has paused Telegram changes.", status=409
                    )
                await self._verify_storage_channel()
                if original_lineage is not None:
                    await original_lineage(store)

            self.backup.verify_lineage = protected_lineage
            self.registry = TransferRegistry(
                ttl_seconds=self.settings.transfer_ttl_seconds,
                mongo=self.mongo.db.jobs,
                mongo_backend=self.mongo,
            )
            from tgdrive.settings.transfer_preferences import get_transfer_preferences

            await get_transfer_preferences(self.settings, self).initialize()
            from tgdrive.settings.performance_preferences import (
                get_performance_settings,
            )

            await get_performance_settings(self.settings, self).initialize()
            check_binding = getattr(self.backup, "check_local_binding", None)
            if check_binding is not None:
                try:
                    check_binding(self.store)
                except RecoveryConflict:
                    logger.warning(
                        "Storage-channel configuration conflicts with local state"
                    )
            await restore_telegram_cooldowns(self.pool, self.registry.list())
            restore_publication = getattr(
                self.backup, "restore_publication_cooldown", None
            )
            if callable(restore_publication):
                await restore_publication()
            connected = False
            try:
                await self.pool.start()
                connected = True
            except Exception as exc:
                logger.warning(
                    "Telegram unavailable during startup: %s; metadata stays available",
                    type(exc).__name__,
                )
            lineage_ready = False
            if connected:
                try:
                    # Clean local copies can still be older than a remote pin.
                    # Verify before resuming any Telegram side effects.
                    await self._verify_storage_channel()
                    await self.backup.backup(self.store)
                    lineage_ready = True
                except RecoveryConflict:
                    logger.warning(
                        "Recovery conflict: metadata stays available; "
                        "Telegram mutation recovery is paused"
                    )
                except Exception:
                    logger.warning(
                        "Initial channel verification failed; the saved catalog "
                        "remains available and transfer recovery waits for repair"
                    )

            async def ensure_backup(required_revision: int) -> int:
                await self._verify_storage_channel()
                return await self.backup.ensure_revision(self.store, required_revision)

            async def ensure_lineage() -> None:
                await self.backup.verify_lineage(self.store)

            self.transfers = TransferManager(
                settings=self.settings,
                pool=self.pool,
                store=self.store,
                remote=self.remote,
                registry=self.registry,
                ensure_backup=ensure_backup,
                ensure_lineage=ensure_lineage,
                ensure_channel=self._verify_storage_channel,
                check_upload_allowed=self._check_upload_allowed,
            )
            self.transfers._ensure_replica = self.replica.before_mutation
            if self.replica.blocked:
                lineage_ready = False
            if lineage_ready:
                try:
                    await self.transfers.recover()
                except Exception:
                    lineage_ready = False
                    logger.warning(
                        "Durable transfers are waiting for verified protection; "
                        "owner metadata and backup controls remain available"
                    )
            bot_client = build_bot_mode_client(self.settings) if lineage_ready else None
            if bot_client is not None:
                bot_client.bind_send_gate(self.pool.pace_send, self.pool.defer_channel)
                self.bot_mode = BotMode(
                    client=bot_client,
                    settings=self.settings,
                    store=self.store,
                    folder_settings=get_bot_settings(self.settings, self),
                    before_write=self._check_bot_upload_allowed,
                    before_delete=lambda: self.backup.verify_lineage(self.store),
                )
                await self.bot_mode.start()
            self.backup.start(self.store)
            self.replica.start()
            self.ready = True
            if not lineage_ready:
                self._reconnector = asyncio.create_task(
                    self._reconnect(), name="telegram-reconnect"
                )
            logger.info("TGDrive runtime is ready")
        except DatabaseUnavailable as exc:
            logger.error(
                "TGDrive startup rejected: %s Correct the MongoDB configuration "
                "or database access, then restart the deployment.",
                exc,
            )
            await self._cleanup_failed_start()
            raise
        except Exception:
            logger.exception("TGDrive startup failed")
            await self._cleanup_failed_start()
            raise

    def _check_upload_allowed(self) -> None:
        if getattr(self, "replica", None) is not None and self.replica.blocked:
            from tgdrive.settings.managed_settings import SettingsProblem

            raise SettingsProblem(
                "Finish or disable channel recovery before uploading.", status=409
            )
        self.backup.check_upload_allowed(self.store)
        manager = getattr(self, "connection_manager", None)
        if manager is not None:
            manager.check_upload_allowed()

    async def _check_bot_upload_allowed(self) -> None:
        self._check_upload_allowed()
        await self._verify_storage_channel()

    async def _reconnect(self) -> None:
        while self.ready:
            try:
                await self.pool.start()
                await self._verify_storage_channel()
                await self.backup.backup(self.store)
                if self.transfers is not None:
                    await self.transfers.recover()
                logger.info("Telegram connection and catalog lineage verified")
                if self.bot_mode is None:
                    client = build_bot_mode_client(self.settings)
                    if client is not None:
                        client.bind_send_gate(
                            self.pool.pace_send, self.pool.defer_channel
                        )
                        self.bot_mode = BotMode(
                            client=client,
                            settings=self.settings,
                            store=self.store,
                            folder_settings=get_bot_settings(self.settings, self),
                            before_write=self._check_bot_upload_allowed,
                            before_delete=lambda: self.backup.verify_lineage(
                                self.store
                            ),
                        )
                if self.bot_mode is not None:
                    await self.bot_mode.start()
                return
            except Exception:
                await asyncio.sleep(30)

    async def _verify_storage_channel(self) -> None:
        """Recheck Telegram-side changes before resuming durable mutations."""
        if time.monotonic() < self._channel_verified_until:
            return
        # Pool adapters may implement only the lifecycle protocol (e.g. an
        # externally supplied transport); production Telethon adapters probe.
        status = getattr(self.pool, "status", None)
        if not callable(status):
            return
        async with self._channel_check_lock:
            if time.monotonic() < self._channel_verified_until:
                return
            from tgdrive.settings.managed_settings import SettingsProblem

            for row in await status():
                if row.kind != "bot" or not row.healthy:
                    continue
                async with self.pool.lease(client_name=row.name) as client:
                    probe = getattr(client, "storage_channel_problem", None)
                    if not callable(probe):
                        continue
                    try:
                        async with asyncio.timeout(30):
                            problem = await probe(
                                self.settings.storage_channel,
                                require_pin=False,
                            )
                    except Exception:
                        self.storage_channel_error = (
                            "Telegram storage channel could not be verified. "
                            "Check the network, channel membership "
                            "and bot permissions, "
                            "then retry or restart TGDrive."
                        )
                        raise ConnectionError(self.storage_channel_error) from None
                    if problem is not None:
                        self.storage_channel_error = problem[1]
                        raise SettingsProblem(problem[1], code=problem[0], status=409)
            self.storage_channel_error = None
            # Bound Telegram requests for concurrent uploads. Failed checks are
            # never cached, so fixing channel settings takes effect on retry.
            self._channel_verified_until = time.monotonic() + 30

    async def _close_media_resources(self) -> list[BaseException]:
        manager = getattr(self, "preloads", None)
        resource = (
            manager
            if manager is not None
            else getattr(self.pool, "_media_read_cache", None)
        )
        if resource is not None:
            try:
                await resource.close()
            except Exception as exc:
                logger.exception("Failed to stop disposable media resources")
                return [exc]
        return []

    async def stop(self) -> list[BaseException]:
        self.ready = False
        if self._reconnector is not None:
            self._reconnector.cancel()
            await asyncio.gather(self._reconnector, return_exceptions=True)
            self._reconnector = None
        failures: list[BaseException] = []
        failures.extend(await self._close_media_resources())
        if self.bot_mode is not None:
            try:
                await self.bot_mode.stop()
            except Exception as exc:
                logger.exception("Failed to stop bot mode")
                failures.append(exc)
        if self.transfers is not None:
            try:
                await self.transfers.close()
            except Exception as exc:
                logger.exception("Failed to stop transfer manager")
                failures.append(exc)
        else:
            await self.remote.close()
        if getattr(self, "replica", None) is not None:
            await self.replica.close()
        if self.store is not None:
            try:
                await self.backup.stop(self.store)
            except Exception as exc:
                logger.exception("Final metadata backup failed")
                failures.append(exc)
        try:
            stop_failures = await self.pool.stop(drain_timeout=20)
            failures.extend(error for _, error in stop_failures)
        except Exception as exc:
            logger.exception("Failed to stop Telegram client pool")
            failures.append(exc)
        closers = [self.registry]
        if self.transfers is not None and hasattr(self.transfers, "uploads"):
            closers.append(self.transfers.uploads)
        if self.bot_mode is not None:
            closers.append(self.bot_mode)
        if self.store is not None:
            closers.append(self.store)
        try:
            for resource in closers:
                try:
                    resource.close()
                except Exception as exc:
                    logger.exception("Failed to close local persistence")
                    failures.append(exc)
        finally:
            self._release_owner_lock()
        if failures:
            logger.error("Shutdown completed with %s error(s)", len(failures))
        else:
            logger.info("TGDrive runtime stopped cleanly")
        return failures

    def _release_owner_lock(self) -> None:
        if self._mongo_acquired:
            self.mongo.release()
            self._mongo_acquired = False
        if self._owner_lock is not None:
            self._owner_lock.close()
            self._owner_lock = None

    async def _cleanup_failed_start(self) -> None:
        if getattr(self, "replica", None) is not None:
            await self.replica.close()
        if self.bot_mode is not None:
            try:
                await self.bot_mode.stop()
            except Exception:
                logger.exception("Bot-mode cleanup after failed startup failed")
            finally:
                self.bot_mode.close()
        try:
            await self.remote.close()
        except Exception:
            logger.exception("Remote-client cleanup after failed startup failed")
        await self._close_media_resources()
        try:
            await self.pool.stop(drain_timeout=10)
        except Exception:
            logger.exception("Client-pool cleanup after failed startup failed")
        try:
            if self.transfers is not None:
                await self.transfers.close()
                self.transfers.uploads.close()
            self.registry.close()
            if self.store is not None:
                self.store.close()
        finally:
            self._release_owner_lock()

    def _prepare_runtime_directories(self) -> None:
        cache_dir = self.settings.resolved_cache_dir
        data_dir = self.settings.resolved_data_dir
        self._validate_runtime_path(cache_dir, "CACHE_DIR")
        self._validate_runtime_path(data_dir, "DATA_DIR")
        if cache_dir.is_relative_to(data_dir) or data_dir.is_relative_to(cache_dir):
            raise ValueError("CACHE_DIR and DATA_DIR must not overlap")
        cleanup_paths = [cache_dir / name for name in ("uploads", "remote", "torrents")]
        for cleanup_path in cleanup_paths:
            resolved = cleanup_path.resolve()
            if resolved == cache_dir or not resolved.is_relative_to(cache_dir):
                raise ValueError("CACHE_DIR cleanup paths must stay inside CACHE_DIR")
        data_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Staged bytes belong to durable jobs and must survive restart.

    @staticmethod
    def _validate_runtime_path(path: Path, name: str) -> None:
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError(
                f"{name} cannot be a filesystem root or the home directory"
            )
