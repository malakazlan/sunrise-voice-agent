"""Booking: a first-time caller's details, the open slots, and the appointment.

A first-time caller says so in intake: `start_new_patient` makes the call a new patient's
booking, which can never be verified. `collect_new_patient` checks their details and keeps them
in the call's state (a second call replaces them); nothing is written yet. `find_slots` offers
at most three open slots in the future, earliest first, to a verified caller or a new patient
with details, and the state remembers each slot it offered. `book_appointment` (a verified
caller, in serving) and `book_new_patient` (a new patient with details, in booking) book one of
those slots and nothing else: any other id, a slot taken since, or one that has begun, all
answer `slot_taken`, alike.

Without `confirmed=true` a booking writes nothing and answers `needs_confirmation`. With it, in
dispatch's one transaction: the slot is taken by one statement that changes it only while it is
still open and in the future (so of two calls booking it at once, one does and the other finds
it taken), then the new patient's record, the appointment and a `booking` request are added.
The slot row is kept, marked unavailable. A verified caller asking again for a slot this call
booked gets the same answer and nothing is written; a new patient books once, and the call
moves to closing.

A new patient's record has an id of its own (`NP-` and a random UUID), no SSN, and is flagged
`possible_duplicate` when a record with the same name key and date of birth exists; the caller
is told the same either way. The confirmation email is simulated: when the patient has an
email address, the booking's result names the appointment and dispatch writes the
`email_simulated` log line once the booking is saved (app/agent/tool_registry.py); no message
leaves the service. With no address on file the request tells staff to confirm by phone.
"""

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Row, extract, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.scripts import ScriptId
from app.agent.serving import (
    DAYS,
    NEEDS_CONFIRMATION,
    Weekday,
    spoken_date,
    spoken_time,
    stored_text,
    trimmed,
    verified_patient_id,
)
from app.agent.state import Caller, CallState, Move, NewPatientDetails, Phase, SlotsOffered
from app.agent.tool_registry import DobKind, HandlerContext, NoArguments, Tool, ToolResult
from app.agent.verification import DateOfBirth
from app.audit.log import Action, Outcome, ResourceType
from app.db.models import SCHEDULED, Appointment, AvailableSlot, Patient, Request
from app.domain.clinic import CLINIC_TZ, Doctor
from app.domain.matching import MAX_NAME_LENGTH, name_key
from app.guard.mask import mask_ssns
from app.guard.text import plain_text

BOOKING = "booking"
BOOKED = "booked"
SLOT_TAKEN = "slot_taken"

# find_slots offers at most this many.
MAX_OFFERED = 3
# Morning is before this hour at the clinic, afternoon from it.
NOON = 12
MAX_REASON = 120
MAX_EMAIL = 254
MAX_INSURANCE_PROVIDER = 100
MAX_MEMBER_ID = 40
NEW_PATIENT_PREFIX = "NP-"
NEW_PATIENT_LABS = "None on file"

# Separators a phone number may be said with; ten digits must remain.
PHONE_SEPARATORS = re.compile(r"[\s().-]")
TEN_DIGITS = re.compile(r"[0-9]{10}")
MEMBER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]*")
# One @, a local part, and a domain of at least two labels joined by dots.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# How the characters of an email address other than letters and digits are read back.
SPOKEN_SYMBOLS = {
    "@": "at",
    ".": "dot",
    "-": "dash",
    "_": "underscore",
    "+": "plus",
    "%": "percent",
}

_ARGUMENTS = ConfigDict(extra="forbid", hide_input_in_errors=True)


