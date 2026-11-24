import hmac
import re
import sys
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy import URL

from app.domain.clinic import CLINIC_TZ

AppEnv = Literal["dev", "demo", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
HostName = Annotated[str, Field(pattern=r"^[A-Za-z0-9.-]+$")]

# The POSTGRES_PASSWORD value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_PASSWORD = "change-me-to-a-long-random-value"  # noqa: S105 (a published placeholder)
MIN_PASSWORD_LENGTH = 16
# The access token and the TURN secret, whenever they are set.
MIN_SECRET_LENGTH = 32
# What coturn's entrypoint accepts too (deploy/coturn/entrypoint.sh), so both agree.
TURN_SECRET_CHARACTERS = re.compile(r"[A-Za-z0-9._~+/=-]+")
PLAIN_WHOLE_NUMBER = re.compile(r"[0-9]+")
# LLM_MODEL: a Claude model id as Anthropic writes them, such as claude-haiku-5-5.
CLAUDE_MODEL_ID = r"^claude-[a-z0-9][a-z0-9.-]{0,59}$"
# The APP_DB_PASSWORD value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_APP_DB_PASSWORD = "change-me-to-another-long-random-value"  # noqa: S105 (a published placeholder)
# The runtime role's name goes into role set-up statements (app/db/roles.py), always quoted.
# Postgres cuts names after 63 bytes and reserves the pg_ prefix.
APP_DB_USER_NAME = re.compile(r"[a-z0-9_]{1,63}")
RESERVED_ROLE_PREFIX = "pg_"
# No database password may be a placeholder from .env.example outside dev.
DATABASE_PASSWORD_PLACEHOLDERS = (EXAMPLE_PASSWORD, EXAMPLE_APP_DB_PASSWORD)
# The AUDIT_HMAC_KEY value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_AUDIT_KEY = "change-me-to-a-long-random-audit-key"  # noqa: S105 (a published placeholder)
# The IDENTITY_HASH_KEY value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_IDENTITY_KEY = "change-me-to-a-long-random-identity-key"  # noqa: S105 (a published placeholder)
# The audit key and the identity key, outside dev: at least this long, not their placeholder.
MIN_AUDIT_KEY_LENGTH = 32
# An admin's user name, as the audit log records it after `admin:` (app/audit/log.py).
ADMIN_NAME = re.compile(r"[A-Za-z][A-Za-z_]{0,31}")
HMAC_KEY_PLACEHOLDERS = {
    "audit_hmac_key": EXAMPLE_AUDIT_KEY,
    "identity_hash_key": EXAMPLE_IDENTITY_KEY,
}


def _outside_dev(info: ValidationInfo) -> bool:
    return info.data.get("app_env") in ("demo", "prod")


# Every secret setting, outside dev, on top of its own length and character rules. The same
# count is checked for TURN_SHARED_SECRET by coturn's entrypoint (deploy/coturn/entrypoint.sh).
MIN_DISTINCT_SECRET_CHARACTERS = 10


def _check_strong_secret(name: str, value: SecretStr, info: ValidationInfo) -> None:
    if not _outside_dev(info):
        return
    secret = value.get_secret_value()
    if secret != secret.strip() or len(set(secret)) < MIN_DISTINCT_SECRET_CHARACTERS:
        raise ValueError(
            f"outside dev {name} must not start or end with whitespace and needs at least"
            f" {MIN_DISTINCT_SECRET_CHARACTERS} different characters"
        )


# SIMULATED_NOW: a date and a time, never a bare number or a date alone.
ISO_DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)


def _check_database_password(name: str, value: SecretStr, info: ValidationInfo) -> None:
    """The rules every database password follows, the owner's and the runtime role's."""
    password = value.get_secret_value()
    if not password:
        raise ValueError(f"{name} must not be empty")
    if not _outside_dev(info):
        return
    if len(password) < MIN_PASSWORD_LENGTH or password in DATABASE_PASSWORD_PLACEHOLDERS:
        raise ValueError(
            f"outside dev {name} needs at least {MIN_PASSWORD_LENGTH} characters"
            " and must not be a .env.example placeholder"
        )
    _check_strong_secret(name, value, info)


class _OneEnvFile(PydanticBaseSettingsSource):
    """The .env file, read for one settings class. One file holds the variables of both
    classes; each class takes its own from it and leaves the other's, and still refuses any key
    that belongs to neither. Only the file is shared: an argument or an environment variable
    of the other class is not read."""

    def __init__(self, settings_cls: type[BaseSettings], env_file: PydanticBaseSettingsSource):
        super().__init__(settings_cls)
        self._env_file = env_file

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._env_file.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        own = self.settings_cls.model_fields
        return {
            key: value
            for key, value in self._env_file().items()
            if key in own or key not in _EVERY_SETTING
        }


