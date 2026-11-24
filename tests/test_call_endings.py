"""The four ways a call ends or leaves the agent: a message for the staff, a transfer to them, the
emergency line and the goodbye; and the start and the end of the call. Every expectation is
written by hand from the spec and the seed data (app/db/seed_data.json); the model's calls go
through the application registry's dispatch."""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import anyio
import pytest
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY
from pydantic import SecretStr
from sqlalchemy import Engine, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent import endings
from app.agent.claims import ClaimKeys
from app.agent.endings import (
    MAX_LIVE_CALLS,
    drain_call_events,
    emergency,
    end_call,
    start_call,
)
from app.agent.scripts import ScriptId
from app.agent.state import (
    CallState,
    IdentityClaim,
    IllegalTransitionError,
    Phase,
    claim,
    count_refusal,
    count_tool_error,
    fail_attempt,
    move,
    new_call,
    verify,
)
from app.agent.tool_registry import Deps, Registry, Status, ToolOutcome
from app.agent.tools import call_tool, register_tools
from app.audit.log import CHAIN_LOCK, Action, AuditLog, Event, Outcome, Reason, ResourceType
from app.config import StartupSettings
from app.db.seed import load_seed_data, seed
from app.db.session import create_database_engine, create_session_factory
from app.guard.input import GuardAction, guard_input
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

# Tuesday 24 November 2026: 10:00 and 20:00 in clinic time.
OPEN_AT = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 11, 25, 1, 0, tzinfo=UTC)
SEED_DAY = date(2026, 11, 24)

GREETING = Phase.GREETING
INTAKE = Phase.INTAKE
SERVING = Phase.SERVING
BOOKING = Phase.BOOKING
EMERGENCY = Phase.EMERGENCY
HANDOFF = Phase.HANDOFF
CLOSING = Phase.CLOSING
ENDED = Phase.ENDED
LIVE = [GREETING, INTAKE, SERVING, BOOKING, EMERGENCY, HANDOFF, CLOSING]

MESSAGE = "take_message"
TRANSFER = "transfer_to_staff"
REPORT = "report_emergency"
CLOSE = "close_call"

# What the model is offered, by state, written by hand from the spec.
CLOSED_TOOLS = [MESSAGE, REPORT, CLOSE]
OPEN_INTAKE_TOOLS = ["verify_identity", "start_new_patient", TRANSFER, REPORT, CLOSE]
SERVING_TOOLS = [
    "get_next_appointment",
    "request_refill",
    "get_lab_status",
    "get_balance",
    "send_message_to_doctor",
    "find_slots",
    "book_appointment",
    MESSAGE,
    TRANSFER,
    REPORT,
    CLOSE,
]
# Booking: a new patient's call before its details, and a verified caller's.
NEW_PATIENT_BOOKING_TOOLS = ["collect_new_patient", TRANSFER, REPORT, CLOSE]
PATIENT_BOOKING_TOOLS = ["find_slots", TRANSFER, REPORT, CLOSE]

HEART_ATTACK = "I think I'm having a heart attack"

REFUSED = ToolOutcome(
    status=Status.REFUSED, message="That is not available right now.", data={}, hand_off=False
)
FAILED = ToolOutcome(
    status=Status.FAILED, message="That could not be done right now.", data={}, hand_off=False
)
DONE = ToolOutcome(status=Status.DONE, message=None, data={}, hand_off=False)
EMERGENCY_LINE = DONE.model_copy(update={"script": ScriptId.EMERGENCY})
GOODBYE = DONE.model_copy(update={"script": ScriptId.GOODBYE})
TRANSFERRING = DONE.model_copy(
    update={"data": {"status": "transferring"}, "script": ScriptId.TRANSFER}
)

EVENTS = text(
    "SELECT action, resource_type, resource_id, outcome, call_id, actor, reason"
    " FROM audit_events ORDER BY seq"
)
MESSAGES = text(
    "SELECT call_id, patient_id, name, callback_number, kind, body, after_hours"
    " FROM messages ORDER BY id"
)
REQUESTS = text("SELECT call_id, patient_id, type, summary FROM requests ORDER BY id")

# A caller's details, synthetic: never in a log line or an audit event.
ANA = "Ana Diaz"
NUMBER_SAID = "(555) 014-2389"
NUMBER = "5550142389"
NOTE = "Please call me about my bill"


@dataclass
class Clock:
    now: datetime = CLOSED_AT

    def __call__(self) -> datetime:
        return self.now


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def start(office_open: bool = True) -> CallState:
    return new_call(
        call_id=new_call_id(),
        started_at=OPEN_AT if office_open else CLOSED_AT,
        office_open=office_open,
    )


def intake(office_open: bool = True) -> CallState:
    return move(start(office_open), INTAKE)


def sarah() -> CallState:
    return verify(intake(), patient_id="P001", first_name="Sarah")


def forced(phase: Phase, office_open: bool) -> CallState:
    """An unverified call in the phase, set past the transitions."""
    return start(office_open).model_copy(update={"phase": phase})


def reach(phase: Phase, office_open: bool) -> CallState:
    """An unverified call in the phase, by the legal moves where there are some."""
    if phase is GREETING:
        return start(office_open)
    if phase is SERVING or (phase is HANDOFF and not office_open):
        return forced(phase, office_open)
    if phase is INTAKE:
        return intake(office_open)
    return move(intake(office_open), phase)


def every_state() -> list[CallState]:
    """Every phase with the office open and closed, unverified, and every phase a verified
    caller can be in."""
    states = [reach(phase, office_open) for phase in Phase for office_open in (False, True)]
    states.append(sarah())
    states += [move(sarah(), phase) for phase in (BOOKING, EMERGENCY, HANDOFF, CLOSING, ENDED)]
    return states


def rows(engine: Engine, query: Any) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(query)]


async def events_of(engine: Engine, state: CallState) -> list[tuple[Any, ...]]:
    """The call's events, once every call event still being written is in."""
    await drain_call_events()
    return [row for row in rows(engine, EVENTS) if row[4] == state.call_id]


