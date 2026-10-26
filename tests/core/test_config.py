from __future__ import annotations

import pytest
from pydantic import ValidationError

from tgdrive.core.config import Settings


def settings_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "MONGODB_URI": "mongodb://synthetic.invalid",
        "API_ID": 12345,
        "API_HASH": "a" * 32,
        "BOT_TOKENS": "123456:" + "x" * 30,
        "STORAGE_CHANNEL": -1001234567890,
        "ADMIN_PASSWORD": "correct horse battery staple",
        "SESSION_SECRET": "s" * 48,
    }
    values.update(overrides)
    return values


def test_settings_parse_csv_and_derive_non_premium_limit() -> None:
    settings = Settings(_env_file=None, **settings_values())

    assert settings.bot_tokens == ("123456:" + "x" * 30,)
    assert settings.string_sessions == ()
    assert settings.telegram_admin_ids == ()
    assert settings.upload_limit == settings.staging_budget_bytes
    assert settings.bot_upload_limit == 2000 * 1024**2
    assert len(settings.session_key) == 32


@pytest.mark.parametrize("legacy_value", [None, "", " , "])
def test_main_bot_seeds_storage_credentials_for_a_new_deployment(
    monkeypatch, legacy_value
):
    monkeypatch.delenv("BOT_TOKENS", raising=False)
    token = "654321:" + "y" * 30
    values = settings_values(MAIN_BOT_TOKEN=token, TELEGRAM_ADMIN_IDS="123")
    if legacy_value is None:
        values.pop("BOT_TOKENS")
    else:
        values["BOT_TOKENS"] = legacy_value
    settings = Settings(_env_file=None, **values)

    assert settings.bot_tokens == (token,)
    assert settings.bot_tokens_csv.get_secret_value() == token
    assert settings.model_dump(by_alias=True)["BOT_TOKENS"].get_secret_value() == token
    assert token not in repr(settings)


def test_main_bot_does_not_replace_a_legacy_storage_bot_list():
    tokens = ("123456:" + "x" * 30, "654321:" + "y" * 30)
    settings = Settings(
        _env_file=None,
        **settings_values(
            BOT_TOKENS=",".join(tokens),
            MAIN_BOT_TOKEN="777777:" + "z" * 30,
            TELEGRAM_ADMIN_IDS="123",
        ),
    )
    assert settings.bot_tokens == tokens


def test_main_bot_does_not_hide_invalid_legacy_storage_credentials():
    with pytest.raises(ValidationError, match="BOT_TOKENS contains an invalid"):
        Settings(
            _env_file=None,
            **settings_values(
                BOT_TOKENS="invalid",
                MAIN_BOT_TOKEN="654321:" + "y" * 30,
                TELEGRAM_ADMIN_IDS="123",
            ),
        )


def test_main_bot_does_not_reseed_a_managed_empty_storage_list():
    with pytest.raises(ValidationError, match="At least one storage bot"):
        Settings(
            _env_file=None,
            **settings_values(
                BOT_TOKENS="",
                MAIN_BOT_TOKEN="654321:" + "y" * 30,
                TELEGRAM_ADMIN_IDS="123",
                managed_revision=1,
            ),
        )


def test_new_deployment_requires_an_initial_storage_bot():
    with pytest.raises(ValidationError, match="set MAIN_BOT_TOKEN"):
        Settings(_env_file=None, **settings_values(BOT_TOKENS="", MAIN_BOT_TOKEN=""))


def test_explicit_main_bot_configuration_keeps_injected_settings_isolated(monkeypatch):
    import tgdrive.storage.mongo as mongo

    values = settings_values(TELEGRAM_ADMIN_IDS="123")
    values.pop("BOT_TOKENS")
    monkeypatch.delenv("BOT_TOKENS", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))

    def unexpected_database_lookup(*_args, **_kwargs):
        raise AssertionError("Injected credentials must not load managed connections")

    monkeypatch.setattr(mongo, "configure", unexpected_database_lookup)
    token = "654321:" + "y" * 30
    settings = Settings(_env_file=None, MAIN_BOT_TOKEN=token)
    assert settings.bot_tokens == (token,)


