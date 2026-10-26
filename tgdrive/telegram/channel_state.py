"""Read-only checks for a private, durable Telegram storage channel."""

from __future__ import annotations

from typing import Any


def storage_channel_problem(chat: Any, full_chat: Any) -> tuple[str, str] | None:
    """Return safe setup guidance without exposing channel identifiers."""
    if not getattr(chat, "broadcast", False):
        return (
            "CHANNEL_TYPE",
            "Use a dedicated Telegram channel, rather than a group.",
        )
    usernames = getattr(chat, "usernames", None) or ()
    if getattr(chat, "username", None) or any(
        getattr(username, "active", False) for username in usernames
    ):
        return (
            "CHANNEL_PUBLIC",
            "The storage channel is public. In Telegram, change Channel Type "
            "to Private and remove active public usernames before continuing.",
        )
    if getattr(full_chat, "ttl_period", None):
        return (
            "CHANNEL_AUTO_DELETE",
            "Telegram auto-delete is enabled for the storage channel. Turn "
            "auto-delete off before continuing so stored files are retained.",
        )
    return None


def storage_bot_rights_problem(
    chat: Any, *, require_pin: bool
) -> tuple[str, str] | None:
    """Every storage bot can publish and clean up any pooled bot's messages."""
    rights = getattr(chat, "admin_rights", None)
    required = ("post_messages", "delete_messages")
    if require_pin:
        required += ("edit_messages",)
    if rights is None or not all(getattr(rights, key, False) for key in required):
        return (
            "CHANNEL_RIGHTS",
            "Every storage bot must be a channel administrator with Post Messages "
            "and Delete Messages enabled."
            + (
                " Also enable Edit Messages so TGDrive can pin its recovery "
                "checkpoints."
                if require_pin
                else ""
            ),
        )
    return None
