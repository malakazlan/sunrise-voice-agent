import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import anyio
import pytest
from conftest import TEST_AUDIT_KEY
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    create_model,
    model_validator,
)
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent.state import CallState, Move, Phase, move, verify
from app.agent.tool_registry import (
    REGISTRY,
    DeclarationError,
    Deps,
    Handler,
    Registry,
    Status,
    Tool,
    ToolDescription,
    ToolOutcome,
    ToolResult,
    check_declaration,
)
from app.audit.log import CHAIN_LOCK, Action, AuditLog, Event, Outcome, ResourceType
from app.config import StartupSettings
from app.db.session import create_database_engine, create_session_factory
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)

GREETING = Phase.GREETING
INTAKE = Phase.INTAKE
SERVING = Phase.SERVING
BOOKING = Phase.BOOKING
EMERGENCY = Phase.EMERGENCY
HANDOFF = Phase.HANDOFF
CLOSING = Phase.CLOSING
ENDED = Phase.ENDED

# The gate, written by hand from the spec (not imported from the code under test).
NO_TOOL_PHASES = {GREETING, EMERGENCY, HANDOFF, CLOSING, ENDED}
PHI_PHASES = {SERVING, BOOKING}
LIVE_PHASES = {INTAKE, SERVING, BOOKING}

# What the model is told, written by hand.
REFUSED = ToolOutcome(
    status=Status.REFUSED, message="That is not available right now.", data={}, hand_off=False
)
FAILED = ToolOutcome(
    status=Status.FAILED, message="That could not be done right now.", data={}, hand_off=False
)

# Text that must never reach an outcome or a log line.
MARKER = "marker-Sarah-Johnson-detail"

EVENTS = text(
    "SELECT action, resource_type, resource_id, outcome, call_id FROM audit_events ORDER BY seq"
)
INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, phone, lab_results_status, balance_due)"
    " VALUES (:id, 'Test', 'Patient', '1980-01-01', '555-000-0000', 'Pending', 0)"
)
PATIENT_IDS = text("SELECT id FROM patients ORDER BY id")
OWN_ADVISORY_LOCKS = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str
    confirmed: bool = False


Calls = list[tuple[CallState, BaseModel]]


def recording(
    calls: Calls, result: ToolResult | None = None, *, fail: BaseException | None = None
) -> Handler:
    """A handler that records each call, then raises `fail` or returns `result`."""

    async def handler(session: AsyncSession, state: CallState, arguments: Any) -> ToolResult:
        calls.append((state, arguments))
        if fail is not None:
            raise fail
        return ToolResult() if result is None else result

    return handler


def declared(name: str, handler: Handler, **overrides: Any) -> Tool:
    """A valid declaration: no patient data, every phase, no write, audited as tool_called."""
    values: dict[str, Any] = {
        "name": name,
        "arguments": NoArguments,
        "handler": handler,
        "phi": False,
        "phases": frozenset(Phase),
        "office_open_only": False,
        "writes": False,
        "audit_action": Action.TOOL_CALLED,
        "resource_type": ResourceType.TOOL,
    }
    values.update(overrides)
    return Tool(**values)


def phi_tool(name: str, handler: Handler) -> Tool:
    return declared(
        name, handler, phi=True, phases=frozenset(PHI_PHASES), resource_type=ResourceType.PATIENT
    )


def registry_of(*tools: Tool) -> Registry:
    registry = Registry()
    for item in tools:
        registry.register(item)
    return registry


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def start(office_open: bool = True) -> CallState:
    return CallState(call_id=new_call_id(), started_at=T0, office_open=office_open)


def intake(office_open: bool = True) -> CallState:
    return move(start(office_open), INTAKE)


def verified_serving() -> CallState:
    return verify(intake(), patient_id="P001", first_name="Sarah")


def forced(phase: Phase, *, verified: bool, office_open: bool) -> CallState:
    """Any combination, legal or not, set past the transitions."""
    update: dict[str, Any] = {"phase": phase}
    if verified:
        update.update(verified=True, patient_id="P001", patient_first_name="Sarah")
    return start(office_open).model_copy(update=update)


