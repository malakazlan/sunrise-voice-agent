"""The admin console's reads: live calls from the recorded events, the summaries of the
guardrails and of the calls' numbers, the appointments and the slots. Every expectation is
written by hand from the rows and events seeded here, at instants chosen by hand; the clinic's
clock is the simulated one the settings give."""

import ast
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY
from pydantic import SecretStr
from sqlalchemy import Engine, text
from test_admin_api import (
    INSERT_PATIENT,
    INSERT_SLOT,
    SIGNED_IN,
    admin_settings,
    cid,
    client_from,
    insert_turn,
)

from app.audit import console
from app.audit.log import Action, AuditLog, Event, Outcome, Reason, ResourceType
from app.config import StartupSettings
from app.db.session import create_database_engine, create_session_factory
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT = AuditLog(SecretStr(TEST_AUDIT_KEY))
# 15:00 on Tuesday 24 November 2026 at the clinic.
NOW = datetime(2026, 11, 24, 20, 0, tzinfo=UTC)


def at(text_: str) -> datetime:
    return datetime.fromisoformat(text_)


def append_at(database: StartupSettings, items: Sequence[tuple[Event, datetime]]) -> None:
    """Append events at the given instants, as the runtime role, through the audit log."""

    async def run() -> None:
        engine = create_database_engine(database)
        try:
            async with create_session_factory(engine)() as session:
                for item, instant in items:
                    await AUDIT.append(session, item, now=instant)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)


def call_event(action: Action, number: int, reason: Reason | None = None) -> Event:
    return Event(
        actor="agent",
        action=action,
        resource_type=ResourceType.CALL,
        outcome=Outcome.SUCCESS,
        call_id=cid(number),
        resource_id=cid(number),
        reason=reason,
    )


def event(
    action: Action,
    number: int,
    resource_type: ResourceType = ResourceType.CALL,
    reason: Reason | None = None,
) -> Event:
    return Event(
        actor="agent",
        action=action,
        resource_type=resource_type,
        outcome=Outcome.REFUSED,
        call_id=cid(number),
        reason=reason,
    )


def read(database: StartupSettings, route: str, now: datetime = NOW) -> Any:
    app = create_app(admin_settings(database, simulated_now=now.isoformat()))
    with client_from(app) as client:
        response = client.get(route, headers=SIGNED_IN)
    assert response.status_code == 200, response.text
    return response.json()


# --- R5 -----------------------------------------------------------------------------------

# The configured longest call (admin_settings leaves the default, 600 seconds) and 15 minutes.
WINDOW = timedelta(minutes=15, seconds=600)


