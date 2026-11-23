import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.session import create_database_engine, create_session_factory

REPO_ROOT = Path(__file__).resolve().parents[1]

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
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg's async mode cannot run on the proactor loop that Windows uses by default.
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="session")
def server_settings() -> Settings:
    """The database server the tests run against, taken from the POSTGRES_* variables."""
    return Settings(_env_file=None, app_env="dev")


def with_database(settings: Settings, name: str) -> Settings:
    return settings.model_copy(update={"postgres_db": name})


def admin_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def create_database(settings: Settings, name: str, template: str | None = None) -> None:
    engine = admin_engine(settings)
    quote = engine.dialect.identifier_preparer.quote
    statement = f"CREATE DATABASE {quote(name)}"
    if template is not None:
        statement += f" TEMPLATE {quote(template)}"
    with engine.connect() as connection:
        connection.execute(text(statement))


def drop_database(settings: Settings, name: str) -> None:
    engine = admin_engine(settings)
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {quote(name)} WITH (FORCE)"))


def database_env(settings: Settings) -> dict[str, str]:
    """Process environment that points the app's settings at the given database."""
    return {
        **os.environ,
        "APP_ENV": "dev",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
    }


def python_module(settings: Settings, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `python -m <args>` against the given database, from a directory without a .env."""
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        env=database_env(settings),
        capture_output=True,
        text=True,
        check=False,
    )


def alembic(settings: Settings, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return python_module(settings, cwd, "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args)


@pytest.fixture
def run_module(tmp_path: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`run_module(settings, "pkg.module", *args)`: run a module against a database."""

    def run(settings: Settings, *args: str) -> subprocess.CompletedProcess[str]:
        return python_module(settings, tmp_path, *args)

    return run


@pytest.fixture
def run_alembic(tmp_path: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`run_alembic(settings, *args)`: run the Alembic command line against a database."""

    def run(settings: Settings, *args: str) -> subprocess.CompletedProcess[str]:
        return alembic(settings, tmp_path, *args)

    return run


@pytest.fixture
def new_database(server_settings: Settings) -> Iterator[Callable[[], Settings]]:
    """`new_database()`: create an empty database; all are dropped after the test."""
    created: list[str] = []

    def create() -> Settings:
        name = f"sva_test_{uuid4().hex[:12]}"
        create_database(server_settings, name)
        created.append(name)
        return with_database(server_settings, name)

    try:
        yield create
    finally:
        for name in created:
            drop_database(server_settings, name)


@pytest.fixture
def empty_database(new_database: Callable[[], Settings]) -> Settings:
    return new_database()


@pytest.fixture(scope="session")
def migrated_template(
    server_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    name = f"sva_test_{uuid4().hex[:12]}_template"
    create_database(server_settings, name)
    try:
        result = alembic(
            with_database(server_settings, name),
            tmp_path_factory.mktemp("alembic"),
            "upgrade",
            "head",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        yield name
    finally:
        drop_database(server_settings, name)


@pytest.fixture
def database(server_settings: Settings, migrated_template: str) -> Iterator[Settings]:
    """A fresh database migrated to head, dropped after the test."""
    name = f"sva_test_{uuid4().hex[:12]}"
    create_database(server_settings, name, template=migrated_template)
    try:
        yield with_database(server_settings, name)
    finally:
        drop_database(server_settings, name)


@pytest.fixture
def sync_engine(database: Settings) -> Iterator[Engine]:
    engine = create_engine(database.database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
async def session(database: Settings) -> AsyncIterator[AsyncSession]:
    engine = create_database_engine(database)
    try:
        async with create_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()
