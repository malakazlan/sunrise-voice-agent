import ast
import asyncio
import base64
import logging
import socket
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, text

from app import main as main_module
from app.api import admin
from app.audit import read as audit_read
from app.audit.log import CHAIN_LOCK, Action, AuditLog, Event, Outcome, Reason, ResourceType
from app.config import Settings, StartupSettings
from app.db.session import create_database_engine, create_session_factory
from app.guard.rate_limit import RateLimiter
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_MODULE = REPO_ROOT / "app" / "api" / "admin.py"

ADMIN_USER = "clinic_admin"
# Built from short pieces, like every fake secret in the tests.
PASSWORD = "-".join(["admin", "pass", "not", "for", "logs"])
TRIED_USER = "intruder_name"
TRIED_PASSWORD = "-".join(["tried", "pass", "not", "for", "logs"])


def cid(number: int) -> str:
    """A call id in the format calls are given (uuid4 hex), distinct per number."""
    return f"{number:012x}4{number:03x}8{number:015x}"


# The one call the single-call routes read in these tests.
CALL_ROUTE = f"/admin/api/calls/{cid(1)}"
TURNS_ROUTE = f"/admin/api/calls/{cid(1)}/turns"
DETAIL_ROUTE = f"/admin/api/calls/{cid(1)}/detail"
LIVE_ROUTE = "/admin/api/calls/live"
QUEUE_ROUTE = "/admin/api/queue"
ROUTES = [
    "/admin/api/requests",
    "/admin/api/messages",
    "/admin/api/audit",
    "/admin/api/audit/verify",
    "/admin/api/calls",
    CALL_ROUTE,
    LIVE_ROUTE,
    TURNS_ROUTE,
    "/admin/api/appointments",
    "/admin/api/slots",
    "/admin/api/guardrails/summary",
    "/admin/api/metrics/summary",
    QUEUE_ROUTE,
    DETAIL_ROUTE,
]
# Each route's audit action, resource type and resource id, written out by hand.
ROUTE_EVENTS = {
    "/admin/api/requests": ("admin_read_requests", "request", None),
    "/admin/api/messages": ("admin_read_messages", "message", None),
    "/admin/api/audit": ("admin_read_audit", "audit_event", None),
    "/admin/api/audit/verify": ("admin_verify_audit", "audit_event", None),
    "/admin/api/calls": ("admin_read_calls", "call", None),
    CALL_ROUTE: ("admin_read_call", "call", cid(1)),
    LIVE_ROUTE: ("admin_read_live_calls", "call", None),
    TURNS_ROUTE: ("admin_read_call_turns", "call", cid(1)),
    "/admin/api/appointments": ("admin_read_appointments", "appointment", None),
    "/admin/api/slots": ("admin_read_slots", "slot", None),
    "/admin/api/guardrails/summary": ("admin_read_guardrails", "audit_event", None),
    "/admin/api/metrics/summary": ("admin_read_metrics", "call", None),
    QUEUE_ROUTE: ("admin_read_queue", "request", None),
    DETAIL_ROUTE: ("admin_read_call_detail", "call", cid(1)),
}
LISTS = {
    "/admin/api/requests": "requests",
    "/admin/api/messages": "messages",
    "/admin/api/audit": "events",
    "/admin/api/calls": "calls",
    "/admin/api/appointments": "appointments",
}
SUMMARIES = ("/admin/api/guardrails/summary", "/admin/api/metrics/summary")

REFUSED_BODY = {"detail": "Sign-in required"}
REFUSED_CHALLENGE = 'Basic realm="admin", charset="UTF-8"'
LIMITED_BODY = {"detail": "Too many failed sign-ins"}
UNAVAILABLE_BODY = {"detail": "Service unavailable"}
INVALID_QUERY_BODY = {"detail": "Invalid query"}
NOT_FOUND_BODY = {"detail": "Not Found"}

T0 = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
AUDIT = AuditLog(SecretStr(TEST_AUDIT_KEY))

EVENTS = text(
    "SELECT seq, call_id, actor, action, resource_type, resource_id, outcome, reason"
    " FROM audit_events ORDER BY seq"
)
INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, phone, lab_results_status, balance_due)"
    " VALUES ('P001', 'Test', 'Patient', '1980-01-01', '555-000-0000', 'Pending', 0)"
)
INSERT_REQUEST = text(
    "INSERT INTO requests (id, call_id, patient_id, type, summary, created_at)"
    " VALUES (:id, :call_id, :patient_id, :type, :summary, :created_at)"
)
INSERT_MESSAGE = text(
    "INSERT INTO messages (id, call_id, patient_id, name, callback_number, body, kind, created_at)"
    " VALUES (:id, :call_id, :patient_id, :name, :callback_number, :body, 'doctor_message',"
    " :created_at)"
)
INSERT_CALL = text(
    "INSERT INTO call_sessions (id, call_id, started_at, ended_at, office_open, verified,"
    " patient_id, end_reason, turn_count, transcript, transcript_truncated, names_masked)"
    " VALUES (:id, :call_id, :started_at, :ended_at, true, :verified, :patient_id, 'completed',"
    " :turn_count, :transcript, false, true)"
)


INSERT_APPOINTMENT = text(
    "INSERT INTO appointments (id, patient_id, doctor, starts_at, reason_short, status)"
    " VALUES (:id, :patient_id, 'Dr. Patel', :starts_at, :reason, 'scheduled')"
)
INSERT_SLOT = text(
    "INSERT INTO available_slots (id, doctor, starts_at, is_available, source)"
    " VALUES (:id, 'Dr. Patel', :starts_at, :available, 'generated')"
)
INSERT_TURN = text(
    "INSERT INTO call_turns (call_id, turn, at, phase, verified, patient_id, guard_action,"
    " last_tool, tool_rounds, vad_ms, stt_ms, first_token_ms, last_token_ms, first_audio_ms,"
    " total_ms, record_us, dropped_before) VALUES (:call_id, :turn, :at, :phase, :verified,"
    " :patient_id, :guard_action, :last_tool, :tool_rounds, :vad_ms, :stt_ms, :first_token_ms,"
    " :last_token_ms, :first_audio_ms, :total_ms, :record_us, :dropped_before)"
)


def insert_turn(connection: Connection, number: int, turn: int, **changes: Any) -> None:
    """A turn of call `number`: a verified caller's, a tool turn, one minute after T0."""
    values: dict[str, Any] = {
        "call_id": cid(number),
        "turn": turn,
        "at": T0 + timedelta(minutes=1),
        "phase": "serving",
        "verified": True,
        "patient_id": "P001",
        "guard_action": "pass",
        "last_tool": "get_lab_status",
        "tool_rounds": 1,
        "vad_ms": 500,
        "stt_ms": 120,
        "first_token_ms": 300,
        "last_token_ms": 900,
        "first_audio_ms": 1500,
        "total_ms": 1400,
        "record_us": 40,
        "dropped_before": 0,
    }
    values.update(changes)
    connection.execute(INSERT_TURN, values)


