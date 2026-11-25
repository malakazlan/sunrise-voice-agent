"""The admin API's browser sessions: signing in and out, the session's anti-forgery rules, and
every route behind a session or HTTP Basic."""

import ast
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from admin_client import (
    ADMIN_USER,
    COOKIE,
    HOST_COOKIE,
    OWN_ORIGIN,
    PASSWORD,
    SESSION_ROUTE,
    SIGNED_IN,
    STREAM_ROUTE,
    Clock,
    admin_events,
    admin_settings,
    admin_with_clock,
    basic,
    cid,
    client_from,
    events,
    sign_in,
    sign_out,
    try_sign_in,
)
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app.api import sessions as admin_sessions
from app.api.sessions import SessionTable
from app.audit.log import CHAIN_LOCK
from app.config import StartupSettings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DIR = REPO_ROOT / "app" / "api"

TRIED_USER = "intruder_name"
TRIED_PASSWORD = "-".join(["tried", "pass", "not", "for", "logs"])
SEEDED_SUMMARY = "Refill seeded for the session test"
REFUSED_BODY = {"detail": "Sign-in required"}
FAILED_BODY = {"detail": "Sign-in failed"}
LIMITED_BODY = {"detail": "Too many failed sign-ins"}
FORBIDDEN_BODY = {"detail": "Forbidden"}
CHALLENGE = 'Basic realm="admin", charset="UTF-8"'
CONSOLE_ORIGIN = "http://console.test"

# Every admin route that reads, as a browser or a script asks for it.
READ_ROUTES = [
    "/admin/api/requests",
    "/admin/api/messages",
    "/admin/api/audit",
    "/admin/api/audit/verify",
    "/admin/api/calls",
    f"/admin/api/calls/{cid(1)}",
    f"/admin/api/calls/{cid(1)}/detail",
    "/admin/api/calls/live",
    f"/admin/api/calls/{cid(1)}/turns",
    "/admin/api/appointments",
    "/admin/api/slots",
    "/admin/api/guardrails/summary",
    "/admin/api/metrics/summary",
    "/admin/api/queue",
]
# The routes a session alone opens.
SESSION_ONLY = [("GET", SESSION_ROUTE), ("DELETE", SESSION_ROUTE), ("GET", STREAM_ROUTE)]
EVERY_ROUTE = [("GET", route) for route in READ_ROUTES] + SESSION_ONLY


def seed(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO requests (id, call_id, type, summary) VALUES (1, :call_id, 'refill',"
                " :summary)"
            ),
            {"call_id": cid(1), "summary": SEEDED_SUMMARY},
        )


@contextmanager
def chain_lock_held(engine: Engine) -> Iterator[None]:
    """The audit chain's lock, held by the owner for the block: every append waits it out and
    fails."""
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
        yield
        holder.rollback()


def session_token(client: TestClient, origin: str | None = None) -> str:
    response = client.get(SESSION_ROUTE, headers={} if origin is None else {"Origin": origin})
    assert response.status_code == 200, response.status_code
    token = response.json()["csrf_token"]
    assert isinstance(token, str)
    return token


# R1
def test_every_route_needs_sign_in(database: StartupSettings, owner_engine: Engine) -> None:
    seed(owner_engine)
    clock = Clock()
    app = admin_with_clock(admin_settings(database), clock)
    with client_from(app) as client:
        idle = sign_in(client)
        # The session works: a read with it shows the data.
        assert SEEDED_SUMMARY in client.get("/admin/api/requests").text
        token = session_token(client)
        # Signed out well inside the idle limit, so only signing out can end it.
        clock.now += admin_sessions.IDLE_SECONDS - 10
        signed_out = sign_in(client)
        assert sign_out(client, session_token(client)).status_code == 204
        # The first session expires by the clock alone, 10 seconds on: no sign-in after it,
        # which would clear the table of it.
        clock.now += 10
        # The stream refuses HTTP Basic, even the admin's.
        refused = client.get(STREAM_ROUTE, headers=SIGNED_IN)
        assert refused.status_code == 401
        assert refused.json() == REFUSED_BODY
        assert "set-cookie" not in refused.headers
        written = events(owner_engine)

    cases: dict[str, str | None] = {
        "no cookie": None,
        "forged id": "A" * 43,
        "empty id": "",
        "expired": idle,
        "signed out": signed_out,
    }
    seen = 0
    for case, cookie in cases.items():
        for method, route in EVERY_ROUTE:
            for origin in (None, OWN_ORIGIN):
                seen += 1
                # One address per attempt, so the sign-in limit never answers instead.
                address = f"198.51.{100 + seen // 250}.{seen % 250 + 1}"
                anyone = TestClient(app, client=(address, 50000))
                if cookie is not None:
                    anyone.cookies.set(COOKIE, cookie, path="/admin/api")
                headers = {"X-CSRF-Token": token}
                if origin is not None:
                    headers["Origin"] = origin
                response = anyone.request(method, route, headers=headers)
                assert response.status_code == 401, (case, route, origin)
                assert response.json() == REFUSED_BODY, (case, route, origin)
                assert SEEDED_SUMMARY not in response.text, (case, route)
                # The Basic prompt only for a script on a route Basic opens: never for a
                # browser (it sends an Origin), never where Basic is refused.
                challenged = origin is None and (method, route) not in SESSION_ONLY
                assert response.headers.get("www-authenticate") == (
                    CHALLENGE if challenged else None
                ), (case, route, origin)
    assert seen == len(cases) * len(EVERY_ROUTE) * 2

    # Nothing refused was read, so nothing was audited.
    assert events(owner_engine) == written


def failed(response: Response) -> bool:
    return (
        response.status_code == 401
        and response.json() == FAILED_BODY
        and "set-cookie" not in response.headers
        and "www-authenticate" not in response.headers
    )


