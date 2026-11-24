import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import TextClause, inspect, text
from sqlalchemy.pool import NullPool

from app.config import StartupSettings
from app.db.models import Base
from app.db.session import create_owner_sync_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
TABLES = {"patients", "medications", "appointments", "available_slots", "audit_events"}

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


def table_names(settings: StartupSettings) -> set[str]:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return set(inspect(connection).get_table_names())
    finally:
        engine.dispose()


# Every column as Postgres reports it, including its default (a serial key's sequence) and
# whether it is an identity column.
COLUMN_DEFINITIONS = text(
    "SELECT table_name, column_name, data_type, character_maximum_length, numeric_precision,"
    " numeric_scale, is_nullable, column_default, is_identity"
    " FROM information_schema.columns"
    " WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
)


def column_definitions(settings: StartupSettings) -> set[tuple[object, ...]]:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return {tuple(row) for row in connection.execute(COLUMN_DEFINITIONS).all()}
    finally:
        engine.dispose()


def constraint_definitions(settings: StartupSettings) -> set[tuple[str, str, str]]:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            rows = connection.execute(CONSTRAINT_DEFINITIONS).all()
    finally:
        engine.dispose()
    return {(table, name, definition) for table, name, definition in rows}


# Triggers and functions as Postgres renders them. Alembic compares neither.
TRIGGER_DEFINITIONS = text(
    "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid), tgenabled"
    " FROM pg_trigger WHERE NOT tgisinternal"
)
FUNCTION_DEFINITIONS = text(
    "SELECT proname, pg_get_functiondef(pro.oid) FROM pg_proc pro"
    " JOIN pg_namespace nsp ON nsp.oid = pro.pronamespace WHERE nsp.nspname = 'public'"
)


def definitions(settings: StartupSettings, query: TextClause) -> set[tuple[object, ...]]:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return {tuple(row) for row in connection.execute(query).all()}
    finally:
        engine.dispose()


def test_migration_matches_models(
    new_database: Callable[[], StartupSettings], run_alembic: Alembic
) -> None:
    migrated = new_database()
    upgrade = run_alembic(migrated, "upgrade", "head")
    assert upgrade.returncode == 0, upgrade.stderr

    engine = create_owner_sync_engine(migrated, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts=COMPARE_OPTIONS)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []
    assert set(Base.metadata.tables) == TABLES
    assert table_names(migrated) == TABLES | {"alembic_version"}

    # Alembic compares CHECK constraints by name only. Build the schema straight from the
    # models as well and compare every constraint's definition.
    from_models = new_database()
    engine = create_owner_sync_engine(from_models, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
    finally:
        engine.dispose()

    migrated_constraints = constraint_definitions(migrated)
    assert len(migrated_constraints) == 25
    assert migrated_constraints == constraint_definitions(from_models)
    assert (
        "audit_events",
        "ck_audit_events_actor",
        "CHECK ((actor ~ '^[A-Za-z0-9_:.-]{1,64}$'::text))",
    ) in migrated_constraints

    migrated_columns = column_definitions(migrated)
    assert len(migrated_columns) == 45
    assert migrated_columns == column_definitions(from_models)
    assert ("patients", "id", "text", None, None, None, "NO", None, "NO") in migrated_columns
    # seq has no default: the app assigns it under the lock, never a sequence.
    assert ("audit_events", "seq", "bigint", None, 64, 0, "NO", None, "NO") in migrated_columns

    migrated_triggers = definitions(migrated, TRIGGER_DEFINITIONS)
    assert {(table, name, enabled) for table, name, _, enabled in migrated_triggers} == {
        ("audit_events", "audit_events_refuse_update", "O"),
        ("audit_events", "audit_events_refuse_delete", "O"),
        ("audit_events", "audit_events_refuse_truncate", "O"),
        ("audit_events", "audit_events_check_position", "O"),
    }
    assert migrated_triggers == definitions(from_models, TRIGGER_DEFINITIONS)
    migrated_functions = definitions(migrated, FUNCTION_DEFINITIONS)
    assert {name for name, _ in migrated_functions} == {
        "audit_events_refuse_change",
        "audit_events_check_position",
    }
    assert migrated_functions == definitions(from_models, FUNCTION_DEFINITIONS)
    # Their search path is pinned, so nothing created later can shadow what they use, and the
    # table they read is named with its schema.
    for name, definition in migrated_functions:
        assert " SET search_path TO 'pg_catalog', 'pg_temp'\n" in str(definition), name
    check_position = [
        definition
        for name, definition in migrated_functions
        if name == "audit_events_check_position"
    ]
    assert "FROM public.audit_events" in str(check_position)
    assert (
        "available_slots",
        "id",
        "integer",
        None,
        32,
        0,
        "NO",
        "nextval('available_slots_id_seq'::regclass)",
        "NO",
    ) in migrated_columns


def test_upgrade_on_migrated_database_is_a_no_op(
    empty_database: StartupSettings, run_alembic: Alembic
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


def test_downgrade_removes_the_tables(
    empty_database: StartupSettings, run_alembic: Alembic
) -> None:
    assert run_alembic(empty_database, "upgrade", "head").returncode == 0

    downgrade = run_alembic(empty_database, "downgrade", "base")

    assert downgrade.returncode == 0, downgrade.stderr
    assert table_names(empty_database) == {"alembic_version"}
    assert definitions(empty_database, FUNCTION_DEFINITIONS) == set()


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
