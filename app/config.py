from typing import Literal

from pydantic import SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

AppEnv = Literal["dev", "demo", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# The POSTGRES_PASSWORD value in .env.example. It is only accepted when APP_ENV=dev.
EXAMPLE_PASSWORD = "change-me-to-a-long-random-value"  # noqa: S105 (a published placeholder)
MIN_PASSWORD_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid", hide_input_in_errors=True)

    app_env: AppEnv
    log_level: LogLevel = "INFO"

    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: SecretStr

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
        return value

    @property
    def database_url(self) -> URL:
        """The only place a database URL is assembled. Its str() and repr() mask the password."""
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )
