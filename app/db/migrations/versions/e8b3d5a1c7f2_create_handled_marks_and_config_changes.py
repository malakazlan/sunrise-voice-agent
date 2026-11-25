"""Create handled marks and config changes

Revision ID: e8b3d5a1c7f2
Revises: 6e2b8c41d7a9
Create Date: 2026-11-25 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b3d5a1c7f2"
down_revision: str | Sequence[str] | None = "6e2b8c41d7a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "handled_marks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("row_id", sa.Integer(), nullable=False),
        sa.Column("marked_by", sa.Text(), nullable=False),
        sa.Column(
            "marked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('requests')", name=op.f("ck_handled_marks_source")),
        sa.CheckConstraint("row_id >= 1", name=op.f("ck_handled_marks_row_id")),
        sa.CheckConstraint(
            "marked_by ~ '^[A-Za-z][A-Za-z_]{0,31}$'", name=op.f("ck_handled_marks_marked_by")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_handled_marks")),
        sa.UniqueConstraint("source", "row_id", name=op.f("uq_handled_marks_source_row_id")),
    )
    op.create_table(
        "config_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(key = 'calls' AND value IN ('on', 'off'))"
            " OR (key = 'office' AND value IN ('schedule', 'open', 'closed'))",
            name=op.f("ck_config_changes_setting"),
        ),
        sa.CheckConstraint(
            "changed_by ~ '^[A-Za-z][A-Za-z_]{0,31}$'", name=op.f("ck_config_changes_changed_by")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_changes")),
    )


def downgrade() -> None:
    op.drop_table("config_changes")
    op.drop_table("handled_marks")
