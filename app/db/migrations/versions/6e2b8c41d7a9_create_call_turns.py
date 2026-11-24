"""Create call turns, and the indexes the console's reads use

Revision ID: 6e2b8c41d7a9
Revises: d2f7a19c4e05
Create Date: 2026-11-25 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6e2b8c41d7a9"
down_revision: str | Sequence[str] | None = "d2f7a19c4e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_turns",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=True),
        sa.Column("guard_action", sa.Text(), nullable=False),
        sa.Column("last_tool", sa.Text(), nullable=True),
        sa.Column("tool_rounds", sa.Integer(), nullable=False),
        sa.Column("vad_ms", sa.Integer(), nullable=True),
        sa.Column("stt_ms", sa.Integer(), nullable=True),
        sa.Column("first_token_ms", sa.Integer(), nullable=True),
        sa.Column("last_token_ms", sa.Integer(), nullable=True),
        sa.Column("first_audio_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=False),
        sa.Column("record_us", sa.Integer(), nullable=False),
        sa.Column("dropped_before", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'",
            name=op.f("ck_call_turns_call_id"),
        ),
        sa.CheckConstraint("patient_id ~ '^P[0-9]{3,6}$'", name=op.f("ck_call_turns_patient_id")),
        sa.CheckConstraint(
            "verified = (patient_id IS NOT NULL)", name=op.f("ck_call_turns_verified_patient")
        ),
        sa.CheckConstraint(
            "phase IN ('greeting', 'intake', 'serving', 'booking', 'emergency', 'handoff',"
            " 'closing', 'ended')",
            name=op.f("ck_call_turns_phase"),
        ),
        sa.CheckConstraint(
            "guard_action IN ('pass', 'swallow', 'emergency')",
            name=op.f("ck_call_turns_guard_action"),
        ),
        sa.CheckConstraint("last_tool ~ '^[a-z_]{2,40}$'", name=op.f("ck_call_turns_last_tool")),
        sa.CheckConstraint("turn >= 1", name=op.f("ck_call_turns_turn")),
        sa.CheckConstraint("tool_rounds >= 0", name=op.f("ck_call_turns_tool_rounds")),
        sa.CheckConstraint("record_us >= 0", name=op.f("ck_call_turns_record_us")),
        sa.CheckConstraint("dropped_before >= 0", name=op.f("ck_call_turns_dropped_before")),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_call_turns_patient_id_patients")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_turns")),
        sa.UniqueConstraint("call_id", "turn", name=op.f("uq_call_turns_call_id_turn")),
    )
    op.create_index("ix_call_turns_at", "call_turns", ["at"])
    op.create_index("ix_audit_events_action_ts", "audit_events", ["action", "ts"])
    op.create_index("ix_verification_attempts_created_at", "verification_attempts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_verification_attempts_created_at", table_name="verification_attempts")
    op.drop_index("ix_audit_events_action_ts", table_name="audit_events")
    op.drop_index("ix_call_turns_at", table_name="call_turns")
    op.drop_table("call_turns")
