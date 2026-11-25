"""The admin API's three writes: marking a request handled, showing a callback's details, and
changing the config (calls on or off, the office's hours or forced open or closed)."""

import logging
import re
from typing import Any

import pytest
from admin_client import (
    ACTOR,
    ADMIN_USER,
    OWN_ORIGIN,
    SIGNED_IN,
    Clock,
    admin_events,
    admin_settings,
    admin_with_clock,
    cid,
    client_from,
    events,
    try_sign_in,
)
from conftest import VOICE_SECRETS
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.audit.log import CHAIN_LOCK
from app.config import StartupSettings

CONSOLE = "https://console.example.test"
HANDLED = "/admin/api/queue/requests/1/handled"
CALLBACK = "/admin/api/queue/requests/1/callback"
CONFIG = "/admin/api/config"
QUEUE = "/admin/api/queue"
CALLS_OFF = {"key": "calls", "value": "off", "confirm": True}

SIGN_IN_REQUIRED = {"detail": "Sign-in required"}
FORBIDDEN = {"detail": "Forbidden"}
NOT_FOUND = {"detail": "Not Found"}
UNAVAILABLE = {"detail": "Service unavailable"}
INVALID = {"detail": "Invalid request"}
TOO_LARGE = {"detail": "Request too large"}
NOT_JSON = {"detail": "Content-Type must be application/json"}
ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00")

# A caller's name and a full callback number: shown by the callback route only.
CALLER = "Jane Doe"
NUMBER = "5552013344"
BILL = "About my bill"

INSERT_REQUEST = text(
    "INSERT INTO requests (id, call_id, type, summary) VALUES (:id, :call_id, :type, :summary)"
)
INSERT_MESSAGE = text(
    "INSERT INTO messages (call_id, name, callback_number, body, kind)"
    " VALUES (:call_id, :name, :number, :body, :kind)"
)
MARKS = text("SELECT source, row_id, marked_by FROM handled_marks ORDER BY id")
CHANGES = text("SELECT key, value, changed_by FROM config_changes ORDER BY id")


def seed(engine: Engine) -> None:
    """Request 1, a call back with the caller's name and number; request 2, a refill with no
    number to call back; request 3, a doctor's message whose message holds neither."""
    with engine.begin() as connection:
        connection.execute(
            INSERT_REQUEST, {"id": 1, "call_id": cid(1), "type": "callback", "summary": BILL}
        )
        connection.execute(
            INSERT_MESSAGE,
            {"call_id": cid(1), "name": CALLER, "number": NUMBER, "body": BILL, "kind": "callback"},
        )
        connection.execute(
            INSERT_REQUEST, {"id": 2, "call_id": cid(2), "type": "refill", "summary": "Refill"}
        )
        connection.execute(
            INSERT_REQUEST,
            {"id": 3, "call_id": cid(3), "type": "doctor_message", "summary": "Ask the doctor"},
        )
        connection.execute(
            INSERT_MESSAGE,
            {
                "call_id": cid(3),
                "name": None,
                "number": None,
                "body": "Ask the doctor",
                "kind": "doctor_message",
            },
        )


def rows(engine: Engine, query: Any) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(query)]


def signed_in(client: TestClient, origin: str = OWN_ORIGIN) -> dict[str, str]:
    """Sign in from a browser at `origin`; the headers a write from there carries."""
    client.cookies.clear()
    answer = try_sign_in(client, origin=origin)
    assert answer.status_code == 200, answer.status_code
    return {"X-CSRF-Token": answer.json()["csrf_token"], "Origin": origin}


SIGNED_IN_EVENT = ("admin_signed_in", "session", None, "success")


def mark_event(row: str, outcome: str = "success") -> tuple[Any, ...]:
    return ("admin_marked_handled", "request", row, outcome)


def reveal_event(row: str) -> tuple[Any, ...]:
    return ("admin_revealed_callback", "request", row, "success")


def config_event(key: str, outcome: str = "success") -> tuple[Any, ...]:
    return ("admin_changed_config", "config", key, outcome)