# R2
def test_sign_in_rules(
    database: StartupSettings, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # Structural: the session sign-in goes through the one check of the admin API, whose
    # comparison of both halves in constant time tests/test_admin_api.py pins; the session's
    # anti-forgery token is compared in constant time, and no credential or token anywhere in
    # the admin API is compared with == or !=.
    api_calls: dict[str, set[str]] = {}
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called.add(
                    func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                )
            if isinstance(node, ast.Compare) and any(
                isinstance(op, ast.Eq | ast.NotEq) for op in node.ops
            ):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                names |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                assert not names & {"password", "token", "supplied", "expected", "csrf"}, path
        api_calls[path.name] = called
    assert {"signs_in", "tokens_match"} <= api_calls["admin.py"]
    assert "compare_digest" in api_calls["security.py"]

    app = create_app(admin_settings(database))
    failures: dict[str, dict[str, Any]] = {
        "wrong user": {"json": {"user": TRIED_USER, "password": PASSWORD}},
        "wrong password": {"json": {"user": ADMIN_USER, "password": TRIED_PASSWORD}},
        "both wrong": {"json": {"user": TRIED_USER, "password": TRIED_PASSWORD}},
        "empty password": {"json": {"user": ADMIN_USER, "password": ""}},
        "no password": {"json": {"user": ADMIN_USER}},
        "a field more": {"json": {"user": ADMIN_USER, "password": PASSWORD, "remember": True}},
        "password a number": {"json": {"user": ADMIN_USER, "password": 7}},
        "user null": {"json": {"user": None, "password": PASSWORD}},
        "a list": {"json": [ADMIN_USER, PASSWORD]},
        "not JSON": {"content": b"user=" + ADMIN_USER.encode()},
        "not UTF-8": {"content": b'{"user": "' + ADMIN_USER.encode() + b'", "password": "\xff"}'},
        "user in another case": {"json": {"user": ADMIN_USER.upper(), "password": PASSWORD}},
    }
    bodies = set()
    for number, (case, sent) in enumerate(failures.items(), start=1):
        with client_from(app, f"198.51.100.{number}") as client:
            headers = {"Origin": OWN_ORIGIN, "Content-Type": "application/json"}
            response = client.post(SESSION_ROUTE, headers=headers, **sent)
            assert failed(response), case
            # Nothing that was typed comes back.
            assert TRIED_USER not in response.text and TRIED_PASSWORD not in response.text
            bodies.add(response.content)
    # The same answer, byte for byte, for every failure.
    assert len(bodies) == 1

    # A body over 1 KiB is not read as a sign-in.
    with client_from(app, "198.51.100.60") as client:
        padded = {"user": ADMIN_USER, "password": PASSWORD, "pad": "x" * 1000}
        response = client.post(SESSION_ROUTE, json=padded, headers={"Origin": OWN_ORIGIN})
        assert response.status_code == 413
        assert "set-cookie" not in response.headers

    # The limit is the API's own, per address, shared both ways with HTTP Basic.
    with client_from(app, "192.0.2.10") as client:
        for _ in range(5):
            assert failed(try_sign_in(client, password=TRIED_PASSWORD))
        limited = try_sign_in(client)
        assert limited.status_code == 429
        assert limited.json() == LIMITED_BODY
        assert "set-cookie" not in limited.headers
        assert client.get("/admin/api/requests", headers=SIGNED_IN).status_code == 429
    with client_from(app, "192.0.2.11") as client:
        for _ in range(5):
            wrong = basic(ADMIN_USER, TRIED_PASSWORD)
            assert client.get("/admin/api/requests", headers=wrong).status_code == 401
        assert try_sign_in(client).status_code == 429
    with client_from(app, "192.0.2.20") as client:
        sign_in(client)
    messages = [r.getMessage() for r in caplog.records if "admin_sign_in" in r.getMessage()]
    assert messages[-13:] == [
        *[f"admin_sign_in_failed failures={count}" for count in range(1, 6)],
        "admin_sign_in_limited",
        "admin_sign_in_limited",
        *[f"admin_sign_in_failed failures={count}" for count in range(1, 6)],
        "admin_sign_in_limited",
    ]
    for secret in (TRIED_USER, TRIED_PASSWORD, PASSWORD):
        assert secret not in caplog.text
    # Failed sign-ins are not audited; the one that worked is.
    assert admin_events(owner_engine) == [("admin_signed_in", "session", None, "success")]

    # The cookie: the id and nothing else, with exactly these flags. Host-only: no Domain. A
    # session cookie: no Max-Age. Outside dev, over HTTPS, a __Host- cookie, which a sibling
    # host cannot plant or overwrite: Secure and Path=/. In dev, over plain http, Path=/admin/api.
    for app_env, name, flags, scheme in (
        ("dev", COOKIE, "HttpOnly; Path=/admin/api; SameSite=strict", "http"),
        ("demo", HOST_COOKIE, "HttpOnly; Path=/; SameSite=strict; Secure", "https"),
    ):
        settings = admin_settings(
            database, app_env=app_env, app_role="admin", identity_hash_key=None
        )
        role_app = admin_with_clock(settings, Clock())
        origin = f"{scheme}://testserver"
        with client_from(role_app, base_url=origin) as client:
            response = try_sign_in(client, origin=origin)
            assert response.status_code == 200, app_env
            session_id = client.cookies[name]
            assert len(session_id) == 43
            assert response.headers.get_list("set-cookie") == [f"{name}={session_id}; {flags}"]
            # Signing out clears the same cookie.
            token = session_token(client)
            cleared = sign_out(client, token, origin=origin)
            assert cleared.status_code == 204, app_env
            assert cleared.headers["set-cookie"].startswith(f'{name}=""; '), app_env
            assert f"; {flags}" in cleared.headers["set-cookie"].replace("Max-Age=0; ", ""), app_env

    # What a signed-in browser is told, on signing in and when it asks: who, the token, the
    # seconds left, and the office as the agent's clock has it (Tuesday 10:40 at the clinic).
    clock = Clock()
    timed = admin_with_clock(admin_settings(database, simulated_now="2026-11-24T15:40:00Z"), clock)
    with client_from(timed, "192.0.2.30") as client:
        shown = try_sign_in(client).json()
        token = shown["csrf_token"]
        assert shown == {
            "user": ADMIN_USER,
            "csrf_token": token,
            "expires_in": 30 * 60,
            "office_open": True,
            "time_zone": "America/New_York",
        }
        assert len(token) == 43 and token != client.cookies[COOKIE]
        clock.now += 60
        # Asking does not keep the session alive; a read does.
        assert client.get(SESSION_ROUTE).json()["expires_in"] == 29 * 60
        assert client.get(SESSION_ROUTE).json()["expires_in"] == 29 * 60
        assert client.get("/admin/api/requests").status_code == 200
        assert client.get(SESSION_ROUTE).json() == {**shown, "expires_in": 30 * 60}

    # Sessions expire: 30 minutes idle, 8 hours in all, by the injected clock.
    clock = Clock()
    table = SessionTable(clock=clock)
    session_id = table.open(ADMIN_USER)[0]
    clock.now += 30 * 60 - 1
    assert table.find(session_id) is not None
    clock.now += 30 * 60 - 1
    assert table.find(session_id) is not None
    clock.now += 30 * 60
    assert table.find(session_id) is None
    session_id = table.open(ADMIN_USER)[0]
    for _ in range(8 * 3 - 1):
        clock.now += 20 * 60
        assert table.find(session_id) is not None
    clock.now += 20 * 60
    assert table.find(session_id) is None
    assert table.find(None) is None and table.find("") is None
    # Looking without using leaves the idle time as it was.
    session_id = table.open(ADMIN_USER)[0]
    clock.now += 30 * 60 - 1
    assert table.find(session_id, touch=False) is not None
    clock.now += 1
    assert table.find(session_id, touch=False) is None

    # The table is bounded: the session used longest ago makes room.
    table = SessionTable(max_sessions=3, clock=clock)
    ids = []
    for _ in range(3):
        ids.append(table.open(ADMIN_USER)[0])
        clock.now += 1
    assert table.find(ids[0]) is not None
    ids.append(table.open(ADMIN_USER)[0])
    assert len(table) == 3
    assert table.find(ids[1]) is None
    assert [table.find(found) is not None for found in (ids[0], ids[2], ids[3])] == [True] * 3
    # Each id is new, 32 random bytes, and the table does not hold it as given.
    assert len(set(ids)) == 4 and all(len(found) == 43 for found in ids)
    assert not any(found in repr(vars(table)) for found in ids)

    # No event, no session: with the chain's lock held, sign-in fails closed.
    before = events(owner_engine)
    with client_from(app, "192.0.2.21") as client:
        with chain_lock_held(owner_engine):
            response = try_sign_in(client)
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
        assert client.get(SESSION_ROUTE).status_code == 401
    assert events(owner_engine) == before


# R3
def test_forged_requests_are_refused(database: StartupSettings, owner_engine: Engine) -> None:
    app = create_app(admin_settings(database))
    with client_from(app, "192.0.2.50") as other, client_from(app, "192.0.2.51") as client:
        sign_in(other)
        other_token = session_token(other)
        sign_in(client)
        session_id = client.cookies[COOKIE]
        token = session_token(client)
        before = events(owner_engine)

        own = {"Origin": OWN_ORIGIN}
        for case, headers in {
            "no token": own,
            "empty token": {**own, "X-CSRF-Token": ""},
            "wrong token": {**own, "X-CSRF-Token": "B" * 43},
            "another session's token": {**own, "X-CSRF-Token": other_token},
            "token with a character more": {**own, "X-CSRF-Token": token + "A"},
            "no Origin": {"X-CSRF-Token": token},
        }.items():
            response = client.delete(SESSION_ROUTE, headers=headers)
            assert response.status_code == 403, case
            assert response.json() == FORBIDDEN_BODY, case
            assert "set-cookie" not in response.headers, case
        # From any other origin the cookie is not honoured at all, the right token with it.
        for case, origin in {
            "another origin": "http://evil.test",
            "a sibling's origin": "http://other.testserver",
            "opaque origin": "null",
            "another port": "http://testserver:8080",
            "another scheme": "https://testserver",
        }.items():
            response = client.delete(
                SESSION_ROUTE, headers={"X-CSRF-Token": token, "Origin": origin}
            )
            assert response.status_code == 401, case
            assert response.json() == REFUSED_BODY, case
            assert "set-cookie" not in response.headers, case
        # The admin's Basic sign-in alone changes nothing, with or without a token.
        client.cookies.clear()
        for headers in (SIGNED_IN, {**SIGNED_IN, **own, "X-CSRF-Token": token}):
            response = client.delete(SESSION_ROUTE, headers=headers)
            assert response.status_code == 401
            assert "www-authenticate" not in response.headers
        client.cookies.set(COOKIE, session_id, path="/admin/api")
        # Nothing else changes state, and the session is still the admin's.
        for method in ("PUT", "PATCH"):
            assert client.request(method, SESSION_ROUTE, headers=own).status_code == 405
        assert client.get(SESSION_ROUTE).status_code == 200
        assert events(owner_engine) == before

        # Signing out, as the console does it, ends the session and clears the cookie.
        response = sign_out(client, token)
        assert response.status_code == 204
        assert response.headers["set-cookie"].startswith(f'{COOKIE}=""; ')
        assert "Path=/admin/api" in response.headers["set-cookie"]
        client.cookies.set(COOKIE, session_id, path="/admin/api")
        assert client.get(SESSION_ROUTE).status_code == 401
        assert client.get("/admin/api/requests").status_code == 401
        assert sign_out(client, token).status_code == 401
        # The other session is its own.
        assert other.get(SESSION_ROUTE).status_code == 200
    assert [row[0] for row in admin_events(owner_engine)[len(before) :]] == ["admin_signed_out"]
    assert admin_events(owner_engine)[-1] == ("admin_signed_out", "session", None, "success")

    # Signing in has no token yet: it needs the Origin rule and a JSON body. A forged sign-in
    # costs the address nothing.
    with client_from(app, "192.0.2.52") as client:
        credentials = {"user": ADMIN_USER, "password": PASSWORD}
        forged: dict[str, dict[str, Any]] = {
            "no Origin": {"json": credentials},
            "another origin": {"json": credentials, "headers": {"Origin": "http://evil.test"}},
            "a form": {"data": credentials, "headers": own},
            "plain text": {
                "content": b'{"user": "x", "password": "y"}',
                "headers": {**own, "Content-Type": "text/plain"},
            },
            "no content type": {"content": b"{}", "headers": own},
        }
        for case, sent in forged.items():
            for _ in range(2):
                response = client.post(SESSION_ROUTE, **sent)
                assert response.status_code == 403, case
                assert response.json() == FORBIDDEN_BODY, case
                assert "set-cookie" not in response.headers, case
        assert try_sign_in(client).status_code == 200

    # With CONSOLE_ORIGIN set, that origin alone may sign in and change state: from the API's
    # own, a sign-in is refused and the session is not honoured.
    clock = Clock()
    console = admin_with_clock(admin_settings(database, console_origin=CONSOLE_ORIGIN), clock)
    with client_from(console, "192.0.2.53") as client:
        assert try_sign_in(client).status_code == 403
        sign_in(client, origin=CONSOLE_ORIGIN)
        token = session_token(client, CONSOLE_ORIGIN)
        assert sign_out(client, token).status_code == 401
        from_console = {"Origin": CONSOLE_ORIGIN}
        assert client.get(SESSION_ROUTE, headers=from_console).status_code == 200
        assert sign_out(client, token, origin=CONSOLE_ORIGIN).status_code == 204
        assert client.get(SESSION_ROUTE, headers=from_console).status_code == 401


def test_a_session_is_honoured_only_from_its_origin(
    database: StartupSettings, owner_engine: Engine
) -> None:
    # A page of another origin on the same site sends the SameSite cookie with its requests,
    # and a page that loads an image or a link sends it with no Origin at all: with
    # CONSOLE_ORIGIN set the session answers neither, so they can neither read, nor check the
    # chain, nor take the session's stream, nor keep it alive.
    clock = Clock()
    app = admin_with_clock(admin_settings(database, console_origin=CONSOLE_ORIGIN), clock)
    with client_from(app, "192.0.2.60") as client:
        sign_in(client, origin=CONSOLE_ORIGIN)
        from_console = {"Origin": CONSOLE_ORIGIN}
        assert client.get("/admin/api/requests", headers=from_console).status_code == 200
        assert client.get(SESSION_ROUTE, headers=from_console).status_code == 200
        before = events(owner_engine)
        clock.now += 60
        for origin in (None, "http://other.console.test", OWN_ORIGIN, "null"):
            headers = {} if origin is None else {"Origin": origin}
            for route in (
                "/admin/api/requests",
                "/admin/api/audit/verify",
                SESSION_ROUTE,
                STREAM_ROUTE,
            ):
                response = client.get(route, headers=headers)
                assert response.status_code == 401, (origin, route)
                assert response.json() == REFUSED_BODY, (origin, route)
        assert events(owner_engine) == before
        # None of them kept the session alive: a minute of its idle time is gone.
        shown = client.get(SESSION_ROUTE, headers=from_console).json()
        assert shown["expires_in"] == admin_sessions.IDLE_SECONDS - 60

    # Without CONSOLE_ORIGIN the console is the API's own origin, whose same-origin reads
    # carry no Origin: those are honoured.
    plain = admin_with_clock(admin_settings(database), Clock())
    with client_from(plain, "192.0.2.62") as client:
        sign_in(client)
        assert client.get("/admin/api/requests").status_code == 200
        assert client.get(SESSION_ROUTE).status_code == 200


def test_bare_requests_do_not_lock_sign_in(database: StartupSettings) -> None:
    # Only credentials presented and wrong count against the address: a page that makes the
    # admin's browser send requests without any cannot lock the admin out.
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app, "192.0.2.63") as client:
        for _ in range(20):
            response = client.get("/admin/api/requests")
            assert response.status_code == 401
            assert response.json() == REFUSED_BODY
        assert try_sign_in(client).status_code == 200
    with client_from(app, "192.0.2.64") as client:
        wrong = basic(ADMIN_USER, TRIED_PASSWORD)
        for _ in range(5):
            assert client.get("/admin/api/requests", headers=wrong).status_code == 401
        assert client.get("/admin/api/requests", headers=wrong).status_code == 429
        assert try_sign_in(client).status_code == 429


