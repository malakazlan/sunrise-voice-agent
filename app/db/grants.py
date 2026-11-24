"""What the runtime database role may do, declared once.

app/db/roles.py applies exactly this at start-up and takes away anything else the role holds;
tests/test_roles.py compares it with what Postgres's own catalogs report. Every table, sequence
and function in the schema has an entry, an empty set where the role may do nothing, so a new one
fails that comparison until it is declared here.
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
    "alembic_version": frozenset(),
}

# Only the sequences the inserts above draw numbers from: appointment ids. Patients have text
# ids and the audit log numbers its own events.
SEQUENCE_PRIVILEGES: dict[str, frozenset[SequencePrivilege]] = {
    "appointments_id_seq": frozenset({"USAGE"}),
    "available_slots_id_seq": frozenset(),
    "medications_id_seq": frozenset(),
}

# Every function in the schema. audit_events' triggers run their functions whoever may call
# them, so nobody needs EXECUTE, which Postgres otherwise gives everyone.
FUNCTION_PRIVILEGES: dict[str, frozenset[Literal["EXECUTE"]]] = {
    "audit_events_check_position": frozenset(),
    "audit_events_refuse_change": frozenset(),
}

# Enough to reach the tables above; never CREATE.
SCHEMA_PRIVILEGES: frozenset[Literal["USAGE"]] = frozenset({"USAGE"})

# Enough to log in; never CREATE or TEMPORARY (temporary tables and functions are DDL too).
DATABASE_PRIVILEGES: frozenset[Literal["CONNECT"]] = frozenset({"CONNECT"})
