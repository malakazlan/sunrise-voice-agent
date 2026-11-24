"""The tools a verified caller is served by, in `serving`: the next appointment, a refill
request, the lab status, the balance and a message to the doctor.

Each acts on the state's patient id only, and returns only what its request needs. Dispatch
runs them inside its transaction and writes the audit event; a handler never ends the
transaction. A writing tool called without `confirmed=true` writes nothing and returns
`needs_confirmation` with what it would write. A call writes the same request once: the
database's unique constraints on `requests` and `messages` decide, and a repeat inserts
nothing and answers as the first did. The event says what came of a writing tool's call:
previewed, not found (a refill, outcome refused), or requested and sent once the row is
written or already there.

"Now", for the next appointment, is the moment of the question on the call's clock (the
handler context's `now`), so an appointment that began during the call is no longer next.
"""

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import Caller, CallState, Phase
from app.agent.tool_registry import Handler, HandlerContext, NoArguments, Tool, ToolResult
from app.audit.log import Action, Outcome, ResourceType
from app.db.models import (
    MAX_MESSAGE_BODY,
    MAX_REQUEST_SUMMARY,
    MESSAGE_KEY,
    SCHEDULED,
    Appointment,
    Medication,
    Message,
    Patient,
    Request,
)
from app.domain.clinic import CLINIC_TZ
from app.domain.matching import match_medication
from app.guard.mask import mask_ssns
from app.guard.text import plain_text

REFILL = "refill"
DOCTOR_MESSAGE = "doctor_message"

NEEDS_CONFIRMATION = "needs_confirmation"
NOT_FOUND = "not_found"
REQUESTED = "requested"
SENT_TO_DOCTOR = "sent_to_doctor"
SENT = "sent"

Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# The day names, Monday first, as datetime.weekday() counts them.
DAYS: tuple[Weekday, ...] = get_args(Weekday)
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class RefillArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    medication_name: str
    confirmed: bool = Field(default=False, strict=True)


class MessageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    summary_short: str = Field(min_length=1, max_length=MAX_MESSAGE_BODY)
    confirmed: bool = Field(default=False, strict=True)

    @field_validator("summary_short", mode="before")
    @classmethod
    def _trimmed(cls, value: object) -> object:
        return trimmed(value)

    @field_validator("summary_short")
    @classmethod
    def _as_stored(cls, value: str) -> str:
        return stored_text(value, MAX_MESSAGE_BODY)


def trimmed(value: object) -> object:
    """Text trimmed before its length bounds apply; anything but text is left to be refused."""
    return value.strip() if isinstance(value, str) else value


def stored_text(value: str, longest: int) -> str:
    """A caller's short text as it is read back and stored: plain text (digits allowed), SSNs
    masked, and 1 to `longest` characters once masked."""
    stored = mask_ssns(plain_text(value, digits=True))
    if not 1 <= len(stored) <= longest:
        raise ValueError(f"the text must be 1 to {longest} characters")
    return stored


def verified_patient_id(state: CallState) -> str:
    """The verified caller's patient id. The gate allows a patient's tools only with one; a
    state without one stops the call."""
    if state.patient_id is None:
        raise PermissionError("no verified patient")
    return state.patient_id


def spoken_date(starts_at: datetime) -> str:
    """For example `Thursday, December 3`, in clinic time."""
    local = starts_at.astimezone(CLINIC_TZ)
    return f"{DAYS[local.weekday()]}, {MONTHS[local.month - 1]} {local.day}"


def spoken_time(starts_at: datetime) -> str:
    """For example `10:30 AM`, in clinic time."""
    local = starts_at.astimezone(CLINIC_TZ)
    noon = "AM" if local.hour < 12 else "PM"
    return f"{local.hour % 12 or 12}:{local.minute:02d} {noon}"


def _spoken(starts_at: datetime, doctor: str) -> str:
    """For example `Thursday, December 3 at 10:30 AM with Dr. Patel`, in clinic time."""
    return f"{spoken_date(starts_at)} at {spoken_time(starts_at)} with {doctor}"


