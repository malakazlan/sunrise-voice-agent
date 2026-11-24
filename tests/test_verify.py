import asyncio
import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY, import_application_modules
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent.claims import ClaimKeys
from app.agent.scripts import ScriptId
from app.agent.state import (
    CallState,
    IdentityClaim,
    Phase,
    SsnStep,
    apply,
    move,
    new_call,
)
from app.agent.tool_registry import REGISTRY, Deps, Status, ToolOutcome
from app.agent.tools import register_tools, submit_claim
from app.audit.log import Action, AuditLog, Event
from app.db.session import create_database_engine, create_session_factory
from app.guard.input import GuardDecision, guard_input
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
IDENTITY_KEY = TEST_IDENTITY_KEY

LAST_FOUR = "33" + "44"
SARAH = ("Sarah Johnson", "1985-03-14")

# Synthetic patients: (id, first, last, date of birth, last four).
PATIENTS = [
    ("P001", "Sarah", "Johnson", "1985-03-14", LAST_FOUR),
    ("P002", "Michael", "Brown", "1972-11-08", "10" + "20"),
    # Two patients sharing a date of birth.
    ("P010", "Ann", "Lee", "1990-01-01", "55" + "66"),
    ("P011", "Bob", "Stone", "1990-01-01", "77" + "88"),
    # Two records with the same name, date and digits.
    ("P020", "Chris", "Moore", "1980-05-05", "99" + "00"),
    ("P021", "Chris", "Moore", "1980-05-05", "99" + "00"),
]
INSERT_PATIENT = text(
    "INSERT INTO patients"
    " (id, first_name, last_name, dob, ssn_last4, phone, lab_results_status, balance_due)"
    " VALUES (:id, :first, :last, :dob, :ssn, '555-000-0000', 'Pending', 0)"
)
EVENTS = text(
    "SELECT action, resource_type, resource_id, outcome, call_id FROM audit_events ORDER BY seq"
)
ATTEMPTS = text(
    "SELECT claim_key, call_id, success, created_at FROM verification_attempts ORDER BY id"
)

# What code and the model are told, written by hand.
ASK = ToolOutcome(
    status=Status.DONE, message=None, data={}, hand_off=False, script=ScriptId.SSN_ASK
)
FAIL = ASK.model_copy(update={"script": ScriptId.VERIFY_FAIL})
LOCKED = ASK.model_copy(update={"script": ScriptId.VERIFY_LOCKED})
VERIFIED = ASK.model_copy(update={"script": ScriptId.VERIFIED})
REFUSED = ToolOutcome(
    status=Status.REFUSED, message="That is not available right now.", data={}, hand_off=False
)
FAILED = ToolOutcome(
    status=Status.FAILED, message="That could not be done right now.", data={}, hand_off=False
)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(autouse=True)
def application_tools() -> Iterator[None]:
    import_application_modules()
    register_tools()
    yield


@pytest.fixture
def seeded(owner_engine: Engine) -> Engine:
    with owner_engine.begin() as connection:
        for patient_id, first, last, dob, ssn in PATIENTS:
            connection.execute(
                INSERT_PATIENT,
                {"id": patient_id, "first": first, "last": last, "dob": dob, "ssn": ssn},
            )
    return owner_engine


def deps_with(runtime: AsyncEngine, clock: Clock, *, audit: AuditLog | None = None) -> Deps:
    return Deps(
        sessions=create_session_factory(runtime),
        audit=AuditLog(SecretStr(TEST_AUDIT_KEY)) if audit is None else audit,
        now=clock,
        claim_keys=ClaimKeys(SecretStr(IDENTITY_KEY)),
    )


@pytest.fixture
async def deps(runtime: AsyncEngine, clock: Clock, seeded: Engine) -> AsyncIterator[Deps]:
    yield deps_with(runtime, clock)


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def intake(office_open: bool = True) -> CallState:
    return move(
        new_call(call_id=new_call_id(), started_at=T0, office_open=office_open), Phase.INTAKE
    )


async def claim(state: CallState, deps: Deps, name: str, dob: str) -> tuple[CallState, ToolOutcome]:
    """The model calls verify_identity with what the caller said."""
    return await REGISTRY.dispatch(state, "verify_identity", {"full_name": name, "dob": dob}, deps)


