"""Set up the runtime database role: `python -m app.db.roles`.

Runs as the owner in the start-up service, after the migrations and before the seed, in one
transaction under its own advisory lock, so set-ups that run at once take turns: if any step
fails, nothing changes and the start-up stops; the next start runs it again from the
beginning. Running it again changes nothing.

The role ends up able to log in to this database and to do exactly what app/db/grants.py
declares, and nothing else:

- It is created if missing. Its attributes are set every time: LOGIN, not a superuser, cannot
  create roles or databases, no replication, no bypassing row security.
- Its password is set every time, so a changed APP_DB_PASSWORD takes effect at the next start
  and the old one stops working; so is a password the role set for itself. It is sent as a
  SCRAM-SHA-256 verifier made by the driver, never as the password itself, in a statement no
  log, event or error message sees (app/db/session.py).
- What it holds besides is taken away: membership in other roles, settings attached to it, and,
  through DROP OWNED, every privilege the owner granted it in this database and on shared
  objects (databases, parameters). DROP OWNED also drops what the role can make itself own and
  Postgres cannot forbid: default privileges, and large objects (whose creation it is refused,
  but one may remain from before). If it owns anything else, anywhere on the server, set-up
  stops instead and names the kind: DROP OWNED would drop those objects with their contents.
- PUBLIC loses what would otherwise reach the role through it: every privilege on the schema's
  tables, sequences and functions and on the catalog's large-object creators, CREATE on the
  schema, CREATE and TEMPORARY on this database, and everything on every other database of the
  server (it keeps USAGE on the schema and CONNECT on this database).
- Then the declaration is granted, and what the role can effectively do (by its own grants,
  PUBLIC's or any role's it belongs to) and is (its attributes, memberships, settings, what it
  owns) is compared with it. Any difference stops the start and is listed by name. That is
  also how a grant made by another role with a grant option, which the owner cannot revoke,
  is caught.

Grants on shared objects, such as CONNECT on a database, are taken away server-wide: use one
runtime role name per server.
"""

import logging

from sqlalchemy import Connection, text
from sqlalchemy.pool import NullPool

from app.config import DatabaseSettings, StartupSettings
from app.db import grants
from app.db.session import create_owner_sync_engine, set_role_password

logger = logging.getLogger(__name__)

# The transaction-scoped advisory lock that serialises set-ups; not the audit chain's.
ROLE_SETUP_LOCK = int.from_bytes(b"roles", "big")

ROLE = text("SELECT oid FROM pg_roles WHERE rolname = :name")
# Everything the role owns, on the whole server: dbid is the database, 0 for shared objects.
OWNED = text(
    "SELECT s.classid, s.objid, s.objsubid, s.classid::regclass::text,"
    " s.dbid IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))"
    " FROM pg_shdepend s"
    " WHERE s.refclassid = 'pg_authid'::regclass AND s.refobjid = :role AND s.deptype = 'o'"
)
KIND = text(
    "SELECT (pg_identify_object(CAST(:classid AS oid), CAST(:objid AS oid),"
    " CAST(:objsubid AS integer))).type"
)
# What a role can make itself own in a database it can connect to, and DROP OWNED removes.
REMOVABLE = ("pg_default_acl", "pg_largeobject")
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
OTHER_DATABASES = text(
    "SELECT datname FROM pg_database WHERE datname <> current_database() ORDER BY 1"
)

# Every privilege Postgres 16 checks for each kind of object (the GRANT reference page).
TABLE_CHECKS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
COLUMN_CHECKS = ["SELECT", "INSERT", "UPDATE", "REFERENCES"]
SEQUENCE_CHECKS = ["USAGE", "SELECT", "UPDATE"]
SCHEMA_CHECKS = ["USAGE", "CREATE"]
DATABASE_CHECKS = ["CONNECT", "CREATE", "TEMPORARY"]
# LOGIN and nothing else.
ATTRIBUTES = {
    "rolsuper": False,
    "rolcreaterole": False,
    "rolcreatedb": False,
    "rolcanlogin": True,
    "rolreplication": False,
    "rolbypassrls": False,
}

