import asyncio
import hashlib
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY
from console_client import (
    ACTOR,
    ADMIN_USER,
    PASSWORD,
    admin_events,
    cid,
    client_from,
    console_settings,
    events,
    form_token,
    sign_in,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, text

from app.agent.turn import EMERGENCY_TURN, SWALLOWED_TURN
from app.audit.log import CHAIN_LOCK, Action, AuditLog, Event, Outcome, ResourceType
from app.config import StartupSettings
from app.console.views import WITHHELD_TURNS
from app.db.session import create_database_engine, create_session_factory
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DIR = REPO_ROOT / "app" / "console"
TEMPLATES = CONSOLE_DIR / "templates"
HTMX = CONSOLE_DIR / "static" / "htmx-2.0.11.min.js"
# htmx.org 2.0.11's dist/htmx.min.js, from the npm tarball whose sha512 matched the registry's.
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self';"
    " connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none';"
    " frame-ancestors 'none'"
)
HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}
T0 = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
UNAVAILABLE = "could not be loaded"

INSERT_PATIENT = text(
    "INSERT INTO patients (id, first_name, last_name, dob, phone, email, lab_results_status,"
    " balance_due) VALUES ('P001', 'Sarah', 'Johnson', '1985-03-14', '555-201-3344',"
    " 'sarah.j@example.com', 'Pending', 0)"
)
INSERT_REQUEST = text(
    "INSERT INTO requests (id, call_id, patient_id, type, summary, created_at)"
    " VALUES (:id, :call_id, :patient_id, :type, :summary, :created_at)"
)
INSERT_MESSAGE = text(
    "INSERT INTO messages (id, call_id, patient_id, name, callback_number, body, kind,"
    " after_hours, created_at) VALUES (:id, :call_id, :patient_id, :name, :callback_number,"
    " :body, :kind, :after_hours, :created_at)"
)
INSERT_CALL = text(
    "INSERT INTO call_sessions (id, call_id, started_at, ended_at, office_open, verified,"
    " patient_id, end_reason, turn_count, transcript, transcript_truncated, names_masked)"
    " VALUES (:id, :call_id, :started_at, :ended_at, :office_open, :verified, :patient_id,"
    " :end_reason, :turn_count, :transcript, :truncated, :names_masked)"
)


def ssn() -> str:
    return "-".join(["123", "45", "6789"])


def insert_call(
    engine: Engine,
    number: int,
    *,
    transcript: str = "agent: Goodbye.",
    patient_id: str | None = None,
    end_reason: str = "completed",
    names_masked: bool = True,
    truncated: bool = False,
    office_open: bool = True,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            INSERT_CALL,
            {
                "id": number,
                "call_id": cid(number),
                "started_at": T0 + timedelta(minutes=number),
                "ended_at": T0 + timedelta(minutes=number, seconds=161),
                "office_open": office_open,
                "verified": patient_id is not None,
                "patient_id": patient_id,
                "end_reason": end_reason,
                "turn_count": number,
                "transcript": transcript,
                "truncated": truncated,
                "names_masked": names_masked,
            },
        )


def insert_request(
    engine: Engine, number: int, request_type: str, summary: str, patient_id: str | None = None
) -> None:
    with engine.begin() as connection:
        connection.execute(
            INSERT_REQUEST,
            {
                "id": number,
                "call_id": cid(1),
                "patient_id": patient_id,
                "type": request_type,
                "summary": summary,
                "created_at": T0 + timedelta(minutes=number),
            },
        )


def insert_message(engine: Engine, number: int, kind: str, body: str, **values: Any) -> None:
    row = {
        "id": number,
        "call_id": cid(1),
        "patient_id": None,
        "name": None,
        "callback_number": None,
        "body": body,
        "kind": kind,
        "after_hours": False,
        "created_at": T0,
    }
    row.update(values)
    with engine.begin() as connection:
        connection.execute(INSERT_MESSAGE, row)


