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
TABLES = {
    "patients",
    "medications",
    "appointments",
    "available_slots",
    "audit_events",
    "verification_attempts",
    "requests",
    "messages",
    "call_sessions",
}

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
    assert len(migrated_constraints) == 46
    assert migrated_constraints == constraint_definitions(from_models)
    assert (
        "audit_events",
        "ck_audit_events_actor",
        "CHECK ((actor ~ '^[A-Za-z0-9_:.-]{1,64}$'::text))",
    ) in migrated_constraints
    # The attempts table holds a keyed hash and a call id of the declared shape, nothing else.
    assert (
        "verification_attempts",
        "ck_verification_attempts_claim_key",
        "CHECK ((claim_key ~ '^[0-9a-f]{64}$'::text))",
    ) in migrated_constraints
    # A call asks for the same thing once, and leaves the same message once: the same text, and
    # for a call back the same name and number too (a doctor's message has neither, and two
    # missing values count as the same).
    assert {
        ("requests", "uq_requests_call_id_type_summary", "UNIQUE (call_id, type, summary)"),
        (
            "messages",
            "uq_messages_call_id_kind_body_name_callback_number",
            "UNIQUE NULLS NOT DISTINCT (call_id, kind, body, name, callback_number)",
        ),
    } <= migrated_constraints

    # The kinds of request and message, and whether a message was left after hours.
    assert {
        (
            "requests",
            "ck_requests_type",
            "CHECK ((type = ANY (ARRAY['refill'::text, 'doctor_message'::text, 'booking'::text,"
            " 'callback'::text, 'transfer'::text])))",
        ),
        (
            "messages",
            "ck_messages_kind",
            "CHECK ((kind = ANY (ARRAY['doctor_message'::text, 'callback'::text])))",
        ),
    } <= migrated_constraints

    # A call is stored once, with a call id of the declared shape, one of the fixed reasons a
    # call ends, a patient only when verified and a transcript of at most 20,000 characters.
    assert {
        ("call_sessions", "uq_call_sessions_call_id", "UNIQUE (call_id)"),
        (
            "call_sessions",
            "ck_call_sessions_call_id",
            "CHECK ((call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text))",
        ),
        (
            "call_sessions",
            "ck_call_sessions_end_reason",
            "CHECK ((end_reason = ANY (ARRAY['completed'::text, 'emergency'::text,"
            " 'transferred'::text, 'caller_hung_up'::text, 'idle_timeout'::text,"
            " 'max_duration'::text, 'error'::text])))",
        ),
        (
            "call_sessions",
            "ck_call_sessions_verified_patient",
            "CHECK ((verified = (patient_id IS NOT NULL)))",
        ),
        (
            "call_sessions",
            "ck_call_sessions_transcript_length",
            "CHECK ((char_length(transcript) <= 20000))",
        ),
        ("call_sessions", "ck_call_sessions_turn_count", "CHECK ((turn_count >= 0))"),
    } <= migrated_constraints

    migrated_columns = column_definitions(migrated)
    assert len(migrated_columns) == 77
    assert (
        "messages",
        "after_hours",
        "boolean",
        None,
        None,
        None,
        "NO",
        "false",
        "NO",
    ) in migrated_columns
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


REQUEST_TYPE = text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_requests_type'"
)


def request_type_rule(settings: StartupSettings) -> str:
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return str(connection.execute(REQUEST_TYPE).scalar_one())
    finally:
        engine.dispose()


def test_booking_requests_step_down_and_up(
    empty_database: StartupSettings, run_alembic: Alembic
) -> None:
    with_booking = (
        "CHECK ((type = ANY (ARRAY['refill'::text, 'doctor_message'::text, 'booking'::text])))"
    )
    without = "CHECK ((type = ANY (ARRAY['refill'::text, 'doctor_message'::text])))"
    assert run_alembic(empty_database, "upgrade", "9a4f2c71d0e8").returncode == 0
    assert request_type_rule(empty_database) == with_booking

    step_down = run_alembic(empty_database, "downgrade", "-1")

    assert step_down.returncode == 0, step_down.stderr
    assert request_type_rule(empty_database) == without
    assert run_alembic(empty_database, "upgrade", "9a4f2c71d0e8").returncode == 0
    assert request_type_rule(empty_database) == with_booking


def test_callback_and_transfer_requests_step_down_to_booking(
    empty_database: StartupSettings, run_alembic: Alembic
) -> None:
    # The call endings' types come after booking's, and stepping back leaves booking's.
    every_type = (
        "CHECK ((type = ANY (ARRAY['refill'::text, 'doctor_message'::text, 'booking'::text,"
        " 'callback'::text, 'transfer'::text])))"
    )
    with_booking = (
        "CHECK ((type = ANY (ARRAY['refill'::text, 'doctor_message'::text, 'booking'::text])))"
    )
    assert run_alembic(empty_database, "upgrade", "head").returncode == 0
    assert request_type_rule(empty_database) == every_type

    step_down = run_alembic(empty_database, "downgrade", "9a4f2c71d0e8")

    assert step_down.returncode == 0, step_down.stderr
    assert request_type_rule(empty_database) == with_booking
    assert run_alembic(empty_database, "upgrade", "head").returncode == 0
    assert request_type_rule(empty_database) == every_type


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