# R1
def test_writes_need_session_and_token(database: StartupSettings, owner_engine: Engine) -> None:
    seed(owner_engine)
    writes = {HANDLED: {}, CALLBACK: {}, CONFIG: CALLS_OFF}
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app) as client:
        # No credentials, and Basic alone, are asked to sign in, and never shown the prompt.
        for path, body in writes.items():
            for headers in ({}, SIGNED_IN, {**SIGNED_IN, "Origin": OWN_ORIGIN}):
                answer = client.post(path, json=body, headers=headers)
                assert answer.status_code == 401, (path, headers)
                assert answer.json() == SIGN_IN_REQUIRED, (path, headers)
                assert "www-authenticate" not in answer.headers, (path, headers)

        write = signed_in(client)
        token = write["X-CSRF-Token"]
        forged = {
            "no token": ({"Origin": OWN_ORIGIN}, 403),
            "empty token": ({"X-CSRF-Token": "", "Origin": OWN_ORIGIN}, 403),
            "wrong token": ({"X-CSRF-Token": token[:-1], "Origin": OWN_ORIGIN}, 403),
            "longer token": ({"X-CSRF-Token": token + "x", "Origin": OWN_ORIGIN}, 403),
            "no origin": ({"X-CSRF-Token": token}, 403),
            "other origin": ({"X-CSRF-Token": token, "Origin": "http://other.example"}, 401),
            "basic and token": ({**SIGNED_IN, "Origin": OWN_ORIGIN}, 403),
        }
        for path, body in writes.items():
            for name, (headers, status) in forged.items():
                answer = client.post(path, json=body, headers=headers)
                assert answer.status_code == status, (path, name)
                expected = FORBIDDEN if status == 403 else SIGN_IN_REQUIRED
                assert answer.json() == expected, (path, name)
                assert CALLER not in answer.text and NUMBER not in answer.text, (path, name)

        # GET on a write path changes nothing: the mark and the details have no GET route, and
        # the config's GET is its audited read.
        assert client.get(HANDLED, headers=SIGNED_IN).status_code == 405
        assert client.get(CALLBACK, headers=SIGNED_IN).status_code == 405
        assert client.get(CONFIG, headers=SIGNED_IN).status_code == 200
        assert rows(owner_engine, MARKS) == []
        assert rows(owner_engine, CHANGES) == []
        assert admin_events(owner_engine) == [
            SIGNED_IN_EVENT,
            ("admin_read_config", "config", None, "success"),
        ]

        # The session with its token, from the API's own origin, writes.
        assert client.post(HANDLED, json={}, headers=write).status_code == 201
        assert client.post(CALLBACK, json={}, headers=write).status_code == 200
        assert client.post(CONFIG, json=CALLS_OFF, headers=write).status_code == 201
    assert rows(owner_engine, MARKS) == [("requests", 1, ADMIN_USER)]
    assert rows(owner_engine, CHANGES) == [("calls", "off", ADMIN_USER)]

    # With CONSOLE_ORIGIN set, a session writes from that origin only: from the API's own, its
    # cookie is not honoured.
    console = admin_with_clock(admin_settings(database, console_origin=CONSOLE), Clock())
    with client_from(console) as client:
        write = signed_in(client, origin=CONSOLE)
        own = {**write, "Origin": OWN_ORIGIN}
        office = {"key": "office", "value": "closed", "confirm": True}
        answer = client.post(CONFIG, json=office, headers=own)
        assert (answer.status_code, answer.json()) == (401, SIGN_IN_REQUIRED)
        assert client.post(CONFIG, json=office, headers=write).status_code == 201
    assert rows(owner_engine, CHANGES) == [
        ("calls", "off", ADMIN_USER),
        ("office", "closed", ADMIN_USER),
    ]


# R2
def test_no_write_without_its_audit_event(
    database: StartupSettings, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    seed(owner_engine)
    caplog.set_level(logging.DEBUG)
    writes = {HANDLED: {}, CALLBACK: {}, CONFIG: CALLS_OFF}
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app) as client:
        write = signed_in(client)
        # The audit chain's lock held elsewhere: no event can be appended, so nothing is
        # marked, shown or changed, and the answer says only that the service is unavailable.
        for path, body in writes.items():
            with owner_engine.connect() as holder:
                holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
                answer = client.post(path, json=body, headers=write)
                holder.rollback()
            assert (answer.status_code, answer.json()) == (503, UNAVAILABLE), path
        assert rows(owner_engine, MARKS) == []
        assert rows(owner_engine, CHANGES) == []
        assert admin_events(owner_engine) == [SIGNED_IN_EVENT]
        assert CALLER not in caplog.text and NUMBER not in caplog.text

        # With the lock free, each write is made with its event.
        assert client.post(HANDLED, json={}, headers=write).status_code == 201
        assert client.post(CALLBACK, json={}, headers=write).status_code == 200
        assert client.post(CONFIG, json=CALLS_OFF, headers=write).status_code == 201
    assert rows(owner_engine, MARKS) == [("requests", 1, ADMIN_USER)]
    assert rows(owner_engine, CHANGES) == [("calls", "off", ADMIN_USER)]
    assert admin_events(owner_engine) == [
        SIGNED_IN_EVENT,
        mark_event("1"),
        reveal_event("1"),
        config_event("calls"),
    ]


