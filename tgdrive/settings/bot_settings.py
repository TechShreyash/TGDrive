"""Owner-managed bot upload destination, applied to the next import."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tgdrive.core.errors import (
    DuplicateNameError,
    NotDirectoryError,
    NotFoundError,
    TrashedItemError,
)
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.store import _normalize_path

DEFAULT_FOLDER_NAME = "TGDriveBotModeFiles"
SETTINGS_LOCATION = "/?view=settings&section=transfers#bot-mode"
_MISSING = (NotDirectoryError, NotFoundError, TrashedItemError)


class BotUploadPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    folder_path: str | None = Field(default=None, max_length=17000)
    site_url: str | None = Field(default=None, max_length=2048)

    @field_validator("folder_path")
    @classmethod
    def valid_folder(cls, value):
        return _normalize_path(value)[1] if value is not None else None

    @field_validator("site_url")
    @classmethod
    def valid_site(cls, value):
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(ord(char) < 33 for char in value)
        ):
            raise ValueError("Use the HTTP or HTTPS address of your TGDrive website")
        return value.rstrip("/")


def folder_label(store, path):
    store.get_directory(path)
    names = ["My Files"]
    parts = path.strip("/").split("/") if path != "/" else []
    for index in range(len(parts)):
        names.append(store.get_directory("/" + "/".join(parts[: index + 1]))["name"])
    label = " / ".join(names)
    return label if len(label) <= 600 else "…" + label[-599:]


class BotSettings:
    def __init__(self, settings, runtime):
        self.settings = settings
        self.runtime = runtime
        self.value = BotUploadPreferences()
        self._loaded = False
        self._lock = asyncio.Lock()

    def _read(self):
        backend = self.runtime.mongo
        row = backend.db.settings.find_one({"_id": "bot-preferences"})
        raw = row["document"] if row else {}
        return BotUploadPreferences.model_validate(raw)

    def _write(self, value):
        document = value.model_dump()
        backend = self.runtime.mongo
        backend.write(
            backend.db.settings,
            "replace_one",
            {"_id": "bot-preferences"},
            {"_id": "bot-preferences", "document": document},
            upsert=True,
        )

    async def _load(self):
        if not self._loaded:
            try:
                self.value = await asyncio.to_thread(self._read)
            except Exception:
                raise SettingsProblem(
                    "Could not read bot upload settings.", status=503
                ) from None
            self._loaded = True

    def _destination(self):
        store = self.runtime.store
        path = self.value.folder_path
        if path is None:
            for item in store.list_directory("/").values():
                if item["name"].casefold() == DEFAULT_FOLDER_NAME.casefold():
                    if item["type"] != "folder":
                        raise SettingsProblem(
                            f"A file named {DEFAULT_FOLDER_NAME} already exists. "
                            "Choose a bot upload folder on the website."
                        )
                    path = f"/{item['id']}"
                    break
        if path is None:
            return None, f"My Files / {DEFAULT_FOLDER_NAME}"
        return path, folder_label(store, path)

    async def snapshot(self):
        async with self._lock:
            await self._load()
            try:
                path, label = await asyncio.to_thread(self._destination)
                available = True
            except (*_MISSING, SettingsProblem):
                path, label, available = (
                    self.value.folder_path,
                    "Folder unavailable",
                    False,
                )
            site_url = getattr(self.settings, "public_url", None) or self.value.site_url
            return {
                "folder_path": self.value.folder_path,
                "resolved_path": path,
                "folder_label": label,
                "available": available,
                "enabled": bool(self.settings.main_bot_token_value),
                "settings_url": site_url + SETTINGS_LOCATION if site_url else None,
            }

    async def save(self, value: BotUploadPreferences):
        async with self._lock:
            await self._load()
            if "site_url" not in value.model_fields_set:
                value = value.model_copy(update={"site_url": self.value.site_url})
            if value.folder_path is not None:
                try:
                    await asyncio.to_thread(
                        self.runtime.store.get_directory, value.folder_path
                    )
                except _MISSING:
                    raise SettingsProblem(
                        "Choose an existing folder outside Trash.", status=409
                    ) from None
            try:
                await asyncio.to_thread(self._write, value)
            except Exception:
                raise SettingsProblem(
                    "Could not save the bot upload folder. Try again.", status=503
                ) from None
            self.value = value
        return await self.snapshot()

    async def destination(self):
        """Create the dedicated default lazily, then pin its stable folder ID."""
        async with self._lock:
            await self._load()
            try:
                path, label = await asyncio.to_thread(self._destination)
            except _MISSING:
                raise SettingsProblem(
                    "The bot upload folder was moved or deleted. "
                    "Use /set_folder to choose another on the website.",
                    status=409,
                ) from None
            if path is None:
                replica = getattr(self.runtime, "replica", None)
                if replica is not None:
                    await replica.before_mutation()
                try:
                    folder = await asyncio.to_thread(
                        self.runtime.store.create_folder, "/", DEFAULT_FOLDER_NAME
                    )
                    path = f"/{folder['id']}"
                except DuplicateNameError:
                    path, label = await asyncio.to_thread(self._destination)
            if self.value.folder_path is None:
                value = self.value.model_copy(update={"folder_path": path})
                try:
                    await asyncio.to_thread(self._write, value)
                except Exception:
                    raise SettingsProblem(
                        "Could not save the bot upload folder.", status=503
                    ) from None
                self.value = value
            return path, label


def get_bot_settings(settings, runtime):
    service = getattr(runtime, "bot_settings", None)
    if service is None:
        service = BotSettings(settings, runtime)
        runtime.bot_settings = service
    return service
