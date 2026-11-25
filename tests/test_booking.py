"""Booking: a new patient's details, open slots, and the appointment, for a verified patient and
for a first-time caller. Every expectation is written by hand from the seed data
(app/db/seed_data.json, seeded for Tuesday 24 November 2026) and the spec, and every call goes
through the application registry's dispatch, as the model's would."""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import (
    TEST_AUDIT_KEY,
    TEST_IDENTITY_KEY,
    assert_refused_by_privileges,
    attempt,
)
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent.claims import ClaimKeys
from app.agent.state import (
    CallState,
    FailedAttempt,
    IdentityClaim,
    IllegalTransitionError,
    Phase,
    apply,
    move,
    new_call,
    verify,
)
from app.agent.tool_registry import Deps, Registry, Status, ToolOutcome
from app.agent.tools import register_tools
from app.audit.log import AuditLog, Event, Outcome
from app.config import StartupSettings
from app.db.roles import runtime_role_differences
from app.db.seed import load_seed_data, seed
from app.db.session import create_session_factory
from app.guard.input import GuardAction, guard_input
from app.runtime_config import taking_calls
from app.voice.calls import CallRegistry

pytestmark = pytest.mark.anyio

EASTERN = ZoneInfo("America/New_York")
# Tuesday 24 November 2026, 10:00 in clinic time.
T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
SEED_DAY = date(2026, 11, 24)

START = "start_new_patient"
COLLECT = "collect_new_patient"
FIND = "find_slots"
BOOK = "book_appointment"
BOOK_NEW = "book_new_patient"
BOOKING_TOOLS = [START, COLLECT, FIND, BOOK, BOOK_NEW]
SERVING_TOOLS = [
    "get_next_appointment",
    "request_refill",
    "get_lab_status",
    "get_balance",
    "send_message_to_doctor",
]

REFUSED = ToolOutcome(
    status=Status.REFUSED, message="That is not available right now.", data={}, hand_off=False
)


def not_valid(outcome: ToolOutcome) -> bool:
    """A refusal for arguments that are not valid: nothing ran, nothing is returned, and
    the model is told to ask the caller for the details."""
    told = outcome.message or ""
    fixed = outcome.model_copy(update={"message": REFUSED.message})
    return fixed == REFUSED and told.startswith("Some details are missing or not valid")


FAILED = ToolOutcome(
    status=Status.FAILED, message="That could not be done right now.", data={}, hand_off=False
)
SLOT_TAKEN = ToolOutcome(
    status=Status.DONE, message=None, data={"status": "slot_taken"}, hand_off=False
)

DETAILS: dict[str, Any] = {
    "full_name": "Ann Example",
    "dob": "1990-02-03",
    "phone": "(555) 410-2233",
    "email": "ann.example@example.org",
    "self_pay": True,
    "reason_short": "Annual physical",
}
ANN_EMAIL_SPELLED = "A N N dot E X A M P L E at E X A M P L E dot O R G"

SLOTS = text("SELECT id, doctor, starts_at, is_available, source FROM available_slots ORDER BY id")
SLOT_ID = text("SELECT id FROM available_slots WHERE doctor = :doctor AND starts_at = :starts_at")
OPEN = text("SELECT is_available FROM available_slots WHERE id = :id")
APPOINTMENTS = text(
    "SELECT patient_id, slot_id, doctor, starts_at, reason_short, status FROM appointments"
    " WHERE slot_id IS NOT NULL ORDER BY id"
)
ALL_APPOINTMENTS = text("SELECT count(*) FROM appointments")
REQUESTS = text("SELECT call_id, patient_id, type, summary FROM requests ORDER BY id")
NEW_PATIENTS = text(
    "SELECT id, first_name, last_name, dob, ssn_last4, phone, email, insurance_provider,"
    " member_id, self_pay, lab_results_status, balance_due, is_new, possible_duplicate"
    " FROM patients WHERE id NOT LIKE 'P0%' ORDER BY created_at, id"
)
PATIENT_COUNT = text("SELECT count(*) FROM patients")
SEEDED_PATIENTS = text("SELECT id, possible_duplicate, is_new FROM patients WHERE id LIKE 'P0%'")
EVENTS = text(
    "SELECT action, resource_type, resource_id, outcome, call_id, actor, reason"
    " FROM audit_events ORDER BY seq"
)
NEW_PATIENT_ID = re.compile(
    r"NP-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time at the clinic in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=EASTERN)


def new_call_id() -> str:
    call_id = CallRegistry(max_calls=1, max_seconds=1).reserve()
    assert call_id is not None
    return call_id


def start(office_open: bool = True) -> CallState:
    return new_call(call_id=new_call_id(), started_at=T0, office_open=office_open)


def intake(office_open: bool = True) -> CallState:
    return move(start(office_open), Phase.INTAKE)


def serving(patient_id: str, first_name: str) -> CallState:
    return verify(intake(), patient_id=patient_id, first_name=first_name)


def sarah() -> CallState:
    return serving("P001", "Sarah")


def michael() -> CallState:
    return serving("P002", "Michael")


def rows(engine: Engine, query: Any, **params: Any) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(query, params)]


def execute(engine: Engine, statement: Any, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(statement, params)


def slot_id(engine: Engine, doctor: str, starts_at: datetime) -> int:
    [(found,)] = rows(engine, SLOT_ID, doctor=doctor, starts_at=starts_at)
    assert isinstance(found, int)
    return found


def is_open(engine: Engine, slot: int) -> bool:
    [(available,)] = rows(engine, OPEN, id=slot)
    assert isinstance(available, bool)
    return available


def clocked(deps: Deps, now: datetime) -> Deps:
    return Deps(
        sessions=deps.sessions,
        audit=deps.audit,
        now=lambda: now,
        claim_keys=deps.claim_keys,
        config=deps.config,
    )


def booking_args(slot: int, confirmed: bool | None = None, **extra: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"slot_id": slot, **extra}
    if confirmed is not None:
        arguments["confirmed"] = confirmed
    return arguments


def book(slot: int, confirmed: bool | None = None, reason: str = "Knee pain") -> dict[str, Any]:
    return booking_args(slot, confirmed, reason_short=reason)


def booked(doctor: str, day: str, clock: str) -> dict[str, str]:
    return {"status": "booked", "doctor": doctor, "date": day, "time": clock}


def preview(doctor: str, day: str, clock: str) -> dict[str, str]:
    return {"status": "needs_confirmation", "doctor": doctor, "date": day, "time": clock}


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
        config=taking_calls(),
    )


@pytest.fixture
async def seeded(owner_session: AsyncSession) -> None:
    """The task's synthetic patients and slots, as the start-up seed writes them on 24 November
    2026: the task's slots, then Dr. Patel at 9:30, 11:00 and 3:00 and Dr. Lee at 10:00 and
    1:30 on the next 14 business days."""
    await seed(owner_session, load_seed_data(), SEED_DAY)
    await owner_session.commit()


async def run(
    registry: Registry, deps: Deps, state: CallState, name: str, arguments: object = None
) -> tuple[CallState, ToolOutcome]:
    return await registry.dispatch(state, name, {} if arguments is None else arguments, deps)


async def done(
    registry: Registry, deps: Deps, state: CallState, name: str, arguments: object = None
) -> tuple[CallState, dict[str, Any]]:
    after, outcome = await run(registry, deps, state, name, arguments)
    assert outcome.status is Status.DONE, (name, outcome)
    return after, outcome.data


