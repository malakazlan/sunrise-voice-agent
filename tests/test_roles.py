"""The runtime database role: what app/db/roles.py sets up from app/db/grants.py, checked
against Postgres's own catalogs and by trying, as the role, what it must not do.
"""

import re
import secrets
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import psycopg
import pytest
from conftest import (
    TEST_AUDIT_KEY,
    assert_refused_by_privileges,
    attempt,
    connects,
    database_env,
    drop_role,
    new_role_name,
    refused_by_privileges,
    set_up_roles,
)
from psycopg import errors, sql
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, TextClause, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.audit.log import Action, AuditLog, Event, Outcome, ResourceType
from app.config import StartupSettings
from app.db import grants
from app.db import session as session_module
from app.db.models import Base
from app.db.roles import runtime_role_differences, set_up_runtime_role
from app.db.session import create_database_engine, create_owner_sync_engine, create_session_factory

pytestmark = pytest.mark.anyio

Module = Callable[..., subprocess.CompletedProcess[str]]

# The spec's privilege table, written out here by hand. The declaration must say exactly this,
# so widening it is caught here as well as by the catalog comparison.
EXPECTED_TABLES = {
    "patients": {"SELECT", "INSERT"},
    "medications": {"SELECT"},
    "appointments": {"SELECT", "INSERT"},
    "available_slots": {"SELECT"},
    "audit_events": {"SELECT", "INSERT"},
    "verification_attempts": {"SELECT", "INSERT"},
    "requests": {"SELECT", "INSERT"},
    "messages": {"SELECT", "INSERT"},
    "call_sessions": {"SELECT", "INSERT"},
    "call_turns": {"SELECT", "INSERT"},
    # The admin's marks and config changes are added, never changed or removed.
    "handled_marks": {"SELECT", "INSERT"},
    "config_changes": {"SELECT", "INSERT"},
    "alembic_version": set(),
}
# Beyond its table's privileges, a column's own: booking takes a slot by changing this one.
EXPECTED_COLUMNS = {("available_slots", "is_available"): {"UPDATE"}}
# Only the appointments, verification attempts, requests, messages, call sessions, call turns,
# handled marks and config changes inserts take a number from a sequence; patients have text ids
# and audit_events numbers its own rows.
EXPECTED_SEQUENCES = {
    "appointments_id_seq": {"USAGE"},
    "verification_attempts_id_seq": {"USAGE"},
    "available_slots_id_seq": set(),
    "medications_id_seq": set(),
    "requests_id_seq": {"USAGE"},
    "messages_id_seq": {"USAGE"},
    "call_sessions_id_seq": {"USAGE"},
    "call_turns_id_seq": {"USAGE"},
    "handled_marks_id_seq": {"USAGE"},
    "config_changes_id_seq": {"USAGE"},
}
# The trigger functions of audit_events run as triggers whoever may call them; nobody needs
# EXECUTE, which Postgres gives everyone by default. Nor the catalog's large-object creators:
# a large object the role created would be one it owns.
EXPECTED_FUNCTIONS: dict[str, set[str]] = {
    "public.audit_events_check_position()": set(),
    "public.audit_events_refuse_change()": set(),
    "pg_catalog.lo_creat(integer)": set(),
    "pg_catalog.lo_create(oid)": set(),
    "pg_catalog.lo_from_bytea(oid, bytea)": set(),
    "pg_catalog.lo_import(text)": set(),
    "pg_catalog.lo_import(text, oid)": set(),
}
EXPECTED_SCHEMAS = {"public": {"USAGE"}}
EXPECTED_DATABASE = {"CONNECT"}