def insert_call(
    connection: Connection,
    number: int,
    transcript: str = "agent: Goodbye.",
    patient_id: str | None = None,
) -> None:
    """Call `number`, stored: it started `number` minutes after T0 and lasted two."""
    connection.execute(
        INSERT_CALL,
        {
            "id": number,
            "call_id": cid(number),
            "started_at": T0 + timedelta(minutes=number),
            "ended_at": T0 + timedelta(minutes=number + 2),
            "verified": patient_id is not None,
            "patient_id": patient_id,
            "turn_count": number,
            "transcript": transcript,
        },
    )


def seed_call(engine: Engine) -> None:
    """Call 1, for the single-call route."""
    with engine.begin() as connection:
        insert_call(connection, 1)


def basic(user: str | bytes, password: str | bytes) -> dict[str, str]:
    raw_user = user.encode() if isinstance(user, str) else user
    raw_password = password.encode() if isinstance(password, str) else password
    encoded = base64.b64encode(raw_user + b":" + raw_password).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


SIGNED_IN = basic(ADMIN_USER, PASSWORD)
# The API's own origin, as the test client reaches it.
OWN_ORIGIN = "http://testserver"


def admin_settings(database: StartupSettings, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "dev",
        "postgres_host": database.postgres_host,
        "postgres_port": database.postgres_port,
        "postgres_db": database.postgres_db,
        "app_db_user": database.app_db_user,
        "app_db_password": database.app_db_password,
        "audit_hmac_key": TEST_AUDIT_KEY,
        "identity_hash_key": TEST_IDENTITY_KEY,
        "admin_user": ADMIN_USER,
        "admin_password": PASSWORD,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@contextmanager
def client_from(app: FastAPI, address: str = "203.0.113.5") -> Iterator[TestClient]:
    """A client at one address, on a selector loop (psycopg's async mode needs one)."""
    with TestClient(
        app,
        client=(address, 50000),
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as client:
        yield client


def events(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS)]


def append_events(database: StartupSettings, items: Sequence[Event]) -> None:
    """Append events as the runtime role, through the audit log."""

    async def run() -> None:
        engine = create_database_engine(database)
        try:
            async with create_session_factory(engine)() as session:
                for offset, item in enumerate(items):
                    await AUDIT.append(session, item, now=T0 + timedelta(seconds=offset))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)


def call_started(number: int) -> Event:
    return Event(
        actor="agent",
        action=Action.CALL_STARTED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.SUCCESS,
        call_id=cid(number),
        resource_id=cid(number),
    )


def admin_event(route: str) -> tuple[Any, ...]:
    action, resource_type, resource_id = ROUTE_EVENTS[route]
    return (None, f"admin:{ADMIN_USER}", action, resource_type, resource_id, "success", None)


@pytest.fixture
def admin_app(database: StartupSettings) -> FastAPI:
    return create_app(admin_settings(database))


def ssn_sample() -> str:
    return "-".join(["123", "45", "6789"])


CALLER_NAME = "Jane Doe"
MESSAGE_BODY = f"Jane Doe asks. Number {ssn_sample()}, mail jane.doe@example.com, born 3/14/1985"


def seed_identifiers(engine: Engine) -> None:
    """A refill request and a doctor's message with its request, one call and one appointment
    holding every kind of identifier the masking hides, the caller's name among them; a turn
    of the call, and an open slot."""
    with engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
        connection.execute(
            INSERT_REQUEST,
            {
                "id": 1,
                "call_id": cid(1),
                "patient_id": "P001",
                "type": "refill",
                "summary": "Refill for Jane Doe, dob 1985-03-14, phone 555-201-3344,"
                f" ssn {ssn_sample()}",
                "created_at": T0,
            },
        )
        connection.execute(
            INSERT_REQUEST,
            {
                "id": 2,
                "call_id": cid(1),
                "patient_id": "P001",
                "type": "doctor_message",
                "summary": MESSAGE_BODY,
                "created_at": T0,
            },
        )
        connection.execute(
            INSERT_MESSAGE,
            {
                "id": 1,
                "call_id": cid(1),
                "patient_id": "P001",
                "name": CALLER_NAME,
                "callback_number": "555-201-3344",
                "body": MESSAGE_BODY,
                "created_at": T0,
            },
        )
        insert_call(
            connection,
            1,
            transcript="caller: Jane Doe here. Refill please, dob 1985-03-14,"
            f" ssn {ssn_sample()}\nagent: I'll mail jane.doe@example.com and call 555-201-3344.",
            patient_id="P001",
        )
        connection.execute(
            INSERT_APPOINTMENT,
            {
                "id": 1,
                "patient_id": "P001",
                "starts_at": T0 + timedelta(days=1),
                "reason": f"Follow-up, dob 1985-03-14, ssn {ssn_sample()}, jane.doe@example.com",
            },
        )
        connection.execute(
            INSERT_SLOT, {"id": 1, "starts_at": T0 + timedelta(days=1), "available": True}
        )
        insert_turn(connection, 1, 1)


# R1
def test_admin_requires_credentials(database: StartupSettings) -> None:
    app = create_app(admin_settings(database))
    cases: dict[str, dict[str, str]] = {
        "no header": {},
        "wrong user": basic(TRIED_USER, PASSWORD),
        "wrong password": basic(ADMIN_USER, TRIED_PASSWORD),
        "both wrong": basic(TRIED_USER, TRIED_PASSWORD),
        "password only": basic("", PASSWORD),
        "malformed base64": {"Authorization": "Basic not*base64!"},
        "no colon": {
            "Authorization": "Basic " + base64.b64encode(ADMIN_USER.encode()).decode("ascii")
        },
        "other scheme": {"Authorization": f"Bearer {PASSWORD}"},
        "scheme alone": {"Authorization": "Basic"},
        "non-UTF-8": basic(ADMIN_USER.encode(), b"\xff\xfe" + PASSWORD.encode()),
        "non-UTF-8 user": basic(b"\xff" + ADMIN_USER.encode(), PASSWORD),
        "empty password": basic(ADMIN_USER, ""),
        "empty both": basic("", ""),
    }
    seen = 0
    for route in ROUTES:
        for name, headers in cases.items():
            seen += 1
            # One address per attempt, so the sign-in limit never answers instead.
            client = TestClient(app, client=(f"198.51.100.{seen}", 50000))
            response = client.get(route, headers=headers)
            assert response.status_code == 401, (route, name)
            assert response.json() == REFUSED_BODY, (route, name)
            assert response.headers["www-authenticate"] == REFUSED_CHALLENGE, (route, name)
    assert seen == 14 * 13


