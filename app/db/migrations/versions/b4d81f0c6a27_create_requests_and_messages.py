"""Create requests and messages

Revision ID: b4d81f0c6a27
Revises: 7c3e9a1d5b40
Create Date: 2026-11-24 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d81f0c6a27"
down_revision: str | Sequence[str] | None = "7c3e9a1d5b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("type IN ('refill', 'doctor_message')", name=op.f("ck_requests_type")),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 200", name=op.f("ck_requests_summary_length")
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_requests_patient_id_patients")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requests")),
        sa.UniqueConstraint(
            "call_id", "type", "summary", name=op.f("uq_requests_call_id_type_summary")
        ),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("callback_number", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('doctor_message')", name=op.f("ck_messages_kind")),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 120", name=op.f("ck_messages_body_length")
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_messages_patient_id_patients")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("call_id", "kind", "body", name=op.f("uq_messages_call_id_kind_body")),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("requests")