async def say(
    state: CallState, words: str, deps: Deps
) -> tuple[CallState, GuardDecision, ToolOutcome | None]:
    """One caller turn through the guard, and the submit when it asks for one."""
    decision = guard_input(words, state)
    if decision.change is not None:
        state = apply(state, decision.change)
    outcome = None
    if decision.submit:
        state, outcome = await submit_claim(state, deps)
    return state, decision, outcome


async def attempt(
    deps: Deps, name: str, dob: str, *turns: str, state: CallState | None = None
) -> tuple[CallState, ToolOutcome | None]:
    """A claim and the turns after it; the outcome of the submit, if one ran."""
    state, outcome = await claim(intake() if state is None else state, deps, name, dob)
    assert outcome == ASK
    result = None
    for words in turns:
        state, _decision, submitted = await say(state, words, deps)
        result = submitted or result
    return state, result


def events(owner_engine: Engine) -> list[tuple[Any, ...]]:
    with owner_engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS)]


def attempts(owner_engine: Engine) -> list[tuple[Any, ...]]:
    with owner_engine.connect() as connection:
        return [tuple(row) for row in connection.execute(ATTEMPTS)]


def observable(state: CallState) -> tuple[Any, ...]:
    """Everything a state shows apart from its call id."""
    return (
        state.phase,
        state.verified,
        state.patient_id,
        state.verify_attempts,
        state.verify_locked,
        state.pending_claim,
        state.expecting_ssn4,
        state.ssn_turns,
        state.pending_ssn4,
        state.verified_ssn4,
        state.tool_errors,
        state.tool_refusals,
    )


def app_log(caplog: pytest.LogCaptureFixture) -> str:
    """The application's own log lines. The driver's carry timings, which differ run to run;
    its parameters are hidden by the engine options (app/db/session.py)."""
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app")
    )


def expected_key(name_key: str, dob: str, key: str = IDENTITY_KEY) -> str:
    """HMAC-SHA256 under the key over the name key and the ISO date, as the spec states it."""
    message = f"{name_key}:{dob}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


# --- R5 -------------------------------------------------------------------------------------


