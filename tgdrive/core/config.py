"""Typed, fail-fast application configuration."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from argon2 import Type, extract_parameters
from argon2.exceptions import InvalidHashError
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BOT_TOKEN_RE = re.compile(r"^[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}$")
# Conservative document cap in binary MiB; new uploads use it for every account.
_DEFAULT_BOT_LIMIT = 2000 * 1024**2
_DEPLOYMENT_ONLY_SETTINGS = frozenset(
    {"DEMO_MODE", "DEMO_ADMIN_PASSWORD", "DEMO_ADMIN_PASSWORD_HASH"}
)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _should_restore_setting(name: str, configured: Mapping[str, object]) -> bool:
    """Fill a missing setting without replacing deployment access policy.

    A configured plaintext owner password also takes precedence over a saved
    password hash, so credential rotation cannot revive the previous password.
    """
    if name in _DEPLOYMENT_ONLY_SETTINGS:
        return False
    if configured.get(name) not in (None, ""):
        return False
    return not (name == "ADMIN_PASSWORD_HASH" and configured.get("ADMIN_PASSWORD"))


class Settings(BaseSettings):
    """Validated deployment configuration and saved Telegram connections.

    Explicit constructor values take priority. Saved connection settings precede
    environment variables and ``.env``; recovery settings fill missing values.
    Secret values are represented by :class:`~pydantic.SecretStr`, which keeps
    accidental logging and validation errors from exposing credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
        hide_input_in_errors=True,
    )

    mongodb_uri: SecretStr = Field(alias="MONGODB_URI")
    mongodb_database: str = Field(
        default="tgdrive", alias="MONGODB_DATABASE", pattern=r"^[A-Za-z0-9_-]{1,63}$"
    )
    mongodb_backup_seconds: int = Field(
        default=3600, alias="MONGODB_BACKUP_SECONDS", ge=60, le=604800
    )
    mongodb_backup_key: SecretStr | None = Field(
        default=None, alias="MONGODB_BACKUP_KEY"
    )

    @field_validator("mongodb_backup_key")
    @classmethod
    def valid_backup_key(cls, value):
        if value is not None and not re.fullmatch(
            r"[0-9a-fA-F]{64}", value.get_secret_value()
        ):
            raise ValueError(
                "MONGODB_BACKUP_KEY must contain 64 hexadecimal characters"
            )
        return value

    @field_validator("mongodb_uri")
    @classmethod
    def valid_mongodb_uri(cls, value):
        raw = value.get_secret_value().strip()
        if not raw.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URI must be a MongoDB connection string")
        return SecretStr(raw)

    api_id: int = Field(alias="API_ID", gt=0)
    api_hash: SecretStr = Field(alias="API_HASH")
    bot_tokens_csv: SecretStr = Field(default=SecretStr(""), alias="BOT_TOKENS")
    storage_channel: int = Field(alias="STORAGE_CHANNEL", lt=0)
    admin_password: SecretStr = Field(default=SecretStr(""), alias="ADMIN_PASSWORD")
    demo_admin_password: SecretStr | None = Field(
        default=None, alias="DEMO_ADMIN_PASSWORD"
    )
    demo_admin_password_hash: SecretStr | None = Field(
        default=None, alias="DEMO_ADMIN_PASSWORD_HASH"
    )
    session_secret: SecretStr = Field(alias="SESSION_SECRET")

    string_sessions_csv: SecretStr = Field(
        default=SecretStr(""), alias="STRING_SESSIONS"
    )
    main_bot_token: SecretStr | None = Field(default=None, alias="MAIN_BOT_TOKEN")
    public_url: str | None = Field(default=None, alias="PUBLIC_URL")

    @field_validator("public_url")
    @classmethod
    def valid_public_url(cls, value):
        from tgdrive.settings.bot_settings import BotUploadPreferences

        return BotUploadPreferences(site_url=value).site_url if value else None

    telegram_admin_ids_csv: str = Field(default="", alias="TELEGRAM_ADMIN_IDS")

    sleep_threshold: int = Field(default=60, alias="SLEEP_THRESHOLD", ge=0, le=3600)
    max_file_size: int | None = Field(default=None, alias="MAX_FILE_SIZE", gt=0)
    cache_dir: Path = Field(default=Path("cache"), alias="CACHE_DIR")
    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    session_ttl_seconds: int = Field(
        default=86400, alias="SESSION_TTL_SECONDS", ge=300, le=2592000
    )
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")
    allow_private_remote_urls: bool = Field(
        default=False, alias="ALLOW_PRIVATE_REMOTE_URLS"
    )
    remote_connect_timeout: float = Field(
        default=20.0, alias="REMOTE_CONNECT_TIMEOUT", gt=0, le=300
    )
    remote_download_concurrency: int = Field(
        default=2, alias="REMOTE_DOWNLOAD_CONCURRENCY", ge=1, le=8
    )
    remote_read_timeout: float = Field(
        default=120.0, alias="REMOTE_READ_TIMEOUT", gt=0, le=3600
    )
    remote_total_timeout: float = Field(
        default=21600.0, alias="REMOTE_TOTAL_TIMEOUT", gt=0, le=86400
    )
    remote_max_redirects: int = Field(
        default=5, alias="REMOTE_MAX_REDIRECTS", ge=0, le=20
    )
    transfer_ttl_seconds: int = Field(
        default=3600, alias="TRANSFER_TTL_SECONDS", ge=60, le=86400
    )
    max_concurrent_transfers: int = Field(
        default=2, alias="MAX_CONCURRENT_TRANSFERS", ge=1, le=32
    )

    telegram_uploads_per_bot: int = Field(
        default=1, alias="TELEGRAM_UPLOADS_PER_BOT", ge=1, le=4
    )

    telegram_upload_pipeline: int = Field(
        default=4, alias="TELEGRAM_UPLOAD_PIPELINE", ge=1, le=16
    )
    telegram_request_limit: int = Field(
        default=8, alias="TELEGRAM_REQUEST_LIMIT", ge=1, le=32
    )
    telegram_parts_per_second: int = Field(
        default=48, alias="TELEGRAM_PARTS_PER_SECOND", ge=1, le=128
    )
    telegram_media_per_bot: int = Field(
        default=3, alias="TELEGRAM_MEDIA_PER_BOT", ge=1, le=16
    )
    telegram_max_queue: int = Field(
        default=256, alias="TELEGRAM_MAX_QUEUE", ge=1, le=4096
    )
    upload_split_mib: int = Field(default=2000, alias="UPLOAD_SPLIT_MIB", ge=0, le=2000)
    cache_memory_bytes: int = Field(
        default=256 * 1024**2, alias="CACHE_MEMORY_BYTES", ge=0, le=8 * 1024**3
    )
    cache_disk_bytes: int = Field(
        default=10 * 1024**3, alias="CACHE_DISK_BYTES", ge=0, le=2 * 1024**4
    )
    cache_min_free_bytes: int = Field(
        default=1024**3, alias="CACHE_MIN_FREE_BYTES", ge=0, le=2 * 1024**4
    )
    cache_ttl_seconds: int = Field(
        default=86400, alias="CACHE_TTL_SECONDS", ge=60, le=2678400
    )
    auto_preload: bool = Field(default=True, alias="AUTO_PRELOAD")
    preload_max_jobs: int = Field(default=2, alias="PRELOAD_MAX_JOBS", ge=1, le=8)
    timezone: str = Field(default="Asia/Kolkata", alias="TIMEZONE")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Use a valid IANA timezone, such as Asia/Kolkata") from exc
        return value

    staging_budget_bytes: int = Field(
        default=16 * 1024**3, alias="STAGING_BUDGET_BYTES", gt=0
    )
    min_free_bytes: int = Field(default=1024**3, alias="MIN_FREE_BYTES", ge=0)
    upload_session_ttl_seconds: int = Field(
        default=86400, alias="UPLOAD_SESSION_TTL_SECONDS", ge=60
    )
    max_concurrent_streams: int = Field(
        default=4, alias="MAX_CONCURRENT_STREAMS", ge=1, le=64
    )
    admin_password_hash: SecretStr | None = Field(
        default=None, alias="ADMIN_PASSWORD_HASH"
    )

    # Internal startup provenance is never exported as configuration or backup.
    managed_revision: int = Field(default=0, exclude=True, ge=0)
    managed_client_ids: tuple[str, ...] = Field(default=(), exclude=True)
    managed_client_labels: tuple[str, ...] = Field(default=(), exclude=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        def managed_source():
            from tgdrive.settings.managed_settings import read_document

            explicit = init_settings()
            credential_fields = {
                "API_ID",
                "API_HASH",
                "BOT_TOKENS",
                "MAIN_BOT_TOKEN",
                "STRING_SESSIONS",
            }
            # Explicit injected configurations remain isolated (tests/embedding).
            if credential_fields.intersection(explicit):
                return {}
            base = {**dotenv_settings(), **env_settings(), **explicit}
            directory = Path(base.get("DATA_DIR", "data")).expanduser().resolve()
            bootstrap = {}
            uri = base.get("MONGODB_URI")
            if not uri:
                # Let the required field produce a redacted configuration error.
                return {}
            from tgdrive.replication.replica_bootstrap import load_bootstrap
            from tgdrive.storage.mongo import DatabaseUnavailable, configure

            recovery_bootstrap = load_bootstrap(
                directory,
                mongodb_uri=uri,
                mongodb_database=str(base.get("MONGODB_DATABASE", "tgdrive")),
            )
            backend = configure(
                directory,
                uri.get_secret_value() if isinstance(uri, SecretStr) else str(uri),
                str(base.get("MONGODB_DATABASE", "tgdrive")),
            )
            try:
                row = backend.db.settings.find_one({"_id": "bootstrap"})
            except Exception:
                raise DatabaseUnavailable(
                    "MongoDB is unavailable. Check MONGODB_URI, authentication, "
                    "read permissions and network access."
                ) from None
            # Recovery copies fill missing values; they must never conceal an
            # explicit credential rotation or channel/database mismatch.
            saved = {
                **(recovery_bootstrap or {}),
                **(row["document"] if row else {}),
            }
            bootstrap = {
                key: value
                for key, value in saved.items()
                if _should_restore_setting(key, base)
            }
            if os.environ.get("TGDRIVE_AUTO_SECRETS") == "true":
                from tgdrive.core.deployment import SECRET_FIELDS, installation_secrets

                known = {}
                for key in SECRET_FIELDS:
                    value = base.get(key) or bootstrap.get(key)
                    if value:
                        known[key] = (
                            value.get_secret_value()
                            if isinstance(value, SecretStr)
                            else str(value)
                        )
                existing = bool(
                    recovery_bootstrap is not None
                    or (directory / "mongodb-drive.json").exists()
                    or row is not None
                    or (
                        not all(known.get(key) for key in SECRET_FIELDS)
                        and backend.db.catalog.find_one({"_id": "catalog"})
                    )
                )
                bootstrap = {
                    **bootstrap,
                    **installation_secrets(directory, known, existing_drive=existing),
                }
            document = read_document(directory)
            if document is None:
                return bootstrap
            rows = [
                row
                for kind in ("bot", "user")
                for row in document.clients
                if row.kind == kind and row.enabled
            ]
            return {
                **bootstrap,
                "API_ID": document.api_id,
                "API_HASH": document.api_hash,
                "BOT_TOKENS": ",".join(
                    row.credential.get_secret_value()
                    for row in rows
                    if row.kind == "bot"
                ),
                "STRING_SESSIONS": ",".join(
                    row.credential.get_secret_value()
                    for row in rows
                    if row.kind == "user"
                ),
                "managed_revision": document.revision,
                "managed_client_ids": tuple(row.id for row in rows),
                "managed_client_labels": tuple(row.label for row in rows),
            }

        return (
            init_settings,
            managed_source,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    @field_validator("max_file_size", mode="before")
    @classmethod
    def optional_file_size(cls, value):
        return None if value in ("", None) else value

    @field_validator(
        "admin_password_hash",
        "mongodb_backup_key",
        "demo_admin_password",
        "demo_admin_password_hash",
        mode="before",
    )
    @classmethod
    def optional_password_hash(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return (
            None if raw is None or (isinstance(raw, str) and not raw.strip()) else value
        )

    @field_validator("api_hash")
    @classmethod
    def validate_api_hash(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
            raise ValueError("API hash must contain exactly 32 hexadecimal characters")
        return SecretStr(raw)

    @field_validator("admin_password", "session_secret", "demo_admin_password")
    @classmethod
    def validate_non_empty_secret(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if raw.startswith("<"):
            raise ValueError("Replace the example placeholder with a generated secret")
        if info.field_name == "admin_password" and not raw:
            return SecretStr("")
        minimum = (
            12 if info.field_name in {"admin_password", "demo_admin_password"} else 32
        )
        if len(raw) < minimum:
            label = info.field_name.replace("_", " ")
            raise ValueError(f"{label} must contain at least {minimum} characters")
        return SecretStr(raw)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("telegram_admin_ids_csv")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        for raw_id in _csv(value):
            if not raw_id.isdecimal() or int(raw_id) <= 0:
                raise ValueError("must be a comma-separated list of positive integers")
        return value

    @model_validator(mode="after")
    def validate_related_settings(self) -> Settings:
        if (
            not self.admin_password.get_secret_value()
            and self.admin_password_hash is None
        ):
            raise ValueError("ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required")
        for name in ("admin_password_hash", "demo_admin_password_hash"):
            encoded = getattr(self, name)
            if encoded is None:
                continue
            label = name.upper()
            try:
                parameters = extract_parameters(encoded.get_secret_value())
                if parameters.type is not Type.ID:
                    raise ValueError(f"{label} must use Argon2id")
                if parameters.memory_cost < 19456 or parameters.time_cost < 2:
                    raise ValueError(f"{label} is below the supported security floor")
                if (
                    parameters.memory_cost > 262144
                    or parameters.time_cost > 10
                    or parameters.parallelism > 16
                ):
                    raise ValueError(
                        f"{label} exceeds supported login costs "
                        "(256 MiB memory, 10 iterations, 16 lanes); generate a new "
                        "Argon2id hash or configure ADMIN_PASSWORD instead"
                    )
            except InvalidHashError as exc:
                raise ValueError(f"{label} must be a valid Argon2id hash") from exc
        if self.demo_mode:
            from tgdrive.core.security import verify_password

            owner = self.admin_password_hash or self.admin_password
            demo = self.demo_admin_password_hash or self.demo_admin_password
            collision = bool(demo and demo == owner)
            if demo and self.demo_admin_password_hash is None:
                collision = verify_password(
                    demo.get_secret_value(), owner.get_secret_value()
                )
            elif demo and self.admin_password_hash is None:
                collision = verify_password(
                    owner.get_secret_value(), demo.get_secret_value()
                )
            if collision:
                raise ValueError(
                    "Demo administrator password must differ from the regular password"
                )
        token = self.main_bot_token_value
        if token:
            if not _BOT_TOKEN_RE.fullmatch(token):
                raise ValueError("MAIN_BOT_TOKEN is not a valid Telegram bot token")
            if not self.telegram_admin_ids:
                raise ValueError(
                    "TELEGRAM_ADMIN_IDS is required when MAIN_BOT_TOKEN is set"
                )
            # Seed new installations without reviving a saved, removed connection.
            if not self.bot_tokens and not self.managed_revision:
                self.bot_tokens_csv = SecretStr(token)
        if not self.bot_tokens:
            raise ValueError(
                "At least one storage bot is required; "
                "set MAIN_BOT_TOKEN for a new deployment"
            )
        if len(set(self.bot_tokens)) != len(self.bot_tokens):
            raise ValueError("BOT_TOKENS must not repeat a storage bot")
        if len(set(self.string_sessions)) != len(self.string_sessions):
            raise ValueError("STRING_SESSIONS must not repeat an account session")
        invalid_tokens = [
            token for token in self.bot_tokens if not _BOT_TOKEN_RE.fullmatch(token)
        ]
        if invalid_tokens:
            raise ValueError("BOT_TOKENS contains an invalid Telegram bot token")
        return self

    @property
    def bot_tokens(self) -> tuple[str, ...]:
        return _csv(self.bot_tokens_csv.get_secret_value())

    @property
    def string_sessions(self) -> tuple[str, ...]:
        return _csv(self.string_sessions_csv.get_secret_value())

    @property
    def telegram_admin_ids(self) -> tuple[int, ...]:
        return tuple(int(value) for value in _csv(self.telegram_admin_ids_csv))

    @property
    def main_bot_token_value(self) -> str | None:
        if self.main_bot_token is None:
            return None
        value = self.main_bot_token.get_secret_value().strip()
        return value or None

    @property
    def bot_upload_limit(self) -> int:
        return _DEFAULT_BOT_LIMIT

    @property
    def upload_limit(self) -> int:
        """Logical file ceiling; Telegram message limits apply to individual parts."""
        return min(
            self.staging_budget_bytes,
            self.max_file_size or self.staging_budget_bytes,
            self.bot_upload_limit * 4096,
        )

    @property
    def session_key(self) -> bytes:
        return hashlib.sha256(self.session_secret.get_secret_value().encode()).digest()

    @property
    def resolved_cache_dir(self) -> Path:
        return self.cache_dir.expanduser().resolve()

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide validated settings instance."""

    return Settings()  # type: ignore[call-arg]
