"""The five tools a verified caller is served by: next appointment, refill request, lab status,
balance and a message to the doctor. Every expectation is written by hand from the seed data
(app/db/seed_data.json) and the spec, and every call goes through the application registry's
dispatch, as the model's would."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY
from psycopg import errors
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent.claims import ClaimKeys
from app.agent.state import CallState, Phase, move, new_call, verify
from app.agent.tool_registry import Deps, Registry, Status, ToolOutcome
from app.agent.tools import register_tools
from app.audit.log import AuditLog, Event, Outcome
from app.db.seed import load_seed_data, seed
from app.db.session import create_session_factory
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

# Tuesday 24 November 2026, 10:00 in clinic time.
T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
SEED_DAY = date(2026, 11, 24)

APPOINTMENT = "get_next_appointment"
REFILL = "request_refill"
LAB = "get_lab_status"
BALANCE = "get_balance"
MESSAGE = "send_message_to_doctor"
SERVING_TOOLS = [APPOINTMENT, REFILL, LAB, BALANCE, MESSAGE]

REFUSED = ToolOutcome(
    status=Status.REFUSED, message="That is not available right now.", data={}, hand_off=False
)
FAILED = ToolOutcome(
    status=Status.FAILED, message="That could not be done right now.", data={}, hand_off=False
)

SARAH_APPOINTMENT = "Thursday, December 3 at 10:30 AM with Dr. Patel"
SARAH_LABS = "Ready - doctor will call to discuss"
LISINOPRIL = {"medication": "Lisinopril 10mg", "pharmacy": "CVS Main St"}
METFORMIN = {"medication": "Metformin 500mg", "pharmacy": "CVS Main St"}
ATORVASTATIN = {"medication": "Atorvastatin 20mg", "pharmacy": "Walgreens Oak Ave"}
NOT_FOUND = {"status": "not_found"}

# Text that must never reach an audit event or a log line.
MARKER = "marker-message-detail"

EVENTS = text(
    "SELECT action, resource_type, resource_id, outcome, call_id, actor, reason"
    " FROM audit_events ORDER BY seq"
)
REQUESTS = text("SELECT call_id, patient_id, type, summary FROM requests ORDER BY id")
MESSAGES = text(
    "SELECT call_id, patient_id, name, callback_number, kind, body FROM messages ORDER BY id"
)
INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, phone, lab_results_status, balance_due)"
    " VALUES (:id, 'Test', 'Patient', '1980-01-01', '555-000-0000', 'Pending', 0)"
)
INSERT_MEDICATION = text(
    "INSERT INTO medications (patient_id, name, refills_remaining, last_filled, pharmacy)"
    " VALUES (:patient_id, :name, :refills, '2026-10-01', :pharmacy)"
)
INSERT_APPOINTMENT = text(
    "INSERT INTO appointments (patient_id, doctor, starts_at, status)"
    " VALUES (:patient_id, :doctor, :starts_at, :status)"
)
DATABASE_NOW = text("SELECT now()")


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def start(office_open: bool = True, at: datetime = T0) -> CallState:
    return new_call(call_id=new_call_id(), started_at=at, office_open=office_open)


def intake(office_open: bool = True) -> CallState:
    return move(start(office_open), Phase.INTAKE)


def serving(patient_id: str, first_name: str, at: datetime = T0) -> CallState:
    return verify(move(start(at=at), Phase.INTAKE), patient_id=patient_id, first_name=first_name)


def sarah(at: datetime = T0) -> CallState:
    return serving("P001", "Sarah", at)


def michael() -> CallState:
    return serving("P002", "Michael")


def forced(phase: Phase, *, verified: bool, office_open: bool) -> CallState:
    """Any combination, legal or not, set past the transitions."""
    update: dict[str, Any] = {"phase": phase}
    if verified:
        update.update(verified=True, patient_id="P001", patient_first_name="Sarah")
    return start(office_open).model_copy(update=update)


def rows(engine: Engine, query: Any) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(query)]


def execute(engine: Engine, statement: Any, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(statement, params)


def database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        now = connection.execute(DATABASE_NOW).scalar_one()
    assert isinstance(now, datetime)
    return now


def offered(registry: Registry, state: CallState) -> list[str]:
    return [item.name for item in registry.tools_for(state) if item.name in SERVING_TOOLS]


def refill(spoken: str, confirmed: bool | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"medication_name": spoken}
    if confirmed is not None:
        arguments["confirmed"] = confirmed
    return arguments


def message(body: str, confirmed: bool | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"summary_short": body}
    if confirmed is not None:
        arguments["confirmed"] = confirmed
    return arguments


def event(action: str, state: CallState) -> tuple[Any, ...]:
    return (action, "patient", state.patient_id, "success", state.call_id, "agent", None)


@pytest.fixture
def registry() -> Registry:
    return register_tools()


@pytest.fixture
async def deps(runtime: AsyncEngine) -> AsyncIterator[Deps]:
    yield Deps(
        sessions=create_session_factory(runtime),
        audit=AuditLog(SecretStr(TEST_AUDIT_KEY)),
        now=lambda: T0,
        claim_keys=ClaimKeys(SecretStr(TEST_IDENTITY_KEY)),
    )


@pytest.fixture
async def seeded(owner_session: AsyncSession) -> None:
    """The task's synthetic patients, P001 to P005, as the start-up seed writes them."""
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()


