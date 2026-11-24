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
- What it holds besides is taken away: the memberships the owner granted it (a membership
  another role granted is left, and the comparison below refuses it), settings attached to
  it, and, through DROP OWNED, every privilege the owner granted it in this database and on shared
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
  owns) is compared with it, and so is every privilege list Postgres keeps, system objects
  included: no entry may name the role beyond the declaration, PUBLIC's entries on what the
  declaration does not manage must be the initial ones, and no role may belong to it. Any
  difference stops the start and is listed by name. That is also how a grant made by another
  role with a grant option, which the owner cannot revoke, is caught.

Its sessions have the start-up steps' lock and statement timeouts (app/db/session.py): a
session holding the set-up lock, the runtime role's included, makes set-up fail, not wait.

Grants on shared objects, such as CONNECT on a database, are taken away server-wide, and
PUBLIC loses everything on the server's other databases: use a dedicated server.
"""

import logging

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.config import DatabaseSettings, StartupSettings
from app.db import grants
from app.db.session import create_owner_sync_engine, set_role_password, startup_wait_failure

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
    "SELECT granted.rolname FROM pg_auth_members m"
    " JOIN pg_roles granted ON granted.oid = m.roleid WHERE m.member = :role ORDER BY 1"
)
# The memberships of the role this session's user granted: the ones an ordinary REVOKE by it
# removes (Postgres 16 records who granted each membership, and REVOKE takes away only the
# revoking role's own grant).
OWN_MEMBERSHIP_GRANTS = text(
    "SELECT granted.rolname FROM pg_auth_members m"
    " JOIN pg_roles granted ON granted.oid = m.roleid"
    " WHERE m.member = :role"
    " AND m.grantor = (SELECT oid FROM pg_roles WHERE rolname = current_user) ORDER BY 1"
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
MEMBERS = text(
    "SELECT member.rolname FROM pg_auth_members m JOIN pg_roles member ON member.oid = m.member"
    " WHERE m.roleid = :role ORDER BY 1"
)
# Every privilege list Postgres keeps (every aclitem[] column of the catalogs in Postgres 16:
# pg_class, pg_attribute, pg_proc, pg_namespace, pg_type, pg_language, pg_foreign_data_wrapper,
# pg_foreign_server, pg_largeobject_metadata, pg_tablespace, pg_parameter_acl, pg_database,
# pg_default_acl), system objects included. For each object: what it grants the role by name,
# what it grants PUBLIC, and what it granted PUBLIC initially: pg_init_privs records that for
# what initdb (or an extension) installed with privileges other than the default, and an object
# without a row there started with its default. initdb's grants on information_schema are
# not recorded there (Postgres 16.16: USAGE on the schema, SELECT on every relation in it but
# INFORMATION_SCHEMA_PRIVATE), so they are written out here. `schema` is the object's
# schema, where it has one; `name` a relation's name.
ACL_ENTRIES = text(
    "WITH objects AS ("
    " SELECT 'pg_class'::regclass AS classid, c.oid AS objid, 0 AS objsubid, c.relacl AS acl,"
    " acldefault(CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::\"char\", c.relowner)"
    " AS default_acl, n.nspname AS schema, c.relname AS name"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " UNION ALL SELECT 'pg_class'::regclass, a.attrelid, a.attnum::integer, a.attacl,"
    " NULL::aclitem[], n.nspname, NULL FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
    " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE a.attacl IS NOT NULL"
    " UNION ALL SELECT 'pg_proc'::regclass, p.oid, 0, p.proacl, acldefault('f', p.proowner),"
    " n.nspname, NULL FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " UNION ALL SELECT 'pg_namespace'::regclass, n.oid, 0, n.nspacl,"
    " acldefault('n', n.nspowner), n.nspname, NULL FROM pg_namespace n"
    " UNION ALL SELECT 'pg_type'::regclass, t.oid, 0, t.typacl, acldefault('T', t.typowner),"
    " n.nspname, NULL FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
    " UNION ALL SELECT 'pg_language'::regclass, l.oid, 0, l.lanacl,"
    " acldefault('l', l.lanowner), NULL, NULL FROM pg_language l"
    " UNION ALL SELECT 'pg_foreign_data_wrapper'::regclass, w.oid, 0, w.fdwacl,"
    " acldefault('F', w.fdwowner), NULL, NULL FROM pg_foreign_data_wrapper w"
    " UNION ALL SELECT 'pg_foreign_server'::regclass, f.oid, 0, f.srvacl,"
    " acldefault('S', f.srvowner), NULL, NULL FROM pg_foreign_server f"
    " UNION ALL SELECT 'pg_largeobject'::regclass, m.oid, 0, m.lomacl,"
    " acldefault('L', m.lomowner), NULL, NULL FROM pg_largeobject_metadata m"
    " UNION ALL SELECT 'pg_tablespace'::regclass, t.oid, 0, t.spcacl,"
    " acldefault('t', t.spcowner), NULL, NULL FROM pg_tablespace t"
    " UNION ALL SELECT 'pg_parameter_acl'::regclass, a.oid, 0, a.paracl, NULL::aclitem[],"
    " NULL, NULL"
    " FROM pg_parameter_acl a"
    " UNION ALL SELECT 'pg_database'::regclass, d.oid, 0, d.datacl,"
    " acldefault('d', d.datdba), NULL, NULL FROM pg_database d"
    " UNION ALL SELECT 'pg_default_acl'::regclass, d.oid, 0, d.defaclacl, NULL::aclitem[],"
    " NULL, NULL"
    " FROM pg_default_acl d"
    "), entries AS ("
    " SELECT o.classid, o.objid, o.objsubid, o.schema,"
    " ARRAY(SELECT x.privilege_type FROM aclexplode(coalesce(o.acl, o.default_acl)) x"
    " WHERE x.grantee = :role ORDER BY 1) AS to_role,"
    " ARRAY(SELECT x.privilege_type FROM aclexplode(coalesce(o.acl, o.default_acl)) x"
    " WHERE x.grantee = 0 ORDER BY 1) AS to_public,"
    " CASE WHEN i.initprivs IS NULL AND o.schema = 'information_schema'"
    " AND o.classid = 'pg_class'::regclass AND o.objsubid = 0"
    " AND o.name <> ALL(CAST(:information_schema_private AS text[])) THEN ARRAY['SELECT']"
    " WHEN i.initprivs IS NULL AND o.classid = 'pg_namespace'::regclass"
    " AND o.schema = 'information_schema' THEN ARRAY['USAGE']"
    " ELSE ARRAY(SELECT x.privilege_type FROM aclexplode(coalesce(i.initprivs, o.default_acl)) x"
    " WHERE x.grantee = 0 ORDER BY 1) END AS initially_public,"
    " o.classid = 'pg_proc'::regclass AND o.objid = ANY(ARRAY(SELECT to_regprocedure(signature)"
    " FROM unnest(CAST(:catalog_functions AS text[])) signature)) AS execute_removed"
    " FROM objects o LEFT JOIN pg_init_privs i"
    " ON i.classoid = o.classid AND i.objoid = o.objid AND i.objsubid = o.objsubid"
    ")"
    " SELECT e.classid::regclass::text, e.schema, ident.type, ident.identity, e.to_role,"
    " e.to_public, e.initially_public, e.execute_removed"
    " FROM entries e CROSS JOIN LATERAL pg_identify_object(e.classid, e.objid, e.objsubid) ident"
    " WHERE cardinality(e.to_role) > 0 OR e.to_public IS DISTINCT FROM e.initially_public"
)
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
# The information_schema relations PUBLIC is not given SELECT on.
INFORMATION_SCHEMA_PRIVATE = [
    "_pg_foreign_data_wrappers",
    "_pg_foreign_servers",
    "_pg_foreign_table_columns",
    "_pg_foreign_tables",
    "_pg_user_mappings",
    "sql_parts",
    "transforms",
]


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


def _is_system_schema(schema: str) -> bool:
    return schema in SYSTEM_SCHEMAS or schema.startswith(("pg_toast", "pg_temp_"))


def _grant_differences(connection: Connection, role: int, database: str) -> list[str]:
    """Grants anywhere Postgres records one that the declaration does not make: any privilege
    list naming the role, and PUBLIC's privileges on everything the declaration does not
    manage, which must be exactly the initial ones."""
    named: dict[tuple[str, str], frozenset[str]] = {
        ("table", f"{grants.SCHEMA}.{name}"): frozenset(privileges)
        for name, privileges in grants.TABLE_PRIVILEGES.items()
    }
    named |= {
        ("sequence", f"{grants.SCHEMA}.{name}"): frozenset(privileges)
        for name, privileges in grants.SEQUENCE_PRIVILEGES.items()
    }
    named["schema", grants.SCHEMA] = frozenset(grants.SCHEMA_PRIVILEGES)
    named["database", database] = frozenset(grants.DATABASE_PRIVILEGES)
    catalog_functions = [
        name for name in grants.FUNCTION_PRIVILEGES if name.startswith("pg_catalog.")
    ]
    differences = []
    rows = connection.execute(
        ACL_ENTRIES,
        {
            "role": role,
            "catalog_functions": catalog_functions,
            "information_schema_private": INFORMATION_SCHEMA_PRIVATE,
        },
    )
    for catalog, schema, kind, identity, to_role, to_public, initially, removed in rows:
        # The application's own relations, functions and schemas, and the databases: the
        # declaration decides them, and the effective comparison checks them.
        managed = catalog == "pg_database" or (
            catalog in ("pg_class", "pg_proc", "pg_namespace")
            and schema is not None
            and not _is_system_schema(schema)
        )
        allowed = named.get((kind, identity), frozenset()) if managed else frozenset()
        differences += [
            f"{kind} {identity}: {privilege} granted to the runtime role"
            for privilege in sorted(set(to_role) - allowed)
        ]
        if managed:
            continue
        expected = set(initially) - ({"EXECUTE"} if removed else set())
        differences += [
            f"{kind} {identity}: {privilege} granted to PUBLIC beyond its initial privileges"
            for privilege in sorted(set(to_public) - expected)
        ]
        differences += [
            f"{kind} {identity}: {privilege} taken from PUBLIC's initial privileges"
            for privilege in sorted(expected - set(to_public))
        ]
    return differences


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
        for granted in connection.execute(MEMBERSHIPS, {"role": role}).scalars()
    ]
    differences += [
        f"role {member} is a member of the runtime role"
        for member in connection.execute(MEMBERS, {"role": role}).scalars()
    ]
    differences += _grant_differences(connection, role, database)
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
    try:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ROLE_SETUP_LOCK})
    except DBAPIError as error:
        failure = startup_wait_failure("role set-up", error, "the role set-up lock")
        if failure is None:
            raise
        raise failure from error
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

    # Only the owner's own grants, never in another grantor's name; any other membership is
    # left, and the comparison below refuses the start naming it.
    for granted in connection.execute(OWN_MEMBERSHIP_GRANTS, {"role": oid}).scalars().all():
        connection.execute(text(f"REVOKE {quote(granted)} FROM {role}"))
    for in_database in connection.execute(ROLE_SETTINGS, {"role": oid}).scalars().all():
        where = "" if in_database is None else f" IN DATABASE {quote(in_database)}"
        connection.execute(text(f"ALTER ROLE {role}{where} RESET ALL"))
    connection.execute(text(f"DROP OWNED BY {role}"))

    connection.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC"))
    connection.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC"))
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
    except DBAPIError as error:
        failure = startup_wait_failure("role set-up", error)
        if failure is None:
            raise
        raise failure from error
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
