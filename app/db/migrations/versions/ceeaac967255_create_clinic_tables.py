"""Create clinic tables

Revision ID: ceeaac967255
Revises:
Create Date: 2026-11-24 01:53:23.473258

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ceeaac967255"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "available_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("doctor", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_available", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "doctor IN ('Dr. Patel', 'Dr. Lee')", name=op.f("ck_available_slots_doctor")
        ),
        sa.CheckConstraint(
            "source IN ('task', 'generated')", name=op.f("ck_available_slots_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_available_slots")),
        sa.UniqueConstraint(
            "doctor", "starts_at", name=op.f("uq_available_slots_doctor_starts_at")
        ),
    )
    op.create_table(
        "patients",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("dob", sa.Date(), nullable=False),
        sa.Column("ssn_last4", sa.CHAR(length=4), nullable=True),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("insurance_provider", sa.Text(), nullable=True),
        sa.Column("member_id", sa.Text(), nullable=True),
        sa.Column("self_pay", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("lab_results_status", sa.Text(), nullable=False),
        sa.Column("balance_due", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("is_new", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "possible_duplicate", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ssn_last4 IS NULL OR ssn_last4 ~ '^[0-9]{4}$'",
            name=op.f("ck_patients_ssn_last4_four_digits"),
        ),
        sa.CheckConstraint("balance_due >= 0", name=op.f("ck_patients_balance_due_not_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_patients")),
    )
    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Integer(), nullable=True),
        sa.Column("doctor", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_short", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "doctor IN ('Dr. Patel', 'Dr. Lee')", name=op.f("ck_appointments_doctor")
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_appointments_patient_id_patients")
        ),
        sa.ForeignKeyConstraint(
            ["slot_id"],
            ["available_slots.id"],
            name=op.f("fk_appointments_slot_id_available_slots"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointments")),
        sa.UniqueConstraint("doctor", "starts_at", name=op.f("uq_appointments_doctor_starts_at")),
        sa.UniqueConstraint("slot_id", name=op.f("uq_appointments_slot_id")),
    )
    op.create_table(
        "medications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("refills_remaining", sa.Integer(), nullable=False),
        sa.Column("last_filled", sa.Date(), nullable=False),
        sa.Column("pharmacy", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "refills_remaining >= 0", name=op.f("ck_medications_refills_remaining_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name=op.f("fk_medications_patient_id_patients")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_medications")),
        sa.UniqueConstraint("patient_id", "name", name=op.f("uq_medications_patient_id_name")),
    )


def downgrade() -> None:
    op.drop_table("medications")
    op.drop_table("appointments")
    op.drop_table("patients")
    op.drop_table("available_slots")
