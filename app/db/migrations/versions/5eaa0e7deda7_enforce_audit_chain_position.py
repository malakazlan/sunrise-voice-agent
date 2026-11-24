"""Enforce audit chain position

Revision ID: 5eaa0e7deda7
Revises: 5ec4a5850975
Create Date: 2026-11-24 02:17:05.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "5eaa0e7deda7"
down_revision: str | Sequence[str] | None = "5ec4a5850975"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 418581342580 is the chain's advisory lock, the one append takes (the bytes of "audit").
    op.execute(
        "CREATE FUNCTION audit_events_check_position() RETURNS trigger LANGUAGE plpgsql AS $$\n"
        "DECLARE\n"
        "  head_seq bigint;\n"
        "  head_mac char(64);\n"
        "BEGIN\n"
        "  PERFORM pg_advisory_xact_lock(418581342580);\n"
        "  SELECT seq, mac INTO head_seq, head_mac FROM audit_events ORDER BY seq DESC LIMIT 1;\n"
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
        "$$"
    )
    op.execute(
        "CREATE TRIGGER audit_events_check_position BEFORE INSERT"
        " ON audit_events FOR EACH ROW EXECUTE FUNCTION audit_events_check_position()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_events_check_position ON audit_events")
    op.execute("DROP FUNCTION audit_events_check_position()")
