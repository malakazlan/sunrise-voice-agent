"""Messages differ by name and number too

Revision ID: c1cc4d6086e9
Revises: 190ccc8799c7
Create Date: 2026-11-24 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1cc4d6086e9"
down_revision: str | Sequence[str] | None = "190ccc8799c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_messages_call_id_kind_body"), "messages", type_="unique")
    op.create_unique_constraint(
        op.f("uq_messages_call_id_kind_body_name_callback_number"),
        "messages",
        ["call_id", "kind", "body", "name", "callback_number"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_messages_call_id_kind_body_name_callback_number"), "messages", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_messages_call_id_kind_body"), "messages", ["call_id", "kind", "body"]
    )