async def call(
    registry: Registry, deps: Deps, state: CallState, name: str, arguments: object = None
) -> ToolOutcome:
    _after, outcome = await registry.dispatch(
        state, name, {} if arguments is None else arguments, deps
    )
    return outcome


async def data_of(
    registry: Registry, deps: Deps, state: CallState, name: str, arguments: object = None
) -> dict[str, Any]:
    outcome = await call(registry, deps, state, name, arguments)
    assert outcome.status is Status.DONE, (name, outcome)
    return outcome.data


# --- R1: the caller's own record -------------------------------------------------------------


async def test_tools_use_the_callers_own_record(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    first, second = sarah(), michael()
    note = "Please call me about the new dose"

    assert await data_of(registry, deps, second, APPOINTMENT) == {"appointment": None}
    assert await data_of(registry, deps, second, LAB) == {"lab_status": "Pending"}
    assert await data_of(registry, deps, second, BALANCE) == {"balance": "0.00"}
    # Sarah's medication is not Michael's to refill.
    assert await data_of(registry, deps, second, REFILL, refill("lisinopril", True)) == NOT_FOUND
    assert await data_of(registry, deps, second, REFILL, refill("atorvastatin", True)) == {
        "status": "requested",
        **ATORVASTATIN,
    }
    sent = await data_of(registry, deps, second, MESSAGE, message(note, True))
    assert sent["status"] == "sent"

    assert await data_of(registry, deps, first, APPOINTMENT) == {"appointment": SARAH_APPOINTMENT}
    assert await data_of(registry, deps, first, LAB) == {"lab_status": SARAH_LABS}
    assert await data_of(registry, deps, first, BALANCE) == {"balance": "45.00"}
    assert await data_of(registry, deps, first, REFILL, refill("atorvastatin", True)) == NOT_FOUND
    assert await data_of(registry, deps, first, REFILL, refill("lisinopril", True)) == {
        "status": "requested",
        **LISINOPRIL,
    }

    assert rows(owner_engine, REQUESTS) == [
        (second.call_id, "P002", "refill", "Atorvastatin 20mg at Walgreens Oak Ave"),
        (second.call_id, "P002", "doctor_message", note),
        (first.call_id, "P001", "refill", "Lisinopril 10mg at CVS Main St"),
    ]
    assert rows(owner_engine, MESSAGES) == [
        (second.call_id, "P002", None, None, "doctor_message", note)
    ]
    # Every event names the caller's own patient id.
    assert {(row[2], row[4]) for row in rows(owner_engine, EVENTS)} == {
        ("P002", second.call_id),
        ("P001", first.call_id),
    }


# --- R2: no patient argument -----------------------------------------------------------------


async def test_no_patient_argument(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    by_name = {item.name: item for item in registry.tools}
    fields = {name: list(by_name[name].arguments.model_fields) for name in SERVING_TOOLS}
    assert fields == {
        APPOINTMENT: [],
        REFILL: ["medication_name", "confirmed"],
        LAB: [],
        BALANCE: [],
        MESSAGE: ["summary_short", "confirmed"],
    }
    for name in SERVING_TOOLS:
        assert by_name[name].arguments.model_config.get("extra") == "forbid", name

    # Naming another patient is refused before any handler runs.
    state = sarah()
    attempts: list[tuple[str, dict[str, Any]]] = [
        (APPOINTMENT, {"patient_id": "P002"}),
        (LAB, {"patient_id": "P002"}),
        (BALANCE, {"patient": "P002"}),
        (REFILL, {**refill("atorvastatin", True), "patient_id": "P002"}),
        (MESSAGE, {**message("hello", True), "patient_id": "P002"}),
        (MESSAGE, {**message("hello", True), "dob": "1972-11-08"}),
    ]
    for name, arguments in attempts:
        assert await call(registry, deps, state, name, arguments) == REFUSED, name
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, MESSAGES) == []


# --- R3 to R6: refills -----------------------------------------------------------------------


async def test_refill_not_found_reveals_nothing(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    state = sarah()
    spoken = [
        # Michael's medication, one nobody has, nothing, two at once, and vague words.
        "atorvastatin",
        "aspirin",
        "",
        "lisinopril and metformin",
        "lisinopril metformin",
        "my medication",
        "the blood pressure one",
    ]
    for name in spoken:
        for confirmed in (None, False, True):
            outcome = await call(registry, deps, state, REFILL, refill(name, confirmed))
            assert outcome.status is Status.DONE
            assert outcome.data == NOT_FOUND, name
            dumped = outcome.model_dump_json().lower()
            for hint in ("lisinopril", "metformin", "cvs", "medications", "["):
                assert hint not in dumped, (name, hint)

    # A patient with no medications at all.
    james = serving("P004", "James")
    assert await data_of(registry, deps, james, REFILL, refill("lisinopril", True)) == NOT_FOUND
    assert rows(owner_engine, REQUESTS) == []


async def test_refill_count_is_never_returned(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    # A count that appears in no name or pharmacy, so any trace of it would show.
    execute(
        owner_engine,
        text(
            "UPDATE medications SET refills_remaining = 7 WHERE patient_id = 'P001'"
            " AND name = 'Lisinopril 10mg'"
        ),
    )
    state = sarah()
    asked = await data_of(registry, deps, state, REFILL, refill("lisinopril"))
    done = await data_of(registry, deps, state, REFILL, refill("lisinopril", True))
    assert asked == {"status": "needs_confirmation", **LISINOPRIL}
    assert done == {"status": "requested", **LISINOPRIL}
    for data in (asked, done):
        assert "7" not in json.dumps(data)
        assert "refill" not in json.dumps(data).lower()


async def test_zero_refills_go_to_the_doctor(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    state = sarah()
    sent = await data_of(registry, deps, state, REFILL, refill("metformin", True))
    assert sent == {"status": "sent_to_doctor", **METFORMIN}
    for word in ("approved", "requested"):
        assert word not in json.dumps(sent).lower()

    emily = serving("P005", "Emily")
    assert await data_of(registry, deps, emily, REFILL, refill("albuterol", True)) == {
        "status": "sent_to_doctor",
        "medication": "Albuterol inhaler",
        "pharmacy": "CVS Main St",
    }
    assert rows(owner_engine, REQUESTS) == [
        (state.call_id, "P001", "refill", "Metformin 500mg at CVS Main St"),
        (emily.call_id, "P005", "refill", "Albuterol inhaler at CVS Main St"),
    ]


async def test_refill_does_not_match_another_drug(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    listed = [
        "Metformin 500mg",
        "Lisinopril 10mg",
        "Atorvastatin 20mg",
        "Plavix 75mg",
        "Paxil 20mg",
        "Citalopram 20mg",
        "Prednisone 10mg",
        "Fluoxetine 20mg",
        "Clonazepam 0.5mg",
        "Tylenol 500mg",
        "Folic acid 1mg",
    ]
    execute(owner_engine, INSERT_PATIENT, id="P900")
    for name in listed:
        execute(
            owner_engine,
            INSERT_MEDICATION,
            patient_id="P900",
            name=name,
            refills=1,
            pharmacy="Test Pharmacy",
        )
    # The matcher's must-not rows (tests/test_matching.py) against this patient's list.
    must_not = [
        "metoprolol",
        "losartan",
        "simvastatin",
        "lipitor",
        "plavex",
        "paxel",
        "escitalopram",
        "fosinopril",
        "taxol",
        "prednisolone",
        "duloxetine",
        "lorazepam",
        "folinic acid",
        "xylenol",
        "lisinopril 20",
        "metformin er",
    ]
    state = serving("P900", "Test")
    for spoken in must_not:
        assert await data_of(registry, deps, state, REFILL, refill(spoken, True)) == NOT_FOUND, (
            spoken
        )
    assert rows(owner_engine, REQUESTS) == []

    # The list itself is matched as it should be.
    assert await data_of(registry, deps, state, REFILL, refill("plavix", True)) == {
        "status": "requested",
        "medication": "Plavix 75mg",
        "pharmacy": "Test Pharmacy",
    }


async def test_long_refill_summary_is_cut_to_fit(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    pharmacy = "Corner Pharmacy " + "p" * 230
    execute(owner_engine, INSERT_PATIENT, id="P901")
    execute(
        owner_engine,
        INSERT_MEDICATION,
        patient_id="P901",
        name="Hydrochlorothiazide 25mg",
        refills=1,
        pharmacy=pharmacy,
    )
    state = serving("P901", "Test")
    expected = {
        "status": "requested",
        "medication": "Hydrochlorothiazide 25mg",
        "pharmacy": pharmacy,
    }
    assert await data_of(registry, deps, state, REFILL, refill("hydrochlorothiazide", True)) == (
        expected
    )
    # The same request again, in the same call, is still the one request.
    assert await data_of(registry, deps, state, REFILL, refill("hydrochlorothiazide", True)) == (
        expected
    )
    # The summary's first 200 characters: 44 of name and pharmacy, then 156 of the p's.
    assert rows(owner_engine, REQUESTS) == [
        (
            state.call_id,
            "P901",
            "refill",
            "Hydrochlorothiazide 25mg at Corner Pharmacy " + "p" * 156,
        )
    ]


# --- R7, R8: confirmation and the request rows -----------------------------------------------


async def test_writes_need_confirmation(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    state = sarah()
    note = "Is it fine to take the new dose with food?"
    for confirmed in (None, False):
        assert await data_of(registry, deps, state, REFILL, refill("lisinopril", confirmed)) == {
            "status": "needs_confirmation",
            **LISINOPRIL,
        }
        assert await data_of(registry, deps, state, MESSAGE, message(note, confirmed)) == {
            "status": "needs_confirmation",
            "message": note,
        }
    # Only a real true confirms; text or a number is refused.
    for value in ("true", "yes", 1):
        for name, arguments in ((REFILL, refill("lisinopril")), (MESSAGE, message(note))):
            assert await call(registry, deps, state, name, {**arguments, "confirmed": value}) == (
                REFUSED
            ), (name, value)
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, MESSAGES) == []

    assert await data_of(registry, deps, state, REFILL, refill("lisinopril", True)) == {
        "status": "requested",
        **LISINOPRIL,
    }
    assert (await data_of(registry, deps, state, MESSAGE, message(note, True)))["status"] == "sent"
    assert [row[2] for row in rows(owner_engine, REQUESTS)] == ["refill", "doctor_message"]
    assert len(rows(owner_engine, MESSAGES)) == 1


async def test_requests_are_recorded_once(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    before = database_now(owner_engine)
    state = sarah()
    note = "Please call me back about my labs"

    first = await data_of(registry, deps, state, REFILL, refill("lisinopril", True))
    # The same medication again, said another way, in the same call.
    again = await data_of(registry, deps, state, REFILL, refill("Lisinopril 10 mg tablets", True))
    assert first == again == {"status": "requested", **LISINOPRIL}
    # Another medication is another request.
    assert await data_of(registry, deps, state, REFILL, refill("metformin", True)) == {
        "status": "sent_to_doctor",
        **METFORMIN,
    }
    sent = await data_of(registry, deps, state, MESSAGE, message(note, True))
    resent = await data_of(registry, deps, state, MESSAGE, message(note, True))
    assert sent == resent
    assert sent["status"] == "sent"
    assert isinstance(sent["message_id"], int)

    # A later call is a new request.
    later = sarah()
    assert await data_of(registry, deps, later, REFILL, refill("lisinopril", True)) == first
    after = database_now(owner_engine)

    assert rows(owner_engine, REQUESTS) == [
        (state.call_id, "P001", "refill", "Lisinopril 10mg at CVS Main St"),
        (state.call_id, "P001", "refill", "Metformin 500mg at CVS Main St"),
        (state.call_id, "P001", "doctor_message", note),
        (later.call_id, "P001", "refill", "Lisinopril 10mg at CVS Main St"),
    ]
    assert rows(owner_engine, MESSAGES) == [
        (state.call_id, "P001", None, None, "doctor_message", note)
    ]
    assert rows(owner_engine, text("SELECT id FROM messages")) == [(sent["message_id"],)]
    for (created_at,) in rows(owner_engine, text("SELECT created_at FROM requests")):
        assert before <= created_at <= after


# --- R9 to R11: the read tools ---------------------------------------------------------------


async def test_lab_status_only(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    assert await data_of(registry, deps, sarah(), LAB) == {"lab_status": SARAH_LABS}
    assert await data_of(registry, deps, michael(), LAB) == {"lab_status": "Pending"}
    assert await data_of(registry, deps, serving("P003", "Maria"), LAB) == {
        "lab_status": "No recent labs"
    }
    # There is no results field anywhere: the status is all the clinic stores.
    columns = text(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND column_name LIKE '%result%'"
    )
    assert rows(owner_engine, columns) == [("patients", "lab_results_status")]


async def test_balance_format(registry: Registry, deps: Deps, seeded: None) -> None:
    for state, expected in (
        (sarah(), "45.00"),
        (michael(), "0.00"),
        (serving("P003", "Maria"), "120.50"),
    ):
        assert await data_of(registry, deps, state, BALANCE) == {"balance": expected}


async def test_next_appointment_is_in_the_future(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    # Sarah's seeded appointment is 2026-12-03 10:30 AM clinic time, 15:30 UTC. Add a past
    # one, a later one, a cancelled one before it, and one before it that is stored after it,
    # so storage order is not time order.
    for starts_at, status in (
        (datetime(2026, 11, 20, 15, 0, tzinfo=UTC), "scheduled"),
        (datetime(2026, 12, 10, 14, 0, tzinfo=UTC), "scheduled"),
        (datetime(2026, 12, 1, 14, 0, tzinfo=UTC), "cancelled"),
        (datetime(2026, 11, 30, 14, 0, tzinfo=UTC), "scheduled"),
    ):
        execute(
            owner_engine,
            INSERT_APPOINTMENT,
            patient_id="P001",
            doctor="Dr. Lee",
            starts_at=starts_at,
            status=status,
        )
    at_appointment = datetime(2026, 12, 3, 15, 30, tzinfo=UTC)
    expected = [
        (T0, "Monday, November 30 at 9:00 AM with Dr. Lee"),
        (datetime(2026, 11, 30, 14, 0, 1, tzinfo=UTC), SARAH_APPOINTMENT),
        # Exactly at "now" counts as next.
        (at_appointment, SARAH_APPOINTMENT),
        (
            at_appointment + timedelta(microseconds=1),
            "Thursday, December 10 at 9:00 AM with Dr. Lee",
        ),
        (datetime(2026, 12, 10, 14, 0, 1, tzinfo=UTC), None),
    ]
    for now, appointment in expected:
        assert await data_of(registry, deps, sarah(now), APPOINTMENT) == {
            "appointment": appointment
        }, now

    # Rendered in clinic time: 19:00 UTC on 28 November is 2:00 PM there.
    assert await data_of(registry, deps, serving("P003", "Maria"), APPOINTMENT) == {
        "appointment": "Saturday, November 28 at 2:00 PM with Dr. Lee"
    }
    assert await data_of(registry, deps, michael(), APPOINTMENT) == {"appointment": None}


# --- R12: the gate ---------------------------------------------------------------------------


async def test_serving_tools_are_gated(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    by_name = {item.name: item for item in registry.tools}
    declared = {
        name: (
            by_name[name].phi,
            by_name[name].phases,
            by_name[name].office_open_only,
            by_name[name].writes,
            by_name[name].may_verify,
            by_name[name].audit_action.value,
            by_name[name].resource_type.value,
        )
        for name in SERVING_TOOLS
    }
    serving_only = frozenset({Phase.SERVING})
    assert declared == {
        APPOINTMENT: (True, serving_only, True, False, False, "appointment_read", "patient"),
        REFILL: (True, serving_only, True, True, False, "refill_requested", "patient"),
        LAB: (True, serving_only, True, False, False, "lab_status_read", "patient"),
        BALANCE: (True, serving_only, True, False, False, "balance_read", "patient"),
        MESSAGE: (True, serving_only, True, True, False, "doctor_message_sent", "patient"),
    }

    # Offered only to a verified caller, with the office open, in serving.
    for phase in Phase:
        for verified in (False, True):
            for office_open in (False, True):
                state = forced(phase, verified=verified, office_open=office_open)
                expected = (
                    SERVING_TOOLS
                    if (phase, verified, office_open)
                    == (
                        Phase.SERVING,
                        True,
                        True,
                    )
                    else []
                )
                assert offered(registry, state) == expected, (phase, verified, office_open)
                if verified:
                    flag_only = state.model_copy(update={"patient_id": None})
                    assert offered(registry, flag_only) == [], phase

    # And refused, writing nothing, in every state a call reaches outside that.
    outside = [
        start(),
        intake(),
        intake(office_open=False),
        move(intake(), Phase.BOOKING),
        move(sarah(), Phase.BOOKING),
        move(sarah(), Phase.CLOSING),
        move(sarah(), Phase.HANDOFF),
        move(sarah(), Phase.EMERGENCY),
        move(sarah(), Phase.ENDED),
        forced(Phase.SERVING, verified=True, office_open=False),
    ]
    arguments = {REFILL: refill("lisinopril", True), MESSAGE: message("hello", True)}
    for state in outside:
        for name in SERVING_TOOLS:
            assert await call(registry, deps, state, name, arguments.get(name)) == REFUSED, (
                state.phase,
                name,
            )
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, MESSAGES) == []
    assert {row[0] for row in rows(owner_engine, EVENTS)} == {"tool_refused"}


# --- R13: the work and its event commit together ---------------------------------------------


class AuditThatFailsAfterWriting(AuditLog):
    """The real append, then a failure, for a tool call's own event only."""

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        seq = await super().append(session, event, now=now)
        if event.outcome is Outcome.SUCCESS:
            raise RuntimeError("the audit failed after writing")
        return seq


async def test_tool_and_audit_commit_together(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    failing = Deps(
        sessions=deps.sessions,
        audit=AuditThatFailsAfterWriting(SecretStr(TEST_AUDIT_KEY)),
        now=deps.now,
        claim_keys=deps.claim_keys,
    )
    state = sarah()
    after, outcome = await registry.dispatch(state, REFILL, refill("lisinopril", True), failing)
    assert (after.tool_errors, outcome) == (1, FAILED)
    after, outcome = await registry.dispatch(after, MESSAGE, message("call me", True), failing)
    assert (after.tool_errors, outcome) == (2, FAILED.model_copy(update={"hand_off": True}))
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, MESSAGES) == []
    assert [row[0] for row in rows(owner_engine, EVENTS)] == ["tool_failed", "tool_failed"]

    # A database error in the middle of the write: in this test's database only, a constraint
    # that refuses a message's request row, after its message row went in. Generic failure,
    # neither row written, counted toward hand-off.
    execute(
        owner_engine,
        text(
            "ALTER TABLE requests ADD CONSTRAINT ck_requests_test_refuses"
            " CHECK (type <> 'doctor_message')"
        ),
    )
    other = michael()
    after, outcome = await registry.dispatch(other, MESSAGE, message("call me", True), deps)
    assert (after.tool_errors, outcome) == (1, FAILED)
    assert rows(owner_engine, MESSAGES) == []
    assert rows(owner_engine, REQUESTS) == []

    # And a success commits both.
    done = sarah()
    assert (await data_of(registry, deps, done, REFILL, refill("lisinopril", True)))[
        "status"
    ] == "requested"
    assert rows(owner_engine, REQUESTS) == [
        (done.call_id, "P001", "refill", "Lisinopril 10mg at CVS Main St")
    ]
    assert rows(owner_engine, EVENTS)[-1] == event("refill_requested", done)


# --- R14, R15: message bounds, and no text in the audit log ----------------------------------


async def test_message_bounds(
    registry: Registry,
    deps: Deps,
    seeded: None,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    state = sarah()
    # The model is told the bounds.
    (described,) = [item for item in registry.tools_for(state) if item.name == MESSAGE]
    assert described.parameters["properties"]["summary_short"] == {
        "maxLength": 120,
        "minLength": 1,
        "title": "Summary Short",
        "type": "string",
    }

    too_long = (MARKER + " ") * 5 + "x" * (121 - len(MARKER + " ") * 5)
    assert len(too_long) == 121
    # 120 characters said, 122 once the nine digits are masked as ***-**-****.
    grows = "x" * 110 + " " + "9" * 3 + "8" * 2 + "7" * 4
    assert len(grows) == 120
    # Control characters inside the text (C0, NUL included, and DEL) are refused.
    control = [
        "hi\x00there",
        f"{MARKER}\x00",
        "hi\x07there",
        "line one\nline two",
        "tab\there",
        "hi\x1fthere",
        "hi\x7fthere",
    ]
    for body in (too_long, grows, "", "   ", "\n\t", *control):
        for confirmed in (None, True):
            outcome = await call(registry, deps, state, MESSAGE, message(body, confirmed))
            assert outcome == REFUSED, repr(body)
            assert MARKER not in outcome.model_dump_json()
    assert MARKER not in caplog.text
    assert rows(owner_engine, MESSAGES) == []
    assert rows(owner_engine, REQUESTS) == []

    # The bound is on the text once trimmed: 125 characters said, 115 kept.
    for said, kept in (
        (" " * 10 + "a" * 115, "a" * 115),
        ("\n  hello there  \n", "hello there"),
        ("\t" + "b" * 120 + "\r\n", "b" * 120),
    ):
        assert await data_of(registry, deps, state, MESSAGE, message(said)) == {
            "status": "needs_confirmation",
            "message": kept,
        }, repr(said)

    longest = "y" * 120
    assert await data_of(registry, deps, state, MESSAGE, message(longest)) == {
        "status": "needs_confirmation",
        "message": longest,
    }
    assert (await data_of(registry, deps, state, MESSAGE, message(longest, True)))["status"] == (
        "sent"
    )
    assert [row[5] for row in rows(owner_engine, MESSAGES)] == [longest]

    # The database holds the same bounds.
    insert_message = text(
        "INSERT INTO messages (call_id, patient_id, body, kind)"
        " VALUES (:call_id, 'P001', :body, 'doctor_message')"
    )
    for body in ("z" * 121, ""):
        with pytest.raises(IntegrityError) as excinfo:
            execute(owner_engine, insert_message, call_id=new_call_id(), body=body)
        assert isinstance(excinfo.value.orig, errors.CheckViolation)
        assert excinfo.value.orig.diag.constraint_name == "ck_messages_body_length"


async def test_audit_carries_no_text(
    registry: Registry,
    deps: Deps,
    seeded: None,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    state = sarah()
    note = f"{MARKER} about the Lisinopril dose"
    for name, arguments in (
        (APPOINTMENT, None),
        (REFILL, refill("lisinopril", True)),
        (REFILL, refill(MARKER)),
        (LAB, None),
        (BALANCE, None),
        (MESSAGE, message(note)),
        (MESSAGE, message(note, True)),
    ):
        assert (await call(registry, deps, state, name, arguments)).status is Status.DONE, name

    stored = rows(owner_engine, EVENTS)
    assert stored == [
        event("appointment_read", state),
        event("refill_requested", state),
        event("refill_requested", state),
        event("lab_status_read", state),
        event("balance_read", state),
        event("doctor_message_sent", state),
        event("doctor_message_sent", state),
    ]
    secrets_said = (MARKER, "Lisinopril", "CVS", "45.00", SARAH_LABS, "December", "Patel")
    for row in stored:
        for value in row:
            for said in secrets_said:
                assert said not in str(value)
    for said in secrets_said:
        assert said not in caplog.text


# --- Neighbours ------------------------------------------------------------------------------


async def test_ssn_shaped_numbers_in_a_message_are_masked(
    registry: Registry,
    deps: Deps,
    seeded: None,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    state = sarah()
    ssn_shaped = "-".join(["9" * 3, "8" * 2, "7" * 4])
    nine_digits = "9" * 3 + "8" * 2 + "7" * 4
    cases = [
        (f"my number is {ssn_shaped} please call", "my number is ***-**-**** please call"),
        (f"it is {nine_digits}", "it is ***-**-****"),
        ("my ssn last four is 3344", "my ssn last four is ****"),
        # Phone numbers and dates are what staff need, and are kept.
        ("call me at 555-201-3344 after 2026-12-03", "call me at 555-201-3344 after 2026-12-03"),
    ]
    for said, stored in cases:
        assert await data_of(registry, deps, state, MESSAGE, message(said)) == {
            "status": "needs_confirmation",
            "message": stored,
        }
        assert (await data_of(registry, deps, state, MESSAGE, message(said, True)))[
            "status"
        ] == "sent"
    assert [row[5] for row in rows(owner_engine, MESSAGES)] == [stored for _, stored in cases]
    assert [row[3] for row in rows(owner_engine, REQUESTS)] == [stored for _, stored in cases]
    for digits in (ssn_shaped, nine_digits, "3344"):
        assert digits not in caplog.text


async def test_request_columns_are_constrained(owner_engine: Engine, seeded: None) -> None:
    insert_request = text(
        "INSERT INTO requests (call_id, patient_id, type, summary)"
        " VALUES (:call_id, 'P001', :type, :summary)"
    )
    for request_type, summary, constraint in (
        ("callback", "ok", "ck_requests_type"),
        ("refill", "s" * 201, "ck_requests_summary_length"),
        ("refill", "", "ck_requests_summary_length"),
    ):
        with pytest.raises(IntegrityError) as excinfo:
            execute(
                owner_engine,
                insert_request,
                call_id=new_call_id(),
                type=request_type,
                summary=summary,
            )
        assert isinstance(excinfo.value.orig, errors.CheckViolation)
        assert excinfo.value.orig.diag.constraint_name == constraint

    insert_message = text(
        "INSERT INTO messages (call_id, body, kind) VALUES (:call_id, 'hello', 'after_hours')"
    )
    with pytest.raises(IntegrityError) as excinfo:
        execute(owner_engine, insert_message, call_id=new_call_id())
    assert isinstance(excinfo.value.orig, errors.CheckViolation)
    assert excinfo.value.orig.diag.constraint_name == "ck_messages_kind"
