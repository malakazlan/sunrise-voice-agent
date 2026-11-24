"""Allow booking requests

Revision ID: 9a4f2c71d0e8
Revises: 3c3da6d6e340
Create Date: 2026-11-24 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "9a4f2c71d0e8"
down_revision: str | Sequence[str] | None = "3c3da6d6e340"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_requests_type"), "requests", type_="check")
    op.create_check_constraint(
        op.f("ck_requests_type"), "requests", "type IN ('refill', 'doctor_message', 'booking')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_requests_type"), "requests", type_="check")
    op.create_check_constraint(
        op.f("ck_requests_type"), "requests", "type IN ('refill', 'doctor_message')"
    )