def call_started(database: StartupSettings, *numbers: int) -> None:
    """call_started events for the calls, appended as the runtime role."""
    audit = AuditLog(SecretStr(TEST_AUDIT_KEY))

    async def run() -> None:
        engine = create_database_engine(database)
        try:
            async with create_session_factory(engine)() as session:
                for offset, number in enumerate(numbers):
                    event = Event(
                        actor="agent",
                        action=Action.CALL_STARTED,
                        resource_type=ResourceType.CALL,
                        outcome=Outcome.SUCCESS,
                        call_id=cid(number),
                        resource_id=cid(number),
                    )
                    await audit.append(session, event, now=T0 + timedelta(seconds=offset))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture
def console(database: StartupSettings) -> FastAPI:
    return create_app(console_settings(database))


@contextmanager
def signed_in(app: FastAPI, address: str = "203.0.113.5") -> Iterator[TestClient]:
    with client_from(app, address) as client:
        sign_in(client)
        yield client


@contextmanager
def chain_lock_held(engine: Engine) -> Iterator[None]:
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
        yield
        holder.rollback()


# Each page and its event: action, resource type, resource id, outcome.
PAGE_EVENTS: dict[str, tuple[str, str, str | None, str]] = {
    "/admin/calls": ("admin_read_calls", "call", None, "success"),
    "/admin/calls?ended=completed": ("admin_read_calls", "call", None, "success"),
    "/admin/calls?before=2": ("admin_read_calls", "call", None, "success"),
    f"/admin/calls/{cid(1)}": ("admin_read_call_detail", "call", cid(1), "success"),
    "/admin/requests": ("admin_read_queue", "request", None, "success"),
    "/admin/requests?type=callback": ("admin_read_queue", "request", None, "success"),
    "/admin/audit": ("admin_read_audit", "audit_event", None, "success"),
    f"/admin/audit?call_id={cid(1)}&action=call_started": (
        "admin_read_audit",
        "audit_event",
        None,
        "success",
    ),
}


def seed_one_of_each(engine: Engine, database: StartupSettings) -> None:
    with engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
    insert_call(engine, 1, patient_id="P001", transcript="caller: Hello.\nagent: Goodbye.")
    insert_request(engine, 1, "refill", "Seeded refill at CVS Main St", "P001")
    call_started(database, 1)


# R4
def test_page_views_are_audited(
    console: FastAPI, owner_engine: Engine, database: StartupSettings
) -> None:
    seed_one_of_each(owner_engine, database)
    with signed_in(console) as client:
        expected: list[tuple[str, str, str | None, str]] = [
            ("admin_signed_in", "console", None, "success")
        ]
        for page, event in PAGE_EVENTS.items():
            for htmx in (False, True):
                response = client.get(page, headers={"HX-Request": "true"} if htmx else {})
                assert response.status_code == 200, (page, htmx)
                expected.append(event)
                assert admin_events(owner_engine) == expected, (page, htmx)

        token = form_token(client.get("/admin/audit"))
        expected.append(PAGE_EVENTS["/admin/audit"])
        checked = client.post("/admin/audit/check", data={"token": token})
        assert checked.status_code == 303
        expected.append(("admin_verify_audit", "audit_event", None, "success"))
        assert admin_events(owner_engine) == expected

        # Nothing found, nothing read, nothing written: an unknown call, a refused filter, the
        # bare address and the sign-in page.
        for page, status in (
            (f"/admin/calls/{cid(99)}", 404),
            ("/admin/calls/not-a-call", 404),
            ("/admin/calls?ended=nope", 400),
            ("/admin/calls?ended=completed&ended=error", 400),
            ("/admin/calls?before=0", 400),
            ("/admin/calls?unknown=1", 400),
            ("/admin/requests?type=nope", 400),
            ("/admin/audit?action=nope", 400),
            ("/admin/audit?call_id=abc", 400),
            ("/admin", 303),
            ("/admin/nothing-here", 404),
        ):
            assert client.get(page).status_code == status, page
        assert admin_events(owner_engine) == expected
        # Every event is the admin's, outside any call.
        assert all(row[1] is None for row in events(owner_engine) if row[2] == ACTOR)

        # No event, no data: with the chain's lock held every read fails, and the page shows
        # the error state and nothing of the rows.
        for page in PAGE_EVENTS:
            for htmx in (False, True):
                with chain_lock_held(owner_engine):
                    response = client.get(page, headers={"HX-Request": "true"} if htmx else {})
                assert response.status_code == 503, page
                assert UNAVAILABLE in response.text, page
                assert "<table" not in response.text, page
                for seeded in ("Seeded refill", "CVS", "Hello.", "P001"):
                    assert seeded not in response.text, (page, seeded)
        assert admin_events(owner_engine) == expected