# What the role can effectively do: has_*_privilege counts its own grants, PUBLIC's and those of
# any role it belongs to. Schemas Postgres itself provides (catalogs, TOAST, temporary) are
# left out; every other object in the database is compared, on the server every database.
RELATIONS_HELD = text(
    "SELECT CASE WHEN c.relkind = 'S' THEN 'sequence' ELSE 'table' END,"
    " CASE WHEN n.nspname = :schema THEN c.relname ELSE n.nspname || '.' || c.relname END,"
    " p.privilege,"
    " CASE WHEN c.relkind = 'S' THEN has_sequence_privilege(:role, c.oid, p.privilege)"
    " ELSE has_table_privilege(:role, c.oid, p.privilege)"
    " OR (p.privilege = ANY(CAST(:column_checks AS text[]))"
    " AND has_any_column_privilege(:role, c.oid, p.privilege)) END"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " CROSS JOIN LATERAL unnest(CASE WHEN c.relkind = 'S'"
    " THEN CAST(:sequence_checks AS text[]) ELSE CAST(:table_checks AS text[]) END) p(privilege)"
    " WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
    " AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
FUNCTIONS_HELD = text(
    "SELECT 'function', n.nspname || '.' || p.proname"
    " || '(' || pg_get_function_identity_arguments(p.oid) || ')',"
    " 'EXECUTE', has_function_privilege(:role, p.oid, 'EXECUTE')"
    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE (n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)')"
    " OR p.oid = ANY(ARRAY(SELECT to_regprocedure(signature)"
    " FROM unnest(CAST(:catalog_functions AS text[])) signature))"
)
SCHEMAS_HELD = text(
    "SELECT 'schema', n.nspname, p.privilege, has_schema_privilege(:role, n.oid, p.privilege)"
    " FROM pg_namespace n CROSS JOIN unnest(CAST(:schema_checks AS text[])) p(privilege)"
    " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname !~ '^pg_(toast|temp_)'"
)
DATABASES_HELD = text(
    "SELECT 'database', d.datname, p.privilege, has_database_privilege(:role, d.oid, p.privilege)"
    " FROM pg_database d CROSS JOIN unnest(CAST(:database_checks AS text[])) p(privilege)"
)
ROLE_ATTRIBUTES = text(
    "SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls"
    " FROM pg_roles WHERE oid = :role"
)
SETTINGS_COUNT = text("SELECT count(*) FROM pg_db_role_setting WHERE setrole = :role")


def _refuse_what_it_must_not_own(connection: Connection, role: int) -> None:
    """Stop if the role owns anything set-up does not remove, naming the kinds."""
    kinds = set()
    for classid, objid, objsubid, catalog, here in connection.execute(OWNED, {"role": role}):
        if not here:
            kinds.add("objects in another database")
        elif catalog not in REMOVABLE:
            kind = connection.execute(
                KIND, {"classid": classid, "objid": objid, "objsubid": objsubid}
            ).scalar_one()
            kinds.add(kind)
    if kinds:
        raise RuntimeError(
            f"the runtime role owns objects set-up does not remove: {', '.join(sorted(kinds))}"
        )


def _declared(database: str, databases: set[str]) -> dict[tuple[str, str], frozenset[str]]:
    expected: dict[tuple[str, str], frozenset[str]] = {}
    for kind, declaration in (
        ("table", grants.TABLE_PRIVILEGES),
        ("sequence", grants.SEQUENCE_PRIVILEGES),
        ("function", grants.FUNCTION_PRIVILEGES),
    ):
        for name, privileges in declaration.items():
            expected[kind, name] = frozenset(privileges)
    expected["schema", grants.SCHEMA] = frozenset(grants.SCHEMA_PRIVILEGES)
    for name in databases:
        expected["database", name] = (
            frozenset(grants.DATABASE_PRIVILEGES) if name == database else frozenset()
        )
    return expected


def runtime_role_differences(connection: Connection, role_name: str) -> list[str]:
    """Every way in which what the role can effectively do, and what it is, differs from the
    declaration, by kind and name only; empty when they match. The one comparison: role set-up
    runs it before it reports success, and the tests run it too."""
    role = connection.execute(ROLE, {"name": role_name}).scalar_one()
    parameters = {
        "role": role,
        "schema": grants.SCHEMA,
        "table_checks": TABLE_CHECKS,
        "column_checks": COLUMN_CHECKS,
        "sequence_checks": SEQUENCE_CHECKS,
        "schema_checks": SCHEMA_CHECKS,
        "database_checks": DATABASE_CHECKS,
        "catalog_functions": [
            name for name in grants.FUNCTION_PRIVILEGES if name.startswith("pg_catalog.")
        ],
    }
    held: dict[tuple[str, str], set[str]] = {}
    for query in (RELATIONS_HELD, FUNCTIONS_HELD, SCHEMAS_HELD, DATABASES_HELD):
        for kind, name, privilege, allowed in connection.execute(query, parameters):
            privileges = held.setdefault((kind, name), set())
            if allowed:
                privileges.add(privilege)

    database = connection.execute(text("SELECT current_database()")).scalar_one()
    databases = {name for kind, name in held if kind == "database"}
    expected = _declared(database, databases)
    differences = []
    for kind, name in sorted(held.keys() - expected.keys()):
        differences.append(f"{kind} {name}: not declared")
    for kind, name in sorted(expected.keys() - held.keys()):
        differences.append(f"{kind} {name}: declared but missing")
    for key in sorted(held.keys() & expected.keys()):
        kind, name = key
        differences += [
            f"{kind} {name}: {privilege} not declared"
            for privilege in sorted(held[key] - expected[key])
        ]
        differences += [
            f"{kind} {name}: {privilege} missing" for privilege in sorted(expected[key] - held[key])
        ]

    attributes = connection.execute(ROLE_ATTRIBUTES, {"role": role}).mappings().one()
    differences += [
        f"role attribute {name}: {'on' if attributes[name] else 'off'}"
        for name, wanted in ATTRIBUTES.items()
        if attributes[name] is not wanted
    ]
    differences += [
        f"member of {granted}"
        for granted, _grantor in connection.execute(MEMBERSHIPS, {"role": role})
    ]
    if connection.execute(SETTINGS_COUNT, {"role": role}).scalar_one():
        differences.append("session settings of its own")
    owned = {
        catalog
        for _classid, _objid, _objsubid, catalog, _here in connection.execute(OWNED, {"role": role})
    }
    differences += [f"owns objects in {catalog}" for catalog in sorted(owned)]
    return sorted(differences)


def set_up_runtime_role(connection: Connection, settings: DatabaseSettings) -> None:
    """Set up the runtime role inside the caller's transaction, as the owner, and check the
    result against the declaration. The caller commits."""
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ROLE_SETUP_LOCK})
    quote = connection.dialect.identifier_preparer.quote_identifier
    role = quote(settings.app_db_user)
    database = quote(connection.execute(text("SELECT current_database()")).scalar_one())
    schema = quote(grants.SCHEMA)

    oid = connection.execute(ROLE, {"name": settings.app_db_user}).scalar_one_or_none()
    created = oid is None
    if oid is None:
        connection.execute(text(f"CREATE ROLE {role}"))
        oid = connection.execute(ROLE, {"name": settings.app_db_user}).scalar_one()
    _refuse_what_it_must_not_own(connection, oid)

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
    # The declaration's function names are SQL signatures.
    for signature in grants.FUNCTION_PRIVILEGES:
        connection.execute(text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"))
    connection.execute(text(f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC"))
    connection.execute(text(f"REVOKE CREATE, TEMPORARY ON DATABASE {database} FROM PUBLIC"))
    for other in connection.execute(OTHER_DATABASES).scalars().all():
        connection.execute(text(f"REVOKE ALL ON DATABASE {quote(other)} FROM PUBLIC"))

    on_database = ", ".join(sorted(grants.DATABASE_PRIVILEGES))
    connection.execute(text(f"GRANT {on_database} ON DATABASE {database} TO {role}"))
    on_schema = ", ".join(sorted(grants.SCHEMA_PRIVILEGES))
    connection.execute(text(f"GRANT {on_schema} ON SCHEMA {schema} TO {role}"))
    for kind, declaration in (
        ("TABLE", grants.TABLE_PRIVILEGES),
        ("SEQUENCE", grants.SEQUENCE_PRIVILEGES),
    ):
        for name, privileges in declaration.items():
            if privileges:
                connection.execute(
                    text(
                        f"GRANT {', '.join(sorted(privileges))}"
                        f" ON {kind} {schema}.{quote(name)} TO {role}"
                    )
                )
    for signature, executable in grants.FUNCTION_PRIVILEGES.items():
        if executable:
            connection.execute(
                text(f"GRANT {', '.join(sorted(executable))} ON FUNCTION {signature} TO {role}")
            )

    differences = runtime_role_differences(connection, settings.app_db_user)
    if differences:
        raise RuntimeError(
            f"the runtime role does not match the declaration: {'; '.join(differences)}"
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