def test_sign_out_always_signs_out(
    database: StartupSettings, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app, "192.0.2.65") as client:
        # The audit log can take the event: it is written.
        sign_in(client)
        response = sign_out(client, session_token(client))
        assert response.status_code == 204
        assert admin_events(owner_engine)[-1] == ("admin_signed_out", "session", None, "success")

        # It cannot: the session ends all the same, the cookie is cleared, and the log says the
        # event is missing, by name only.
        session_id = sign_in(client)
        token = session_token(client)
        before = events(owner_engine)
        with chain_lock_held(owner_engine):
            response = sign_out(client, token)
        assert response.status_code == 204
        assert response.headers["set-cookie"].startswith(f'{COOKIE}=""; ')
        assert events(owner_engine) == before
        client.cookies.clear()
        client.cookies.set(COOKIE, session_id, path="/admin/api")
        assert client.get(SESSION_ROUTE).status_code == 401
    messages = [record.getMessage() for record in caplog.records]
    assert [m for m in messages if m.startswith("admin_sign_out_unaudited error=")], messages
    for message in messages:
        assert session_id not in message and token not in message


def test_sign_in_replaces_the_browsers_session(database: StartupSettings) -> None:
    app = admin_with_clock(admin_settings(database), Clock())
    with client_from(app, "192.0.2.61") as client:
        first = sign_in(client)
        # Signing in again, with the cookie of the first session: the first one ends.
        assert try_sign_in(client).status_code == 200
        second = client.cookies[COOKIE]
        assert second != first
        for session_id, status in ((first, 401), (second, 200)):
            client.cookies.clear()
            client.cookies.set(COOKIE, session_id, path="/admin/api")
            assert client.get(SESSION_ROUTE).status_code == status