def raw_identifiers() -> list[str]:
    return [
        ssn(),
        "45-6789",
        "1985-03-14",
        "3/14/1985",
        "555-201-3344",
        "201-3344",
        "555-867-5309",
        "867-5309",
        "8675309",
        "sarah.j@",
    ]


CALLER_NAMES = ["sarah", "johnson", "dana", "whitfield"]


def every_kind(subject: str) -> str:
    """A text holding each kind of identifier and both callers' names, short enough for a
    message's body (120 characters)."""
    text = (
        f"{subject} Sarah Johnson, DANA Whitfield, dob 1985-03-14, 3/14/1985, 555-201-3344,"
        f" {ssn()}, sarah.j@example.com"
    )
    assert len(text) <= 120
    return text


# R5
def test_pages_are_masked(
    console: FastAPI, owner_engine: Engine, database: StartupSettings
) -> None:
    with owner_engine.begin() as connection:
        connection.execute(INSERT_PATIENT)
    insert_call(
        owner_engine,
        1,
        patient_id="P001",
        transcript=f"caller: {every_kind('Caller')}\nagent: {every_kind('Agent')}",
    )
    insert_call(owner_engine, 2, transcript=f"caller: {every_kind('Unmasked')}", names_masked=False)
    for number, request_type in enumerate(
        ("refill", "doctor_message", "booking", "callback", "transfer"), start=1
    ):
        insert_request(
            owner_engine,
            number,
            request_type,
            every_kind(request_type),
            "P001" if number % 2 else None,
        )
    insert_message(
        owner_engine,
        1,
        "callback",
        every_kind("callback"),
        name="Dana Whitfield",
        callback_number="5558675309",
        after_hours=True,
    )
    insert_message(
        owner_engine, 2, "doctor_message", every_kind("doctor_message"), patient_id="P001"
    )
    call_started(database, 1, 2)

    pages = [
        "/admin/calls",
        f"/admin/calls/{cid(1)}",
        f"/admin/calls/{cid(2)}",
        "/admin/requests",
        *(
            f"/admin/requests?type={kind}"
            for kind in ("refill", "doctor_message", "booking", "callback", "transfer")
        ),
        "/admin/audit",
    ]
    with signed_in(console) as client:
        bodies = {page: client.get(page) for page in pages}
    for page, response in bodies.items():
        assert response.status_code == 200, page
        for value in raw_identifiers():
            assert value not in response.text, (page, value)
        lowered = response.text.lower()
        for name in CALLER_NAMES:
            assert name not in lowered, (page, name)

    # What is shown instead: the masked forms, the last four of the callback number only.
    queue = bodies["/admin/requests"].text
    assert "***-***-5309" in queue
    assert "***-**-****" in queue and "****-**-14" in queue and "s***@***.com" in queue
    assert "&lt;PERSON&gt;" in queue
    assert "left after hours" in queue
    detail = bodies[f"/admin/calls/{cid(1)}"].text
    assert "***-***-3344" in detail and "&lt;PERSON&gt;" in detail
    hidden = bodies[f"/admin/calls/{cid(2)}"].text
    assert "Transcript hidden: names were not masked" in hidden
    assert "Unmasked" not in hidden


