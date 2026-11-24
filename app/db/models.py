from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Connection,
    DateTime,
    ForeignKey,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    event,
    false,
    func,
    literal_column,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.clinic import DOCTORS

# Every constraint gets a predictable name, so the migration and the models can be compared
# constraint by constraint and errors name the rule that was broken.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SLOT_SOURCES = ("task", "generated")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {str: Text(), datetime: DateTime(timezone=True)}


def _doctor_is_known() -> CheckConstraint:
    return CheckConstraint(literal_column("doctor").in_(DOCTORS), name="doctor")


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint(
            "ssn_last4 IS NULL OR ssn_last4 ~ '^[0-9]{4}$'", name="ssn_last4_four_digits"
        ),
        CheckConstraint("balance_due >= 0", name="balance_due_not_negative"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    first_name: Mapped[str]
    last_name: Mapped[str]
    dob: Mapped[date]
    ssn_last4: Mapped[str | None] = mapped_column(CHAR(4))
    phone: Mapped[str]
    email: Mapped[str | None]
    insurance_provider: Mapped[str | None]
    member_id: Mapped[str | None]
    self_pay: Mapped[bool] = mapped_column(server_default=false())
    lab_results_status: Mapped[str]
    balance_due: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_new: Mapped[bool] = mapped_column(server_default=false())
    possible_duplicate: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Medication(Base):
    __tablename__ = "medications"
    __table_args__ = (
        UniqueConstraint("patient_id", "name"),
        CheckConstraint("refills_remaining >= 0", name="refills_remaining_not_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    name: Mapped[str]
    refills_remaining: Mapped[int]
    last_filled: Mapped[date]
    pharmacy: Mapped[str]


class AvailableSlot(Base):
    __tablename__ = "available_slots"
    __table_args__ = (
        UniqueConstraint("doctor", "starts_at"),
        _doctor_is_known(),
        CheckConstraint(literal_column("source").in_(SLOT_SOURCES), name="source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor: Mapped[str]
    starts_at: Mapped[datetime]
    is_available: Mapped[bool] = mapped_column(server_default=true())
    source: Mapped[str]


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("doctor", "starts_at"),
        _doctor_is_known(),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"))
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("available_slots.id"), unique=True)
    doctor: Mapped[str]
    starts_at: Mapped[datetime]
    reason_short: Mapped[str | None]
    status: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# Every text column of an audit event except the MACs: identifiers and registry codes only.
# Letters, digits, _ - : and ., 1 to 64 of them; no space, so never a sentence.
AUDIT_TEXT_PATTERN = "[A-Za-z0-9_:.-]{1,64}"


def _audit_text(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^{AUDIT_TEXT_PATTERN}$'", name=column)


class AuditEvent(Base):
    """One event of the audit log. Written and verified only by app/audit/log.py."""

    __tablename__ = "audit_events"
    __table_args__ = tuple(
        _audit_text(column)
        for column in (
            "call_id",
            "actor",
            "action",
            "resource_type",
            "resource_id",
            "outcome",
            "reason",
        )
    )

    # No default and no sequence: the app assigns previous + 1 under the chain's lock.
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ts: Mapped[datetime]
    call_id: Mapped[str | None]
    actor: Mapped[str]
    action: Mapped[str]
    resource_type: Mapped[str]
    resource_id: Mapped[str | None]
    outcome: Mapped[str]
    reason: Mapped[str | None]
    prev_mac: Mapped[str] = mapped_column(CHAR(64))
    mac: Mapped[str] = mapped_column(CHAR(64))


# audit_events is append-only for every role, the owner and superusers included: a statement
# that would update, delete or truncate is refused before it touches a row, even when it
# matches none. The migration creates the same function and triggers.
AUDIT_PROTECTION = (
    "CREATE FUNCTION audit_events_refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$\n"
    "BEGIN\n"
    "  RAISE EXCEPTION 'audit_events is append-only'\n"
    "    USING ERRCODE = 'insufficient_privilege', TABLE = TG_TABLE_NAME;\n"
    "END\n"
    "$$",
    "CREATE TRIGGER audit_events_refuse_update BEFORE UPDATE"
    " ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_refuse_change()",
    "CREATE TRIGGER audit_events_refuse_delete BEFORE DELETE"
    " ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_refuse_change()",
    "CREATE TRIGGER audit_events_refuse_truncate BEFORE TRUNCATE"
    " ON audit_events FOR EACH STATEMENT EXECUTE FUNCTION audit_events_refuse_change()",
)


@event.listens_for(AuditEvent.__table__, "after_create")
def _protect_audit_events(target: Table, connection: Connection, **kw: Any) -> None:
    for statement in AUDIT_PROTECTION:
        connection.execute(text(statement))
