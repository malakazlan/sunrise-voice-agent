from typing import Annotated, Literal

from pydantic import Field, PositiveInt, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "demo", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
HostName = Annotated[str, Field(pattern=r"^[A-Za-z0-9.-]+$")]


def _outside_dev(info: ValidationInfo) -> bool:
    return info.data.get("app_env") in ("demo", "prod")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="forbid", hide_input_in_errors=True, env_ignore_empty=True
    )

    app_env: AppEnv
    log_level: LogLevel = "INFO"
    max_concurrent_calls: PositiveInt = 3
    max_call_seconds: PositiveInt = 600
    # The voice fields are checked in this order, each against the ones before it, so that
    # every error is raised on a field whose value is missing and no secret is echoed in it.
    turn_shared_secret: SecretStr | None = Field(default=None, validate_default=True)
    turn_host: HostName | None = Field(default=None, validate_default=True)
    demo_access_token: SecretStr | None = Field(default=None, validate_default=True)

    @field_validator("turn_shared_secret")
    @classmethod
    def _secret_outside_dev(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        if value is None and _outside_dev(info):
            raise ValueError("turn_shared_secret is required unless app_env is dev")
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
        return value