async def test_failures_are_indistinguishable(
    deps: Deps, seeded: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    failures = {
        "wrong name": ("Sarah Jonson", "1985-03-14", (LAST_FOUR,)),
        "wrong date": ("Sarah Johnson", "1985-03-15", (LAST_FOUR,)),
        "wrong digits": ("Sarah Johnson", "1985-03-14", ("33" + "45",)),
        "unknown person": ("Nobody Here", "1999-09-09", (LAST_FOUR,)),
        "no digits given": ("Sarah Johnson", "1985-03-14", ("um", "what", "hold on")),
        "two records match": ("Chris Moore", "1980-05-05", ("99" + "00",)),
    }
    seen = []
    for kind, (name, dob, turns) in failures.items():
        caplog.clear()
        before = len(events(seeded))
        state, outcome = await attempt(deps, name, dob, *turns)
        new_events = events(seeded)[before:]
        rows = [row for row in attempts(seeded) if row[1] == state.call_id]
        logged = app_log(caplog).replace(state.call_id, "<call>")
        seen.append(
            (
                outcome,
                observable(state),
                [event[:4] for event in new_events],
                [(success, created_at) for _key, _call, success, created_at in rows],
                logged,
            )
        )
        assert [event[4] for event in new_events] == [state.call_id] * 2, kind

    # Written by hand: the same outcome, script, events, state change and record for each.
    first = seen[0]
    assert first[0] == FAIL
    assert first[1] == (Phase.INTAKE, False, None, 1, False, None, False, 0, None, None, 0, 0)
    assert first[2] == [
        ("identity_claimed", "tool", "verify_identity", "success"),
        ("identity_refused", "patient", None, "refused"),
    ]
    assert first[3] == [(False, T0)]
    for kind, result in zip(failures, seen, strict=True):
        assert result == first, kind

    # And the one that matches is different.
    state, outcome = await attempt(deps, *SARAH, LAST_FOUR)
    assert outcome == VERIFIED
    assert (state.phase, state.verified, state.patient_id) == (Phase.SERVING, True, "P001")


# --- R6 -------------------------------------------------------------------------------------


async def test_all_three_must_match_one_record(deps: Deps, seeded: Engine) -> None:
    # Ann and Bob share a date of birth: one's name with the other's digits matches nobody.
    for name, digits in (("Ann Lee", "77" + "88"), ("Bob Stone", "55" + "66")):
        state, outcome = await attempt(deps, name, "1990-01-01", digits)
        assert outcome == FAIL, name
        assert (state.verified, state.patient_id) == (False, None)

    # Two records with the same name, date and digits: no match.
    state, outcome = await attempt(deps, "Chris Moore", "1980-05-05", "99" + "00")
    assert outcome == FAIL
    assert state.verified is False

    # A name transcribed with another spelling fails like any other mismatch.
    state, outcome = await attempt(deps, "Ann Leigh", "1990-01-01", "55" + "66")
    assert outcome == FAIL

    # Each with their own: verified as that record, with that first name.
    for name, digits, patient_id, first in (
        ("Ann Lee", "55" + "66", "P010", "Ann"),
        ("Bob Stone", "77" + "88", "P011", "Bob"),
    ):
        state, outcome = await attempt(deps, name, "1990-01-01", digits)
        assert outcome == VERIFIED, name
        assert (state.patient_id, state.patient_first_name, state.verified_ssn4) == (
            patient_id,
            first,
            digits,
        )
        # The step is over and nothing of the claim is left.
        assert (state.pending_claim, state.pending_ssn4, state.expecting_ssn4) == (
            None,
            None,
            False,
        )


# --- R7 -------------------------------------------------------------------------------------


async def test_three_failures_lock_the_call(deps: Deps, seeded: Engine, clock: Clock) -> None:
    state = intake()
    outcomes = []
    for _ in range(3):
        state, outcome = await attempt(deps, *SARAH, "00" + "00", state=state)
        outcomes.append(outcome)
    assert outcomes == [FAIL, FAIL, LOCKED]
    assert (state.verify_attempts, state.verify_locked) == (3, True)
    assert [event[0] for event in events(seeded)[-2:]] == ["identity_claimed", "identity_locked"]
    assert events(seeded)[-1][:4] == ("identity_locked", "patient", None, "refused")

    # A locked call cannot arm the step, even for the right person with the right digits.
    count = len(attempts(seeded))
    after, outcome = await claim(state, deps, *SARAH)
    assert outcome == LOCKED
    assert (after.expecting_ssn4, after.pending_claim, after.verify_attempts) == (False, None, 3)
    assert events(seeded)[-1][:4] == ("identity_locked", "tool", "verify_identity", "refused")
    after, _decision, submitted = await say(after, LAST_FOUR, deps)
    assert submitted is None
    assert after.verified is False
    # Code cannot push a submit through either: there is no claim to submit.
    after, outcome = await submit_claim(after, deps)
    assert (outcome.status, after.verified) == (Status.FAILED, False)
    assert len(attempts(seeded)) == count

    # Two failures do not lock; the third attempt can still succeed. Two hours on, so that
    # the failures above no longer count across calls.
    clock.now = T0 + timedelta(hours=2)
    state = intake()
    for _ in range(2):
        state, outcome = await attempt(deps, *SARAH, "00" + "00", state=state)
        assert outcome == FAIL
    state, outcome = await attempt(deps, *SARAH, LAST_FOUR, state=state)
    assert outcome == VERIFIED
    assert (state.verify_attempts, state.verify_locked, state.patient_id) == (2, False, "P001")


# --- R8 -------------------------------------------------------------------------------------


async def test_cross_call_lockout(deps: Deps, seeded: Engine, clock: Clock) -> None:
    for name, dob, wrong in (
        ("Sarah Johnson", "1985-03-14", "00" + "00"),
        ("Nobody Here", "1999-09-09", LAST_FOUR),
    ):
        clock.now = T0
        # Five calls, one failure each.
        for _ in range(5):
            _state, outcome = await attempt(deps, name, dob, wrong)
            assert outcome == FAIL
        # The sixth call is told the identity is locked; nothing is armed or recorded.
        count = len(attempts(seeded))
        for minutes in (0, 59, 60):
            clock.now = T0 + timedelta(minutes=minutes)
            state, outcome = await claim(intake(), deps, name, dob)
            assert outcome == LOCKED, (name, minutes)
            assert (state.expecting_ssn4, state.pending_claim) == (False, None)
            assert events(seeded)[-1][:4] == (
                "identity_locked",
                "tool",
                "verify_identity",
                "refused",
            )
        assert len(attempts(seeded)) == count
        # Another spacing or case of the same name is the same claim.
        _state, outcome = await claim(intake(), deps, name.upper().replace(" ", "  "), dob)
        assert outcome == LOCKED
        # A failure 61 minutes old no longer counts.
        clock.now = T0 + timedelta(minutes=61)
        state, outcome = await claim(intake(), deps, name, dob)
        assert outcome == ASK, name
        assert state.expecting_ssn4 is True

    # Another identity is untouched by those failures.
    clock.now = T0
    state, outcome = await attempt(deps, "Michael Brown", "1972-11-08", "10" + "20")
    assert outcome == VERIFIED

    # A success does not clear earlier failures: four, a success, one more locks.
    clock.now = T0 + timedelta(hours=3)
    for _ in range(4):
        _state, outcome = await attempt(deps, "Ann Lee", "1990-01-01", "00" + "00")
        assert outcome == FAIL
    _state, outcome = await attempt(deps, "Ann Lee", "1990-01-01", "55" + "66")
    assert outcome == VERIFIED
    _state, outcome = await claim(intake(), deps, "Ann Lee", "1990-01-01")
    assert outcome == ASK
    _state, outcome = await attempt(deps, "Ann Lee", "1990-01-01", "00" + "00")
    assert outcome == FAIL
    _state, outcome = await claim(intake(), deps, "Ann Lee", "1990-01-01")
    assert outcome == LOCKED


# --- R10 ------------------------------------------------------------------------------------


async def test_no_verification_when_closed(deps: Deps, seeded: Engine) -> None:
    closed = intake(office_open=False)
    # Only the message, the emergency line and the goodbye: nothing that verifies.
    assert [item.name for item in REGISTRY.tools_for(closed)] == [
        "take_message",
        "report_emergency",
        "close_call",
    ]
    after, outcome = await claim(closed, deps, *SARAH)
    assert (after, outcome) == (closed.model_copy(update={"tool_refusals": 1}), REFUSED)
    assert after.expecting_ssn4 is False

    # An armed state forced past the transitions with the office closed still cannot submit.
    forced = closed.model_copy(
        update={
            "pending_claim": IdentityClaim(full_name="Sarah Johnson", dob=date(1985, 3, 14)),
            "pending_ssn4": LAST_FOUR,
        }
    )
    after, outcome = await submit_claim(forced, deps)
    assert outcome == REFUSED
    assert (after.verified, after.pending_claim, after.pending_ssn4) == (False, None, None)
    assert attempts(seeded) == []
    assert [event[0] for event in events(seeded)] == ["tool_refused", "tool_refused"]


# --- R11 ------------------------------------------------------------------------------------


async def test_ssn_fields_stay_hidden(
    deps: Deps, seeded: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    states = []
    state, _outcome = await claim(intake(), deps, *SARAH)
    state, _decision, _submitted = await say(state, "three three", deps)
    states.append(state)
    assert state.pending_ssn4 == "33"
    state, _decision, outcome = await say(state, "four four", deps)
    assert outcome == VERIFIED
    assert state.verified_ssn4 == LAST_FOUR
    states.append(state)
    failed, outcome = await attempt(deps, *SARAH, "00" + "00")
    assert outcome == FAIL
    call_ids = [item.call_id for item in (*states, failed)]

    def without_ids(shown: str) -> str:
        for call_id in call_ids:
            shown = shown.replace(call_id, "<call>")
        return shown

    for item in states:
        for shown in (repr(item), str(item), repr(item.model_dump()), item.model_dump_json()):
            assert "33" not in without_ids(shown), shown
        assert "pending_ssn4" not in item.model_dump()
        assert "verified_ssn4" not in item.model_dump(include={"verified_ssn4"})
    # The transition that carries the digits hides them too.
    step = SsnStep(expecting_ssn4=True, ssn_turns=1, pending_ssn4=LAST_FOUR)
    for shown in (repr(step), str(step), step.model_dump_json()):
        assert LAST_FOUR not in shown

    # An error names the field, never the digits: not in its text form, nor in the details
    # without their input (pydantic's errors() returns the input only when asked to).
    for build in (
        lambda: SsnStep(expecting_ssn4=True, ssn_turns=1, pending_ssn4=LAST_FOUR + "5"),
        lambda: CallState.model_validate({**dict(states[0]), "pending_ssn4": LAST_FOUR + "x"}),
        lambda: CallState.model_validate({**dict(states[1]), "verified_ssn4": "33" + "4"}),
    ):
        with pytest.raises(ValidationError) as invalid:
            build()
        for shown in (str(invalid.value), repr(invalid.value)):
            assert "334" not in shown
        assert "334" not in repr(invalid.value.errors(include_input=False))
        assert "pending_ssn4" in str(invalid.value) or "verified_ssn4" in str(invalid.value)

    assert app_log(caplog)
    assert "33" not in without_ids(app_log(caplog))
    with seeded.connect() as connection:
        audit_rows = connection.execute(
            text(
                "SELECT seq, ts, call_id, actor, action, resource_type, resource_id, outcome,"
                " reason FROM audit_events"
            )
        ).all()
        attempt_rows = connection.execute(
            text("SELECT id, success, created_at FROM verification_attempts")
        ).all()
    assert audit_rows and attempt_rows
    for row in [*audit_rows, *attempt_rows]:
        assert LAST_FOUR not in without_ids(repr(tuple(row)))
    # The attempts table has no column the digits could be in.
    columns = {column["name"] for column in inspect(seeded).get_columns("verification_attempts")}
    assert columns == {"id", "claim_key", "call_id", "success", "created_at"}


# --- R12 ------------------------------------------------------------------------------------


async def test_attempts_store_only_a_keyed_hash(
    deps: Deps, seeded: Engine, runtime: AsyncEngine, clock: Clock
) -> None:
    state, outcome = await attempt(deps, "  Sarah   JOHNSON ", "1985-03-14", "00" + "00")
    assert outcome == FAIL
    other_key = "-".join(["another", "identity", "key"] * 3)
    other = Deps(
        sessions=deps.sessions,
        audit=deps.audit,
        now=clock,
        claim_keys=ClaimKeys(SecretStr(other_key)),
    )
    second, outcome = await attempt(other, *SARAH, "00" + "00")
    assert outcome == FAIL

    rows = attempts(seeded)
    assert rows == [
        (expected_key("sarahjohnson", "1985-03-14"), state.call_id, False, T0),
        (expected_key("sarahjohnson", "1985-03-14", other_key), second.call_id, False, T0),
    ]
    # Without the key the hash is not the plain digest of the claim.
    plain = hashlib.sha256(b"sarahjohnson:1985-03-14").hexdigest()
    assert plain not in {row[0] for row in rows}
    with seeded.connect() as connection:
        stored = repr(connection.execute(text("SELECT * FROM verification_attempts")).all())
    for part in ("sarah", "Sarah", "johnson", "Johnson", "1985-03-14", "03-14"):
        assert part not in stored.replace(rows[0][0], "").replace(rows[1][0], "")

    # The database refuses anything but a hash, and a call id of the declared shape.
    for column, value in (("claim_key", "sarahjohnson"), ("call_id", "Sarah Johnson")):
        values = {
            "claim_key": "0" * 64,
            "call_id": state.call_id,
            "success": False,
            "created_at": T0,
            column: value,
        }
        with (
            pytest.raises(IntegrityError, match=f"ck_verification_attempts_{column}"),
            seeded.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO verification_attempts (claim_key, call_id, success, created_at)"
                    " VALUES (:claim_key, :call_id, :success, :created_at)"
                ),
                values,
            )

    # The key is a required, non-empty secret.
    with pytest.raises(ValueError, match="key"):
        ClaimKeys(SecretStr(""))


# --- R14 ------------------------------------------------------------------------------------


class AuditThatFailsAfterWriting(AuditLog):
    """The real append, then a failure, for the identity events only."""

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        seq = await super().append(session, event, now=now)
        if event.action in (Action.IDENTITY_VERIFIED, Action.IDENTITY_REFUSED):
            raise RuntimeError("the audit failed after writing")
        return seq


async def test_attempt_and_audit_commit_together(
    runtime: AsyncEngine, clock: Clock, seeded: Engine
) -> None:
    failing = deps_with(runtime, clock, audit=AuditThatFailsAfterWriting(SecretStr(TEST_AUDIT_KEY)))
    for digits in (LAST_FOUR, "00" + "00"):
        state, outcome = await attempt(failing, *SARAH, digits)
        assert outcome == FAILED
        # Nothing of the attempt: no record, no identity event, no state change but the count.
        assert observable(state) == (
            Phase.INTAKE,
            False,
            None,
            0,
            False,
            None,
            False,
            0,
            None,
            None,
            1,
            0,
        )
    assert attempts(seeded) == []
    assert [event[0] for event in events(seeded)] == [
        "identity_claimed",
        "tool_failed",
        "identity_claimed",
        "tool_failed",
    ]

    # With a working audit log, the record and the event are both there.
    working = deps_with(runtime, clock)
    state, outcome = await attempt(working, *SARAH, LAST_FOUR)
    assert outcome == VERIFIED
    assert [row[1:3] for row in attempts(seeded)] == [(state.call_id, True)]
    assert events(seeded)[-1] == ("identity_verified", "patient", "P001", "success", state.call_id)


# --- Neighbours -----------------------------------------------------------------------------


async def test_said_all_at_once_then_asked(deps: Deps, seeded: Engine) -> None:
    decision = guard_input(
        f"This is Sarah Johnson, born March 14 1985, last four {LAST_FOUR}", intake()
    )
    assert decision.text == "This is Sarah Johnson, born March 14 1985, last four ****"
    # The model still calls verify_identity; the step asks for the digits.
    state, outcome = await claim(intake(), deps, *SARAH)
    assert outcome == ASK
    assert state.expecting_ssn4 is True


async def test_second_claim_replaces_the_first(deps: Deps, seeded: Engine) -> None:
    state, outcome = await claim(intake(), deps, "Sarah Jonson", "1985-03-14")
    state, _decision, _submitted = await say(state, "three three", deps)
    assert state.pending_ssn4 == "33"
    state, outcome = await claim(state, deps, *SARAH)
    assert outcome == ASK
    assert state.pending_claim == IdentityClaim(full_name="Sarah Johnson", dob=date(1985, 3, 14))
    assert (state.pending_ssn4, state.ssn_turns, state.expecting_ssn4) == (None, 0, True)
    state, _decision, submitted = await say(state, LAST_FOUR, deps)
    assert submitted == VERIFIED


async def test_bad_claims_are_refused_by_argument_validation(deps: Deps, seeded: Engine) -> None:
    state = intake()
    for arguments in (
        {"full_name": "Sarah Johnson", "dob": "2026-11-25"},
        {"full_name": "Sarah Johnson", "dob": "3000-01-01"},
        {"full_name": "Sarah Johnson", "dob": "1985-3-14"},
        {"full_name": "Sarah Johnson", "dob": "March 14 1985"},
        {"full_name": "Sarah Johnson", "dob": "1985-02-30"},
        {"full_name": "Sarah Johnson", "dob": 19850314},
        {"full_name": "Sarah Johnson", "dob": "1985-03-14T00:00:00"},
        {"full_name": "Sarah Johnson"},
        {"full_name": "李雷", "dob": "1985-03-14"},
        {"full_name": "Dr.", "dob": "1985-03-14"},
        {"full_name": "", "dob": "1985-03-14"},
        {"full_name": "a" * 201, "dob": "1985-03-14"},
        {"full_name": "Sarah Johnson", "dob": "1985-03-14", "ssn": LAST_FOUR},
    ):
        after, outcome = await REGISTRY.dispatch(state, "verify_identity", arguments, deps)
        assert (after, outcome) == (state.model_copy(update={"tool_refusals": 1}), REFUSED)
    assert attempts(seeded) == []
    # Born today is a date of birth.
    after, outcome = await claim(state, deps, "Sarah Johnson", "2026-11-24")
    assert outcome == ASK


async def test_database_down_mid_verification(
    database: Any, seeded: Engine, clock: Clock, hanging_up_server: int
) -> None:
    engine = create_database_engine(database)
    down_engine = create_database_engine(
        database.model_copy(update={"postgres_port": hanging_up_server})
    )
    try:
        up = deps_with(engine, clock)
        down = deps_with(down_engine, clock)
        state, outcome = await claim(intake(), up, *SARAH)
        assert outcome == ASK
        state, _decision, submitted = await say(state, LAST_FOUR, down)
        assert submitted == FAILED
        assert (state.verified, state.verify_attempts, state.tool_errors) == (False, 0, 1)
        # The calling code drops the claim and the digits.
        assert (state.pending_claim, state.pending_ssn4, state.expecting_ssn4) == (
            None,
            None,
            False,
        )
        assert attempts(seeded) == []
    finally:
        await engine.dispose()
        await down_engine.dispose()


async def test_hang_up_while_armed_stores_nothing(deps: Deps, seeded: Engine) -> None:
    state, _outcome = await claim(intake(), deps, *SARAH)
    state, _decision, _submitted = await say(state, "three three", deps)
    ended = move(state, Phase.ENDED)
    assert (ended.pending_claim, ended.pending_ssn4, ended.expecting_ssn4) == (None, None, False)
    assert attempts(seeded) == []


@pytest.fixture
async def hanging_up_server() -> AsyncIterator[int]:
    """A local server that closes every connection at once, as a database that went down."""
    import asyncio

    async def hang_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


# --- Rework: the count across calls under concurrency, and a locked second claim -------------


async def test_concurrent_claims_record_at_most_five(deps: Deps, seeded: Engine) -> None:
    # Ten calls each claim the same identity before any of them submits: each is armed, since
    # nothing is counted yet. Their wrong digits are then submitted all at once.
    armed = []
    for _ in range(10):
        state, outcome = await claim(intake(), deps, *SARAH)
        assert outcome == ASK
        decision = guard_input("00" + "00", state)
        assert decision.change is not None and decision.submit
        armed.append(apply(state, decision.change))
    results = await asyncio.gather(*(submit_claim(state, deps) for state in armed))

    failures = [row for row in attempts(seeded) if row[2] is False]
    assert len(failures) == 5
    assert sorted(str(outcome.script) for _state, outcome in results) == sorted(
        ["verify_fail"] * 5 + ["verify_locked"] * 5
    )
    # A call told the identity is locked counted nothing, and holds no claim.
    for state, outcome in results:
        if outcome.script is ScriptId.VERIFY_LOCKED:
            assert (state.verify_attempts, state.pending_claim, state.pending_ssn4) == (
                0,
                None,
                None,
            )
    assert [event[0] for event in events(seeded)].count("identity_locked") == 5


async def test_a_locked_second_claim_ends_the_first(
    deps: Deps, seeded: Engine, clock: Clock
) -> None:
    # Lock one claimed identity with five failed calls.
    for _ in range(5):
        _state, outcome = await attempt(deps, "Nobody Here", "1999-09-09", "00" + "00")
        assert outcome == FAIL
    # A call claims Sarah, then the locked identity: the first claim is gone.
    state, outcome = await claim(intake(), deps, *SARAH)
    assert outcome == ASK
    state, outcome = await claim(state, deps, "Nobody Here", "1999-09-09")
    assert outcome == LOCKED
    assert (state.pending_claim, state.expecting_ssn4, state.pending_ssn4) == (None, False, None)
    # Sarah's digits afterwards verify nothing.
    state, _decision, submitted = await say(state, LAST_FOUR, deps)
    assert submitted is None
    assert (state.verified, state.patient_id) == (False, None)
