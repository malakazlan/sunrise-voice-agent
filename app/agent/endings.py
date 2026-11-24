"""How a call starts, and the four ways it ends or leaves the agent: a message for the staff, a
transfer to them, the emergency line and the goodbye.

The four tools go through dispatch like any other. `take_message` is the only one that writes
caller text, and only once the caller has confirmed it; it never reads a patient record: an
unverified caller's message has no patient id, a verified caller's has the state's. With the
office closed it is all the call can do, besides the emergency line and the goodbye, and a
message closes the call. `transfer_to_staff` needs the office open; the transfer is simulated:
the request row and the event are real, there is no phone system behind them.

`start_call` creates the call, deciding once from the call's clock whether the office is open,
and `end_call` is the one way it ends; `emergency` is the input guard's way to the emergency
line. Their events (`call_started`, `call_ended`, `emergency_detected`) are written in a
transaction of their own within RECORD_DEADLINE_SECONDS. A write that fails or runs out of time
is logged and changes nothing else: the state and the line are returned all the same, so the
emergency line never depends on the database.

`end_call` writes `call_ended` once per call: for a call `start_call` started in this process,
the first `end_call` that ends it. The calls this process has started and not ended are held
in memory.

Logs carry event names, call ids, reasons and exception class names; never a name, a number or
message text.
"""

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.scripts import ScriptId
from app.agent.serving import CONTROL_CHARACTER, NEEDS_CONFIRMATION
from app.agent.state import CallState, IllegalTransitionError, Move, Phase, move, new_call
from app.agent.tool_registry import Deps, HandlerContext, NoArguments, Tool, ToolResult
from app.audit.log import AGENT, Action, Event, Outcome, Reason, ResourceType
from app.db.models import MAX_MESSAGE_BODY, Message, Request
from app.domain.office_hours import is_open, part_of_day
from app.guard.mask import mask_ssns

logger = logging.getLogger(__name__)

CALLBACK = "callback"
TRANSFER = "transfer"
RECORDED = "recorded"
TRANSFERRING = "transferring"

MAX_CALLER_NAME = 80
CALLBACK_DIGITS = 10
# What a caller may say between the digits of a callback number.
NUMBER_SEPARATORS = frozenset(" -.()")

# A call event's write ends within this many seconds, or it is given up and logged.
RECORD_DEADLINE_SECONDS = 2

# The phases the model has tools in.
TOOL_PHASES = frozenset({Phase.INTAKE, Phase.SERVING, Phase.BOOKING})
_LIVE_PHASES = frozenset(Phase) - {Phase.ENDED}

# Why a call ends, and the phases it may end from for that reason. The call is over whatever
# phase it is in when the caller hangs up, the line goes idle, the call runs out of time or
# the pipeline fails.
END_PHASES: dict[Reason, frozenset[Phase]] = {
    Reason.COMPLETED: frozenset({Phase.CLOSING}),
    Reason.EMERGENCY: frozenset({Phase.EMERGENCY}),
    Reason.TRANSFERRED: frozenset({Phase.HANDOFF}),
    Reason.CALLER_HUNG_UP: _LIVE_PHASES,
    Reason.IDLE_TIMEOUT: _LIVE_PHASES,
    Reason.MAX_DURATION: _LIVE_PHASES,
    Reason.ERROR: _LIVE_PHASES,
}
EMERGENCY_TRIGGERS = frozenset({Reason.GUARD, Reason.MODEL})

_LIVE_CALLS: set[str] = set()


def _as_stored(value: str, longest: int, field: str) -> str:
    """Caller text as it is read back and stored: no control character, SSNs masked, and at most
    `longest` characters once masked. Masking makes text longer, never empty."""
    if CONTROL_CHARACTER.search(value):
        raise ValueError(f"{field} must not contain control characters")
    stored = mask_ssns(value)
    if len(stored) > longest:
        raise ValueError(f"{field} must be at most {longest} characters once SSNs are masked")
    return stored


class TakeMessageArguments(BaseModel):
    """The caller's name, a ten-digit number to call back and the reason, in short."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=MAX_CALLER_NAME)
    callback_number: str
    reason_short: str = Field(min_length=1, max_length=MAX_MESSAGE_BODY)
    confirmed: bool = Field(default=False, strict=True)

    @field_validator("name", "reason_short", mode="before")
    @classmethod
    def _trimmed(cls, value: object) -> object:
        """Trimmed before the length bounds apply; anything but text is left to be refused."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def _name_as_stored(cls, value: str) -> str:
        return _as_stored(value, MAX_CALLER_NAME, "name")

    @field_validator("reason_short")
    @classmethod
    def _reason_as_stored(cls, value: str) -> str:
        return _as_stored(value, MAX_MESSAGE_BODY, "reason_short")

    @field_validator("callback_number")
    @classmethod
    def _ten_digits(cls, value: str) -> str:
        """The digits alone, once the separators are taken out: exactly ten of them."""
        digits = "".join(character for character in value if character not in NUMBER_SEPARATORS)
        if len(digits) != CALLBACK_DIGITS or not (digits.isascii() and digits.isdigit()):
            raise ValueError("callback_number must be ten digits")
        return digits


class TransferArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    reason: Literal["caller_request", "frustration", "verification_locked", "other"]
    confirmed: bool = Field(default=False, strict=True)


async def _record_request(
    session: AsyncSession, state: CallState, request_type: str, summary: str
) -> None:
    """The request's row, with the caller's patient id if verified, unless this call already
    asked for the same."""
    await session.execute(
        insert(Request)
        .values(
            call_id=state.call_id,
            patient_id=state.patient_id,
            type=request_type,
            summary=summary,
        )
        .on_conflict_do_nothing(index_elements=["call_id", "type", "summary"])
    )


async def take_message(
    session: AsyncSession,
    state: CallState,
    arguments: TakeMessageArguments,
    context: HandlerContext,
) -> ToolResult:
    details = {
        "name": arguments.name,
        "callback_number": arguments.callback_number,
        "reason_short": arguments.reason_short,
    }
    if not arguments.confirmed:
        return ToolResult(
            data={"status": NEEDS_CONFIRMATION, **details},
            audit_action=Action.MESSAGE_PREVIEWED,
            resource_id=state.call_id,
        )

    await session.execute(
        insert(Message)
        .values(
            call_id=state.call_id,
            patient_id=state.patient_id,
            name=arguments.name,
            callback_number=arguments.callback_number,
            kind=CALLBACK,
            body=arguments.reason_short,
            after_hours=not state.office_open,
        )
        .on_conflict_do_nothing(index_elements=["call_id", "kind", "body"])
    )
    await _record_request(session, state, CALLBACK, arguments.reason_short)
    if state.office_open:
        return ToolResult(data={"status": RECORDED}, resource_id=state.call_id)
    return ToolResult(
        data={"status": RECORDED},
        transition=Move(to=Phase.CLOSING),
        script=ScriptId.AFTER_HOURS_CLOSE,
        script_values={"name": arguments.name, "part_of_day": part_of_day(context.now)},
        resource_id=state.call_id,
    )


async def _transfer(session: AsyncSession, state: CallState, reason: Reason) -> ToolResult:
    """The transfer's request row, and the call handed to the staff."""
    await _record_request(session, state, TRANSFER, reason.value)
    return ToolResult(
        data={"status": TRANSFERRING},
        transition=Move(to=Phase.HANDOFF),
        script=ScriptId.TRANSFER,
        audit_reason=reason,
        resource_id=state.call_id,
    )


async def transfer_to_staff(
    session: AsyncSession, state: CallState, arguments: TransferArguments, context: HandlerContext
) -> ToolResult:
    if not arguments.confirmed:
        return ToolResult(
            data={"status": NEEDS_CONFIRMATION, "reason": arguments.reason},
            audit_action=Action.TRANSFER_PREVIEWED,
            resource_id=state.call_id,
        )
    return await _transfer(session, state, Reason(arguments.reason))


