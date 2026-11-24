"""Add callback messages and transfer requests

Revision ID: 190ccc8799c7
Revises: 9a4f2c71d0e8
Create Date: 2026-11-24 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "190ccc8799c7"
down_revision: str | Sequence[str] | None = "9a4f2c71d0e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("after_hours", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.drop_constraint(op.f("ck_messages_kind"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_kind"), "messages", "kind IN ('doctor_message', 'callback')"
    )
    op.drop_constraint(op.f("ck_requests_type"), "requests", type_="check")
    op.create_check_constraint(
        op.f("ck_requests_type"),
        "requests",
        "type IN ('refill', 'doctor_message', 'booking', 'callback', 'transfer')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_requests_type"), "requests", type_="check")
    op.create_check_constraint(
        op.f("ck_requests_type"),
        "requests",
        "type IN ('refill', 'doctor_message', 'booking')",
    )
    op.drop_constraint(op.f("ck_messages_kind"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_kind"), "messages", "kind IN ('doctor_message')")
    op.drop_column("messages", "after_hours")
