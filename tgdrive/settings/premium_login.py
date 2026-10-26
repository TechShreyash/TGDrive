"""Short-lived owner-bound Telegram login without browser session exports."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

LOGIN_TTL = 300
MAX_CHALLENGES = 2
MAX_ATTEMPTS = 5
# Telegram RPC diagnostics can contain authentication input. This dedicated
# transport logger intentionally has no output even when application DEBUG is on.
_AUTH_LOG = logging.getLogger("tgdrive.private_telegram_login")
_AUTH_LOG.addHandler(logging.NullHandler())
_AUTH_LOG.propagate = False
_AUTH_LOG.setLevel(logging.CRITICAL + 1)


class PremiumLoginProblem(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        code: str = "TELEGRAM_LOGIN_FAILED",
        status: int = 422,
        retry_after: int | None = None,
    ):
        super().__init__(detail)
        self.code, self.status, self.retry_after = code, status, retry_after


@dataclass(repr=False)
class _Challenge:
    owner: str
    client: Any
    phone: str
    label: str
    revision: int
    expires: float
    phone_code_hash: str = ""
    stage: str = "code"
    attempts: int = 0
    authorized: bool = False
    saved: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup: asyncio.Task | None = None


def _new_client(api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        receive_updates=False,
        request_retries=1,
        connection_retries=1,
        flood_sleep_threshold=0,
        auto_reconnect=False,
        base_logger=_AUTH_LOG,
        device_model="TGDrive web connection",
    )


class PremiumLoginService:
    def __init__(
        self,
        api_credentials: Callable[[], tuple[int, Any, int]],
        save_session: Callable[[int, str, str], Awaitable[dict[str, Any]]],
        *,
        client_factory: Callable[[int, str], Any] = _new_client,
        clock: Callable[[], float] = time.monotonic,
        is_session_saved: Callable[[str], bool | None] | None = None,
    ):
        self.api_credentials = api_credentials
        self.save_session = save_session
        self.client_factory, self.clock = client_factory, clock
        self.is_session_saved = is_session_saved
        self.challenges: dict[str, _Challenge] = {}
        self._starts: deque[float] = deque(maxlen=6)
        self._phones: deque[tuple[str, float]] = deque(maxlen=32)
        self._cooldown = 0.0
        self._guard = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self._closed = False
        self._operations: set[asyncio.Task] = set()
        self._cleanups: set[asyncio.Task] = set()

    @asynccontextmanager
    async def _operation(self):
        if self._closed:
            raise PremiumLoginProblem(
                "Login is unavailable while TGDrive stops.", status=503
            )
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            yield
        finally:
            self._operations.discard(task)

    async def _dispose(self, challenge: _Challenge) -> None:
        # Cleanup owns its own bounded task: a disconnected HTTP request or
        # cancelled reaper must not abandon an authorization after map removal.
        task = challenge.cleanup
        if task is None:
            task = asyncio.create_task(
                self._cleanup(challenge), name="telegram-login-cleanup"
            )
            challenge.cleanup = task
            self._cleanups.add(task)
            task.add_done_callback(self._cleanups.discard)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        task.result()

    async def _cleanup(self, challenge: _Challenge) -> None:
        # All challenges own a fresh session. Even a lost sign-in reply may
        # have authorized it remotely, so try logout for every unsaved session.
        # Never revoke a session whose settings save committed or is ambiguous.
        try:
            if not challenge.saved:
                with suppress(Exception):
                    async with asyncio.timeout(5):
                        await challenge.client.log_out()
        finally:
            with suppress(Exception):
                async with asyncio.timeout(5):
                    await challenge.client.disconnect()

    async def _expire(self) -> None:
        for identity, challenge in list(self.challenges.items()):
            if challenge.expires <= self.clock() and not challenge.lock.locked():
                self.challenges.pop(identity, None)
                await self._dispose(challenge)

    async def _reap(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(15)
                await self._expire()
        except asyncio.CancelledError:
            return

    def _check_rate(self) -> None:
        now = self.clock()
        if self._cooldown > now:
            raise PremiumLoginProblem(
                "Telegram asks you to wait before trying again.",
                code="TELEGRAM_LOGIN_WAIT",
                status=429,
                retry_after=max(1, int(self._cooldown - now + 1)),
            )

    def _flood(self, error: FloodWaitError) -> PremiumLoginProblem:
        seconds = max(1, min(86400, int(error.seconds)))
        self._cooldown = max(self._cooldown, self.clock() + seconds)
        return PremiumLoginProblem(
            "Telegram asks you to wait before trying again.",
            code="TELEGRAM_LOGIN_WAIT",
            status=429,
            retry_after=seconds,
        )

    def _result(self, identity: str, challenge: _Challenge) -> dict[str, Any]:
        return {
            "challenge_id": identity,
            "stage": challenge.stage,
            "expires_in_seconds": max(0, int(challenge.expires - self.clock())),
        }

    async def begin(
        self,
        owner: str,
        expected_revision: int,
        phone: str,
        label: str,
    ) -> dict[str, Any]:
        async with self._operation():
            return await self._begin(owner, expected_revision, phone, label)

    async def _begin(
        self,
        owner: str,
        expected_revision: int,
        phone: str,
        label: str,
    ) -> dict[str, Any]:
        phone = re.sub(r"[ ()-]", "", phone)
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise PremiumLoginProblem("Enter your phone number with its country code.")
        label = label.strip()
        if not 1 <= len(label) <= 80 or any(
            ord(char) < 32 or ord(char) == 127 for char in label
        ):
            raise PremiumLoginProblem(
                "Choose a connection name of 1-80 visible characters."
            )
        await self._expire()
        async with self._guard:
            if self._closed:
                raise PremiumLoginProblem(
                    "Login is unavailable while TGDrive stops.", status=503
                )
            self._check_rate()
            now = self.clock()
            while self._starts and self._starts[0] <= now - 900:
                self._starts.popleft()
            if len(self._starts) >= 6 or len(self.challenges) >= MAX_CHALLENGES:
                raise PremiumLoginProblem(
                    "Finish or cancel the current login "
                    "before requesting another code.",
                    status=429,
                    retry_after=60,
                )
            digest = hashlib.sha256(phone.encode()).hexdigest()
            if any(key == digest and stamp > now - 60 for key, stamp in self._phones):
                raise PremiumLoginProblem(
                    "Wait a minute before requesting another code.",
                    status=429,
                    retry_after=60,
                )
            api_id, api_hash, revision = self.api_credentials()
            if expected_revision != revision:
                raise PremiumLoginProblem(
                    "Settings changed. Reload settings before connecting this account.",
                    code="SETTINGS_CONFLICT",
                    status=409,
                )
            raw_hash = (
                api_hash.get_secret_value()
                if hasattr(api_hash, "get_secret_value")
                else api_hash
            )
            client = self.client_factory(api_id, raw_hash)
            challenge = _Challenge(
                owner, client, phone, label, revision, now + LOGIN_TTL
            )
            identity = secrets.token_urlsafe(32)
            self.challenges[identity] = challenge
            self._starts.append(now)
            self._phones.append((digest, now))
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap())
        try:
            async with challenge.lock, asyncio.timeout(30):
                await client.connect()
                sent = await client.send_code_request(phone)
                code_hash = getattr(sent, "phone_code_hash", None)
                if not isinstance(code_hash, str) or not code_hash:
                    raise PremiumLoginProblem(
                        "Telegram could not send a login code. Try again later."
                    )
                challenge.phone_code_hash = code_hash
                return self._result(identity, challenge)
        except BaseException as error:
            self.challenges.pop(identity, None)
            await self._dispose(challenge)
            if isinstance(error, asyncio.CancelledError):
                raise
            if isinstance(error, FloodWaitError):
                raise self._flood(error) from None
            if isinstance(error, PhoneNumberInvalidError):
                raise PremiumLoginProblem(
                    "Telegram did not recognize this phone number."
                ) from None
            if isinstance(error, PremiumLoginProblem):
                raise
            raise PremiumLoginProblem(
                "Telegram could not send a login code. "
                "Check your API settings and try again.",
                status=502,
            ) from None

    def _owned(self, identity: str, owner: str) -> _Challenge:
        challenge = self.challenges.get(identity)
        if challenge is None or challenge.owner != owner:
            raise PremiumLoginProblem(
                "This login expired. Start again.",
                code="TELEGRAM_LOGIN_EXPIRED",
                status=410,
            )
        return challenge

    async def verify(
        self,
        owner: str,
        identity: str,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        async with self._operation():
            return await self._verify(owner, identity, code=code, password=password)

    async def _verify(
        self,
        owner: str,
        identity: str,
        *,
        code: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        await self._expire()
        challenge = self._owned(identity, owner)
        async with challenge.lock:
            self._owned(identity, owner)
            self._check_rate()
            if challenge.expires <= self.clock():
                self.challenges.pop(identity, None)
                await self._dispose(challenge)
                raise PremiumLoginProblem(
                    "This login expired. Start again.",
                    code="TELEGRAM_LOGIN_EXPIRED",
                    status=410,
                )
            if challenge.stage == "code" and (
                code is None
                or not re.fullmatch(r"[0-9]{3,10}", code)
                or password is not None
            ):
                raise PremiumLoginProblem("Enter the code from Telegram.")
            if challenge.stage == "password" and (
                password is None or not 1 <= len(password) <= 512 or code is not None
            ):
                raise PremiumLoginProblem(
                    "Enter your Telegram two-step verification password."
                )
            challenge.attempts += 1
            try:
                async with asyncio.timeout(90):
                    if challenge.stage == "code":
                        await challenge.client.sign_in(
                            phone=challenge.phone,
                            code=code,
                            phone_code_hash=challenge.phone_code_hash,
                        )
                    else:
                        await challenge.client.sign_in(password=password)
                    challenge.authorized = True
                    account = await challenge.client.get_me()
                    if (
                        not account
                        or getattr(account, "bot", False)
                        or not getattr(account, "premium", False)
                    ):
                        raise PremiumLoginProblem(
                            "This account does not have Telegram Premium. "
                            "Your storage bots are still available.",
                            code="TELEGRAM_PREMIUM_REQUIRED",
                        )
                    session = challenge.client.session.save()
                    try:
                        result = await self.save_session(
                            challenge.revision, challenge.label, session
                        )
                    except BaseException:
                        if self.is_session_saved is not None:
                            try:
                                committed = self.is_session_saved(session)
                            except Exception:
                                committed = None
                            # An unreadable post-commit file cannot prove that
                            # nothing was saved. Preserve the authorization.
                            challenge.saved = committed is not False
                        raise
                    challenge.saved = True
                self.challenges.pop(identity, None)
                await self._dispose(challenge)
                return {"stage": "complete", "settings": result}
            except SessionPasswordNeededError:
                challenge.stage = "password"
                return self._result(identity, challenge)
            except PhoneCodeInvalidError, PasswordHashInvalidError:
                if challenge.attempts < MAX_ATTEMPTS:
                    raise PremiumLoginProblem(
                        "That code is incorrect. Try again."
                        if challenge.stage == "code"
                        else "That password is incorrect. Try again.",
                        code="TELEGRAM_LOGIN_INVALID",
                    ) from None
                self.challenges.pop(identity, None)
                await self._dispose(challenge)
                raise PremiumLoginProblem(
                    "Too many attempts. Start a new login.", status=429, retry_after=60
                ) from None
            except FloodWaitError as error:
                raise self._flood(error) from None
            except BaseException as error:
                self.challenges.pop(identity, None)
                await self._dispose(challenge)
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, PhoneCodeExpiredError):
                    raise PremiumLoginProblem(
                        "This login code expired. Start again.",
                        code="TELEGRAM_LOGIN_EXPIRED",
                        status=410,
                    ) from None
                if isinstance(error, PremiumLoginProblem) or (
                    hasattr(error, "code") and str(error.code).startswith("SETTINGS_")
                ):
                    raise
                raise PremiumLoginProblem(
                    "This account could not be connected. "
                    "Check its storage channel access and try again.",
                    status=502,
                ) from None

    async def cancel(self, owner: str, identity: str) -> None:
        challenge = self.challenges.get(identity)
        if challenge is None or challenge.owner != owner:
            return
        async with challenge.lock:
            if self.challenges.pop(identity, None) is not None:
                await self._dispose(challenge)

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        pending = {task for task in self._operations if task is not current}
        if self._reaper is not None and self._reaper is not current:
            pending.add(self._reaper)
        for task in pending:
            task.cancel()
        # Provider waits are interrupted before waiting for challenge locks.
        # Each owned cleanup remains bounded by its two five-second deadlines.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        remaining = list(self.challenges.values())
        self.challenges.clear()
        if remaining:
            await asyncio.gather(
                *(self._dispose(challenge) for challenge in remaining),
                return_exceptions=True,
            )
        if self._cleanups:
            await asyncio.gather(*self._cleanups, return_exceptions=True)