# R8
def test_console_headers_and_csp(
    console: FastAPI, owner_engine: Engine, database: StartupSettings
) -> None:
    seed_one_of_each(owner_engine, database)
    responses: dict[str, Response] = {}
    with client_from(console) as client:
        responses["sign-in page"] = client.get("/admin/login")
        responses["failed sign-in"] = client.post(
            "/admin/login",
            data={"user": "x", "password": "y", "token": form_token(responses["sign-in page"])},
        )
        responses["forged sign-in"] = client.post("/admin/login", data={"user": "x"})
        responses["no session"] = client.get("/admin/calls")
        responses["no session, htmx"] = client.get("/admin/calls", headers={"HX-Request": "true"})
        responses["bare address"] = client.get("/admin")
        for asset in (
            "console.css",
            "htmx-2.0.11.min.js",
            "fonts/inter-latin-400-normal.woff2",
            "fonts/space-grotesk-latin-600-normal.woff2",
            "licences/htmx-LICENSE.txt",
            "missing.css",
        ):
            responses[asset] = client.get(f"/admin/static/{asset}")
        sign_in(client)
        for page in PAGE_EVENTS:
            responses[page] = client.get(page)
        responses["partial"] = client.get("/admin/calls", headers={"HX-Request": "true"})
        responses["unknown call"] = client.get(f"/admin/calls/{cid(99)}")
        responses["refused filter"] = client.get("/admin/calls?ended=nope")
        responses["unknown page"] = client.get("/admin/nothing")
        responses["wrong method"] = client.delete("/admin/calls")
        responses["forged post"] = client.post("/admin/audit/check", data={"token": "x"})
        with chain_lock_held(owner_engine):
            responses["read failed"] = client.get("/admin/requests")
    statuses = {name: response.status_code for name, response in responses.items()}
    assert statuses["missing.css"] == 404
    assert statuses["read failed"] == 503
    assert statuses["wrong method"] == 405
    for name, response in responses.items():
        for header, value in HEADERS.items():
            assert response.headers.get(header) == value, (name, header)
        assert "unsafe-inline" not in response.headers["content-security-policy"]

    # No inline script, no inline style, no handler attribute in any template: the CSP would
    # refuse them, and none is needed.
    inline = re.compile(
        r"<script(?![^>]*\bsrc=)|<style|\sstyle=|\son[a-z]+=|javascript:", re.IGNORECASE
    )
    templates = sorted(TEMPLATES.rglob("*.html"))
    assert len(templates) >= 6
    for template in templates:
        assert not inline.findall(template.read_text(encoding="utf-8")), template.name
    for response in responses.values():
        if response.headers.get("content-type", "").startswith("text/html"):
            assert not inline.findall(response.text)
            assert "http://" not in response.text and "https://" not in response.text

    # The vendored htmx is the pinned file, byte for byte.
    assert hashlib.sha256(HTMX.read_bytes()).hexdigest() == HTMX_SHA256


# R9
def test_stored_text_is_escaped(
    console: FastAPI, owner_engine: Engine, database: StartupSettings
) -> None:
    script = "<script>alert(1)</script>"
    image = '"><img src=x onerror=alert(2)>'
    insert_call(owner_engine, 1, transcript=f"caller: {script} & {image}\nagent: <b>bold</b>")
    insert_request(owner_engine, 1, "callback", f"Call back {script}")
    insert_request(owner_engine, 2, "transfer", image)
    insert_message(owner_engine, 1, "callback", f"Call back {script}", callback_number=image)
    with signed_in(console) as client:
        pages = [
            client.get(f"/admin/calls/{cid(1)}"),
            client.get("/admin/requests"),
            client.get("/admin/requests", headers={"HX-Request": "true"}),
            client.get("/admin/audit?call_id=%3Cscript%3E"),
        ]
    for response in pages:
        for markup in ("<script>alert", "<img src=x", "<b>bold"):
            assert markup not in response.text, (response.url, markup)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[0].text
    assert "&lt;b&gt;bold&lt;/b&gt;" in pages[0].text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[1].text
    assert "&#34;&gt;&lt;img src=x onerror=alert(2)&gt;" in pages[1].text
    assert pages[3].status_code == 400


# Neighbours
def test_call_detail_states(console: FastAPI, owner_engine: Engine) -> None:
    insert_call(
        owner_engine,
        1,
        transcript=(
            f"agent: Hello.\ncaller: {SWALLOWED_TURN}\nagent: Thank you.\ncaller: {EMERGENCY_TURN}"
        ),
        end_reason="emergency",
        truncated=True,
        office_open=False,
    )
    insert_call(owner_engine, 2, transcript="caller: hidden words", names_masked=False)
    with signed_in(console) as client:
        shown = client.get(f"/admin/calls/{cid(1)}").text
        hidden = client.get(f"/admin/calls/{cid(2)}").text
        missing = client.get(f"/admin/calls/{cid(3)}")
        malformed = client.get(f"/admin/calls/{cid(1)[:-1]}g")
    assert shown.count('class="turn turn--caller turn--withheld"') == 2
    assert "Ended: emergency" in shown
    assert "office closed" in shown
    assert "The transcript was cut" in shown
    assert "Transcript hidden: names were not masked" in hidden
    assert "hidden words" not in hidden
    for response in (missing, malformed):
        assert response.status_code == 404
        assert "No call with this id" in response.text
    # The console marks the turns the agent withholds, as app/agent/turn.py writes them.
    assert {SWALLOWED_TURN, EMERGENCY_TURN} == WITHHELD_TURNS


