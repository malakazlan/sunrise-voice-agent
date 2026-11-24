import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
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
# The APP_DB_PASSWORD value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_APP_DB_PASSWORD = "change-me-to-another-long-random-value"  # noqa: S105 (a published placeholder)
# The runtime role's name goes into role set-up statements (app/db/roles.py), always quoted.
# Postgres cuts names after 63 bytes and reserves the pg_ prefix.
APP_DB_USER_NAME = re.compile(r"[a-z0-9_]{1,63}")
RESERVED_ROLE_PREFIX = "pg_"
# The AUDIT_HMAC_KEY value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_AUDIT_KEY = "change-me-to-a-long-random-audit-key"  # noqa: S105 (a published placeholder)
MIN_AUDIT_KEY_LENGTH = 32


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="forbid", hide_input_in_errors=True, env_ignore_empty=True
    )

    app_env: AppEnv
    log_level: LogLevel = "INFO"

    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    # The owner: migrations, role set-up and the seed at start-up only.
    postgres_user: str
    postgres_password: SecretStr
    # The runtime role the application connects as (app/db/roles.py sets it up).
    app_db_user: str
    app_db_password: SecretStr

    # The key of the audit log's hash chain (app/audit/log.py).
    audit_hmac_key: SecretStr

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

    @field_validator("postgres_password")
    @classmethod
    def password_is_strong_outside_dev(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        if info.data.get("app_env") in (None, "dev"):
            return value
        password = value.get_secret_value()
        if len(password) < MIN_PASSWORD_LENGTH or password == EXAMPLE_PASSWORD:
            raise ValueError(
                f"outside dev the password needs at least {MIN_PASSWORD_LENGTH} characters"
                " and must not be the .env.example placeholder"
            )
        _check_strong_secret("postgres_password", value, info)
        return value

    @field_validator("app_db_user")
    @classmethod
    def runtime_user_is_a_plain_name_of_its_own(cls, value: str, info: ValidationInfo) -> str:
        if not APP_DB_USER_NAME.fullmatch(value) or value.startswith(RESERVED_ROLE_PREFIX):
            raise ValueError(
                "app_db_user must be 1 to 63 lower-case letters, digits and underscores,"
                f" not starting with {RESERVED_ROLE_PREFIX}"
            )
        if value == info.data.get("postgres_user"):
            raise ValueError("app_db_user must differ from postgres_user")
        return value

    @field_validator("app_db_password")
    @classmethod
    def runtime_password_is_its_own_and_strong_outside_dev(
        cls, value: SecretStr, info: ValidationInfo
    ) -> SecretStr:
        password = value.get_secret_value()
        if not password:
            raise ValueError("app_db_password must not be empty")
        owner_password = info.data.get("postgres_password")
        if owner_password is not None and password == owner_password.get_secret_value():
            raise ValueError("app_db_password must differ from postgres_password")
        if info.data.get("app_env") in (None, "dev"):
            return value
        if len(password) < MIN_PASSWORD_LENGTH or password in (
            EXAMPLE_PASSWORD,
            EXAMPLE_APP_DB_PASSWORD,
        ):
            raise ValueError(
                f"outside dev app_db_password needs at least {MIN_PASSWORD_LENGTH} characters"
                " and must not be a .env.example placeholder"
            )
        _check_strong_secret("app_db_password", value, info)
        return value

    @field_validator("audit_hmac_key")
    @classmethod
    def audit_key_is_set_and_strong_outside_dev(
        cls, value: SecretStr, info: ValidationInfo
    ) -> SecretStr:
        key = value.get_secret_value()
        if not key:
            raise ValueError("audit_hmac_key must not be empty")
        if info.data.get("app_env") == "dev":
            return value
        if len(key) < MIN_AUDIT_KEY_LENGTH or key == EXAMPLE_AUDIT_KEY:
            raise ValueError(
                f"outside dev the audit key needs at least {MIN_AUDIT_KEY_LENGTH} characters"
                " and must not be the .env.example placeholder"
            )
        _check_strong_secret("audit_hmac_key", value, info)
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

    @property
    def owner_database_url(self) -> URL:
        """The start-up steps' connections only: the owner (see app/db/session.py)."""
        return self._database_url(self.postgres_user, self.postgres_password)
