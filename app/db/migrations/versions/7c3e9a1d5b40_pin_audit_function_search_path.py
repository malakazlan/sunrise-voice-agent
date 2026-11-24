"""Pin the audit functions' search path

Revision ID: 7c3e9a1d5b40
Revises: 5eaa0e7deda7
Create Date: 2026-11-24 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7c3e9a1d5b40"
down_revision: str | Sequence[str] | None = "5eaa0e7deda7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both functions run with a fixed search path and name the table with its schema, so nothing
# created later, ahead of them on a session's search path, can stand in for what they use.
# CREATE OR REPLACE keeps each function's owner, privileges and triggers.
REFUSE_CHANGE_BODY = (
    "BEGIN\n"
    "  RAISE EXCEPTION 'audit_events is append-only'\n"
    "    USING ERRCODE = 'insufficient_privilege', TABLE = TG_TABLE_NAME;\n"
    "END\n"
)
POSITION_CHECKS = (
    "  IF NEW.seq IS DISTINCT FROM coalesce(head_seq, 0) + 1 THEN\n"
    "    RAISE EXCEPTION 'audit_events row out of position' USING\n"
    "      ERRCODE = 'integrity_constraint_violation', TABLE = TG_TABLE_NAME, COLUMN = 'seq';\n"
    "  END IF;\n"
    "  IF NEW.prev_mac IS DISTINCT FROM coalesce(head_mac, repeat('0', 64)) THEN\n"
    "    RAISE EXCEPTION 'audit_events row out of position' USING\n"
    "      ERRCODE = 'integrity_constraint_violation', TABLE = TG_TABLE_NAME,\n"
    "      COLUMN = 'prev_mac';\n"
    "  END IF;\n"
    "  RETURN NEW;\n"
    "END\n"
)
DECLARATIONS = "DECLARE\n  head_seq bigint;\n  head_mac char(64);\nBEGIN\n"
CHECK_POSITION_BODY = (
    DECLARATIONS
    + "  SELECT seq, mac INTO head_seq, head_mac FROM public.audit_events\n"
    + "    ORDER BY seq DESC LIMIT 1;\n"
    + POSITION_CHECKS
)
PREVIOUS_CHECK_POSITION_BODY = (
    DECLARATIONS
    + "  SELECT seq, mac INTO head_seq, head_mac FROM audit_events ORDER BY seq DESC LIMIT 1;\n"
    + POSITION_CHECKS
)
PINNED = " SET search_path = pg_catalog, pg_temp"


def upgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION audit_events_refuse_change() RETURNS trigger"
        f" LANGUAGE plpgsql{PINNED} AS $$\n{REFUSE_CHANGE_BODY}$$"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION audit_events_check_position() RETURNS trigger"
        f" LANGUAGE plpgsql{PINNED} AS $$\n{CHECK_POSITION_BODY}$$"
    )


def downgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION audit_events_refuse_change() RETURNS trigger"
        f" LANGUAGE plpgsql AS $$\n{REFUSE_CHANGE_BODY}$$"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION audit_events_check_position() RETURNS trigger"
        f" LANGUAGE plpgsql AS $$\n{PREVIOUS_CHECK_POSITION_BODY}$$"
    )