async def new_patient(
    registry: Registry, deps: Deps, details: dict[str, Any] | None = None
) -> CallState:
    """A first-time caller in booking with their details collected."""
    state, data = await done(registry, deps, intake(), START)
    assert data == {"status": "started"}
    state, _ = await done(registry, deps, state, COLLECT, DETAILS if details is None else details)
    return state


def offered(registry: Registry, state: CallState) -> list[str]:
    return [item.name for item in registry.tools_for(state) if item.name in BOOKING_TOOLS]


# --- R6: find_slots ----------------------------------------------------------------------


async def test_find_slots(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    patel, lee = "Dr. Patel", "Dr. Lee"
    wednesday, thursday, friday = (
        "Wednesday, November 25",
        "Thursday, November 26",
        "Friday, November 27",
    )
    cases: list[tuple[dict[str, Any], list[tuple[str, datetime, str, str]]]] = [
        (
            {},
            [
                (patel, at(11, 25, 9, 30), wednesday, "9:30 AM"),
                (lee, at(11, 25, 10), wednesday, "10:00 AM"),
                (patel, at(11, 25, 11), wednesday, "11:00 AM"),
            ],
        ),
        (
            {"doctor": lee},
            [
                (lee, at(11, 25, 10), wednesday, "10:00 AM"),
                (lee, at(11, 25, 13, 30), wednesday, "1:30 PM"),
                (lee, at(11, 26, 10), thursday, "10:00 AM"),
            ],
        ),
        (
            {"preferred_days": ["Friday"]},
            [
                (patel, at(11, 27, 9, 30), friday, "9:30 AM"),
                (lee, at(11, 27, 10), friday, "10:00 AM"),
                (patel, at(11, 27, 11), friday, "11:00 AM"),
            ],
        ),
        (
            {"preferred_time_of_day": "afternoon"},
            [
                (lee, at(11, 25, 13, 30), wednesday, "1:30 PM"),
                (patel, at(11, 25, 15), wednesday, "3:00 PM"),
                (lee, at(11, 26, 13, 30), thursday, "1:30 PM"),
            ],
        ),
        # The task's own slots on Friday 4 December: 9:00 AM and 2:30 PM with Dr. Patel.
        (
            {"doctor": patel, "preferred_days": ["Friday"], "preferred_time_of_day": "afternoon"},
            [
                (patel, at(11, 27, 15), friday, "3:00 PM"),
                (patel, at(12, 4, 14, 30), "Friday, December 4", "2:30 PM"),
                (patel, at(12, 4, 15), "Friday, December 4", "3:00 PM"),
            ],
        ),
        (
            {"doctor": patel, "preferred_days": ["Friday"], "preferred_time_of_day": "morning"},
            [
                (patel, at(11, 27, 9, 30), friday, "9:30 AM"),
                (patel, at(11, 27, 11), friday, "11:00 AM"),
                (patel, at(12, 4, 9), "Friday, December 4", "9:00 AM"),
            ],
        ),
        (
            {"doctor": lee, "preferred_days": ["Monday"], "preferred_time_of_day": "morning"},
            [
                (lee, at(11, 30, 10), "Monday, November 30", "10:00 AM"),
                (lee, at(12, 7, 10), "Monday, December 7", "10:00 AM"),
                (lee, at(12, 14, 10), "Monday, December 14", "10:00 AM"),
            ],
        ),
        (
            {"preferred_days": ["Monday", "Thursday"], "preferred_time_of_day": "morning"},
            [
                (patel, at(11, 26, 9, 30), thursday, "9:30 AM"),
                (lee, at(11, 26, 10), thursday, "10:00 AM"),
                (patel, at(11, 26, 11), thursday, "11:00 AM"),
            ],
        ),
        # Nothing matches: an empty list.
        ({"preferred_days": ["Saturday", "Sunday"]}, []),
    ]

    def listed(expected: list[tuple[str, datetime, str, str]]) -> dict[str, Any]:
        return {
            "slots": [
                {
                    "slot_id": slot_id(owner_engine, doctor, starts_at),
                    "doctor": doctor,
                    "date": day,
                    "time": clock,
                }
                for doctor, starts_at, day, clock in expected
            ]
        }

    state = sarah()
    for arguments, expected in cases:
        state, data = await done(registry, deps, state, FIND, arguments)
        assert data == listed(expected), arguments
    # Every id it returned is remembered, once each, in the order first offered.
    first_offered = [
        slot_id(owner_engine, doctor, starts_at)
        for _arguments, expected in cases
        for doctor, starts_at, _day, _clock in expected
    ]
    assert state.offered_slots == tuple(dict.fromkeys(first_offered))

    # Only future slots: one starting at the moment of the question is not offered.
    _, data = await done(registry, clocked(deps, at(11, 25, 10)), sarah(), FIND)
    assert data == listed(
        [
            (patel, at(11, 25, 11), wednesday, "11:00 AM"),
            (lee, at(11, 25, 13, 30), wednesday, "1:30 PM"),
            (patel, at(11, 25, 15), wednesday, "3:00 PM"),
        ]
    )

    # Only open slots.
    taken = slot_id(owner_engine, patel, at(11, 25, 11))
    execute(
        owner_engine,
        text("UPDATE available_slots SET is_available = false WHERE id = :id"),
        id=taken,
    )
    _, data = await done(registry, deps, sarah(), FIND)
    assert data == listed(
        [
            (patel, at(11, 25, 9, 30), wednesday, "9:30 AM"),
            (lee, at(11, 25, 10), wednesday, "10:00 AM"),
            (lee, at(11, 25, 13, 30), wednesday, "1:30 PM"),
        ]
    )

    # The doctor list is fixed, and so are the day names and the times of day.
    for arguments in (
        {"doctor": "Dr. Smith"},
        {"doctor": "Patel"},
        {"preferred_days": ["Funday"]},
        {"preferred_days": ["friday"]},
        {"preferred_days": "Friday"},
        {"preferred_time_of_day": "evening"},
        {"date": "2026-11-25"},
    ):
        assert not_valid((await run(registry, deps, sarah(), FIND, arguments))[1]), arguments


# --- R2: only an open, offered, future slot --------------------------------------------------


async def test_only_an_open_offered_future_slot_books(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    second = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    third = slot_id(owner_engine, "Dr. Patel", at(11, 25, 11))
    never_offered = slot_id(owner_engine, "Dr. Lee", at(11, 25, 13, 30))
    state, _ = await done(registry, deps, sarah(), FIND)
    other, _ = await done(registry, deps, michael(), FIND)
    assert state.offered_slots == other.offered_slots == (first, second, third)

    # Michael takes the first.
    _, data = await done(registry, deps, other, BOOK, book(first, True))
    assert data == booked("Dr. Patel", "Wednesday, November 25", "9:30 AM")

    # Taken, never offered in this call, no such slot, and offered but now begun or past:
    # all the same to the caller, asked or confirmed.
    later = clocked(deps, at(11, 25, 10))
    attempts: list[tuple[Deps, int]] = [
        (deps, first),
        (deps, never_offered),
        (deps, 999_999),
        (later, second),
        (clocked(deps, at(11, 25, 10, 1)), second),
    ]
    for when, slot in attempts:
        for confirmed in (None, False, True):
            outcome = (await run(registry, when, state, BOOK, book(slot, confirmed)))[1]
            assert outcome == SLOT_TAKEN, (slot, confirmed)

    # A new patient is held to the same rule.
    newcomer, _ = await done(registry, deps, await new_patient(registry, deps), FIND)
    assert newcomer.offered_slots == (second, third, never_offered)
    # Open and in the future, but never offered to this call.
    open_elsewhere = slot_id(owner_engine, "Dr. Patel", at(11, 25, 15))
    for when, slot in [(deps, first), (deps, open_elsewhere), (deps, 999_999), (later, second)]:
        for confirmed in (None, True):
            outcome = (
                await run(registry, when, newcomer, BOOK_NEW, booking_args(slot, confirmed))
            )[1]
            assert outcome == SLOT_TAKEN, (slot, confirmed)

    # Nothing was written for any of them.
    assert rows(owner_engine, APPOINTMENTS) == [
        ("P002", first, "Dr. Patel", at(11, 25, 9, 30), "Knee pain", "scheduled")
    ]
    assert [row[2] for row in rows(owner_engine, REQUESTS)] == ["booking"]
    assert rows(owner_engine, PATIENT_COUNT) == [(5,)]
    for slot in (second, third, never_offered, open_elsewhere):
        assert is_open(owner_engine, slot)

    # And an open, offered, future slot books.
    _, data = await done(registry, deps, state, BOOK, book(third, True))
    assert data == booked("Dr. Patel", "Wednesday, November 25", "11:00 AM")


# --- R3: confirmation ------------------------------------------------------------------------


async def test_booking_needs_confirmation(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    second = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    state, _ = await done(registry, deps, sarah(), FIND)
    newcomer, _ = await done(registry, deps, await new_patient(registry, deps), FIND)

    for confirmed in (None, False):
        after, data = await done(registry, deps, state, BOOK, book(first, confirmed))
        assert data == preview("Dr. Patel", "Wednesday, November 25", "9:30 AM")
        assert after.phase is Phase.SERVING
        after, data = await done(
            registry, deps, newcomer, BOOK_NEW, booking_args(second, confirmed)
        )
        assert data == preview("Dr. Lee", "Wednesday, November 25", "10:00 AM")
        assert after.phase is Phase.BOOKING
    # Only a real true confirms.
    for value in ("true", "yes", 1):
        outcome = (await run(registry, deps, state, BOOK, {**book(first), "confirmed": value}))[1]
        assert not_valid(outcome), value
        confirming = {**booking_args(second), "confirmed": value}
        assert not_valid((await run(registry, deps, newcomer, BOOK_NEW, confirming))[1]), value

    assert rows(owner_engine, APPOINTMENTS) == []
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, PATIENT_COUNT) == [(5,)]
    assert is_open(owner_engine, first) and is_open(owner_engine, second)

    _, data = await done(registry, deps, state, BOOK, book(first, True))
    assert data == booked("Dr. Patel", "Wednesday, November 25", "9:30 AM")
    _, data = await done(registry, deps, newcomer, BOOK_NEW, booking_args(second, True))
    assert data == booked("Dr. Lee", "Wednesday, November 25", "10:00 AM")
    assert not is_open(owner_engine, first) and not is_open(owner_engine, second)


# --- R4: one transaction ---------------------------------------------------------------------


class AuditThatFailsAfterWriting(AuditLog):
    """The real append, then a failure, for a tool call's own event only."""

    async def append(self, session: AsyncSession, event: Event, *, now: datetime) -> int:
        seq = await super().append(session, event, now=now)
        if event.outcome is Outcome.SUCCESS:
            raise RuntimeError("the audit failed after writing")
        return seq


async def test_booking_is_one_transaction(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    second = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    state, _ = await done(registry, deps, sarah(), FIND)
    newcomer, _ = await done(registry, deps, await new_patient(registry, deps), FIND)
    failing = Deps(
        sessions=deps.sessions,
        audit=AuditThatFailsAfterWriting(SecretStr(TEST_AUDIT_KEY)),
        now=deps.now,
        claim_keys=deps.claim_keys,
        config=deps.config,
    )
    before = rows(owner_engine, EVENTS)

    # The slot was taken and the rows written before the audit failed: none of it stays.
    after, outcome = await run(registry, failing, state, BOOK, book(first, True))
    assert (after.tool_errors, outcome) == (1, FAILED)
    after, outcome = await run(registry, failing, newcomer, BOOK_NEW, booking_args(second, True))
    assert (after.tool_errors, after.phase, outcome) == (1, Phase.BOOKING, FAILED)

    # A database error after the slot, the patient and the appointment: in this test's
    # database only, a constraint that refuses a booking's request row.
    execute(
        owner_engine,
        text(
            "ALTER TABLE requests ADD CONSTRAINT ck_requests_test_refuses CHECK (type <> 'booking')"
        ),
    )
    after, outcome = await run(registry, deps, state, BOOK, book(first, True))
    assert (after.tool_errors, outcome) == (1, FAILED)
    after, outcome = await run(registry, deps, newcomer, BOOK_NEW, booking_args(second, True))
    assert (after.tool_errors, after.phase, outcome) == (1, Phase.BOOKING, FAILED)

    assert is_open(owner_engine, first) and is_open(owner_engine, second)
    assert rows(owner_engine, APPOINTMENTS) == []
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, PATIENT_COUNT) == [(5,)]
    assert [row[0] for row in rows(owner_engine, EVENTS)[len(before) :]] == ["tool_failed"] * 4

    # A success commits them all together, with the event.
    execute(owner_engine, text("ALTER TABLE requests DROP CONSTRAINT ck_requests_test_refuses"))
    _, data = await done(registry, deps, state, BOOK, book(first, True))
    assert data["status"] == "booked"
    assert not is_open(owner_engine, first)
    assert rows(owner_engine, APPOINTMENTS) == [
        ("P001", first, "Dr. Patel", at(11, 25, 9, 30), "Knee pain", "scheduled")
    ]
    assert rows(owner_engine, REQUESTS) == [
        (
            state.call_id,
            "P001",
            "booking",
            "Dr. Patel on Wednesday, November 25, 2026 at 9:30 AM; confirm by phone",
        )
    ]
    assert rows(owner_engine, EVENTS)[-1] == (
        "appointment_booked",
        "patient",
        "P001",
        "success",
        state.call_id,
        "agent",
        None,
    )


# --- the confirmation email, simulated ----------------------------------------------------------

BY_ID = text("SELECT id FROM appointments ORDER BY id")


def simulated_emails(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("email_simulated")
    ]


async def test_confirmation_email_is_simulated_once_the_booking_is_saved(
    registry: Registry,
    deps: Deps,
    seeded: None,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    second = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    state, _ = await done(registry, deps, sarah(), FIND)
    newcomer, _ = await done(registry, deps, await new_patient(registry, deps), FIND)

    # A preview saves nothing, and a booking that fails saves nothing: no email for either.
    await done(registry, deps, newcomer, BOOK_NEW, booking_args(second, False))
    execute(
        owner_engine,
        text(
            "ALTER TABLE requests ADD CONSTRAINT ck_requests_test_refuses CHECK (type <> 'booking')"
        ),
    )
    assert (await run(registry, deps, newcomer, BOOK_NEW, booking_args(second, True)))[1] == FAILED
    execute(owner_engine, text("ALTER TABLE requests DROP CONSTRAINT ck_requests_test_refuses"))
    assert simulated_emails(caplog) == []

    # A patient with no email on file is confirmed by phone: no email.
    await done(registry, deps, state, BOOK, book(first, True))
    assert simulated_emails(caplog) == []
    assert rows(owner_engine, REQUESTS)[-1][3].endswith("; confirm by phone")

    # A new patient gave an email: once the booking is saved, one line, naming the appointment
    # and the call and nothing of the caller.
    await done(registry, deps, newcomer, BOOK_NEW, booking_args(second, True))
    [*_, (appointment,)] = rows(owner_engine, BY_ID)
    assert simulated_emails(caplog) == [
        f"email_simulated kind=booking_confirmation appointment_id={appointment}"
        f" call_id={newcomer.call_id}"
    ]
    assert rows(owner_engine, REQUESTS)[-1][3].endswith("; confirmation email simulated")
    assert "@" not in caplog.text

    # A patient with an email on file gets one too.
    execute(owner_engine, text("UPDATE patients SET email = 'p001@example.org' WHERE id = 'P001'"))
    third = next(slot for slot in state.offered_slots if slot not in (first, second))
    await done(registry, deps, state, BOOK, book(third, True))
    [*_, (latest,)] = rows(owner_engine, BY_ID)
    assert simulated_emails(caplog)[1:] == [
        f"email_simulated kind=booking_confirmation appointment_id={latest} call_id={state.call_id}"
    ]
    assert "@" not in caplog.text


# --- R5: the slot row is kept ----------------------------------------------------------------


async def test_booked_slot_is_kept_and_hidden(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine, runtime: AsyncEngine
) -> None:
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    before = rows(owner_engine, SLOTS)
    state, _ = await done(registry, deps, sarah(), FIND)
    await done(registry, deps, state, BOOK, book(first, True))

    # The same rows, the booked one marked unavailable and nothing else changed.
    assert rows(owner_engine, SLOTS) == [
        (row[0], row[1], row[2], row[0] != first, row[4]) for row in before
    ]
    expected = {
        "slots": [
            {
                "slot_id": slot_id(owner_engine, "Dr. Lee", at(11, 25, 10)),
                "doctor": "Dr. Lee",
                "date": "Wednesday, November 25",
                "time": "10:00 AM",
            },
            {
                "slot_id": slot_id(owner_engine, "Dr. Patel", at(11, 25, 11)),
                "doctor": "Dr. Patel",
                "date": "Wednesday, November 25",
                "time": "11:00 AM",
            },
            {
                "slot_id": slot_id(owner_engine, "Dr. Lee", at(11, 25, 13, 30)),
                "doctor": "Dr. Lee",
                "date": "Wednesday, November 25",
                "time": "1:30 PM",
            },
        ]
    }
    # Never offered again: to the same call, another, or a new patient.
    for asking in (state, michael(), await new_patient(registry, deps)):
        assert (await done(registry, deps, asking, FIND))[1] == expected
    patel_wednesday_mornings = {
        "doctor": "Dr. Patel",
        "preferred_days": ["Wednesday"],
        "preferred_time_of_day": "morning",
    }
    _, data = await done(registry, deps, michael(), FIND, patel_wednesday_mornings)
    assert [item["slot_id"] for item in data["slots"]] == [
        slot_id(owner_engine, "Dr. Patel", at(11, 25, 11)),
        slot_id(owner_engine, "Dr. Patel", at(12, 2, 9, 30)),
        slot_id(owner_engine, "Dr. Patel", at(12, 2, 11)),
    ]
    # The application cannot remove it.
    await assert_refused_by_privileges(runtime, "DELETE FROM available_slots WHERE false")


# --- R7: a possible duplicate ----------------------------------------------------------------


async def test_duplicate_is_flagged_and_invisible(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    patel_first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    lee = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    patel_second = slot_id(owner_engine, "Dr. Patel", at(11, 25, 11))
    callers: list[tuple[dict[str, Any], int, dict[str, str]]] = [
        # Sarah Johnson's name and date of birth (P001), as a first-time caller gives them.
        (
            {**DETAILS, "full_name": "Sarah Johnson", "dob": "1985-03-14"},
            patel_first,
            booked("Dr. Patel", "Wednesday, November 25", "9:30 AM"),
        ),
        # Nobody's.
        (DETAILS, lee, booked("Dr. Lee", "Wednesday, November 25", "10:00 AM")),
        # The same name spaced another way is the same name.
        (
            {**DETAILS, "full_name": "SARAH JOHN SON", "dob": "1985-03-14"},
            patel_second,
            booked("Dr. Patel", "Wednesday, November 25", "11:00 AM"),
        ),
    ]
    outcomes = []
    for details, slot, expected in callers:
        state, _ = await done(registry, deps, await new_patient(registry, deps, details), FIND)
        after, outcome = await run(registry, deps, state, BOOK_NEW, booking_args(slot, True))
        assert outcome.data == expected
        assert after.phase is Phase.CLOSING
        outcomes.append(outcome)
    # The same answer, line and hand-off whether or not a record matched.
    assert {(item.status, item.message, item.hand_off, item.script) for item in outcomes} == {
        (Status.DONE, None, False, "booking_instructions")
    }

    created = rows(owner_engine, NEW_PATIENTS)
    assert [row[1:] for row in created] == [
        (
            "Sarah",
            "Johnson",
            date(1985, 3, 14),
            None,
            "555-410-2233",
            "ann.example@example.org",
            None,
            None,
            True,
            "None on file",
            0,
            True,
            True,
        ),
        (
            "Ann",
            "Example",
            date(1990, 2, 3),
            None,
            "555-410-2233",
            "ann.example@example.org",
            None,
            None,
            True,
            "None on file",
            0,
            True,
            False,
        ),
        (
            "SARAH JOHN",
            "SON",
            date(1985, 3, 14),
            None,
            "555-410-2233",
            "ann.example@example.org",
            None,
            None,
            True,
            "None on file",
            0,
            True,
            True,
        ),
    ]
    assert all(NEW_PATIENT_ID.fullmatch(row[0]) for row in created)
    assert len({row[0] for row in created}) == 3
    # The existing record is untouched.
    assert sorted(rows(owner_engine, SEEDED_PATIENTS)) == [
        (f"P00{number}", False, False) for number in range(1, 6)
    ]
    # Each new patient has their appointment, and the events are alike.
    assert [(row[0], row[1]) for row in rows(owner_engine, APPOINTMENTS)] == [
        (created[0][0], patel_first),
        (created[1][0], lee),
        (created[2][0], patel_second),
    ]
    booked_events = [row for row in rows(owner_engine, EVENTS) if row[0] == "new_patient_booked"]
    assert [(row[1], row[3], row[5], row[6]) for row in booked_events] == [
        ("appointment", "success", "agent", None)
    ] * 3


# --- R8: the details are bounded -------------------------------------------------------------


async def test_new_patient_details_are_bounded(
    registry: Registry, deps: Deps, seeded: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    local_part = "a" * (254 - len("@example.org"))
    grows = "x" * 110 + " " + "9" * 3 + "8" * 2 + "7" * 4
    insured = {"self_pay": False, "insurance_provider": "Acme Health", "member_id": "AH-5521"}
    without_self_pay = {key: value for key, value in DETAILS.items() if key != "self_pay"}
    # Each row: the arguments, and what is stored (the draft's fields), or None when refused.
    accepted: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (DETAILS, {}),
        ({**DETAILS, **insured}, {**insured}),
        ({**DETAILS, "phone": "555-410-2233"}, {}),
        ({**DETAILS, "phone": "555.410.2233"}, {}),
        ({**DETAILS, "phone": "5554102233"}, {}),
        ({**DETAILS, "phone": " 555 410 2233 "}, {}),
        ({**DETAILS, "dob": "2026-11-24"}, {"dob": date(2026, 11, 24)}),
        ({**DETAILS, "dob": "1900-01-01"}, {"dob": date(1900, 1, 1)}),
        ({**DETAILS, "email": local_part + "@example.org"}, {"email": local_part + "@example.org"}),
        (
            {**DETAILS, "email": "a.b-c_d+e%f@mail-1.example.co"},
            {"email": "a.b-c_d+e%f@mail-1.example.co"},
        ),
        ({**DETAILS, "reason_short": "y" * 120}, {"reason_short": "y" * 120}),
        ({**DETAILS, "reason_short": "  Annual physical \n"}, {}),
        (
            {**DETAILS, "reason_short": "my number is " + "-".join(["9" * 3, "8" * 2, "7" * 4])},
            {"reason_short": "my number is ***-**-****"},
        ),
        (
            {**DETAILS, "full_name": "Ann Marie Example"},
            {"first_name": "Ann Marie", "last_name": "Example"},
        ),
        (
            {**DETAILS, "full_name": "  O'Brien-Smith  Ann  "},
            {"first_name": "O'Brien-Smith", "last_name": "Ann"},
        ),
        # Letters with accents, and digits where text may hold them.
        ({**DETAILS, "full_name": "Zoë Ångström"}, {"first_name": "Zoë", "last_name": "Ångström"}),
        (
            {**DETAILS, "reason_short": "Knee pain for 2 weeks"},
            {"reason_short": "Knee pain for 2 weeks"},
        ),
        (
            {**DETAILS, **insured, "insurance_provider": "Acme Health 2"},
            {**insured, "insurance_provider": "Acme Health 2"},
        ),
    ]
    stored_default = {
        "first_name": "Ann",
        "last_name": "Example",
        "dob": date(1990, 2, 3),
        "phone": "555-410-2233",
        "email": "ann.example@example.org",
        "insurance_provider": None,
        "member_id": None,
        "self_pay": True,
        "reason_short": "Annual physical",
    }
    refused: list[dict[str, Any]] = [
        # Date of birth: strict ISO, a real date, not in the future.
        *[
            {**DETAILS, "dob": dob}
            for dob in (
                "1990-2-3",
                "02/03/1990",
                "1990-02-03T00:00:00",
                # ISO 8601 forms other than YYYY-MM-DD, which Python's own parser accepts.
                "19900203",
                "1990-W05-6",
                19900203,
                "1990-13-01",
                "2026-11-25",
                "",
                # Not before 1900.
                "1899-12-31",
                "0001-01-01",
            )
        ],
        # Phone: exactly ten digits once separators are removed.
        *[
            {**DETAILS, "phone": phone}
            for phone in (
                "555-410-223",
                "1-555-410-2233",
                "555-410-22334",
                "555-410-ABCD",
                "+1 555 410 2233",
                "",
                5554102233,
            )
        ],
        # Email: one @, a dot in the domain, at most 254 characters.
        *[
            {**DETAILS, "email": email}
            for email in (
                "ann.example.org",
                "ann@@example.org",
                "ann@example@org",
                "ann@example",
                "a" + local_part + "@example.org",
                "ann @example.org",
                "@example.org",
                "ann@.org",
                "ann@example..org",
                "",
            )
        ],
        # The reason: 1 to 120 characters once trimmed and masked, no control characters.
        *[
            {**DETAILS, "reason_short": reason}
            for reason in ("", "   ", "z" * 121, grows, "line one\nline two", "tab\there")
        ],
        # Exactly one of insurance (both given) or self-pay.
        {**DETAILS, **insured, "self_pay": True},
        {**DETAILS, "self_pay": False},
        {**DETAILS, "self_pay": False, "insurance_provider": "Acme Health"},
        {**DETAILS, "self_pay": False, "member_id": "AH-5521"},
        {**DETAILS, "member_id": "AH-5521"},
        without_self_pay,
        {**DETAILS, "self_pay": "true"},
        {**DETAILS, **insured, "member_id": ""},
        {**DETAILS, **insured, "member_id": "-".join(["123", "45", "6789"])},
        {**DETAILS, **insured, "member_id": "***-**-****"},
        {**DETAILS, **insured, "member_id": "AH/5521"},
        {**DETAILS, **insured, "insurance_provider": "Acme " + "".join(["123", "45", "6789"])},
        # A name: with a name key, two words at least, no digits.
        *[
            {**DETAILS, "full_name": name}
            for name in ("", "   ", "Ann", "Dr.", "Ann 9 Example", "李 小龙", "x" * 201)
        ],
        # Unicode digits in a name; control and format characters (a bidi override, a
        # zero-width space, a soft hyphen, a line separator) in any text.
        *[
            {**DETAILS, "full_name": name}
            for name in (
                "Ann ٣ Example",
                "Ann Example²",
                "Ann Ex‮elpma",
                "Ann Exa​mple",
                "Ann Ex­ample",
                "Ann Example",
            )
        ],
        *[
            {**DETAILS, "reason_short": reason}
            for reason in ("Knee ‮pain", "Knee​ pain", "Knee \u0085pain")
        ],
        {**DETAILS, **insured, "insurance_provider": "Acme​ Health"},
        {**DETAILS, **insured, "insurance_provider": "Acme ‮Health"},
        # Nothing else can be collected.
        {**DETAILS, "allergies": "none"},
        {**DETAILS, "address": "1 Main St"},
        {**DETAILS, "patient_id": "P001"},
        {**DETAILS, "ssn": "-".join(["123", "45", "6789"])},
    ]
    state, _ = await done(registry, deps, intake(), START)
    for arguments in refused:
        after, outcome = await run(registry, deps, state, COLLECT, arguments)
        assert not_valid(outcome), arguments
        assert after.new_patient_details is None
    for arguments, stored in accepted:
        after, data = await done(registry, deps, state, COLLECT, arguments)
        draft = after.new_patient_details
        assert draft is not None, arguments
        assert {name: getattr(draft, name) for name in stored_default} == {
            **stored_default,
            **stored,
        }, arguments
    _, data = await done(registry, deps, state, COLLECT, DETAILS)
    assert data == {"email_spelled": ANN_EMAIL_SPELLED}
    _, data = await done(
        registry, deps, state, COLLECT, {**DETAILS, "email": "a.b-c_d+e%f@mail-1.example.co"}
    )
    assert data == {
        "email_spelled": "A dot B dash C underscore D plus E percent F at M A I L dash 1 dot"
        " E X A M P L E dot C O"
    }
    assert "Example" not in caplog.text


# --- R9: the draft stays hidden --------------------------------------------------------------


async def test_draft_stays_hidden(
    registry: Registry,
    deps: Deps,
    seeded: None,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    details = {
        "full_name": "Zelda Markerton",
        "dob": "1977-07-17",
        "phone": "555-717-4242",
        "email": "zelda.markerton@example.net",
        "self_pay": False,
        "insurance_provider": "Quillfeather Mutual",
        "member_id": "QM-ZZ-4242",
        "reason_short": "quillfeather knee pain",
    }
    secrets_said = (
        "Zelda",
        "Markerton",
        "markerton",
        "1977",
        "4242",
        "Quillfeather",
        "quillfeather",
    )
    started, _ = await done(registry, deps, intake(), START)
    state, _ = await done(registry, deps, started, COLLECT, details)
    assert state.new_patient_details is not None

    texts = [
        repr(state),
        str(state),
        repr(state.new_patient_details),
        str(state.new_patient_details),
        state.model_dump_json(),
        json.dumps(state.model_dump(mode="json")),
        repr(state.model_dump()),
    ]
    # Errors about a state that holds the draft show none of it.
    with pytest.raises(ValidationError) as invalid:
        CallState.model_validate({**dict(state), "tool_errors": -1})
    texts.append(str(invalid.value))
    with pytest.raises(ValidationError) as invalid:
        CallState.model_validate({**dict(state), "phase": Phase.SERVING})
    texts.append(str(invalid.value))
    with pytest.raises(IllegalTransitionError) as illegal:
        move(state, Phase.SERVING)
    texts.append(str(illegal.value))
    with pytest.raises(ValidationError) as invalid:
        type(state.new_patient_details).model_validate(
            {**dict(state.new_patient_details), "self_pay": "Zelda"}
        )
    texts.append(str(invalid.value))

    # A booking that fails, and one that succeeds.
    slot = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    offered_state, _ = await done(registry, deps, state, FIND)
    execute(
        owner_engine,
        text(
            "ALTER TABLE requests ADD CONSTRAINT ck_requests_test_refuses CHECK (type <> 'booking')"
        ),
    )
    assert (await run(registry, deps, offered_state, BOOK_NEW, booking_args(slot, True)))[
        1
    ] == FAILED
    execute(owner_engine, text("ALTER TABLE requests DROP CONSTRAINT ck_requests_test_refuses"))
    after, outcome = await run(registry, deps, offered_state, BOOK_NEW, booking_args(slot, True))
    assert outcome.data == booked("Dr. Patel", "Wednesday, November 25", "9:30 AM")
    assert after.new_patient_details is None
    texts += [repr(after), after.model_dump_json(), caplog.text]

    stored_events = rows(owner_engine, EVENTS)
    assert [row[:4] for row in stored_events] == [
        ("new_patient_started", "tool", START, "success"),
        ("new_patient_details_collected", "tool", COLLECT, "success"),
        ("slots_offered", "tool", FIND, "success"),
        ("tool_failed", "tool", BOOK_NEW, "failure"),
        ("new_patient_booked", "appointment", stored_events[-1][2], "success"),
    ]
    texts += [str(row) for row in stored_events]
    # What staff read: the request names the slot and how to confirm, nothing of the caller.
    [(_, _, request_type, summary)] = rows(owner_engine, REQUESTS)
    assert (request_type, summary) == (
        "booking",
        "Dr. Patel on Wednesday, November 25, 2026 at 9:30 AM; confirmation email simulated",
    )
    texts.append(summary)

    for text_seen in texts:
        for said in secrets_said:
            assert said not in text_seen, (said, text_seen[:80])


# --- R10, R11: the gate ----------------------------------------------------------------------


async def test_booking_tools_are_gated(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    by_name = {item.name: item for item in registry.tools}
    declared = {
        name: (
            by_name[name].phi,
            by_name[name].phases,
            # Office open only: no phase while it is closed.
            not by_name[name].closed_phases,
            by_name[name].writes,
            by_name[name].may_verify,
            by_name[name].model_callable,
            by_name[name].audit_action.value,
            by_name[name].resource_type.value,
        )
        for name in BOOKING_TOOLS
    }
    intake_only, booking_only = frozenset({Phase.INTAKE}), frozenset({Phase.BOOKING})
    assert declared == {
        START: (False, intake_only, True, False, False, True, "new_patient_started", "tool"),
        COLLECT: (
            False,
            booking_only,
            True,
            False,
            False,
            True,
            "new_patient_details_collected",
            "tool",
        ),
        FIND: (
            False,
            frozenset({Phase.SERVING, Phase.BOOKING}),
            True,
            False,
            False,
            True,
            "slots_offered",
            "tool",
        ),
        BOOK: (
            True,
            frozenset({Phase.SERVING}),
            True,
            True,
            False,
            True,
            "appointment_booked",
            "patient",
        ),
        BOOK_NEW: (
            False,
            booking_only,
            True,
            True,
            False,
            True,
            "new_patient_booked",
            "appointment",
        ),
    }
    # Neither booking tool takes anything that says whose record it is.
    assert list(by_name[BOOK].arguments.model_fields) == ["slot_id", "reason_short", "confirmed"]
    assert list(by_name[BOOK_NEW].arguments.model_fields) == ["slot_id", "confirmed"]

    started, _ = await done(registry, deps, intake(), START)
    assert (started.phase, started.is_new_patient, started.verified) == (Phase.BOOKING, True, False)
    with_draft, _ = await done(registry, deps, started, COLLECT, DETAILS)
    patient = sarah()
    cases: list[tuple[CallState, list[str]]] = [
        (start(), []),
        (intake(), [START]),
        (intake(office_open=False), []),
        (patient, [FIND, BOOK]),
        (started, [COLLECT]),
        (with_draft, [COLLECT, FIND, BOOK_NEW]),
        # A verified caller may look for slots in booking and books from serving.
        (move(patient, Phase.BOOKING), [FIND]),
        (move(intake(office_open=False), Phase.BOOKING), []),
        *[
            (move(with_draft, phase), [])
            for phase in (Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY, Phase.ENDED)
        ],
        *[
            (move(patient, phase), [])
            for phase in (Phase.CLOSING, Phase.HANDOFF, Phase.EMERGENCY, Phase.ENDED)
        ],
        (patient.model_copy(update={"office_open": False}), []),
    ]
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    arguments = {
        START: {},
        COLLECT: DETAILS,
        FIND: {},
        BOOK: book(first, True),
        BOOK_NEW: booking_args(first, True),
    }
    for state, expected in cases:
        assert offered(registry, state) == expected, (state.phase, state.verified, expected)
        for name in BOOKING_TOOLS:
            if name not in expected:
                assert (await run(registry, deps, state, name, arguments[name]))[1] == REFUSED, (
                    state.phase,
                    name,
                )

    # No patient identifier is taken, whatever it is called.
    offered_patient, _ = await done(registry, deps, patient, FIND)
    offered_new, _ = await done(registry, deps, with_draft, FIND)
    for extra in (
        {"patient_id": "P002"},
        {"patient": "P002"},
        {"dob": "1972-11-08"},
        {"ssn_last4": "1020"},
    ):
        named = {**book(first, True), **extra}
        assert not_valid((await run(registry, deps, offered_patient, BOOK, named))[1])
        named = {**booking_args(first, True), **extra}
        assert not_valid((await run(registry, deps, offered_new, BOOK_NEW, named))[1])
    assert rows(owner_engine, APPOINTMENTS) == []

    # Each books for its own caller: the verified patient's record, or the new one's.
    second = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    await done(registry, deps, offered_patient, BOOK, book(first, True))
    offered_michael, _ = await done(registry, deps, michael(), FIND)
    await done(registry, deps, offered_michael, BOOK, book(second, True))
    third = slot_id(owner_engine, "Dr. Patel", at(11, 25, 11))
    await done(registry, deps, offered_new, BOOK_NEW, booking_args(third, True))
    [(new_id, *_)] = rows(owner_engine, NEW_PATIENTS)
    assert [(row[0], row[1]) for row in rows(owner_engine, APPOINTMENTS)] == [
        ("P001", first),
        ("P002", second),
        (new_id, third),
    ]


async def test_new_patient_call_never_serves(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    started, _ = await done(registry, deps, intake(), START)
    with_draft, _ = await done(registry, deps, started, COLLECT, DETAILS)
    offered_state, _ = await done(registry, deps, with_draft, FIND)
    slot = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    closing, _ = await done(registry, deps, offered_state, BOOK_NEW, booking_args(slot, True))
    states = [
        started,
        with_draft,
        offered_state,
        closing,
        *[move(with_draft, phase) for phase in (Phase.HANDOFF, Phase.EMERGENCY, Phase.ENDED)],
    ]
    for state in states:
        assert state.is_new_patient and not state.verified
        assert [item.name for item in registry.tools_for(state) if item.name in SERVING_TOOLS] == []
        for name in SERVING_TOOLS:
            assert (await run(registry, deps, state, name))[1] == REFUSED, (state.phase, name)
        # Nothing makes it a verified call.
        with pytest.raises(IllegalTransitionError):
            verify(state, patient_id="P001", first_name="Sarah")
        with pytest.raises(IllegalTransitionError):
            move(state, Phase.SERVING)
    # Not even a state written by hand.
    verified = {"verified": True, "patient_id": "P001", "patient_first_name": "Sarah"}
    for phase in (Phase.SERVING, Phase.BOOKING, Phase.CLOSING):
        with pytest.raises(ValidationError):
            CallState.model_validate({**dict(started), **verified, "phase": phase})
    # Nor does a verified caller get a new patient's tools.
    in_booking = move(sarah(), Phase.BOOKING)
    assert (await run(registry, deps, in_booking, COLLECT, DETAILS))[1] == REFUSED


# --- R12: the runtime role -------------------------------------------------------------------


async def test_runtime_role_booking_privileges(
    database: StartupSettings, runtime: AsyncEngine, owner_engine: Engine
) -> None:
    # The one column of a slot it may change.
    assert (
        await attempt(runtime, "UPDATE available_slots SET is_available = false WHERE false")
        is None
    )
    # The names below come from this test, not from input.
    for column in ("doctor", "starts_at", "source", "id"):
        statement = f"UPDATE available_slots SET {column} = {column} WHERE false"  # noqa: S608
        await assert_refused_by_privileges(runtime, statement)
    # New patients and appointments are added, never changed or removed.
    assert await attempt(runtime, "INSERT INTO patients (id) SELECT NULL WHERE false") is None
    assert (
        await attempt(runtime, "INSERT INTO appointments (doctor) SELECT NULL WHERE false") is None
    )
    for table, column in (
        ("patients", "possible_duplicate"),
        ("patients", "phone"),
        ("appointments", "status"),
        ("appointments", "slot_id"),
    ):
        statement = f"UPDATE {table} SET {column} = {column} WHERE false"  # noqa: S608
        await assert_refused_by_privileges(runtime, statement)
    for table in ("patients", "appointments", "available_slots"):
        await assert_refused_by_privileges(runtime, f"DELETE FROM {table} WHERE false")  # noqa: S608

    # The comparison set-up runs catches a column grant beyond the declaration, and a missing one.
    role = f'"{database.app_db_user}"'
    with owner_engine.begin() as connection:
        connection.execute(text(f"GRANT UPDATE (doctor) ON available_slots TO {role}"))
        connection.execute(text(f"REVOKE UPDATE (is_available) ON available_slots FROM {role}"))
    with owner_engine.connect() as connection:
        assert runtime_role_differences(connection, database.app_db_user) == [
            "column available_slots.doctor: not declared",
            "column available_slots.is_available: declared but missing",
            "table column public.available_slots.doctor: UPDATE granted to the runtime role",
        ]


# --- R13: the repeat rule --------------------------------------------------------------------


async def test_repeat_booking_writes_once(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    second = slot_id(owner_engine, "Dr. Lee", at(11, 25, 10))
    expected = booked("Dr. Patel", "Wednesday, November 25", "9:30 AM")
    state, _ = await done(registry, deps, sarah(), FIND)
    # The booking, then the same again (said with another reason), then asked without a
    # confirmation: the same answer each time.
    for arguments in (book(first, True), book(first, True, reason="Said again"), book(first)):
        assert (await done(registry, deps, state, BOOK, arguments))[1] == expected, arguments
    # A verified patient may book more than one.
    _, data = await done(registry, deps, state, BOOK, book(second, True))
    assert data == booked("Dr. Lee", "Wednesday, November 25", "10:00 AM")
    # Another call by the same patient is not a repeat: for it, the slot is taken. (The slot is
    # set as offered by hand, since no call is offered a taken slot.)
    later = sarah().model_copy(update={"offered_slots": (first,)})
    assert (await run(registry, deps, later, BOOK, book(first, True)))[1] == SLOT_TAKEN
    # The audit trail shows one booking per slot, and each repeat as a repeat.
    assert [row[0] for row in rows(owner_engine, EVENTS) if row[4] == state.call_id] == [
        "slots_offered",
        "appointment_booked",
        "booking_repeated",
        "booking_repeated",
        "appointment_booked",
    ]

    assert rows(owner_engine, APPOINTMENTS) == [
        ("P001", first, "Dr. Patel", at(11, 25, 9, 30), "Knee pain", "scheduled"),
        ("P001", second, "Dr. Lee", at(11, 25, 10), "Knee pain", "scheduled"),
    ]
    assert [row[2:] for row in rows(owner_engine, REQUESTS)] == [
        ("booking", "Dr. Patel on Wednesday, November 25, 2026 at 9:30 AM; confirm by phone"),
        ("booking", "Dr. Lee on Wednesday, November 25, 2026 at 10:00 AM; confirm by phone"),
    ]

    # A new patient books once: the call moves on, and asking again is refused.
    third = slot_id(owner_engine, "Dr. Patel", at(11, 25, 11))
    newcomer, _ = await done(registry, deps, await new_patient(registry, deps), FIND)
    closing, data = await done(registry, deps, newcomer, BOOK_NEW, booking_args(third, True))
    assert data == booked("Dr. Patel", "Wednesday, November 25", "11:00 AM")
    assert (await run(registry, deps, closing, BOOK_NEW, booking_args(third, True)))[1] == REFUSED
    assert len(rows(owner_engine, NEW_PATIENTS)) == 1
    assert len(rows(owner_engine, APPOINTMENTS)) == 3


# --- R14: office closed ----------------------------------------------------------------------


async def test_no_booking_when_closed(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    with_draft = await new_patient(registry, deps)
    first = slot_id(owner_engine, "Dr. Patel", at(11, 25, 9, 30))
    closed = [
        intake(office_open=False),
        move(intake(office_open=False), Phase.BOOKING),
        sarah().model_copy(update={"office_open": False, "offered_slots": (first,)}),
        with_draft.model_copy(update={"office_open": False, "offered_slots": (first,)}),
    ]
    arguments = {
        START: {},
        COLLECT: DETAILS,
        FIND: {},
        BOOK: book(first, True),
        BOOK_NEW: booking_args(first, True),
    }
    before = rows(owner_engine, PATIENT_COUNT)
    for state in closed:
        assert offered(registry, state) == [], state.phase
        for name in BOOKING_TOOLS:
            assert (await run(registry, deps, state, name, arguments[name]))[1] == REFUSED, (
                state.phase,
                name,
            )
    assert rows(owner_engine, APPOINTMENTS) == []
    assert rows(owner_engine, REQUESTS) == []
    assert rows(owner_engine, PATIENT_COUNT) == before
    assert is_open(owner_engine, first)


# --- R1: two callers, one slot ---------------------------------------------------------------

ROUNDS = 20


async def test_concurrent_booking_gives_one_winner(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    first, second = sarah(), michael()
    won = {"P001": 0, "P002": 0}
    for round_number in range(ROUNDS):
        first, offered_first = await done(registry, deps, first, FIND)
        second, offered_second = await done(registry, deps, second, FIND)
        target = offered_first["slots"][0]
        assert offered_second["slots"][0] == target
        slot = target["slot_id"]
        # Two sessions of the runtime role, each in its own dispatch, run at once.
        (first, one), (second, other) = await asyncio.gather(
            registry.dispatch(first, BOOK, book(slot, True), deps),
            registry.dispatch(second, BOOK, book(slot, True), deps),
        )
        expected_win = {
            "status": "booked",
            "doctor": target["doctor"],
            "date": target["date"],
            "time": target["time"],
        }
        answers = sorted([one.data, other.data], key=lambda data: data["status"])
        assert answers == [expected_win, {"status": "slot_taken"}], round_number
        assert one.status is other.status is Status.DONE
        winner = "P001" if one.data["status"] == "booked" else "P002"
        won[winner] += 1
        assert [row[0] for row in rows(owner_engine, APPOINTMENTS) if row[1] == slot] == [winner], (
            round_number
        )
        assert not is_open(owner_engine, slot)
    assert len(rows(owner_engine, APPOINTMENTS)) == ROUNDS
    assert len(rows(owner_engine, REQUESTS)) == ROUNDS
    assert sum(won.values()) == ROUNDS
    # The earliest slot went first, and no dispatch failed.
    assert rows(owner_engine, APPOINTMENTS)[0][3] == at(11, 25, 9, 30)
    assert first.tool_errors == second.tool_errors == 0


# --- Rework: smaller items ----------------------------------------------------------------------


async def test_details_again_forget_the_offers(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    offered_state, data = await done(registry, deps, await new_patient(registry, deps), FIND)
    first = data["slots"][0]["slot_id"]
    assert len(offered_state.offered_slots) == 3
    # The email was spelled back wrongly: the details are given again.
    again, _ = await done(
        registry, deps, offered_state, COLLECT, {**DETAILS, "phone": "5554102299"}
    )
    assert again.offered_slots == ()
    draft = again.new_patient_details
    assert draft is not None
    assert draft.phone == "555-410-2299"
    # A slot offered before the new details is not offered now.
    assert (await run(registry, deps, again, BOOK_NEW, booking_args(first, True)))[1] == SLOT_TAKEN
    assert is_open(owner_engine, first)


async def test_find_slots_boundaries(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    # Slots of this test's own, after every seeded one: noon on a Wednesday, 11:30 PM on a
    # Monday (already Tuesday in UTC), and two after the March clock change (EDT, UTC-4).
    inserted = [
        ("Dr. Lee", at(12, 16, 12)),
        ("Dr. Lee", at(12, 21, 23, 30)),
        ("Dr. Patel", datetime(2027, 3, 15, 11, 30, tzinfo=EASTERN)),
        ("Dr. Patel", datetime(2027, 3, 15, 12, 0, tzinfo=EASTERN)),
    ]
    assert inserted[1][1].astimezone(UTC) == datetime(2026, 12, 22, 4, 30, tzinfo=UTC)
    assert inserted[2][1].astimezone(UTC) == datetime(2027, 3, 15, 15, 30, tzinfo=UTC)
    for doctor, starts_at in inserted:
        execute(
            owner_engine,
            text(
                "INSERT INTO available_slots (doctor, starts_at, source)"
                " VALUES (:doctor, :starts_at, 'task')"
            ),
            doctor=doctor,
            starts_at=starts_at,
        )
    noon, late, march_morning, march_noon = (
        {
            "slot_id": slot_id(owner_engine, doctor, starts_at),
            "doctor": doctor,
            "date": day,
            "time": clock,
        }
        for (doctor, starts_at), day, clock in zip(
            inserted,
            (
                "Wednesday, December 16",
                "Monday, December 21",
                "Monday, March 15",
                "Monday, March 15",
            ),
            ("12:00 PM", "11:30 PM", "11:30 AM", "12:00 PM"),
            strict=True,
        )
    )
    after_the_seed = clocked(deps, at(12, 15, 18))
    cases: list[tuple[dict[str, Any], list[dict[str, Any]]]] = [
        ({"preferred_time_of_day": "morning"}, [march_morning]),
        ({"preferred_time_of_day": "afternoon"}, [noon, late, march_noon]),
        ({"preferred_days": ["Monday"]}, [late, march_morning, march_noon]),
        ({"preferred_days": ["Tuesday"]}, []),
        ({"preferred_days": ["Wednesday"], "preferred_time_of_day": "morning"}, []),
    ]
    for arguments, expected in cases:
        _, data = await done(registry, after_the_seed, sarah(), FIND, arguments)
        assert data == {"slots": expected}, arguments


# --- Rework M1: the SSN step cannot be left for a booking ------------------------------------

# Last four digits as a caller might give them, built from pieces so no file holds them.
SAID_DIGITS = ["44" + "21", "4 4 2 1", "four four two one"]


async def test_armed_step_cannot_start_a_new_patient(
    registry: Registry, deps: Deps, seeded: None, owner_engine: Engine
) -> None:
    claim = {"full_name": "Ann Example", "dob": "1990-02-03"}
    claimed, _ = await done(registry, deps, intake(), "verify_identity", claim)
    assert (claimed.expecting_ssn4, claimed.pending_claim is not None) == (True, True)
    # Armed, then partly answered, then with the digits complete and the submit still to come.
    first_half = guard_input("four four", claimed).change
    assert first_half is not None
    partly = apply(claimed, first_half)
    second_half = guard_input("two one", partly).change
    assert second_half is not None
    complete = apply(partly, second_half)
    assert (partly.pending_ssn4, complete.expecting_ssn4) == ("44", False)
    for state in (claimed, partly, complete):
        assert START not in offered(registry, state)
        after, outcome = await run(registry, deps, state, START)
        assert outcome == REFUSED
        assert (after.phase, after.expecting_ssn4, after.pending_claim, after.pending_ssn4) == (
            Phase.INTAKE,
            state.expecting_ssn4,
            state.pending_claim,
            state.pending_ssn4,
        )
        with pytest.raises(IllegalTransitionError, match="claim"):
            move(state, Phase.BOOKING)
    # So the caller's digits are still taken by the step, never shown to the model.
    for said in SAID_DIGITS:
        decision = guard_input(said, claimed)
        assert (decision.action, decision.text) == (GuardAction.SWALLOW, None), said
    refused = ["tool_refused"] * 3
    assert [row[0] for row in rows(owner_engine, EVENTS)] == ["identity_claimed", *refused]


async def test_locked_call_cannot_start_a_new_patient(
    registry: Registry, deps: Deps, seeded: None
) -> None:
    claim = IdentityClaim(full_name="Ann Example", dob=date(1990, 2, 3))
    locked = intake()
    for _ in range(3):
        locked = apply(apply(locked, claim), FailedAttempt())
    assert (locked.verify_locked, locked.pending_claim) == (True, None)
    assert START not in offered(registry, locked)
    after, outcome = await run(registry, deps, locked, START)
    assert (outcome, after.phase, after.is_new_patient) == (REFUSED, Phase.INTAKE, False)
    with pytest.raises(IllegalTransitionError, match="locked"):
        move(locked, Phase.BOOKING)
    # Staff is still open to it.
    assert move(locked, Phase.HANDOFF).phase is Phase.HANDOFF
