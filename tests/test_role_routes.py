"""Ways the runtime role could come to own things or keep privileges the declaration does not
give it, tried as the role where the role could try them, and what role set-up does about
each: close the route, repair what Postgres cannot forbid, or refuse to start.
"""

import functools
import secrets
import threading
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from conftest import (
    assert_refused_by_privileges,
    connects,
    drop_database,
    drop_role,
    new_role_name,
    set_up_roles,
)
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.config import StartupSettings
from app.db import session as session_module
from app.db.session import create_database_engine, create_owner_sync_engine, set_role_password

pytestmark = pytest.mark.anyio

OWNED_BY = text(
    "SELECT classid::regclass::text FROM pg_shdepend"
    " WHERE refclassid = 'pg_authid'::regclass AND deptype = 'o'"
    " AND refobjid = (SELECT oid FROM pg_roles WHERE rolname = :name)"
)
ROLE_SETTINGS = text(
    "SELECT count(*) FROM pg_db_role_setting"
    " WHERE setrole = (SELECT oid FROM pg_roles WHERE rolname = :name)"
)


def owned_by(engine: Engine, role: str) -> list[str]:
    with engine.connect() as connection:
        return sorted(connection.execute(OWNED_BY, {"name": role}).scalars())


async def as_role(settings: StartupSettings, *statements: str) -> None:
    """Run statements as the runtime role, committed: what the role can do to itself."""
    engine = create_database_engine(settings)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


# --- F2: owning things -----------------------------------------------------------------------


async def test_runtime_role_cannot_create_large_objects(
    database: StartupSettings, runtime: AsyncEngine, owner_engine: Engine
) -> None:
    for statement in (
        "SELECT lo_from_bytea(0, 'x')",
        "SELECT lo_creat(-1)",
        "SELECT lo_create(0)",
        "SELECT lo_import('/etc/hostname')",
        "SELECT lo_import('/etc/hostname', 4242)",
    ):
        await assert_refused_by_privileges(runtime, statement)

    assert owned_by(owner_engine, database.app_db_user) == []


async def test_runtime_role_can_connect_to_its_database_only(
    database: StartupSettings, owner_engine: Engine
) -> None:
    with owner_engine.connect() as connection:
        others = (
            connection.execute(
                text(
                    "SELECT datname FROM pg_database"
                    " WHERE datallowconn AND datname <> current_database()"
                )
            )
            .scalars()
            .all()
        )
    assert "postgres" in others and "template1" in others

    assert await connects(database)
    for other in others:
        assert not await connects(database.model_copy(update={"postgres_db": other})), other


async def test_what_the_role_sets_for_itself_is_undone_at_the_next_start(
    fresh_role: StartupSettings, owner_engine: Engine
) -> None:
    set_up_roles(fresh_role)
    role = f'"{fresh_role.app_db_user}"'
    changed = "-".join(["changed", "by", "the", "role", "itself"])
    # Postgres lets every role do these to itself; none of it can be forbidden.
    await as_role(
        fresh_role,
        "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO PUBLIC",
        "ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
        f"ALTER ROLE {role} SET work_mem = '64kB'",
    )
    await as_role(fresh_role, f"ALTER ROLE {role} PASSWORD '{changed}'")
    assert owned_by(owner_engine, fresh_role.app_db_user) == ["pg_default_acl"] * 2

    set_up_roles(fresh_role)

    assert owned_by(owner_engine, fresh_role.app_db_user) == []
    with owner_engine.connect() as connection:
        assert connection.execute(ROLE_SETTINGS, {"name": fresh_role.app_db_user}).scalar() == 0
    assert await connects(fresh_role)
    changed_password = SecretStr(changed)
    assert not await connects(fresh_role.model_copy(update={"app_db_password": changed_password}))


def test_large_objects_the_role_owns_are_removed_at_the_next_start(
    fresh_role: StartupSettings, owner_engine: Engine
) -> None:
    # As if the role had made one before the route was closed.
    set_up_roles(fresh_role)
    with owner_engine.begin() as connection:
        oid = connection.execute(text("SELECT lo_from_bytea(0, 'left behind')")).scalar_one()
        connection.execute(
            text(f'ALTER LARGE OBJECT {int(oid)} OWNER TO "{fresh_role.app_db_user}"')
        )
    assert owned_by(owner_engine, fresh_role.app_db_user) == ["pg_largeobject"]

    set_up_roles(fresh_role)

    assert owned_by(owner_engine, fresh_role.app_db_user) == []
    with owner_engine.connect() as connection:
        left = connection.execute(
            text("SELECT count(*) FROM pg_largeobject_metadata WHERE oid = :oid"), {"oid": oid}
        ).scalar()
    assert left == 0


