"""A database for each run of a scenario, made as the start-up service makes the application's:
migrated, with the runtime role set up and the seed loaded for the scenario's day. It is
dropped after the run.

Like the test suite (tests/conftest.py), the evals use the dedicated database server that the
POSTGRES_* environment variables name (never .env), as its superuser, and a runtime role of
their own with a random password: one that does not exist yet, dropped at the end, so the evals
never take over or drop a role they did not make. Each run's database is copied from one
template, migrated once.
"""

import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.config import StartupSettings
from app.db.roles import set_up_runtime_role
from app.db.seed import load_seed_data, seed
from app.db.session import (
    create_database_engine,
    create_owner_engine,
    create_owner_sync_engine,
    create_session_factory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE = text("SELECT 1 FROM pg_roles WHERE rolname = :name")
# Names that need no quoting in a statement.
PLAIN_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


def _new_name() -> str:
    return f"sva_eval_{uuid4().hex[:12]}"


def eval_settings() -> StartupSettings:
    """The server the POSTGRES_* environment variables name, and a new runtime role."""
    return StartupSettings(
        _env_file=None,
        app_env="dev",
        app_db_user=f"{_new_name()}_app",
        app_db_password=secrets.token_urlsafe(24),
    )


def _as_owner(settings: StartupSettings, *statements: str) -> None:
    """Run the statements, each on its own, in the settings' database."""
    engine = create_owner_sync_engine(settings, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _role_exists(settings: StartupSettings) -> bool:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            found = connection.execute(ROLE, {"name": settings.app_db_user}).first()
        return found is not None
    finally:
        engine.dispose()


def _plain(name: str) -> str:
    """A database or role name the evals made, which goes into a statement as it is."""
    if not PLAIN_NAME.fullmatch(name):
        raise ValueError("a name the evals use is lower-case letters, digits and underscores")
    return name


def _create(settings: StartupSettings, name: str, template: str | None = None) -> None:
    statement = f"CREATE DATABASE {_plain(name)}"
    if template is not None:
        statement += f" TEMPLATE {_plain(template)}"
    _as_owner(settings, statement)


def _drop(settings: StartupSettings, name: str) -> None:
    _as_owner(settings, f"DROP DATABASE IF EXISTS {_plain(name)} WITH (FORCE)")


def _drop_role(settings: StartupSettings) -> None:
    """Drop the evals' role, after what it holds on shared objects, if it was made."""
    if _role_exists(settings):
        role = _plain(settings.app_db_user)
        _as_owner(settings, f"DROP OWNED BY {role}", f"DROP ROLE {role}")


def _migrate(settings: StartupSettings) -> None:
    """Alembic, as the start-up service runs it, from a folder without a .env."""
    environment = {
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
    }
    command = [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini")]
    with tempfile.TemporaryDirectory() as empty:
        done = subprocess.run(  # noqa: S603 (this interpreter, fixed arguments)
            [*command, "upgrade", "head"],
            cwd=empty,
            env=environment,
            capture_output=True,
            check=False,
        )
    if done.returncode != 0:
        raise RuntimeError("the migrations of the evals' template failed")


def _set_up_role(settings: StartupSettings) -> None:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            set_up_runtime_role(connection, settings)
    finally:
        engine.dispose()


class EvalServer:
    """The server, the evals' runtime role, and the migrated template."""

    def __init__(self, settings: StartupSettings, template: str) -> None:
        self._settings = settings
        self._template = template

    @asynccontextmanager
    async def database(self, today: date) -> AsyncIterator[AsyncEngine]:
        """A new database seeded for `today`, as the runtime role's engine; dropped after."""
        name = _new_name()
        _create(self._settings, name, template=self._template)
        settings = self._settings.model_copy(update={"postgres_db": name})
        try:
            _set_up_role(settings)
            owner = create_owner_engine(settings)
            try:
                async with create_session_factory(owner)() as session, session.begin():
                    await seed(session, load_seed_data(), today)
            finally:
                await owner.dispose()
            engine = create_database_engine(settings)
            try:
                yield engine
            finally:
                await engine.dispose()
        finally:
            _drop(self._settings, name)


@contextmanager
def eval_server(settings: StartupSettings) -> Iterator[EvalServer]:
    """The template migrated, and the role and the template dropped at the end. The settings'
    runtime role must not exist yet."""
    if _role_exists(settings):
        raise RuntimeError("the evals' runtime role must be a new one")
    template = f"{_new_name()}_template"
    _create(settings, template)
    try:
        _migrate(settings.model_copy(update={"postgres_db": template}))
        yield EvalServer(settings, template)
    finally:
        _drop(settings, template)
        _drop_role(settings)
