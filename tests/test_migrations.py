import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
CLINIC_TABLES = {"patients", "medications", "appointments", "available_slots"}

Alembic = Callable[..., subprocess.CompletedProcess[str]]

# Alembic's default comparisons plus server defaults and named CHECK constraints, which it
# does not compare unless asked.
COMPARE_OPTIONS = {
    "compare_type": True,
    "compare_server_default": True,
    "autogenerate_plugins": ["alembic.autogenerate.*", "alembic.ext.checkconstraint_byname"],
}


# Every constraint as Postgres itself renders it, so that two schemas built different ways
# compare equal only if the database enforces the same rules.
CONSTRAINT_DEFINITIONS = text(
    "SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)"
    " FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid"
    " JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace"
    " WHERE nsp.nspname = 'public' AND rel.relname <> 'alembic_version'"
)


def table_names(settings: Settings) -> set[str]:
    engine = create_engine(settings.database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


def constraint_definitions(settings: Settings) -> set[tuple[str, str, str]]:
    engine = create_engine(settings.database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            rows = connection.execute(CONSTRAINT_DEFINITIONS).all()
    finally:
        engine.dispose()
    return {(table, name, definition) for table, name, definition in rows}


def test_migration_matches_models(
    new_database: Callable[[], Settings], run_alembic: Alembic
) -> None:
    migrated = new_database()
    upgrade = run_alembic(migrated, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    engine = create_engine(migrated.database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts=COMPARE_OPTIONS)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []
    assert set(Base.metadata.tables) == CLINIC_TABLES
    assert table_names(migrated) == CLINIC_TABLES | {"alembic_version"}

    # Alembic compares CHECK constraints by name only. Build the schema straight from the
    # models as well and compare every constraint's definition.
    from_models = new_database()
    engine = create_engine(from_models.database_url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
    finally:
        engine.dispose()

    migrated_constraints = constraint_definitions(migrated)
    assert len(migrated_constraints) == 17
    assert migrated_constraints == constraint_definitions(from_models)


def test_upgrade_on_migrated_database_is_a_no_op(
    empty_database: Settings, run_alembic: Alembic
) -> None:
    first = run_alembic(empty_database, "upgrade", "head")
    assert first.returncode == 0, first.stderr
    assert "Running upgrade" in first.stderr

    second = run_alembic(empty_database, "upgrade", "head")
    assert second.returncode == 0, second.stderr
    assert "Running upgrade" not in second.stderr

    current = run_alembic(empty_database, "current")
    assert current.returncode == 0, current.stderr
    assert "(head)" in current.stdout


def test_downgrade_removes_the_tables(empty_database: Settings, run_alembic: Alembic) -> None:
    assert run_alembic(empty_database, "upgrade", "head").returncode == 0

    downgrade = run_alembic(empty_database, "downgrade", "base")

    assert downgrade.returncode == 0, downgrade.stderr
    assert table_names(empty_database) == {"alembic_version"}


def test_migration_without_database_settings_names_them(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("POSTGRES_")}
    env["APP_ENV"] = "dev"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    for name in ("postgres_host", "postgres_db", "postgres_user", "postgres_password"):
        assert name in result.stderr, name
