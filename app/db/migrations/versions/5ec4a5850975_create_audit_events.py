"""Create audit events

Revision ID: 5ec4a5850975
Revises: ceeaac967255
Create Date: 2026-11-24 06:04:05.119614

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5ec4a5850975"
down_revision: str | Sequence[str] | None = "ceeaac967255"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prev_mac", sa.CHAR(length=64), nullable=False),
        sa.Column("mac", sa.CHAR(length=64), nullable=False),
        sa.CheckConstraint(
            "call_id ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_call_id")
        ),
        sa.CheckConstraint("actor ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_actor")),
        sa.CheckConstraint(
            "action ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_action")
        ),
        sa.CheckConstraint(
            "resource_type ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_resource_type")
        ),
        sa.CheckConstraint(
            "resource_id ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_resource_id")
        ),
        sa.CheckConstraint(
            "outcome ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_outcome")
        ),
        sa.CheckConstraint(
            "reason ~ '^[A-Za-z0-9_:.-]{1,64}$'", name=op.f("ck_audit_events_reason")
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_events")),
    )
    op.execute(
        "CREATE FUNCTION audit_events_refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        "  RAISE EXCEPTION 'audit_events is append-only'\n"
        "    USING ERRCODE = 'insufficient_privilege', TABLE = TG_TABLE_NAME;\n"
        "END\n"
        "$$"
    )
    op.execute(
        "CREATE TRIGGER audit_events_refuse_update BEFORE UPDATE"
        " ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_refuse_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_refuse_delete BEFORE DELETE"
        " ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_refuse_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_refuse_truncate BEFORE TRUNCATE"
        " ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_refuse_change()"
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION audit_events_refuse_change()")