def every_combination(phases: Iterable[Phase] = Phase) -> list[CallState]:
    return [
        forced(phase, verified=verified, office_open=office_open)
        for phase in phases
        for verified in (False, True)
        for office_open in (False, True)
    ]


def names(registry: Registry, state: CallState) -> list[str]:
    return [description.name for description in registry.tools_for(state)]


def events(owner_engine: Engine) -> list[tuple[Any, ...]]:
    with owner_engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS)]


def patients(owner_engine: Engine) -> list[str]:
    with owner_engine.connect() as connection:
        return [row.id for row in connection.execute(PATIENT_IDS)]


def refused_event(state: CallState, name: str | None) -> tuple[Any, ...]:
    return ("tool_refused", "tool", name, "refused", state.call_id)


def failed_event(state: CallState, name: str) -> tuple[Any, ...]:
    return ("tool_failed", "tool", name, "failure", state.call_id)


@pytest.fixture
async def deps(runtime: AsyncEngine) -> AsyncIterator[Deps]:
    yield Deps(
        sessions=create_session_factory(runtime),
        audit=AuditLog(SecretStr(TEST_AUDIT_KEY)),
        now=lambda: T0,
    )


# --- R1 to R3: the gate ---------------------------------------------------------------------


async def test_no_phi_tools_before_verification(deps: Deps, owner_engine: Engine) -> None:
    chart: Calls = []
    info: Calls = []
    registry = registry_of(
        phi_tool("read_chart", recording(chart)), declared("office_info", recording(info))
    )

    unverified = [state for state in every_combination() if not state.verified]
    # And the unverified states a call reaches: intake and a new patient's booking.
    unverified += [intake(), move(intake(), BOOKING)]
    for state in unverified:
        assert "read_chart" not in names(registry, state), state.phase
        after, outcome = await registry.dispatch(state, "read_chart", {}, deps)
        assert (after, outcome) == (state, REFUSED)
    assert chart == []
    assert events(owner_engine) == [refused_event(state, "read_chart") for state in unverified]

    # Once verified, the same tool is advertised and runs.
    state = verified_serving()
    assert names(registry, state) == ["read_chart", "office_info"]
    after, outcome = await registry.dispatch(state, "read_chart", {}, deps)
    assert outcome.status is Status.DONE
    assert after == state
    assert [call[0] for call in chart] == [state]


async def test_no_phi_tools_when_closed(deps: Deps, owner_engine: Engine) -> None:
    chart: Calls = []
    registry = registry_of(phi_tool("read_chart", recording(chart)))

    for phase in Phase:
        state = forced(phase, verified=True, office_open=False)
        assert state.verified is True
        assert names(registry, state) == [], phase
        assert await registry.dispatch(state, "read_chart", {}, deps) == (state, REFUSED)
    assert chart == []
    assert len(events(owner_engine)) == len(Phase)

    # The same forced state with the office open is served.
    state = forced(SERVING, verified=True, office_open=True)
    assert names(registry, state) == ["read_chart"]
    assert (await registry.dispatch(state, "read_chart", {}, deps))[1].status is Status.DONE
    assert len(chart) == 1


async def test_office_open_only_tools_need_the_office_open(
    deps: Deps, owner_engine: Engine
) -> None:
    desk: Calls = []
    registry = registry_of(
        declared(
            "transfer_desk", recording(desk), phases=frozenset(LIVE_PHASES), office_open_only=True
        )
    )
    for state in every_combination(LIVE_PHASES):
        if state.office_open:
            assert names(registry, state) == ["transfer_desk"]
        else:
            assert names(registry, state) == []
            assert await registry.dispatch(state, "transfer_desk", {}, deps) == (state, REFUSED)
    assert desk == []
    assert (await registry.dispatch(intake(), "transfer_desk", {}, deps))[1].status is Status.DONE
    assert len(desk) == 1


