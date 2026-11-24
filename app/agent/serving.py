"""The tools a verified caller is served by, in `serving`: the next appointment, a refill
request, the lab status, the balance and a message to the doctor.

Each acts on the state's patient id only, and returns only what its request needs. Dispatch
runs them inside its transaction and writes the audit event; a handler never ends the
transaction. A writing tool called without `confirmed=true` writes nothing and returns
`needs_confirmation` with what it would write. A call writes the same request once: the
database's unique constraints on `requests` and `messages` decide, and a repeat inserts
nothing and answers as the first did.

"Now", for the next appointment, is the moment of the question on the call's clock (the
handler context's `now`), so an appointment that began during the call is no longer next.
"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import CallState, Phase
from app.agent.tool_registry import Handler, HandlerContext, NoArguments, Tool, ToolResult
from app.audit.log import Action, ResourceType
from app.db.models import (
    MAX_MESSAGE_BODY,
    MAX_REQUEST_SUMMARY,
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

REFILL = "refill"
DOCTOR_MESSAGE = "doctor_message"

NEEDS_CONFIRMATION = "needs_confirmation"
NOT_FOUND = "not_found"
REQUESTED = "requested"
SENT_TO_DOCTOR = "sent_to_doctor"
SENT = "sent"

# C0 control characters, NUL included, and DEL: never in a stored message.
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
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
        """Trimmed before the length bounds apply; anything but text is left to be refused."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("summary_short")
    @classmethod
    def _as_stored(cls, value: str) -> str:
        """The text as it is read back and stored: trimmed, SSNs masked, still in bounds."""
        if CONTROL_CHARACTER.search(value):
            raise ValueError("summary_short must not contain control characters")
        stored = mask_ssns(value)
        if not 1 <= len(stored) <= MAX_MESSAGE_BODY:
            raise ValueError(f"summary_short must be 1 to {MAX_MESSAGE_BODY} characters")
        return stored


def _patient_id(state: CallState) -> str:
    """The verified caller's patient id. The gate allows these tools only with one; a state
    without one stops the call."""
    if state.patient_id is None:
        raise PermissionError("no verified patient")
    return state.patient_id


def _spoken(starts_at: datetime, doctor: str) -> str:
    """For example `Thursday, December 3 at 10:30 AM with Dr. Patel`, in clinic time."""
    local = starts_at.astimezone(CLINIC_TZ)
    hour = local.hour % 12 or 12
    noon = "AM" if local.hour < 12 else "PM"
    return (
        f"{DAYS[local.weekday()]}, {MONTHS[local.month - 1]} {local.day}"
        f" at {hour}:{local.minute:02d} {noon} with {doctor}"
    )


async def get_next_appointment(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    next_one = (
        await session.execute(
            select(Appointment.starts_at, Appointment.doctor)
            .where(
                Appointment.patient_id == _patient_id(state),
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
            select(Patient.lab_results_status).where(Patient.id == _patient_id(state))
        )
    ).scalar_one()
    return ToolResult(data={"lab_status": status})


async def get_balance(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    balance = (
        await session.execute(select(Patient.balance_due).where(Patient.id == _patient_id(state)))
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
            patient_id=_patient_id(state),
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
                Medication.patient_id == _patient_id(state)
            )
        )
    ).all()
    matched = match_medication(arguments.medication_name, [row.name for row in medications])
    if matched is None:
        return ToolResult(data={"status": NOT_FOUND})
    (medication,) = [row for row in medications if row.name == matched]
    named = {"medication": medication.name, "pharmacy": medication.pharmacy}
    if not arguments.confirmed:
        return ToolResult(data={"status": NEEDS_CONFIRMATION, **named})

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
        return ToolResult(data={"status": NEEDS_CONFIRMATION, "message": body})

    patient_id = _patient_id(state)
    message_id = await session.scalar(
        insert(Message)
        .values(call_id=state.call_id, patient_id=patient_id, kind=DOCTOR_MESSAGE, body=body)
        .on_conflict_do_nothing(index_elements=["call_id", "kind", "body"])
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
    name: str, arguments: type[BaseModel], handler: Handler, *, writes: bool, action: Action
) -> Tool:
    return Tool(
        name=name,
        arguments=arguments,
        handler=handler,
        model_callable=True,
        phi=True,
        phases=frozenset({Phase.SERVING}),
        office_open_only=True,
        writes=writes,
        may_verify=False,
        takes_identity_claim=False,
        audit_action=action,
        outcome_actions={},
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
    ),
)