def call_event(action: str, state: CallState, reason: str | None = None) -> tuple[Any, ...]:
    return (action, "call", state.call_id, "success", state.call_id, "agent", reason)


def refused_event(state: CallState, name: str | None) -> tuple[Any, ...]:
    return ("tool_refused", "tool", name, "refused", state.call_id, "agent", None)


def names(registry: Registry, state: CallState) -> list[str]:
    return [item.name for item in registry.tools_for(state)]


def message(
    name: object = ANA, number: object = NUMBER_SAID, reason: object = NOTE, **more: Any
) -> dict[str, Any]:
    return {"name": name, "callback_number": number, "reason_short": reason, **more}


def deps_with(runtime: AsyncEngine, clock: Clock, audit: AuditLog | None = None) -> Deps:
    return Deps(
        sessions=create_session_factory(runtime),
        audit=AuditLog(SecretStr(TEST_AUDIT_KEY)) if audit is None else audit,
        now=clock,
        claim_keys=ClaimKeys(SecretStr(TEST_IDENTITY_KEY)),
    )


@pytest.fixture
def registry() -> Registry:
    return register_tools()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def deps(runtime: AsyncEngine, clock: Clock) -> AsyncIterator[Deps]:
    yield deps_with(runtime, clock)
    # No call event is left being written once the test's engine goes.
    await drain_call_events()


@pytest.fixture
async def seeded(owner_session: AsyncSession) -> None:
    """The task's synthetic patients, P001 to P005, as the start-up seed writes them."""
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()


class AuditThatFailsAfterWriting(AuditLog):
    """The real append, then a failure, for a tool call's own event only."""

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        seq = await super().append(session, event, now=now)
        if event.outcome is Outcome.SUCCESS:
            raise RuntimeError("the audit failed after writing")
        return seq


class AuditThatFails(AuditLog):
    """Every append fails before it writes anything, as with the database down."""

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        raise OSError("the database is down")


class AuditThatRefuses(AuditLog):
    """The real append, except for events of one action, which fail before anything is
    written."""

    def __init__(self, key: SecretStr, action: Action) -> None:
        super().__init__(key)
        self.refused = action

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        if event.action is self.refused:
            raise OSError("this event cannot be written")
        return await super().append(session, event, now=now)


class StallingProxy:
    """A relay between the application and the database that can stop passing bytes, both
    ways, on every connection, open or new: a server that has stopped answering while the
    pool's connections stay open. A cancel request, which libpq sends on a connection of its
    own, stalls the same way."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    def stall(self) -> None:
        self._flowing.clear()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        self._writers.append(upstream_writer)
        await asyncio.gather(
            self._relay(reader, upstream_writer), self._relay(upstream_reader, writer)
        )

    async def _relay(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while data := await source.read(65536):
                await self._flowing.wait()
                sink.write(data)
                await sink.drain()
        except ConnectionError:
            pass
        finally:
            sink.close()

    async def close(self) -> None:
        assert self._server is not None
        self._server.close()
        self._flowing.set()
        for writer in self._writers:
            writer.close()
        await self._server.wait_closed()


async def timed[Result](call: Awaitable[Result]) -> tuple[Result, float]:
    """The call's result and how many seconds it took to return."""
    started = time.perf_counter()
    result = await call
    return result, time.perf_counter() - started


# --- R1, R2: the emergency line ----------------------------------------------------------------


async def test_emergency_everywhere(
    registry: Registry, deps: Deps, clock: Clock, owner_engine: Engine
) -> None:
    # The model may report one in every phase that has tools, open or closed, verified or
    # not: intake, serving and booking with the office open, intake with it closed.
    reported = []
    for state in every_state():
        offered = names(registry, state)
        reachable = state.phase in ({INTAKE, SERVING, BOOKING} if state.office_open else {INTAKE})
        assert (REPORT in offered) == reachable, (state.phase, state.office_open)
        if offered:
            assert REPORT in offered, (state.phase, state.office_open)
        if reachable:
            clock.now = OPEN_AT if state.office_open else CLOSED_AT
            after, outcome = await registry.dispatch(state, REPORT, {}, deps)
            assert (after.phase, outcome) == (EMERGENCY, EMERGENCY_LINE)
            reported.append(state)
    assert len(reported) == 6
    for state in reported:
        assert await events_of(owner_engine, state) == [
            call_event("emergency_detected", state, "model")
        ]

    # The input guard's emergency moves every live call, in every phase, open or closed.
    guarded = []
    for phase in LIVE:
        for office_open in (False, True):
            state = reach(phase, office_open)
            decision = guard_input(HEART_ATTACK, state)
            assert decision.action is GuardAction.EMERGENCY
            after, script = await emergency(state, Reason.GUARD, deps)
            assert (after.phase, script) == (EMERGENCY, ScriptId.EMERGENCY)
            assert script is decision.script
            guarded.append(state)
    verified = sarah()
    after, script = await emergency(verified, Reason.GUARD, deps)
    assert (after.phase, after.patient_id, script) == (EMERGENCY, "P001", ScriptId.EMERGENCY)
    for state in [*guarded, verified]:
        assert await events_of(owner_engine, state) == [
            call_event("emergency_detected", state, "guard")
        ]

    # A call that has ended has no line to speak.
    gone = reach(ENDED, True)
    with pytest.raises(IllegalTransitionError):
        await emergency(gone, Reason.GUARD, deps)
    assert await events_of(owner_engine, gone) == []