async def test_no_tools_in_terminal_phases(deps: Deps, owner_engine: Engine) -> None:
    info: Calls = []
    chart: Calls = []
    registry = registry_of(
        declared("office_info", recording(info)), phi_tool("read_chart", recording(chart))
    )
    states = every_combination(NO_TOOL_PHASES)
    for state in states:
        assert registry.tools_for(state) == [], state.phase
        for name in ("office_info", "read_chart"):
            assert await registry.dispatch(state, name, {}, deps) == (state, REFUSED)
    assert (info, chart) == ([], [])
    assert len(events(owner_engine)) == 2 * len(states)

    # A tool declared for every phase is still offered in intake.
    assert names(registry, intake()) == ["office_info"]


# --- R4 to R6: what dispatch refuses ---------------------------------------------------------


async def test_dispatch_refuses_outside_the_gate(deps: Deps, owner_engine: Engine) -> None:
    parsed: list[object] = []

    class Watched(BaseModel):
        model_config = ConfigDict(extra="forbid")

        topic: str

        @model_validator(mode="before")
        @classmethod
        def record(cls, data: object) -> object:
            parsed.append(data)
            return data

    desk: Calls = []
    chart: Calls = []
    registry = registry_of(
        declared("transfer_desk", recording(desk), arguments=Watched, phases=frozenset({INTAKE})),
        phi_tool("read_chart", recording(chart)),
    )
    outside = [
        (start(), "transfer_desk", {"topic": "billing"}),
        (verified_serving(), "transfer_desk", {"topic": "billing"}),
        (intake(), "read_chart", {}),
    ]
    for state, name, arguments in outside:
        assert await registry.dispatch(state, name, arguments, deps) == (state, REFUSED)
    # Neither the handler nor even the argument model ran.
    assert (desk, chart, parsed) == ([], [], [])
    assert events(owner_engine) == [refused_event(state, name) for state, name, _ in outside]

    state = intake()
    after, outcome = await registry.dispatch(state, "transfer_desk", {"topic": "billing"}, deps)
    assert outcome.status is Status.DONE
    assert parsed == [{"topic": "billing"}]
    assert desk == [(state, Watched(topic="billing"))]


