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
    "call_turns",
    "handled_marks",
    "config_changes",
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


CONSOLE_INDEXES = text(
    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
    " AND indexname IN ('ix_audit_events_action_ts', 'ix_call_turns_at',"
    " 'ix_verification_attempts_created_at')"
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
    assert len(migrated_constraints) == 67
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

    # A call's turn is stored once, with ids of their declared shapes, registry names, and a
    # patient only when verified.
    assert {
        ("call_turns", "uq_call_turns_call_id_turn", "UNIQUE (call_id, turn)"),
        (
            "call_turns",
            "ck_call_turns_call_id",
            "CHECK ((call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text))",
        ),
        (
            "call_turns",
            "ck_call_turns_patient_id",
            "CHECK ((patient_id ~ '^P[0-9]{3,6}$'::text))",
        ),
        (
            "call_turns",
            "ck_call_turns_verified_patient",
            "CHECK ((verified = (patient_id IS NOT NULL)))",
        ),
        ("call_turns", "ck_call_turns_last_tool", "CHECK ((last_tool ~ '^[a-z_]{2,40}$'::text))"),
        ("call_turns", "ck_call_turns_turn", "CHECK ((turn >= 1))"),
        ("call_turns", "ck_call_turns_tool_rounds", "CHECK ((tool_rounds >= 0))"),
        ("call_turns", "ck_call_turns_record_us", "CHECK ((record_us >= 0))"),
        ("call_turns", "ck_call_turns_dropped_before", "CHECK ((dropped_before >= 0))"),
    } <= migrated_constraints

    # A queue row is marked handled once, by an admin, and only a row of a known source; a
    # config change holds a known key with one of its values, made by an admin.
    assert {
        ("handled_marks", "uq_handled_marks_source_row_id", "UNIQUE (source, row_id)"),
        (
            "handled_marks",
            "ck_handled_marks_source",
            "CHECK ((source = 'requests'::text))",
        ),
        ("handled_marks", "ck_handled_marks_row_id", "CHECK ((row_id >= 1))"),
        (
            "handled_marks",
            "ck_handled_marks_marked_by",
            "CHECK ((marked_by ~ '^[A-Za-z][A-Za-z_]{0,31}$'::text))",
        ),
        (
            "config_changes",
            "ck_config_changes_setting",
            "CHECK ((((key = 'calls'::text) AND (value = ANY (ARRAY['on'::text, 'off'::text])))"
            " OR ((key = 'office'::text) AND (value = ANY (ARRAY['schedule'::text,"
            " 'open'::text, 'closed'::text])))))",
        ),
        (
            "config_changes",
            "ck_config_changes_changed_by",
            "CHECK ((changed_by ~ '^[A-Za-z][A-Za-z_]{0,31}$'::text))",
        ),
    } <= migrated_constraints

    # The indexes the console's reads use: the audit log by action and time, the attempts and
    # the turns by time.
    assert definitions(migrated, CONSOLE_INDEXES) == {
        (
            "ix_audit_events_action_ts",
            "CREATE INDEX ix_audit_events_action_ts"
            " ON public.audit_events USING btree (action, ts)",
        ),
        ("ix_call_turns_at", "CREATE INDEX ix_call_turns_at ON public.call_turns USING btree (at)"),
        (
            "ix_verification_attempts_created_at",
            "CREATE INDEX ix_verification_attempts_created_at"
            " ON public.verification_attempts USING btree (created_at)",
        ),
    }
    assert definitions(from_models, CONSOLE_INDEXES) == definitions(migrated, CONSOLE_INDEXES)

    migrated_columns = column_definitions(migrated)
    assert len(migrated_columns) == 105
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
