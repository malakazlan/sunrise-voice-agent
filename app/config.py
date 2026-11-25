import hashlib
import hmac
import re
import sys
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy import URL

from app.domain.clinic import CLINIC_TZ

AppEnv = Literal["dev", "demo", "prod"]
# What one process serves (app/server.py): everything, for development and tests; the calls
# alone; or the admin API alone.
AppRole = Literal["all", "voice", "admin"]
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
# ELEVENLABS_VOICE_ID and TTS_MODEL go into the ElevenLabs WebSocket URL: letters and digits
# only for the voice; for the model, ElevenLabs' Flash models, the lowest-latency ones its
# multi-stream endpoint serves (it serves no v3 or v4 model).
ELEVENLABS_VOICE_ID = r"^[A-Za-z0-9]{1,64}$"
LOW_LATENCY_TTS_MODEL = r"^eleven_flash_v2(_5)?$"
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
# The speech vendors of a call, by name: the keys and voice below are theirs.
SPEECH_TO_TEXT = "deepgram"
TEXT_TO_SPEECH = "elevenlabs"
# CONSOLE_ORIGIN: one exact origin, as a browser sends it: a scheme, a lower-case host name or
# IPv4 address, and a port if not the scheme's own. No path, no wildcard, nothing else.
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
WEB_ORIGIN = re.compile(
    rf"(?P<scheme>https?)://{HOST_LABEL}(?:\.{HOST_LABEL})*"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?"
)
MAX_PORT = 65535
# The port a browser leaves out of the Origin it sends, for each scheme.
DEFAULT_PORTS = {"https": 443, "http": 80}


def _outside_dev(info: ValidationInfo) -> bool:
    return info.data.get("app_env") in ("demo", "prod")


def _admin(info: ValidationInfo) -> bool:
    """Whether the settings are the admin role's. A role that did not validate is not."""
    return info.data.get("app_role") == "admin"


