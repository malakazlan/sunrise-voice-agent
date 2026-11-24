"""The start-up steps never wait without a limit. A session of the runtime role holding a lock
a step needs makes that step fail within its lock timeout, saying what it waited for, and no
session of the runtime role is ended to get past it.
"""

import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from alembic import command
from alembic.config import Config
from conftest import set_up_roles
from psycopg import errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import StartupSettings
from app.db import seed
from app.db import session as session_module
from app.db.roles import ROLE_SETUP_LOCK

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[1]
# Short limits for these tests; the real ones are the session module's constants.
LOCK_TIMEOUT_MS = 2_000
STATEMENT_TIMEOUT_MS = 20_000
# How long a test waits from outside before it calls the step hung: well past the short lock
# timeout, and short of the 30 s after which the server ends the holding session's idle
# transaction (the runtime engine's setting), so a step without its limits is seen hanging.
OUTER_BOUND_SECONDS = 20


@pytest.fixture
def short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "STARTUP_LOCK_TIMEOUT_MS", LOCK_TIMEOUT_MS)
    monkeypatch.setattr(session_module, "STARTUP_STATEMENT_TIMEOUT_MS", STATEMENT_TIMEOUT_MS)


@pytest.fixture
def startup_env(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """The environment the start-up service has, from a directory without a .env."""
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "APP_ENV": "dev",
        "POSTGRES_HOST": database.postgres_host,
        "POSTGRES_PORT": str(database.postgres_port),
        "POSTGRES_DB": database.postgres_db,
        "POSTGRES_USER": database.postgres_user,
        "POSTGRES_PASSWORD": database.postgres_password.get_secret_value(),
        "APP_DB_USER": database.app_db_user,
        "APP_DB_PASSWORD": database.app_db_password.get_secret_value(),
    }.items():
        monkeypatch.setenv(name, value)
    yield


async def outcome_within(seconds: float, step: Callable[[], None]) -> tuple[BaseException, float]:
    """Run a start-up step in a thread; its exception and how long it took. Fails the test if
    the step has not returned after `seconds` (the thread is then left to finish on its own)."""
    raised: list[BaseException] = []

    def run() -> None:
        try:
            step()
        except BaseException as error:
            raised.append(error)

    started = time.monotonic()
    with anyio.fail_after(seconds):
        await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
    assert raised, "the step returned although a lock it needs is held"
    return raised[0], time.monotonic() - started


async def test_setup_fails_when_the_runtime_role_holds_its_lock(
    database: StartupSettings, runtime: AsyncEngine, short_timeouts: None
) -> None:
    # The reviewer's case: a session of the runtime role takes the set-up lock at session level
    # and idles outside a transaction, where no idle timeout applies.
    async with runtime.connect() as hostile:
        autocommit = await hostile.execution_options(isolation_level="AUTOCOMMIT")
        await autocommit.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ROLE_SETUP_LOCK})

        error, took = await outcome_within(OUTER_BOUND_SECONDS, lambda: set_up_roles(database))

        assert str(error) == (
            "role set-up: waited more than 2 s for the role set-up lock, which another session"
            " holds; stopping"
        )
        assert took < 10
        # The holder was not ended to get past it.
        assert await autocommit.scalar(text("SELECT 1")) == 1


async def test_seed_fails_when_the_runtime_role_holds_a_lock_it_needs(
    runtime: AsyncEngine, short_timeouts: None, startup_env: None
) -> None:
    # The runtime role may only read and insert patients, so it cannot lock the table against
    # the seed (that takes UPDATE, DELETE or TRUNCATE on it). It can still hold a row the seed
    # inserts: a patient with a seed id, inserted and not committed, makes the seed wait.
    with pytest.raises(DBAPIError) as refused:
        async with runtime.begin() as connection:
            await connection.execute(text("LOCK TABLE patients IN SHARE MODE"))
    assert type(refused.value.orig) is errors.InsufficientPrivilege
    async with runtime.begin() as hostile:
        await hostile.execute(
            text(
                "INSERT INTO patients (id, first_name, last_name, dob, phone,"
                " lab_results_status, balance_due)"
                " VALUES ('P001', 'Test', 'Patient', '1980-01-01', '555-000-0000', 'Pending', 0)"
            )
        )

        error, took = await outcome_within(
            OUTER_BOUND_SECONDS, lambda: seed.main(clock=lambda: datetime(2026, 11, 24, tzinfo=UTC))
        )

        assert str(error) == (
            "the seed: waited more than 2 s for a lock on a database object, which another"
            " session holds; stopping"
        )
        assert took < 10
        assert await hostile.scalar(text("SELECT 1")) == 1


async def test_migrations_fail_when_the_runtime_role_holds_a_table_lock(
    database: StartupSettings,
    runtime: AsyncEngine,
    run_alembic: Callable[..., subprocess.CompletedProcess[str]],
    short_timeouts: None,
    startup_env: None,
) -> None:
    # Back to the revision before the position trigger, whose CREATE TRIGGER needs a lock that
    # the runtime role's ROW EXCLUSIVE (from its INSERT privilege) conflicts with.
    downgrade = run_alembic(database, "downgrade", "5ec4a5850975")
    assert downgrade.returncode == 0, downgrade.stderr
    # In this process, so the short limits apply.
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    async with runtime.begin() as hostile:
        await hostile.execute(text("LOCK TABLE audit_events IN ROW EXCLUSIVE MODE"))

        error, took = await outcome_within(
            OUTER_BOUND_SECONDS, lambda: command.upgrade(config, "head")
        )

        assert str(error) == (
            "migrations: waited more than 2 s for a lock on a database object, which another"
            " session holds; stopping"
        )
        assert took < 10
        assert await hostile.scalar(text("SELECT 1")) == 1


def test_startup_sessions_have_both_limits(database: StartupSettings, short_timeouts: None) -> None:
    # Every engine the start-up steps use: the owner's, sync and async alike.
    engine = session_module.create_owner_sync_engine(database)
    try:
        with engine.connect() as connection:
            limits = connection.execute(
                text("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            ).one()
    finally:
        engine.dispose()
    assert tuple(limits) == ("2s", "20s")


async def test_a_statement_past_the_statement_timeout_fails(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the statement timeout adds to the lock timeout: a start-up statement that runs too
    # long for any other reason (here, a sleep) ends instead of holding the start-up.
    monkeypatch.setattr(session_module, "STARTUP_STATEMENT_TIMEOUT_MS", 1_000)
    engine = session_module.create_owner_engine(database)
    try:
        started = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT pg_sleep(30)"))
        took = time.monotonic() - started
    finally:
        await engine.dispose()
    failure = session_module.startup_wait_failure("a test step", excinfo.value)
    assert failure is not None
    assert str(failure) == "a test step: a statement ran longer than 1 s; stopping"
    assert took < 10
