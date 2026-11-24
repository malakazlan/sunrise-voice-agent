import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

import anyio
import pytest
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY, import_application_modules
from pydantic import (
    AliasChoices,
    AliasPath,
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

from app.agent import tool_registry
from app.agent.claims import ClaimKeys
from app.agent.scripts import ScriptId
from app.agent.state import (
    Caller,
    CallState,
    IdentityClaim,
    Move,
    NewPatientDetails,
    Phase,
    Verify,
    count_tool_error,
    move,
    new_call,
    verify,
)
from app.agent.tool_registry import (
    REGISTRY,
    DeclarationError,
    Deps,
    DobKind,
    Grant,
    Handler,
    HandlerContext,
    Registry,
    Status,
    Tool,
    ToolDescription,
    ToolOutcome,
    ToolResult,
    check_declaration,
    is_available,
)
from app.agent.tools import register_tools
from app.audit.log import CHAIN_LOCK, Action, AuditLog, Event, Outcome, Reason, ResourceType
from app.config import StartupSettings
from app.db.session import create_database_engine, create_session_factory
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
CLAIM_KEYS = ClaimKeys(SecretStr(TEST_IDENTITY_KEY))

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

    async def handler(
        session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
    ) -> ToolResult:
        calls.append((state, arguments))
        if fail is not None:
            raise fail
        return ToolResult() if result is None else result

    return handler


def declared(name: str, handler: Handler, **overrides: Any) -> Tool:
    """A valid declaration: callable by the model, no patient data, every phase, the same with
    the office open or closed unless its closed phases are given, no write, audited as
    tool_called."""
    values: dict[str, Any] = {
        "name": name,
        "arguments": NoArguments,
        "handler": handler,
        "model_callable": True,
        "phi": False,
        "phases": frozenset(Phase),
        "writes": False,
        "may_verify": False,
        "takes_date_of_birth": None,
        "callers": frozenset(Caller),
        "audit_action": Action.TOOL_CALLED,
        "outcome_actions": {},
        "audit_reasons": frozenset(),
        "resource_type": ResourceType.TOOL,
    }
    values.update(overrides)
    values.setdefault("closed_phases", values["phases"])
    return Tool(**values)


def phi_tool(name: str, handler: Handler) -> Tool:
    return declared(
        name,
        handler,
        phi=True,
        phases=frozenset(PHI_PHASES),
        closed_phases=frozenset(),
        resource_type=ResourceType.PATIENT,
    )


def registry_of(*tools: Tool) -> Registry:
    """A registry of the tools; a code-only tool gets a grant of its own, unused here."""
    registry = Registry()
    for item in tools:
        registry.register(item, grant=None if item.model_callable else Grant())
    return registry


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def start(office_open: bool = True) -> CallState:
    return new_call(call_id=new_call_id(), started_at=T0, office_open=office_open)


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


def after_refusal(state: CallState) -> CallState:
    """The state a refusal leaves: one more refusal counted, nothing else changed."""
    return state.model_copy(update={"tool_refusals": state.tool_refusals + 1})


def called_event(state: CallState, name: str) -> tuple[Any, ...]:
    """A tool's own event, for a tool whose resource type is `tool`: the id is its name."""
    return ("tool_called", "tool", name, "success", state.call_id)


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
        claim_keys=CLAIM_KEYS,
    )


# --- R1 to R3: the gate ---------------------------------------------------------------------


async def test_no_phi_tools_before_verification(deps: Deps, owner_engine: Engine) -> None:
    chart: Calls = []
    info: Calls = []
    registry = registry_of(
        phi_tool("read_chart", recording(chart)), declared("office_info", recording(info))
    )

    unverified = [state for state in every_combination() if not state.verified]
    # And the unverified states a call reaches: intake and a new patient's booking. And a
    # state with the verified flag forced on but no patient id: the gate wants both.
    flag_only = forced(SERVING, verified=True, office_open=True).model_copy(
        update={"patient_id": None}
    )
    unverified += [intake(), move(intake(), BOOKING), flag_only]
    for state in unverified:
        assert "read_chart" not in names(registry, state), state.phase
        after, outcome = await registry.dispatch(state, "read_chart", {}, deps)
        assert (after, outcome) == (after_refusal(state), REFUSED)
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
        assert await registry.dispatch(state, "read_chart", {}, deps) == (
            after_refusal(state),
            REFUSED,
        )
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
            "transfer_desk",
            recording(desk),
            phases=frozenset(LIVE_PHASES),
            closed_phases=frozenset(),
        )
    )
    for state in every_combination(LIVE_PHASES):
        if state.office_open:
            assert names(registry, state) == ["transfer_desk"]
        else:
            assert names(registry, state) == []
            assert await registry.dispatch(state, "transfer_desk", {}, deps) == (
                after_refusal(state),
                REFUSED,
            )
    assert desk == []
    assert (await registry.dispatch(intake(), "transfer_desk", {}, deps))[1].status is Status.DONE
    assert len(desk) == 1