def test_call_history_filters_and_paging(console: FastAPI, owner_engine: Engine) -> None:
    with signed_in(console) as client:
        empty = client.get("/admin/calls").text
        assert "No calls yet" in empty
        for number in range(1, 61):
            insert_call(owner_engine, number, end_reason="emergency" if number % 10 else "error")
        first = client.get("/admin/calls").text
        assert first.count('class="id-link"') == 50
        assert 'href="/admin/calls?before=11"' in first
        second = client.get("/admin/calls?before=11").text
        assert second.count('class="id-link"') == 10
        assert "Show older calls" not in second
        errors = client.get("/admin/calls?ended=error").text
        assert errors.count('class="id-link"') == 6
        assert 'aria-current="true">Error' in errors
        assert "No calls match this filter" in client.get("/admin/calls?ended=idle_timeout").text
        partial = client.get("/admin/calls?ended=error", headers={"HX-Request": "true"}).text
        assert partial.lstrip().startswith('<section class="card" id="calls-card"')
        assert "<html" not in partial


def test_requests_filter(console: FastAPI, owner_engine: Engine) -> None:
    for number, request_type in enumerate(("refill", "transfer", "refill"), start=1):
        insert_request(owner_engine, number, request_type, f"{request_type} number {number}")
    with signed_in(console) as client:
        bookings = client.get("/admin/requests?type=booking").text
        refills = client.get("/admin/requests?type=refill").text
        transfers = client.get("/admin/requests?type=transfer").text
    assert "No requests match this filter" in bookings
    assert "refill number 1" in refills and "refill number 3" in refills
    assert "transfer number 2" not in refills
    assert "Caller passed to staff" in transfers
    assert "Mark handled" in refills and "disabled" in refills


def test_chain_check_states(
    console: FastAPI,
    owner_engine: Engine,
    database: StartupSettings,
    tamper_with_audit_events: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_started(database, 1, 2, 3)
    with signed_in(console) as client:
        token = form_token(client.get("/admin/audit"))
        intact = client.post("/admin/audit/check", data={"token": token}, follow_redirects=True)
        assert "Chain intact" in intact.text
        partial = client.post(
            "/admin/audit/check", data={"token": token}, headers={"HX-Request": "true"}
        )
        assert partial.status_code == 200
        assert "Chain intact" in partial.text and "<html" not in partial.text

        tamper_with_audit_events(
            owner_engine, "UPDATE audit_events SET action = 'call_ended' WHERE seq = 2"
        )
        broken = client.post("/admin/audit/check", data={"token": token}, follow_redirects=True)
        assert "Chain broken" in broken.text
        assert "event 2" in broken.text and "mac_mismatch" in broken.text

        entered = threading.Event()
        release = threading.Event()
        original = AuditLog.verify

        async def held(self: AuditLog, session: Any) -> Any:
            entered.set()
            await asyncio.to_thread(release.wait, 10)
            return await original(self, session)

        monkeypatch.setattr(AuditLog, "verify", held)
        first: list[Response] = []
        worker = threading.Thread(
            target=lambda: first.append(
                client.post("/admin/audit/check", data={"token": token}, follow_redirects=True)
            )
        )
        worker.start()
        assert entered.wait(10)
        busy = client.post(
            "/admin/audit/check", data={"token": token}, headers={"HX-Request": "true"}
        )
        api_busy = client.get("/admin/api/audit/verify", auth=(ADMIN_USER, PASSWORD))
        release.set()
        worker.join(10)
    assert "A check is already running" in busy.text
    # One check at a time across the console and the API.
    assert api_busy.status_code == 429
    assert first[0].status_code == 200
