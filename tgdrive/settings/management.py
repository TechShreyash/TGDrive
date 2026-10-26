"""Owner-only connection management with isolated verification and restart apply."""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from collections import Counter
from contextlib import suppress
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationError
from telethon import TelegramClient, functions, utils
from telethon.sessions import StringSession

from tgdrive.core.config import Settings
from tgdrive.settings.managed_settings import (
    MAX_CLIENTS,
    ManagedClient,
    ManagedDocument,
    PrivateModel,
    SettingsProblem,
    initial_clients,
    read_document,
    save_document,
)
from tgdrive.telegram.channel_state import storage_channel_problem
from tgdrive.telegram.client_identity import IdentityCache, account_identity
from tgdrive.telegram.connection_errors import connection_problem

_PREFLIGHT_LOG = logging.getLogger("tgdrive.private_telegram_preflight")
_PREFLIGHT_LOG.addHandler(logging.NullHandler())
_PREFLIGHT_LOG.propagate = False
_PREFLIGHT_LOG.setLevel(logging.CRITICAL + 1)


class ClientChange(PrivateModel):
    id: str | None = Field(default=None, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["bot", "user"]
    enabled: bool = True
    credential: SecretStr | None = None


class SettingsChange(PrivateModel):
    expected_revision: int = Field(ge=0)
    api_id: int | None = Field(default=None, gt=0, le=2147483647)
    api_hash: SecretStr | None = None
    clients: list[ClientChange] | None = Field(
        default=None, min_length=1, max_length=MAX_CLIENTS
    )


async def verify_connection(
    api_id: int,
    api_hash: str,
    row: ManagedClient,
    channel: int,
    *,
    identity_cache: IdentityCache | None = None,
    require_pin: bool = True,
) -> str:
    """No messages are sent: use a separate in-memory session for every probe."""
    client = None
    try:
        session = (
            StringSession(row.credential.get_secret_value())
            if row.kind == "user"
            else StringSession()
        )
        client = TelegramClient(
            session,
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
            request_retries=2,
            connection_retries=0,
            timeout=10,
            auto_reconnect=False,
            base_logger=_PREFLIGHT_LOG,
        )
        client.parse_mode = None
        async with asyncio.timeout(25):
            await client.connect()
            if row.kind == "bot":
                await client.sign_in(bot_token=row.credential.get_secret_value())
            elif not await client.is_user_authorized():
                raise SettingsProblem(
                    "Premium session has expired or is not authorized"
                )
            account = await client.get_me()
            if account is None or bool(account.bot) != (row.kind == "bot"):
                raise SettingsProblem(
                    "Telegram account does not match the connection type"
                )
            if row.kind == "bot" and account.id != int(
                row.credential.get_secret_value().split(":", 1)[0]
            ):
                raise SettingsProblem("Telegram bot identity did not match its token")
            if row.kind == "user" and not getattr(account, "premium", False):
                raise SettingsProblem(
                    "This account does not have an active Telegram Premium subscription"
                )
            peer = await client.get_input_entity(channel)
            result = await client(
                functions.channels.GetFullChannelRequest(utils.get_input_channel(peer))
            )
            chat = next(
                (item for item in result.chats if utils.get_peer_id(item) == channel),
                None,
            )
            if chat is None or not getattr(chat, "broadcast", False):
                raise SettingsProblem(
                    "The account cannot access the configured storage channel"
                )
            problem = storage_channel_problem(chat, getattr(result, "full_chat", None))
            if problem is not None:
                raise SettingsProblem(problem[1], code=problem[0])
            rights = getattr(chat, "admin_rights", None)
            required = (
                (
                    ("post_messages", "edit_messages", "delete_messages")
                    if require_pin
                    else ("post_messages", "delete_messages")
                )
                if row.kind == "bot"
                else ("post_messages",)
            )
            if not getattr(chat, "creator", False) and (
                rights is None
                or not all(getattr(rights, key, False) for key in required)
            ):
                raise SettingsProblem(
                    "Storage bots need the required channel administrator permissions; "
                    "Premium accounts need permission to post."
                )
            runtime_name = (
                f"storage-bot-{account.id}"
                if row.kind == "bot"
                else f"premium-user-{account.id}"
            )
            if identity_cache is not None:
                with suppress(Exception):
                    identity = account_identity(account)
                    if any(identity.values()):
                        await asyncio.to_thread(
                            identity_cache.write, runtime_name, identity
                        )
            return runtime_name
    except SettingsProblem:
        raise
    except Exception as error:
        access_problem = connection_problem(error, row.kind)
        delay = getattr(error, "seconds", None)
        if access_problem is not None:
            detail = str(access_problem)
        elif isinstance(delay, int) and delay > 0:
            detail = (
                "Telegram temporarily limited verification. Wait before trying again."
            )
        else:
            detail = (
                "Telegram could not verify this connection. Check credentials, "
                "account access, and network connectivity."
            )
        problem = SettingsProblem(detail, code="SETTINGS_PREFLIGHT_FAILED", status=503)
        problem.retry_after = delay if isinstance(delay, int) and delay > 0 else None
        problem.retryable = isinstance(
            error, (TimeoutError, ConnectionError, OSError)
        ) or type(error).__name__ in {
            "ServerError",
            "RpcCallFailError",
            "TimedOutError",
        }
        raise problem from None
    finally:
        if client is not None:
            with suppress(Exception):
                await client.disconnect()


class ConnectionManager:
    def __init__(self, settings: Any, runtime: Any, *, verifier=verify_connection):
        self.settings = settings
        self.runtime = runtime
        self.verifier = verifier
        self.lock = asyncio.Lock()
        self.active = initial_clients(settings)
        self.identity_cache = IdentityCache(settings.resolved_data_dir)
        self._cached_names: dict[str, dict[str, str | None]] = {}
        self._checks: dict[str, dict[str, Any]] = {}
        self._checks_revision: int | None = None
        self._check_tasks: set[asyncio.Task] = set()
        self._refresh_not_before = 0.0

    @property
    def restart_required(self) -> bool:
        revision, _, _, _ = self._desired()
        return revision != self.settings.managed_revision

    def login_api(self) -> tuple[int, SecretStr, int]:
        """Private handoff for bounded login challenges; never an HTTP response."""
        self._ensure_idle()
        revision, api_id, api_hash, _ = self._desired()
        return api_id, api_hash, revision

    def session_is_saved(self, session_string: str) -> bool | None:
        """Resolve ambiguous save outcomes without exposing the private session."""
        try:
            document = read_document(self.settings.resolved_data_dir)
            if document is None:
                return False
            return any(
                row.kind == "user"
                and secrets.compare_digest(
                    row.credential.get_secret_value().encode(), session_string.encode()
                )
                for row in document.clients
            )
        except Exception:
            # Never revoke an authorization that may already be persisted.
            return None

    def check_upload_allowed(self) -> None:
        if self.restart_required:
            raise SettingsProblem(
                "Connection changes are saved. Restart TGDrive before uploading.",
                code="SETTINGS_RESTART_REQUIRED",
                status=409,
            )

    def _ensure_idle(self) -> None:
        runtime = self.runtime
        if getattr(getattr(runtime, "bot_mode", None), "active_imports", 0):
            raise SettingsProblem(
                "Wait for the bot import to finish before changing connections.",
                code="SETTINGS_BUSY",
                status=409,
            )
        registry = getattr(runtime, "registry", None) or getattr(
            getattr(runtime, "transfers", None), "registry", None
        )
        if registry is not None and any(
            str(record.committed.status) not in {"completed", "cancelled"}
            for record in registry.list()
        ):
            raise SettingsProblem(
                "Finish or cancel pending transfers before changing connections.",
                code="SETTINGS_BUSY",
                status=409,
            )
        if self._stats()["staging_reserved_bytes"]:
            raise SettingsProblem(
                "Finish or cancel browser uploads before changing connections.",
                code="SETTINGS_BUSY",
                status=409,
            )
        backup = getattr(runtime, "backup", None)
        store = getattr(runtime, "store", None)
        if store is not None and callable(getattr(backup, "protection_status", None)):
            status = backup.protection_status(store)
            clean_watermark = (
                status.get("protected_revision") == store.revision
                and status.get("metadata_revision") == store.revision
            )
            if status.get("recovery_required") or (
                status.get("state") != "protected" and not clean_watermark
            ):
                raise SettingsProblem(
                    "Wait for the current drive backup to be confirmed before "
                    "changing connections.",
                    code="SETTINGS_BUSY",
                    status=409,
                )

    def _desired(self) -> tuple[int, int, SecretStr, list[ManagedClient]]:
        document = read_document(self.settings.resolved_data_dir)
        if document is not None:
            return (
                document.revision,
                document.api_id,
                document.api_hash,
                document.clients,
            )
        return 0, self.settings.api_id, self.settings.api_hash, self.active

    async def _active_snapshot(
        self,
        desired: list[ManagedClient] | None = None,
    ) -> list[dict[str, Any]]:
        pool = getattr(self.runtime, "pool", None)
        statuses = (
            await pool.status() if callable(getattr(pool, "status", None)) else ()
        )
        names = [getattr(status, "name", "") for status in statuses] + [
            row.runtime_name
            or (
                "storage-bot-" + row.credential.get_secret_value().split(":", 1)[0]
                if row.kind == "bot"
                else ""
            )
            for row in desired or []
        ]
        try:
            self._cached_names = await asyncio.to_thread(
                self.identity_cache.read_many, names
            )
        except Exception:
            self._cached_names = {}  # Optional display data never blocks settings.
        result = []
        for index, row in enumerate(self.active):
            # Pool preserves bot-first and user-second source order, including failures.
            status = statuses[index] if index < len(statuses) else None
            result.append(
                {
                    "id": row.id,
                    "label": row.label,
                    "display_name": getattr(status, "display_name", None),
                    "username": getattr(status, "username", None),
                    "identity_refreshing": bool(
                        getattr(status, "identity_refreshing", False)
                    ),
                    "kind": row.kind,
                    "identity_retry_at": getattr(status, "identity_retry_at", None),
                    "runtime_name": getattr(status, "name", None),
                    "connected": getattr(status, "connected", None),
                    "healthy": bool(getattr(status, "healthy", False)),
                    "premium": bool(
                        row.kind == "user" and getattr(status, "started", False)
                    ),
                    "active_leases": getattr(status, "active_leases", 0),
                    "error": getattr(status, "error", None)
                    or (
                        "Connection unavailable. Refresh to check credentials and "
                        "storage-channel access; repair the connection and restart "
                        "TGDrive if needed."
                        if getattr(status, "last_error_type", None)
                        else None
                    ),
                    "error_code": getattr(status, "error_code", None),
                }
            )
        return result

    async def refresh_connections(self) -> dict[str, Any]:
        """Check saved credentials in isolated sessions; never block startup."""
        async with self.lock:
            if not self._check_tasks and time.monotonic() >= self._refresh_not_before:
                revision, api_id, api_hash, rows = await asyncio.to_thread(
                    self._desired
                )
                self._checks_revision = revision
                self._checks = {
                    row.id: {"connection_check": "queued"}
                    for row in rows
                    if row.enabled
                }
                self._refresh_not_before = time.monotonic() + 30
                gate = asyncio.Semaphore(2)
                for row in rows:
                    if not row.enabled:
                        continue
                    task = asyncio.create_task(
                        self._check_connection(revision, api_id, api_hash, row, gate)
                    )
                    self._check_tasks.add(task)
                    task.add_done_callback(self._check_tasks.discard)
        return await self.snapshot()

    async def _check_connection(self, revision, api_id, api_hash, row, gate):
        state = self._checks[row.id]
        name = row.runtime_name
        if name is None and row.kind == "bot":
            name = "storage-bot-" + row.credential.get_secret_value().split(":", 1)[0]
        try:
            deadline = 0.0
            for attempt in range(3):
                while True:
                    if name:
                        with suppress(Exception):
                            deadline = max(
                                deadline,
                                await asyncio.to_thread(
                                    self.identity_cache.retry_at, name
                                ),
                            )
                        retry_at = getattr(
                            getattr(self.runtime, "pool", None),
                            "account_retry_at",
                            None,
                        )
                        if callable(retry_at):
                            deadline = max(deadline, await retry_at(name))
                    if deadline <= time.time():
                        break
                    state.update(
                        connection_check="waiting", connection_retry_at=deadline
                    )
                    await asyncio.sleep(max(0, deadline - time.time()))
                async with gate:
                    current_revision, _, _, _ = await asyncio.to_thread(self._desired)
                    if current_revision != revision:
                        return
                    state.update(connection_check="checking", connection_retry_at=None)
                    try:
                        async with asyncio.timeout(30):
                            if self.verifier is verify_connection:
                                verified_name = await self.verifier(
                                    api_id,
                                    api_hash.get_secret_value(),
                                    row,
                                    self.settings.storage_channel,
                                    identity_cache=self.identity_cache,
                                    require_pin=False,
                                )
                            else:
                                verified_name = await self.verifier(
                                    api_id,
                                    api_hash.get_secret_value(),
                                    row,
                                    self.settings.storage_channel,
                                )
                        pool = getattr(self.runtime, "pool", None)
                        restore = getattr(pool, "restore_channel_access", None)
                        if (
                            callable(restore)
                            and revision == self.settings.managed_revision
                        ):
                            await restore(verified_name)
                        state.update(
                            connection_check="passed",
                            connection_check_error=None,
                            connection_checked_at=time.time(),
                        )
                        return
                    except Exception as error:
                        # Only our sanitized verifier messages may reach the browser.
                        detail = (
                            str(error)
                            if isinstance(error, SettingsProblem)
                            else ("Could not check this connection. Try again.")
                        )
                        delay = getattr(error, "retry_after", None)
                        limited = isinstance(delay, int) and delay > 0
                        deadline = time.time() + (
                            delay + 0.25 if limited else (5 if attempt == 0 else 15)
                        )
                        if limited and name:
                            with suppress(Exception):
                                await asyncio.to_thread(
                                    self.identity_cache.defer, name, deadline
                                )
                            defer = getattr(
                                getattr(self.runtime, "pool", None),
                                "defer_client",
                                None,
                            )
                            if callable(defer):
                                await defer(name, deadline)
                        if attempt == 2 or not (
                            limited
                            or getattr(error, "retryable", False)
                            or isinstance(
                                error, (TimeoutError, ConnectionError, OSError)
                            )
                        ):
                            state.update(
                                connection_check="failed",
                                connection_check_error=detail,
                                connection_checked_at=time.time(),
                                connection_retry_at=deadline if limited else None,
                            )
                            return
                        state.update(
                            connection_check="waiting",
                            connection_retry_at=deadline,
                            connection_check_error=None,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            state.update(
                connection_check="failed",
                connection_check_error="Could not check this connection. Try again.",
                connection_checked_at=time.time(),
                connection_retry_at=None,
            )

    async def close(self) -> None:
        tasks = list(self._check_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._check_tasks.clear()

    async def snapshot(self) -> dict[str, Any]:
        revision, api_id, _, rows = await asyncio.to_thread(self._desired)
        active = await self._active_snapshot(rows)
        return await asyncio.to_thread(self._snapshot, revision, api_id, rows, active)

    def _snapshot(
        self,
        revision: int,
        api_id: int,
        rows: list[ManagedClient],
        active: list[dict[str, Any]],
    ) -> dict[str, Any]:
        active_by_id = {row["id"]: row for row in active}
        clients = []
        for row in rows:
            live = active_by_id.get(row.id, {})
            if row.runtime_name and live.get("runtime_name") != row.runtime_name:
                live = {}  # A replacement account has not started yet.
            identity = {
                "display_name": live.get("display_name"),
                "username": live.get("username"),
            }
            identity = self._cached_names.get(
                row.runtime_name
                or live.get("runtime_name")
                or (
                    "storage-bot-" + row.credential.get_secret_value().split(":", 1)[0]
                    if row.kind == "bot"
                    else None
                ),
                identity,
            )
            clients.append(
                {
                    "id": row.id,
                    "label": row.label,
                    "kind": row.kind,
                    "enabled": row.enabled,
                    "configured": True,
                    "display_name": identity.get("display_name"),
                    "username": identity.get("username"),
                    "identity_refreshing": live.get("identity_refreshing", False),
                    "identity_retry_at": live.get("identity_retry_at"),
                    "connected": live.get("connected"),
                    "healthy": live.get("healthy", False),
                    "premium": live.get("premium", False),
                    "active_leases": live.get("active_leases", 0),
                    "error": live.get("error"),
                    "error_code": live.get("error_code"),
                    **(
                        self._checks.get(row.id, {})
                        if row.enabled and revision == self._checks_revision
                        else {}
                    ),
                }
            )
        backup = getattr(self.runtime, "backup", None)
        store = getattr(self.runtime, "store", None)
        return {
            "revision": revision,
            "applied_revision": self.settings.managed_revision,
            "restart_required": revision != self.settings.managed_revision,
            "admission_paused": revision != self.settings.managed_revision,
            "application_mode": "restart",
            "api_id": api_id,
            "api_hash_configured": True,
            "storage_channel": self.settings.storage_channel,
            "clients": clients,
            "active_clients": active,
            "storage_channel_error": getattr(
                self.runtime, "storage_channel_error", None
            ),
            "stats": self._stats(),
            "recovery": backup.protection_status(store)
            if store is not None
            and callable(getattr(backup, "protection_status", None))
            else None,
        }

    def _stats(self) -> dict[str, Any]:
        store = getattr(self.runtime, "store", None)
        result = {
            "files": 0,
            "folders": 0,
            "total_bytes": 0,
            "trashed_files": 0,
            "trashed_bytes": 0,
        }
        if callable(getattr(store, "statistics", None)):
            result.update(store.statistics())
        registry = getattr(self.runtime, "registry", None) or getattr(
            getattr(self.runtime, "transfers", None), "registry", None
        )
        records = (
            [record.committed for record in registry.list()]
            if registry is not None
            else ()
        )
        counts = Counter(str(item.status) for item in records)
        result.update(
            active_jobs=counts["running"] + counts["cancelling"],
            queued_jobs=counts["queued"],
            failed_jobs=counts["error"],
            staging_budget_bytes=self.settings.staging_budget_bytes,
            staging_reserved_bytes=0,
            free_disk_bytes=None,
        )
        mongo = self.runtime.mongo
        finished = {
            row.id for row in records if str(row.status) in {"completed", "cancelled"}
        }
        result["staging_reserved_bytes"] = sum(
            row["data"]["total_size"]
            for row in mongo.db.receipts.find(
                {"data.state": {"$in": ["receiving", "queued", "cancelling"]}}
            )
            if row["_id"] not in finished
        )
        with suppress(OSError):
            result["free_disk_bytes"] = shutil.disk_usage(
                self.settings.resolved_cache_dir
            ).free
        return result

    async def _credential_repair(
        self, document: ManagedDocument, existing: list[ManagedClient], api_id, api_hash
    ) -> dict[str, str] | None:
        """Allow credential-only repairs without discarding pending sender journals."""
        if (
            document.api_id != api_id
            or document.api_hash != api_hash
            or [row.id for row in document.clients] != [row.id for row in existing]
        ):
            return None
        statuses = ()
        pool = getattr(self.runtime, "pool", None)
        if callable(getattr(pool, "status", None)):
            statuses = await pool.status()
        live_names = {
            row.id: status.name
            for row, status in zip(self.active, statuses, strict=False)
        }
        changed = {}
        for row, prior in zip(document.clients, existing, strict=True):
            if row.kind != prior.kind or row.enabled != prior.enabled:
                return None
            if row.credential == prior.credential:
                continue
            name = (
                "storage-bot-" + prior.credential.get_secret_value().split(":", 1)[0]
                if prior.kind == "bot"
                else prior.runtime_name or live_names.get(prior.id)
            )
            if not name or not name.startswith(("storage-bot-", "premium-user-")):
                return None
            changed[row.id] = name
        return changed or None

    async def save(self, change: SettingsChange) -> dict[str, Any]:
        async with self.lock:
            await self.close()
            self._checks.clear()
            self._refresh_not_before = 0
            revision, api_id, api_hash, existing = self._desired()
            if change.expected_revision != revision:
                raise SettingsProblem(
                    "Connection settings changed. Refresh before saving.",
                    code="SETTINGS_CONFLICT",
                    status=409,
                )
            known = {row.id: row for row in existing}
            rows = existing
            if change.clients is not None:
                rows = []
                for requested in change.clients:
                    prior = known.get(requested.id) if requested.id else None
                    if requested.id is not None and prior is None:
                        raise SettingsProblem("Unknown connection identifier")
                    if prior is not None and prior.kind != requested.kind:
                        raise SettingsProblem("A connection type cannot be changed")
                    credential = requested.credential or (
                        prior.credential if prior else None
                    )
                    if credential is None:
                        raise SettingsProblem("New connections require a credential")
                    rows.append(
                        ManagedClient(
                            id=prior.id if prior else secrets.token_hex(16),
                            label=requested.label,
                            kind=requested.kind,
                            enabled=requested.enabled,
                            credential=credential,
                            runtime_name=prior.runtime_name
                            if prior and requested.credential is None
                            else None,
                        )
                    )
            try:
                document = ManagedDocument(
                    revision=revision + 1,
                    api_id=change.api_id if change.api_id is not None else api_id,
                    api_hash=change.api_hash
                    if change.api_hash is not None
                    else api_hash,
                    clients=rows,
                )
            except ValidationError:
                raise SettingsProblem(
                    "Invalid connection settings. Check API credentials and keep "
                    "at least one enabled storage bot."
                ) from None
            document.private_bytes()  # Bound the file before any network work.
            # Validate the effective startup configuration before saving.
            effective = self.settings.model_dump(by_alias=True)
            effective.update(
                API_ID=document.api_id,
                API_HASH=document.api_hash,
                BOT_TOKENS=",".join(
                    row.credential.get_secret_value()
                    for row in rows
                    if row.kind == "bot" and row.enabled
                ),
                STRING_SESSIONS=",".join(
                    row.credential.get_secret_value()
                    for row in rows
                    if row.kind == "user" and row.enabled
                ),
            )
            try:
                Settings(_env_file=None, **effective)
            except ValidationError:
                raise SettingsProblem(
                    "These connections cannot satisfy the current configuration. "
                    "Check the configured storage credentials."
                ) from None
            repair = None
            try:
                self._ensure_idle()
            except SettingsProblem:
                repair = await self._credential_repair(
                    document, existing, api_id, api_hash
                )
                if repair is None:
                    raise
            # Verify replacements before saving; retain unchanged connections
            # during a same-account repair. Running clients are not replaced here.
            gate = asyncio.Semaphore(2)

            async def verify(row):
                if not row.enabled:
                    return row.runtime_name
                if repair is not None and row.id not in repair:
                    return row.runtime_name
                async with gate:
                    if self.verifier is verify_connection:
                        return await verify_connection(
                            document.api_id,
                            document.api_hash.get_secret_value(),
                            row,
                            self.settings.storage_channel,
                            identity_cache=self.identity_cache,
                            require_pin=False,
                        )
                    return await self.verifier(
                        document.api_id,
                        document.api_hash.get_secret_value(),
                        row,
                        self.settings.storage_channel,
                    )

            try:
                async with asyncio.timeout(90):
                    names = await asyncio.gather(
                        *(verify(row) for row in rows), return_exceptions=True
                    )
                    for result in names:
                        if isinstance(result, SettingsProblem):
                            raise result
                        if isinstance(result, BaseException):
                            raise SettingsProblem(
                                "Telegram verification failed. No changes were saved.",
                                code="SETTINGS_PREFLIGHT_FAILED",
                                status=503,
                            )
            except TimeoutError:
                raise SettingsProblem(
                    "Telegram verification timed out. No changes were saved.",
                    code="SETTINGS_PREFLIGHT_FAILED",
                    status=503,
                ) from None
            active_names = [
                name
                for row, name in zip(rows, names, strict=True)
                if row.enabled and name is not None
            ]
            if len(set(active_names)) != len(active_names):
                raise SettingsProblem("The same Telegram account cannot be added twice")
            for row, name in zip(rows, names, strict=True):
                if repair is not None and row.id in repair and name != repair[row.id]:
                    raise SettingsProblem(
                        "Pending transfers require the same Telegram account. "
                        "Replace the token or session for that account; changing "
                        "its identity requires finishing pending transfers first.",
                        code="SETTINGS_BUSY",
                        status=409,
                    )
                row.runtime_name = name
            # Build every fallible response field before the commit. In
            # particular, cancellation while collecting pool health must not
            # make an authenticated login revoke an already persisted session.
            active = await self._active_snapshot(rows)
            response = self._snapshot(document.revision, document.api_id, rows, active)
            if repair is None:
                self._ensure_idle()
            await asyncio.to_thread(
                save_document, self.settings.resolved_data_dir, document, revision
            )
            return response

    async def add_authenticated_session(
        self, expected_revision: int, label: str, session_string: str
    ) -> dict[str, Any]:
        revision, _, _, existing = self._desired()
        if revision != expected_revision:
            raise SettingsProblem(
                "Connection settings changed. Refresh before saving.",
                code="SETTINGS_CONFLICT",
                status=409,
            )
        clients = [
            ClientChange(id=row.id, label=row.label, kind=row.kind, enabled=row.enabled)
            for row in existing
        ]
        clients.append(
            ClientChange(label=label, kind="user", credential=SecretStr(session_string))
        )
        return await self.save(
            SettingsChange(expected_revision=revision, clients=clients)
        )


def get_connection_manager(settings, runtime) -> ConnectionManager:
    manager = getattr(runtime, "connection_manager", None)
    if manager is None:
        manager = ConnectionManager(settings, runtime)
        runtime.connection_manager = manager
    return manager