async def test_closed_office_has_phases_of_its_own(deps: Deps, owner_engine: Engine) -> None:
    # A tool declared for serving with the office open and for intake with it closed.
    notes: Calls = []
    registry = registry_of(
        declared(
            "leave_note",
            recording(notes),
            phases=frozenset({SERVING}),
            closed_phases=frozenset({INTAKE}),
        )
    )
    offered_in = {
        (state.phase, state.office_open)
        for state in every_combination()
        if names(registry, state) == ["leave_note"]
    }
    assert offered_in == {(SERVING, True), (INTAKE, False)}
    for state in (intake(), forced(SERVING, verified=False, office_open=False)):
        assert await registry.dispatch(state, "leave_note", {}, deps) == (
            after_refusal(state),
            REFUSED,
        )
    assert notes == []
    for state in (intake(office_open=False), verified_serving()):
        assert (await registry.dispatch(state, "leave_note", {}, deps))[1].status is Status.DONE
    assert len(notes) == 2


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
            assert await registry.dispatch(state, name, {}, deps) == (after_refusal(state), REFUSED)
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
        assert await registry.dispatch(state, name, arguments, deps) == (
            after_refusal(state),
            REFUSED,
        )
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
    unknown: list[object] = [
        "no_such_tool",
        "Office_Info",
        "office_info ",
        "sarah_johnson",
        "-".join(["1" * 3, "2" * 2, "3" * 4]),
        "x" * 100,
        "",
        # Not text at all.
        5,
        None,
        ["office_info"],
        {"name": "office_info"},
        b"office_info",
    ]
    for name in unknown:
        assert await registry.dispatch(state, name, {}, deps) == (after_refusal(state), REFUSED)
    assert info == []
    # The model's text is not a tool name, so the events do not record it.
    assert events(owner_engine) == [refused_event(state, None)] * len(unknown)
    for name in unknown:
        if isinstance(name, str) and name:
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
        assert (after, outcome) == (after_refusal(state), REFUSED)
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

    async def handler(
        session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
    ) -> ToolResult:
        await session.execute(INSERT_PATIENT, {"id": patient_id})
        if locks_seen is not None:
            locks_seen.append((await session.execute(OWN_ADVISORY_LOCKS)).scalar_one())
        if then is not None:
            await then(session)
        return ToolResult() if result is None else result

    return handler


