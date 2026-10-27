"""Lifecycle-safe leasing for Telegram bot and user clients."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from tgdrive.media.media_scheduler import (
    MediaPriority,
    MediaPriorityScope,
    effective_priority,
)
from tgdrive.telegram.connection_errors import (
    TelegramConnectionError,
    connection_problem,
    removed_sender,
)

if TYPE_CHECKING:
    from tgdrive.core.config import Settings

type ClientKind = Literal["bot", "user"]


class TelegramClientLifecycle(Protocol):
    """The small portion of the Telegram client lifecycle used by the pool."""

    def start(self) -> Any | Awaitable[Any]: ...

    def stop(self) -> Any | Awaitable[Any]: ...


type ClientFactory = Callable[
    [], TelegramClientLifecycle | Awaitable[TelegramClientLifecycle]
]
type ClientSource = TelegramClientLifecycle | ClientFactory


class PoolState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ClientPoolError(RuntimeError):
    """Base class for client-pool failures."""


class ClientPoolNotRunning(ClientPoolError):
    """A lease was requested while the pool was not accepting work."""


class NoClientAvailable(ClientPoolError):
    """No started, healthy client satisfies a lease request."""


class MediaQueueFull(NoClientAvailable):
    """Retryable origin overload; cached delivery does not enter this queue."""

    code = "TELEGRAM_QUEUE_FULL"

    def __init__(self, retry_after: int = 1) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__("Telegram origin queue is full; retry shortly")


class DuplicateClientIdentity(ClientPoolError):
    """A second configured connection resolves to an already pooled account."""


class NoPremiumClientAvailable(NoClientAvailable):
    """A premium lease was requested without a usable user client."""


class ClientPoolStartError(ClientPoolError):
    """One or more clients failed to start.

    All clients that were materialized are asked to stop before this exception
    is raised.  The original exceptions remain available in ``failures``.
    """

    def __init__(
        self,
        failures: tuple[tuple[str, BaseException], ...],
        cleanup_failures: tuple[tuple[str, BaseException], ...] = (),
    ) -> None:
        self.failures = failures
        self.cleanup_failures = cleanup_failures
        labels = ", ".join(
            f"{name} ({type(error).__name__})" for name, error in failures
        )
        message = f"failed to start client pool: {labels}"
        if cleanup_failures:
            message += f"; {len(cleanup_failures)} cleanup operation(s) also failed"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ClientStatus:
    name: str
    kind: ClientKind
    started: bool
    healthy: bool
    connected: bool | None
    active_leases: int
    last_error_type: str | None
    display_name: str | None = None
    username: str | None = None
    identity_refreshing: bool = False
    identity_retry_at: float | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PoolHealth:
    state: PoolState
    ready: bool
    total_clients: int
    started_clients: int
    healthy_clients: int
    active_leases: int
    bot_clients: int
    premium_clients: int


@dataclass(slots=True)
class _ClientSlot:
    source: ClientSource
    kind: ClientKind
    index: int
    is_factory: bool
    client: TelegramClientLifecycle | None = None
    started: bool = False
    healthy: bool = False
    active: int = 0
    selected_at: int = 0
    media_active: int = 0
    download_active: int = 0
    cooldown_until: float = 0
    error: BaseException | None = None
    needs_stop: bool = False

    @property
    def fallback_name(self) -> str:
        return f"{self.kind}-{self.index + 1}"

    @property
    def name(self) -> str:
        value = getattr(self.client, "name", None)
        return str(value) if value not in (None, "") else self.fallback_name


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ClientPool:
    """Own and fairly lease bot and premium-user clients.

    Sources may be already-created Telegram clients or zero-argument
    synchronous/asynchronous factories.  ``start`` and ``stop`` are
    idempotent; clients start and stop concurrently.
    """

    def __init__(
        self,
        bot_clients: Iterable[ClientSource] = (),
        user_clients: Iterable[ClientSource] = (),
        *,
        allow_partial_start: bool = False,
        media_send_interval: float = 0.0,
        uploads_per_bot: int = 1,
        media_cache_dir: Any = None,
        media_per_bot: int = 3,
        max_media_queue: int = 256,
    ) -> None:
        self._allow_partial_start = allow_partial_start
        self.media_cache_dir = media_cache_dir
        self._media_read_cache: Any = None
        self._validate_upload_limit(uploads_per_bot)
        self._uploads_per_bot = uploads_per_bot
        self._validate_media_limit(media_per_bot)
        self._media_per_bot = media_per_bot
        self._validate_queue_limit(max_media_queue)
        self._max_media_queue = max_media_queue
        self._origin_waiters: list[dict[str, Any]] = []
        self._origin_active: dict[object, dict[str, Any]] = {}
        self._origin_sequence = 0
        self._origin_durations: deque[float] = deque(maxlen=128)
        self._load_history: deque[dict[str, Any]] = deque(maxlen=120)
        self._last_load_sample = 0.0
        self._slots: list[_ClientSlot] = []
        for kind, sources in (("bot", bot_clients), ("user", user_clients)):
            for index, source in enumerate(sources):
                is_factory = callable(source) and not callable(
                    getattr(source, "start", None)
                )
                self._slots.append(
                    _ClientSlot(
                        source=source,
                        kind=kind,
                        index=index,
                        is_factory=is_factory,
                        client=None if is_factory else source,
                    )
                )

        self._state = PoolState.NEW
        self._lifecycle_lock = asyncio.Lock()
        self._leases = asyncio.Condition()
        self._selection_counter = 0
        self._send_interval = max(0.0, media_send_interval)
        self._send_lock = asyncio.Lock()
        self._next_send = 0.0
        self._channel_cooldown_until = 0.0
        self._account_cooldowns: dict[str, float] = {}

    @staticmethod
    def _validate_upload_limit(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 4:
            raise ValueError("Uploads per bot must be between 1 and 4")

    @property
    def uploads_per_bot(self) -> int:
        return self._uploads_per_bot

    @staticmethod
    def _validate_media_limit(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 32:
            raise ValueError("Combined media activity must be between 1 and 32")

    @property
    def media_per_bot(self) -> int:
        return self._media_per_bot

    async def set_media_per_bot(self, value: int) -> None:
        self._validate_media_limit(value)
        async with self._leases:
            self._media_per_bot = value
            self._leases.notify_all()

    @staticmethod
    def _validate_queue_limit(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 4096:
            raise ValueError("Telegram queue limit must be between 1 and 4096")

    async def set_media_queue_limit(self, value: int) -> None:
        self._validate_queue_limit(value)
        async with self._leases:
            self._max_media_queue = value
            self._leases.notify_all()

    async def set_uploads_per_bot(self, value: int) -> None:
        """Change admission without cancelling leases already in progress."""
        self._validate_upload_limit(value)
        async with self._leases:
            self._uploads_per_bot = value
            self._leases.notify_all()

    @property
    def state(self) -> PoolState:
        return self._state

    async def __aenter__(self) -> ClientPool:
        return await self.start()

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.stop()

    async def start(self) -> ClientPool:
        """Materialize and concurrently start every configured client."""

        async with self._lifecycle_lock:
            if self._state is PoolState.RUNNING:
                return self

            self._state = PoolState.STARTING
            for slot in self._slots:
                slot.error = None
                slot.healthy = False

            try:
                results = await asyncio.gather(
                    *(self._start_slot(slot) for slot in self._slots),
                    return_exceptions=True,
                )
            except BaseException:
                await self._stop_materialized()
                self._state = PoolState.FAILED
                raise

            failures = tuple(
                (slot.name, result)
                for slot, result in zip(self._slots, results, strict=True)
                if isinstance(result, BaseException)
            )
            if failures:
                usable_bot = any(
                    slot.kind == "bot" and slot.started and self._slot_is_healthy(slot)
                    for slot in self._slots
                )
                if self._allow_partial_start and usable_bot:
                    # Optional accounts must not take working storage bots down.
                    # Failed clients may have opened connections before failing.
                    await asyncio.gather(
                        *(
                            self._stop_slot(slot)
                            for slot, result in zip(self._slots, results, strict=True)
                            if isinstance(result, BaseException) and slot.needs_stop
                        ),
                        return_exceptions=True,
                    )
                else:
                    cleanup_failures = await self._stop_materialized()
                    self._state = PoolState.FAILED
                    raise ClientPoolStartError(failures, cleanup_failures)

            seen: dict[str, _ClientSlot] = {}
            for slot in self._slots:
                if not slot.started or not self._slot_is_healthy(slot):
                    continue
                previous = seen.get(slot.name)
                if previous is None:
                    seen[slot.name] = slot
                    continue
                error = DuplicateClientIdentity(
                    "This Telegram account already has a usable connection"
                )
                if slot.client is previous.client:
                    # A shared instance must only be stopped by its owning slot.
                    slot.started = False
                    slot.needs_stop = False
                else:
                    # Failed disconnects remain unhealthy and are retried at stop.
                    await asyncio.gather(self._stop_slot(slot), return_exceptions=True)
                slot.healthy = False
                slot.error = error

            self._state = PoolState.RUNNING
            return self

    async def stop(
        self,
        *,
        drain_timeout: float | None = None,
    ) -> tuple[tuple[str, BaseException], ...]:
        """Stop accepting leases, drain active leases, and stop all clients.

        The returned tuple contains any stop errors after every client has
        been attempted.  Repeated calls after shutdown are no-ops.
        """

        async with self._lifecycle_lock:
            if self._state in (PoolState.NEW, PoolState.STOPPED):
                self._state = PoolState.STOPPED
                return ()

            previous_state = self._state
            self._state = PoolState.STOPPING
            async with self._leases:
                self._leases.notify_all()
            try:
                wait_for_leases = self._wait_for_all_leases()
                if drain_timeout is None:
                    await wait_for_leases
                else:
                    await asyncio.wait_for(wait_for_leases, timeout=drain_timeout)
            except TimeoutError:
                self._state = (
                    PoolState.RUNNING
                    if previous_state is PoolState.RUNNING
                    else previous_state
                )
                raise

            failures = await self._stop_materialized()
            self._state = PoolState.STOPPED
            return failures

    @asynccontextmanager
    async def lease(
        self,
        *,
        premium: bool = False,
        premium_required: bool | None = None,
        prefer_premium: bool = False,
        client_name: str | None = None,
        media: bool = False,
        allow_users: bool = False,
        download: bool = False,
        respect_cooldown: bool = False,
        priority: MediaPriority | MediaPriorityScope = "demand",
        request_id: str | None = None,
    ) -> AsyncIterator[TelegramClientLifecycle]:
        """Lease a healthy client and always release its count.

        premium requires a user account, as for larger uploads.
        prefer_premium first considers healthy users and otherwise bots,
        so reads continue when no usable optional user is present.
        allow_users admits both account kinds for bot-compatible media parts;
        with prefer_premium, ready user slots are selected before bot slots.
        premium-required documents remain restricted to user accounts.
        """

        if effective_priority(priority) not in ("demand", "preload"):
            raise ValueError("Unknown media priority")
        if premium_required is not None:
            premium = premium_required
        kind: ClientKind = "user" if premium else "bot"

        async with self._leases:
            ticket = object()
            unsubscribe = None
            if media or download:
                if len(self._origin_waiters) >= self._max_media_queue:
                    mean = (
                        sum(self._origin_durations) / len(self._origin_durations)
                        if self._origin_durations
                        else 1.0
                    )
                    raise MediaQueueFull(retry_after=int(mean) + 1)
                self._origin_sequence += 1
                self._origin_waiters.append(
                    dict(
                        ticket=ticket,
                        kind=None if allow_users and not premium else kind,
                        name=client_name,
                        priority=effective_priority(priority),
                        download=download,
                        queued_at=time.monotonic(),
                        request_id=request_id or f"media-{self._origin_sequence}",
                    )
                )
                if isinstance(priority, MediaPriorityScope):
                    own = self._origin_waiters[-1]

                    def promote_waiter():
                        # Its demand arrival is now, not when background work
                        # originally queued. Keep existing demand ahead of it.
                        own["priority"] = "demand"
                        self._origin_waiters.remove(own)
                        self._origin_waiters.append(own)

                    unsubscribe = priority.subscribe(
                        self._leases,
                        promote_waiter,
                    )
            try:
                while True:
                    if self._state is not PoolState.RUNNING:
                        raise ClientPoolNotRunning(
                            f"client pool is {self._state.value}; not accepting leases"
                        )

                    def available(candidate_kind: ClientKind) -> list[_ClientSlot]:
                        return [
                            slot
                            for slot in self._slots
                            if slot.kind == candidate_kind
                            and slot.client is not None
                            and slot.started
                            and self._slot_is_healthy(slot)
                            and (client_name is None or slot.name == client_name)
                        ]

                    candidates = available(kind)
                    if allow_users and not premium:
                        candidates += available("user")
                    elif prefer_premium and not premium:
                        preferred = available("user")
                        if download or respect_cooldown:
                            # A cooling Premium account must not strand idle bots.
                            now = time.time()
                            ready_users = [
                                slot for slot in preferred if slot.cooldown_until <= now
                            ]
                            ready_bots = [
                                slot
                                for slot in candidates
                                if slot.cooldown_until <= now
                            ]
                            candidates = (
                                ready_users or ready_bots or (preferred + candidates)
                            )
                        else:
                            candidates = preferred or candidates
                    if not candidates:
                        matching = [
                            slot
                            for slot in self._slots
                            if (client_name is None or slot.name == client_name)
                            and (slot.kind == kind or (allow_users and not premium))
                        ]
                        if client_name is not None and not matching:
                            raise removed_sender(client_name)
                        for unavailable in matching:
                            problem = connection_problem(
                                unavailable.error,
                                unavailable.kind,
                                client_name=unavailable.name,
                            )
                            if problem is not None:
                                raise problem
                        if premium and not any(
                            slot.kind == kind for slot in self._slots
                        ):
                            raise NoPremiumClientAvailable(
                                "no premium user clients are configured"
                            )
                        raise NoClientAvailable(
                            "no healthy matching Telegram clients are available"
                        )
                    now = time.time()

                    def earlier_origin_for(candidate: _ClientSlot, now=now) -> bool:
                        own_rank = effective_priority(priority) == "preload"
                        for queued in sorted(
                            self._origin_waiters,
                            key=lambda waiter: waiter["priority"] == "preload",
                        ):
                            if queued["ticket"] is ticket:
                                return False
                            if (queued["priority"] == "preload") > own_rank:
                                continue
                            if queued["kind"] not in (None, candidate.kind) or (
                                queued["name"] is not None
                                and queued["name"] != candidate.name
                            ):
                                continue
                            # A full upload lane must not strand a free read lane.
                            if queued["download"]:
                                if (
                                    candidate.download_active < self._media_per_bot
                                    and candidate.cooldown_until <= now
                                ):
                                    return True
                            elif (
                                candidate.media_active
                                < (
                                    self._uploads_per_bot
                                    if candidate.kind == "bot"
                                    else 1
                                )
                                and max(
                                    candidate.cooldown_until,
                                    self._channel_cooldown_until,
                                )
                                <= now
                            ):
                                return True
                        return False

                    eligible = [
                        slot
                        for slot in candidates
                        if (not download or slot.download_active < self._media_per_bot)
                        and (
                            not (download or media)
                            or (
                                slot.media_active + slot.download_active
                                < self._media_per_bot
                                and not earlier_origin_for(slot)
                            )
                        )
                        and (
                            not (download or respect_cooldown)
                            or slot.cooldown_until <= now
                        )
                        and (
                            not media
                            or (
                                slot.media_active
                                < (self._uploads_per_bot if slot.kind == "bot" else 1)
                                and max(
                                    slot.cooldown_until, self._channel_cooldown_until
                                )
                                <= now
                            )
                        )
                    ]
                    if eligible:
                        slot = min(
                            eligible,
                            key=lambda item: (
                                prefer_premium and item.kind != "user",
                                item.download_active
                                if download
                                else item.media_active
                                if media
                                else item.active,
                                item.selected_at,
                                item.index,
                            ),
                        )
                        self._selection_counter += 1
                        slot.selected_at = self._selection_counter
                        slot.active += 1
                        if download:
                            slot.download_active += 1
                        if media:
                            slot.media_active += 1
                        client = slot.client
                        if media or download:
                            own = next(
                                row
                                for row in self._origin_waiters
                                if row["ticket"] is ticket
                            )
                            self._origin_active[ticket] = dict(
                                request_id=own["request_id"],
                                name=slot.name,
                                priority=effective_priority(priority),
                                operation="download" if download else "upload",
                                started_at=time.monotonic(),
                            )
                            if download:
                                # Rotate read-ahead behind other waiting requests.
                                self._origin_waiters.sort(
                                    key=lambda row: (
                                        row["download"]
                                        and row["request_id"] == own["request_id"]
                                        and row["priority"] == own["priority"]
                                    )
                                )
                        break
                    deadlines = [
                        max(
                            slot.cooldown_until,
                            self._channel_cooldown_until if media else 0,
                        )
                        for slot in candidates
                        if max(
                            slot.cooldown_until,
                            self._channel_cooldown_until if media else 0,
                        )
                        > now
                    ]
                    timeout = max(0.001, min(deadlines) - now) if deadlines else None
                    try:
                        if timeout is None:
                            await self._leases.wait()
                        else:
                            await asyncio.wait_for(self._leases.wait(), timeout)
                    except TimeoutError:
                        pass
            finally:
                if unsubscribe is not None:
                    unsubscribe()
                if media or download:
                    self._origin_waiters = [
                        row
                        for row in self._origin_waiters
                        if row["ticket"] is not ticket
                    ]
                    self._leases.notify_all()

        started = time.monotonic()
        try:
            yield client
        except Exception as error:
            problem = await self.report_failure(client, error)
            if problem is not None:
                raise problem from None
            raise
        finally:
            async with self._leases:
                if slot.active <= 0:
                    raise RuntimeError("client lease accounting underflow")
                slot.active -= 1
                if download:
                    slot.download_active -= 1
                if media:
                    slot.media_active -= 1
                if media or download:
                    self._origin_active.pop(ticket, None)
                    self._origin_durations.append(time.monotonic() - started)
                self._leases.notify_all()

    async def download_capacity(self) -> int:
        """Potential read windows; upload/read admission shares the media ceiling."""
        async with self._leases:
            return sum(
                self._media_per_bot
                for slot in self._slots
                if slot.started and self._slot_is_healthy(slot)
            )

    def performance_snapshot(self) -> dict[str, Any]:
        """Event-loop-local, bounded observations; queue ETAs are estimates."""
        now = time.monotonic()
        mean = (
            sum(self._origin_durations) / len(self._origin_durations)
            if self._origin_durations
            else None
        )
        queue = sorted(
            self._origin_waiters, key=lambda row: row["priority"] == "preload"
        )
        accounts = []
        for slot in self._slots:
            scheduler = getattr(slot.client, "_media_scheduler", None)
            accounts.append(
                dict(
                    name=slot.name,
                    kind=slot.kind,
                    uploads=slot.media_active,
                    downloads=slot.download_active,
                    combined_limit=self._media_per_bot,
                    cooldown_seconds=max(0.0, slot.cooldown_until - time.time()),
                    requests=scheduler.snapshot() if scheduler is not None else None,
                )
            )
        sample = dict(
            at=time.time(),
            queued=len(queue),
            active=sum(
                slot.media_active + slot.download_active for slot in self._slots
            ),
        )
        wall_now = sample["at"]
        if (
            self._load_history
            and not 0 <= wall_now - self._load_history[-1]["at"] <= 30
        ):
            self._load_history.clear()
            self._last_load_sample = 0
        while self._load_history and self._load_history[0]["at"] < wall_now - 300:
            self._load_history.popleft()
        if now - self._last_load_sample >= 1:
            self._load_history.append(sample)
            self._last_load_sample = now
        return dict(
            **sample,
            max_queue=self._max_media_queue,
            estimates=True,
            mean_activity_seconds=mean,
            queue=[
                dict(
                    position=index + 1,
                    priority=row["priority"],
                    request_id=row["request_id"],
                    operation="download" if row["download"] else "upload",
                    client_name=row["name"],
                    waiting_seconds=round(now - row["queued_at"], 3),
                    estimated_wait_seconds=self._estimate_origin_wait(row, index, mean),
                )
                for index, row in enumerate(queue[:128])
            ],
            accounts=accounts,
            active_requests=[
                dict(
                    request_id=row["request_id"],
                    client_name=row["name"],
                    priority=row["priority"],
                    operation=row["operation"],
                    elapsed_seconds=round(now - row["started_at"], 3),
                )
                for row in list(self._origin_active.values())[:128]
            ],
            history=list(self._load_history),
            history_window_seconds=300,
        )

    def _estimate_origin_wait(self, row, index, mean):
        candidates = [
            slot
            for slot in self._slots
            if slot.started
            and self._slot_is_healthy(slot)
            and row["kind"] in (None, slot.kind)
            and row["name"] in (None, slot.name)
        ]
        capacity = max(1, len(candidates) * self._media_per_bot)
        occupancy = sum(slot.media_active + slot.download_active for slot in candidates)
        channel_deadline = 0 if row["download"] else self._channel_cooldown_until
        cooldown = min(
            (
                max(
                    0, slot.cooldown_until - time.time(), channel_deadline - time.time()
                )
                for slot in candidates
            ),
            default=0.0,
        )
        return (
            cooldown + ((occupancy + index) // capacity) * mean
            if mean is not None
            else None
        )

    def queue_estimate(self, request_id: str) -> dict[str, Any] | None:
        """Find a request without exposing other visitors' queue identifiers."""
        return self.queue_estimates((request_id,)).get(request_id)

    def queue_estimates(self, request_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Share one queue observation across a batch of owner transfer snapshots."""
        now = time.monotonic()
        requested = set(request_ids)
        active = Counter(row["request_id"] for row in self._origin_active.values())
        queue = sorted(
            self._origin_waiters, key=lambda row: row["priority"] == "preload"
        )
        matches = {}
        counts = Counter()
        for index, row in enumerate(queue):
            identity = row["request_id"]
            if identity in requested:
                matches.setdefault(identity, (index, row))
                counts[identity] += 1
        mean = (
            sum(self._origin_durations) / len(self._origin_durations)
            if self._origin_durations
            else None
        )
        result = {}
        for identity in requested:
            if identity in matches:
                index, row = matches[identity]
                result[identity] = dict(
                    request_id=identity,
                    position=index + 1,
                    priority=row["priority"],
                    estimated_wait_seconds=self._estimate_origin_wait(row, index, mean),
                    waiting_seconds=round(now - row["queued_at"], 3),
                    active_operations=active.get(identity, 0),
                    queued_operations=counts[identity],
                    estimates=True,
                )
            elif identity in active:
                result[identity] = dict(
                    request_id=identity,
                    position=0,
                    estimated_wait_seconds=0.0,
                    active_operations=active[identity],
                    queued_operations=0,
                    estimates=True,
                )
        return result

    async def media_capacity(self, *, premium=False, allow_users=False) -> int:
        """Potential media slots; leases enforce occupancy, fairness and cooldowns.

        Include occupied/cooling slots so a coordinator does not permanently
        shrink when another transfer temporarily owns them.
        """
        async with self._leases:
            if self._state is not PoolState.RUNNING:
                return 0
            return sum(
                self._uploads_per_bot if slot.kind == "bot" else 1
                for slot in self._slots
                if slot.started
                and self._slot_is_healthy(slot)
                and (
                    slot.kind == "user"
                    if premium
                    else allow_users or slot.kind == "bot"
                )
            )

    async def defer_client(self, client_name: str, deadline: float) -> None:
        """Apply a persisted account FloodWait to every new media lease."""
        async with self._leases:
            self._account_cooldowns[client_name] = max(
                self._account_cooldowns.get(client_name, 0),
                deadline,
            )
            for slot in self._slots:
                if slot.name == client_name:
                    slot.cooldown_until = max(slot.cooldown_until, deadline)
            self._leases.notify_all()

    async def account_retry_at(self, client_name: str) -> float:
        """Share account waits with isolated owner-initiated connection checks."""
        async with self._leases:
            return max(
                self._account_cooldowns.get(client_name, 0),
                *(
                    slot.cooldown_until
                    for slot in self._slots
                    if slot.name == client_name
                ),
                0,
            )

    async def defer_channel(self, deadline: float) -> None:
        """Message-publishing FloodWait is shared across channel writers."""
        async with self._leases:
            self._channel_cooldown_until = max(self._channel_cooldown_until, deadline)
            self._leases.notify_all()

    async def pace_send(self, channel: int) -> None:
        """Conservative shared pacing; server FloodWait always takes precedence."""
        async with self._send_lock:
            while True:
                remaining = (
                    max(self._next_send, self._channel_cooldown_until) - time.time()
                )
                if remaining <= 0:
                    self._next_send = time.time() + self._send_interval
                    return
                await asyncio.sleep(remaining)

    def kind_of(self, client: TelegramClientLifecycle) -> ClientKind:
        """Return the configured account kind without exposing credentials."""

        return self._find_slot(client).kind

    async def mark_unhealthy(
        self,
        client: TelegramClientLifecycle,
        error: BaseException | None = None,
    ) -> None:
        """Exclude ``client`` from future leases until marked healthy again."""

        async with self._leases:
            slot = self._find_slot(client)
            slot.healthy = False
            slot.error = error
            self._leases.notify_all()

    async def report_failure(
        self, client: TelegramClientLifecycle, error: BaseException
    ) -> TelegramConnectionError | None:
        """Quarantine revoked credentials or channel access, leaving waits alone."""
        slot = self._find_slot(client)
        if (
            isinstance(error, TelegramConnectionError)
            and error.client_name is not None
            and error.client_name != slot.name
        ):
            return None  # A nested lease failed on another account.
        problem = connection_problem(error, slot.kind, client_name=slot.name)
        if problem is not None:
            await self.mark_unhealthy(client, problem)
        return problem

    async def restore_channel_access(self, name: str) -> None:
        """Re-enable a live account only after Connections verified its access."""
        async with self._leases:
            for slot in self._slots:
                if (
                    slot.name == name
                    and slot.started
                    and self._connected_state(slot.client) is not False
                    and isinstance(slot.error, TelegramConnectionError)
                    and slot.error.code == "TELEGRAM_CHANNEL_ACCESS"
                ):
                    slot.healthy = True
                    slot.error = None
            self._leases.notify_all()

    async def mark_healthy(self, client: TelegramClientLifecycle) -> None:
        async with self._leases:
            slot = self._find_slot(client)
            if not slot.started:
                raise ClientPoolError("cannot mark a stopped client healthy")
            slot.healthy = True
            slot.error = None
            self._leases.notify_all()

    async def status(self) -> tuple[ClientStatus, ...]:
        """Return a stable snapshot without exposing credential values."""

        async with self._leases:
            return tuple(
                ClientStatus(
                    name=slot.name,
                    kind=slot.kind,
                    started=slot.started,
                    healthy=self._slot_is_healthy(slot),
                    connected=self._connected_state(slot.client),
                    active_leases=slot.active,
                    last_error_type=(
                        type(slot.error).__name__ if slot.error is not None else None
                    ),
                    display_name=getattr(slot.client, "display_name", None),
                    username=getattr(slot.client, "username", None),
                    identity_refreshing=bool(
                        getattr(slot.client, "identity_refreshing", False)
                    ),
                    identity_retry_at=getattr(slot.client, "identity_retry_at", None),
                    error=str(problem)
                    if (
                        problem := connection_problem(
                            slot.error, slot.kind, client_name=slot.name
                        )
                    )
                    else None,
                    error_code=problem.code if problem else None,
                )
                for slot in self._slots
            )

    def refresh_identities(self) -> None:
        """Schedule optional lookups without delaying requests or replacing clients."""
        for slot in self._slots:
            refresh = getattr(slot.client, "refresh_identity", None)
            if slot.started and callable(refresh):
                refresh()

    async def health(self) -> PoolHealth:
        statuses = await self.status()
        started = sum(item.started for item in statuses)
        healthy = sum(item.healthy for item in statuses)
        total = len(statuses)
        return PoolHealth(
            state=self._state,
            ready=(
                self._state is PoolState.RUNNING
                and (
                    any(item.kind == "bot" and item.healthy for item in statuses)
                    if self._allow_partial_start
                    else total > 0 and started == total and healthy == total
                )
            ),
            total_clients=total,
            started_clients=started,
            healthy_clients=healthy,
            active_leases=sum(item.active_leases for item in statuses),
            bot_clients=sum(item.kind == "bot" for item in statuses),
            premium_clients=sum(item.kind == "user" for item in statuses),
        )

    async def _start_slot(self, slot: _ClientSlot) -> None:
        try:
            if slot.is_factory:
                slot.client = await _maybe_await(slot.source())  # type: ignore[operator]
            if slot.client is None:
                raise TypeError(f"{slot.fallback_name} factory returned None")
            slot.needs_stop = True

            starter = getattr(slot.client, "start", None)
            stopper = getattr(slot.client, "stop", None)
            if not callable(starter) or not callable(stopper):
                raise TypeError(
                    f"{slot.fallback_name} must provide callable start() and stop()"
                )

            binder = getattr(slot.client, "bind_send_gate", None)
            if callable(binder):
                binder(self.pace_send, self.defer_channel)
            identity_binder = getattr(slot.client, "bind_identity_pool", None)
            if callable(identity_binder):
                identity_binder(self)
            await _maybe_await(starter())
            slot.started = True
            slot.healthy = True
            slot.cooldown_until = max(
                slot.cooldown_until,
                self._account_cooldowns.get(slot.name, 0),
            )
            slot.error = None
        except BaseException as error:
            slot.error = error
            slot.healthy = False
            raise

    async def _stop_materialized(
        self,
    ) -> tuple[tuple[str, BaseException], ...]:
        slots = [
            slot for slot in self._slots if slot.client is not None and slot.needs_stop
        ]
        results = await asyncio.gather(
            *(self._stop_slot(slot) for slot in slots),
            return_exceptions=True,
        )
        return tuple(
            (slot.name, result)
            for slot, result in zip(slots, results, strict=True)
            if isinstance(result, BaseException)
        )

    async def _stop_slot(self, slot: _ClientSlot) -> None:
        client = slot.client
        stopped = False
        try:
            if client is not None:
                stopper = getattr(client, "stop", None)
                if callable(stopper):
                    await _maybe_await(stopper())
            stopped = True
        except BaseException as error:
            slot.error = error
            raise
        finally:
            slot.started = False
            slot.healthy = False
            slot.active = 0
            slot.media_active = 0
            if stopped:
                slot.needs_stop = False
            if slot.is_factory and stopped:
                slot.client = None

    async def _wait_for_all_leases(self) -> None:
        async with self._leases:
            await self._leases.wait_for(
                lambda: all(slot.active == 0 for slot in self._slots)
            )

    def _find_slot(self, client: TelegramClientLifecycle) -> _ClientSlot:
        for slot in self._slots:
            if slot.client is client:
                return slot
        raise ValueError("client does not belong to this pool")

    @staticmethod
    def _connected_state(client: TelegramClientLifecycle | None) -> bool | None:
        if client is None:
            return None
        connected = getattr(client, "is_connected", None)
        return connected if isinstance(connected, bool) else None

    def _slot_is_healthy(self, slot: _ClientSlot) -> bool:
        connected = self._connected_state(slot.client)
        return slot.healthy and connected is not False


async def current_upload_limit(settings: Settings, pool: Any) -> int:
    """Admit logical files independently of optional Premium availability."""

    return settings.upload_limit
