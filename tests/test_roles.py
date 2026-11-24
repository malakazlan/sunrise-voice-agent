"""The runtime database role: what app/db/roles.py sets up from app/db/grants.py, checked
against Postgres's own catalogs and by trying, as the role, what it must not do.
"""

import re
import secrets
import subprocess
import traceback
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import drop_role, new_role_name, set_up_roles
from psycopg import errors
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.audit.log import Action, AuditLog, Event, Outcome, ResourceType
from app.config import Settings
from app.db import grants
from app.db.models import Base
from app.db.roles import set_up_runtime_role
from app.db.session import create_database_engine, create_owner_sync_engine, create_session_factory

pytestmark = pytest.mark.anyio

Module = Callable[..., subprocess.CompletedProcess[str]]

# The spec's privilege table, written out here by hand. The declaration must say exactly this,
# so widening it is caught here as well as by the catalog comparison.
EXPECTED_TABLES = {
    "patients": {"SELECT", "INSERT", "UPDATE"},
    "medications": {"SELECT"},
    "appointments": {"SELECT", "INSERT", "UPDATE"},
    "available_slots": {"SELECT", "UPDATE"},
    "audit_events": {"SELECT", "INSERT"},
    "alembic_version": set(),
}
# Only the appointments insert takes a number from a sequence; patients have text ids and
# audit_events numbers its own rows.
EXPECTED_SEQUENCES = {
    "appointments_id_seq": {"USAGE"},
    "available_slots_id_seq": set(),
    "medications_id_seq": set(),
}
EXPECTED_SCHEMAS = {"public": {"USAGE"}}
EXPECTED_DATABASE = {"CONNECT"}

# Every privilege Postgres 16 knows for each kind of object (the GRANT reference page).
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
SCHEMA_PRIVILEGES = ("USAGE", "CREATE")
DATABASE_PRIVILEGES = ("CONNECT", "CREATE", "TEMPORARY")

