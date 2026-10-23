"""Safe, actionable errors for Telegram accounts that need owner attention."""

from __future__ import annotations


class TelegramConnectionError(RuntimeError):
    def __init__(self, detail: str, code: str, *, client_name: str | None = None):
        super().__init__(detail)
        self.code = code
        self.client_name = client_name


_CREDENTIAL_ERRORS = {
    "AccessTokenExpiredError",
    "AccessTokenInvalidError",
    "AuthKeyUnregisteredError",
    "AuthKeyInvalidError",
    "AuthKeyDuplicatedError",
    "SessionRevokedError",
    "SessionExpiredError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "TokenExpiredError",
    "TokenInvalidError",
    "UnauthorizedError",
}
_CHANNEL_ERRORS = {
    "ChannelPrivateError",
    "ChannelInvalidError",
    "ChatAdminRequiredError",
    "UserNotParticipantError",
    "ChatWriteForbiddenError",
    "ChatForbiddenError",
    "UserBannedInChannelError",
}


def connection_problem(
    error: BaseException | None, kind: str, *, client_name: str | None = None
) -> TelegramConnectionError | None:
    """Classify provider failures without returning raw RPC text or credentials."""
    if isinstance(error, TelegramConnectionError):
        return error
    name = type(error).__name__
    account = "storage bot" if kind == "bot" else "Premium account"
    if name in _CREDENTIAL_ERRORS or getattr(error, "code", None) == 401:
        repair = (
            "replace its revoked or invalid bot token"
            if kind == "bot"
            else "sign in to the same Telegram account again"
        )
        return TelegramConnectionError(
            f"This {account} can no longer sign in. Open Connections and {repair}, "
            "then restart TGDrive and retry. Existing files and saved upload "
            "progress are preserved.",
            "TELEGRAM_CREDENTIALS_REVOKED",
            client_name=client_name,
        )
    if name in _CHANNEL_ERRORS:
        return TelegramConnectionError(
            f"This {account} lost access or required permissions to the storage "
            "channel. Restore its channel membership and administrator permissions, "
            "then open Connections, Refresh and retry. Existing files and saved "
            "upload progress are preserved.",
            "TELEGRAM_CHANNEL_ACCESS",
            client_name=client_name,
        )
    return None


def removed_sender(client_name: str) -> TelegramConnectionError:
    return TelegramConnectionError(
        "The Telegram account that started this upload is no longer configured. "
        "Reconnect the same account in Connections, restart TGDrive and retry. "
        "The saved upload is preserved; switching an already dispatched part to "
        "another account could create a duplicate.",
        "TELEGRAM_ACCOUNT_REMOVED",
        client_name=client_name,
    )
