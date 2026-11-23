import os

import pytest

from app.config import Settings

# Connection values for tests that only build Settings and never connect.
OFFLINE_DB_ENV = {
    "POSTGRES_HOST": "db.invalid",
    "POSTGRES_DB": "sunrise_offline",
    "POSTGRES_USER": "sunrise_offline",
    "POSTGRES_PASSWORD": "offline-test-password",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable for the duration of the test."""
    for key in list(os.environ):
        monkeypatch.delenv(key)


@pytest.fixture
def db_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean environment holding only the required database settings."""
    for key, value in OFFLINE_DB_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(scope="session")
def server_settings() -> Settings:
    """The database server the tests run against, taken from the POSTGRES_* variables."""
    return Settings(_env_file=None, app_env="dev")