# Each object with every entry of its ACL: who holds which privilege, grantee 0 being PUBLIC.
# An object whose ACL was never changed has none stored; its default ACL applies. Schemas that
# Postgres itself provides (catalogs, TOAST, temporary) are left out; every other is compared.
RELATIONS = text(
    "SELECT c.oid, n.nspname, c.relname, c.relkind, x.grantee, x.privilege_type"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " LEFT JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault("
    "   CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::\"char\", c.relowner))) x ON true"
    " WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
    " AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
SCHEMAS = text(
    "SELECT n.oid, n.nspname, x.grantee, x.privilege_type FROM pg_namespace n"
    " LEFT JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) x ON true"
    " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
DATABASE = text(
    "SELECT d.oid, x.grantee, x.privilege_type FROM pg_database d"
    " LEFT JOIN LATERAL aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) x ON true"
    " WHERE d.datname = current_database()"
)
COLUMN_GRANTS = text(
    "SELECT c.relname, a.attname, x.privilege_type FROM pg_attribute a"
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
    """What a role may do, per kind of object, keyed by name."""

    tables: dict[str, set[str]]
    sequences: dict[str, set[str]]
    schemas: dict[str, set[str]]
    database: set[str]


def declared() -> Privileges:
    return Privileges(
        tables={name: set(held) for name, held in grants.TABLE_PRIVILEGES.items()},
        sequences={name: set(held) for name, held in grants.SEQUENCE_PRIVILEGES.items()},
        schemas={grants.SCHEMA: set(grants.SCHEMA_PRIVILEGES)},
        database=set(grants.DATABASE_PRIVILEGES),
    )


@dataclass(frozen=True, kw_only=True)
class Catalog:
    """What Postgres reports for a role: `granted` to it by name, `public` to everyone, and
    `effective`, what its privilege checks allow (its own grants, PUBLIC's, its roles')."""

    granted: Privileges
    public: Privileges
    effective: Privileges
    column_grants: list[tuple[str, str, str]]


def catalog(engine: Engine, role: str) -> Catalog:
    granted = Privileges(tables={}, sequences={}, schemas={}, database=set())
    public = Privileges(tables={}, sequences={}, schemas={}, database=set())
    effective = Privileges(tables={}, sequences={}, schemas={}, database=set())
    with engine.connect() as connection:
        role_oid = connection.execute(ROLE_OID, {"name": role}).scalar_one()

        def allowed(function: str, oid: int, privileges: tuple[str, ...]) -> set[str]:
            return {
                privilege
                for privilege in privileges
                if connection.execute(
                    text(f"SELECT {function}(:role, :oid, :privilege)"),
                    {"role": role_oid, "oid": oid, "privilege": privilege},
                ).scalar_one()
            }

        def note(
            kinds: tuple[dict[str, set[str]], ...], name: str, grantee: Any, held: Any
        ) -> None:
            for kind in kinds:
                kind.setdefault(name, set())
            if grantee == role_oid:
                kinds[0][name].add(held)
            elif grantee == 0:
                kinds[1][name].add(held)

        for oid, schema, name, kind, grantee, held in connection.execute(RELATIONS).all():
            assert schema == grants.SCHEMA, (schema, name)
            if kind == "S":
                kinds = (granted.sequences, public.sequences, effective.sequences)
                can = allowed("has_sequence_privilege", oid, SEQUENCE_PRIVILEGES)
            else:
                kinds = (granted.tables, public.tables, effective.tables)
                can = allowed("has_table_privilege", oid, TABLE_PRIVILEGES)
                can |= allowed("has_any_column_privilege", oid, COLUMN_PRIVILEGES)
            note(kinds, name, grantee, held)
            kinds[2][name] = can
        for oid, name, grantee, held in connection.execute(SCHEMAS).all():
            kinds = (granted.schemas, public.schemas, effective.schemas)
            note(kinds, name, grantee, held)
            effective.schemas[name] = allowed("has_schema_privilege", oid, SCHEMA_PRIVILEGES)
        databases: tuple[dict[str, set[str]], ...] = ({}, {}, {})
        for oid, grantee, held in connection.execute(DATABASE).all():
            note(databases, "", grantee, held)
            databases[2][""] = allowed("has_database_privilege", oid, DATABASE_PRIVILEGES)
        granted.database.update(databases[0][""])
        public.database.update(databases[1][""])
        effective.database.update(databases[2][""])
        column_grants = [
            (table, column, privilege)
            for table, column, privilege in connection.execute(
                COLUMN_GRANTS, {"role": role_oid}
            ).all()
        ]
    return Catalog(granted=granted, public=public, effective=effective, column_grants=column_grants)


def assert_privileges_match_the_declaration(engine: Engine, role: str) -> None:
    found = catalog(engine, role)
    expected = declared()
    # Everything the role may do, it was granted by name, and nothing else.
    assert found.granted == expected
    assert found.effective == expected
    # PUBLIC holds nothing beyond the declaration: Postgres 16 gives it USAGE on the public
    # schema and CONNECT on the database; set-up takes TEMPORARY on the database away.
    assert found.public == Privileges(
        tables={name: set() for name in expected.tables},
        sequences={name: set() for name in expected.sequences},
        schemas={grants.SCHEMA: {"USAGE"}},
        database={"CONNECT"},
    )
    assert found.column_grants == []


@pytest.fixture
def owner(database: Settings) -> Iterator[Engine]:
    engine = create_owner_sync_engine(database, poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
async def runtime(database: Settings) -> AsyncIterator[AsyncEngine]:
    """The application's engine: the runtime role."""
    engine = create_database_engine(database)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def fresh_role(database: Settings) -> Iterator[Settings]:
    """The test database with a runtime role of its own that does not exist yet."""
    settings = database.model_copy(
        update={
            "app_db_user": new_role_name("fresh"),
            "app_db_password": SecretStr(secrets.token_urlsafe(24)),
        }
    )
    try:
        yield settings
    finally:
        drop_role(database, settings.app_db_user)


async def attempt(engine: AsyncEngine, statement: str) -> DBAPIError | None:
    """Run one statement in its own transaction, rolled back; the error, if it failed."""
    async with engine.connect() as connection:
        try:
            await connection.execute(text(statement))
        except DBAPIError as error:
            return error
        finally:
            await connection.rollback()
    return None


async def assert_refused_by_privileges(engine: AsyncEngine, statement: str) -> DBAPIError:
    error = await attempt(engine, statement)
    assert error is not None, statement
    assert type(error.orig) is errors.InsufficientPrivilege, (statement, type(error.orig))
    return error


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
    database: Settings, runtime: AsyncEngine, owner: Engine
) -> None:
    audit = AuditLog(database.audit_hmac_key)
    await append_one(audit, runtime)
    before = await stored_events(runtime)
    statements = (
        "UPDATE audit_events SET outcome = 'failure'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    )

    for statement in statements:
        error = await assert_refused_by_privileges(runtime, statement)
        # The triggers refuse with the same SQLSTATE but name the table; the privilege check,
        # which comes first, names none.
        assert isinstance(error.orig, errors.Error)
        assert error.orig.diag.table_name is None, statement

    # Without the triggers, the privileges alone still refuse.
    with owner.begin() as connection:
        for trigger in TRIGGERS:
            connection.execute(text(f"DROP TRIGGER {trigger} ON audit_events"))
    for statement in statements:
        await assert_refused_by_privileges(runtime, statement)

    assert await stored_events(runtime) == before
    async with create_session_factory(runtime)() as session:
        assert (await audit.verify(session)).ok


async def test_runtime_role_cannot_bypass_triggers(
    database: Settings, runtime: AsyncEngine
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
    database: Settings, runtime: AsyncEngine
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


# The same table, tried statement by statement as the role. Each statement touches no row, so
# only the privilege check decides; it runs in a transaction that is rolled back.
FIRST_COLUMN = {
    "patients": "email",
    "medications": "pharmacy",
    "appointments": "reason_short",
    "available_slots": "source",
    "audit_events": "reason",
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
    assert allowed == EXPECTED_TABLES

    sequences: dict[str, set[str]] = {}
    for sequence in EXPECTED_SEQUENCES:
        sequences[sequence] = set()
        if await attempt(runtime, f"SELECT nextval('{sequence}')") is None:
            sequences[sequence].add("USAGE")
        if await attempt(runtime, f"SELECT last_value FROM {sequence}") is None:  # noqa: S608
            sequences[sequence].add("SELECT")
    assert sequences == EXPECTED_SEQUENCES


# --- R5, R6: what the catalogs say ----------------------------------------------------------


def test_privileges_match_the_declaration(database: Settings, owner: Engine) -> None:
    # The declaration is the spec's table, and covers every table the models define.
    assert declared() == Privileges(
        tables=EXPECTED_TABLES,
        sequences=EXPECTED_SEQUENCES,
        schemas=EXPECTED_SCHEMAS,
        database=EXPECTED_DATABASE,
    )
    assert set(grants.TABLE_PRIVILEGES) == {*Base.metadata.tables, "alembic_version"}

    # Postgres agrees, both ways, for every table, sequence and schema in the database.
    found = catalog(owner, database.app_db_user)
    assert set(found.granted.tables) == set(EXPECTED_TABLES)
    assert set(found.granted.sequences) == set(EXPECTED_SEQUENCES)
    assert_privileges_match_the_declaration(owner, database.app_db_user)


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
    database: Settings, owner: Engine, runtime: AsyncEngine
) -> None:
    assert_role_holds_nothing_else(owner, database.app_db_user)

    # As the role: no database can be created (outside a transaction block, where it runs).
    async with runtime.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        with pytest.raises(DBAPIError) as excinfo:
            await autocommit.execute(text("CREATE DATABASE intruder"))
    assert type(excinfo.value.orig) is errors.InsufficientPrivilege


# --- R7, R8, R12: credentials ---------------------------------------------------------------


def password_statements(statements: list[str]) -> list[str]:
    return [statement for statement in statements if "PASSWORD" in statement.upper()]


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
    fresh_role: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    password = "-".join(["runtime", "password", "not", "for", "logs"])
    settings = fresh_role.model_copy(update={"app_db_password": SecretStr(password)})
    caplog.set_level("DEBUG")
    caplog.set_level("DEBUG", logger="sqlalchemy")

    sent: list[str] = []

    def record(conn: Connection, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        sent.append(statement)
        sent.append(repr(parameters))

    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    event.listen(engine, "before_cursor_execute", record)
    try:
        with engine.begin() as connection:
            set_up_runtime_role(connection, settings)
    finally:
        engine.dispose()

    # The password went to the server once, as a SCRAM verifier, never as itself.
    [statement] = password_statements(sent)
    assert SCRAM_IN_STATEMENT.search(statement), "the password was not sent as a verifier"
    assert all(password not in text_sent for text_sent in sent)

    # Forced failure: the same set-up run by a role that may not alter roles (the runtime role
    # itself). The statement that fails is the one carrying the verifier.
    as_runtime = settings.model_copy(
        update={"postgres_user": settings.app_db_user, "postgres_password": SecretStr(password)}
    )
    failing = create_owner_sync_engine(as_runtime, poolclass=NullPool)
    sent.clear()
    event.listen(failing, "before_cursor_execute", record)
    try:
        with pytest.raises(DBAPIError) as excinfo, failing.begin() as connection:
            set_up_runtime_role(connection, settings)
    finally:
        failing.dispose()

    assert type(excinfo.value.orig) is errors.InsufficientPrivilege
    assert SCRAM_IN_STATEMENT.search(str(excinfo.value)), "the failing statement is not shown"
    rendered = [str(excinfo.value), repr(excinfo.value)]
    rendered.append("".join(traceback.format_exception(excinfo.value)))
    rendered += [
        text_of for linked in chain(excinfo.value) for text_of in (str(linked), repr(linked))
    ]
    for text_of in [*rendered, *sent, caplog.text, repr(settings), repr(failing.url)]:
        assert password not in text_of
    assert any(logged.name.startswith("sqlalchemy") for logged in caplog.records)


async def test_application_engine_uses_the_runtime_role(database: Settings) -> None:
    engine = create_database_engine(database)
    try:
        async with engine.connect() as connection:
            users = (await connection.execute(text("SELECT current_user, session_user"))).one()
    finally:
        await engine.dispose()

    assert database.app_db_user != database.postgres_user
    assert tuple(users) == (database.app_db_user, database.app_db_user)


async def connects(settings: Settings) -> bool:
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection:
            user = await connection.scalar(text("SELECT current_user"))
    except OperationalError as error:
        assert "password authentication failed" in str(error)
        return False
    finally:
        await engine.dispose()
    assert user == settings.app_db_user
    return True


async def test_password_rotation(fresh_role: Settings) -> None:
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


async def test_setup_twice_changes_nothing(fresh_role: Settings, owner: Engine) -> None:
    set_up_roles(fresh_role)
    first = snapshot(owner, fresh_role.app_db_user)
    assert first[0], "the role was created"

    set_up_roles(fresh_role)

    assert snapshot(owner, fresh_role.app_db_user) == first
    assert await connects(fresh_role)
    assert_privileges_match_the_declaration(owner, fresh_role.app_db_user)


async def test_undeclared_privilege_is_revoked(fresh_role: Settings, owner: Engine) -> None:
    set_up_roles(fresh_role)
    role = f'"{fresh_role.app_db_user}"'
    owner_role = f'"{fresh_role.postgres_user}"'
    database = f'"{fresh_role.postgres_db}"'
    with owner.begin() as connection:
        for statement in (
            # Object privileges, to the role and to everyone.
            f"GRANT DELETE, TRUNCATE ON audit_events TO {role}",
            f"GRANT UPDATE ON medications TO {role}",
            f"GRANT SELECT ON patients TO {role} WITH GRANT OPTION",
            f"GRANT INSERT (pharmacy) ON medications TO {role}",
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

    assert_privileges_match_the_declaration(owner, fresh_role.app_db_user)
    assert_role_holds_nothing_else(owner, fresh_role.app_db_user)
    engine = create_database_engine(fresh_role)
    try:
        async with engine.connect() as connection:
            mode = await connection.scalar(text("SHOW session_replication_role"))
    finally:
        await engine.dispose()
    assert mode == "origin"


# --- Start-up -------------------------------------------------------------------------------


async def test_startup_steps_on_an_empty_database(
    new_database: Callable[[], Settings],
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