def writing_tool(handler: Handler) -> Tool:
    return declared(
        "save_note",
        handler,
        arguments=Note,
        phases=frozenset(LIVE_PHASES),
        writes=True,
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
    assert events(owner_engine) == [called_event(state, "save_note")]

    failing: list[tuple[str, Handler, Deps]] = [
        ("P902", inserting("P902", then=raise_error), deps),
        (
            "P903",
            inserting("P903"),
            Deps(
                sessions=deps.sessions,
                audit=AuditThatFailsAfterWriting(SecretStr(TEST_AUDIT_KEY)),
                now=deps.now,
                claim_keys=CLAIM_KEYS,
            ),
        ),
        # A handler may not commit on its own.
        ("P904", inserting("P904", then=lambda session: session.commit()), deps),
        # A transition that is illegal from the current state.
        ("P905", inserting("P905", result=ToolResult(transition=Move(to=GREETING))), deps),
    ]
    for patient_id, handler, these_deps in failing:
        registry = registry_of(writing_tool(handler))
        after, outcome = await registry.dispatch(state, "save_note", NOTE, these_deps)
        assert outcome.status is Status.FAILED, patient_id
        assert after.tool_errors == 1
    # Nothing of the failed calls committed: neither the row nor a success event.
    assert patients(owner_engine) == ["P901"]
    assert events(owner_engine) == [
        called_event(state, "save_note"),
        *[failed_event(state, "save_note")] * len(failing),
    ]


async def test_every_dispatch_is_audited(deps: Deps, owner_engine: Engine) -> None:
    async def not_a_result(
        session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
    ) -> ToolResult:
        return {"data": {}}  # type: ignore[return-value]

    unused: Calls = []
    registry = registry_of(
        declared("look_up", recording(unused), arguments=Topic),
        declared("breaks", recording(unused, fail=RuntimeError("broken"))),
        declared("bad_move", recording(unused, ToolResult(transition=Move(to=GREETING)))),
        declared("bad_result", not_a_result),
        phi_tool("read_chart", recording(unused)),
    )
    state = intake()
    dispatches: list[tuple[str, object, Status, tuple[Any, ...]]] = [
        ("look_up", {"topic": "hours"}, Status.DONE, called_event(state, "look_up")),
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
        declared("bad_move", recording(unused, ToolResult(transition=Move(to=GREETING)))),
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


async def noop(
    session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
) -> ToolResult:
    return ToolResult()


def valid(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "read_chart",
        "arguments": NoArguments,
        "handler": noop,
        "model_callable": True,
        "phi": True,
        "phases": frozenset(PHI_PHASES),
        "closed_phases": frozenset(),
        "writes": False,
        "may_verify": False,
        "takes_date_of_birth": None,
        "callers": frozenset(Caller),
        "audit_action": Action.TOOL_CALLED,
        "outcome_actions": {},
        "audit_reasons": frozenset(),
        "resource_type": ResourceType.PATIENT,
    }
    values.update(overrides)
    return values


class ClaimArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    dob: str


class DetailsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    dob: str
    phone: str


# A tool that may verify, as the model would call it; the one that takes a claim; and the one
# that takes a new patient's details.
VERIFIER = valid(name="check_identity", may_verify=True, phi=False, phases=frozenset({INTAKE}))
CLAIMER = valid(
    name="claim_identity",
    arguments=ClaimArguments,
    phi=False,
    phases=frozenset({INTAKE}),
    takes_date_of_birth=DobKind.IDENTITY_CLAIM,
    callers=frozenset({Caller.UNIDENTIFIED, Caller.CLAIMANT, Caller.LOCKED}),
    resource_type=ResourceType.TOOL,
)
NEW_PATIENTS = frozenset({Caller.NEW_PATIENT, Caller.NEW_PATIENT_WITH_DETAILS})
DETAILER = valid(
    name="take_details",
    arguments=DetailsArguments,
    phi=False,
    phases=frozenset({BOOKING}),
    takes_date_of_birth=DobKind.NEW_PATIENT_DETAILS,
    callers=NEW_PATIENTS,
    resource_type=ResourceType.TOOL,
)


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


class ValidationAliased(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: str = Field(validation_alias="patient_id")


class ChoiceAliased(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str = Field(validation_alias=AliasChoices("date", "dob"))


class PathAliased(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(validation_alias=AliasPath("record", "ssn"))


class Loose(BaseModel):
    code: str


class HoldsLoose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inner: Loose


class HoldsLooseList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inners: list[Loose] | None = None


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
                "closed_phases",
                "may_verify",
                "resource_type",
                "arguments",
                "handler",
                "name",
                "model_callable",
                "takes_date_of_birth",
                "callers",
                "outcome_actions",
                "audit_reasons",
            )
        ],
        ("declare", Tool.model_construct(**valid(audit_reasons={Reason.OTHER}))),
        ("declare", Tool.model_construct(**valid(audit_reasons=frozenset({"other"})))),
        ("declare", Tool.model_construct(**valid(callers=frozenset()))),
        ("declare", Tool.model_construct(**valid(callers={Caller.PATIENT}))),
        ("declare", Tool.model_construct(**valid(takes_date_of_birth="identity_claim"))),
        ("declare", Tool.model_construct(**valid(model_callable="no"))),
        ("declare", Tool.model_construct(**valid(outcome_actions={"tool_called": "success"}))),
        ("declare", Tool.model_construct(**valid(outcome_actions={Action.TOOL_CALLED: "ok"}))),
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
        ("names a patient", Tool(**valid(arguments=ValidationAliased))),
        ("names a patient", Tool(**valid(arguments=ChoiceAliased))),
        ("names a patient", Tool(**valid(arguments=PathAliased))),
        # A phi tool lists only serving and booking.
        *[
            ("phi", Tool(**valid(phases=frozenset({SERVING, phase}))))
            for phase in (INTAKE, *NO_TOOL_PHASES)
        ],
        # A phi tool runs only while the office is open.
        *[
            ("phi", Tool(**valid(closed_phases=frozenset(phases))))
            for phases in ({SERVING}, {BOOKING}, {INTAKE}, set(Phase))
        ],
        ("declare", Tool.model_construct(**valid(closed_phases={INTAKE}))),
        ("declare", Tool.model_construct(**valid(closed_phases=frozenset({"intake"})))),
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
        # And so do the models nested in them.
        ("extra", Tool(**valid(arguments=HoldsLoose))),
        ("extra", Tool(**valid(arguments=HoldsLooseList))),
        # No field takes free keys or anything at all.
        *[
            ("free", Tool(**valid(arguments=arguments_with(details=(annotation, ...)))))
            for annotation in (
                dict[str, str],
                Mapping[str, int],
                list[dict[str, str]],
                dict[str, str] | None,
                Any,
                object,
                list[Any],
            )
        ],
        # The actions dispatch, the call, the admin API and the output guard write are not a
        # tool's to declare.
        *[
            ("reserved", Tool(**valid(audit_action=action)))
            for action in (
                Action.TOOL_REFUSED,
                Action.TOOL_FAILED,
                Action.CALL_STARTED,
                Action.CALL_ENDED,
                Action.GUARD_BLOCK,
                Action.ADMIN_READ_REQUESTS,
                Action.ADMIN_READ_MESSAGES,
                Action.ADMIN_READ_AUDIT,
                Action.ADMIN_READ_CALLS,
                Action.ADMIN_READ_CALL,
                Action.ADMIN_VERIFY_AUDIT,
                Action.ADMIN_SIGNED_IN,
                Action.ADMIN_SIGNED_OUT,
                Action.ADMIN_READ_CALL_DETAIL,
                Action.ADMIN_READ_QUEUE,
            )
        ],
        # A tool that may verify the caller: not a patient-data tool, in intake only, and never
        # callable by the model.
        ("verif", Tool(**valid(may_verify=True, model_callable=False))),
        *[
            (
                "verif",
                Tool(
                    **valid(
                        may_verify=True,
                        model_callable=False,
                        phi=False,
                        phases=frozenset(phases),
                    )
                ),
            )
            for phases in ({INTAKE, SERVING}, {SERVING}, {INTAKE, GREETING}, set(Phase))
        ],
        ("verif", Tool(**VERIFIER)),
        # And it runs only while the office is open.
        (
            "verif",
            Tool(**{**VERIFIER, "model_callable": False, "closed_phases": frozenset({INTAKE})}),
        ),
        # The tool that takes an identity claim: not a patient-data tool, intake only, for an
        # unidentified caller, office open only, callable by the model, and it may not verify.
        *[
            ("an identity claim", Tool(**{**CLAIMER, **change}))
            for change in (
                {"phi": True},
                {"phases": frozenset({INTAKE, SERVING})},
                {"phases": frozenset({SERVING})},
                {"closed_phases": frozenset({INTAKE})},
                {"callers": frozenset(Caller)},
                {"callers": NEW_PATIENTS},
                {"callers": frozenset({Caller.UNIDENTIFIED})},
                {"model_callable": False},
                {"may_verify": True},
            )
        ],
        # The tool that takes a new patient's details: the same, in booking only, for a new
        # patient's call only.
        *[
            ("a new patient's details", Tool(**{**DETAILER, **change}))
            for change in (
                {"phi": True},
                {"phases": frozenset({INTAKE})},
                {"phases": frozenset({BOOKING, SERVING})},
                {"callers": frozenset(Caller)},
                {"callers": frozenset({Caller.UNIDENTIFIED})},
                {"callers": frozenset({Caller.NEW_PATIENT})},
                {"closed_phases": frozenset({BOOKING})},
                {"model_callable": False},
                {"may_verify": True},
            )
        ],
        # Only those tools may take a date of birth, and nothing else that names a patient.
        ("names a patient", Tool(**{**CLAIMER, "takes_date_of_birth": None})),
        ("names a patient", Tool(**{**DETAILER, "takes_date_of_birth": None})),
        *[
            ("names a patient", Tool(**{**declaration, "arguments": arguments}))
            for declaration in (CLAIMER, DETAILER)
            for arguments in (
                arguments_with(full_name=(str, ...), date_of_birth=(str, ...)),
                arguments_with(full_name=(str, ...), dob=(str, ...), ssn=(str, ...)),
                arguments_with(dob=(str, ...), patient_id=(str, ...)),
            )
        ],
        # The other actions a tool's events may carry: never a reserved one, never its own
        # default action again.
        *[
            ("reserved", Tool(**valid(outcome_actions={action: Outcome.REFUSED})))
            for action in (
                Action.TOOL_REFUSED,
                Action.TOOL_FAILED,
                Action.CALL_ENDED,
                Action.ADMIN_READ_REQUESTS,
                Action.ADMIN_VERIFY_AUDIT,
            )
        ],
        ("default action", Tool(**valid(outcome_actions={Action.TOOL_CALLED: Outcome.SUCCESS}))),
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

    # One tool at most may verify the caller.
    verifier = {**VERIFIER, "model_callable": False}
    registry.register(Tool(**{**verifier, "name": "check_identity"}), grant=Grant())
    with pytest.raises(DeclarationError, match="verif"):
        registry.register(Tool(**{**verifier, "name": "check_identity_again"}), grant=Grant())
    assert [item.name for item in registry.tools if item.may_verify] == ["check_identity"]

    # One tool at most of each kind takes a date of birth.
    registry.register(Tool(**CLAIMER))
    with pytest.raises(DeclarationError, match="an identity claim"):
        registry.register(Tool(**{**CLAIMER, "name": "claim_identity_again"}))
    registry.register(Tool(**DETAILER))
    with pytest.raises(DeclarationError, match="a new patient's details"):
        registry.register(Tool(**{**DETAILER, "name": "take_details_again"}))
    assert {item.name: item.takes_date_of_birth for item in registry.tools} == {
        "read_chart": None,
        "office_info": None,
        "check_identity": None,
        "claim_identity": DobKind.IDENTITY_CLAIM,
        "take_details": DobKind.NEW_PATIENT_DETAILS,
    }

    # A code-only tool is registered with a grant of its own; a model-callable tool without.
    code_only = valid(name="code_only", phi=False, phases=frozenset({INTAKE}), model_callable=False)
    grant = Grant()
    for tool, given, rule in (
        (Tool(**code_only), None, "grant"),
        (Tool(**{**code_only, "model_callable": True}), Grant(), "grant"),
    ):
        with pytest.raises(DeclarationError, match=rule):
            registry.register(tool, grant=given)
    registry.register(Tool(**code_only), grant=grant)
    with pytest.raises(DeclarationError, match="grant"):
        registry.register(Tool(**{**code_only, "name": "code_only_again"}), grant=grant)

    # A code-only writing tool declares an argument model with no fields at all.
    with pytest.raises(DeclarationError, match="no arguments"):
        registry.register(
            Tool(**{**code_only, "name": "code_writer", "writes": True, "arguments": Topic}),
            grant=Grant(),
        )
    assert "code_writer" not in [item.name for item in registry.tools]

    # The confirmation rule is for tools the model can call: a code-only tool writes without
    # arguments; a model-callable one cannot.
    code_writer = {**code_only, "name": "code_writer", "writes": True}
    registry.register(Tool(**code_writer), grant=Grant())
    with pytest.raises(DeclarationError, match="confirmed"):
        registry.register(Tool(**{**code_writer, "name": "model_writer", "model_callable": True}))
    assert [item.name for item in registry.tools] == [
        "read_chart",
        "office_info",
        "check_identity",
        "claim_identity",
        "take_details",
        "code_only",
        "code_writer",
    ]


# Who the caller is, written by hand from the spec: a verified patient, a new patient's call with
# or without the details collected, a locked call, one with a claim pending, or nobody known
# yet; nobody, for a state no transition makes.
def caller_in(state: CallState) -> Caller | None:
    if state.verified:
        consistent = state.patient_id is not None and not state.is_new_patient
        return Caller.PATIENT if consistent else None
    if state.is_new_patient:
        if state.new_patient_details is None:
            return Caller.NEW_PATIENT
        return Caller.NEW_PATIENT_WITH_DETAILS
    if state.verify_locked:
        return Caller.LOCKED
    if state.pending_claim is not None:
        return Caller.CLAIMANT
    return Caller.UNIDENTIFIED


# The phase each kind of date of birth may be taken in.
DOB_PHASES = {DobKind.IDENTITY_CLAIM: INTAKE, DobKind.NEW_PATIENT_DETAILS: BOOKING}

SOME_DETAILS = NewPatientDetails(
    first_name="Ann",
    last_name="Lee",
    dob=date(1990, 1, 1),
    phone="555-000-0000",
    email="ann@example.org",
    insurance_provider=None,
    member_id=None,
    self_pay=True,
    reason_short="Check-up",
)


def gate_violations(registry: Registry) -> list[str]:
    """Every way the registry's tools break a declaration or the gate, over every phase,
    verification and office state, possible or not, and every kind of caller."""
    problems = []
    for item in registry.tools:
        try:
            check_declaration(item)
        except DeclarationError as error:
            problems.append(f"{item.name}: {error}")
    by_name = {item.name: item for item in registry.tools}
    states = every_combination()
    # The verified flag without a patient id, for each phase and office state.
    states += [state.model_copy(update={"patient_id": None}) for state in states if state.verified]
    # A new patient's call, with and without the details, and one that also claims to be
    # verified.
    states += [
        state.model_copy(update={"is_new_patient": True, "new_patient_details": details})
        for state in every_combination()
        for details in (None, SOME_DETAILS)
    ]
    # An unverified call with a claim pending, and one whose verification is locked.
    claim = IdentityClaim(full_name="Ann Lee", dob=date(1990, 1, 1))
    states += [
        state.model_copy(update=update)
        for state in every_combination()
        if not state.verified
        for update in (
            {"pending_claim": claim, "expecting_ssn4": True},
            {"verify_attempts": 3, "verify_locked": True},
        )
    ]
    for state in states:
        offered = {description.name for description in registry.tools_for(state)}
        # What dispatch would run, for the model or for code with the tool's grant.
        runnable = {item.name for item in registry.tools if is_available(item, state)}
        for name in sorted(offered | runnable):
            item = by_name[name]
            where = (
                f"{item.name} in {state.phase} verified={state.verified} open={state.office_open}"
            )
            if name in offered and not item.model_callable:
                problems.append(f"code only: {where}")
            if caller_in(state) not in item.callers:
                problems.append(f"caller: {where}")
            kind = item.takes_date_of_birth
            if kind is not None and state.phase is not DOB_PHASES[kind]:
                problems.append(f"date of birth: {where}")
            if state.phase in NO_TOOL_PHASES:
                problems.append(f"no tools: {where}")
            if state.phase not in item.phases | item.closed_phases:
                problems.append(f"undeclared phase: {where}")
            if item.phi and not (
                state.verified
                and state.patient_id is not None
                and state.office_open
                and state.phase in PHI_PHASES
            ):
                problems.append(f"phi: {where}")
            if item.may_verify and (item.phi or state.phase is not INTAKE):
                problems.append(f"verifier: {where}")
            if not state.office_open and state.phase not in item.closed_phases:
                problems.append(f"office closed: {where}")
            if state.office_open and state.phase not in item.phases:
                problems.append(f"office open: {where}")
    return problems


def test_application_registry_obeys_the_gate() -> None:
    # Every tool the application has: registered through the one entry point, wherever its
    # module is, once every module of the application is imported.
    import_application_modules()
    assert register_tools() is REGISTRY
    assert REGISTRY.sealed
    assert gate_violations(REGISTRY) == []
    assert len([item for item in REGISTRY.tools if item.may_verify]) <= 1
    for kind in DobKind:
        assert len([item for item in REGISTRY.tools if item.takes_date_of_birth is kind]) <= 1
    assert not [item for item in REGISTRY.tools if item.may_verify and item.model_callable]

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
                    closed_phases=frozenset(),
                    resource_type=ResourceType.PATIENT,
                )
            )

    # The check finds what it looks for: it passes the test tools, and fails a registry that
    # offers every tool everywhere.
    tools = [
        phi_tool("read_chart", noop),
        declared("office_info", noop),
        declared("transfer_desk", noop, phases=frozenset({INTAKE}), closed_phases=frozenset()),
        declared(
            "leave_note", noop, phases=frozenset({SERVING}), closed_phases=frozenset({INTAKE})
        ),
        declared(
            "check_identity",
            noop,
            phases=frozenset({INTAKE}),
            closed_phases=frozenset(),
            may_verify=True,
            model_callable=False,
        ),
        Tool(**CLAIMER),
        Tool(**DETAILER),
        declared("visit_kind", noop, callers=frozenset({Caller.NEW_PATIENT_WITH_DETAILS})),
    ]
    assert gate_violations(registry_of(*tools)) == []

    class Ungated(Registry):
        def tools_for(self, state: CallState) -> list[ToolDescription]:
            return [ToolDescription(name=item.name, parameters={}) for item in self.tools]

    ungated = Ungated()
    for item in tools:
        ungated.register(item, grant=None if item.model_callable else Grant())
    kinds = {problem.split(":")[0] for problem in gate_violations(ungated)}
    assert kinds == {
        "no tools",
        "undeclared phase",
        "phi",
        "office closed",
        "office open",
        "verifier",
        "code only",
        "caller",
        "date of birth",
    }


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
        after_refusal(state),
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
        assert await registry.dispatch(state, name, {}, deps) == (after_refusal(state), REFUSED)
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
            claim_keys=CLAIM_KEYS,
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
            claim_keys=CLAIM_KEYS,
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


# --- Rework: verification, the transaction, sealing, audit ids, bounds ----------------------


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_number: str


async def test_only_the_verifying_tool_can_verify(deps: Deps, owner_engine: Engine) -> None:
    async def claims_identity(
        session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
    ) -> ToolResult:
        return ToolResult(transition=Verify(patient_id=arguments.member_number, first_name="Ann"))

    async def arms(
        session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
    ) -> ToolResult:
        return ToolResult(transition=IdentityClaim(full_name="Ann Lee", dob=date(1990, 1, 1)))

    grant = Grant()
    registry = Registry()
    registry.register(
        declared("look_up_member", claims_identity, arguments=Member, phases=frozenset({INTAKE}))
    )
    registry.register(
        declared(
            "check_identity",
            claims_identity,
            arguments=Member,
            phases=frozenset({INTAKE}),
            closed_phases=frozenset(),
            may_verify=True,
            model_callable=False,
        ),
        grant=grant,
    )
    registry.register(phi_tool("read_chart", noop))
    registry.register(declared("look_up_claim", arms, phases=frozenset({INTAKE})))
    state = intake()

    # Any other tool asking to verify fails, and the caller stays unverified.
    after, outcome = await registry.dispatch(
        state, "look_up_member", {"member_number": "P002"}, deps
    )
    assert outcome == FAILED
    assert (after.verified, after.patient_id, after.phase, after.tool_errors) == (
        False,
        None,
        INTAKE,
        1,
    )
    assert "read_chart" not in names(registry, after)

    # So does any tool but the one that takes a claim asking to arm the SSN step.
    after, outcome = await registry.dispatch(state, "look_up_claim", {}, deps)
    assert outcome == FAILED
    assert (after.expecting_ssn4, after.pending_claim) == (False, None)

    # The one tool declared to verify does, called by code with its grant.
    after, outcome = await registry.dispatch(
        state, "check_identity", {"member_number": "P002"}, deps, grant=grant
    )
    assert outcome.status is Status.DONE
    assert (after.verified, after.patient_id, after.phase) == (True, "P002", SERVING)
    assert "read_chart" in names(registry, after)
    assert events(owner_engine) == [
        failed_event(state, "look_up_member"),
        failed_event(state, "look_up_claim"),
        called_event(state, "check_identity"),
    ]


async def commit_through_the_connection(session: AsyncSession) -> None:
    await (await session.connection()).commit()


async def roll_back_raw(session: AsyncSession) -> None:
    await session.execute(text("ROLLBACK"))


async def roll_back_and_write_again(session: AsyncSession) -> None:
    await session.rollback()
    await session.execute(INSERT_PATIENT, {"id": "P999"})


async def test_handler_cannot_leave_its_transaction(
    deps: Deps, runtime: AsyncEngine, owner_engine: Engine
) -> None:
    state = intake()

    def then_fail(
        step: Callable[[AsyncSession], Awaitable[None]],
    ) -> Callable[[AsyncSession], Awaitable[None]]:
        async def both(session: AsyncSession) -> None:
            await step(session)
            raise RuntimeError("the handler failed after ending its transaction")

        return both

    cases: list[tuple[str, Callable[[AsyncSession], Awaitable[None]]]] = [
        # It commits through the connection, then fails, or returns success.
        ("P911", then_fail(commit_through_the_connection)),
        ("P912", commit_through_the_connection),
        # It rolls back (raw, or through the session and writes again) and returns success.
        ("P913", roll_back_raw),
        ("P914", roll_back_and_write_again),
    ]
    for patient_id, step in cases:
        registry = registry_of(writing_tool(inserting(patient_id, then=step)))
        after, outcome = await registry.dispatch(state, "save_note", NOTE, deps)
        assert (after.tool_errors, outcome) == (1, FAILED), patient_id
    assert patients(owner_engine) == []
    assert events(owner_engine) == [failed_event(state, "save_note")] * len(cases)

    # The same commit through the connection while the audit chain's lock is held elsewhere:
    # neither its row nor any event.
    registry = registry_of(writing_tool(inserting("P915", then=commit_through_the_connection)))
    async with create_session_factory(runtime)() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
        after, outcome = await registry.dispatch(state, "save_note", NOTE, deps)
        await holder.rollback()
    assert (after.tool_errors, outcome) == (1, FAILED)
    assert patients(owner_engine) == []
    assert len(events(owner_engine)) == len(cases)


def test_the_application_registry_is_sealed() -> None:
    import_application_modules()
    assert register_tools() is REGISTRY
    assert register_tools() is REGISTRY
    with pytest.raises(DeclarationError, match="sealed"):
        REGISTRY.register(declared("late_tool", noop))
    assert "late_tool" not in [item.name for item in REGISTRY.tools]

    # A registry of its own is sealed the same way.
    registry = registry_of(declared("office_info", noop))
    registry.seal()
    with pytest.raises(DeclarationError, match="sealed"):
        registry.register(declared("look_up", noop, arguments=Topic))
    assert [item.name for item in registry.tools] == ["office_info"]


async def test_tool_calls_are_audited_with_their_ids(deps: Deps, owner_engine: Engine) -> None:
    registry = registry_of(
        declared("office_info", noop),
        phi_tool("read_chart", noop),
        declared(
            "book_slot",
            recording([], ToolResult(resource_id="42")),
            resource_type=ResourceType.APPOINTMENT,
        ),
        # A handler does not name the record of a tool or patient event.
        declared("names_itself", recording([], ToolResult(resource_id="names_itself"))),
        phi_tool("names_a_patient", recording([], ToolResult(resource_id="P002"))),
    )
    serving = verify(intake(), patient_id="P042", first_name="Ann")
    for name, status in (
        ("office_info", Status.DONE),
        ("read_chart", Status.DONE),
        ("book_slot", Status.DONE),
        ("names_itself", Status.FAILED),
        ("names_a_patient", Status.FAILED),
    ):
        _after, outcome = await registry.dispatch(serving, name, {}, deps)
        assert outcome.status is status, name
    call = serving.call_id
    assert events(owner_engine) == [
        ("tool_called", "tool", "office_info", "success", call),
        ("tool_called", "patient", "P042", "success", call),
        ("tool_called", "appointment", "42", "success", call),
        ("tool_failed", "tool", "names_itself", "failure", call),
        ("tool_failed", "tool", "names_a_patient", "failure", call),
    ]


async def never_returns(
    session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
) -> ToolResult:
    await anyio.sleep_forever()
    raise AssertionError("unreachable")


async def test_dispatch_has_a_deadline(
    deps: Deps, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tool_registry.DISPATCH_DEADLINE_SECONDS == 10
    monkeypatch.setattr(tool_registry, "DISPATCH_DEADLINE_SECONDS", 0.5)
    registry = registry_of(declared("stalls", never_returns))
    state = intake()
    with anyio.fail_after(10):
        after, outcome = await registry.dispatch(state, "stalls", {}, deps)
    assert (after.tool_errors, outcome) == (1, FAILED)
    assert events(owner_engine) == [failed_event(state, "stalls")]


async def test_oversized_arguments_are_refused(deps: Deps, owner_engine: Engine) -> None:
    parsed: list[int] = []

    class Counted(BaseModel):
        model_config = ConfigDict(extra="forbid")

        topic: str

        @model_validator(mode="before")
        @classmethod
        def record(cls, data: object) -> object:
            parsed.append(1)
            return data

    registry = registry_of(declared("look_up", noop, arguments=Counted))
    state = intake()
    # {"topic": "..."} serialized is the value's length plus 13 bytes.
    at_limit = {"topic": "x" * (16 * 1024 - 13)}
    over_limit = {"topic": "x" * (16 * 1024 - 12)}
    assert (await registry.dispatch(state, "look_up", at_limit, deps))[1].status is Status.DONE
    assert parsed == [1]
    assert await registry.dispatch(state, "look_up", over_limit, deps) == (
        after_refusal(state),
        REFUSED,
    )
    # Arguments that are not JSON at all are refused the same way.
    assert await registry.dispatch(state, "look_up", {"topic": {1, 2}}, deps) == (
        after_refusal(state),
        REFUSED,
    )
    assert parsed == [1]


async def test_five_refusals_hand_off(deps: Deps) -> None:
    registry = registry_of(declared("look_up", noop, arguments=Topic))
    state = intake()
    for count in range(1, 6):
        state, outcome = await registry.dispatch(state, "no_such_tool", {}, deps)
        assert (state.tool_refusals, state.tool_errors) == (count, 0)
        assert outcome == REFUSED.model_copy(update={"hand_off": count >= 5}), count
    # Every outcome after that says so too, a success included.
    state, outcome = await registry.dispatch(state, "look_up", {"topic": "hours"}, deps)
    assert (outcome.status, outcome.hand_off) == (Status.DONE, True)


# --- Slice 08: who may call a tool, and the events it may write ------------------------------


async def test_code_only_tools_are_refused_to_the_model(deps: Deps, owner_engine: Engine) -> None:
    code: Calls = []
    info: Calls = []
    grant = Grant()
    registry = Registry()
    registry.register(
        declared("settle_claim", recording(code), phases=frozenset({INTAKE}), model_callable=False),
        grant=grant,
    )
    registry.register(declared("office_info", recording(info)))
    state = intake()

    # Never offered to the model, in any state.
    for each in every_combination():
        assert "settle_claim" not in names(registry, each)
    assert names(registry, state) == ["office_info"]

    refusals: list[tuple[str, Grant | None]] = [
        # The model: no grant.
        ("settle_claim", None),
        # Code with a grant that is not this tool's.
        ("settle_claim", Grant()),
        # Code with this tool's grant, calling a tool it was not given the right to call.
        ("office_info", grant),
    ]
    for name, given in refusals:
        after, outcome = await registry.dispatch(state, name, {}, deps, grant=given)
        assert (after, outcome) == (after_refusal(state), REFUSED), (name, given)
    assert (code, info) == ([], [])
    assert events(owner_engine) == [refused_event(state, name) for name, _ in refusals]

    # Every gate rule applies to code too: outside its phase, even with its grant.
    for outside in (
        start(),
        verified_serving(),
        intake(office_open=True).model_copy(update={"phase": EMERGENCY}),
    ):
        after, outcome = await registry.dispatch(outside, "settle_claim", {}, deps, grant=grant)
        assert outcome == REFUSED
    assert code == []

    # With its own grant, in its phase, it runs.
    after, outcome = await registry.dispatch(state, "settle_claim", {}, deps, grant=grant)
    assert outcome.status is Status.DONE
    assert len(code) == 1

    # The application: the model cannot call submit_identity, and it is the only tool that
    # may verify, so code cannot verify through any other.
    import_application_modules()
    register_tools()
    by_name = {item.name: item for item in REGISTRY.tools}
    assert by_name["submit_identity"].model_callable is False
    assert [item.name for item in REGISTRY.tools if item.may_verify] == ["submit_identity"]
    assert "submit_identity" not in names(REGISTRY, intake())
    assert "verify_identity" in names(REGISTRY, intake())
    after, outcome = await REGISTRY.dispatch(intake(), "submit_identity", {}, deps)
    assert outcome == REFUSED
    assert after.verified is False


async def test_events_carry_only_declared_actions(deps: Deps, owner_engine: Engine) -> None:
    def picking(action: Action | None) -> Handler:
        return recording([], ToolResult(audit_action=action))

    outcomes = {Action.IDENTITY_REFUSED: Outcome.REFUSED, Action.IDENTITY_LOCKED: Outcome.REFUSED}
    registry = registry_of(
        declared("default", picking(None), outcome_actions=outcomes),
        declared("own", picking(Action.TOOL_CALLED), outcome_actions=outcomes),
        declared("refused", picking(Action.IDENTITY_REFUSED), outcome_actions=outcomes),
        declared("locked", picking(Action.IDENTITY_LOCKED), outcome_actions=outcomes),
        declared("undeclared", picking(Action.IDENTITY_VERIFIED), outcome_actions=outcomes),
        declared("reserved", picking(Action.TOOL_FAILED), outcome_actions=outcomes),
    )
    state = intake()
    for name in ("default", "own", "refused", "locked", "undeclared", "reserved"):
        await registry.dispatch(state, name, {}, deps)
    call = state.call_id
    assert events(owner_engine) == [
        ("tool_called", "tool", "default", "success", call),
        ("tool_called", "tool", "own", "success", call),
        ("identity_refused", "tool", "refused", "refused", call),
        ("identity_locked", "tool", "locked", "refused", call),
        ("tool_failed", "tool", "undeclared", "failure", call),
        ("tool_failed", "tool", "reserved", "failure", call),
    ]


async def test_scripts_and_reasons_keep_to_their_declarations(
    deps: Deps, owner_engine: Engine
) -> None:
    # A line's values are exactly its placeholders, each at most 80 characters of plain text;
    # an event's reason is one its tool declares. Anything else fails the call.
    verified = ScriptId.VERIFIED
    after_hours = ScriptId.AFTER_HOURS_CLOSE
    results: list[tuple[ToolResult, bool]] = [
        (ToolResult(), True),
        (ToolResult(script=ScriptId.GOODBYE), True),
        (ToolResult(script=verified, script_values={"first_name": "s" * 80}), True),
        (ToolResult(script=after_hours, script_values={"name": "Ana", "part_of_day": "day"}), True),
        (ToolResult(audit_reason=Reason.OTHER), True),
        (ToolResult(script=verified), False),
        (ToolResult(script=verified, script_values={"name": "Sarah"}), False),
        (ToolResult(script=verified, script_values={"first_name": "S", "last_name": "J"}), False),
        (ToolResult(script=after_hours, script_values={"name": "Ana"}), False),
        (ToolResult(script_values={"first_name": "Sarah"}), False),
        (ToolResult(script=ScriptId.GOODBYE, script_values={"first_name": "Sarah"}), False),
        (ToolResult(script=verified, script_values={"first_name": "s" * 81}), False),
        (ToolResult(script=verified, script_values={"first_name": "Sa​rah"}), False),
        (ToolResult(script=verified, script_values={"first_name": "‮Sarah"}), False),
        (ToolResult(script=verified, script_values={"first_name": "Sa\nrah"}), False),
        (ToolResult(script=verified, script_values={"first_name": "Sa\x85rah"}), False),
        (ToolResult(audit_reason=Reason.GUARD), False),
    ]
    registry = Registry()
    names_of = []
    for index, (result, _done) in enumerate(results):
        name = "case_" + "abcdefghijklmnopqrstuvwxyz"[index]
        registry.register(
            declared(name, recording([], result), audit_reasons=frozenset({Reason.OTHER}))
        )
        names_of.append(name)
    # A tool that declares no reason attaches none.
    registry.register(declared("no_reason", recording([], ToolResult(audit_reason=Reason.OTHER))))
    state = intake()
    for name, (result, done) in zip(names_of, results, strict=True):
        after, outcome = await registry.dispatch(state, name, {}, deps)
        if done:
            assert (after, outcome.status) == (state, Status.DONE), name
            assert outcome.script_values == result.script_values
        else:
            assert (after, outcome) == (count_tool_error(state), FAILED), name
    after, outcome = await registry.dispatch(state, "no_reason", {}, deps)
    assert (after, outcome) == (count_tool_error(state), FAILED)

    reasons = text("SELECT resource_id, action, reason FROM audit_events ORDER BY seq")
    with owner_engine.connect() as connection:
        written = [tuple(row) for row in connection.execute(reasons)]
    expected = [
        (name, "tool_called" if done else "tool_failed", "other" if name == "case_e" else None)
        for name, (_result, done) in zip(names_of, results, strict=True)
    ]
    assert written == [*expected, ("no_reason", "tool_failed", None)]


class NestedDob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dob: str


class ClaimWithNestedDob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    born: NestedDob


class ClaimWithUpperDob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    DOB: str  # noqa: N815 (the name under test)


class ClaimWithAliasedDob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    dob: str = Field(alias="DOB")


class ClaimWithValidationAliasDob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    dob: str = Field(validation_alias=AliasChoices("dob", "d_o_b"))


def test_the_date_of_birth_exemption_is_narrow() -> None:
    # Exactly a top-level field named `dob`, of a tool declared to take one of the two kinds:
    # nothing nested, no other spelling, no alias that names it otherwise.
    for declaration in (CLAIMER, DETAILER):
        for arguments in (
            ClaimWithNestedDob,
            ClaimWithUpperDob,
            ClaimWithAliasedDob,
            ClaimWithValidationAliasDob,
            arguments_with(full_name=(str, ...), d_o_b=(str, ...)),
        ):
            with pytest.raises(DeclarationError, match="names a patient"):
                Registry().register(Tool(**{**declaration, "arguments": arguments}))
        Registry().register(Tool(**declaration))
    # The two kinds live side by side, each once.
    registry = Registry()
    registry.register(Tool(**CLAIMER))
    registry.register(Tool(**DETAILER))
    for declaration in (CLAIMER, DETAILER):
        with pytest.raises(DeclarationError, match="only one tool"):
            registry.register(Tool(**{**declaration, "name": "another_one"}))


async def test_only_the_declared_tool_takes_new_patient_details(
    deps: Deps, owner_engine: Engine
) -> None:
    async def drafts(
        session: AsyncSession, state: CallState, arguments: Any, context: HandlerContext
    ) -> ToolResult:
        return ToolResult(transition=SOME_DETAILS)

    registry = Registry()
    registry.register(declared("look_up_visit", drafts, phases=frozenset({BOOKING})))
    registry.register(Tool(**{**DETAILER, "arguments": NoArguments, "handler": drafts}))
    state = move(intake(), BOOKING)

    after, outcome = await registry.dispatch(state, "look_up_visit", {}, deps)
    assert (outcome, after.new_patient_details) == (FAILED, None)
    after, outcome = await registry.dispatch(state, "take_details", {}, deps)
    assert outcome.status is Status.DONE
    assert after.new_patient_details == SOME_DETAILS
    assert events(owner_engine) == [
        failed_event(state, "look_up_visit"),
        called_event(state, "take_details"),
    ]
