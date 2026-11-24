import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.pool import NullPool

from app.config import Settings, StartupSettings
from app.db.roles import set_up_runtime_role
from app.db.session import (
    create_database_engine,
    create_owner_engine,
    create_owner_sync_engine,
    create_session_factory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The audit key every test uses unless it sets its own: long enough for any APP_ENV, built from
# short pieces so that no file holds a key-shaped literal.
TEST_AUDIT_KEY = "-".join(["test", "audit", "key"] * 4)

# Required values for tests that only build StartupSettings and never connect.
OFFLINE_DB_ENV = {
    "POSTGRES_HOST": "db.invalid",
    "POSTGRES_DB": "sunrise_offline",
    "POSTGRES_USER": "sunrise_offline",
    "POSTGRES_PASSWORD": "offline-test-password",
    "APP_DB_USER": "sunrise_offline_app",
    "APP_DB_PASSWORD": "offline-test-app-password",
    "AUDIT_HMAC_KEY": TEST_AUDIT_KEY,
}

# Obviously fake voice settings that let a demo or prod configuration validate. Built from
# short pieces so that no secret scanner reads them as credentials.
VOICE_SECRETS = {
    "DEMO_ACCESS_TOKEN": "-".join(["token", "not", "for", "logs"] + ["x" * 4] * 3),
    "TURN_HOST": "turn.example.test",
    "TURN_SHARED_SECRET": "-".join(["turn", "secret", "not", "for", "logs"] + ["y" * 4] * 3),
}


# pytest's own plugin for running an inner test session (tests/test_skip_hook.py).
pytest_plugins = ["pytester"]


class NoSkipsPlugin:
    """No test may end skipped, xfailed or xpassed, however the skip was written (marker,
    call, alias, exception, collection-time skip). Each one is recorded and the run fails at
    the end. The record belongs to the instance, so an inner session gets its own.
    """

    def __init__(self) -> None:
        self.not_run: list[str] = []

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if hasattr(report, "wasxfail"):
            self.not_run.append(f"{report.nodeid}: xfailed or xpassed")
        elif report.skipped:
            self.not_run.append(f"{report.nodeid}: skipped")

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.not_run.append(f"{report.nodeid}: skipped at collection")

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        if self.not_run:
            terminalreporter.section("tests that must run but did not", red=True)
            for line in self.not_run:
                terminalreporter.line(line)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.not_run and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(NoSkipsPlugin(), "no-skips")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable for the duration of the test."""
    for key in list(os.environ):
        monkeypatch.delenv(key)


@pytest.fixture
def db_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean environment holding only the required database settings and the audit key."""
    for key, value in OFFLINE_DB_ENV.items():
        monkeypatch.setenv(key, value)


def offline_settings(**overrides: Any) -> Settings:
    """Demo settings with every required value, for app tests that never connect."""
    values: dict[str, Any] = {
        key.lower(): value
        for key, value in OFFLINE_DB_ENV.items()
        if key.lower() in Settings.model_fields
    }
    values.update({key.lower(): value for key, value in VOICE_SECRETS.items()})
    values["app_env"] = "demo"
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def offline_env(db_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The required database settings plus the voice settings a demo or prod app needs."""
    for key, value in VOICE_SECRETS.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(scope="session")
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # psycopg's async mode cannot run on the proactor loop that Windows uses by default.
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


def new_role_name(kind: str) -> str:
    """A runtime role name no other test run uses: roles belong to the whole server, so two
    runs, or a role left behind by a run that crashed, never meet."""
    return f"sva_test_{uuid4().hex[:12]}_{kind}"


def drop_role(settings: StartupSettings, name: str) -> None:
    """Drop a runtime role, after revoking what it holds in this database and on shared
    objects. Grants it still has in another database make the drop fail, so a leak shows."""
    engine = admin_engine(settings)
    quote = engine.dialect.identifier_preparer.quote_identifier
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}
            ).first()
            if exists is not None:
                connection.execute(text(f"DROP OWNED BY {quote(name)}"))
                connection.execute(text(f"DROP ROLE {quote(name)}"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def server_settings() -> Iterator[StartupSettings]:
    """The database server the tests run against, as the start-up steps see it: the owner
    from the POSTGRES_* variables, and a runtime role of this test run's own, with a random
    password. The role is created in each test database by the `database` fixture and dropped
    when the run ends."""
    settings = StartupSettings(
        _env_file=None,
        app_env="dev",
        app_db_user=new_role_name("app"),
        app_db_password=secrets.token_urlsafe(24),
    )
    try:
        yield settings
    finally:
        drop_role(settings, settings.app_db_user)


def with_database(settings: StartupSettings, name: str) -> StartupSettings:
    return settings.model_copy(update={"postgres_db": name})


def admin_engine(settings: StartupSettings) -> Engine:
    return create_owner_sync_engine(settings, isolation_level="AUTOCOMMIT", poolclass=NullPool)


def set_up_roles(settings: StartupSettings) -> None:
    """The start-up role set-up, run as the owner in the settings' database."""
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            set_up_runtime_role(connection, settings)
    finally:
        engine.dispose()


def create_database(settings: StartupSettings, name: str, template: str | None = None) -> None:
    engine = admin_engine(settings)
    quote = engine.dialect.identifier_preparer.quote
    statement = f"CREATE DATABASE {quote(name)}"
    if template is not None:
        statement += f" TEMPLATE {quote(template)}"
    with engine.connect() as connection:
        connection.execute(text(statement))


def drop_database(settings: StartupSettings, name: str) -> None:
    engine = admin_engine(settings)
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {quote(name)} WITH (FORCE)"))


def database_env(settings: StartupSettings) -> dict[str, str]:
    """Process environment that points the app's settings at the given database."""
    return {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "APP_ENV": "dev",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "APP_DB_USER": settings.app_db_user,
        "APP_DB_PASSWORD": settings.app_db_password.get_secret_value(),
        "AUDIT_HMAC_KEY": TEST_AUDIT_KEY,
    }


def python_module(
    settings: StartupSettings, cwd: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run `python -m <args>` against the given database, from a directory without a .env."""
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        env=database_env(settings),
        capture_output=True,
        text=True,
        check=False,
    )


def alembic(settings: StartupSettings, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return python_module(settings, cwd, "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args)


@pytest.fixture
def run_module(tmp_path: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`run_module(settings, "pkg.module", *args)`: run a module against a database."""

    def run(settings: StartupSettings, *args: str) -> subprocess.CompletedProcess[str]:
        return python_module(settings, tmp_path, *args)

    return run


@pytest.fixture
def run_alembic(tmp_path: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """`run_alembic(settings, *args)`: run the Alembic command line against a database."""

    def run(settings: StartupSettings, *args: str) -> subprocess.CompletedProcess[str]:
        return alembic(settings, tmp_path, *args)

    return run


@pytest.fixture
def new_database(server_settings: StartupSettings) -> Iterator[Callable[[], StartupSettings]]:
    """`new_database()`: create an empty database; all are dropped after the test."""
    created: list[str] = []

    def create() -> StartupSettings:
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
def empty_database(new_database: Callable[[], StartupSettings]) -> StartupSettings:
    return new_database()


@pytest.fixture(scope="session")
def migrated_template(
    server_settings: StartupSettings, tmp_path_factory: pytest.TempPathFactory
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
def database(server_settings: StartupSettings, migrated_template: str) -> Iterator[StartupSettings]:
    """A fresh database migrated to head with the runtime role set up in it, as the container
    starts it; dropped after the test. Database-level privileges are not copied from a
    template, so the set-up runs for each database."""
    name = f"sva_test_{uuid4().hex[:12]}"
    create_database(server_settings, name, template=migrated_template)
    try:
        settings = with_database(server_settings, name)
        set_up_roles(settings)
        yield settings
    finally:
        drop_database(server_settings, name)


@pytest.fixture
def owner_engine(database: StartupSettings) -> Iterator[Engine]:
    """The owner, for set-up, schema checks and reading what the application committed."""
    engine = create_owner_sync_engine(database, poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
async def session(database: StartupSettings) -> AsyncIterator[AsyncSession]:
    """A session as the application has it: the runtime role."""
    engine = create_database_engine(database)
    try:
        async with create_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
async def owner_session(database: StartupSettings) -> AsyncIterator[AsyncSession]:
    """A session as the start-up seed has it: the owner."""
    engine = create_owner_engine(database)
    try:
        async with create_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


# Tests reach the driver connection under an engine only through these two fixtures; the
# repo hygiene test allows raw-connection access in this file and in app/db/session.py only.
@pytest.fixture
def close_driver_connection() -> Callable[[Connection], None]:
    """Close the libpq connection under a live connection, as a lost link would."""

    def close(connection: Connection) -> None:
        driver = connection.connection.driver_connection
        assert driver is not None
        driver.pgconn.finish()

    return close


@pytest.fixture
def close_async_driver_connection() -> Callable[[AsyncConnection], Awaitable[None]]:
    """The same for a connection from the async engine."""

    async def close(connection: AsyncConnection) -> None:
        driver = (await connection.get_raw_connection()).driver_connection
        assert driver is not None
        driver.pgconn.finish()

    return close


# audit_events' triggers refuse UPDATE, DELETE and TRUNCATE, and any INSERT out of chain
# position, for ordinary statements from every role, superusers included. Tamper tests must get
# past them, to show that the chain catches what an owner could do: this fixture switches
# ordinary triggers off for one transaction with session_replication_role=replica, which only a
# superuser may set. It is not the only way past them: whoever owns the table can disable or
# drop the triggers, or drop the table. The hygiene scan that keeps this setting out of other
# files is a lint against accidents, not a control; the control is the runtime role the
# application connects as (app/db/roles.py), which neither owns the table nor is a superuser and
# may not set this (tests/test_roles.py). The engine passed in is the owner's.
@pytest.fixture
def tamper_with_audit_events() -> Callable[..., None]:
    """`tamper_with_audit_events(engine, *statements)`: run statements past audit_events'
    triggers, in one transaction. A statement is SQL text, or (SQL text, bound parameters).
    """

    def tamper(engine: Engine, *statements: str | tuple[str, dict[str, Any]]) -> None:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            for statement in statements:
                sql, parameters = (statement, {}) if isinstance(statement, str) else statement
                connection.execute(text(sql), parameters)

    return tamper
