"""Create verification attempts

Revision ID: 3c3da6d6e340
Revises: 7c3e9a1d5b40
Create Date: 2026-11-24 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c3da6d6e340"
down_revision: str | Sequence[str] | None = "7c3e9a1d5b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_attempts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("claim_key", sa.Text(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "claim_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_verification_attempts_claim_key")
        ),
        sa.CheckConstraint(
            "call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'",
            name=op.f("ck_verification_attempts_call_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verification_attempts")),
    )
    op.create_index(
        "ix_verification_attempts_claim_key_created_at",
        "verification_attempts",
        ["claim_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_verification_attempts_claim_key_created_at", table_name="verification_attempts"
    )
    op.drop_table("verification_attempts")