# R3
def test_callback_details_are_shown_once_and_only_there(
    database: StartupSettings, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    seed(owner_engine)
    caplog.set_level(logging.DEBUG)
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app) as client:
        write = signed_in(client)
        shown = client.post(CALLBACK, json={}, headers=write)
        assert shown.status_code == 200
        assert shown.json() == {
            "source": "requests",
            "id": 1,
            "callbacks": [{"name": CALLER, "callback_number": NUMBER}],
        }

        # Only a row with a number to call back can be asked for; any other is not found, and
        # not audited.
        for path in (
            "/admin/api/queue/requests/2/callback",
            "/admin/api/queue/requests/3/callback",
            "/admin/api/queue/requests/99/callback",
            "/admin/api/queue/messages/1/callback",
            "/admin/api/queue/requests/01/callback",
            "/admin/api/queue/requests/1x/callback",
            f"/admin/api/queue/requests/{10**9}/callback",
        ):
            answer = client.post(path, json={}, headers=write)
            assert (answer.status_code, answer.json()) == (404, NOT_FOUND), path
        # A body the route does not take is refused with a fixed answer that echoes nothing.
        refused = client.post(CALLBACK, json={"name": CALLER, "number": NUMBER}, headers=write)
        assert (refused.status_code, refused.json()) == (422, INVALID)

        # No other answer holds the name or the number: the reads, the mark, the config, the
        # session.
        others = [
            client.get(f"{QUEUE}?state=all", headers=SIGNED_IN),
            client.get("/admin/api/messages", headers=SIGNED_IN),
            client.get("/admin/api/requests", headers=SIGNED_IN),
            client.get("/admin/api/audit", headers=SIGNED_IN),
            client.get(CONFIG, headers=SIGNED_IN),
            client.get("/admin/api/session"),
            client.post(HANDLED, json={}, headers=write),
            client.post(CONFIG, json=CALLS_OFF, headers=write),
        ]
        for answer in others:
            assert answer.status_code in (200, 201), answer.request.url
            assert CALLER not in answer.text, answer.request.url
            assert NUMBER not in answer.text and NUMBER[-7:] not in answer.text

    # The event names the admin and the row; the audit rows hold nothing of the details.
    assert reveal_event("1") in admin_events(owner_engine)
    assert [row for row in admin_events(owner_engine) if row[0] == "admin_revealed_callback"] == [
        reveal_event("1")
    ]
    stored = str(events(owner_engine))
    assert CALLER not in stored and NUMBER not in stored
    # Nor any log line, the read's own included.
    assert CALLER not in caplog.text and NUMBER not in caplog.text
    assert "admin_revealed_callback" in caplog.text


# R5
def test_handled_once(database: StartupSettings, owner_engine: Engine) -> None:
    seed(owner_engine)
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app) as client:
        write = signed_in(client)
        first = client.post(HANDLED, json={}, headers=write)
        assert first.status_code == 201
        mark = first.json()
        assert set(mark) == {"source", "id", "handled"}
        assert (mark["source"], mark["id"], mark["handled"]["by"]) == ("requests", 1, ADMIN_USER)
        assert ISO_INSTANT.fullmatch(mark["handled"]["at"])
        # Again: the same mark, nothing written but the event.
        again = client.post(HANDLED, json={}, headers=write)
        assert (again.status_code, again.json()) == (200, mark)
        # A request that is not there, or a source or id that cannot be one, is not found.
        for path in (
            "/admin/api/queue/requests/99/handled",
            "/admin/api/queue/messages/1/handled",
            "/admin/api/queue/calls/1/handled",
            "/admin/api/queue/requests/0/handled",
            "/admin/api/queue/requests/-1/handled",
        ):
            answer = client.post(path, json={}, headers=write)
            assert (answer.status_code, answer.json()) == (404, NOT_FOUND), path

        # The queue shows the mark on its row, and keeps what is left to do by default.
        def ids(query: str) -> list[int]:
            answer = client.get(f"{QUEUE}{query}", headers=SIGNED_IN)
            assert answer.status_code == 200, query
            return [row["id"] for row in answer.json()["requests"]]

        assert ids("") == [3, 2]
        assert ids("?state=todo") == [3, 2]
        assert ids("?state=handled") == [1]
        assert ids("?state=all") == [3, 2, 1]
        assert ids("?state=handled&type=refill") == []
        rows_shown = client.get(f"{QUEUE}?state=all", headers=SIGNED_IN).json()["requests"]
        assert [row["handled"] for row in rows_shown] == [None, None, mark["handled"]]
        for state in ("done", "", "TODO", "all,todo"):
            answer = client.get(f"{QUEUE}?state={state}", headers=SIGNED_IN)
            assert (answer.status_code, answer.json()) == (422, {"detail": "Invalid query"})

    assert rows(owner_engine, MARKS) == [("requests", 1, ADMIN_USER)]
    assert [row for row in admin_events(owner_engine) if row[0] == "admin_marked_handled"] == [
        mark_event("1"),
        mark_event("1", "already"),
    ]
    # The table takes one mark per row whoever writes it, and only of a known source.
    for source, row_id, by in (("requests", 1, "other"), ("messages", 7, ADMIN_USER)):
        with pytest.raises(IntegrityError), owner_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO handled_marks (source, row_id, marked_by)"
                    " VALUES (:source, :row_id, :by)"
                ),
                {"source": source, "row_id": row_id, "by": by},
            )