# R2
def test_admin_is_off_without_a_password(
    database: StartupSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    for overrides in ({"admin_password": None}, {"admin_password": None, "admin_user": None}):
        caplog.clear()
        app = create_app(admin_settings(database, **overrides))
        with client_from(app) as client:
            for route in ROUTES:
                assert client.get(route, headers=SIGNED_IN).status_code == 404, route
        warnings = [record for record in caplog.records if record.getMessage() == "admin_api_off"]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING

    caplog.clear()
    create_app(admin_settings(database))
    assert not [record for record in caplog.records if record.getMessage() == "admin_api_off"]


# R4
def test_admin_output_is_masked(owner_engine: Engine, database: StartupSettings) -> None:
    seed_identifiers(owner_engine)
    append_events(database, [call_started(1)])
    raw = [
        ssn_sample(),
        "1985-03-14",
        "555-201-3344",
        "201-3344",
        "jane.doe@",
        "3/14/1985",
        "Jane",
        "Doe",
    ]
    # The clinic's clock ten minutes after the call started: it is live, and the slot is in
    # the week from today.
    admin_app = create_app(admin_settings(database, simulated_now="2026-11-24T15:40:00Z"))

    with client_from(admin_app) as client:
        # The audit read asks for call events only, so its answer is the one seeded above and
        # not the admin's own reads before it.
        bodies = {
            route: client.get(
                route + ("?action=call_started" if route == "/admin/api/audit" else ""),
                headers=SIGNED_IN,
            )
            for route in ROUTES
        }
        # The session's routes and the writes, from a signed-in browser.
        signed = client.post(
            "/admin/api/session",
            json={"user": ADMIN_USER, "password": PASSWORD},
            headers={"Origin": OWN_ORIGIN},
        )
        write = {"X-CSRF-Token": signed.json()["csrf_token"], "Origin": OWN_ORIGIN}
        written = {
            "POST /session": signed,
            "GET /session": client.get("/admin/api/session"),
            "GET /config": client.get("/admin/api/config", headers=SIGNED_IN),
            "POST /config": client.post(
                "/admin/api/config",
                json={"key": "office", "value": "open", "confirm": True},
                headers=write,
            ),
            "POST /queue/{source}/{row_id}/handled": client.post(
                "/admin/api/queue/requests/2/handled", json={}, headers=write
            ),
            "GET /queue?state=all": client.get("/admin/api/queue?state=all", headers=SIGNED_IN),
        }
        # The one route that shows a value unmasked: a callback's name and number, asked for
        # one row at a time.
        shown = client.post("/admin/api/queue/requests/2/callback", json={}, headers=write)
        written["DELETE /session"] = client.delete("/admin/api/session", headers=write)

    for route, response in [*bodies.items(), *written.items()]:
        assert response.status_code in (200, 201, 204), route
        for value in raw:
            assert value not in response.text, (route, value)
    assert shown.status_code == 200
    assert shown.json() == {
        "source": "requests",
        "id": 2,
        "callbacks": [{"name": CALLER_NAME, "callback_number": "555-201-3344"}],
    }

    # Every route of the admin API is one of these, so each is checked here: the reads above
    # (the live stream sends the rows of /calls/live, its first included), the session's
    # routes and the writes; the callback's details are the one exception. A new route fails
    # this until it is added and checked.
    routes = {
        (decorator.func.attr.upper(), decorator.args[0].value)
        for node in ast.walk(ast.parse(ADMIN_MODULE.read_text(encoding="utf-8")))
        if isinstance(node, ast.AsyncFunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id in ("app", "reads")
        and isinstance(decorator.args[0], ast.Constant)
    }
    checked = {
        ("GET", route.removeprefix("/admin/api").replace(cid(1), "{call_id}")) for route in ROUTES
    } | {(name.split()[0], name.split()[1].split("?")[0]) for name in written}
    assert routes == checked | {
        ("GET", "/calls/live/stream"),
        ("POST", "/queue/{source}/{row_id}/callback"),
    }
    # The details are read by one function, called by that one route only.
    unmasked = [
        node
        for node in ast.walk(ast.parse(ADMIN_MODULE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "callback_details"
    ]
    assert len(unmasked) == 1

    # The caller's name is hidden wherever it stands, the message's own field included.
    refill = {
        "id": 1,
        "created_at": "2026-11-24T15:30:00+00:00",
        "call_id": cid(1),
        "type": "refill",
        "summary": "Refill for <PERSON> <PERSON>, dob ****-**-14, phone ***-***-3344, ssn ****",
        "patient_id": "P001",
    }
    left = {
        "id": 2,
        "created_at": "2026-11-24T15:30:00+00:00",
        "call_id": cid(1),
        "type": "doctor_message",
        "summary": "<PERSON> <PERSON> asks. Number ***-**-****, mail j***@***.com, born ****-**-14",
        "patient_id": "P001",
    }
    assert bodies["/admin/api/requests"].json() == {"requests": [left, refill]}
    assert bodies["/admin/api/messages"].json() == {
        "messages": [
            {
                "id": 1,
                "created_at": "2026-11-24T15:30:00+00:00",
                "call_id": cid(1),
                "kind": "doctor_message",
                "name": "<PERSON> <PERSON>",
                "callback_number": "***-***-3344",
                "body": "<PERSON> <PERSON> asks. Number ***-**-****, mail j***@***.com,"
                " born ****-**-14",
                "patient_id": "P001",
            }
        ]
    }
    # The Requests page's rows: newest received first, the higher id first at the same instant;
    # each with the masked numbers its message left, never the caller's name.
    assert bodies[QUEUE_ROUTE].json() == {
        "requests": [
            {**left, "callbacks": ["***-***-3344"], "after_hours": False, "handled": None},
            {**refill, "callbacks": [], "after_hours": False, "handled": None},
        ]
    }
    call = {
        "id": 1,
        "call_id": cid(1),
        "started_at": "2026-11-24T15:31:00+00:00",
        "ended_at": "2026-11-24T15:33:00+00:00",
        "office_open": True,
        "verified": True,
        "patient_id": "P001",
        "end_reason": "completed",
        "turn_count": 1,
    }
    assert bodies["/admin/api/calls"].json() == {"calls": [call]}
    stored = {
        **call,
        # As in the request's summary: digits after an SSN cue are hidden whole; and the names
        # the call's records know are hidden a second time.
        "transcript": "caller: <PERSON> <PERSON> here. Refill please, dob ****-**-14, ssn ****\n"
        "agent: I'll mail j***@***.com and call ***-***-3344.",
        "transcript_truncated": False,
        "names_masked": True,
        "transcript_withheld": None,
    }
    assert bodies[CALL_ROUTE].json() == {"call": stored}
    assert bodies[DETAIL_ROUTE].json() == {
        "call": stored,
        "events": [
            {
                "seq": 1,
                "ts": "2026-11-24T15:30:00+00:00",
                "call_id": cid(1),
                "actor": "agent",
                "action": "call_started",
                "resource_type": "call",
                "resource_id": cid(1),
                "outcome": "success",
                "reason": None,
            }
        ],
    }
    assert bodies["/admin/api/audit"].json() == {
        "events": [
            {
                "seq": 1,
                "ts": "2026-11-24T15:30:00+00:00",
                "call_id": cid(1),
                "actor": "agent",
                "action": "call_started",
                "resource_type": "call",
                "resource_id": cid(1),
                "outcome": "success",
                "reason": None,
            }
        ]
    }
    timings = {
        "vad_ms": 500,
        "stt_ms": 120,
        "first_token_ms": 300,
        "last_token_ms": 900,
        "first_audio_ms": 1500,
        "total_ms": 1400,
    }
    assert bodies[LIVE_ROUTE].json() == {
        "calls": [
            {
                "call_id": cid(1),
                "started_at": "2026-11-24T15:30:00+00:00",
                "turn": 1,
                "phase": "serving",
                "verified": True,
                "patient_id": "P001",
                "last_tool": "get_lab_status",
                **timings,
            }
        ]
    }
    assert bodies[TURNS_ROUTE].json() == {
        "turns": [
            {
                "turn": 1,
                "at": "2026-11-24T15:31:00+00:00",
                "phase": "serving",
                "verified": True,
                "patient_id": "P001",
                "guard_action": "pass",
                "last_tool": "get_lab_status",
                "tool_rounds": 1,
                **timings,
                "record_us": 40,
            }
        ]
    }
    # Instants in clinic time, and the appointment's in UTC as well.
    assert bodies["/admin/api/appointments"].json() == {
        "appointments": [
            {
                "id": 1,
                "starts_at": "2026-11-25T10:30:00-05:00",
                "starts_at_utc": "2026-11-25T15:30:00+00:00",
                "doctor": "Dr. Patel",
                "patient_id": "P001",
                "new_patient": False,
                "reason": "Follow-up, dob ****-**-14, ssn ****, j***@***.com",
                "status": "scheduled",
                "booked_by_call": None,
            }
        ]
    }
    assert bodies["/admin/api/slots"].json() == {
        "slots": [
            {
                "id": 1,
                "doctor": "Dr. Patel",
                "starts_at": "2026-11-25T10:30:00-05:00",
                "available": True,
            }
        ],
        "truncated": False,
    }


# R5
def test_admin_reads_are_audited(admin_app: FastAPI, owner_engine: Engine) -> None:
    seed_call(owner_engine)
    expected: list[tuple[Any, ...]] = []
    with client_from(admin_app) as client:
        for route in ROUTES:
            response = client.get(route, headers=SIGNED_IN)
            assert response.status_code == 200, route
            expected.append(admin_event(route))
            assert [row[1:] for row in events(owner_engine)] == expected, route

        before = events(owner_engine)
        for headers in ({}, basic(ADMIN_USER, TRIED_PASSWORD), basic(TRIED_USER, PASSWORD)):
            assert client.get(ROUTES[0], headers=headers).status_code == 401
        assert events(owner_engine) == before

        # A call that is not there, or an id that is not a call's, is not read: no event.
        missing_ids = (cid(2), cid(0xABC).upper(), "1", cid(1) + "0", "x'; DROP TABLE requests;--")
        for missing in missing_ids:
            response = client.get(f"/admin/api/calls/{missing}", headers=SIGNED_IN)
            assert response.status_code == 404, missing
            assert response.json() == NOT_FOUND_BODY, missing
        # Nor are the turns of an id that is not a call's, nor the detail of either.
        for malformed in missing_ids[1:]:
            response = client.get(f"/admin/api/calls/{malformed}/turns", headers=SIGNED_IN)
            assert response.status_code == 404, malformed
            assert response.json() == NOT_FOUND_BODY, malformed
        for missing in missing_ids:
            response = client.get(f"/admin/api/calls/{missing}/detail", headers=SIGNED_IN)
            assert response.status_code == 404, missing
            assert response.json() == NOT_FOUND_BODY, missing
        assert events(owner_engine) == before

        # The chain the reads wrote verifies.
        verified = client.get("/admin/api/audit/verify", headers=SIGNED_IN).json()
    assert verified == {"ok": True, "checked": 14, "breaks": [], "truncated": False}
    assert [row[0] for row in events(owner_engine)] == list(range(1, 16))


# R6
def test_no_data_without_an_audit_event(admin_app: FastAPI, owner_engine: Engine) -> None:
    seed_identifiers(owner_engine)
    with client_from(admin_app) as client:
        responses = {}
        # The lock taken afresh for each route: each read waits out its lock timeout, and the
        # holder's transaction must not sit idle past the server's limit for all of them.
        for route in ROUTES:
            with owner_engine.connect() as holder:
                holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
                responses[route] = client.get(route, headers=SIGNED_IN)
                holder.rollback()
        for route, response in responses.items():
            assert response.status_code == 503, route
            assert response.json() == UNAVAILABLE_BODY, route
            assert "Refill" not in response.text
        assert events(owner_engine) == []

        # With the lock free again the same read answers, and is audited.
        assert client.get(ROUTES[0], headers=SIGNED_IN).status_code == 200
    assert [row[1:] for row in events(owner_engine)] == [admin_event(ROUTES[0])]


# R7
def test_admin_query_bounds(
    admin_app: FastAPI, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    refused_everywhere = [
        "limit=0",
        "limit=101",
        "limit=-1",
        "limit=abc",
        "limit=1;",
        "limit=1'",
        "limit=",
        "before=0",
        "before=-1",
        "before=1;",
        "before=1'",
        "unknown=1",
        "limit=5&limit=6",
        "before=5&before=6",
    ]
    refused_on_audit = [
        "call_id=abc",
        "call_id=" + cid(0xABC).upper(),
        "call_id=" + cid(0xABC)[:-1] + "g",
        "before=9223372036854775808",
        "call_id=" + cid(1) + "'",
        "call_id=" + cid(1) + ";",
        "call_id=x'; DROP TABLE requests;--",
        "call_id=" + cid(1) + "&call_id=" + cid(2),
        "action=nope",
        "action=call_started'",
        "action=call_started;",
        "action=CALL_STARTED",
        "action=call_started&action=call_ended",
    ]
    accepted = ["limit=1", "limit=100", "before=1"]
    # Request and message ids are integers, audit seqs bigints.
    on_rows = {"refused": ["before=2147483648"], "accepted": ["before=2147483647"]}
    accepted_on_audit = [
        "before=9223372036854775807",
        "call_id=" + cid(1),
        "action=call_started",
        "action=admin_read_audit&call_id=" + cid(1) + "&limit=7&before=3",
    ]
    written = 0
    with client_from(admin_app) as client:
        for route in LISTS:
            if route == "/admin/api/audit":
                refused = refused_everywhere + refused_on_audit
                allowed = accepted + accepted_on_audit
            else:
                refused = refused_everywhere + on_rows["refused"]
                refused += ["call_id=" + cid(1), "action=call_started"]
                allowed = accepted + on_rows["accepted"]
            for query in refused:
                response = client.get(f"{route}?{query}", headers=SIGNED_IN)
                assert response.status_code == 422, (route, query)
                assert response.json() == INVALID_QUERY_BODY, (route, query)
            # A refused query reads nothing, so nothing is audited.
            assert len(events(owner_engine)) == written, route

            for query in allowed:
                response = client.get(f"{route}?{query}", headers=SIGNED_IN)
                assert response.status_code == 200, (route, query)
            written += len(allowed)
            assert len(events(owner_engine)) == written, route

        # The verify, single-call and live routes take no parameters at all.
        for route in ("/admin/api/audit/verify", CALL_ROUTE, LIVE_ROUTE, DETAIL_ROUTE):
            for query in ["limit=1", "before=1", "call_id=" + cid(1), "x=1"]:
                response = client.get(f"{route}?{query}", headers=SIGNED_IN)
                assert response.status_code == 422, (route, query)
                assert response.json() == INVALID_QUERY_BODY, (route, query)

        # The other routes' own parameters: the requests' type, the queue's type and the last
        # request of the page before, one call's turns after a turn number, the slots of up to
        # 14 days from a date, and summaries of 1 to 30 days.
        own = {
            "/admin/api/calls": (
                [
                    "end_reason=nope",
                    "end_reason=COMPLETED",
                    "end_reason=",
                    "end_reason=completed'",
                    "end_reason=completed&end_reason=error",
                ],
                ["end_reason=completed", "end_reason=error&limit=5&before=7"],
            ),
            QUEUE_ROUTE: (
                [
                    "type=nope",
                    "type=REFILL",
                    "type=",
                    "type=refill&type=booking",
                    "before=0",
                    "before=-1",
                    "before=2147483648",
                    "before=1;",
                    "before=1&before=2",
                    "limit=5",
                    "call_id=" + cid(1),
                    "x=1",
                ],
                ["type=refill", "before=1", "type=callback&before=2147483647"],
            ),
            "/admin/api/requests": (
                [
                    "type=nope",
                    "type=REFILL",
                    "type=",
                    "type=refill'",
                    "type=refill&type=booking",
                ],
                ["type=refill", "type=transfer&limit=5&before=7"],
            ),
            TURNS_ROUTE: (
                [
                    "limit=0",
                    "limit=101",
                    "after=0",
                    "after=-1",
                    "after=2147483648",
                    "after=1;",
                    "after=1&after=2",
                    "before=1",
                    "x=1",
                ],
                ["limit=1", "after=1", "limit=100&after=2147483647"],
            ),
            "/admin/api/slots": (
                [
                    "days=0",
                    "days=15",
                    "days=-1",
                    "days=x",
                    "from=2026-13-01",
                    "from=2026-11-24T00:00",
                    "from=1764000000",
                    "from=24/11/2026",
                    "from=",
                    "from=2026-11-24&from=2026-11-25",
                    "to=2026-11-30",
                    "limit=1",
                    # From 2000-01-01 to 2100-12-31 only: a date past them is refused here,
                    # never read.
                    "from=1999-12-31",
                    "from=2101-01-01",
                    "from=9999-12-31",
                    "from=9999-12-31&days=14",
                ],
                [
                    "from=2026-11-24",
                    "days=14",
                    "days=1&from=2026-02-28",
                    "from=2000-01-01",
                    "from=2100-12-31&days=14",
                ],
            ),
            **{
                route: (
                    ["days=0", "days=31", "days=-1", "days=x", "days=7&days=8", "limit=1"],
                    ["days=1", "days=30"],
                )
                for route in SUMMARIES
            },
        }
        for route, (refused, allowed) in own.items():
            for query in refused:
                response = client.get(f"{route}?{query}", headers=SIGNED_IN)
                assert response.status_code == 422, (route, query)
                assert response.json() == INVALID_QUERY_BODY, (route, query)
            assert len(events(owner_engine)) == written, route
            for query in allowed:
                response = client.get(f"{route}?{query}", headers=SIGNED_IN)
                assert response.status_code == 200, (route, query)
            written += len(allowed)
            assert len(events(owner_engine)) == written, route
    assert len(events(owner_engine)) == written
    assert written == 4 + 4 + 7 + 4 + 4 + 2 + 3 + 2 + 3 + 5 + 2 + 2
    # No read failed: every refused value was refused before the database was asked.
    assert "admin_read_failed" not in caplog.text


def test_audit_reader_refuses_unvalidated_filters() -> None:
    # The reader checks its filters itself, before it touches the session it is given.
    unused: Any = object()
    for change in (
        {"call_id": "x'; DROP TABLE requests;--"},
        {"call_id": cid(0xABC).upper()},
        {"before": 2**63},
        {"action": "call_started"},
        {"limit": 0},
        {"limit": 101},
        {"before": 0},
    ):
        arguments: dict[str, Any] = {"limit": 50, "before": None, "call_id": None, "action": None}
        arguments.update(change)
        with pytest.raises((TypeError, ValueError)):
            asyncio.run(
                audit_read.recent_events(unused, **arguments),
                loop_factory=asyncio.SelectorEventLoop,
            )


# R8
def test_admin_paging(admin_app: FastAPI, owner_engine: Engine, database: StartupSettings) -> None:
    with owner_engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
        for number in range(1, 121):
            connection.execute(
                INSERT_APPOINTMENT,
                {
                    "id": number,
                    "patient_id": "P001",
                    "starts_at": T0 + timedelta(hours=number),
                    "reason": None,
                },
            )
            insert_turn(connection, 1, number)
            at = T0 + timedelta(minutes=number)
            connection.execute(
                INSERT_REQUEST,
                {
                    "id": number,
                    "call_id": cid(number),
                    "patient_id": None,
                    "type": "refill",
                    "summary": f"Refill number {number}",
                    "created_at": at,
                },
            )
            connection.execute(
                INSERT_MESSAGE,
                {
                    "id": number,
                    "call_id": cid(number),
                    "patient_id": None,
                    "name": None,
                    "callback_number": None,
                    "body": f"Message number {number}",
                    "created_at": at,
                },
            )
            insert_call(connection, number)
    append_events(database, [call_started(number) for number in range(1, 121)])

    with client_from(admin_app) as client:
        for route, key, extra in (
            ("/admin/api/requests", "id", ""),
            ("/admin/api/messages", "id", ""),
            ("/admin/api/calls", "id", ""),
            ("/admin/api/appointments", "id", ""),
            # Only the events appended above; the reads add their own as they go.
            ("/admin/api/audit", "seq", "&action=call_started"),
        ):
            seen: list[int] = []
            sizes: list[int] = []
            query = f"limit=50{extra}"
            # Four pages hold the 120 rows and the empty end; a fifth means paging does not
            # move on, and the test stops there instead of asking for ever.
            for _ in range(5):
                response = client.get(f"{route}?{query}", headers=SIGNED_IN)
                assert response.status_code == 200, route
                rows = response.json()[LISTS[route]]
                sizes.append(len(rows))
                if not rows:
                    break
                seen += [row[key] for row in rows]
                query = f"limit=50&before={rows[-1][key]}{extra}"
            assert sizes == [50, 50, 20, 0], route
            assert seen == list(range(120, 0, -1)), route

        # The queue, in pages of 50, from the last request of the page before.
        seen = []
        sizes = []
        query = ""
        for _ in range(5):
            rows = client.get(QUEUE_ROUTE + query, headers=SIGNED_IN).json()["requests"]
            sizes.append(len(rows))
            if not rows:
                break
            seen += [row["id"] for row in rows]
            query = f"?before={rows[-1]['id']}"
        assert sizes == [50, 50, 20, 0]
        assert seen == list(range(120, 0, -1))

        # A call's turns come in order, a page at a time after the last turn seen.
        turns: list[int] = []
        sizes = []
        query = "limit=50"
        for _ in range(5):
            rows = client.get(f"{TURNS_ROUTE}?{query}", headers=SIGNED_IN).json()["turns"]
            sizes.append(len(rows))
            if not rows:
                break
            turns += [row["turn"] for row in rows]
            query = f"limit=50&after={rows[-1]['turn']}"
        assert sizes == [50, 50, 20, 0]
        assert turns == list(range(1, 121))


# R9
def test_admin_verify_reports_breaks(
    admin_app: FastAPI,
    owner_engine: Engine,
    database: StartupSettings,
    tamper_with_audit_events: Callable[..., None],
) -> None:
    append_events(database, [call_started(1), call_started(2), call_started(3)])
    with client_from(admin_app) as client:
        intact = client.get("/admin/api/audit/verify", headers=SIGNED_IN).json()
        assert intact == {"ok": True, "checked": 3, "breaks": [], "truncated": False}

        tamper_with_audit_events(
            owner_engine, "UPDATE audit_events SET action = 'call_ended' WHERE seq = 2"
        )
        broken = client.get("/admin/api/audit/verify", headers=SIGNED_IN).json()
    # Events 1 to 3, and the first verification's own event (4).
    assert broken == {
        "ok": False,
        "checked": 4,
        "breaks": [{"seq": 2, "kind": "mac_mismatch"}],
        "truncated": False,
    }


# R10
def test_admin_sign_in_is_rate_limited(admin_app: FastAPI) -> None:
    wrong = basic(ADMIN_USER, TRIED_PASSWORD)
    with client_from(admin_app, "192.0.2.10") as client:
        for _ in range(5):
            assert client.get(ROUTES[0], headers=wrong).status_code == 401
        sixth = client.get(ROUTES[0], headers=wrong)
        assert sixth.status_code == 429
        assert sixth.json() == LIMITED_BODY
        # Limited before any comparison: the right password is refused the same way.
        assert client.get(ROUTES[0], headers=SIGNED_IN).status_code == 429

    # Another address is unaffected, and a correct sign-in is never counted.
    with client_from(admin_app, "192.0.2.20") as client:
        for _ in range(4):
            assert client.get(ROUTES[0], headers=wrong).status_code == 401
        for _ in range(6):
            assert client.get(ROUTES[0], headers=SIGNED_IN).status_code == 200
        assert client.get(ROUTES[0], headers=wrong).status_code == 401
        assert client.get(ROUTES[0], headers=wrong).status_code == 429


def test_rate_limiter_window_and_address_cap() -> None:
    now = [1000.0]
    limiter = RateLimiter(limit=5, window_seconds=60, max_addresses=3, clock=lambda: now[0])
    for count in range(1, 6):
        assert not limiter.blocked("a")
        assert limiter.record_failure("a") == count
        now[0] += 1
    assert limiter.blocked("a")
    assert not limiter.blocked("b")
    # The window slides: the first failure (at 1000) leaves it at 1060.
    now[0] = 1059.9
    assert limiter.blocked("a")
    now[0] = 1060.0
    assert not limiter.blocked("a")
    assert limiter.record_failure("a") == 5
    assert limiter.blocked("a")

    # At most three addresses are kept; the one that failed longest ago goes first.
    limiter.record_failure("b")
    limiter.record_failure("c")
    limiter.record_failure("d")
    assert limiter.tracked == 3
    assert not limiter.blocked("a")
    assert limiter.record_failure("a") == 1


# R11
def test_admin_secrets_stay_out_of_logs(
    database: StartupSettings,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    app = create_app(admin_settings(database, log_level="DEBUG"))
    caplog.set_level(logging.DEBUG)
    tried = [
        basic(TRIED_USER, TRIED_PASSWORD),
        basic(ADMIN_USER, TRIED_PASSWORD),
        basic(TRIED_USER, PASSWORD),
    ]
    bodies: list[str] = []
    with client_from(app, "192.0.2.30") as client:
        for headers in tried:
            bodies.append(client.get(ROUTES[0], headers=headers).text)
        bodies.append(client.get(ROUTES[0], headers=tried[0]).text)
        bodies.append(client.get(ROUTES[0], headers=tried[0]).text)
        bodies.append(client.get(ROUTES[0], headers=tried[0]).text)
    with client_from(app, "192.0.2.31") as client:
        for route in ROUTES:
            bodies.append(client.get(route, headers=SIGNED_IN).text)
    output = capfd.readouterr()
    stored = repr(events(owner_engine))

    secrets = [TRIED_USER, TRIED_PASSWORD, PASSWORD]
    secrets += [headers["Authorization"].split(" ", 1)[1] for headers in [*tried, SIGNED_IN]]
    for secret in secrets:
        assert secret not in caplog.text, secret
        assert secret not in output.out + output.err, secret
        assert secret not in stored, secret
        for body in bodies:
            assert secret not in body, secret
    failures = [r.getMessage() for r in caplog.records if "admin_sign_in" in r.getMessage()]
    assert failures == [
        "admin_sign_in_failed failures=1",
        "admin_sign_in_failed failures=2",
        "admin_sign_in_failed failures=3",
        "admin_sign_in_failed failures=4",
        "admin_sign_in_failed failures=5",
        "admin_sign_in_limited",
    ]


# R12
def test_admin_headers_and_schema(
    admin_app: FastAPI, owner_engine: Engine, database: StartupSettings
) -> None:
    with client_from(admin_app, "192.0.2.40") as client:
        responses = [
            client.get(ROUTES[0], headers=SIGNED_IN),
            client.get(ROUTES[0]),
            client.get(f"{ROUTES[0]}?limit=0", headers=SIGNED_IN),
            client.post(ROUTES[0], headers=SIGNED_IN),
            client.get("/admin/api/nothing-here", headers=SIGNED_IN),
            client.get(f"/admin/api/calls/{cid(2)}", headers=SIGNED_IN),
            client.get("/admin/api/calls/not-a-call", headers=SIGNED_IN),
        ]
        wrong = basic(ADMIN_USER, TRIED_PASSWORD)
        for _ in range(5):
            client.get(ROUTES[0], headers=wrong)
        responses.append(client.get(ROUTES[0], headers=wrong))
    down = create_app(admin_settings(database, postgres_port=closed_port()))
    with client_from(down) as client:
        responses.append(client.get(ROUTES[0], headers=SIGNED_IN))
    assert [response.status_code for response in responses] == [
        200,
        401,
        422,
        405,
        404,
        404,
        404,
        429,
        503,
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.status_code
        assert response.headers["x-content-type-options"] == "nosniff", response.status_code
        assert response.headers["referrer-policy"] == "no-referrer", response.status_code
        assert (
            response.headers["content-security-policy"]
            == "default-src 'none'; frame-ancestors 'none'"
        ), response.status_code

    # Docs are on in dev, and the admin routes are not in them.
    with client_from(admin_app) as client:
        schema = client.get("/openapi.json").json()
        assert not [path for path in schema["paths"] if path.startswith("/admin")]
        for path in ("/admin/api/openapi.json", "/admin/api/docs", "/admin/api/redoc"):
            assert client.get(path, headers=SIGNED_IN).status_code == 404, path


def closed_port() -> int:
    """A local port nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# Neighbours
def test_admin_methods_are_refused(admin_app: FastAPI, owner_engine: Engine) -> None:
    with client_from(admin_app) as client:
        for route in ROUTES:
            for method in ("HEAD", "POST", "PUT", "DELETE", "PATCH"):
                response = client.request(method, route, headers=SIGNED_IN)
                assert response.status_code == 405, (method, route)
    assert events(owner_engine) == []


def test_admin_empty_tables(admin_app: FastAPI, owner_engine: Engine) -> None:
    with client_from(admin_app) as client:
        assert client.get("/admin/api/requests", headers=SIGNED_IN).json() == {"requests": []}
        assert client.get("/admin/api/messages", headers=SIGNED_IN).json() == {"messages": []}
        assert client.get("/admin/api/calls", headers=SIGNED_IN).json() == {"calls": []}
        assert client.get("/admin/api/appointments", headers=SIGNED_IN).json() == {
            "appointments": []
        }
        assert client.get(LIVE_ROUTE, headers=SIGNED_IN).json() == {"calls": []}
        assert client.get(TURNS_ROUTE, headers=SIGNED_IN).json() == {"turns": []}
        assert client.get(QUEUE_ROUTE, headers=SIGNED_IN).json() == {"requests": []}
        # Past the oldest row: nothing.
        seed_identifiers(owner_engine)
        assert client.get("/admin/api/requests?before=1", headers=SIGNED_IN).json() == {
            "requests": []
        }
    assert [row[1:] for row in events(owner_engine)] == [
        admin_event("/admin/api/requests"),
        admin_event("/admin/api/messages"),
        admin_event("/admin/api/calls"),
        admin_event("/admin/api/appointments"),
        admin_event(LIVE_ROUTE),
        admin_event(TURNS_ROUTE),
        admin_event(QUEUE_ROUTE),
        admin_event("/admin/api/requests"),
    ]


def test_queue_is_newest_received_first(admin_app: FastAPI, owner_engine: Engine) -> None:
    # Received time decides, not the id; at the same instant the higher id comes first. A page
    # goes on from the last request of the page before; an id that is not a request's ends it.
    with owner_engine.begin() as connection:
        for number, minutes in ((1, 2), (2, 1), (3, 2)):
            connection.execute(
                INSERT_REQUEST,
                {
                    "id": number,
                    "call_id": cid(number),
                    "patient_id": None,
                    "type": "refill",
                    "summary": f"Refill number {number}",
                    "created_at": T0 + timedelta(minutes=minutes),
                },
            )
    with client_from(admin_app) as client:
        for query, expected in (
            ("", [3, 1, 2]),
            ("?before=3", [1, 2]),
            ("?before=1", [2]),
            ("?before=2", []),
            ("?before=99", []),
            ("?type=callback", []),
        ):
            rows = client.get(QUEUE_ROUTE + query, headers=SIGNED_IN).json()["requests"]
            assert [row["id"] for row in rows] == expected, query


def test_a_transcript_whose_names_were_not_masked_is_withheld(
    admin_app: FastAPI, owner_engine: Engine
) -> None:
    with owner_engine.begin() as connection:
        insert_call(connection, 1, transcript="caller: Jane Doe here.")
        connection.execute(text("UPDATE call_sessions SET names_masked = false"))
    with client_from(admin_app) as client:
        for route in (CALL_ROUTE, DETAIL_ROUTE):
            response = client.get(route, headers=SIGNED_IN)
            assert response.status_code == 200, route
            shown = response.json()["call"]
            assert shown["transcript"] is None, route
            assert shown["transcript_withheld"] == "names_not_masked", route
            assert "Jane" not in response.text, route


def test_admin_database_down(database: StartupSettings, caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(admin_settings(database, postgres_port=closed_port()))
    caplog.set_level(logging.INFO)
    with client_from(app) as client:
        for route in ROUTES:
            response = client.get(route, headers=SIGNED_IN)
            assert response.status_code == 503, route
            assert response.json() == UNAVAILABLE_BODY, route
        # An id that is not a call id is refused before the database is asked.
        for malformed in (cid(0xABC).upper(), "not-a-call", cid(1) + "0"):
            for path in (f"/admin/api/calls/{malformed}", f"/admin/api/calls/{malformed}/turns"):
                response = client.get(path, headers=SIGNED_IN)
                assert response.status_code == 404, path
                assert response.json() == NOT_FOUND_BODY, path
    assert [r.getMessage() for r in caplog.records if r.name == "app.api.admin"] == [
        f"admin_read_failed action={ROUTE_EVENTS[route][0]} error=OperationalError"
        for route in ROUTES
    ]


def test_admin_long_authorization_header(
    admin_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a header over the limit was decoded or compared")

    monkeypatch.setattr(admin, "b64decode", must_not_run)
    monkeypatch.setattr(admin, "_credentials_match", must_not_run)
    long_header = {"Authorization": "Basic " + "QUFB" * 100_000}
    response = TestClient(admin_app, client=("192.0.2.50", 1)).get(ROUTES[0], headers=long_header)
    assert response.status_code == 401
    assert response.json() == REFUSED_BODY


def test_admin_credentials_compared_in_constant_time() -> None:
    # Structural: both halves go through compare_digest, both are always evaluated (a bitwise
    # and, never a short-circuit), and nothing in the check compares with == or !=.
    tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
    check = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_credentials_match"
    )
    calls = [
        node
        for node in ast.walk(check)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
    ]
    assert len(calls) == 2
    assert not [node for node in ast.walk(check) if isinstance(node, ast.Compare | ast.BoolOp)]
    returned = [node for node in ast.walk(check) if isinstance(node, ast.Return)]
    assert len(returned) == 1
    value = returned[0].value
    assert isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitAnd)


def test_admin_shares_the_app_engine(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # create_app builds one engine, one session factory and one audit log, and the admin API
    # is given those; it builds none of its own.
    counts = {"create_database_engine": 0, "create_session_factory": 0, "AuditLog": 0}
    for name in counts:
        original = getattr(main_module, name)

        def counting(*args: Any, _name: str = name, _original: Any = original) -> Any:
            counts[_name] += 1
            return _original(*args)

        monkeypatch.setattr(main_module, name, counting)

    create_app(admin_settings(database))

    assert counts == {"create_database_engine": 1, "create_session_factory": 1, "AuditLog": 1}
    built = {
        node.func.id
        for node in ast.walk(ast.parse(ADMIN_MODULE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not built & {"create_database_engine", "create_session_factory", "AuditLog"}


def call_ended(number: int) -> Event:
    return Event(
        actor="agent",
        action=Action.CALL_ENDED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.FAILURE,
        call_id=cid(number),
        resource_id=cid(number),
        reason=Reason.CALLER_HUNG_UP,
    )


def test_admin_audit_filters(
    admin_app: FastAPI, owner_engine: Engine, database: StartupSettings
) -> None:
    append_events(database, [call_started(1), call_started(2), call_ended(1), call_ended(2)])
    rows = {
        "call_id=" + cid(1): [3, 1],
        "call_id=" + cid(2): [4, 2],
        "action=call_ended": [4, 3],
        "action=call_started&call_id=" + cid(2): [2],
        "action=call_ended&call_id=" + cid(1): [3],
        "call_id=" + cid(3): [],
        "action=tool_called": [],
    }
    with client_from(admin_app) as client:
        for query, seqs in rows.items():
            response = client.get(f"/admin/api/audit?{query}", headers=SIGNED_IN)
            assert response.status_code == 200, query
            assert [event["seq"] for event in response.json()["events"]] == seqs, query
        # The reads' own events carry no call id, and their own action.
        reads = client.get("/admin/api/audit?action=admin_read_audit", headers=SIGNED_IN)
    assert [event["seq"] for event in reads.json()["events"]] == list(range(11, 4, -1))


def test_admin_verify_runs_one_at_a_time(
    admin_app: FastAPI, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    runs: list[int] = []
    original = AuditLog.verify

    async def held_verify(self: AuditLog, session: Any) -> Any:
        runs.append(1)
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return await original(self, session)

    monkeypatch.setattr(AuditLog, "verify", held_verify)
    with client_from(admin_app) as client:
        first: list[Any] = []
        worker = threading.Thread(
            target=lambda: first.append(client.get("/admin/api/audit/verify", headers=SIGNED_IN))
        )
        worker.start()
        assert entered.wait(10)
        second = client.get("/admin/api/audit/verify", headers=SIGNED_IN)
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        # Once the first has finished, a new run is accepted.
        third = client.get("/admin/api/audit/verify", headers=SIGNED_IN)

    assert second.status_code == 429
    assert second.json() == {"detail": "A verification is already running"}
    assert second.headers["retry-after"] == "5"
    assert second.headers["cache-control"] == "no-store"
    assert first[0].status_code == 200
    assert third.status_code == 200
    assert len(runs) == 2
    # The refused run did no work and left no event.
    assert [row[1:] for row in events(owner_engine)] == [
        admin_event("/admin/api/audit/verify"),
        admin_event("/admin/api/audit/verify"),
    ]


def test_admin_callback_numbers_show_only_last_four(
    admin_app: FastAPI, owner_engine: Engine
) -> None:
    shown = {
        "555-201-3344": "***-***-3344",
        "+44 20 7946 0958": "***-***-0958",
        "555 201 33 44": "***-***-3344",
        "201-3344": "***-***-3344",
        "(555) 201-3344 ext 12": "***-***-4412",
        "3344": "***-***-3344",
        "344": "***-***-****",
        "12": "***-***-****",
        "no number": "***-***-****",
    }
    with owner_engine.begin() as connection:
        for number, raw in enumerate([*shown, None], start=1):
            connection.execute(
                INSERT_MESSAGE,
                {
                    "id": number,
                    "call_id": cid(number),
                    "patient_id": None,
                    "name": None,
                    "callback_number": raw,
                    "body": f"Message number {number}",
                    "created_at": T0,
                },
            )
    with client_from(admin_app) as client:
        response = client.get("/admin/api/messages", headers=SIGNED_IN)
    by_id = {row["id"]: row["callback_number"] for row in response.json()["messages"]}
    assert by_id == {
        **{number: masked for number, masked in enumerate(shown.values(), start=1)},
        len(shown) + 1: None,
    }
    for raw in shown:
        if len(raw) > 4:
            assert raw not in response.text, raw


def test_rate_limiter_keeps_an_address_that_failed_again() -> None:
    now = [1000.0]
    limiter = RateLimiter(limit=5, window_seconds=60, max_addresses=3, clock=lambda: now[0])
    for address in ("a", "b", "c"):
        limiter.record_failure(address)
        now[0] += 1
    # "a" failed first but fails again, so "b" is now the one that failed longest ago.
    assert limiter.record_failure("a") == 2
    limiter.record_failure("d")
    assert limiter.tracked == 3
    assert limiter.record_failure("a") == 3
    # "b" was dropped: it starts again from one (and "c" makes room for it).
    assert limiter.record_failure("b") == 1


INSERT_ENDED_CALL = text(
    "INSERT INTO call_sessions (id, call_id, started_at, ended_at, office_open, verified,"
    " patient_id, end_reason, turn_count, transcript, transcript_truncated, names_masked)"
    " VALUES (:id, :call_id, :started_at, :ended_at, true, false, NULL, :end_reason, 1,"
    " 'agent: Goodbye.', false, true)"
)
END_REASONS = (
    "completed",
    "emergency",
    "transferred",
    "caller_hung_up",
    "idle_timeout",
    "max_duration",
    "error",
)


def test_calls_filter_by_how_they_ended(admin_app: FastAPI, owner_engine: Engine) -> None:
    # Three calls for each way a call ends, written out by hand, ids in turn.
    with owner_engine.begin() as connection:
        for number in range(1, 3 * len(END_REASONS) + 1):
            connection.execute(
                INSERT_ENDED_CALL,
                {
                    "id": number,
                    "call_id": cid(number),
                    "started_at": T0 + timedelta(minutes=number),
                    "ended_at": T0 + timedelta(minutes=number + 1),
                    "end_reason": END_REASONS[(number - 1) % len(END_REASONS)],
                },
            )
    with client_from(admin_app) as client:
        everything = client.get("/admin/api/calls", headers=SIGNED_IN).json()["calls"]
        assert len(everything) == 21
        for offset, reason in enumerate(END_REASONS):
            response = client.get(f"/admin/api/calls?end_reason={reason}", headers=SIGNED_IN)
            ids = [row["id"] for row in response.json()["calls"]]
            assert ids == [offset + 15, offset + 8, offset + 1], reason
            assert {row["end_reason"] for row in response.json()["calls"]} == {reason}
            # Paging keeps the filter: a page of one at a time.
            seen: list[int] = []
            query = f"end_reason={reason}&limit=1"
            for _ in range(4):
                rows = client.get(f"/admin/api/calls?{query}", headers=SIGNED_IN).json()["calls"]
                if not rows:
                    break
                seen += [row["id"] for row in rows]
                query = f"end_reason={reason}&limit=1&before={rows[-1]['id']}"
            assert seen == ids, reason
        refused = client.get("/admin/api/calls?end_reason=hung_up", headers=SIGNED_IN)
        assert refused.status_code == 422
        assert refused.json() == INVALID_QUERY_BODY
