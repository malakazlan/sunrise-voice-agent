from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "demo", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    app_env: AppEnv = "dev"
    log_level: LogLevel = "INFO"
