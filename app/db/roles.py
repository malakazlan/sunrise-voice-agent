"""Set up the runtime database role: `python -m app.db.roles`.

Runs as the owner at container start, after the migrations and before the seed, in one
transaction: if any step fails, nothing changes and the container stops; the next start runs
it again from the beginning. Running it again changes nothing.

The role ends up able to log in and to do exactly what app/db/grants.py declares in this
database, and nothing else:

- It is created if missing. Its attributes are set every time: LOGIN, not a superuser, cannot
  create roles or databases, no replication, no bypassing row security.
- Its password is set every time, so a changed APP_DB_PASSWORD takes effect at the next start
  and the old one stops working. It is sent as a SCRAM-SHA-256 verifier made by the driver,
  never as the password itself, in a statement no log, event or error message sees
  (app/db/session.py).
- What it holds besides is taken away: membership in other roles, settings attached to it,
  and, through DROP OWNED, every privilege granted to it in this database and on shared objects
  (databases, parameters), default privileges included. If it owns anything, anywhere on the
  server, set-up stops instead: DROP OWNED would drop those objects, and the role must never
  own any.
- PUBLIC loses what would otherwise reach the role through it: every privilege on the schema's
  tables and sequences, EXECUTE on its functions, CREATE on the schema, CREATE and TEMPORARY
  on the database.
- Then the declaration is granted.

Privileges the role holds in other databases on the same server are not this database's to
manage and are left alone, except grants on shared objects, which DROP OWNED takes away
everywhere: one runtime role name per server.
"""

import logging

from sqlalchemy import Connection, text
from sqlalchemy.pool import NullPool

from app.config import DatabaseSettings, StartupSettings
from app.db import grants
from app.db.session import create_owner_sync_engine, set_role_password

logger = logging.getLogger(__name__)

ROLE = text("SELECT oid FROM pg_roles WHERE rolname = :name")
OWNED_ANYWHERE = text(
    "SELECT count(*) FROM pg_shdepend"
    " WHERE refclassid = 'pg_authid'::regclass AND refobjid = :role AND deptype = 'o'"
)
MEMBERSHIPS = text(
    "SELECT granted.rolname, grantor.rolname FROM pg_auth_members m"
    " JOIN pg_roles granted ON granted.oid = m.roleid"
    " JOIN pg_roles grantor ON grantor.oid = m.grantor"
    " WHERE m.member = :role ORDER BY 1, 2"
)
ROLE_SETTINGS = text(
    "SELECT d.datname FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid = s.setdatabase"
    " WHERE s.setrole = :role ORDER BY 1"
)


def set_up_runtime_role(connection: Connection, settings: DatabaseSettings) -> None:
    """Set up the runtime role inside the caller's transaction, as the owner. The caller
    commits."""
    quote = connection.dialect.identifier_preparer.quote_identifier
    role = quote(settings.app_db_user)
    database = quote(connection.execute(text("SELECT current_database()")).scalar_one())
    schema = quote(grants.SCHEMA)

    oid = connection.execute(ROLE, {"name": settings.app_db_user}).scalar_one_or_none()
    created = oid is None
    if created:
        connection.execute(text(f"CREATE ROLE {role}"))
        oid = connection.execute(ROLE, {"name": settings.app_db_user}).scalar_one()
    elif connection.execute(OWNED_ANYWHERE, {"role": oid}).scalar_one():
        raise RuntimeError("the runtime role owns database objects; it must own none")

    set_role_password(connection, settings.app_db_user, settings.app_db_password)
    connection.execute(
        text(
            f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
            " NOBYPASSRLS"
        )
    )

    for granted, grantor in connection.execute(MEMBERSHIPS, {"role": oid}).all():
        connection.execute(text(f"REVOKE {quote(granted)} FROM {role} GRANTED BY {quote(grantor)}"))
    for in_database in connection.execute(ROLE_SETTINGS, {"role": oid}).scalars().all():
        where = "" if in_database is None else f" IN DATABASE {quote(in_database)}"
        connection.execute(text(f"ALTER ROLE {role}{where} RESET ALL"))
    connection.execute(text(f"DROP OWNED BY {role}"))

    connection.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC"))
    connection.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC"))
    connection.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM PUBLIC"))
    connection.execute(text(f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC"))
    connection.execute(text(f"REVOKE CREATE, TEMPORARY ON DATABASE {database} FROM PUBLIC"))

    on_database = ", ".join(sorted(grants.DATABASE_PRIVILEGES))
    connection.execute(text(f"GRANT {on_database} ON DATABASE {database} TO {role}"))
    on_schema = ", ".join(sorted(grants.SCHEMA_PRIVILEGES))
    connection.execute(text(f"GRANT {on_schema} ON SCHEMA {schema} TO {role}"))
    for kind, declaration in (
        ("TABLE", grants.TABLE_PRIVILEGES),
        ("SEQUENCE", grants.SEQUENCE_PRIVILEGES),
        ("FUNCTION", grants.FUNCTION_PRIVILEGES),
    ):
        for name, privileges in declaration.items():
            if privileges:
                connection.execute(
                    text(
                        f"GRANT {', '.join(sorted(privileges))}"
                        f" ON {kind} {schema}.{quote(name)} TO {role}"
                    )
                )

    logger.info(
        "runtime_role_ready created=%s tables=%d sequences=%d functions=%d",
        created,
        len(grants.TABLE_PRIVILEGES),
        len(grants.SEQUENCE_PRIVILEGES),
        len(grants.FUNCTION_PRIVILEGES),
    )


def main() -> None:
    """Set up the runtime role from the environment's settings, as the owner."""
    settings = StartupSettings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s [%(name)s] %(message)s")
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            set_up_runtime_role(connection, settings)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