async def get_next_appointment(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    next_one = (
        await session.execute(
            select(Appointment.starts_at, Appointment.doctor)
            .where(
                Appointment.patient_id == verified_patient_id(state),
                Appointment.status == SCHEDULED,
                Appointment.starts_at >= context.now,
            )
            .order_by(Appointment.starts_at)
            .limit(1)
        )
    ).one_or_none()
    spoken = None if next_one is None else _spoken(next_one.starts_at, next_one.doctor)
    return ToolResult(data={"appointment": spoken})


async def get_lab_status(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    status = (
        await session.execute(
            select(Patient.lab_results_status).where(Patient.id == verified_patient_id(state))
        )
    ).scalar_one()
    return ToolResult(data={"lab_status": status})


async def get_balance(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    balance = (
        await session.execute(
            select(Patient.balance_due).where(Patient.id == verified_patient_id(state))
        )
    ).scalar_one()
    return ToolResult(data={"balance": f"{balance:.2f}"})


async def _record_request(
    session: AsyncSession, state: CallState, request_type: str, summary: str
) -> None:
    """The request's row, unless this call already asked for the same."""
    await session.execute(
        insert(Request)
        .values(
            call_id=state.call_id,
            patient_id=verified_patient_id(state),
            type=request_type,
            summary=summary,
        )
        .on_conflict_do_nothing(index_elements=["call_id", "type", "summary"])
    )


async def request_refill(
    session: AsyncSession, state: CallState, arguments: RefillArguments, context: HandlerContext
) -> ToolResult:
    medications = (
        await session.execute(
            select(Medication.name, Medication.refills_remaining, Medication.pharmacy).where(
                Medication.patient_id == verified_patient_id(state)
            )
        )
    ).all()
    matched = match_medication(arguments.medication_name, [row.name for row in medications])
    if matched is None:
        return ToolResult(data={"status": NOT_FOUND}, audit_action=Action.REFILL_NOT_FOUND)
    (medication,) = [row for row in medications if row.name == matched]
    named = {"medication": medication.name, "pharmacy": medication.pharmacy}
    if not arguments.confirmed:
        return ToolResult(
            data={"status": NEEDS_CONFIRMATION, **named}, audit_action=Action.REFILL_PREVIEWED
        )

    # Cut to the column's bound, always at the same place, so a repeat has the same summary.
    summary = f"{medication.name} at {medication.pharmacy}"[:MAX_REQUEST_SUMMARY]
    await _record_request(session, state, REFILL, summary)
    status = REQUESTED if medication.refills_remaining > 0 else SENT_TO_DOCTOR
    return ToolResult(data={"status": status, **named})


async def send_message_to_doctor(
    session: AsyncSession, state: CallState, arguments: MessageArguments, context: HandlerContext
) -> ToolResult:
    body = arguments.summary_short
    if not arguments.confirmed:
        return ToolResult(
            data={"status": NEEDS_CONFIRMATION, "message": body},
            audit_action=Action.DOCTOR_MESSAGE_PREVIEWED,
        )

    patient_id = verified_patient_id(state)
    message_id = await session.scalar(
        insert(Message)
        .values(call_id=state.call_id, patient_id=patient_id, kind=DOCTOR_MESSAGE, body=body)
        .on_conflict_do_nothing(index_elements=MESSAGE_KEY)
        .returning(Message.id)
    )
    if message_id is None:
        # This call left the same message already: its id, as the first call returned it.
        message_id = (
            await session.execute(
                select(Message.id).where(
                    Message.call_id == state.call_id,
                    Message.patient_id == patient_id,
                    Message.kind == DOCTOR_MESSAGE,
                    Message.body == body,
                )
            )
        ).scalar_one()
    await _record_request(session, state, DOCTOR_MESSAGE, body)
    return ToolResult(data={"status": SENT, "message_id": message_id})


def _serving_tool(
    name: str,
    arguments: type[BaseModel],
    handler: Handler,
    *,
    writes: bool,
    action: Action,
    outcome_actions: dict[Action, Outcome] | None = None,
) -> Tool:
    return Tool(
        name=name,
        arguments=arguments,
        handler=handler,
        model_callable=True,
        phi=True,
        phases=frozenset({Phase.SERVING}),
        closed_phases=frozenset(),
        writes=writes,
        may_verify=False,
        takes_date_of_birth=None,
        callers=frozenset({Caller.PATIENT}),
        audit_action=action,
        outcome_actions={} if outcome_actions is None else outcome_actions,
        audit_reasons=frozenset(),
        resource_type=ResourceType.PATIENT,
    )


SERVING_TOOLS = (
    _serving_tool(
        "get_next_appointment",
        NoArguments,
        get_next_appointment,
        writes=False,
        action=Action.APPOINTMENT_READ,
    ),
    _serving_tool(
        "request_refill",
        RefillArguments,
        request_refill,
        writes=True,
        action=Action.REFILL_REQUESTED,
        outcome_actions={
            Action.REFILL_PREVIEWED: Outcome.SUCCESS,
            Action.REFILL_NOT_FOUND: Outcome.REFUSED,
        },
    ),
    _serving_tool(
        "get_lab_status", NoArguments, get_lab_status, writes=False, action=Action.LAB_STATUS_READ
    ),
    _serving_tool(
        "get_balance", NoArguments, get_balance, writes=False, action=Action.BALANCE_READ
    ),
    _serving_tool(
        "send_message_to_doctor",
        MessageArguments,
        send_message_to_doctor,
        writes=True,
        action=Action.DOCTOR_MESSAGE_SENT,
        outcome_actions={Action.DOCTOR_MESSAGE_PREVIEWED: Outcome.SUCCESS},
    ),
)