# R6
def test_config_accepts_only_known_values(database: StartupSettings, owner_engine: Engine) -> None:
    app = admin_with_clock(admin_settings(database), Clock())
    refused_bodies: list[Any] = [
        {"key": "calls", "value": "off"},
        {"key": "calls", "value": "off", "confirm": False},
        {"key": "calls", "value": "off", "confirm": "true"},
        {"key": "calls", "value": "off", "confirm": 1},
        {"key": "calls", "value": "open", "confirm": True},
        {"key": "office", "value": "off", "confirm": True},
        {"key": "office", "value": "OPEN", "confirm": True},
        {"key": "lights", "value": "on", "confirm": True},
        {"key": "calls", "value": True, "confirm": True},
        {"key": "calls", "value": "off", "confirm": True, "reason": "testing"},
        {"value": "off", "confirm": True},
        {},
        [],
        "calls off",
    ]
    with client_from(app) as client:
        write = signed_in(client)
        for body in refused_bodies:
            answer = client.post(CONFIG, json=body, headers=write)
            assert (answer.status_code, answer.json()) == (422, INVALID), body
        # JSON only, and at most 1 KiB of it.
        answer = client.post(CONFIG, content=b"key=calls&value=off&confirm=true", headers=write)
        assert (answer.status_code, answer.json()) == (415, NOT_JSON)
        padded = {**CALLS_OFF, "pad": "x" * 1024}
        answer = client.post(CONFIG, json=padded, headers=write)
        assert (answer.status_code, answer.json()) == (413, TOO_LARGE)
        answer = client.post(
            CONFIG, content=b"{", headers={**write, "Content-Type": "application/json"}
        )
        assert (answer.status_code, answer.json()) == (422, INVALID)
        assert rows(owner_engine, CHANGES) == []
        assert admin_events(owner_engine) == [SIGNED_IN_EVENT]

        # Each known value is taken, once; setting the value a key already has writes nothing
        # but its event, the default included.
        changed = client.post(CONFIG, json=CALLS_OFF, headers=write)
        assert changed.status_code == 201
        change = changed.json()
        assert ISO_INSTANT.fullmatch(change.pop("changed_at"))
        assert change == {
            "key": "calls",
            "value": "off",
            "changed_by": ADMIN_USER,
            "applies_within_seconds": 5,
        }
        unchanged = client.post(CONFIG, json=CALLS_OFF, headers=write)
        assert unchanged.status_code == 200
        assert unchanged.json() == {**change, "changed_at": changed.json()["changed_at"]}
        default = client.post(
            CONFIG, json={"key": "office", "value": "schedule", "confirm": True}, headers=write
        )
        assert (default.status_code, default.json()) == (
            200,
            {
                "key": "office",
                "value": "schedule",
                "changed_by": None,
                "changed_at": None,
                "applies_within_seconds": 5,
            },
        )
        for key, value in (("office", "open"), ("office", "closed"), ("calls", "on")):
            body = {"key": key, "value": value, "confirm": True}
            assert client.post(CONFIG, json=body, headers=write).status_code == 201, body

        shown = client.get(CONFIG, headers=SIGNED_IN).json()
    assert {key: setting["value"] for key, setting in shown["config"].items()} == {
        "calls": "on",
        "office": "closed",
    }
    assert rows(owner_engine, CHANGES) == [
        ("calls", "off", ADMIN_USER),
        ("office", "open", ADMIN_USER),
        ("office", "closed", ADMIN_USER),
        ("calls", "on", ADMIN_USER),
    ]
    with owner_engine.connect() as connection:
        reasons = connection.execute(
            text(
                "SELECT outcome, reason FROM audit_events"
                " WHERE action = 'admin_changed_config' ORDER BY seq"
            )
        ).all()
    assert [tuple(row) for row in reasons] == [
        ("success", "calls_off"),
        ("unchanged", "calls_off"),
        ("unchanged", "office_schedule"),
        ("success", "office_open"),
        ("success", "office_closed"),
        ("success", "calls_on"),
    ]

    # The table itself takes only the known keys, each with its own values.
    for key, value in (("calls", "open"), ("office", "off"), ("lights", "on"), ("calls", "")):
        with pytest.raises(IntegrityError), owner_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO config_changes (key, value, changed_by)"
                    " VALUES (:key, :value, 'clinic_admin')"
                ),
                {"key": key, "value": value},
            )