class NewPatientArguments(BaseModel):
    """A first-time caller's details, and nothing else: the full name (first and last, at
    least), the date of birth as an ISO date (YYYY-MM-DD), a ten-digit phone number, an email
    address, either an insurance provider and member id or self-pay, and a short reason for the
    visit."""

    model_config = _ARGUMENTS

    full_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    dob: DateOfBirth
    phone: str
    email: str = Field(max_length=MAX_EMAIL)
    insurance_provider: str | None = Field(
        default=None, min_length=1, max_length=MAX_INSURANCE_PROVIDER
    )
    member_id: str | None = Field(default=None, min_length=1, max_length=MAX_MEMBER_ID)
    self_pay: bool = Field(strict=True)
    reason_short: str = Field(min_length=1, max_length=MAX_REASON)

    @field_validator("full_name", "insurance_provider", "member_id", "reason_short", mode="before")
    @classmethod
    def _trimmed(cls, value: object) -> object:
        return trimmed(value)

    @field_validator("full_name")
    @classmethod
    def _is_a_full_name(cls, value: str) -> str:
        if name_key(plain_text(value, digits=False)) is None or len(value.split()) < 2:
            raise ValueError("full_name is not a first and last name")
        return value

    @field_validator("phone")
    @classmethod
    def _ten_digits(cls, value: str) -> str:
        digits = PHONE_SEPARATORS.sub("", value)
        if not TEN_DIGITS.fullmatch(digits):
            raise ValueError("phone must be ten digits")
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"

    @field_validator("email")
    @classmethod
    def _an_address(cls, value: str) -> str:
        if not EMAIL.fullmatch(value):
            raise ValueError("email is not an address")
        return value

    @field_validator("insurance_provider", "member_id")
    @classmethod
    def _no_identifier(cls, value: str | None) -> str | None:
        if value is not None and mask_ssns(plain_text(value, digits=True)) != value:
            raise ValueError("insurance details hold no SSN")
        return value

    @field_validator("member_id")
    @classmethod
    def _a_member_id(cls, value: str | None) -> str | None:
        if value is not None and not MEMBER_ID.fullmatch(value):
            raise ValueError("member_id is letters, digits, spaces and dashes")
        return value

    @field_validator("reason_short")
    @classmethod
    def _as_stored(cls, value: str) -> str:
        return stored_text(value, MAX_REASON)

    @model_validator(mode="after")
    def _insurance_or_self_pay(self) -> "NewPatientArguments":
        given = (self.insurance_provider is not None, self.member_id is not None)
        if given != ((False, False) if self.self_pay else (True, True)):
            raise ValueError("either insurance_provider and member_id, or self_pay")
        return self


class FindSlotsArguments(BaseModel):
    """Optional filters: one of the clinic's doctors, day names (Monday to Sunday), and the
    time of day."""

    model_config = _ARGUMENTS

    doctor: Doctor | None = None
    preferred_days: list[Weekday] | None = Field(default=None, min_length=1, max_length=7)
    preferred_time_of_day: Literal["morning", "afternoon"] | None = None


class BookAppointmentArguments(BaseModel):
    """A slot find_slots offered in this call, the reason for the visit, and whether the
    caller confirmed."""

    model_config = _ARGUMENTS

    slot_id: int = Field(strict=True, ge=1)
    reason_short: str = Field(min_length=1, max_length=MAX_REASON)
    confirmed: bool = Field(default=False, strict=True)

    @field_validator("reason_short", mode="before")
    @classmethod
    def _trimmed(cls, value: object) -> object:
        return trimmed(value)

    @field_validator("reason_short")
    @classmethod
    def _as_stored(cls, value: str) -> str:
        return stored_text(value, MAX_REASON)


class BookNewPatientArguments(BaseModel):
    """A slot find_slots offered in this call, and whether the caller confirmed."""

    model_config = _ARGUMENTS

    slot_id: int = Field(strict=True, ge=1)
    confirmed: bool = Field(default=False, strict=True)


def spelled(email: str) -> str:
    """The address letter by letter, for reading back: `A N N dot E X at ...`."""
    return " ".join(SPOKEN_SYMBOLS.get(char, char.upper()) for char in email)


def _answer(status: str, doctor: str, starts_at: datetime) -> dict[str, Any]:
    return {
        "status": status,
        "doctor": doctor,
        "date": spoken_date(starts_at),
        "time": spoken_time(starts_at),
    }


# A slot's doctor and start, as a booking reads them.
SlotTime = Row[str, datetime]

_TAKEN = ToolResult(data={"status": SLOT_TAKEN}, audit_action=Action.SLOT_TAKEN)


