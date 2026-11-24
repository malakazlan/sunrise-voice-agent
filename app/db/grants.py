"""What the runtime database role may do, declared once.

app/db/roles.py applies exactly this at start-up, takes away anything else the role holds, and
then compares what Postgres says the role can effectively do with it, refusing to start on any
difference; tests/test_roles.py uses the same comparison. Every table, sequence and function in
the schema has an entry, an empty set where the role may do nothing, so a new one stops the
start until it is declared here.
"""

from typing import Literal

TablePrivilege = Literal["SELECT", "INSERT", "UPDATE"]
SequencePrivilege = Literal["USAGE"]

SCHEMA = "public"

TABLE_PRIVILEGES: dict[str, frozenset[TablePrivilege]] = {
    "patients": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "medications": frozenset({"SELECT"}),
    "appointments": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "available_slots": frozenset({"SELECT", "UPDATE"}),
    "audit_events": frozenset({"SELECT", "INSERT"}),
    "verification_attempts": frozenset({"SELECT", "INSERT"}),
    "requests": frozenset({"SELECT", "INSERT"}),
    "messages": frozenset({"SELECT", "INSERT"}),
    "alembic_version": frozenset(),
}

# Only the sequences the inserts above draw numbers from: appointment, verification attempt,
# request and message ids. Patients have text ids and the audit log numbers its own events.
SEQUENCE_PRIVILEGES: dict[str, frozenset[SequencePrivilege]] = {
    "appointments_id_seq": frozenset({"USAGE"}),
    "verification_attempts_id_seq": frozenset({"USAGE"}),
    "available_slots_id_seq": frozenset(),
    "medications_id_seq": frozenset(),
    "requests_id_seq": frozenset({"USAGE"}),
    "messages_id_seq": frozenset({"USAGE"}),
}

# By SQL signature: every function in the schema, and the catalog's functions that create a
# large object. audit_events' triggers run their functions whoever may call them, so nobody
# needs EXECUTE, which Postgres otherwise gives everyone; a large object the role created would
# be an object it owns.
FUNCTION_PRIVILEGES: dict[str, frozenset[Literal["EXECUTE"]]] = {
    "public.audit_events_check_position()": frozenset(),
    "public.audit_events_refuse_change()": frozenset(),
    "pg_catalog.lo_creat(integer)": frozenset(),
    "pg_catalog.lo_create(oid)": frozenset(),
    "pg_catalog.lo_from_bytea(oid, bytea)": frozenset(),
    "pg_catalog.lo_import(text)": frozenset(),
    "pg_catalog.lo_import(text, oid)": frozenset(),
}

# Enough to reach the tables above; never CREATE.
SCHEMA_PRIVILEGES: frozenset[Literal["USAGE"]] = frozenset({"USAGE"})

# Enough to log in to the application's database; never CREATE or TEMPORARY (temporary tables
# and functions are DDL too). Nothing at all on the server's other databases.
DATABASE_PRIVILEGES: frozenset[Literal["CONNECT"]] = frozenset({"CONNECT"})