# Neighbours
def test_config_read_shows_values_and_facts(
    database: StartupSettings, owner_engine: Engine
) -> None:
    settings = admin_settings(
        database,
        anthropic_api_key=VOICE_SECRETS["ANTHROPIC_API_KEY"],
        deepgram_api_key=VOICE_SECRETS["DEEPGRAM_API_KEY"],
        elevenlabs_api_key=VOICE_SECRETS["ELEVENLABS_API_KEY"],
        llm_model="claude-sonnet-5-5",
        max_concurrent_calls=4,
        max_call_seconds=300,
        simulated_now="2026-11-24T15:40:00Z",
    )
    app = admin_with_clock(settings, Clock())
    with client_from(app) as client:
        first = client.get(CONFIG, headers=SIGNED_IN)
        client.post(CONFIG, json=CALLS_OFF, headers=signed_in(client))
        second = client.get(CONFIG, headers=SIGNED_IN)
    facts = {
        "environment": "dev",
        "model": "claude-sonnet-5-5",
        "speech_to_text": "deepgram",
        "text_to_speech": "elevenlabs",
        "max_concurrent_calls": 4,
        "max_call_seconds": 300,
        "test_clock": True,
    }
    default = {"value": "schedule", "changed_by": None, "changed_at": None}
    assert first.json() == {
        "config": {"calls": {**default, "value": "on"}, "office": default},
        "applies_within_seconds": 5,
        "facts": facts,
    }
    calls = second.json()["config"]["calls"]
    assert (calls["value"], calls["changed_by"]) == ("off", ADMIN_USER)
    assert ISO_INSTANT.fullmatch(calls["changed_at"])
    # No key, nor its first characters (a vendor's name alone is not a key's prefix).
    for secret in VOICE_SECRETS.values():
        for answer in (first, second):
            assert secret not in answer.text and secret[:12] not in answer.text
    assert [row[0] for row in admin_events(owner_engine)].count("admin_read_config") == 2


def test_write_bodies_are_bounded(database: StartupSettings, owner_engine: Engine) -> None:
    seed(owner_engine)
    app = admin_with_clock(admin_settings(database), Clock())
    refused: list[Any] = [{"note": "x"}, [], "x"]
    with client_from(app) as client:
        write = signed_in(client)
        for path in (HANDLED, CALLBACK):
            for body in refused:
                answer = client.post(path, json=body, headers=write)
                assert (answer.status_code, answer.json()) == (422, INVALID), (path, body)
            for content in (b"null", b"{}{}", b""):
                answer = client.post(
                    path, content=content, headers={**write, "Content-Type": "application/json"}
                )
                assert (answer.status_code, answer.json()) == (422, INVALID), (path, content)
            answer = client.post(path, content=b"", headers=write)
            assert (answer.status_code, answer.json()) == (415, NOT_JSON), path
            answer = client.post(
                path,
                content=b"{" + b" " * 1024 + b"}",
                headers={**write, "Content-Type": "application/json"},
            )
            assert (answer.status_code, answer.json()) == (413, TOO_LARGE), path
    assert rows(owner_engine, MARKS) == []
    assert admin_events(owner_engine) == [SIGNED_IN_EVENT]
    assert ACTOR == f"admin:{ADMIN_USER}"