async def test_emergency_is_terminal(
    registry: Registry, deps: Deps, clock: Clock, owner_engine: Engine
) -> None:
    clock.now = OPEN_AT
    states = [
        (await registry.dispatch(intake(), REPORT, {}, deps))[0],
        (await registry.dispatch(sarah(), REPORT, {}, deps))[0],
        (await emergency(intake(office_open=False), Reason.GUARD, deps))[0],
    ]
    every_tool = [item.name for item in registry.tools]
    for state in states:
        assert state.phase is EMERGENCY
        assert registry.tools_for(state) == []
        # Reported twice: one move; the second report is refused, like every other tool.
        for name in every_tool:
            after, outcome = await registry.dispatch(state, name, {}, deps)
            assert (after.phase, outcome) == (EMERGENCY, REFUSED), name
        for phase in (GREETING, INTAKE, SERVING, BOOKING, HANDOFF, CLOSING):
            with pytest.raises(IllegalTransitionError):
                move(state, phase)
        assert move(state, ENDED).phase is ENDED
    for state in states:
        assert [row[0] for row in await events_of(owner_engine, state)] == [
            "emergency_detected",
            *["tool_refused"] * len(every_tool),
        ]


# --- R3: what the office being closed leaves ---------------------------------------------------


async def test_closed_office_tools(
    registry: Registry, deps: Deps, owner_engine: Engine, seeded: None
) -> None:
    # Closed: exactly these three, and only where there are tools at all.
    assert names(registry, intake(office_open=False)) == CLOSED_TOOLS
    for state in every_state():
        if not state.office_open:
            assert names(registry, state) in ([], CLOSED_TOOLS), state.phase
            expected = CLOSED_TOOLS if state.phase is INTAKE else []
            assert names(registry, state) == expected, state.phase

    # Against the open office, phase by phase.
    assert names(registry, intake()) == OPEN_INTAKE_TOOLS
    assert names(registry, sarah()) == SERVING_TOOLS
    assert names(registry, move(intake(), BOOKING)) == NEW_PATIENT_BOOKING_TOOLS
    assert names(registry, move(sarah(), BOOKING)) == PATIENT_BOOKING_TOOLS
    for phase in (GREETING, EMERGENCY, HANDOFF, CLOSING, ENDED):
        assert names(registry, reach(phase, True)) == [], phase

    # Nothing else runs closed, whoever asks: identity, patient data and the transfer.
    closed = intake(office_open=False)
    for name in ("verify_identity", "get_balance", "send_message_to_doctor", TRANSFER):
        arguments = {"full_name": "Sarah Johnson", "dob": "1985-03-14"}
        after, outcome = await registry.dispatch(
            closed, name, arguments if name == "verify_identity" else {}, deps
        )
        assert (after.phase, outcome) == (INTAKE, REFUSED), name
    assert [row[0] for row in await events_of(owner_engine, closed)] == ["tool_refused"] * 4


# --- R4 to R6: the message ---------------------------------------------------------------------


@pytest.fixture
def statements(runtime: AsyncEngine) -> Iterator[list[str]]:
    """Every statement the application's engine sends, as SQL text."""
    sent: list[str] = []

    def capture(*args: Any) -> None:
        sent.append(args[2])

    event.listen(runtime.sync_engine, "before_cursor_execute", capture)
    try:
        yield sent
    finally:
        event.remove(runtime.sync_engine, "before_cursor_execute", capture)


async def test_message_never_looks_up_a_patient(
    registry: Registry,
    deps: Deps,
    clock: Clock,
    seeded: None,
    owner_engine: Engine,
    statements: list[str],
) -> None:
    # A caller after hours who gives a patient's own name and number: still nobody's message.
    closed = intake(office_open=False)
    said = message("Sarah Johnson", "555-201-3344", "About my lab results")
    for confirmed in (False, True):
        _after, outcome = await registry.dispatch(
            closed, MESSAGE, {**said, "confirmed": confirmed}, deps
        )
        assert outcome.status is Status.DONE
    # And a verified caller in office hours: the patient id comes from the state.
    clock.now = OPEN_AT
    verified = sarah()
    _after, outcome = await registry.dispatch(
        verified, MESSAGE, message("Sarah Johnson", confirmed=True), deps
    )
    assert outcome.status is Status.DONE

    assert any(re.search(r"\bINSERT INTO messages\b", sql) for sql in statements)
    assert any(re.search(r"\bINSERT INTO requests\b", sql) for sql in statements)
    assert [sql for sql in statements if re.search(r"\bpatients\b", sql, re.IGNORECASE)] == []
    assert [row[1] for row in rows(owner_engine, MESSAGES)] == [None, "P001"]