def test_setup_refuses_real_objects_the_role_owns_and_names_their_kind(
    fresh_role: StartupSettings, owner_engine: Engine
) -> None:
    set_up_roles(fresh_role)
    role = f'"{fresh_role.app_db_user}"'
    with owner_engine.begin() as connection:
        for statement in (
            "CREATE TABLE owned_table (id int)",
            f"ALTER TABLE owned_table OWNER TO {role}",
            "CREATE FUNCTION owned_function() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            f"ALTER FUNCTION owned_function() OWNER TO {role}",
            "CREATE SCHEMA owned_schema",
            f"ALTER SCHEMA owned_schema OWNER TO {role}",
        ):
            connection.execute(text(statement))

    with pytest.raises(RuntimeError) as excinfo:
        set_up_roles(fresh_role)

    assert str(excinfo.value) == (
        "the runtime role owns objects set-up does not remove: function, schema, table"
    )
    with owner_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM owned_table")).scalar() == 0


# --- F3: privileges from another grantor ----------------------------------------------------


@pytest.fixture
def helper(
    server_settings: StartupSettings, fresh_role: StartupSettings
) -> Iterator[StartupSettings]:
    """A second role, as the owner of the test database could create one: it may log in and
    is given grant options by the owner. Dropped after the test database."""
    name = new_role_name("helper")
    password = secrets.token_urlsafe(24)
    engine = create_owner_sync_engine(fresh_role, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'"))
    finally:
        engine.dispose()
    try:
        yield fresh_role.model_copy(
            update={"postgres_user": name, "postgres_password": SecretStr(password)}
        )
    finally:
        # The database goes first, with every grant the helper made in it.
        drop_database(server_settings, fresh_role.postgres_db)
        drop_role(server_settings, name)


def grant_as_owner(owner_engine: Engine, *statements: str) -> None:
    with owner_engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def grant_as(helper: StartupSettings, *statements: str) -> None:
    engine = create_owner_sync_engine(helper, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def test_grants_by_another_grantor_stop_the_setup(
    fresh_role: StartupSettings, helper: StartupSettings, owner_engine: Engine
) -> None:
    # The reviewer's case: a role holding grant options gives the runtime role DELETE on the
    # audit log and everyone TEMPORARY on the database. The owner cannot revoke what another
    # role granted, so set-up must not report success.
    set_up_roles(fresh_role)
    role = f'"{fresh_role.app_db_user}"'
    database = f'"{fresh_role.postgres_db}"'
    helper_role = f'"{helper.postgres_user}"'
    grant_as_owner(
        owner_engine,
        f"GRANT DELETE ON audit_events TO {helper_role} WITH GRANT OPTION",
        f"GRANT TEMPORARY ON DATABASE {database} TO {helper_role} WITH GRANT OPTION",
    )
    grant_as(
        helper,
        f"GRANT DELETE ON audit_events TO {role}",
        f"GRANT TEMPORARY ON DATABASE {database} TO PUBLIC",
    )

    with pytest.raises(RuntimeError) as excinfo:
        set_up_roles(fresh_role)

    assert str(excinfo.value) == (
        "the runtime role does not match the declaration:"
        f" database {fresh_role.postgres_db}: TEMPORARY not declared;"
        " table audit_events: DELETE not declared"
    )


SURPLUS = [
    # (grant to the helper with grant option, the helper's grant, the difference reported)
    (
        "GRANT UPDATE ON medications TO {helper} WITH GRANT OPTION",
        "GRANT UPDATE ON medications TO {role}",
        "table medications: UPDATE not declared",
    ),
    (
        "GRANT SELECT ON alembic_version TO {helper} WITH GRANT OPTION",
        "GRANT SELECT ON alembic_version TO PUBLIC",
        "table alembic_version: SELECT not declared",
    ),
    (
        "GRANT UPDATE (outcome) ON audit_events TO {helper} WITH GRANT OPTION",
        "GRANT UPDATE (outcome) ON audit_events TO {role}",
        "table audit_events: UPDATE not declared",
    ),
    (
        "GRANT USAGE ON SEQUENCE medications_id_seq TO {helper} WITH GRANT OPTION",
        "GRANT USAGE ON SEQUENCE medications_id_seq TO PUBLIC",
        "sequence medications_id_seq: USAGE not declared",
    ),
    (
        "GRANT EXECUTE ON FUNCTION audit_events_refuse_change() TO {helper} WITH GRANT OPTION",
        "GRANT EXECUTE ON FUNCTION audit_events_refuse_change() TO {role}",
        "function public.audit_events_refuse_change(): EXECUTE not declared",
    ),
    (
        "GRANT CREATE ON SCHEMA public TO {helper} WITH GRANT OPTION",
        "GRANT CREATE ON SCHEMA public TO PUBLIC",
        "schema public: CREATE not declared",
    ),
    (
        "GRANT CREATE ON DATABASE {database} TO {helper} WITH GRANT OPTION",
        "GRANT CREATE ON DATABASE {database} TO {role}",
        "database {database_name}: CREATE not declared",
    ),
]


@pytest.mark.parametrize(
    ("to_helper", "by_helper", "reported"),
    SURPLUS,
    ids=["table", "table-public", "column", "sequence", "function", "schema", "database"],
)
def test_each_kind_of_surplus_stops_the_setup(
    fresh_role: StartupSettings,
    helper: StartupSettings,
    owner_engine: Engine,
    to_helper: str,
    by_helper: str,
    reported: str,
) -> None:
    set_up_roles(fresh_role)
    names = {
        "helper": f'"{helper.postgres_user}"',
        "role": f'"{fresh_role.app_db_user}"',
        "database": f'"{fresh_role.postgres_db}"',
        "database_name": fresh_role.postgres_db,
    }
    grant_as_owner(owner_engine, to_helper.format(**names))
    grant_as(helper, by_helper.format(**names))

    with pytest.raises(RuntimeError) as excinfo:
        set_up_roles(fresh_role)

    assert str(excinfo.value) == (
        f"the runtime role does not match the declaration: {reported.format(**names)}"
    )


# --- F5: concurrent set-ups ------------------------------------------------------------------


def run_together(count: int, job: Callable[[], None]) -> list[BaseException]:
    """Start `count` threads running `job` at the same moment; the exceptions they raised."""
    start = threading.Barrier(count)
    failures: list[BaseException] = []

    def run() -> None:
        start.wait()
        try:
            job()
        except BaseException as failure:
            failures.append(failure)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads), "a set-up did not finish"
    return failures


async def test_concurrent_setups_all_succeed(fresh_role: StartupSettings) -> None:
    # On a role that does not exist yet, then on one that does, several times each.
    for round_ in range(3):
        role = fresh_role.model_copy(update={"app_db_user": f"{fresh_role.app_db_user}{round_}"})
        try:
            for _ in range(2):
                failures = run_together(4, functools.partial(set_up_roles, role))
                assert [type(failure).__name__ for failure in failures] == []
            assert await connects(role)
        finally:
            engine = create_owner_sync_engine(fresh_role, poolclass=NullPool)
            try:
                with engine.begin() as connection:
                    connection.execute(text(f'DROP OWNED BY "{role.app_db_user}"'))
                    connection.execute(text(f'DROP ROLE "{role.app_db_user}"'))
            finally:
                engine.dispose()


# --- F6: the verifier's shape ----------------------------------------------------------------

NOT_SCRAM = [
    "md5" + "0" * 32,
    "",
    "SCRAM-SHA-256$4096:c2FsdA==$a2V5",
    "SCRAM-SHA-256$0:c2FsdA==$a2V5:a2V5",
    "SCRAM-SHA-256$4096:c2FsdA==$a2V5:a2V5' OR 'x",
    "SCRAM-SHA-256$4096:c2FsdA==$a2V5:a2V5\nALTER ROLE x SUPERUSER",
    "plain password",
]


@pytest.mark.parametrize(
    "verifier",
    NOT_SCRAM,
    ids=["md5", "empty", "no-server-key", "no-iterations", "quote", "newline", "plain"],
)
async def test_a_verifier_not_in_scram_form_is_refused_before_it_is_sent(
    fresh_role: StartupSettings, monkeypatch: pytest.MonkeyPatch, verifier: str
) -> None:
    set_up_roles(fresh_role)
    monkeypatch.setattr(session_module, "_scram_verifier", lambda *args: verifier)
    sent: list[str] = []
    execute = psycopg.Cursor.execute

    def recording_execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        sent.append(query.as_string(cursor) if isinstance(query, sql.Composable) else str(query))
        return execute(cursor, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", recording_execute)
    new_password = SecretStr(secrets.token_urlsafe(24))
    engine = create_owner_sync_engine(fresh_role, poolclass=NullPool)
    try:
        with (
            pytest.raises(RuntimeError, match="did not return a SCRAM-SHA-256 verifier"),
            engine.begin() as connection,
        ):
            sent.clear()
            set_role_password(connection, fresh_role.app_db_user, new_password)
    finally:
        engine.dispose()

    assert sent == []
    monkeypatch.undo()
    assert await connects(fresh_role)