# Each object with every entry of its ACL: who holds which privilege, grantee 0 being PUBLIC.
# An object whose ACL was never changed has none stored; its default ACL applies. Schemas that
# Postgres itself provides (catalogs, TOAST, temporary) are left out; every other is compared.
RELATIONS = text(
    "SELECT n.nspname, c.relname, c.relkind, x.grantee, x.privilege_type"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " LEFT JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault("
    "   CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::\"char\", c.relowner))) x ON true"
    " WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
    " AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
# The schema's functions and the catalog's large-object creators, by schema, name and arguments.
FUNCTIONS = text(
    "SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid)"
    " || ')', x.grantee, x.privilege_type FROM pg_proc p"
    " JOIN pg_namespace n ON n.oid = p.pronamespace"
    " LEFT JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x ON true"
    " WHERE (n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)')"
    " OR (n.nspname = 'pg_catalog'"
    " AND p.proname IN ('lo_creat', 'lo_create', 'lo_from_bytea', 'lo_import'))"
)
SCHEMAS = text(
    "SELECT n.nspname, x.grantee, x.privilege_type FROM pg_namespace n"
    " LEFT JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) x ON true"
    " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
DATABASES = text(
    "SELECT d.datname, d.datname = current_database(), x.grantee, x.privilege_type"
    " FROM pg_database d"
    " LEFT JOIN LATERAL aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) x ON true"
)
COLUMN_GRANTS = text(
    "SELECT c.relname, a.attname, x.grantee, x.privilege_type FROM pg_attribute a"
    " JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
    " CROSS JOIN LATERAL aclexplode(a.attacl) x"
    " WHERE x.grantee IN (0, :role)"
    " AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
ROLE_OID = text("SELECT oid FROM pg_roles WHERE rolname = :name")
ATTRIBUTES = text(
    "SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls"
    " FROM pg_roles WHERE rolname = :name"
)
# What a role holds apart from object privileges, all of it server-wide.
MEMBERSHIPS = text("SELECT roleid::regrole::text FROM pg_auth_members WHERE member = :role")
OWNED = text(
    "SELECT classid::regclass::text, objid FROM pg_shdepend"
    " WHERE refclassid = 'pg_authid'::regclass AND refobjid = :role AND deptype = 'o'"
)
ROLE_SETTINGS = text("SELECT setdatabase, setconfig FROM pg_db_role_setting WHERE setrole = :role")
PARAMETER_GRANTS = text(
    "SELECT p.parname, x.privilege_type FROM pg_parameter_acl p"
    " CROSS JOIN LATERAL aclexplode(p.paracl) x WHERE x.grantee IN (0, :role)"
)
DEFAULT_GRANTS = text(
    "SELECT d.defaclobjtype, x.privilege_type FROM pg_default_acl d"
    " CROSS JOIN LATERAL aclexplode(d.defaclacl) x"
    " WHERE d.defaclrole = :role OR x.grantee IN (0, :role)"
)

TRIGGERS = (
    "audit_events_refuse_update",
    "audit_events_refuse_delete",
    "audit_events_refuse_truncate",
    "audit_events_check_position",
)
T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
CALL = "0123456789ab4def8123456789abcdef"


@dataclass(frozen=True, kw_only=True)
class Privileges:
    """What a role may do, per kind of object, keyed by name; `database` is this database, and
    `columns` holds only the columns with privileges of their own, keyed by table and column."""

    tables: dict[str, set[str]]
    sequences: dict[str, set[str]]
    functions: dict[str, set[str]]
    schemas: dict[str, set[str]]
    database: set[str]
    columns: dict[tuple[str, str], set[str]]


def declared() -> Privileges:
    return Privileges(
        tables={name: set(held) for name, held in grants.TABLE_PRIVILEGES.items()},
        sequences={name: set(held) for name, held in grants.SEQUENCE_PRIVILEGES.items()},
        functions={name: set(held) for name, held in grants.FUNCTION_PRIVILEGES.items()},
        schemas={grants.SCHEMA: set(grants.SCHEMA_PRIVILEGES)},
        database=set(grants.DATABASE_PRIVILEGES),
        columns={name: set(held) for name, held in grants.COLUMN_PRIVILEGES.items()},
    )


@dataclass(frozen=True, kw_only=True)
class Catalog:
    """The ACLs as Postgres stores them: `granted` to the role by name, `public` to everyone,
    `public_elsewhere` to everyone on the server's other databases. What the role can
    effectively do is the application's own comparison (app/db/roles.py)."""

    granted: Privileges
    public: Privileges
    public_elsewhere: dict[str, set[str]]


def catalog(engine: Engine, role: str) -> Catalog:
    granted = Privileges(
        tables={}, sequences={}, functions={}, schemas={}, database=set(), columns={}
    )
    public = Privileges(
        tables={}, sequences={}, functions={}, schemas={}, database=set(), columns={}
    )
    public_elsewhere: dict[str, set[str]] = {}
    with engine.connect() as connection:
        role_oid = connection.execute(ROLE_OID, {"name": role}).scalar_one()

        def note(
            kinds: tuple[dict[str, set[str]], dict[str, set[str]]],
            name: str,
            grantee: Any,
            held: Any,
        ) -> None:
            for kind in kinds:
                kind.setdefault(name, set())
            if grantee == role_oid:
                kinds[0][name].add(held)
            elif grantee == 0:
                kinds[1][name].add(held)

        for schema, name, kind, grantee, held in connection.execute(RELATIONS).all():
            assert schema == grants.SCHEMA, (schema, name)
            if kind == "S":
                note((granted.sequences, public.sequences), name, grantee, held)
            else:
                note((granted.tables, public.tables), name, grantee, held)
        for name, grantee, held in connection.execute(FUNCTIONS).all():
            note((granted.functions, public.functions), name, grantee, held)
        for name, grantee, held in connection.execute(SCHEMAS).all():
            note((granted.schemas, public.schemas), name, grantee, held)
        for name, here, grantee, held in connection.execute(DATABASES).all():
            if here:
                if grantee == role_oid:
                    granted.database.add(held)
                elif grantee == 0:
                    public.database.add(held)
            else:
                public_elsewhere.setdefault(name, set())
                if grantee == 0:
                    public_elsewhere[name].add(held)
        for table, column, grantee, held in connection.execute(
            COLUMN_GRANTS, {"role": role_oid}
        ).all():
            holder = granted if grantee == role_oid else public
            holder.columns.setdefault((table, column), set()).add(held)
    return Catalog(granted=granted, public=public, public_elsewhere=public_elsewhere)


def assert_privileges_match_the_declaration(engine: Engine, role: str) -> None:
    found = catalog(engine, role)
    expected = declared()
    # Everything the role may do, it was granted by name.
    assert found.granted == expected
    # PUBLIC holds nothing beyond the declaration: Postgres 16 gives it USAGE on the public
    # schema and CONNECT on the database; set-up takes TEMPORARY on the database, EXECUTE on the
    # schema's functions and the large-object creators, and everything on the other databases.
    assert found.public == Privileges(
        tables={name: set() for name in expected.tables},
        sequences={name: set() for name in expected.sequences},
        functions={name: set() for name in expected.functions},
        schemas={grants.SCHEMA: {"USAGE"}},
        database={"CONNECT"},
        columns={},
    )
    assert found.public_elsewhere, "the server has other databases"
    assert found.public_elsewhere == {name: set() for name in found.public_elsewhere}
    # And what the role can effectively do, by the comparison role set-up itself uses.
    with engine.connect() as connection:
        assert runtime_role_differences(connection, role) == []


async def append_one(audit: AuditLog, engine: AsyncEngine) -> None:
    async with create_session_factory(engine)() as session:
        event_ = Event(
            actor="agent",
            action=Action.CALL_STARTED,
            resource_type=ResourceType.CALL,
            outcome=Outcome.SUCCESS,
            call_id=CALL,
            resource_id=CALL,
        )
        assert await audit.append(session, event_, now=T0) == 1
        await session.commit()


async def stored_events(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT seq, outcome, mac FROM audit_events"))
        return [tuple(row) for row in rows.all()]


# --- R1 to R4: what the runtime role is refused ---------------------------------------------


async def test_runtime_role_cannot_change_audit_events(
    database: StartupSettings, runtime: AsyncEngine, owner_engine: Engine
) -> None:
    audit = AuditLog(SecretStr(TEST_AUDIT_KEY))
    await append_one(audit, runtime)
    before = await stored_events(runtime)
    statements = (
        "UPDATE audit_events SET outcome = 'failure'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    )

    for statement in statements:
        # The privilege check comes first: the error is not the triggers' (which name the table).
        await assert_refused_by_privileges(runtime, statement)

    # Without the triggers, the privileges alone still refuse.
    with owner_engine.begin() as connection:
        for trigger in TRIGGERS:
            connection.execute(text(f"DROP TRIGGER {trigger} ON audit_events"))
    for statement in statements:
        await assert_refused_by_privileges(runtime, statement)

    assert await stored_events(runtime) == before
    async with create_session_factory(runtime)() as session:
        assert (await audit.verify(session)).ok


async def test_runtime_role_cannot_bypass_triggers(
    database: StartupSettings, runtime: AsyncEngine
) -> None:
    for statement in (
        "SET session_replication_role = replica",
        "SET LOCAL session_replication_role = replica",
        "SELECT set_config('session_replication_role', 'replica', false)",
        "ALTER TABLE audit_events DISABLE TRIGGER ALL",
        "ALTER TABLE audit_events DISABLE TRIGGER USER",
        "ALTER TABLE audit_events DISABLE TRIGGER audit_events_refuse_update",
        "ALTER TABLE audit_events ENABLE REPLICA TRIGGER audit_events_check_position",
        "DROP TRIGGER audit_events_refuse_delete ON audit_events",
        "CREATE RULE skip_audit AS ON INSERT TO audit_events DO INSTEAD NOTHING",
    ):
        await assert_refused_by_privileges(runtime, statement)

    async with runtime.connect() as connection:
        mode = await connection.scalar(text("SHOW session_replication_role"))
        may_set = await connection.scalar(
            text("SELECT has_parameter_privilege('session_replication_role', 'SET')")
        )
    assert (mode, may_set) == ("origin", False)


async def test_runtime_role_cannot_change_the_schema(
    database: StartupSettings, runtime: AsyncEngine
) -> None:
    role = database.app_db_user
    owner = database.postgres_user
    for statement in (
        # Tables, views and indexes, in the schema and as temporary objects.
        "CREATE TABLE intruder (id int)",
        "CREATE TABLE public.intruder (id int)",
        "CREATE TEMPORARY TABLE intruder (id int)",
        "CREATE VIEW intruder AS SELECT 1 AS id",
        "CREATE INDEX intruder ON patients (phone)",
        "ALTER TABLE patients ADD COLUMN intruder text",
        "ALTER TABLE audit_events RENAME TO intruder",
        "DROP TABLE audit_events",
        "DROP TABLE alembic_version",
        "CREATE SCHEMA intruder",
        # Functions.
        "CREATE FUNCTION intruder() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "CREATE FUNCTION pg_temp.intruder() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "CREATE OR REPLACE FUNCTION audit_events_refuse_change() RETURNS trigger"
        " LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
        "ALTER FUNCTION audit_events_check_position() RENAME TO intruder",
        "DROP FUNCTION audit_events_check_position()",
        # Triggers.
        "CREATE TRIGGER intruder BEFORE INSERT ON patients"
        " FOR EACH ROW EXECUTE FUNCTION audit_events_check_position()",
        "DROP TRIGGER audit_events_check_position ON audit_events",
        # Roles, its own included.
        "CREATE ROLE intruder",
        f'ALTER ROLE "{role}" SUPERUSER',
        f'ALTER ROLE "{role}" CREATEROLE',
        f'ALTER ROLE "{owner}" NOLOGIN',
        f'DROP ROLE "{owner}"',
        f'GRANT "{owner}" TO "{role}"',
    ):
        await assert_refused_by_privileges(runtime, statement)


async def test_runtime_role_cannot_delete(runtime: AsyncEngine) -> None:
    tables = [*Base.metadata.tables, "alembic_version"]
    assert set(tables) >= {"patients", "medications", "appointments", "available_slots"}

    for table in tables:
        # The names come from the models, not from input.
        await assert_refused_by_privileges(runtime, f"DELETE FROM {table}")  # noqa: S608
        await assert_refused_by_privileges(runtime, f"TRUNCATE {table}")


async def test_audit_triggers_refusal_names_the_table(
    owner_engine: Engine, runtime: AsyncEngine
) -> None:
    # The tests above tell a privilege refusal from the triggers' by the table name the
    # triggers give. If the triggers stopped naming it, their refusal would pass for one by
    # privileges; this pins it.
    with pytest.raises(DBAPIError) as excinfo, owner_engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_events"))
    assert type(excinfo.value.orig) is errors.InsufficientPrivilege
    assert excinfo.value.orig.diag.table_name == "audit_events"
    assert not refused_by_privileges(excinfo.value)

    # The position trigger still fires for the runtime role, which may not EXECUTE it.
    out_of_position = await attempt(
        runtime,
        "INSERT INTO audit_events (seq, ts, actor, action, resource_type, outcome, prev_mac, mac)"
        " VALUES (5, now(), 'agent', 'call_started', 'call', 'success',"
        " repeat('0', 64), repeat('0', 64))",
    )
    assert out_of_position is not None
    assert type(out_of_position.orig) is errors.IntegrityConstraintViolation
    assert out_of_position.orig.diag.column_name == "seq"
    async with runtime.connect() as connection:
        may_execute = await connection.scalar(
            text("SELECT has_function_privilege('audit_events_check_position()', 'EXECUTE')")
        )
    assert may_execute is False


# The same table, tried statement by statement as the role. Each statement touches no row, so
# only the privilege check decides; it runs in a transaction that is rolled back.
FIRST_COLUMN = {
    "patients": "email",
    "medications": "pharmacy",
    "appointments": "reason_short",
    "available_slots": "source",
    "audit_events": "reason",
    "verification_attempts": "success",
    "requests": "patient_id",
    "messages": "name",
    "call_sessions": "patient_id",
    "call_turns": "patient_id",
    "handled_marks": "marked_by",
    "config_changes": "value",
    "alembic_version": "version_num",
}


async def test_runtime_role_is_refused_what_is_not_declared(runtime: AsyncEngine) -> None:
    statements = {
        "SELECT": "SELECT 1 FROM {table} LIMIT 0",
        "INSERT": "INSERT INTO {table} ({column}) SELECT NULL WHERE false",
        "UPDATE": "UPDATE {table} SET {column} = NULL WHERE false",
        "DELETE": "DELETE FROM {table} WHERE false",
        "TRUNCATE": "TRUNCATE {table}",
    }
    allowed: dict[str, set[str]] = {}
    for table, column in FIRST_COLUMN.items():
        allowed[table] = set()
        for privilege, statement in statements.items():
            error = await attempt(runtime, statement.format(table=table, column=column))
            if error is None:
                allowed[table].add(privilege)
            else:
                assert type(error.orig) is errors.InsufficientPrivilege, (table, privilege)
                # Past the privilege check, a trigger refused it: the role holds the privilege.
                if not refused_by_privileges(error):
                    allowed[table].add(privilege)
    assert allowed == EXPECTED_TABLES
    # A column with an UPDATE of its own: allowed for that column, while the table refuses the
    # others (the loop above).
    for table, column in EXPECTED_COLUMNS:
        statement = f"UPDATE {table} SET {column} = {column} WHERE false"  # noqa: S608
        assert await attempt(runtime, statement) is None

    sequences: dict[str, set[str]] = {}
    for sequence in EXPECTED_SEQUENCES:
        sequences[sequence] = set()
        if await attempt(runtime, f"SELECT nextval('{sequence}')") is None:
            sequences[sequence].add("USAGE")
        if await attempt(runtime, f"SELECT last_value FROM {sequence}") is None:  # noqa: S608
            sequences[sequence].add("SELECT")
    assert sequences == EXPECTED_SEQUENCES


# --- R5, R6: what the catalogs say ----------------------------------------------------------


def test_privileges_match_the_declaration(database: StartupSettings, owner_engine: Engine) -> None:
    # The declaration is the spec's table, and covers every table the models define.
    assert declared() == Privileges(
        tables=EXPECTED_TABLES,
        sequences=EXPECTED_SEQUENCES,
        functions=EXPECTED_FUNCTIONS,
        schemas=EXPECTED_SCHEMAS,
        database=EXPECTED_DATABASE,
        columns=EXPECTED_COLUMNS,
    )
    assert set(grants.TABLE_PRIVILEGES) == {*Base.metadata.tables, "alembic_version"}

    # Postgres agrees, both ways, for every table, sequence and schema in the database.
    found = catalog(owner_engine, database.app_db_user)
    assert set(found.granted.tables) == set(EXPECTED_TABLES)
    assert set(found.granted.sequences) == set(EXPECTED_SEQUENCES)
    assert set(found.granted.functions) == set(EXPECTED_FUNCTIONS)
    assert_privileges_match_the_declaration(owner_engine, database.app_db_user)


def role_facts(engine: Engine, role: str) -> dict[str, Any]:
    with engine.connect() as connection:
        oid = connection.execute(ROLE_OID, {"name": role}).scalar_one()
        return {
            "attributes": tuple(connection.execute(ATTRIBUTES, {"name": role}).one()),
            "member_of": connection.execute(MEMBERSHIPS, {"role": oid}).scalars().all(),
            "owns": connection.execute(OWNED, {"role": oid}).all(),
            "settings": connection.execute(ROLE_SETTINGS, {"role": oid}).all(),
            "parameters": connection.execute(PARAMETER_GRANTS, {"role": oid}).all(),
            "default_grants": connection.execute(DEFAULT_GRANTS, {"role": oid}).all(),
        }


# superuser, create roles, create databases, log in, replication, bypass row security
EXPECTED_ATTRIBUTES = (False, False, False, True, False, False)


def assert_role_holds_nothing_else(engine: Engine, role: str) -> None:
    assert role_facts(engine, role) == {
        "attributes": EXPECTED_ATTRIBUTES,
        "member_of": [],
        "owns": [],
        "settings": [],
        "parameters": [],
        "default_grants": [],
    }


async def test_runtime_role_attributes(
    database: StartupSettings, owner_engine: Engine, runtime: AsyncEngine
) -> None:
    assert_role_holds_nothing_else(owner_engine, database.app_db_user)

    # As the role: no database can be created (outside a transaction block, where it runs).
    async with runtime.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        with pytest.raises(DBAPIError) as excinfo:
            await autocommit.execute(text("CREATE DATABASE intruder"))
    assert type(excinfo.value.orig) is errors.InsufficientPrivilege


# --- R7, R8, R12: credentials ---------------------------------------------------------------


def password_statements(statements: list[str]) -> list[str]:
    return [statement for statement in statements if "PASSWORD '" in statement.upper()]


SCRAM_IN_STATEMENT = re.compile(
    r"PASSWORD 'SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+'"
)


def chain(error: BaseException) -> Iterator[BaseException]:
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending += [current.__cause__, current.__context__, getattr(current, "orig", None)]
    return iter(seen)


def test_role_password_is_never_in_sql_or_errors(
    database: StartupSettings,
    fresh_role: StartupSettings,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = "-".join(["runtime", "password", "not", "for", "logs"])
    settings = fresh_role.model_copy(update={"app_db_password": SecretStr(password)})
    # Every logger at its most verbose, SQLAlchemy's statement log included.
    caplog.set_level("DEBUG")
    caplog.set_level("DEBUG", logger="sqlalchemy")

    # What psycopg was asked to send, whichever way it came: through SQLAlchemy, or straight
    # through the driver connection. psycopg's Cursor.execute is Python in every build.
    sent_to_driver: list[str] = []
    parameters_sent: list[str] = []
    execute = psycopg.Cursor.execute

    def recording_execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(query, sql.Composable):
            sent_to_driver.append(query.as_string(cursor))
        else:
            sent_to_driver.append(query.decode() if isinstance(query, bytes) else str(query))
        parameters_sent.extend(repr(arg) for arg in (*args, *kwargs.values()))
        return execute(cursor, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", recording_execute)
    seen_by_sqlalchemy: list[str] = []

    def record(conn: Connection, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        seen_by_sqlalchemy.extend([statement, repr(parameters)])

    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    event.listen(engine, "before_cursor_execute", record)
    try:
        with engine.begin() as connection:
            # The verifier's algorithm is named, never taken from the server's setting.
            connection.execute(text("SET LOCAL password_encryption = 'md5'"))
            set_up_runtime_role(connection, settings)
    finally:
        engine.dispose()

    # The password went to the server once, as a SCRAM verifier, never as itself.
    [statement] = password_statements(sent_to_driver)
    assert SCRAM_IN_STATEMENT.search(statement), "the password was not sent as a verifier"
    assert seen_by_sqlalchemy, "the capture saw the other statements"
    everything_sent = [*sent_to_driver, *parameters_sent, *seen_by_sqlalchemy]
    assert all(password not in text_sent for text_sent in everything_sent)
    # It went past SQLAlchemy, so neither its events nor its statement log saw it.
    assert password_statements(seen_by_sqlalchemy) == []
    assert any(logged.name.startswith("sqlalchemy") for logged in caplog.records)
    assert "SCRAM-SHA-256" not in caplog.text
    assert password not in caplog.text

    # Forced failure: a role that may log in but not alter other roles (this run's runtime
    # role) runs the set-up for the fresh role, so the password statement is refused.
    as_another_role = settings.model_copy(
        update={
            "postgres_user": database.app_db_user,
            "postgres_password": database.app_db_password,
        }
    )
    failing = create_owner_sync_engine(as_another_role, poolclass=NullPool)
    sent_to_driver.clear()
    try:
        with pytest.raises(errors.InsufficientPrivilege) as excinfo, failing.begin() as connection:
            set_up_runtime_role(connection, settings)
    finally:
        failing.dispose()

    assert password_statements(sent_to_driver[-1:]), "the password statement is the one refused"
    assert "SQLSTATE 42501" in str(excinfo.value)
    rendered = [str(excinfo.value), repr(excinfo.value)]
    rendered.append("".join(traceback.format_exception(excinfo.value)))
    rendered += [
        text_of for linked in chain(excinfo.value) for text_of in (str(linked), repr(linked))
    ]
    rendered += [str(excinfo.value.diag.message_detail), str(excinfo.value.diag.message_hint)]
    for text_of in [*rendered, caplog.text, repr(settings), repr(failing.url)]:
        assert password not in text_of
        assert "SCRAM-SHA-256" not in text_of


async def test_application_engine_uses_the_runtime_role(database: StartupSettings) -> None:
    engine = create_database_engine(database)
    try:
        async with engine.connect() as connection:
            users = (await connection.execute(text("SELECT current_user, session_user"))).one()
    finally:
        await engine.dispose()

    assert database.app_db_user != database.postgres_user
    assert tuple(users) == (database.app_db_user, database.app_db_user)


async def test_password_rotation(fresh_role: StartupSettings) -> None:
    old = fresh_role
    new = old.model_copy(update={"app_db_password": SecretStr(secrets.token_urlsafe(24))})
    set_up_roles(old)
    assert await connects(old)

    set_up_roles(new)

    assert await connects(new)
    assert not await connects(old)


# --- R11: idempotent, and it takes away what is not declared --------------------------------

SNAPSHOT = (
    text(
        "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
        " rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil"
        " FROM pg_authid WHERE rolname = :name"
    ),
    text(
        "SELECT c.relname, c.relacl::text[] FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
        " ORDER BY c.relname"
    ),
    text(
        "SELECT attrelid::regclass::text, attname, attacl::text[] FROM pg_attribute"
        " WHERE attacl IS NOT NULL ORDER BY 1, 2"
    ),
    text("SELECT nspname, nspacl::text[] FROM pg_namespace ORDER BY nspname"),
    text("SELECT datacl::text[] FROM pg_database WHERE datname = current_database()"),
    text("SELECT roleid, member, grantor FROM pg_auth_members ORDER BY 1, 2, 3"),
    text("SELECT setdatabase, setrole, setconfig FROM pg_db_role_setting ORDER BY 1, 2"),
    text("SELECT parname, paracl::text[] FROM pg_parameter_acl ORDER BY 1"),
    text("SELECT defaclrole, defaclobjtype, defaclacl::text[] FROM pg_default_acl ORDER BY 1, 2"),
)


def snapshot(engine: Engine, role: str) -> list[list[tuple[Any, ...]]]:
    with engine.connect() as connection:
        return [
            [tuple(row) for row in connection.execute(query, {"name": role}).all()]
            for query in SNAPSHOT
        ]


async def test_setup_twice_changes_nothing(
    fresh_role: StartupSettings, owner_engine: Engine
) -> None:
    set_up_roles(fresh_role)
    first = snapshot(owner_engine, fresh_role.app_db_user)
    assert first[0], "the role was created"

    set_up_roles(fresh_role)

    assert snapshot(owner_engine, fresh_role.app_db_user) == first
    assert await connects(fresh_role)
    assert_privileges_match_the_declaration(owner_engine, fresh_role.app_db_user)


async def test_undeclared_privilege_is_revoked(
    fresh_role: StartupSettings, owner_engine: Engine
) -> None:
    set_up_roles(fresh_role)
    role = f'"{fresh_role.app_db_user}"'
    owner_role = f'"{fresh_role.postgres_user}"'
    database = f'"{fresh_role.postgres_db}"'
    with owner_engine.begin() as connection:
        for statement in (
            # Object privileges, to the role and to everyone.
            f"GRANT DELETE, TRUNCATE ON audit_events TO {role}",
            f"GRANT UPDATE ON medications TO {role}",
            f"GRANT SELECT ON patients TO {role} WITH GRANT OPTION",
            f"GRANT INSERT (pharmacy) ON medications TO {role}",
            f"GRANT UPDATE (starts_at) ON available_slots TO {role}",
            f"GRANT UPDATE ON appointments TO {role}",
            f"GRANT SELECT, UPDATE ON alembic_version TO {role}",
            f"GRANT ALL ON SEQUENCE medications_id_seq TO {role}",
            f"GRANT SELECT, UPDATE ON SEQUENCE appointments_id_seq TO {role}",
            f"GRANT CREATE ON SCHEMA public TO {role}",
            f"GRANT CREATE, TEMPORARY ON DATABASE {database} TO {role}",
            "GRANT SELECT ON audit_events TO PUBLIC",
            "GRANT USAGE ON SEQUENCE medications_id_seq TO PUBLIC",
            "GRANT UPDATE (outcome) ON audit_events TO PUBLIC",
            "GRANT CREATE ON SCHEMA public TO PUBLIC",
            f"GRANT TEMPORARY ON DATABASE {database} TO PUBLIC",
            # The routes to owning things and to other databases, reopened.
            f"GRANT EXECUTE ON FUNCTION pg_catalog.lo_from_bytea(oid, bytea) TO {role}",
            "GRANT EXECUTE ON FUNCTION pg_catalog.lo_creat(integer) TO PUBLIC",
            f"GRANT CONNECT ON DATABASE postgres TO {role}",
            "GRANT CONNECT, TEMPORARY ON DATABASE postgres TO PUBLIC",
            # What the role is, what it belongs to, and how its sessions start.
            f"ALTER ROLE {role} SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS",
            f"GRANT pg_read_all_data, pg_write_all_data TO {role}",
            f"ALTER ROLE {role} SET session_replication_role = replica",
            f"ALTER ROLE {role} IN DATABASE {database} SET search_path = pg_temp",
            f"GRANT SET ON PARAMETER session_replication_role TO {role}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_role} GRANT ALL ON TABLES TO {role}",
        ):
            connection.execute(text(statement))

    set_up_roles(fresh_role)

    assert_privileges_match_the_declaration(owner_engine, fresh_role.app_db_user)
    assert_role_holds_nothing_else(owner_engine, fresh_role.app_db_user)
    engine = create_database_engine(fresh_role)
    try:
        async with engine.connect() as connection:
            mode = await connection.scalar(text("SHOW session_replication_role"))
    finally:
        await engine.dispose()
    assert mode == "origin"


def test_setup_stops_when_the_role_owns_something(
    fresh_role: StartupSettings, owner_engine: Engine
) -> None:
    set_up_roles(fresh_role)
    role = f'"{fresh_role.app_db_user}"'
    with owner_engine.begin() as connection:
        connection.execute(text("CREATE TABLE owned_by_runtime (id int)"))
        connection.execute(text("INSERT INTO owned_by_runtime VALUES (1)"))
        connection.execute(text(f"ALTER TABLE owned_by_runtime OWNER TO {role}"))
        connection.execute(text(f"GRANT pg_read_all_data TO {role}"))

    # DROP OWNED would drop the table with its rows; set-up stops instead, changing nothing.
    with pytest.raises(RuntimeError, match="owns objects set-up does not remove: table"):
        set_up_roles(fresh_role)

    with owner_engine.begin() as connection:
        assert connection.execute(text("SELECT id FROM owned_by_runtime")).scalars().all() == [1]
        oid = connection.execute(ROLE_OID, {"name": fresh_role.app_db_user}).scalar_one()
        member_of = connection.execute(MEMBERSHIPS, {"role": oid}).scalars().all()
        assert member_of == ["pg_read_all_data"]
        connection.execute(text("DROP TABLE owned_by_runtime"))


# --- Start-up -------------------------------------------------------------------------------


async def test_startup_steps_on_an_empty_database(
    new_database: Callable[[], StartupSettings],
    run_module: Module,
    run_alembic: Module,
) -> None:
    empty = new_database().model_copy(
        update={
            "app_db_user": new_role_name("start"),
            "app_db_password": SecretStr(secrets.token_urlsafe(24)),
        }
    )
    password = empty.app_db_password.get_secret_value()
    try:
        # Set-up before the migrations fails as a whole: no role is left half made.
        early = run_module(empty, "app.db.roles")
        assert early.returncode != 0
        owner = create_owner_sync_engine(empty, poolclass=NullPool)
        try:
            with owner.connect() as connection:
                assert connection.execute(ROLE_OID, {"name": empty.app_db_user}).first() is None

            # The container's order: migrate, roles, seed; then the app connects as the role.
            for step in (
                run_alembic(empty, "upgrade", "head"),
                run_module(empty, "app.db.roles"),
                run_module(empty, "app.db.seed"),
                run_module(empty, "app.db.roles"),
            ):
                assert step.returncode == 0, step.stderr
                assert password not in step.stdout + step.stderr
            assert "runtime_role_ready" in step.stderr

            assert_privileges_match_the_declaration(owner, empty.app_db_user)
            assert_role_holds_nothing_else(owner, empty.app_db_user)
        finally:
            owner.dispose()

        engine = create_database_engine(empty)
        try:
            async with engine.connect() as connection:
                counts = (
                    await connection.execute(
                        text(
                            "SELECT (SELECT count(*) FROM patients),"
                            " (SELECT count(*) FROM medications),"
                            " (SELECT count(*) FROM appointments),"
                            " (SELECT count(*) FROM available_slots)"
                        )
                    )
                ).one()
        finally:
            await engine.dispose()
        assert counts[:3] == (5, 5, 2)
        assert counts[3] > 0

        # A runtime user name equal to the owner's stops the set-up before it connects.
        same = run_module(
            empty.model_copy(update={"app_db_user": empty.postgres_user}), "app.db.roles"
        )
        assert same.returncode != 0
        assert "app_db_user" in same.stderr
    finally:
        drop_role(empty, empty.app_db_user)


# --- Lock waits -------------------------------------------------------------------------------

# A short lock limit for these tests; the real one is the session module's constant.
SHORT_LOCK_TIMEOUT_MS = 2_000
# Well past the short limit, and short of the 30 s after which the server ends the holder's
# idle transaction (the runtime engine's setting), so a set-up that waits without its limit is
# seen waiting.
OUTER_BOUND_SECONDS = 20
ROLE_ROW_WAIT = (
    "role set-up: waited more than 2 s for a lock on a database object, which another session"
    " holds; stopping"
)
# `python -m app.db.roles` with the short limit: the same main(), in a fresh interpreter.
MAIN_WITH_A_SHORT_LOCK_LIMIT = (
    "from app.db import session\n"
    f"session.STARTUP_LOCK_TIMEOUT_MS = {SHORT_LOCK_TIMEOUT_MS}\n"
    "from app.db.roles import main\n"
    "main()\n"
)


def hold_own_role_row(role: str) -> TextClause:
    """What a session of the runtime role may run on itself and leave uncommitted, holding the
    role's row in the catalog that set-up's password step updates. NULL needs no password."""
    return text(f'ALTER ROLE "{role}" PASSWORD NULL')


async def test_setup_names_a_wait_on_the_role_row(
    database: StartupSettings, runtime: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "STARTUP_LOCK_TIMEOUT_MS", SHORT_LOCK_TIMEOUT_MS)
    # Leaving the block without a commit rolls the change back.
    async with runtime.connect() as holder:
        await holder.execute(hold_own_role_row(database.app_db_user))

        with anyio.fail_after(OUTER_BOUND_SECONDS), pytest.raises(RuntimeError) as excinfo:
            await anyio.to_thread.run_sync(set_up_roles, database, abandon_on_cancel=True)

        assert str(excinfo.value) == ROLE_ROW_WAIT
        # The holder was not ended to get past it.
        assert await holder.scalar(text("SELECT 1")) == 1


async def test_main_reports_a_lock_wait(
    database: StartupSettings, runtime: AsyncEngine, tmp_path: Path
) -> None:
    async with runtime.connect() as holder:
        await holder.execute(hold_own_role_row(database.app_db_user))

        result = await anyio.to_thread.run_sync(
            partial(
                subprocess.run,
                [sys.executable, "-c", MAIN_WITH_A_SHORT_LOCK_LIMIT],
                cwd=tmp_path,
                env={**database_env(database), "PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=OUTER_BOUND_SECONDS,
                check=False,
            )
        )

        assert result.returncode == 1, result.stderr
        assert result.stdout == ""
        assert ROLE_ROW_WAIT in result.stderr
        # Nothing of the driver's error: its class, its module, its SQLSTATE.
        for driver_text in ("LockNotAvailable", "psycopg", "SQLSTATE"):
            assert driver_text not in result.stderr
        assert await holder.scalar(text("SELECT 1")) == 1