async def test_message_rules(
    registry: Registry, deps: Deps, clock: Clock, seeded: None, owner_engine: Engine
) -> None:
    closed = intake(office_open=False)
    # The model is told the bounds.
    (described,) = [item for item in registry.tools_for(closed) if item.name == MESSAGE]
    assert described.parameters["properties"] == {
        "name": {"maxLength": 80, "minLength": 1, "title": "Name", "type": "string"},
        "callback_number": {"title": "Callback Number", "type": "string"},
        "reason_short": {
            "maxLength": 120,
            "minLength": 1,
            "title": "Reason Short",
            "type": "string",
        },
        "confirmed": {"default": False, "title": "Confirmed", "type": "boolean"},
    }
    read_back = {
        "status": "needs_confirmation",
        "name": ANA,
        "callback_number": NUMBER,
        "reason_short": NOTE,
    }
    # Without a JSON true, nothing is written and the details are read back.
    for arguments in (message(), message(confirmed=False)):
        after, outcome = await registry.dispatch(closed, MESSAGE, arguments, deps)
        assert (after, outcome) == (closed, DONE.model_copy(update={"data": read_back}))
    for value in ("true", "yes", 1, None):
        _after, outcome = await registry.dispatch(closed, MESSAGE, message(confirmed=value), deps)
        assert outcome == REFUSED, value

    # Each bound, refused whether confirmed or not.
    ssn_shaped = "-".join(["9" * 3, "8" * 2, "7" * 4])
    nine_digits = "9" * 3 + "8" * 2 + "7" * 4
    # 120 and 119 characters said, 122 and 121 once the nine digits are masked; and a name of
    # 79 that becomes 81.
    grows = "x" * 110 + " " + nine_digits
    grows_by_one = "x" * 109 + " " + nine_digits
    name_grows = "n" * 69 + " " + nine_digits
    assert (len(grows), len(grows_by_one), len(name_grows)) == (120, 119, 79)
    refused = [
        message(name=""),
        message(name="   "),
        message(name="n" * 81),
        message(name=name_grows),
        message(name="Ana\x00Diaz"),
        message(name="Ana\nDiaz"),
        # Format characters: zero-width, and the bidi overrides and isolates.
        message(name="Ana​Diaz"),
        message(name="‮Ana Diaz"),
        message(name="Ana﻿"),
        message(name=12),
        # Seven digits: the model asks for the area code.
        message(number="555-2013"),
        message(number="555 014 238"),
        message(number="555-014-23890"),
        message(number="1 555 014 2389"),
        message(number="+1 555 014 2389"),
        message(number="555-014-238x"),
        message(number="555/014/2389"),
        message(number="５５５０１４２３８９"),
        message(number=5550142389),
        message(number=""),
        message(reason=""),
        message(reason="  \t "),
        message(reason="r" * 121),
        message(reason=grows),
        message(reason=grows_by_one),
        message(reason="call\x07me"),
        message(reason="call\x85me"),
        message(reason="call me⁦ back"),
        message(reason="call me‍ back"),
    ]
    for arguments in refused:
        for confirmed in (False, True):
            _after, outcome = await registry.dispatch(
                closed, MESSAGE, {**arguments, "confirmed": confirmed}, deps
            )
            assert outcome == REFUSED, arguments
    assert rows(owner_engine, MESSAGES) == []
    assert rows(owner_engine, REQUESTS) == []

    # At each bound, and as stored: trimmed, separators gone, SSNs masked.
    accepted = [
        (message(name="n" * 80), {"name": "n" * 80}),
        (message(name="  Ana Diaz  "), {}),
        (message(reason="r" * 120), {"reason_short": "r" * 120}),
        (
            message(reason=f" my number is {ssn_shaped} "),
            {"reason_short": "my number is ***-**-****"},
        ),
        (message(name=f"Ana {ssn_shaped}"), {"name": "Ana ***-**-****"}),
        (message(number="555-014-2389"), {}),
        (message(number="555.014.2389"), {}),
        (message(number=" 555 014 2389 "), {}),
        (message(number="5550142389"), {}),
    ]
    for arguments, changed in accepted:
        _after, outcome = await registry.dispatch(closed, MESSAGE, arguments, deps)
        assert outcome == DONE.model_copy(update={"data": {**read_back, **changed}}), arguments
    assert rows(owner_engine, MESSAGES) == []

    # Confirmed after hours: the message and its request, and the call closes.
    after, outcome = await registry.dispatch(closed, MESSAGE, message(confirmed=True), deps)
    assert after.phase is CLOSING
    assert outcome == DONE.model_copy(
        update={
            "data": {"status": "recorded"},
            "script": ScriptId.AFTER_HOURS_CLOSE,
            "script_values": {"name": ANA, "part_of_day": "night"},
        }
    )
    assert registry.tools_for(after) == []
    # A repeat in the same call writes nothing.
    again, outcome = await registry.dispatch(closed, MESSAGE, message(confirmed=True), deps)
    assert (again.phase, outcome.data) == (CLOSING, {"status": "recorded"})

    # A verified caller in office hours: linked to their patient id, and the call goes on.
    clock.now = OPEN_AT
    verified = sarah()
    after, outcome = await registry.dispatch(
        verified,
        MESSAGE,
        message("Sarah Johnson", "555-201-3344", "Lab question", confirmed=True),
        deps,
    )
    assert (after, outcome) == (verified, DONE.model_copy(update={"data": {"status": "recorded"}}))

    assert rows(owner_engine, MESSAGES) == [
        (closed.call_id, None, ANA, NUMBER, "callback", NOTE, True),
        (
            verified.call_id,
            "P001",
            "Sarah Johnson",
            "5552013344",
            "callback",
            "Lab question",
            False,
        ),
    ]
    assert rows(owner_engine, REQUESTS) == [
        (closed.call_id, None, "callback", NOTE),
        (verified.call_id, "P001", "callback", "Lab question"),
    ]
    assert [
        row[0] for row in await events_of(owner_engine, closed) if row[0] != "tool_refused"
    ] == [
        "message_previewed",
        "message_previewed",
        *["message_previewed"] * len(accepted),
        "message_taken",
        "message_taken",
    ]
    assert await events_of(owner_engine, verified) == [call_event("message_taken", verified)]


