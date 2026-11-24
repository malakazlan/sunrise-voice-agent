"""Create call sessions

Revision ID: d2f7a19c4e05
Revises: c1cc4d6086e9
Create Date: 2026-11-24 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f7a19c4e05"
down_revision: str | Sequence[str] | None = "c1cc4d6086e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("office_open", sa.Boolean(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("transcript_truncated", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'",
            name=op.f("ck_call_sessions_call_id"),
        ),
        sa.CheckConstraint(
            "end_reason IN ('completed', 'emergency', 'transferred', 'caller_hung_up',"
            " 'idle_timeout', 'max_duration', 'error')",
            name=op.f("ck_call_sessions_end_reason"),
        ),
        sa.CheckConstraint(
            "verified = (patient_id IS NOT NULL)", name=op.f("ck_call_sessions_verified_patient")
        ),
        sa.CheckConstraint(
            "char_length(transcript) <= 20000", name=op.f("ck_call_sessions_transcript_length")
        ),
        sa.CheckConstraint("turn_count >= 0", name=op.f("ck_call_sessions_turn_count")),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_call_sessions_patient_id_patients")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_sessions")),
        sa.UniqueConstraint("call_id", name=op.f("uq_call_sessions_call_id")),
    )


def downgrade() -> None:
    op.drop_table("call_sessions")