def _summary(doctor: str, starts_at: datetime, email: str | None) -> str:
    """What staff read: the slot, and how it is confirmed: by the simulated email when the
    patient has an address, by a phone call from staff otherwise."""
    local = starts_at.astimezone(CLINIC_TZ)
    confirmation = "confirm by phone" if email is None else "confirmation email simulated"
    return (
        f"{doctor} on {spoken_date(starts_at)}, {local.year} at {spoken_time(starts_at)};"
        f" {confirmation}"
    )


async def _open_slot(session: AsyncSession, slot_id: int, now: datetime) -> SlotTime | None:
    """The slot's doctor and time, while it is open and in the future."""
    return (
        await session.execute(
            select(AvailableSlot.doctor, AvailableSlot.starts_at).where(
                AvailableSlot.id == slot_id,
                AvailableSlot.is_available,
                AvailableSlot.starts_at > now,
            )
        )
    ).one_or_none()


async def _take(session: AsyncSession, slot_id: int, now: datetime) -> SlotTime | None:
    """Take the slot, in one statement that changes it only while it is still open and in the
    future; its doctor and time if this took it, None if it was not there to take. A booking
    that took it holds its row until the transaction ends, so another waits and then finds it
    taken."""
    return (
        await session.execute(
            update(AvailableSlot)
            .where(
                AvailableSlot.id == slot_id,
                AvailableSlot.is_available,
                AvailableSlot.starts_at > now,
            )
            .values(is_available=False)
            .returning(AvailableSlot.doctor, AvailableSlot.starts_at)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()


async def _add_appointment(
    session: AsyncSession, patient_id: str, slot_id: int, slot: SlotTime, reason: str
) -> int:
    appointment_id: int = (
        await session.execute(
            insert(Appointment)
            .values(
                patient_id=patient_id,
                slot_id=slot_id,
                doctor=slot.doctor,
                starts_at=slot.starts_at,
                reason_short=reason,
                status=SCHEDULED,
            )
            .returning(Appointment.id)
        )
    ).scalar_one()
    return appointment_id


async def _add_request(
    session: AsyncSession, state: CallState, patient_id: str, summary: str
) -> None:
    await session.execute(
        insert(Request).values(
            call_id=state.call_id, patient_id=patient_id, type=BOOKING, summary=summary
        )
    )


async def start_new_patient(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    # The model is told plainly that it worked: with nothing returned it has asked again.
    return ToolResult(data={"status": "started"}, transition=Move(to=Phase.BOOKING))


async def collect_new_patient(
    session: AsyncSession,
    state: CallState,
    arguments: NewPatientArguments,
    context: HandlerContext,
) -> ToolResult:
    *first, last = arguments.full_name.split()
    details = NewPatientDetails(
        first_name=" ".join(first),
        last_name=last,
        dob=arguments.dob,
        phone=arguments.phone,
        email=arguments.email,
        insurance_provider=arguments.insurance_provider,
        member_id=arguments.member_id,
        self_pay=arguments.self_pay,
        reason_short=arguments.reason_short,
    )
    return ToolResult(data={"email_spelled": spelled(arguments.email)}, transition=details)


async def find_slots(
    session: AsyncSession,
    state: CallState,
    arguments: FindSlotsArguments,
    context: HandlerContext,
) -> ToolResult:
    local = func.timezone(CLINIC_TZ.key, AvailableSlot.starts_at)
    query = select(AvailableSlot.id, AvailableSlot.doctor, AvailableSlot.starts_at).where(
        AvailableSlot.is_available, AvailableSlot.starts_at > context.now
    )
    if arguments.doctor is not None:
        query = query.where(AvailableSlot.doctor == arguments.doctor)
    if arguments.preferred_days is not None:
        # ISO day numbers: Monday is 1.
        days = sorted({DAYS.index(day) + 1 for day in arguments.preferred_days})
        query = query.where(extract("isodow", local).in_(days))
    if arguments.preferred_time_of_day == "morning":
        query = query.where(extract("hour", local) < NOON)
    elif arguments.preferred_time_of_day == "afternoon":
        query = query.where(extract("hour", local) >= NOON)
    slots = (
        await session.execute(
            query.order_by(AvailableSlot.starts_at, AvailableSlot.id).limit(MAX_OFFERED)
        )
    ).all()
    listed = [
        {
            "slot_id": slot.id,
            "doctor": slot.doctor,
            "date": spoken_date(slot.starts_at),
            "time": spoken_time(slot.starts_at),
        }
        for slot in slots
    ]
    offered = SlotsOffered(slot_ids=tuple(slot.id for slot in slots)) if slots else None
    return ToolResult(data={"slots": listed}, transition=offered)


async def book_appointment(
    session: AsyncSession,
    state: CallState,
    arguments: BookAppointmentArguments,
    context: HandlerContext,
) -> ToolResult:
    patient_id = verified_patient_id(state)
    if arguments.slot_id not in state.offered_slots:
        return _TAKEN
    slot = (
        await session.execute(
            select(AvailableSlot.doctor, AvailableSlot.starts_at).where(
                AvailableSlot.id == arguments.slot_id
            )
        )
    ).one()
    email = (
        await session.execute(select(Patient.email).where(Patient.id == patient_id))
    ).scalar_one()
    summary = _summary(slot.doctor, slot.starts_at, email)
    # This call booked it already: the same answer, and nothing written.
    repeat = await session.scalar(
        select(Request.id).where(
            Request.call_id == state.call_id,
            Request.patient_id == patient_id,
            Request.type == BOOKING,
            Request.summary == summary,
        )
    )
    if repeat is not None:
        return ToolResult(
            data=_answer(BOOKED, slot.doctor, slot.starts_at),
            audit_action=Action.BOOKING_REPEATED,
        )
    if not arguments.confirmed:
        if await _open_slot(session, arguments.slot_id, context.now) is None:
            return _TAKEN
        return ToolResult(
            data=_answer(NEEDS_CONFIRMATION, slot.doctor, slot.starts_at),
            audit_action=Action.BOOKING_PREVIEWED,
        )

    taken = await _take(session, arguments.slot_id, context.now)
    if taken is None:
        return _TAKEN
    appointment_id = await _add_appointment(
        session, patient_id, arguments.slot_id, taken, arguments.reason_short
    )
    await _add_request(session, state, patient_id, summary)
    return ToolResult(
        data=_answer(BOOKED, taken.doctor, taken.starts_at),
        emailed_appointment=None if email is None else appointment_id,
    )


async def _has_record(session: AsyncSession, details: NewPatientDetails) -> bool:
    """Whether a record has the same name key and date of birth. Code alone reads it."""
    key = name_key(f"{details.first_name} {details.last_name}")
    named = (
        await session.execute(
            select(Patient.first_name, Patient.last_name).where(Patient.dob == details.dob)
        )
    ).all()
    return any(name_key(f"{row.first_name} {row.last_name}") == key for row in named)


async def book_new_patient(
    session: AsyncSession,
    state: CallState,
    arguments: BookNewPatientArguments,
    context: HandlerContext,
) -> ToolResult:
    details = state.new_patient_details
    if details is None:
        raise PermissionError("no new patient's details")
    if arguments.slot_id not in state.offered_slots:
        return _TAKEN
    if not arguments.confirmed:
        slot = await _open_slot(session, arguments.slot_id, context.now)
        if slot is None:
            return _TAKEN
        return ToolResult(
            data=_answer(NEEDS_CONFIRMATION, slot.doctor, slot.starts_at),
            audit_action=Action.BOOKING_PREVIEWED,
        )

    taken = await _take(session, arguments.slot_id, context.now)
    if taken is None:
        return _TAKEN
    patient_id = f"{NEW_PATIENT_PREFIX}{uuid.uuid4()}"
    await session.execute(
        insert(Patient).values(
            id=patient_id,
            first_name=details.first_name,
            last_name=details.last_name,
            dob=details.dob,
            ssn_last4=None,
            phone=details.phone,
            email=details.email,
            insurance_provider=details.insurance_provider,
            member_id=details.member_id,
            self_pay=details.self_pay,
            lab_results_status=NEW_PATIENT_LABS,
            balance_due=0,
            is_new=True,
            possible_duplicate=await _has_record(session, details),
        )
    )
    appointment_id = await _add_appointment(
        session, patient_id, arguments.slot_id, taken, details.reason_short
    )
    await _add_request(
        session, state, patient_id, _summary(taken.doctor, taken.starts_at, details.email)
    )
    return ToolResult(
        data=_answer(BOOKED, taken.doctor, taken.starts_at),
        transition=Move(to=Phase.CLOSING),
        script=ScriptId.BOOKING_INSTRUCTIONS,
        resource_id=str(appointment_id),
        emailed_appointment=appointment_id,
    )


_PREVIEW_OR_TAKEN = {Action.BOOKING_PREVIEWED: Outcome.SUCCESS, Action.SLOT_TAKEN: Outcome.REFUSED}

BOOKING_TOOLS = (
    Tool(
        name="start_new_patient",
        arguments=NoArguments,
        handler=start_new_patient,
        model_callable=True,
        phi=False,
        phases=frozenset({Phase.INTAKE}),
        closed_phases=frozenset(),
        writes=False,
        may_verify=False,
        takes_date_of_birth=None,
        callers=frozenset({Caller.UNIDENTIFIED}),
        audit_action=Action.NEW_PATIENT_STARTED,
        outcome_actions={},
        audit_reasons=frozenset(),
        resource_type=ResourceType.TOOL,
    ),
    Tool(
        name="collect_new_patient",
        arguments=NewPatientArguments,
        handler=collect_new_patient,
        model_callable=True,
        phi=False,
        phases=frozenset({Phase.BOOKING}),
        closed_phases=frozenset(),
        writes=False,
        may_verify=False,
        takes_date_of_birth=DobKind.NEW_PATIENT_DETAILS,
        callers=frozenset({Caller.NEW_PATIENT, Caller.NEW_PATIENT_WITH_DETAILS}),
        audit_action=Action.NEW_PATIENT_DETAILS_COLLECTED,
        outcome_actions={},
        audit_reasons=frozenset(),
        resource_type=ResourceType.TOOL,
    ),
    Tool(
        name="find_slots",
        arguments=FindSlotsArguments,
        handler=find_slots,
        model_callable=True,
        phi=False,
        phases=frozenset({Phase.SERVING, Phase.BOOKING}),
        closed_phases=frozenset(),
        writes=False,
        may_verify=False,
        takes_date_of_birth=None,
        callers=frozenset({Caller.PATIENT, Caller.NEW_PATIENT_WITH_DETAILS}),
        audit_action=Action.SLOTS_OFFERED,
        outcome_actions={},
        audit_reasons=frozenset(),
        resource_type=ResourceType.TOOL,
    ),
    Tool(
        name="book_appointment",
        arguments=BookAppointmentArguments,
        handler=book_appointment,
        model_callable=True,
        phi=True,
        phases=frozenset({Phase.SERVING}),
        closed_phases=frozenset(),
        writes=True,
        may_verify=False,
        takes_date_of_birth=None,
        callers=frozenset({Caller.PATIENT}),
        audit_action=Action.APPOINTMENT_BOOKED,
        outcome_actions={**_PREVIEW_OR_TAKEN, Action.BOOKING_REPEATED: Outcome.SUCCESS},
        audit_reasons=frozenset(),
        resource_type=ResourceType.PATIENT,
    ),
    Tool(
        name="book_new_patient",
        arguments=BookNewPatientArguments,
        handler=book_new_patient,
        model_callable=True,
        phi=False,
        phases=frozenset({Phase.BOOKING}),
        closed_phases=frozenset(),
        writes=True,
        may_verify=False,
        takes_date_of_birth=None,
        callers=frozenset({Caller.NEW_PATIENT_WITH_DETAILS}),
        audit_action=Action.NEW_PATIENT_BOOKED,
        outcome_actions=_PREVIEW_OR_TAKEN,
        audit_reasons=frozenset(),
        resource_type=ResourceType.APPOINTMENT,
    ),
)
