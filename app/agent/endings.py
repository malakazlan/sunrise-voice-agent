"""How a call starts, and the four ways it ends or leaves the agent: a message for the staff, a
transfer to them, the emergency line and the goodbye.

The four tools go through dispatch like any other. `take_message` is the only one that writes
caller text, and only once the caller has confirmed it; it never reads a patient record: an
unverified caller's message has no patient id, a verified caller's has the state's. With the
office closed it is all the call can do, besides the emergency line and the goodbye, and a
message closes the call. `transfer_to_staff` needs the office open; the transfer is simulated:
the request row and the event are real, there is no phone system behind them.

`start_call` creates the call, deciding once from the call's clock, or from the office override
staff set (read from memory, app/runtime_config.py), whether the office is open,
and `end_call` is the one way it ends; `emergency` is the one way to the emergency line, for the
input guard and for the model's report alike (`call_tool` in app/agent/tools.py). Each returns
the state and the line before any database work: its event (`call_started`, `call_ended`,
`emergency_detected`) is written by a task of its own that nobody waits for. The write is given
up after RECORD_DEADLINE_SECONDS, its clean-up included, and a write that fails or is given up
is logged; nothing else changes. `drain_call_events` waits for the writes still running, for
tests and for shutdown.

`end_call` writes `call_ended` once per call: for a call `start_call` started in this process,
the first `end_call` that ends it. The calls this process has started and not ended are held
in memory, at most MAX_LIVE_CALLS of them; a failed `call_ended` write is not tried again.

Logs carry event names, call ids, reasons and exception class names; never a name, a number or
message text.
"""

import asyncio
import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.scripts import ScriptId
from app.agent.serving import NEEDS_CONFIRMATION, stored_text, trimmed
from app.agent.state import (
    Caller,
    CallState,
    IllegalTransitionError,
    Move,
    Phase,
    move,
    new_call,
)
from app.agent.tool_registry import Deps, HandlerContext, NoArguments, Tool, ToolResult
from app.audit.log import AGENT, Action, Event, Outcome, Reason, ResourceType
from app.db.models import MAX_MESSAGE_BODY, MESSAGE_KEY, Message, Request
from app.domain.office_hours import part_of_day

logger = logging.getLogger(__name__)

CALLBACK = "callback"
TRANSFER = "transfer"
RECORDED = "recorded"
TRANSFERRING = "transferring"

MAX_CALLER_NAME = 80
CALLBACK_DIGITS = 10
# What a caller may say between the digits of a callback number.
NUMBER_SEPARATORS = frozenset(" -.()")

# A call event's write is given up after this many seconds: it is cancelled, then cancelled
# again every CANCEL_INTERVAL_SECONDS until it has ended. The driver, interrupted a second time
# while it waits on the server, closes the connection, which ends every wait on it.
RECORD_DEADLINE_SECONDS = 2
CANCEL_INTERVAL_SECONDS = 0.1

# The calls held as started and not ended: far above any number of calls at once
# (MAX_CONCURRENT_CALLS is at most 20). Past it the oldest is forgotten.
MAX_LIVE_CALLS = 1000

# Every kind of caller: the emergency line, the goodbye and the transfer are for everyone.
EVERY_CALLER = frozenset(Caller)
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
TRANSFER_REASONS = frozenset(
    {Reason.CALLER_REQUEST, Reason.FRUSTRATION, Reason.VERIFICATION_LOCKED, Reason.OTHER}
)
HAND_OFF_REASONS = frozenset({Reason.VERIFICATION_LOCKED, Reason.OTHER})

# Started and not ended, oldest first.
_LIVE_CALLS: dict[str, None] = {}
# The call event writes still running, and what bounds them: held until each has ended.
_PENDING: set[asyncio.Future[None]] = set()
# Each call's latest event write still running: the next one waits for it, so a call's events
# are written in the order they happened.
_LATEST: dict[str, asyncio.Future[None]] = {}


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
        return trimmed(value)

    @field_validator("name")
    @classmethod
    def _name_as_stored(cls, value: str) -> str:
        return stored_text(value, MAX_CALLER_NAME)

    @field_validator("reason_short")
    @classmethod
    def _reason_as_stored(cls, value: str) -> str:
        return stored_text(value, MAX_MESSAGE_BODY)

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
        .on_conflict_do_nothing(index_elements=MESSAGE_KEY)
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
    takes_date_of_birth=None,
    callers=frozenset({Caller.UNIDENTIFIED, Caller.PATIENT}),
    audit_action=Action.MESSAGE_TAKEN,
    outcome_actions={Action.MESSAGE_PREVIEWED: Outcome.SUCCESS},
    audit_reasons=frozenset(),
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
    takes_date_of_birth=None,
    callers=EVERY_CALLER,
    audit_action=Action.TRANSFER_REQUESTED,
    outcome_actions={Action.TRANSFER_PREVIEWED: Outcome.SUCCESS},
    audit_reasons=TRANSFER_REASONS,
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
    takes_date_of_birth=None,
    callers=EVERY_CALLER,
    audit_action=Action.EMERGENCY_DETECTED,
    outcome_actions={},
    audit_reasons=frozenset({Reason.MODEL}),
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
    takes_date_of_birth=None,
    callers=EVERY_CALLER,
    audit_action=Action.CALL_CLOSED,
    outcome_actions={},
    audit_reasons=frozenset(),
    resource_type=ResourceType.CALL,
)