async def test_message_text_stays_out_of_logs_and_audit(
    registry: Registry,
    deps: Deps,
    seeded: None,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    closed = intake(office_open=False)
    marker = "marker-callback-detail"
    for arguments in (
        message(name=f"{ANA} {marker}", reason=f"{NOTE} {marker}"),
        message(name=f"{ANA} {marker}", number=f"{NUMBER_SAID}9", reason=marker),
        message(name="n" * 81, reason=marker),
        message(name=f"{ANA} {marker}", reason=f"{NOTE} {marker}", confirmed=True),
    ):
        await registry.dispatch(closed, MESSAGE, arguments, deps)
    assert len(rows(owner_engine, MESSAGES)) == 1

    said = (ANA, "Diaz", marker, NOTE, NUMBER, "014-2389", "0142389")
    for row in rows(owner_engine, EVENTS):
        for value in row:
            for text_said in said:
                assert text_said not in str(value), text_said
    for text_said in said:
        assert text_said not in caplog.text, text_said


# --- R7: the transfer --------------------------------------------------------------------------


async def test_transfer_rules(
    registry: Registry, deps: Deps, clock: Clock, seeded: None, owner_engine: Engine
) -> None:
    # Closed: never offered, refused when called, nothing written.
    for state in (intake(office_open=False), move(intake(office_open=False), BOOKING)):
        assert TRANSFER not in names(registry, state)
        after, outcome = await registry.dispatch(
            state, TRANSFER, {"reason": "caller_request", "confirmed": True}, deps
        )
        assert (after.phase, outcome) == (state.phase, REFUSED)
    with pytest.raises(IllegalTransitionError):
        move(intake(office_open=False), HANDOFF)

    clock.now = OPEN_AT
    # Open, not confirmed: the reason read back, nothing written.
    state = intake()
    after, outcome = await registry.dispatch(state, TRANSFER, {"reason": "caller_request"}, deps)
    assert (after, outcome) == (
        state,
        DONE.model_copy(
            update={"data": {"status": "needs_confirmation", "reason": "caller_request"}}
        ),
    )
    assert rows(owner_engine, REQUESTS) == []

    # Confirmed: a request row, the call in handoff, the transfer line, and no tool after.
    transferred = []
    for state, reason in (
        (intake(), "caller_request"),
        (sarah(), "frustration"),
        (move(intake(), BOOKING), "other"),
        # Verification locked, and the caller accepts the transfer the line offers.
        (fail_attempt(fail_attempt(fail_attempt(intake()))), "verification_locked"),
    ):
        after, outcome = await registry.dispatch(
            state, TRANSFER, {"reason": reason, "confirmed": True}, deps
        )
        assert (after.phase, outcome) == (HANDOFF, TRANSFERRING), reason
        assert registry.tools_for(after) == []
        assert await registry.dispatch(
            after, TRANSFER, {"reason": reason, "confirmed": True}, deps
        ) == (
            count_refusal(after),
            REFUSED,
        )
        transferred.append((state, reason))
    assert rows(owner_engine, REQUESTS) == [
        (state.call_id, "P001" if state.verified else None, "transfer", reason)
        for state, reason in transferred
    ]
    for state, reason in transferred:
        assert await events_of(owner_engine, state) == [
            call_event("transfer_requested", state, reason),
            refused_event(state, TRANSFER),
        ]


# --- R8 to R10: the call's start and end -------------------------------------------------------


async def test_end_call_rules(deps: Deps, clock: Clock, owner_engine: Engine) -> None:
    clock.now = OPEN_AT

    async def started(*phases: Phase) -> CallState:
        state, _script = await start_call(new_call_id(), deps)
        for phase in phases:
            state = move(state, phase)
        return state

    async def ended_events(state: CallState) -> list[tuple[Any, ...]]:
        return [row for row in await events_of(owner_engine, state) if row[0] == "call_ended"]

    # Each reason from the phases it ends a call from.
    for path, reason in (
        ([INTAKE, CLOSING], Reason.COMPLETED),
        ([INTAKE, EMERGENCY], Reason.EMERGENCY),
        ([INTAKE, HANDOFF], Reason.TRANSFERRED),
        # The caller hangs up during the 911 line.
        ([INTAKE, EMERGENCY], Reason.CALLER_HUNG_UP),
        ([], Reason.CALLER_HUNG_UP),
        ([INTAKE], Reason.CALLER_HUNG_UP),
        ([INTAKE, BOOKING], Reason.CALLER_HUNG_UP),
        ([INTAKE, HANDOFF], Reason.CALLER_HUNG_UP),
        ([INTAKE, CLOSING], Reason.CALLER_HUNG_UP),
        ([], Reason.IDLE_TIMEOUT),
        ([INTAKE, BOOKING], Reason.MAX_DURATION),
        ([INTAKE], Reason.ERROR),
    ):
        state = await started(*path)
        ended = await end_call(state, reason, deps)
        assert ended.phase is ENDED, (path, reason)
        assert await ended_events(state) == [call_event("call_ended", state, reason.value)]
        # Twice, with the ended state or with the one before it: one event.
        assert await end_call(ended, reason, deps) == ended
        assert await end_call(state, reason, deps) == ended
        assert await ended_events(state) == [call_event("call_ended", state, reason.value)]

    # And a verified caller's call that the model closed.
    state, _script = await start_call(new_call_id(), deps)
    state = move(verify(move(state, INTAKE), patient_id="P001", first_name="Sarah"), CLOSING)
    assert (await end_call(state, Reason.COMPLETED, deps)).phase is ENDED

    # Refused from a phase it may not end from, and nothing written.
    for path, reason in (
        ([], Reason.COMPLETED),
        ([INTAKE], Reason.COMPLETED),
        ([INTAKE, BOOKING], Reason.COMPLETED),
        ([INTAKE, EMERGENCY], Reason.COMPLETED),
        ([INTAKE, HANDOFF], Reason.COMPLETED),
        ([INTAKE], Reason.EMERGENCY),
        ([INTAKE, CLOSING], Reason.EMERGENCY),
        ([INTAKE, HANDOFF], Reason.EMERGENCY),
        ([], Reason.TRANSFERRED),
        ([INTAKE, CLOSING], Reason.TRANSFERRED),
        ([INTAKE, EMERGENCY], Reason.TRANSFERRED),
    ):
        state = await started(*path)
        with pytest.raises(IllegalTransitionError):
            await end_call(state, reason, deps)
        assert await ended_events(state) == [], (path, reason)

    # Two ends of one call at the same moment: one event.
    state = await started(INTAKE, CLOSING)
    both = await asyncio.gather(
        end_call(state, Reason.COMPLETED, deps), end_call(state, Reason.CALLER_HUNG_UP, deps)
    )
    assert [item.phase for item in both] == [ENDED, ENDED]
    assert await ended_events(state) == [call_event("call_ended", state, "completed")]

    # A state that start_call did not start is ended without an event.
    stray = move(intake(), CLOSING)
    assert (await end_call(stray, Reason.COMPLETED, deps)).phase is ENDED
    assert await events_of(owner_engine, stray) == []


async def test_reasons_are_fixed(
    registry: Registry, deps: Deps, clock: Clock, owner_engine: Engine
) -> None:
    clock.now = OPEN_AT
    state, _script = await start_call(new_call_id(), deps)
    closing = move(move(state, INTAKE), CLOSING)
    for reason in (
        "completed",
        "the caller left",
        Reason.GUARD,
        Reason.MODEL,
        Reason.FRUSTRATION,
        None,
    ):
        with pytest.raises(ValueError, match="fixed reasons"):
            await end_call(closing, reason, deps)  # type: ignore[arg-type]
    for trigger in ("guard", "model", Reason.COMPLETED, Reason.OTHER, None):
        with pytest.raises(ValueError, match="guard or the model"):
            await emergency(closing, trigger, deps)  # type: ignore[arg-type]
    assert [row[0] for row in await events_of(owner_engine, state)] == ["call_started"]

    # The model is offered the transfer's four reasons, and nothing else is taken.
    (described,) = [item for item in registry.tools_for(intake()) if item.name == TRANSFER]
    assert described.parameters["properties"]["reason"] == {
        "enum": ["caller_request", "frustration", "verification_locked", "other"],
        "title": "Reason",
        "type": "string",
    }
    for said in ("my own words", "Caller_Request", "guard", "completed", "", 1, None):
        _after, outcome = await registry.dispatch(
            intake(), TRANSFER, {"reason": said, "confirmed": True}, deps
        )
        assert outcome == REFUSED, said

    # The audit log itself takes only members of its reason registry.
    with pytest.raises(TypeError, match="Reason"):
        Event(
            actor="agent",
            action=Action.EMERGENCY_DETECTED,
            resource_type=ResourceType.CALL,
            outcome=Outcome.SUCCESS,
            reason="guard",  # type: ignore[arg-type]
        )


async def test_start_call(deps: Deps, clock: Clock, owner_engine: Engine) -> None:
    for now, office_open, script in (
        # Monday 23 November 2026, 10:00 in clinic time.
        (datetime(2026, 11, 23, 15, 0, tzinfo=UTC), True, ScriptId.GREETING),
        # Saturday 28 November, 10:00.
        (datetime(2026, 11, 28, 15, 0, tzinfo=UTC), False, ScriptId.CLOSED),
        # Friday 27 November, 17:00 exactly.
        (datetime(2026, 11, 27, 22, 0, tzinfo=UTC), False, ScriptId.CLOSED),
    ):
        clock.now = now
        call_id = new_call_id()
        state, first = await start_call(call_id, deps)
        assert state == new_call(call_id=call_id, started_at=now, office_open=office_open)
        assert first is script
        assert await events_of(owner_engine, state) == [call_event("call_started", state)]

        # The same call is not started twice.
        with pytest.raises(ValueError, match="already"):
            await start_call(call_id, deps)
        assert await events_of(owner_engine, state) == [call_event("call_started", state)]


# --- R11: the automatic hand-off ---------------------------------------------------------------


def refused_four_times(state: CallState) -> CallState:
    for _ in range(4):
        state = count_refusal(state)
    return state


async def test_automatic_handoff(
    registry: Registry,
    deps: Deps,
    runtime: AsyncEngine,
    clock: Clock,
    seeded: None,
    owner_engine: Engine,
) -> None:
    clock.now = OPEN_AT
    # Open: the fifth refusal, or a locked call's fifth: handoff.
    locked = fail_attempt(fail_attempt(fail_attempt(intake())))
    handed = []
    for state, name, reason in (
        (refused_four_times(intake()), "no_such_tool", "other"),
        (refused_four_times(move(intake(), BOOKING)), "get_balance", "other"),
        (refused_four_times(locked), "get_balance", "verification_locked"),
    ):
        after, outcome = await call_tool(state, name, {}, deps)
        assert after.phase is HANDOFF, reason
        assert outcome == TRANSFERRING.model_copy(update={"hand_off": True})
        handed.append((state, reason))
        assert await events_of(owner_engine, state) == [
            refused_event(state, name if name != "no_such_tool" else None),
            call_event("transfer_requested", state, reason),
        ]
    # The second failure: a goodbye whose event cannot be written fails, and the call goes to
    # the staff.
    failing_goodbye = deps_with(
        runtime, clock, AuditThatRefuses(SecretStr(TEST_AUDIT_KEY), Action.CALL_CLOSED)
    )
    state = count_tool_error(sarah())
    after, outcome = await call_tool(state, CLOSE, {}, failing_goodbye)
    assert after.phase is HANDOFF
    assert outcome == TRANSFERRING.model_copy(update={"hand_off": True})
    handed.append((state, "other"))
    assert await events_of(owner_engine, state) == [
        ("tool_failed", "tool", CLOSE, "failure", state.call_id, "agent", None),
        call_event("transfer_requested", state, "other"),
    ]
    assert rows(owner_engine, REQUESTS) == [
        (state.call_id, "P001" if state.verified else None, "transfer", reason)
        for state, reason in handed
    ]

    # Closed: never handoff; the caller is offered the message instead.
    clock.now = CLOSED_AT
    closed = refused_four_times(intake(office_open=False))
    after, outcome = await call_tool(closed, "verify_identity", {}, deps)
    assert after == count_refusal(closed)
    assert outcome == REFUSED.model_copy(update={"hand_off": True, "script": ScriptId.FALLBACK})
    assert await events_of(owner_engine, closed) == [refused_event(closed, "verify_identity")]

    # A call already on its way out is left to go: no second move, no request.
    clock.now = OPEN_AT
    leaving = count_tool_error(count_tool_error(intake()))
    after, outcome = await call_tool(leaving, CLOSE, {}, deps)
    assert (after.phase, outcome) == (CLOSING, GOODBYE.model_copy(update={"hand_off": True}))
    # Without the signal nothing happens beyond the tool itself.
    calm = intake()
    after, outcome = await call_tool(calm, "get_balance", {}, deps)
    assert (after, outcome) == (count_refusal(calm), REFUSED)
    assert len(rows(owner_engine, REQUESTS)) == len(handed)


async def test_hand_off_acts_once(
    registry: Registry, deps: Deps, clock: Clock, owner_engine: Engine
) -> None:
    # Open: the outcome that reaches the limit hands the call off. A call past the limit (its
    # hand-off could not be done) is not handed off again on every later outcome.
    clock.now = OPEN_AT
    past = count_refusal(refused_four_times(intake()))
    after, outcome = await call_tool(past, "no_such_tool", {}, deps)
    assert (after, outcome) == (count_refusal(past), REFUSED.model_copy(update={"hand_off": True}))
    failed_twice = count_tool_error(count_tool_error(intake()))
    after, outcome = await call_tool(failed_twice, "get_balance", {}, deps)
    assert after.phase is INTAKE
    assert outcome == REFUSED.model_copy(update={"hand_off": True})

    # Closed: the message is offered once, not over every later reply.
    clock.now = CLOSED_AT
    closed = refused_four_times(intake(office_open=False))
    first, outcome = await call_tool(closed, "verify_identity", {}, deps)
    assert outcome.script is ScriptId.FALLBACK
    second, outcome = await call_tool(first, "verify_identity", {}, deps)
    assert (second, outcome) == (
        count_refusal(first),
        REFUSED.model_copy(update={"hand_off": True}),
    )
    third, outcome = await call_tool(second, CLOSE, {}, deps)
    assert (third.phase, outcome) == (CLOSING, GOODBYE.model_copy(update={"hand_off": True}))
    assert rows(owner_engine, REQUESTS) == []


# --- R13: one transaction ----------------------------------------------------------------------


async def test_endings_are_one_transaction(
    registry: Registry, runtime: AsyncEngine, clock: Clock, seeded: None, owner_engine: Engine
) -> None:
    failing = deps_with(runtime, clock, AuditThatFailsAfterWriting(SecretStr(TEST_AUDIT_KEY)))
    for office_open, state, name, arguments in (
        (False, intake(office_open=False), MESSAGE, message(confirmed=True)),
        (True, sarah(), MESSAGE, message(confirmed=True)),
        (True, intake(), TRANSFER, {"reason": "caller_request", "confirmed": True}),
        (True, sarah(), REPORT, {}),
        (False, intake(office_open=False), REPORT, {}),
        (True, move(intake(), BOOKING), CLOSE, {}),
        (False, intake(office_open=False), CLOSE, {}),
    ):
        clock.now = OPEN_AT if office_open else CLOSED_AT
        after, outcome = await registry.dispatch(state, name, arguments, failing)
        assert (after, outcome) == (count_tool_error(state), FAILED), name
        assert await events_of(owner_engine, state) == [
            ("tool_failed", "tool", name, "failure", state.call_id, "agent", None)
        ]
    assert rows(owner_engine, MESSAGES) == []
    assert rows(owner_engine, REQUESTS) == []

    # And the code-only hand-off, the same: the second failure asks for it, and it fails too.
    handing = count_tool_error(intake())
    after, outcome = await call_tool(handing, CLOSE, {}, failing)
    assert (after.phase, after.tool_errors) == (INTAKE, 3)
    assert outcome == FAILED.model_copy(update={"hand_off": True})
    assert rows(owner_engine, REQUESTS) == []
    await drain_call_events()


# --- Neighbours --------------------------------------------------------------------------------


async def test_emergency_line_without_the_database(
    registry: Registry,
    runtime: AsyncEngine,
    clock: Clock,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    down = deps_with(runtime, clock, AuditThatFails(SecretStr(TEST_AUDIT_KEY)))
    # From the guard and from the model: the call moves and the line is given; the event's
    # failure is logged, and nothing is counted.
    reported = []
    for office_open in (False, True):
        state = intake(office_open)
        after, script = await emergency(state, Reason.GUARD, down)
        assert (after, script) == (move(state, EMERGENCY), ScriptId.EMERGENCY)
        reported.append(state)
    state = sarah()
    after, outcome = await call_tool(state, REPORT, {}, down)
    assert (after, outcome) == (move(state, EMERGENCY), EMERGENCY_LINE)
    reported.append(state)
    await drain_call_events()
    for state in reported:
        assert (
            f"call_audit_failed action=emergency_detected call_id={state.call_id} error=OSError"
            in caplog.text
        )
    assert rows(owner_engine, EVENTS) == []


async def test_report_emergency_comes_first(
    registry: Registry, deps: Deps, clock: Clock, seeded: None, owner_engine: Engine
) -> None:
    clock.now = OPEN_AT
    # Whatever the model sends with it, the report moves the call and gives the 911 line; it
    # is never a refusal, never counted, and never a hand-off.
    stray: list[object] = [{"confirmed": True}, {"reason": "chest pain"}, None, "", [], {}, 1]
    reported = []
    for arguments in stray:
        state = intake()
        after, outcome = await call_tool(state, REPORT, arguments, deps)
        assert (after, outcome) == (move(state, EMERGENCY), EMERGENCY_LINE), arguments
        reported.append(state)
    for state in (
        # The report that would have been the fifth refusal.
        refused_four_times(intake()),
        # A call already at the failure limit, and one past the refusal limit.
        count_tool_error(count_tool_error(sarah())),
        count_refusal(refused_four_times(move(intake(), BOOKING))),
        intake(office_open=False),
    ):
        after, outcome = await call_tool(state, REPORT, {"confirmed": True}, deps)
        assert (after, outcome) == (move(state, EMERGENCY), EMERGENCY_LINE)
        reported.append(state)
    for state in reported:
        assert await events_of(owner_engine, state) == [
            call_event("emergency_detected", state, "model")
        ]
    assert rows(owner_engine, REQUESTS) == []

    # In a phase without tools it is refused as any tool is.
    for phase in (GREETING, CLOSING, HANDOFF, EMERGENCY):
        state = reach(phase, True)
        after, outcome = await call_tool(state, REPORT, {}, deps)
        assert (after, outcome) == (count_refusal(state), REFUSED), phase
        assert await events_of(owner_engine, state) == [refused_event(state, REPORT)]


# Well inside the ~0.2 s budget of a turn: the line goes out without a database round trip.
RETURN_BOUND_SECONDS = 0.2
# The event's write, its clean-up included, ends within this, however the database behaves.
WRITE_BOUND_SECONDS = 3


async def test_emergency_line_never_waits_on_the_database(
    registry: Registry,
    database: StartupSettings,
    runtime: AsyncEngine,
    deps: Deps,
    clock: Clock,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    clock.now = OPEN_AT
    proxy = StallingProxy(database.postgres_host, database.postgres_port)
    port = await proxy.start()
    engine = create_database_engine(
        database.model_copy(update={"postgres_host": "127.0.0.1", "postgres_port": port})
    )
    try:
        stalled = deps_with(engine, clock)
        # A warm pool: a call started and its event written through the relay.
        warm, _script = await start_call(new_call_id(), stalled)
        assert await events_of(owner_engine, warm) == [call_event("call_started", warm)]

        proxy.stall()
        guarded = intake()
        (after, script), took = await timed(emergency(guarded, Reason.GUARD, stalled))
        assert (after, script) == (move(guarded, EMERGENCY), ScriptId.EMERGENCY)
        assert took < RETURN_BOUND_SECONDS, took
        reported = sarah()
        (after, outcome), took = await timed(call_tool(reported, REPORT, {}, stalled))
        assert (after, outcome) == (move(reported, EMERGENCY), EMERGENCY_LINE)
        assert took < RETURN_BOUND_SECONDS, took
        (started, script), took = await timed(start_call(new_call_id(), stalled))
        assert script is ScriptId.GREETING
        assert took < RETURN_BOUND_SECONDS, took
        ended, took = await timed(end_call(move(warm, INTAKE), Reason.CALLER_HUNG_UP, stalled))
        assert ended.phase is ENDED
        assert took < RETURN_BOUND_SECONDS, took

        # The writes give up within their bound, clean-up included, and say so.
        with anyio.fail_after(WRITE_BOUND_SECONDS):
            _none, drained = await timed(drain_call_events())
        assert drained < WRITE_BOUND_SECONDS, drained
        for state, action in (
            (guarded, "emergency_detected"),
            (reported, "emergency_detected"),
            (started, "call_started"),
            (warm, "call_ended"),
        ):
            assert (
                f"call_audit_failed action={action} call_id={state.call_id} error=TimeoutError"
                in caplog.text
            )
    finally:
        await proxy.close()
        await engine.dispose()

    # The audit chain's lock held by another transaction: the line does not wait for it, and
    # the events are written once it is free.
    async with create_session_factory(runtime)() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
        guarded = intake()
        (after, script), took = await timed(emergency(guarded, Reason.GUARD, deps))
        assert (after.phase, script) == (EMERGENCY, ScriptId.EMERGENCY)
        assert took < RETURN_BOUND_SECONDS, took
        reported = intake(office_open=False)
        (after, outcome), took = await timed(call_tool(reported, REPORT, {}, deps))
        assert (after.phase, outcome) == (EMERGENCY, EMERGENCY_LINE)
        assert took < RETURN_BOUND_SECONDS, took
        await holder.rollback()
    assert await events_of(owner_engine, guarded) == [
        call_event("emergency_detected", guarded, "guard")
    ]
    assert await events_of(owner_engine, reported) == [
        call_event("emergency_detected", reported, "model")
    ]


async def test_live_calls_are_bounded(
    deps: Deps,
    clock: Clock,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Far above any number of calls at once (MAX_CONCURRENT_CALLS is at most 20).
    assert MAX_LIVE_CALLS == 1000
    caplog.set_level(logging.INFO)
    clock.now = OPEN_AT
    monkeypatch.setattr(endings, "MAX_LIVE_CALLS", 3)
    monkeypatch.setattr(endings, "_LIVE_CALLS", {})
    calls = [(await start_call(new_call_id(), deps))[0] for _ in range(4)]
    # The oldest was dropped to make room for the fourth: its end writes nothing.
    assert f"live_call_dropped call_id={calls[0].call_id}" in caplog.text
    for state in calls:
        await end_call(state, Reason.CALLER_HUNG_UP, deps)
    assert await events_of(owner_engine, calls[0]) == [call_event("call_started", calls[0])]
    for state in calls[1:]:
        assert await events_of(owner_engine, state) == [
            call_event("call_started", state),
            call_event("call_ended", state, "caller_hung_up"),
        ]


async def test_corrected_message_is_kept(
    registry: Registry, deps: Deps, clock: Clock, seeded: None, owner_engine: Engine
) -> None:
    clock.now = OPEN_AT
    state = sarah()
    for said in (
        message(confirmed=True),
        # The same again: nothing new.
        message(confirmed=True),
        # The number corrected, then the name: each a message of its own.
        message(number="555-014-2390", confirmed=True),
        message(name="Ana Diaz Ruiz", number="555-014-2390", confirmed=True),
        message(name="Ana Diaz Ruiz", number="555-014-2390", confirmed=True),
    ):
        _after, outcome = await registry.dispatch(state, MESSAGE, said, deps)
        assert outcome == DONE.model_copy(update={"data": {"status": "recorded"}})
    assert rows(owner_engine, MESSAGES) == [
        (state.call_id, "P001", ANA, NUMBER, "callback", NOTE, False),
        (state.call_id, "P001", ANA, "5550142390", "callback", NOTE, False),
        (state.call_id, "P001", "Ana Diaz Ruiz", "5550142390", "callback", NOTE, False),
    ]
    # The request for a call back is one: its reason did not change.
    assert rows(owner_engine, REQUESTS) == [(state.call_id, "P001", "callback", NOTE)]


async def test_close_call_clears_the_ssn_step(
    registry: Registry, deps: Deps, clock: Clock, owner_engine: Engine
) -> None:
    clock.now = OPEN_AT
    armed = claim(intake(), IdentityClaim(full_name="Sarah Johnson", dob=date(1985, 3, 14)))
    assert armed.expecting_ssn4
    after, outcome = await registry.dispatch(armed, CLOSE, {}, deps)
    assert outcome == GOODBYE
    assert after == move(armed, CLOSING)
    assert (after.pending_claim, after.expecting_ssn4, after.ssn_turns, after.pending_ssn4) == (
        None,
        False,
        0,
        None,
    )
    # The digits said after it are no longer collected: the turn passes.
    decision = guard_input("33 44", after)
    assert (decision.action, decision.change, decision.submit) == (GuardAction.PASS, None, False)
    assert await events_of(owner_engine, armed) == [call_event("call_closed", armed)]
