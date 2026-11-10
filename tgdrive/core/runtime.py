"""Application lifecycle orchestration and graceful shutdown."""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from tgdrive.core.config import Settings
from tgdrive.recovery.backup import RecoveryConflict
from tgdrive.recovery.mongo_backup import MongoProtection
from tgdrive.settings.bot_settings import get_bot_settings
from tgdrive.storage.mongo import DatabaseUnavailable
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.bot_mode import BotMode
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import build_bot_mode_client, build_client_pool
from tgdrive.transfers.manager import TransferManager, restore_telegram_cooldowns
from tgdrive.transfers.remote import RemoteClient, RemoteTimeouts
from tgdrive.transfers.tasks import TransferRegistry, _journalled

if TYPE_CHECKING:
    from tgdrive.core.governor import Governor
    from tgdrive.media.preloading import PreloadManager
    from tgdrive.replication.replica import ChannelReplica
    from tgdrive.replication.replica_activation import ActivationError
    from tgdrive.transfers.uploads import ResumableUploads

logger = logging.getLogger(__name__)
_worker = _journalled(asyncio.to_thread)


class Runtime:
    preloads: PreloadManager
    replica: ChannelReplica
    governor: Governor

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
            ),
        )
        self.registry = TransferRegistry(ttl_seconds=settings.transfer_ttl_seconds)
        self.store: MongoStore | None = None
        self.transfers: TransferManager | None = None
        self.bot_mode: BotMode | None = None
        self.ready = False
        self._activation_unavailable: ActivationError | None = None
        self._owner_lock = None
        self._mongo_acquired = False
        self._reconnector = None
        self._channel_check_lock = asyncio.Lock()
        self._channel_verified_until = 0.0
        self.storage_channel_error: str | None = None

    async def start(self) -> None:
        if self.ready:
            return
        starting_task = asyncio.current_task()
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
            from tgdrive.replication.replica import ChannelReplica

            # Database configuration, connectivity and ownership are startup
            # requirements, including when recovery metadata is available.
            logger.info("Checking MongoDB connectivity")
            await _worker(self.mongo.ping)
            logger.info("Acquiring MongoDB drive ownership")

            def acquire():
                self.mongo.acquire()
                self._mongo_acquired = True

            await _worker(acquire)
            logger.info("Checking MongoDB transaction support and write permissions")
            await _worker(self.mongo.transaction, self.mongo.fence)

            self.replica = ChannelReplica(self.settings, self)
            if not await self._open_catalog():
                return
            assert self.store is not None
            self.recovery_only = False
            self.backup.replica = self.replica

            def finish_activation():
                with self.replica.journal.lock:
                    if not self.replica.journal.get("activation"):
                        return
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

            await _worker(finish_activation)
            try:
                await _worker(self.replica.key)
            except Exception as error:
                if starting_task is not None and starting_task.cancelling():
                    raise
                await _worker(self.replica._record_failure, error)
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
            from tgdrive.settings.performance_preferences import (
                get_performance_settings,
            )

            await get_performance_settings(self.settings, self).initialize()
            check_binding = getattr(self.backup, "check_local_binding", None)
            if check_binding is not None:
                try:
                    await _worker(check_binding, self.store)
                except RecoveryConflict:
                    if starting_task is not None and starting_task.cancelling():
                        raise
                    logger.warning(
                        "Storage-channel configuration conflicts with local state"
                    )
            await restore_telegram_cooldowns(self.pool, self.registry.list())
            connected = False
            try:
                await self.pool.start()
                connected = True
            except Exception as exc:
                if starting_task is not None and starting_task.cancelling():
                    raise
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
                    if starting_task is not None and starting_task.cancelling():
                        raise
                    logger.warning(
                        "Recovery conflict: metadata stays available; "
                        "Telegram mutation recovery is paused"
                    )
                except Exception:
                    if starting_task is not None and starting_task.cancelling():
                        raise
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
            from tgdrive.core.governor import Governor
            from tgdrive.transfers.uploads import ResumableUploads

            manager = self.transfers

            def restore_uploads():
                manager.uploads = ResumableUploads(self.settings, manager)

            await _worker(restore_uploads)
            self.governor = Governor(self.settings, self)
            await self.governor.tick()
            if await _worker(lambda: self.replica.blocked):
                lineage_ready = False
            if lineage_ready:
                try:
                    await self.transfers.recover()
                except Exception:
                    if starting_task is not None and starting_task.cancelling():
                        raise
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
                    transfers=self.transfers,
                )
                await self.bot_mode.start()
            self.backup.start(self.store)
            self.replica.start()
            self.ready = True
            self.governor.start()
            if not lineage_ready:
                self._reconnector = asyncio.create_task(
                    self._reconnect(), name="telegram-reconnect"
                )
            logger.info("TGDrive runtime is ready")
        except BaseException as error:
            if isinstance(error, DatabaseUnavailable):
                logger.error(
                    "TGDrive startup rejected: %s Correct the MongoDB configuration "
                    "or database access, then restart the deployment.",
                    error,
                )
            elif not isinstance(error, asyncio.CancelledError):
                logger.exception("TGDrive startup failed")
            try:
                await self._cleanup_failed_start()
            except asyncio.CancelledError:
                pass
            except BaseException:
                logger.exception("Cleanup after failed startup failed")
            raise

    async def _open_catalog(self) -> bool:
        """Open the owned catalog, or retain the existing recovery-only path."""
        from tgdrive.core.errors import CorruptStoreError
        from tgdrive.recovery.backup import BackupError
        from tgdrive.replication.replica_activation import (
            ActivationError,
            apply_activation,
        )
        from tgdrive.storage.mongo import initialize_if_empty

        try:
            await _worker(self.replica._check_saved_state)
            if activation_error := getattr(self, "_activation_unavailable", None):
                raise activation_error
            await _worker(apply_activation, self.settings, self.mongo)
            from tgdrive.storage.upgrade import upgrade

            # A 2.0-2.2 drive is upgraded in place; its files stay where they are.
            await _worker(upgrade, self.mongo, self.settings)
            if await self._offer_backup_restore():
                return False
            created = await _worker(initialize_if_empty, self.settings, self.mongo)
            logger.info(
                "Created a new MongoDB drive from deployment configuration"
                if created
                else "Opening the existing MongoDB drive"
            )

            @_journalled
            async def restore_store():
                self.store = await self.backup.restore()

            await restore_store()
            from tgdrive.core.encryption import check_drive_key

            await _worker(check_drive_key, self.mongo, self.settings.encryption_key)
        except (
            ActivationError,
            BackupError,
            CorruptStoreError,
            ValueError,
        ) as error:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            self.recovery_only = True
            await _worker(self.replica._record_failure, error)
            if getattr(error, "code", None) != "ACTIVATION_RESOURCE_LIMIT":
                await _worker(
                    self.replica.report,
                    "error",
                    "The MongoDB catalog is unavailable or requires explicit "
                    "recovery. Existing data has been preserved; normal file "
                    "operations are stopped.",
                )
            self.replica.start()
            logger.warning(
                "Catalog unavailable: %s Owner web recovery is available; "
                "file writers are stopped",
                error,
            )
            return False
        return True

    def _check_upload_allowed(self) -> None:
        if getattr(self, "replica", None) is not None and self.replica.blocked:
            from tgdrive.settings.managed_settings import SettingsProblem

            raise SettingsProblem(
                "Finish or disable channel recovery before uploading.", status=409
            )
        self.backup.check_upload_allowed(self.store)

    async def _check_bot_upload_allowed(self) -> None:
        self._check_upload_allowed()
        await self._verify_storage_channel()

    async def _reconnect(self) -> None:
        store = self.store
        if store is None:
            return
        while self.ready:
            try:
                await self.pool.start()
                await self._verify_storage_channel()
                await self.backup.backup(store)
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
                            store=store,
                            folder_settings=get_bot_settings(self.settings, self),
                            before_write=self._check_bot_upload_allowed,
                            before_delete=lambda: self.backup.verify_lineage(store),
                            transfers=self.transfers,
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

            probed = False
            for row in await self.pool.status():
                if row.kind != "bot" or not row.healthy:
                    continue
                async with self.pool.lease(client_name=row.name) as client:
                    probe = getattr(client, "storage_channel_problem", None)
                    if not callable(probe):
                        continue
                    try:
                        async with asyncio.timeout(30):
                            pending_problem = probe(
                                self.settings.storage_channel,
                                require_pin=False,
                            )
                            if not inspect.isawaitable(pending_problem):
                                raise TypeError("Channel probe must be asynchronous")
                            problem = await pending_problem
                    except Exception:
                        self.storage_channel_error = (
                            "Telegram storage channel could not be verified. "
                            "Check the network, channel membership "
                            "and bot permissions, "
                            "then retry."
                        )
                        raise ConnectionError(self.storage_channel_error) from None
                    if problem is not None:
                        self.storage_channel_error = problem[1]
                        raise SettingsProblem(problem[1], code=problem[0], status=409)
                    probed = True
            if not probed:
                self.storage_channel_error = (
                    "No healthy bot could verify the Telegram storage channel. "
                    "Check bot tokens and connectivity, then retry."
                )
                raise ConnectionError(self.storage_channel_error)
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

    @_journalled
    async def stop(self) -> list[BaseException]:
        self.ready = False
        failures: list[BaseException] = []
        if getattr(self, "governor", None) is not None:
            try:
                await self.governor.stop()
            except Exception as exc:
                logger.exception("Failed to stop the resource governor")
                failures.append(exc)
        if self._reconnector is not None:
            self._reconnector.cancel()
            await asyncio.gather(self._reconnector, return_exceptions=True)
            self._reconnector = None
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
            try:
                await self.remote.close()
            except Exception as exc:
                logger.exception("Failed to stop the remote transport")
                failures.append(exc)
        if getattr(self, "replica", None) is not None:
            try:
                await self.replica.close()
            except Exception as exc:
                logger.exception("Failed to stop the channel replica")
                failures.append(exc)
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
        closers: list[TransferRegistry | ResumableUploads | BotMode | MongoStore] = [
            self.registry
        ]
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
            await _worker(self._release_owner_lock)
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

    @_journalled
    async def _cleanup_failed_start(self) -> None:
        self.ready = False
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(_worker, self._release_owner_lock)
            if self.store is not None:
                cleanup.callback(self.store.close)
            cleanup.callback(self.registry.close)
            if self.transfers is not None:
                uploads = getattr(self.transfers, "uploads", None)
                if uploads is not None:
                    cleanup.callback(uploads.close)
            cleanup.push_async_callback(self.pool.stop, drain_timeout=10)
            cleanup.push_async_callback(self.remote.close)
            if getattr(self, "replica", None) is not None:
                cleanup.push_async_callback(self.replica.close)
            if self.transfers is not None:
                cleanup.push_async_callback(self.transfers.close)
            if self.bot_mode is not None:
                cleanup.callback(self.bot_mode.close)
                cleanup.push_async_callback(self.bot_mode.stop)
            cleanup.push_async_callback(self._close_media_resources)
            if getattr(self, "governor", None) is not None:
                cleanup.push_async_callback(self.governor.stop)

    async def _offer_backup_restore(self) -> bool:
        """An empty database with a pinned backup opens the restore screen.

        Same env + empty MongoDB must restore the drive, not start a new one.
        """
        from tgdrive.replication.replica import ChannelReplica
        from tgdrive.storage.mongo import database_is_empty

        if not await _worker(database_is_empty, self.mongo):
            return False
        replica = getattr(self, "replica", None)
        created = replica is None
        if created:
            replica = ChannelReplica(self.settings, self)
        candidate = await _worker(replica.journal.get, "candidate")
        if candidate and candidate.get("discovered"):
            found = True
        else:
            try:
                found = await replica.discover_backup()
            except Exception as error:
                if (task := asyncio.current_task()) is not None and task.cancelling():
                    raise
                # Never initialize over a backup we could not read.
                logger.warning("A pinned backup could not be checked; opening recovery")
                await _worker(replica._record_failure, error)
                found = True
        if not found:
            if created:
                await replica.close()
            return False
        self.recovery_only = True
        self.replica = replica
        replica.start()
        logger.warning("MongoDB is empty and a backup exists: opening Restore drive")
        return True

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