class _SettingsBase(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="forbid", hide_input_in_errors=True, env_ignore_empty=True
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _OneEnvFile(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    app_env: AppEnv
    log_level: LogLevel = "INFO"


class DatabaseSettings(_SettingsBase):
    """Where the database is, and the runtime role the application connects as."""

    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    # The runtime role (app/db/roles.py sets it up).
    app_db_user: str
    app_db_password: SecretStr

    @field_validator("app_db_user")
    @classmethod
    def runtime_user_is_a_plain_name(cls, value: str) -> str:
        if not APP_DB_USER_NAME.fullmatch(value) or value.startswith(RESERVED_ROLE_PREFIX):
            raise ValueError(
                "app_db_user must be 1 to 63 lower-case letters, digits and underscores,"
                f" not starting with {RESERVED_ROLE_PREFIX}"
            )
        return value

    @field_validator("app_db_password")
    @classmethod
    def runtime_password_is_strong(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        _check_database_password("app_db_password", value, info)
        return value

    def _database_url(self, user: str, password: SecretStr) -> URL:
        """The only place a database URL is assembled. Its str() and repr() mask the password."""
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def runtime_database_url(self) -> URL:
        """The application's connections: the runtime role."""
        return self._database_url(self.app_db_user, self.app_db_password)


class Settings(DatabaseSettings):
    """The application's settings. They hold no part of the owner's identity, and the app
    container is never given the owner's variables (deploy/docker-compose.yml)."""

    # The key of the audit log's hash chain (app/audit/log.py).
    audit_hmac_key: SecretStr
    # The key of the identity claims' hashes in verification_attempts (app/agent/claims.py).
    identity_hash_key: SecretStr

    # A fixed clock for dev and demo. Set together with app_env=prod, startup stops.
    simulated_now: datetime | None = None

    @field_validator("simulated_now", mode="before")
    @classmethod
    def simulated_now_is_iso_text(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str) and ISO_DATE_TIME.fullmatch(value):
            return value
        raise ValueError(
            "simulated_now must be an ISO date and time: YYYY-MM-DDTHH:MM[:SS[.ffffff]],"
            " T or a space between, optionally Z or +HH:MM"
        )

    @field_validator("simulated_now")
    @classmethod
    def simulated_now_only_in_dev_or_demo(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        if value is None:
            return None
        if info.data.get("app_env") not in ("dev", "demo"):
            raise ValueError("simulated_now is only allowed when app_env is dev or demo")
        if value.utcoffset() is not None:
            return value
        # Without a zone the value is clinic time. A wall time that a clock change skips or
        # repeats has no single instant, so it is refused rather than guessed.
        first = value.replace(tzinfo=CLINIC_TZ, fold=0)
        if first.utcoffset() != value.replace(tzinfo=CLINIC_TZ, fold=1).utcoffset():
            raise ValueError("simulated_now without a zone falls in a clock change; add an offset")
        return first

    max_concurrent_calls: int = Field(default=3, gt=0, le=20)
    max_call_seconds: int = Field(default=600, gt=0, le=3600)
    # The voice fields are checked in this order, each against the ones before it, so that
    # a missing value is reported on its own field and never next to a secret given in another.
    turn_shared_secret: SecretStr | None = Field(
        default=None, validate_default=True, min_length=MIN_SECRET_LENGTH
    )
    turn_host: HostName | None = Field(default=None, validate_default=True)
    demo_access_token: SecretStr | None = Field(
        default=None, validate_default=True, min_length=MIN_SECRET_LENGTH
    )
    # The admin API's sign-in (app/api/admin.py). Without a password it is not mounted. The
    # password comes first: the user's check reads it.
    admin_password: SecretStr | None = None
    admin_user: str | None = Field(default=None, validate_default=True)
    # The vendors of a call: the model (app/agent/model.py) and speech-to-text
    # (app/voice/pipeline.py). Without both keys a call is refused at set-up.
    anthropic_api_key: SecretStr | None = Field(default=None, validate_default=True)
    deepgram_api_key: SecretStr | None = Field(default=None, validate_default=True)
    llm_model: str = Field(default="claude-haiku-5-5", pattern=CLAUDE_MODEL_ID)

    @field_validator("anthropic_api_key", "deepgram_api_key")
    @classmethod
    def _vendor_key_outside_dev(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        name = info.field_name or ""
        if value is None:
            if _outside_dev(info):
                raise ValueError(f"{name} is required unless app_env is dev")
            return None
        _check_strong_secret(name, value, info)
        return value

    @field_validator("audit_hmac_key", "identity_hash_key")
    @classmethod
    def hmac_key_is_set_and_strong_outside_dev(
        cls, value: SecretStr, info: ValidationInfo
    ) -> SecretStr:
        name = info.field_name or ""
        key = value.get_secret_value()
        if not key:
            raise ValueError(f"{name} must not be empty")
        if info.data.get("app_env") == "dev":
            return value
        if len(key) < MIN_AUDIT_KEY_LENGTH or key == HMAC_KEY_PLACEHOLDERS[name]:
            raise ValueError(
                f"outside dev {name} needs at least {MIN_AUDIT_KEY_LENGTH} characters"
                " and must not be the .env.example placeholder"
            )
        _check_strong_secret(name, value, info)
        return value

    @field_validator("identity_hash_key")
    @classmethod
    def identity_key_is_not_the_audit_key(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        audit_key = info.data.get("audit_hmac_key")
        if audit_key is not None and value.get_secret_value() == audit_key.get_secret_value():
            raise ValueError("identity_hash_key must differ from audit_hmac_key")
        return value

    @field_validator("max_concurrent_calls", mode="before")
    @classmethod
    def _plain_call_count(cls, value: Any) -> Any:
        # Digits only, as coturn's entrypoint reads it: no sign, spaces, underscores or point.
        if isinstance(value, str) and not PLAIN_WHOLE_NUMBER.fullmatch(value):
            raise ValueError("max_concurrent_calls must be written with digits only")
        return value

    @field_validator("turn_shared_secret")
    @classmethod
    def _secret_outside_dev(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        if value is None and _outside_dev(info):
            raise ValueError("turn_shared_secret is required unless app_env is dev")
        if value is not None and not TURN_SECRET_CHARACTERS.fullmatch(value.get_secret_value()):
            raise ValueError(
                "turn_shared_secret may contain only letters, digits and . _ ~ + / = -"
            )
        if value is not None:
            _check_strong_secret("turn_shared_secret", value, info)
        return value

    @field_validator("turn_host")
    @classmethod
    def _host_with_secret(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None and _outside_dev(info):
            raise ValueError("turn_host is required unless app_env is dev")
        if "turn_shared_secret" in info.data:
            has_secret = info.data["turn_shared_secret"] is not None
            if has_secret != (value is not None):
                raise ValueError("turn_host and turn_shared_secret must be set together")
        return value

    @field_validator("demo_access_token")
    @classmethod
    def _token_when_needed(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        if value is None:
            if _outside_dev(info):
                raise ValueError("demo_access_token is required unless app_env is dev")
            if info.data.get("turn_shared_secret") is not None:
                raise ValueError("demo_access_token is required when TURN is configured")
        else:
            _check_strong_secret("demo_access_token", value, info)
        return value

    @field_validator("admin_password")
    @classmethod
    def _admin_password_is_strong(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is None:
            return None
        password = value.get_secret_value()
        if not password:
            raise ValueError("admin_password must not be empty")
        if _outside_dev(info) and len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"outside dev admin_password needs at least {MIN_PASSWORD_LENGTH} characters"
            )
        _check_strong_secret("admin_password", value, info)
        return value

    @field_validator("admin_user")
    @classmethod
    def _admin_user_with_password(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            if info.data.get("admin_password") is not None:
                raise ValueError("admin_user is required when admin_password is set")
            return None
        if not ADMIN_NAME.fullmatch(value):
            raise ValueError(
                "admin_user must be 1 to 32 letters and underscores, starting with a letter"
            )
        return value

    @model_validator(mode="after")
    def _admin_password_is_its_own(self) -> "Settings":
        """The admin password is none of the other secrets: every SecretStr field, whatever
        its name or place, compared in constant time and reported by field name only."""
        if self.admin_password is None:
            return self
        password = self.admin_password.get_secret_value().encode("utf-8")
        for name in type(self).model_fields:
            other = getattr(self, name)
            if (
                name != "admin_password"
                and isinstance(other, SecretStr)
                and hmac.compare_digest(other.get_secret_value().encode("utf-8"), password)
            ):
                raise ValueError(f"admin_password must differ from {name}")
        return self


class StartupSettings(DatabaseSettings):
    """The start-up steps' settings, and theirs only: migrations, role set-up and the seed
    (a repo hygiene test keeps them there). They add the owner, which never reaches the
    application. The owner's fields come after the runtime role's, so they are checked
    against it."""

    postgres_user: str
    postgres_password: SecretStr

    @field_validator("postgres_user")
    @classmethod
    def owner_is_not_the_runtime_role(cls, value: str, info: ValidationInfo) -> str:
        if value == info.data.get("app_db_user"):
            raise ValueError("postgres_user must differ from app_db_user")
        return value

    @field_validator("postgres_password")
    @classmethod
    def owner_password_is_strong_and_its_own(
        cls, value: SecretStr, info: ValidationInfo
    ) -> SecretStr:
        _check_database_password("postgres_password", value, info)
        runtime_password = info.data.get("app_db_password")
        if runtime_password is not None and (
            value.get_secret_value() == runtime_password.get_secret_value()
        ):
            raise ValueError("postgres_password must differ from app_db_password")
        return value

    @property
    def owner_database_url(self) -> URL:
        """The start-up steps' connections only: the owner (see app/db/session.py)."""
        return self._database_url(self.postgres_user, self.postgres_password)


# Every variable of either class, so that each can leave the other's in the shared .env file.
_EVERY_SETTING = frozenset(Settings.model_fields) | frozenset(StartupSettings.model_fields)


def main() -> None:
    """`python -m app.config`: the start-up service's first step, which checks the start-up
    steps' settings from the environment before anything touches the database. Every error is
    reported, by field name and a fixed message, never a value. The application checks its own
    settings when it starts."""
    try:
        StartupSettings()
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