def test_live_calls(owner_engine: Engine, database: StartupSettings) -> None:
    append_at(
        database,
        [
            # Started two minutes ago: live, with two turns.
            (call_event(Action.CALL_STARTED, 1), NOW - timedelta(minutes=2)),
            # Started and ended: not live.
            (call_event(Action.CALL_STARTED, 2), NOW - timedelta(minutes=3)),
            (call_event(Action.CALL_ENDED, 2, Reason.COMPLETED), NOW - timedelta(minutes=1)),
            # Started just past the window: stale, as a process that died mid-call leaves it.
            (call_event(Action.CALL_STARTED, 3), NOW - WINDOW - timedelta(seconds=1)),
            # Started just inside it, no turn yet.
            (call_event(Action.CALL_STARTED, 4), NOW - WINDOW + timedelta(seconds=1)),
        ],
    )
    with owner_engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
        insert_turn(connection, 1, 1, phase="intake", verified=False, patient_id=None)
        insert_turn(connection, 1, 2, total_ms=2200, last_tool="get_next_appointment")
        insert_turn(connection, 2, 1)

    no_turn = {
        "turn": None,
        "phase": None,
        "verified": None,
        "patient_id": None,
        "last_tool": None,
        "vad_ms": None,
        "stt_ms": None,
        "first_token_ms": None,
        "last_token_ms": None,
        "first_audio_ms": None,
        "total_ms": None,
    }
    assert read(database, "/admin/api/calls/live") == {
        "calls": [
            {
                "call_id": cid(1),
                "started_at": "2026-11-24T19:58:00+00:00",
                "turn": 2,
                "phase": "serving",
                "verified": True,
                "patient_id": "P001",
                "last_tool": "get_next_appointment",
                "vad_ms": 500,
                "stt_ms": 120,
                "first_token_ms": 300,
                "last_token_ms": 900,
                "first_audio_ms": 1500,
                "total_ms": 2200,
            },
            {"call_id": cid(4), "started_at": "2026-11-24T19:35:01+00:00", **no_turn},
        ]
    }
    # Two seconds later the fourth call ages out; once the window has passed the first too.
    later = read(database, "/admin/api/calls/live", NOW + timedelta(seconds=2))
    assert [call["call_id"] for call in later["calls"]] == [cid(1)]
    passed = NOW + WINDOW - timedelta(minutes=2) + timedelta(seconds=1)
    assert read(database, "/admin/api/calls/live", passed) == {"calls": []}

    # Nothing is read from the voice process: the routes and their reads import nothing of it.
    for module in ("app/api/admin.py", "app/audit/console.py"):
        tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))
        imported = [
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        imported += [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not [name for name in imported if name.startswith(("app.voice", "app.agent"))]


# --- R6 -----------------------------------------------------------------------------------


def test_guardrails_summary(owner_engine: Engine, database: StartupSettings) -> None:
    block = ResourceType.CALL
    append_at(
        database,
        [
            # 23:59 on the 22nd at the clinic: before the two days.
            (event(Action.GUARD_BLOCK, 1, block, Reason.MEDICAL_ADVICE), at("2026-11-23T04:59Z")),
            # Midnight on the 23rd at the clinic: the first instant of the two days.
            (event(Action.GUARD_BLOCK, 1, block, Reason.MEDICAL_ADVICE), at("2026-11-23T05:00Z")),
            # 23:30 on the 23rd at the clinic, though already the 24th in UTC.
            (event(Action.GUARD_BLOCK, 2, block, Reason.SSN_SHAPE), at("2026-11-24T04:30Z")),
            (event(Action.TOOL_REFUSED, 2, ResourceType.TOOL), at("2026-11-23T18:00Z")),
            (event(Action.TOOL_REFUSED, 2, ResourceType.TOOL), at("2026-11-23T18:01Z")),
            (event(Action.GUARD_BLOCK, 3, block, Reason.MEDICAL_ADVICE), at("2026-11-24T15:00Z")),
            (event(Action.GUARD_BLOCK, 3, block, Reason.MEDICAL_ADVICE), at("2026-11-24T15:01Z")),
            (event(Action.EMERGENCY_DETECTED, 3, block, Reason.GUARD), at("2026-11-24T15:10Z")),
            (event(Action.EMERGENCY_DETECTED, 4, block, Reason.MODEL), at("2026-11-24T15:20Z")),
            (event(Action.TOOL_REFUSED, 4, ResourceType.TOOL), at("2026-11-24T16:00Z")),
            (event(Action.IDENTITY_REFUSED, 4, ResourceType.PATIENT), at("2026-11-24T17:00Z")),
            (event(Action.IDENTITY_REFUSED, 4, ResourceType.PATIENT), at("2026-11-24T17:01Z")),
            (event(Action.IDENTITY_LOCKED, 4, ResourceType.PATIENT), at("2026-11-24T17:02Z")),
            # Not a guardrail.
            (call_event(Action.CALL_STARTED, 5), at("2026-11-24T15:00Z")),
            (event(Action.TOOL_FAILED, 5, ResourceType.TOOL), at("2026-11-24T15:00Z")),
        ],
    )
    day_23, day_24 = "2026-11-23T00:00:00-05:00", "2026-11-24T00:00:00-05:00"
    assert read(database, "/admin/api/guardrails/summary?days=2") == {
        "days": 2,
        "since": day_23,
        "counts": [
            {"day": day_23, "action": "guard_block", "reason": "medical_advice", "count": 1},
            {"day": day_23, "action": "guard_block", "reason": "ssn_shape", "count": 1},
            {"day": day_23, "action": "tool_refused", "reason": None, "count": 2},
            {"day": day_24, "action": "emergency_detected", "reason": "guard", "count": 1},
            {"day": day_24, "action": "emergency_detected", "reason": "model", "count": 1},
            {"day": day_24, "action": "guard_block", "reason": "medical_advice", "count": 2},
            {"day": day_24, "action": "identity_locked", "reason": None, "count": 1},
            {"day": day_24, "action": "identity_refused", "reason": None, "count": 2},
            {"day": day_24, "action": "tool_refused", "reason": None, "count": 1},
        ],
    }
    # The default is a week; today alone holds the 24th's counts.
    assert read(database, "/admin/api/guardrails/summary")["since"] == "2026-11-18T00:00:00-05:00"
    today = read(database, "/admin/api/guardrails/summary?days=1")
    assert {row["day"] for row in today["counts"]} == {day_24}


INSERT_ATTEMPT = text(
    "INSERT INTO verification_attempts (claim_key, call_id, success, created_at)"
    " VALUES (:claim_key, :call_id, :success, :created_at)"
)
STAGES = ("vad_ms", "stt_ms", "first_token_ms", "last_token_ms", "first_audio_ms", "total_ms")


def latency(kind: str, turns: int, figures: dict[str, tuple[int, int | None, int | None]]) -> Any:
    return [
        {"kind": kind, "turns": turns, "stage": stage, "values": n, "p50": p50, "p95": p95}
        for stage, (n, p50, p95) in figures.items()
    ]


def test_metrics_summary(owner_engine: Engine, database: StartupSettings) -> None:
    append_at(
        database,
        [
            (call_event(Action.CALL_STARTED, 9), at("2026-11-22T15:00Z")),
            (call_event(Action.CALL_ENDED, 9, Reason.ERROR), at("2026-11-22T15:05Z")),
            (call_event(Action.CALL_STARTED, 1), at("2026-11-23T15:00Z")),
            (call_event(Action.CALL_ENDED, 1, Reason.COMPLETED), at("2026-11-23T15:05Z")),
            (call_event(Action.CALL_STARTED, 2), at("2026-11-24T14:00Z")),
            (call_event(Action.CALL_ENDED, 2, Reason.TRANSFERRED), at("2026-11-24T14:05Z")),
            (call_event(Action.CALL_STARTED, 3), at("2026-11-24T15:00Z")),
            (call_event(Action.CALL_ENDED, 3, Reason.COMPLETED), at("2026-11-24T15:05Z")),
            # Calls 1 and 3 verified, call 1 twice; call 9's is before the two days.
            (event(Action.IDENTITY_VERIFIED, 9, ResourceType.PATIENT), at("2026-11-22T15:01Z")),
            (event(Action.IDENTITY_VERIFIED, 1, ResourceType.PATIENT), at("2026-11-23T15:01Z")),
            (event(Action.IDENTITY_VERIFIED, 1, ResourceType.PATIENT), at("2026-11-23T15:02Z")),
            (event(Action.IDENTITY_VERIFIED, 3, ResourceType.PATIENT), at("2026-11-24T15:01Z")),
        ],
    )
    claim_key = "ab" * 32
    in_window = at("2026-11-24T15:01Z")
    with owner_engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
        for success, created_at in (
            (True, at("2026-11-22T15:01Z")),
            (False, in_window),
            (False, in_window),
            (True, in_window),
        ):
            connection.execute(
                INSERT_ATTEMPT,
                {
                    "claim_key": claim_key,
                    "call_id": cid(1),
                    "success": success,
                    "created_at": created_at,
                },
            )
        # Before the two days: a turn that would change every figure.
        insert_turn(
            connection,
            9,
            1,
            at=at("2026-11-22T15:00Z"),
            tool_rounds=0,
            total_ms=99_999,
            record_us=99_999,
        )
        record_us = iter(range(10, 430, 10))
        # 20 plain turns of call 2: enough for figures, but for the 10 with a transcript time.
        for turn in range(1, 21):
            insert_turn(
                connection,
                2,
                turn,
                at=in_window,
                tool_rounds=0,
                last_tool=None,
                vad_ms=500,
                stt_ms=50 if turn <= 10 else None,
                first_token_ms=20 * turn,
                last_token_ms=40 * turn,
                first_audio_ms=100 * turn + 1000,
                total_ms=100 * turn,
                record_us=next(record_us),
            )
        # 19 tool turns of call 3: one short of figures.
        for turn in range(1, 20):
            insert_turn(
                connection,
                3,
                turn,
                at=in_window,
                stt_ms=None,
                first_token_ms=10,
                last_token_ms=50,
                first_audio_ms=2000,
                total_ms=3000,
                record_us=next(record_us),
            )
        # Three scripted turns of call 1, the model not asked; the last row written says
        # three rows had been dropped before it.
        for turn in range(1, 4):
            insert_turn(
                connection,
                1,
                turn,
                at=in_window,
                tool_rounds=0,
                last_tool=None,
                stt_ms=None,
                first_token_ms=None,
                last_token_ms=None,
                first_audio_ms=900,
                total_ms=400,
                record_us=next(record_us),
                dropped_before=3 if turn == 3 else 0,
            )

    # Percentiles interpolate between the values in order (Postgres's percentile_cont): of 20
    # values the median lies halfway between the 10th and 11th, the 95th percentile 5% of the
    # way from the 19th to the 20th; rounded to whole milliseconds.
    assert read(database, "/admin/api/metrics/summary?days=2") == {
        "days": 2,
        "since": "2026-11-23T00:00:00-05:00",
        "calls_per_day": [
            {"day": "2026-11-23T00:00:00-05:00", "calls": 1},
            {"day": "2026-11-24T00:00:00-05:00", "calls": 2},
        ],
        "ended": [{"reason": "completed", "calls": 2}, {"reason": "transferred", "calls": 1}],
        "verified_calls": 2,
        "verification_attempts": 3,
        "verification_successes": 1,
        "latency": [
            *latency(
                "scripted",
                3,
                {
                    "vad_ms": (3, None, None),
                    "stt_ms": (0, None, None),
                    "first_token_ms": (0, None, None),
                    "last_token_ms": (0, None, None),
                    "first_audio_ms": (3, None, None),
                    "total_ms": (3, None, None),
                },
            ),
            *latency(
                "plain",
                20,
                {
                    "vad_ms": (20, 500, 500),
                    "stt_ms": (10, None, None),
                    "first_token_ms": (20, 210, 381),
                    "last_token_ms": (20, 420, 762),
                    "first_audio_ms": (20, 2050, 2905),
                    "total_ms": (20, 1050, 1905),
                },
            ),
            *latency(
                "tool",
                19,
                {
                    "vad_ms": (19, None, None),
                    "stt_ms": (0, None, None),
                    "first_token_ms": (19, None, None),
                    "last_token_ms": (19, None, None),
                    "first_audio_ms": (19, None, None),
                    "total_ms": (19, None, None),
                },
            ),
        ],
        # 42 turns, record_us 10 to 420: the median halfway between 210 and 220.
        "record_us_turns": 42,
        "record_us_p50": 215,
        "turn_rows_dropped": 3,
    }
    assert STAGES == console.STAGES

    # A single day: only call 3's and the turns' figures of the 24th; under 20 of a kind, none.
    one_day = read(database, "/admin/api/metrics/summary?days=1")
    assert one_day["calls_per_day"] == [{"day": "2026-11-24T00:00:00-05:00", "calls": 2}]
    assert one_day["verified_calls"] == 1


# --- appointments and slots ----------------------------------------------------------------

INSERT_BOOKED = text(
    "INSERT INTO appointments (id, patient_id, doctor, starts_at, status)"
    " VALUES (:id, :patient_id, 'Dr. Lee', :starts_at, 'scheduled')"
)
INSERT_BOOKING = text(
    "INSERT INTO requests (call_id, patient_id, type, summary)"
    " VALUES (:call_id, :patient_id, 'booking', 'Dr. Lee')"
)
INSERT_NEW_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, phone, lab_results_status,"
    " balance_due, is_new) VALUES ('P002', 'New', 'Patient', '1990-01-01', '555-000-0001',"
    " 'Pending', 0, true)"
)


def test_appointments_name_the_booking_call(
    owner_engine: Engine, database: StartupSettings
) -> None:
    starts = at("2026-12-01T15:00Z")
    with owner_engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
        connection.execute(INSERT_NEW_PATIENT)
    # Each booking writes its appointment and its request in one transaction, so both carry the
    # same created_at.
    bookings: list[tuple[int, str, list[tuple[int, str]]]] = [
        (1, "P001", [(1, "P001")]),
        # Seeded: no request.
        (2, "P002", []),
        # Two calls' requests in the same transaction: which booked it is not known.
        (3, "P001", [(2, "P001"), (3, "P001")]),
        # Another patient's request.
        (4, "P002", [(4, "P001")]),
    ]
    for number, patient, requests in bookings:
        with owner_engine.begin() as connection:
            connection.execute(
                INSERT_BOOKED,
                {
                    "id": number,
                    "patient_id": patient,
                    "starts_at": starts + timedelta(hours=number),
                },
            )
            for call, requester in requests:
                connection.execute(INSERT_BOOKING, {"call_id": cid(call), "patient_id": requester})

    shown = read(database, "/admin/api/appointments")["appointments"]
    assert [(row["id"], row["new_patient"], row["booked_by_call"]) for row in shown] == [
        (4, True, None),
        (3, False, None),
        (2, True, None),
        (1, False, cid(1)),
    ]


def test_slots_window(
    owner_engine: Engine, database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with owner_engine.begin() as connection:
        for number, (starts_at, available) in enumerate(
            [
                # 23:59 on the 23rd at the clinic: before the window.
                (at("2026-11-24T04:59Z"), True),
                # Midnight on the 24th: the window's first instant.
                (at("2026-11-24T05:00Z"), False),
                (at("2026-11-26T15:00Z"), True),
                # 23:59 on the 30th: the last minute of seven days.
                (at("2026-12-01T04:59Z"), True),
                # Midnight on 1 December: past them.
                (at("2026-12-01T05:00Z"), True),
            ],
            start=1,
        ):
            connection.execute(
                INSERT_SLOT, {"id": number, "starts_at": starts_at, "available": available}
            )

    # From the clinic's today by default, a week of days.
    week = read(database, "/admin/api/slots")
    assert [(slot["id"], slot["available"]) for slot in week["slots"]] == [
        (2, False),
        (3, True),
        (4, True),
    ]
    assert week["slots"][0]["starts_at"] == "2026-11-24T00:00:00-05:00"
    assert week["truncated"] is False
    one_day = read(database, "/admin/api/slots?from=2026-11-26&days=1")
    assert [slot["id"] for slot in one_day["slots"]] == [3]
    # At most MAX_SLOTS rows, and the answer says when there were more.
    monkeypatch.setattr(console, "MAX_SLOTS", 2)
    capped = read(database, "/admin/api/slots?from=2026-11-23&days=14")
    assert ([slot["id"] for slot in capped["slots"]], capped["truncated"]) == ([1, 2], True)