ENDING_TOOLS = (TAKE_MESSAGE, TRANSFER_TO_STAFF, REPORT_EMERGENCY, CLOSE_CALL)


def _failed(action: Action, call_id: str, error: BaseException) -> None:
    logger.error(
        "call_audit_failed action=%s call_id=%s error=%s",
        action.value,
        call_id,
        type(error).__name__,
    )


def _held(task: "asyncio.Future[None]") -> None:
    """Hold the task until it has ended."""
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def _write(
    event: Event, now: datetime, deps: Deps, before: "asyncio.Future[None] | None"
) -> None:
    if before is not None:
        await asyncio.wait({before})
    async with deps.sessions() as session:
        await deps.audit.append(session, event, now=now)
        await session.commit()


async def _bound(write: "asyncio.Future[None]", action: Action, call_id: str) -> None:
    """Give the write up after RECORD_DEADLINE_SECONDS and see it end; log how it failed."""
    done, _pending = await asyncio.wait({write}, timeout=RECORD_DEADLINE_SECONDS)
    given_up = not done
    while not done:
        write.cancel()
        done, _pending = await asyncio.wait({write}, timeout=CANCEL_INTERVAL_SECONDS)
    error = asyncio.CancelledError() if write.cancelled() else write.exception()
    if given_up:
        error = TimeoutError()
    if error is not None:
        _failed(action, call_id, error)


def _record_later(state: CallState, action: Action, reason: Reason | None, deps: Deps) -> None:
    """Write the call's event by a task of its own; the caller does not wait for it. Nothing
    here raises: a failure is logged."""
    try:
        event = Event(
            actor=AGENT,
            action=action,
            resource_type=ResourceType.CALL,
            outcome=Outcome.SUCCESS,
            call_id=state.call_id,
            resource_id=state.call_id,
            reason=reason,
        )
        now = deps.now()
    except Exception as error:
        _failed(action, state.call_id, error)
        return
    call_id = state.call_id
    write = asyncio.ensure_future(_write(event, now, deps, _LATEST.get(call_id)))
    _LATEST[call_id] = write

    def forget(_ended: "asyncio.Future[None]") -> None:
        if _LATEST.get(call_id) is write:
            del _LATEST[call_id]

    write.add_done_callback(forget)
    _held(write)
    _held(asyncio.ensure_future(_bound(write, action, call_id)))


async def drain_call_events() -> None:
    """Wait until every call event write still running has been written or given up: for tests,
    and for shutdown."""
    while _PENDING:
        await asyncio.wait(set(_PENDING))


async def start_call(call_id: str, deps: Deps) -> tuple[CallState, ScriptId]:
    """The call's first state, with the office open or closed as the call's clock says now or
    as staff forced it (the config in memory, never the database), and its first line: the
    greeting, or the closed line. The call keeps that answer to its end."""
    if call_id in _LIVE_CALLS:
        raise ValueError("the call has already started")
    started_at = deps.now()
    state = new_call(
        call_id=call_id, started_at=started_at, office_open=deps.config.office_open(started_at)
    )
    if len(_LIVE_CALLS) >= MAX_LIVE_CALLS:
        oldest = next(iter(_LIVE_CALLS))
        del _LIVE_CALLS[oldest]
        logger.warning("live_call_dropped call_id=%s", oldest)
    _LIVE_CALLS[state.call_id] = None
    logger.info("call_started call_id=%s office_open=%s", state.call_id, state.office_open)
    _record_later(state, Action.CALL_STARTED, None, deps)
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
        del _LIVE_CALLS[state.call_id]
        _record_later(ended, Action.CALL_ENDED, reason, deps)
    return ended


async def emergency(state: CallState, trigger: Reason, deps: Deps) -> tuple[CallState, ScriptId]:
    """The call moved to emergency, and the emergency line, before any database work."""
    if not isinstance(trigger, Reason) or trigger not in EMERGENCY_TRIGGERS:
        raise ValueError("an emergency is detected by the guard or the model")
    after = move(state, Phase.EMERGENCY)
    logger.warning("emergency_detected call_id=%s trigger=%s", state.call_id, trigger.value)
    _record_later(after, Action.EMERGENCY_DETECTED, trigger, deps)
    return after, ScriptId.EMERGENCY
