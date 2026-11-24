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
    Index,
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
SCHEDULED = "scheduled"
REQUEST_TYPES = ("refill", "doctor_message", "callback", "transfer")
MESSAGE_KINDS = ("doctor_message", "callback")
MAX_REQUEST_SUMMARY = 200
MAX_MESSAGE_BODY = 120
# What makes two messages of one call the same message.
MESSAGE_KEY = ["call_id", "kind", "body", "name", "callback_number"]


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


def _length_between(column: str, longest: int, name: str) -> CheckConstraint:
    return CheckConstraint(f"char_length({column}) BETWEEN 1 AND {longest}", name=name)


class Request(Base):
    """Something a caller asked the clinic's staff to do. A call asks for the same thing once:
    the same type and summary again in one call is the same request."""

    __tablename__ = "requests"
    __table_args__ = (
        UniqueConstraint("call_id", "type", "summary"),
        CheckConstraint(literal_column("type").in_(REQUEST_TYPES), name="type"),
        _length_between("summary", MAX_REQUEST_SUMMARY, "summary_length"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str]
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"))
    type: Mapped[str]
    summary: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Message(Base):
    """A message a caller left for the clinic. The body is the caller's text. A call leaves
    the same message once: the same kind and text, and for a call back the same name and
    number (a doctor's message has neither; two missing values count as the same). A corrected
    name or number is a message of its own. `after_hours` says the office was closed when the
    call started."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(*MESSAGE_KEY, postgresql_nulls_not_distinct=True),
        CheckConstraint(literal_column("kind").in_(MESSAGE_KINDS), name="kind"),
        _length_between("body", MAX_MESSAGE_BODY, "body_length"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str]
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"))
    name: Mapped[str | None]
    callback_number: Mapped[str | None]
    body: Mapped[str]
    kind: Mapped[str]
    after_hours: Mapped[bool] = mapped_column(server_default=false())
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


class VerificationAttempt(Base):
    """One submitted identity check: the claim's keyed hash (app/agent/claims.py), the call, and
    whether it matched. Never a name, a date of birth or a digit of the SSN."""

    __tablename__ = "verification_attempts"
    __table_args__ = (
        CheckConstraint("claim_key ~ '^[0-9a-f]{64}$'", name="claim_key"),
        # The declared shape of a call id (CALL_ID in app/audit/log.py).
        CheckConstraint("call_id ~ '^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'", name="call_id"),
        Index("ix_verification_attempts_claim_key_created_at", "claim_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claim_key: Mapped[str]
    call_id: Mapped[str]
    success: Mapped[bool]
    # The call's clock, as the audit log's: never the database's.
    created_at: Mapped[datetime]


# audit_events is append-only for every role, the owner and superusers included: a statement
# that would update, delete or truncate is refused before it touches a row, even when it
# matches none. The migrations create the same functions and triggers. Both functions pin
# their search path and name the table with its schema, so nothing created later, ahead of
# them on a session's search path, can stand in for what they use.
AUDIT_PROTECTION = (
    "CREATE FUNCTION audit_events_refuse_change() RETURNS trigger LANGUAGE plpgsql"
    " SET search_path = pg_catalog, pg_temp AS $$\n"
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
    *(
        # The database itself keeps the chain in position: a row is accepted only at seq
        # head + 1 (1 on an empty table) after the head's MAC (64 zeros on an empty table),
        # as far as the inserting statement can see the head. Two inserts at the same
        # position can both pass this check when neither sees the other's uncommitted row;
        # the primary key on seq then refuses the second once the first commits. append
        # serialises its own writes with the chain's advisory lock, so they never meet here.
        "CREATE FUNCTION audit_events_check_position() RETURNS trigger LANGUAGE plpgsql"
        " SET search_path = pg_catalog, pg_temp AS $$\n"
        "DECLARE\n"
        "  head_seq bigint;\n"
        "  head_mac char(64);\n"
        "BEGIN\n"
        "  SELECT seq, mac INTO head_seq, head_mac FROM public.audit_events\n"
        "    ORDER BY seq DESC LIMIT 1;\n"
        "  IF NEW.seq IS DISTINCT FROM coalesce(head_seq, 0) + 1 THEN\n"
        "    RAISE EXCEPTION 'audit_events row out of position' USING\n"
        "      ERRCODE = 'integrity_constraint_violation', TABLE = TG_TABLE_NAME, COLUMN = 'seq';\n"
        "  END IF;\n"
        "  IF NEW.prev_mac IS DISTINCT FROM coalesce(head_mac, repeat('0', 64)) THEN\n"
        "    RAISE EXCEPTION 'audit_events row out of position' USING\n"
        "      ERRCODE = 'integrity_constraint_violation', TABLE = TG_TABLE_NAME,\n"
        "      COLUMN = 'prev_mac';\n"
        "  END IF;\n"
        "  RETURN NEW;\n"
        "END\n"
        "$$",
        "CREATE TRIGGER audit_events_check_position BEFORE INSERT"
        " ON audit_events FOR EACH ROW EXECUTE FUNCTION audit_events_check_position()",
    ),
)


@event.listens_for(AuditEvent.__table__, "after_create")
def _protect_audit_events(target: Table, connection: Connection, **kw: Any) -> None:
    for statement in AUDIT_PROTECTION:
        connection.execute(text(statement))