async def hand_off_to_staff(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    """Code's transfer, when the call keeps failing or being refused."""
    reason = Reason.VERIFICATION_LOCKED if state.verify_locked else Reason.OTHER
    return await _transfer(session, state, reason)


async def report_emergency(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    return ToolResult(
        transition=Move(to=Phase.EMERGENCY),
        script=ScriptId.EMERGENCY,
        audit_reason=Reason.MODEL,
        resource_id=state.call_id,
    )


async def close_call(
    session: AsyncSession, state: CallState, arguments: NoArguments, context: HandlerContext
) -> ToolResult:
    return ToolResult(
        transition=Move(to=Phase.CLOSING), script=ScriptId.GOODBYE, resource_id=state.call_id
    )


TAKE_MESSAGE = Tool(
    name="take_message",
    arguments=TakeMessageArguments,
    handler=take_message,
    model_callable=True,
    phi=False,
    phases=frozenset({Phase.SERVING}),
    closed_phases=frozenset({Phase.INTAKE}),
    writes=True,
    may_verify=False,
    takes_identity_claim=False,
    audit_action=Action.MESSAGE_TAKEN,
    outcome_actions={Action.MESSAGE_PREVIEWED: Outcome.SUCCESS},
    resource_type=ResourceType.CALL,
)

TRANSFER_TO_STAFF = Tool(
    name="transfer_to_staff",
    arguments=TransferArguments,
    handler=transfer_to_staff,
    model_callable=True,
    phi=False,
    phases=TOOL_PHASES,
    closed_phases=frozenset(),
    writes=True,
    may_verify=False,
    takes_identity_claim=False,
    audit_action=Action.TRANSFER_REQUESTED,
    outcome_actions={Action.TRANSFER_PREVIEWED: Outcome.SUCCESS},
    resource_type=ResourceType.CALL,
)

REPORT_EMERGENCY = Tool(
    name="report_emergency",
    arguments=NoArguments,
    handler=report_emergency,
    model_callable=True,
    phi=False,
    phases=TOOL_PHASES,
    closed_phases=frozenset({Phase.INTAKE}),
    writes=False,
    may_verify=False,
    takes_identity_claim=False,
    audit_action=Action.EMERGENCY_DETECTED,
    outcome_actions={},
    resource_type=ResourceType.CALL,
)

CLOSE_CALL = Tool(
    name="close_call",
    arguments=NoArguments,
    handler=close_call,
    model_callable=True,
    phi=False,
    phases=TOOL_PHASES,
    closed_phases=frozenset({Phase.INTAKE}),
    writes=False,
    may_verify=False,
    takes_identity_claim=False,
    audit_action=Action.CALL_CLOSED,
    outcome_actions={},
    resource_type=ResourceType.CALL,
)

ENDING_TOOLS = (TAKE_MESSAGE, TRANSFER_TO_STAFF, REPORT_EMERGENCY, CLOSE_CALL)


async def _record(state: CallState, action: Action, reason: Reason | None, deps: Deps) -> None:
    """A call event, in a transaction of its own, within RECORD_DEADLINE_SECONDS. If it cannot
    be written the failure is logged and nothing else changes."""
    try:
        async with asyncio.timeout(RECORD_DEADLINE_SECONDS), deps.sessions() as session:
            await deps.audit.append(
                session,
                Event(
                    actor=AGENT,
                    action=action,
                    resource_type=ResourceType.CALL,
                    outcome=Outcome.SUCCESS,
                    call_id=state.call_id,
                    resource_id=state.call_id,
                    reason=reason,
                ),
                now=deps.now(),
            )
            await session.commit()
    except Exception as error:
        logger.error(
            "call_audit_failed action=%s call_id=%s error=%s",
            action.value,
            state.call_id,
            type(error).__name__,
        )


async def start_call(call_id: str, deps: Deps) -> tuple[CallState, ScriptId]:
    """The call's first state, with the office open or closed as the call's clock says now, and
    its first line: the greeting, or the closed line."""
    if call_id in _LIVE_CALLS:
        raise ValueError("the call has already started")
    started_at = deps.now()
    state = new_call(call_id=call_id, started_at=started_at, office_open=is_open(started_at))
    _LIVE_CALLS.add(state.call_id)
    logger.info("call_started call_id=%s office_open=%s", state.call_id, state.office_open)
    await _record(state, Action.CALL_STARTED, None, deps)
    return state, ScriptId.GREETING if state.office_open else ScriptId.CLOSED


async def end_call(state: CallState, reason: Reason, deps: Deps) -> CallState:
    """The ended state. A call ends from closing (completed), emergency or handoff (transferred),
    or from any live phase when the caller hangs up, the line goes idle, the call runs out of
    time or the pipeline fails; any other end is refused. An ended state is returned as it is."""
    if not isinstance(reason, Reason) or reason not in END_PHASES:
        raise ValueError("a call ends only for one of the fixed reasons")
    if state.phase is Phase.ENDED:
        return state
    if state.phase not in END_PHASES[reason]:
        raise IllegalTransitionError(f"{state.phase.value} -> ended is refused for {reason.value}")
    ended = move(state, Phase.ENDED)
    if state.call_id in _LIVE_CALLS:
        _LIVE_CALLS.discard(state.call_id)
        await _record(ended, Action.CALL_ENDED, reason, deps)
    return ended


async def emergency(state: CallState, trigger: Reason, deps: Deps) -> tuple[CallState, ScriptId]:
    """The call moved to emergency, and the emergency line, whatever the database does."""
    if not isinstance(trigger, Reason) or trigger not in EMERGENCY_TRIGGERS:
        raise ValueError("an emergency is detected by the guard or the model")
    after = move(state, Phase.EMERGENCY)
    logger.warning("emergency_detected call_id=%s trigger=%s", state.call_id, trigger.value)
    await _record(after, Action.EMERGENCY_DETECTED, trigger, deps)
    return after, ScriptId.EMERGENCY