async def test_dispatch_refuses_unknown_tool(
    deps: Deps, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    info: Calls = []
    registry = registry_of(declared("office_info", recording(info)))
    state = intake()
    unknown = [
        "no_such_tool",
        "Office_Info",
        "office_info ",
        "sarah_johnson",
        "-".join(["1" * 3, "2" * 2, "3" * 4]),
        "x" * 100,
        "",
    ]
    for name in unknown:
        assert await registry.dispatch(state, name, {}, deps) == (state, REFUSED)
    assert info == []
    # The model's text is not a tool name, so the events do not record it.
    assert events(owner_engine) == [refused_event(state, None)] * len(unknown)
    for name in unknown:
        if name:
            assert name not in caplog.text


async def test_dispatch_refuses_bad_arguments(
    deps: Deps, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: Calls = []
    registry = registry_of(declared("look_up", recording(calls), arguments=Topic))
    state = intake()
    bad: list[object] = [
        {},
        {"topic": 5},
        {"topic": None},
        {"topic": "hours", "patient_id": "P002"},
        {"topic": "hours", "note": MARKER},
        {MARKER: MARKER},
        f"topic={MARKER}",
        [MARKER],
        None,
    ]
    for arguments in bad:
        after, outcome = await registry.dispatch(state, "look_up", arguments, deps)
        assert (after, outcome) == (state, REFUSED)
        assert MARKER not in outcome.model_dump_json()
    assert calls == []
    assert MARKER not in caplog.text
    assert events(owner_engine) == [refused_event(state, "look_up")] * len(bad)

    after, outcome = await registry.dispatch(state, "look_up", {"topic": "hours"}, deps)
    assert outcome.status is Status.DONE
    assert calls == [(state, Topic(topic="hours"))]


# --- R7 to R10: the transaction, the audit and the outcome ----------------------------------


class AuditThatFailsAfterWriting(AuditLog):
    """The real append, then a failure, for the success event only."""

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        seq = await super().append(session, event, now=now)
        if event.action is Action.TOOL_CALLED:
            raise RuntimeError("the audit failed after writing")
        return seq


def inserting(
    patient_id: str,
    *,
    then: Callable[[AsyncSession], Awaitable[object]] | None = None,
    result: ToolResult | None = None,
    locks_seen: list[int] | None = None,
) -> Handler:
    """A writing handler: it inserts a patient row, then does `then`."""

    async def handler(session: AsyncSession, state: CallState, arguments: Any) -> ToolResult:
        await session.execute(INSERT_PATIENT, {"id": patient_id})
        if locks_seen is not None:
            locks_seen.append((await session.execute(OWN_ADVISORY_LOCKS)).scalar_one())
        if then is not None:
            await then(session)
        return ToolResult(resource_id=patient_id) if result is None else result

    return handler


def writing_tool(handler: Handler) -> Tool:
    return declared(
        "save_note",
        handler,
        arguments=Note,
        phases=frozenset(LIVE_PHASES),
        writes=True,
        resource_type=ResourceType.PATIENT,
    )


NOTE = {"note": "call back", "confirmed": True}


async def raise_error(session: AsyncSession) -> None:
    raise RuntimeError("the handler failed after writing")


async def test_handler_and_audit_commit_together(deps: Deps, owner_engine: Engine) -> None:
    state = intake()

    # Committed together, and the handler ran before the audit took the chain's lock.
    locks_seen: list[int] = []
    registry = registry_of(writing_tool(inserting("P901", locks_seen=locks_seen)))
    after, outcome = await registry.dispatch(state, "save_note", NOTE, deps)
    assert outcome.status is Status.DONE
    assert locks_seen == [0]
    assert patients(owner_engine) == ["P901"]
    assert events(owner_engine) == [("tool_called", "patient", "P901", "success", state.call_id)]

    failing: list[tuple[str, Handler, Deps]] = [
        ("P902", inserting("P902", then=raise_error), deps),
        (
            "P903",
            inserting("P903"),
            Deps(
                sessions=deps.sessions,
                audit=AuditThatFailsAfterWriting(SecretStr(TEST_AUDIT_KEY)),
                now=deps.now,
            ),
        ),
        # A handler may not commit on its own.
        ("P904", inserting("P904", then=lambda session: session.commit()), deps),
        # A transition that is illegal from the current state.
        ("P905", inserting("P905", result=ToolResult(transition=Move(to=ENDED))), deps),
    ]
    for patient_id, handler, these_deps in failing:
        registry = registry_of(writing_tool(handler))
        after, outcome = await registry.dispatch(state, "save_note", NOTE, these_deps)
        assert outcome.status is Status.FAILED, patient_id
        assert after.tool_errors == 1
    # Nothing of the failed calls committed: neither the row nor a success event.
    assert patients(owner_engine) == ["P901"]
    assert events(owner_engine) == [
        ("tool_called", "patient", "P901", "success", state.call_id),
        *[failed_event(state, "save_note")] * len(failing),
    ]


async def test_every_dispatch_is_audited(deps: Deps, owner_engine: Engine) -> None:
    async def not_a_result(session: AsyncSession, state: CallState, arguments: Any) -> ToolResult:
        return {"data": {}}  # type: ignore[return-value]

    unused: Calls = []
    registry = registry_of(
        declared("look_up", recording(unused), arguments=Topic),
        declared("breaks", recording(unused, fail=RuntimeError("broken"))),
        declared("bad_move", recording(unused, ToolResult(transition=Move(to=ENDED)))),
        declared("bad_result", not_a_result),
        phi_tool("read_chart", recording(unused)),
    )
    state = intake()
    i = state.call_id
    dispatches: list[tuple[str, object, Status, tuple[Any, ...]]] = [
        ("look_up", {"topic": "hours"}, Status.DONE, ("tool_called", "tool", None, "success", i)),
        ("no_such_tool", {}, Status.REFUSED, refused_event(state, None)),
        ("read_chart", {}, Status.REFUSED, refused_event(state, "read_chart")),
        ("look_up", {}, Status.REFUSED, refused_event(state, "look_up")),
        ("breaks", {}, Status.FAILED, failed_event(state, "breaks")),
        ("bad_move", {}, Status.FAILED, failed_event(state, "bad_move")),
        ("bad_result", {}, Status.FAILED, failed_event(state, "bad_result")),
    ]
    expected: list[tuple[Any, ...]] = []
    for name, arguments, status, event in dispatches:
        _after, outcome = await registry.dispatch(state, name, arguments, deps)
        assert outcome.status is status, name
        expected.append(event)
        assert events(owner_engine) == expected, name

    async with deps.sessions() as session:
        verification = await deps.audit.verify(session)
    assert (verification.ok, verification.checked) == (True, len(dispatches))


async def test_failures_are_generic(
    deps: Deps, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    unused: Calls = []
    registry = registry_of(
        declared("look_up", recording(unused), arguments=Topic),
        declared("breaks", recording(unused, fail=RuntimeError(MARKER))),
        declared("breaks_too", recording(unused, fail=ValueError(f"no row for {MARKER}"))),
        declared("bad_move", recording(unused, ToolResult(transition=Move(to=ENDED)))),
        phi_tool("read_chart", recording(unused)),
    )
    state = intake()
    outcomes = []
    for name, arguments in (("no_such_tool", {}), ("read_chart", {}), ("look_up", {"x": MARKER})):
        after, outcome = await registry.dispatch(state, name, arguments, deps)
        assert outcome == REFUSED
        outcomes.append(outcome)
    for name in ("breaks", "breaks_too", "bad_move"):
        # A fresh state each time, so the hand-off signal stays out of this test.
        after, outcome = await registry.dispatch(intake(), name, {}, deps)
        assert outcome == FAILED
        assert after.tool_errors == 1
        outcomes.append(outcome)
    for outcome in outcomes:
        assert MARKER not in outcome.model_dump_json()
        assert "Error" not in outcome.model_dump_json()
    assert MARKER not in caplog.text


async def test_two_failures_hand_off(deps: Deps, owner_engine: Engine) -> None:
    unused: Calls = []
    registry = registry_of(
        declared("look_up", recording(unused), arguments=Topic),
        declared("breaks", recording(unused, fail=RuntimeError("broken"))),
    )
    state = intake()

    # Refusals are not failures.
    for _ in range(3):
        state, outcome = await registry.dispatch(state, "no_such_tool", {}, deps)
        assert outcome == REFUSED
    assert state.tool_errors == 0

    state, outcome = await registry.dispatch(state, "breaks", {}, deps)
    assert (state.tool_errors, outcome) == (1, FAILED)
    state, outcome = await registry.dispatch(state, "look_up", {"topic": "hours"}, deps)
    assert (state.tool_errors, outcome.status, outcome.hand_off) == (1, Status.DONE, False)
    state, outcome = await registry.dispatch(state, "breaks", {}, deps)
    assert (state.tool_errors, outcome) == (2, FAILED.model_copy(update={"hand_off": True}))
    # From then on every outcome says so.
    state, outcome = await registry.dispatch(state, "no_such_tool", {}, deps)
    assert outcome == REFUSED.model_copy(update={"hand_off": True})
    state, outcome = await registry.dispatch(state, "look_up", {"topic": "hours"}, deps)
    assert (outcome.status, outcome.hand_off) == (Status.DONE, True)


# --- R13, R14: declarations ------------------------------------------------------------------


async def noop(session: AsyncSession, state: CallState, arguments: Any) -> ToolResult:
    return ToolResult()


def valid(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "read_chart",
        "arguments": NoArguments,
        "handler": noop,
        "phi": True,
        "phases": frozenset(PHI_PHASES),
        "office_open_only": True,
        "writes": False,
        "audit_action": Action.TOOL_CALLED,
        "resource_type": ResourceType.PATIENT,
    }
    values.update(overrides)
    return values


def without(field: str) -> dict[str, Any]:
    values = valid()
    del values[field]
    return values


def arguments_with(**fields: Any) -> type[BaseModel]:
    """An argument model that forbids unknown fields, with the given fields, each
    `(annotation, default or ...)`."""
    return create_model("Arguments", __config__=ConfigDict(extra="forbid"), **fields)


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssn: str


class Aliased(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: str = Field(alias="patient_id")


class Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: Owner


class Ignoring(BaseModel):
    topic: str


class Allowing(BaseModel):
    model_config = ConfigDict(extra="allow")

    topic: str


def test_registration_rules() -> None:
    assert check_declaration(Tool(**valid())) == Tool(**valid())

    broken: list[tuple[str, Tool]] = [
        # Every tool states phi, phases, writes and an audit action of the audit registry.
        *[
            ("declare", Tool.model_construct(**without(field)))
            for field in (
                "phi",
                "phases",
                "writes",
                "audit_action",
                "office_open_only",
                "resource_type",
                "arguments",
                "handler",
                "name",
            )
        ],
        ("declare", Tool.model_construct(**valid(phases=frozenset()))),
        ("declare", Tool.model_construct(**valid(phases={SERVING}))),
        ("declare", Tool.model_construct(**valid(phases=frozenset({"serving"})))),
        ("declare", Tool.model_construct(**valid(phi="yes"))),
        ("declare", Tool.model_construct(**valid(writes=0))),
        ("declare", Tool.model_construct(**valid(audit_action="tool_called"))),
        ("declare", Tool.model_construct(**valid(audit_action="tool_exploded"))),
        ("declare", Tool.model_construct(**valid(audit_action=Outcome.SUCCESS))),
        ("declare", Tool.model_construct(**valid(resource_type="patient"))),
        ("declare", Tool.model_construct(**valid(arguments=dict))),
        ("declare", Tool.model_construct(**valid(handler="noop"))),
        # No argument names a patient or a record's owner.
        *[
            ("names a patient", Tool(**valid(arguments=arguments_with(**{field: (str, ...)}))))
            for field in (
                "patient_id",
                "patient",
                "ssn",
                "dob",
                "date_of_birth",
                "ssn_last4",
                "owner",
                "patientId",
                "Patient_ID",
            )
        ],
        ("names a patient", Tool(**valid(arguments=Aliased))),
        ("names a patient", Tool(**valid(arguments=Nested))),
        # A phi tool lists only serving and booking.
        *[
            ("phi", Tool(**valid(phases=frozenset({SERVING, phase}))))
            for phase in (INTAKE, *NO_TOOL_PHASES)
        ],
        # A writing tool's arguments carry `confirmed: bool = False`.
        ("confirmed", Tool(**valid(writes=True))),
        *[
            ("confirmed", Tool(**valid(writes=True, arguments=arguments_with(confirmed=spec))))
            for spec in [
                (bool, True),
                (bool, ...),
                (int, 0),
                (str, "no"),
                (bool | None, False),
                (bool | None, None),
            ]
        ],
        # Argument models forbid unknown fields.
        ("extra", Tool(**valid(arguments=Ignoring))),
        ("extra", Tool(**valid(arguments=Allowing))),
        # The name is the tool's audit id: lower-case letters and underscores, 2 to 40.
        *[
            ("name", Tool.model_construct(**valid(name=name)))
            for name in ("Read_Chart", "read-chart", "r", "x" * 41, "read_chart2", "", "P001")
        ],
    ]
    registry = Registry()
    for rule, declaration in broken:
        with pytest.raises(DeclarationError, match=rule):
            registry.register(declaration)
        with pytest.raises(DeclarationError, match=rule):
            check_declaration(declaration)
    assert [item.name for item in registry.tools] == []

    # The constructor already refuses what it can see.
    for values in (without("phi"), valid(audit_action="tool_called"), valid(phases=frozenset())):
        with pytest.raises(ValidationError):
            Tool(**values)

    # A writing tool with its confirmation, and a second name, are accepted; a name once.
    confirmed = arguments_with(note=(str, ...), confirmed=(bool, False))
    registry.register(Tool(**valid(writes=True, arguments=confirmed)))
    with pytest.raises(DeclarationError, match="already"):
        registry.register(Tool(**valid()))
    registry.register(Tool(**valid(name="office_info", phi=False, phases=frozenset(Phase))))
    assert [item.name for item in registry.tools] == ["read_chart", "office_info"]


def gate_violations(registry: Registry) -> list[str]:
    """Every way the registry's tools break a declaration or the gate, over every phase,
    verification and office state, possible or not."""
    problems = []
    for item in registry.tools:
        try:
            check_declaration(item)
        except DeclarationError as error:
            problems.append(f"{item.name}: {error}")
    by_name = {item.name: item for item in registry.tools}
    for state in every_combination():
        for description in registry.tools_for(state):
            item = by_name[description.name]
            where = (
                f"{item.name} in {state.phase} verified={state.verified} open={state.office_open}"
            )
            if state.phase in NO_TOOL_PHASES:
                problems.append(f"no tools: {where}")
            if state.phase not in item.phases:
                problems.append(f"undeclared phase: {where}")
            if item.phi and not (
                state.verified and state.office_open and state.phase in PHI_PHASES
            ):
                problems.append(f"phi: {where}")
            if item.office_open_only and not state.office_open:
                problems.append(f"office closed: {where}")
    return problems


def test_application_registry_obeys_the_gate() -> None:
    assert gate_violations(REGISTRY) == []

    # The gate offers a tool only in its declared phases, and a patient-data tool cannot be
    # declared for any phase but serving and booking: so no registry, whatever it holds, can
    # offer one anywhere else.
    for phase in set(Phase) - PHI_PHASES:
        with pytest.raises(DeclarationError, match="phi"):
            Registry().register(
                declared(
                    "read_chart",
                    noop,
                    phi=True,
                    phases=frozenset({SERVING, phase}),
                    resource_type=ResourceType.PATIENT,
                )
            )

    # The check finds what it looks for: it passes the test tools, and fails a registry that
    # offers every tool everywhere.
    tools = [
        phi_tool("read_chart", noop),
        declared("office_info", noop),
        declared("transfer_desk", noop, phases=frozenset({INTAKE}), office_open_only=True),
    ]
    assert gate_violations(registry_of(*tools)) == []

    class Ungated(Registry):
        def tools_for(self, state: CallState) -> list[ToolDescription]:
            return [ToolDescription(name=item.name, parameters={}) for item in self.tools]

    ungated = Ungated()
    for item in tools:
        ungated.register(item)
    kinds = {problem.split(":")[0] for problem in gate_violations(ungated)}
    assert kinds == {"no tools", "undeclared phase", "phi", "office closed"}


# --- Neighbours -----------------------------------------------------------------------------


def test_tools_are_described_neutrally() -> None:
    registry = registry_of(declared("look_up", noop, arguments=Topic))
    assert registry.tools_for(intake()) == [
        ToolDescription(
            name="look_up",
            parameters={
                "additionalProperties": False,
                "properties": {"topic": {"title": "Topic", "type": "string"}},
                "required": ["topic"],
                "title": "Topic",
                "type": "object",
            },
        )
    ]
    # What a caller does to a description it was given does not reach the registry.
    registry.tools_for(intake())[0].parameters.clear()
    assert registry.tools_for(intake())[0].parameters["required"] == ["topic"]


async def test_handler_gets_the_patient_from_the_state(deps: Deps) -> None:
    chart: Calls = []
    registry = registry_of(
        phi_tool("read_chart", recording(chart, ToolResult(data={"balance": "0.00"})))
    )
    state = verify(intake(), patient_id="P042", first_name="Ann")
    after, outcome = await registry.dispatch(state, "read_chart", {}, deps)
    assert outcome == ToolOutcome(
        status=Status.DONE, message=None, data={"balance": "0.00"}, hand_off=False
    )
    assert [(call[0].patient_id, call[1]) for call in chart] == [("P042", NoArguments())]
    # The model cannot name another patient: unknown fields are refused.
    assert await registry.dispatch(state, "read_chart", {"patient_id": "P001"}, deps) == (
        state,
        REFUSED,
    )
    assert len(chart) == 1


async def test_emergency_mid_call_leaves_no_tools(deps: Deps) -> None:
    calls: Calls = []
    registry = registry_of(
        declared("raise_alarm", recording(calls, ToolResult(transition=Move(to=EMERGENCY)))),
        phi_tool("read_chart", recording(calls)),
    )
    state, outcome = await registry.dispatch(verified_serving(), "raise_alarm", {}, deps)
    assert (state.phase, outcome.status) == (EMERGENCY, Status.DONE)
    assert registry.tools_for(state) == []
    for name in ("raise_alarm", "read_chart"):
        assert await registry.dispatch(state, name, {}, deps) == (state, REFUSED)
    assert len(calls) == 1

    # Raised by the caller's side between tool calls, the same.
    state = move(verified_serving(), EMERGENCY)
    assert registry.tools_for(state) == []


async def test_held_audit_lock_fails_generically(
    deps: Deps, runtime: AsyncEngine, owner_engine: Engine
) -> None:
    registry = registry_of(writing_tool(inserting("P950")))
    async with create_session_factory(runtime)() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
        after, outcome = await registry.dispatch(intake(), "save_note", NOTE, deps)
        await holder.rollback()
    assert (after.tool_errors, outcome) == (1, FAILED)
    # The failure's own event could not be written either: nothing at all committed.
    assert patients(owner_engine) == []
    assert events(owner_engine) == []


# The outside bound on a dispatch against a database that does not answer: two connection
# attempts (the handler's transaction, then the failure's event) at the 5-second connect
# timeout, with room to spare; the driver's own default would be 130 seconds each.
SILENT_DATABASE_BOUND_SECONDS = 30


async def test_unanswering_database_fails_within_a_bound(
    server_settings: StartupSettings, silent_server: int
) -> None:
    engine = create_database_engine(
        server_settings.model_copy(update={"postgres_port": silent_server})
    )
    try:
        silent = Deps(
            sessions=create_session_factory(engine),
            audit=AuditLog(SecretStr(TEST_AUDIT_KEY)),
            now=lambda: T0,
        )
        calls: Calls = []
        registry = registry_of(declared("look_up", recording(calls), arguments=Topic))
        with anyio.fail_after(SILENT_DATABASE_BOUND_SECONDS):
            state, outcome = await registry.dispatch(intake(), "look_up", {"topic": "x"}, silent)
        assert (state.tool_errors, outcome) == (1, FAILED)
    finally:
        await engine.dispose()


@pytest.fixture
async def hanging_up_server() -> AsyncIterator[int]:
    """The port of a local server that closes every connection at once, as a database server
    that has gone down does. (A refused connection would do as well on Linux, but on Windows
    the driver's asynchronous connect never sees the refusal.)"""

    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


async def test_database_down_fails_generically(
    database: StartupSettings, hanging_up_server: int
) -> None:
    engine = create_database_engine(
        database.model_copy(update={"postgres_port": hanging_up_server})
    )
    try:
        down = Deps(
            sessions=create_session_factory(engine),
            audit=AuditLog(SecretStr(TEST_AUDIT_KEY)),
            now=lambda: T0,
        )
        calls: Calls = []
        registry = registry_of(declared("look_up", recording(calls), arguments=Topic))
        state = intake()
        state, outcome = await registry.dispatch(state, "look_up", {"topic": "hours"}, down)
        assert (state.tool_errors, outcome) == (1, FAILED)
        state, outcome = await registry.dispatch(state, "no_such_tool", {}, down)
        assert (state.tool_errors, outcome) == (1, REFUSED)
        state, outcome = await registry.dispatch(state, "look_up", {"topic": "hours"}, down)
        assert (state.tool_errors, outcome.hand_off) == (2, True)
    finally:
        await engine.dispose()