# What each long-running role never loads, whatever its environment holds: the other role's
# secrets, and TURN_HOST with the TURN secret (the admin role has no TURN). A platform may give
# every container every variable of the deployment; the role then holds None for these, every
# rule sees them unset, and its server names them once at start-up (app/server.py). Role all,
# and the start-up check, load and check them all.
OTHER_ROLES_SETTINGS: dict[str, frozenset[str]] = {
    "voice": frozenset({"admin_password"}),
    "admin": frozenset(
        {
            "identity_hash_key",
            "turn_shared_secret",
            "turn_host",
            "call_access_token",
            "anthropic_api_key",
            "deepgram_api_key",
            "elevenlabs_api_key",
        }
    ),
}


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

    # What this process serves. First, since the rules of the fields after it depend on it: the
    # admin role needs none of the calls' settings. The voice role never loads the admin
    # password, the admin role never loads the calls' secrets (OTHER_ROLES_SETTINGS).
    app_role: AppRole = "all"
    # The key of the audit log's hash chain (app/audit/log.py).
    audit_hmac_key: SecretStr
    # The key of the identity claims' hashes in verification_attempts (app/agent/claims.py).
    # Required, but never loaded by the admin role.
    identity_hash_key: SecretStr | None = Field(default=None, validate_default=True)
    # The other role's settings this process was given and did not load, by field name.
    _ignored_settings: tuple[str, ...] = PrivateAttr(default=())

    @model_validator(mode="wrap")
    @classmethod
    def _other_roles_settings_are_not_loaded(
        cls, data: dict[str, Any], handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        """The other role's settings are dropped before any rule runs: every rule finds them
        unset, and no value of theirs reaches the settings, their repr or an error. Only their
        names are kept."""
        other = OTHER_ROLES_SETTINGS.get(data.get("app_role", "all"), frozenset())
        settings = handler({key: value for key, value in data.items() if key not in other})
        settings._ignored_settings = tuple(
            sorted(name for name in other if data.get(name) is not None)
        )
        return settings

    @property
    def ignored_settings(self) -> tuple[str, ...]:
        """The other role's settings this process was given and did not load, by field name."""
        return self._ignored_settings

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
    call_access_token: SecretStr | None = Field(
        default=None, validate_default=True, min_length=MIN_SECRET_LENGTH
    )
    # The admin's sign-in, for the admin API (app/api/admin.py): HTTP Basic, or a browser
    # session. Without a password it is not mounted. The password comes first: the user's check
    # reads it.
    admin_password: SecretStr | None = Field(default=None, validate_default=True)
    admin_user: str | None = Field(default=None, validate_default=True)
    # The console's origin (app/api/security.py): the one other origin the admin API answers,
    # and the one a browser session may change state from. Unset: no other origin is answered,
    # and a session changes state from the API's own origin only.
    console_origin: str | None = None
    # The vendors of a call: the model (app/agent/model.py) and speech-to-text
    # (app/voice/pipeline.py), SPEECH_TO_TEXT. Without both keys a call is refused at set-up.
    anthropic_api_key: SecretStr | None = Field(default=None, validate_default=True)
    deepgram_api_key: SecretStr | None = Field(default=None, validate_default=True)
    llm_model: str = Field(default="claude-haiku-5-5", pattern=CLAUDE_MODEL_ID)
    # Speech out (app/voice/speech.py), TEXT_TO_SPEECH: its key and the voice. No voice is guessed:
    # without both, a call is refused at set-up, and outside dev the app does not start.
    elevenlabs_api_key: SecretStr | None = Field(default=None, validate_default=True)
    elevenlabs_voice_id: str | None = Field(
        default=None, validate_default=True, pattern=ELEVENLABS_VOICE_ID
    )
    tts_model: str = Field(default="eleven_flash_v2_5", pattern=LOW_LATENCY_TTS_MODEL)
    # The silence that ends a caller's turn (app/voice/pipeline.py).
    vad_stop_seconds: float = Field(default=0.5, ge=0.2, le=2.0)
    # Silence from the caller before the agent asks whether they are still there, and again
    # before it says goodbye.
    idle_seconds: int = Field(default=30, ge=5, le=300)

    @field_validator("anthropic_api_key", "deepgram_api_key", "elevenlabs_api_key")
    @classmethod
    def _vendor_key_outside_dev(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        name = info.field_name or ""
        if value is None:
            if _outside_dev(info) and not _admin(info):
                raise ValueError(f"{name} is required unless app_env is dev")
            return None
        _check_strong_secret(name, value, info)
        return value

    @field_validator("elevenlabs_voice_id")
    @classmethod
    def _voice_outside_dev(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None and _outside_dev(info) and not _admin(info):
            raise ValueError("elevenlabs_voice_id is required unless app_env is dev")
        return value

    @field_validator("identity_hash_key")
    @classmethod
    def _identity_key_unless_admin(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is None and not _admin(info):
            raise PydanticCustomError("missing", "Field required")
        return value

    @field_validator("audit_hmac_key", "identity_hash_key")
    @classmethod
    def hmac_key_is_set_and_strong_outside_dev(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if value is None:
            return None
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
    def identity_key_is_not_the_audit_key(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        audit_key = info.data.get("audit_hmac_key")
        if (
            value is not None
            and audit_key is not None
            and value.get_secret_value() == audit_key.get_secret_value()
        ):
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
        if value is None and _outside_dev(info) and not _admin(info):
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
        if value is None and _outside_dev(info) and not _admin(info):
            raise ValueError("turn_host is required unless app_env is dev")
        if "turn_shared_secret" in info.data:
            has_secret = info.data["turn_shared_secret"] is not None
            if has_secret != (value is not None):
                raise ValueError("turn_host and turn_shared_secret must be set together")
        return value

    @field_validator("call_access_token")
    @classmethod
    def _token_when_needed(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        if value is None:
            if _outside_dev(info) and not _admin(info):
                raise ValueError("call_access_token is required unless app_env is dev")
            if info.data.get("turn_shared_secret") is not None:
                raise ValueError("call_access_token is required when TURN is configured")
        else:
            _check_strong_secret("call_access_token", value, info)
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

    @field_validator("console_origin")
    @classmethod
    def _console_origin_is_one_origin(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        match = WEB_ORIGIN.fullmatch(value)
        if match is None or int(match["port"] or 0) > MAX_PORT:
            raise ValueError(
                "console_origin must be one origin: https:// (or http:// in dev), a lower-case"
                " host and an optional port; no path, no wildcard"
            )
        if match["scheme"] == "http" and _outside_dev(info):
            raise ValueError("outside dev console_origin must be https")
        if match["port"] is not None and int(match["port"]) == DEFAULT_PORTS[match["scheme"]]:
            raise ValueError(
                "console_origin must leave out its scheme's own port: a browser never sends it"
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


class DeploymentSettings(Settings, StartupSettings):
    """Every setting of the deployment at once, as the start-up check reads them: the start-up
    steps' and the application's, checked by both classes' rules as the role that holds them
    all (main() sets it). Each long-running role holds only some of the secrets, so only here
    are they all compared: every secret with every other, the database passwords included.
    Never the settings of a running application."""

    @model_validator(mode="after")
    def _every_secret_is_its_own(self) -> "DeploymentSettings":
        """No two secrets are the same: every SecretStr field, whatever its name, by the
        SHA-256 of its value; a clash names both fields and never a value."""
        seen: dict[bytes, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if not isinstance(value, SecretStr):
                continue
            digest = hashlib.sha256(value.get_secret_value().encode("utf-8")).digest()
            if digest in seen:
                raise ValueError(f"{name} must differ from {seen[digest]}")
            seen[digest] = name
        return self


# Every variable of either class, so that each can leave the other's in the shared .env file.
_EVERY_SETTING = frozenset(Settings.model_fields) | frozenset(StartupSettings.model_fields)

# The settings each service's container is given, and no other (deploy/docker-compose.yml; CI
# compares the running containers with these). The voice role: every application setting but
# the admin's. The admin role: the database as the runtime role, the audit key, the clock and
# the calls' longest (the live calls follow both), the model and the most calls at once (the
# config page shows them), the admin's sign-in and the console's origin; no key of a vendor, no
# TURN secret, no access token, no identity key. The start-up service: every setting of the
# deployment but the role, which its check sets.
VOICE_FIELDS = frozenset(Settings.model_fields) - {"admin_user", "admin_password", "console_origin"}
ADMIN_FIELDS = frozenset(
    {
        "app_env",
        "log_level",
        "app_role",
        "postgres_host",
        "postgres_port",
        "postgres_db",
        "app_db_user",
        "app_db_password",
        "audit_hmac_key",
        "simulated_now",
        "max_call_seconds",
        "llm_model",
        "max_concurrent_calls",
        "admin_user",
        "admin_password",
        "console_origin",
    }
)
STARTUP_CHECK_FIELDS = frozenset(DeploymentSettings.model_fields) - {"app_role"}


def main() -> None:
    """`python -m app.config`: the start-up service's first step, which checks every setting
    of the deployment from the environment, together, before anything touches the database.
    Every error is reported, by field name and a fixed message, never a value. Each running
    application checks its own settings again when it starts."""
    try:
        DeploymentSettings(app_role="all")
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