def test_premium_sessions_do_not_raise_new_document_or_logical_limits() -> None:
    settings = Settings(
        _env_file=None,
        **settings_values(
            STRING_SESSIONS="session-one, session-two",
            TELEGRAM_ADMIN_IDS="123, 456",
        ),
    )

    assert settings.string_sessions == ("session-one", "session-two")
    assert settings.telegram_admin_ids == (123, 456)
    assert settings.upload_limit == settings.staging_budget_bytes
    assert settings.bot_upload_limit == 2000 * 1024**2
    assert settings.upload_split_mib == 2000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MONGODB_URI", ""),
        ("MONGODB_URI", "   "),
        ("MONGODB_URI", "https://synthetic.invalid"),
        ("ADMIN_PASSWORD", "admin"),
        ("ADMIN_PASSWORD", ""),
        ("ADMIN_PASSWORD", " \t " * 12),
        ("SESSION_SECRET", "short"),
        ("API_HASH", "z" * 32),
        ("BOT_TOKENS", "not-a-token"),
        ("MAIN_BOT_TOKEN", "not-a-token"),
        ("TELEGRAM_ADMIN_IDS", "123,nope"),
    ],
)
def test_settings_reject_unsafe_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **settings_values(**{field: value}))


def test_bot_mode_requires_admin_ids() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_ADMIN_IDS"):
        Settings(
            _env_file=None,
            **settings_values(BOT_TOKENS="", MAIN_BOT_TOKEN="654321:" + "y" * 30),
        )


def test_logical_limit_can_exceed_message_limit_but_is_bounded_by_staging() -> None:
    settings = Settings(_env_file=None, **settings_values(MAX_FILE_SIZE=4 * 1024**3))
    assert settings.upload_limit == 4 * 1024**3
    smaller = Settings(
        _env_file=None,
        **settings_values(
            MAX_FILE_SIZE=4 * 1024**3,
            STAGING_BUDGET_BYTES=3 * 1024**3,
        ),
    )
    assert smaller.upload_limit == 3 * 1024**3


@pytest.mark.parametrize("variant", ["i", "d"])
def test_non_argon2id_owner_hash_is_rejected(variant: str) -> None:
    from argon2 import PasswordHasher, Type

    algorithm = Type.I if variant == "i" else Type.D
    encoded = PasswordHasher(type=algorithm).hash("synthetic owner password")
    with pytest.raises(ValidationError, match="Argon2id"):
        Settings(
            _env_file=None,
            **settings_values(ADMIN_PASSWORD_HASH=encoded),
        )


def test_invalid_configuration_traceback_hides_raw_secret_inputs() -> None:
    secret = "synthetic-secret-value-that-must-not-be-rendered"
    with pytest.raises(ValidationError) as captured:
        Settings(
            _env_file=None,
            **settings_values(API_HASH=secret, SESSION_SECRET=secret),
        )
    assert secret not in str(captured.value)
    assert "input_value" not in str(captured.value)


def test_blank_plaintext_password_with_owner_hash_is_canonical_empty() -> None:
    from argon2 import PasswordHasher

    encoded = PasswordHasher().hash("synthetic owner password")
    settings = Settings(
        _env_file=None,
        **settings_values(ADMIN_PASSWORD=" \t " * 12, ADMIN_PASSWORD_HASH=encoded),
    )
    assert settings.admin_password.get_secret_value() == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("BOT_TOKENS", ",".join(["123456:" + "x" * 30] * 2)),
        ("STRING_SESSIONS", "synthetic-session,synthetic-session"),
    ],
)
def test_duplicate_telegram_credentials_are_rejected_without_echo(field, value):
    with pytest.raises(ValidationError, match="must not repeat") as caught:
        Settings(_env_file=None, **settings_values(**{field: value}))
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "cost", ["m=262145,t=3,p=4", "m=65536,t=11,p=4", "m=65536,t=3,p=17"]
)
def test_owner_hash_cannot_request_excessive_login_resources(cost):
    from argon2 import PasswordHasher

    encoded = PasswordHasher().hash("synthetic owner password")
    encoded = encoded.replace("m=65536,t=3,p=4", cost)
    with pytest.raises(ValidationError, match="exceeds supported login costs"):
        Settings(_env_file=None, **settings_values(ADMIN_PASSWORD_HASH=encoded))


def test_blank_optional_backup_key_is_absent():
    settings = Settings(_env_file=None, **settings_values(MONGODB_BACKUP_KEY=""))
    assert settings.mongodb_backup_key is None


def test_mongodb_uri_is_required_even_with_complete_telegram_configuration(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    values = settings_values()
    values.pop("MONGODB_URI")
    with pytest.raises(ValidationError, match="MONGODB_URI"):
        Settings(_env_file=None, **values)
