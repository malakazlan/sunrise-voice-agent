import ast
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY
from console_client import (
    ADMIN_USER,
    PASSWORD,
    admin_events,
    cid,
    client_from,
    console_settings,
    events,
    form_token,
    sign_in,
    try_sign_in,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, text

from app.audit.log import CHAIN_LOCK, AuditLog
from app.config import Settings, StartupSettings
from app.console import sessions as console_sessions
from app.console.service import mount_admin
from app.console.sessions import SessionTable
from app.db.session import create_database_engine, create_session_factory
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_DIR = REPO_ROOT / "app" / "console"
ADMIN_MODULE = REPO_ROOT / "app" / "api" / "admin.py"

TRIED_USER = "intruder_name"
TRIED_PASSWORD = "-".join(["tried", "pass", "not", "for", "logs"])
FAILED = "The user name or password is not right."
LIMITED = "Too many failed sign-ins."
SEEDED_SUMMARY = "Refill seeded for the session test"

# Every console address that is not the sign-in page or a static file.
GET_PAGES = [
    "/admin/calls",
    "/admin/calls?ended=completed",
    f"/admin/calls/{cid(1)}",
    "/admin/requests",
    "/admin/requests?type=refill",
    "/admin/audit",
    f"/admin/audit?call_id={cid(1)}",
    "/admin/nothing-here",
]
POSTS = ["/admin/audit/check", "/admin/logout"]
# The console's bare address only points on, to a page that checks.
POINTERS = ["/admin", "/admin/"]


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def console_with_clock(settings: Settings, clock: Clock) -> FastAPI:
    """The admin API and the console mounted as create_app mounts them, with the sessions'
    clock in the test's hands."""
    app = FastAPI()
    engine = create_database_engine(settings)
    mount_admin(
        app,
        settings,
        create_session_factory(engine),
        AuditLog(SecretStr(TEST_AUDIT_KEY)),
        clock=clock,
    )
    return app


def console_only(database: StartupSettings, **overrides: Any) -> Settings:
    """Settings of the console role: no identity key, no vendor key."""
    return console_settings(database, app_role="console", identity_hash_key=None, **overrides)


@contextmanager
def chain_lock_held(engine: Engine) -> Iterator[None]:
    """The audit chain's lock, held by the owner for the block: every append waits it out and
    fails."""
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK})
        yield
        holder.rollback()


def seed(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO requests (id, call_id, type, summary) VALUES (1, :call_id, 'refill',"
                " :summary)"
            ),
            {"call_id": cid(1), "summary": SEEDED_SUMMARY},
        )


def fresh_sign_in(client: TestClient) -> str:
    """Sign in from a browser with no cookie; the new session's id."""
    client.cookies.clear()
    sign_in(client)
    return client.cookies["console_session"]


# R1
def test_every_page_needs_a_session(database: StartupSettings, owner_engine: Engine) -> None:
    seed(owner_engine)
    clock = Clock()
    app = console_with_clock(console_settings(database), clock)
    with client_from(app) as client:
        signed_out = fresh_sign_in(client)
        # The session works: the same page, signed in, shows the data.
        page = client.get("/admin/requests")
        assert SEEDED_SUMMARY in page.text
        token = form_token(page)
        assert client.post("/admin/logout", data={"token": token}).status_code == 303
        idle = fresh_sign_in(client)
        # Expired by the clock alone: no sign-in after it, which would clear the table of it.
        clock.now += console_sessions.IDLE_SECONDS
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
            for route in POINTERS + GET_PAGES + POSTS:
                for htmx in (False, True):
                    client.cookies.clear()
                    if cookie is not None:
                        client.cookies.set("console_session", cookie, path="/admin")
                    headers = {"HX-Request": "true"} if htmx else {}
                    if route in POSTS:
                        response = client.post(route, data={"token": token}, headers=headers)
                    else:
                        response = client.get(route, headers=headers)
                    assert SEEDED_SUMMARY not in response.text, (case, route)
                    assert cid(1) not in response.text, (case, route)
                    if route in POINTERS:
                        assert response.status_code == 303, (case, route)
                        assert response.headers["location"] == "/admin/calls", (case, route)
                    elif htmx:
                        assert response.status_code == 401, (case, route)
                        assert response.headers["hx-redirect"] == "/admin/login", (case, route)
                    else:
                        assert response.status_code == 303, (case, route)
                        assert response.headers["location"] == "/admin/login", (case, route)
                    seen += 1
        assert seen == len(cases) * (len(POINTERS) + len(GET_PAGES) + len(POSTS)) * 2

        # Static files carry no data and are served to anyone: the sign-in page needs them.
        client.cookies.clear()
        for path in ("console.css", "htmx-2.0.11.min.js", "fonts/inter-latin-400-normal.woff2"):
            assert client.get(f"/admin/static/{path}").status_code == 200, path
        assert client.get("/admin/login").status_code == 200

    # Nothing refused was read, so nothing was audited.
    assert events(owner_engine) == written


# R2
def test_sign_in_rules(
    database: StartupSettings, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # Structural: the console signs in only through the admin API's check, whose comparison of
    # both halves in constant time tests/test_admin_api.py pins; the console itself compares
    # no credential or token but in constant time.
    admin_tree = ast.parse(ADMIN_MODULE.read_text(encoding="utf-8"))
    signs_in = next(
        node
        for node in ast.walk(admin_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "signs_in"
    )
    assert "_credentials_match" in {
        node.func.id
        for node in ast.walk(signs_in)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    console_calls: set[str] = set()
    for path in CONSOLE_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                console_calls.add(node.func.attr)
            if isinstance(node, ast.Compare):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert not names & {"password", "user", "token", "supplied", "expected"}, path
    assert {"signs_in", "compare_digest"} <= console_calls

    app = create_app(console_settings(database))
    failures = {
        "wrong user": {"user": TRIED_USER, "password": PASSWORD},
        "wrong password": {"user": ADMIN_USER, "password": TRIED_PASSWORD},
        "both wrong": {"user": TRIED_USER, "password": TRIED_PASSWORD},
        "empty password": {"user": ADMIN_USER, "password": ""},
        "no password": {"user": ADMIN_USER},
        "user in another case": {"user": ADMIN_USER.upper(), "password": PASSWORD},
    }
    for number, (case, fields) in enumerate(failures.items(), start=1):
        with client_from(app, f"198.51.100.{number}") as client:
            token = form_token(client.get("/admin/login"))
            response = client.post("/admin/login", data={**fields, "token": token})
            assert response.status_code == 401, case
            assert FAILED in response.text, case
            assert LIMITED not in response.text, case
            assert "console_session" not in response.headers.get("set-cookie", ""), case
            # Nothing the admin typed comes back.
            assert TRIED_USER not in response.text and TRIED_PASSWORD not in response.text
    # A repeated field and text that is not UTF-8 fail the same way. Each answer is a new form,
    # with a new token.
    with client_from(app, "198.51.100.40") as client:
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        token = form_token(client.get("/admin/login"))
        repeated = client.post(
            "/admin/login",
            content=f"user={ADMIN_USER}&user={ADMIN_USER}&password={PASSWORD}&token={token}",
            headers=form,
        )
        token = form_token(repeated)
        broken = client.post(
            "/admin/login", content=f"user={ADMIN_USER}&password=%ff%fe&token={token}", headers=form
        )
    for response in (repeated, broken):
        assert response.status_code == 401
        assert FAILED in response.text

    # The limit: the API's own, per address, and shared with the API.
    with client_from(app, "192.0.2.10") as client:
        for _ in range(5):
            assert try_sign_in(client, password=TRIED_PASSWORD).status_code == 401
        limited = try_sign_in(client)
        assert limited.status_code == 429
        assert LIMITED in limited.text
        assert "console_session" not in limited.headers.get("set-cookie", "")
        assert client.get("/admin/api/requests", auth=(ADMIN_USER, PASSWORD)).status_code == 429
    with client_from(app, "192.0.2.20") as client:
        sign_in(client)
    messages = [r.getMessage() for r in caplog.records if "admin_sign_in" in r.getMessage()]
    assert messages[-7:] == [
        "admin_sign_in_failed failures=1",
        "admin_sign_in_failed failures=2",
        "admin_sign_in_failed failures=3",
        "admin_sign_in_failed failures=4",
        "admin_sign_in_failed failures=5",
        "admin_sign_in_limited",
        "admin_sign_in_limited",
    ]
    for secret in (TRIED_USER, TRIED_PASSWORD, PASSWORD):
        assert secret not in caplog.text
    # Failed sign-ins are not audited; the one that worked is.
    assert admin_events(owner_engine) == [("admin_signed_in", "console", None, "success")]

    # The cookies: an id and nothing else, with exactly these flags; Secure outside dev, where
    # the console is reached over HTTPS.
    for app_env, secure, scheme in (("dev", "", "http"), ("demo", "; Secure", "https")):
        demo_app = console_with_clock(console_only(database, app_env=app_env), Clock())
        with client_from(demo_app, base_url=f"{scheme}://testserver") as c:
            login = c.get("/admin/login")
            assert login.headers.get_list("set-cookie") == [
                f"console_login={c.cookies['console_login']}; HttpOnly; Path=/admin/login;"
                f" SameSite=strict{secure}"
            ]
            response = try_sign_in(c)
            session_id = c.cookies["console_session"]
            assert len(session_id) == 43
            assert (
                f"console_session={session_id}; HttpOnly; Path=/admin; SameSite=strict{secure}"
                in response.headers.get_list("set-cookie")
            )

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


def post_with(
    client: TestClient, route: str, token: str | None, headers: dict[str, str] | None = None
) -> Response:
    data = {} if token is None else {"token": token}
    if route == "/admin/login":
        data |= {"user": ADMIN_USER, "password": PASSWORD}
    return client.post(route, data=data, headers=headers or {})


# R3
def test_forged_requests_are_refused(
    database: StartupSettings, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifications: list[int] = []
    original = AuditLog.verify

    async def counted(self: AuditLog, session: Any) -> Any:
        verifications.append(1)
        return await original(self, session)

    monkeypatch.setattr(AuditLog, "verify", counted)
    app = create_app(console_settings(database))
    with client_from(app, "192.0.2.50") as other, client_from(app, "192.0.2.51") as client:
        sign_in(other)
        other_token = form_token(other.get("/admin/requests"))
        sign_in(client)
        token = form_token(client.get("/admin/requests"))
        before = events(owner_engine)

        for route in POSTS:
            for case, supplied in {
                "no token": None,
                "empty token": "",
                "wrong token": "B" * 43,
                "another session's token": other_token,
                "token with a character more": token + "A",
            }.items():
                response = post_with(client, route, supplied)
                assert response.status_code == 403, (route, case)
                assert "set-cookie" not in response.headers, (route, case)
            # From another site, with the right token: refused all the same.
            for site in ("cross-site", "same-site"):
                response = post_with(client, route, token, {"Sec-Fetch-Site": site})
                assert response.status_code == 403, (route, site)
            # GET changes nothing.
            assert client.get(route).status_code == 405, route
        # The session is still the admin's, and nothing ran or was written but this read.
        assert client.get("/admin/requests").status_code == 200
        assert verifications == []
        assert [row[3] for row in events(owner_engine)[len(before) :]] == ["admin_read_queue"]

        # A cross-site form post carries no cookie (SameSite=Strict): with the token copied
        # from the page, it still cannot act.
        client.cookies.clear()
        for route in POSTS:
            response = post_with(client, route, token)
            assert response.status_code == 303, route
            assert response.headers["location"] == "/admin/login", route
        assert verifications == []

    # Sign-in: the form's token must come back with the cookie it was set with.
    with client_from(app, "192.0.2.52") as client:
        login_token = form_token(client.get("/admin/login"))
        cookie = client.cookies["console_login"]
        cases: dict[str, tuple[str | None, str | None, dict[str, str]]] = {
            "no token": (None, cookie, {}),
            "wrong token": ("C" * 43, cookie, {}),
            "no cookie (another site)": (login_token, None, {}),
            "cookie of another form": (login_token, "D" * 43, {}),
            "cross-site": (login_token, cookie, {"Sec-Fetch-Site": "cross-site"}),
        }
        for case, (supplied, sent_cookie, headers) in cases.items():
            client.cookies.clear()
            if sent_cookie is not None:
                client.cookies.set("console_login", sent_cookie, path="/admin/login")
            response = post_with(client, "/admin/login", supplied, headers)
            assert response.status_code == 403, case
            assert "console_session" not in response.headers.get("set-cookie", ""), case
        # The same form, as it was sent, works: a forged sign-in cost the address nothing.
        client.cookies.clear()
        client.cookies.set("console_login", cookie, path="/admin/login")
        assert post_with(client, "/admin/login", login_token).status_code == 303
    assert [row for row in admin_events(owner_engine) if row[0] == "admin_signed_in"] == [
        ("admin_signed_in", "console", None, "success")
    ] * 3


def test_sign_out_ends_the_session(database: StartupSettings, owner_engine: Engine) -> None:
    app = create_app(console_settings(database))
    with client_from(app) as client:
        sign_in(client)
        session_id = client.cookies["console_session"]
        token = form_token(client.get("/admin/calls"))
        response = client.post("/admin/logout", data={"token": token})
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login?signed_out=1"
        assert 'console_session=""' in response.headers["set-cookie"]
        assert "You are signed out." in client.get("/admin/login?signed_out=1").text
        client.cookies.set("console_session", session_id, path="/admin")
        assert client.get("/admin/calls").status_code == 303
    assert [row[0] for row in admin_events(owner_engine)] == [
        "admin_signed_in",
        "admin_read_calls",
        "admin_signed_out",
    ]


def test_signed_in_admin_skips_the_form(database: StartupSettings) -> None:
    app = create_app(console_settings(database))
    with client_from(app) as client:
        sign_in(client)
        response = client.get("/admin/login")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/calls"


def test_sign_in_needs_its_audit_event(database: StartupSettings, owner_engine: Engine) -> None:
    # No event, no session: with the chain's lock held, sign-in fails closed.
    app = create_app(console_settings(database))
    with client_from(app) as client:
        token = form_token(client.get("/admin/login"))
        with chain_lock_held(owner_engine):
            response = client.post(
                "/admin/login", data={"user": ADMIN_USER, "password": PASSWORD, "token": token}
            )
        assert response.status_code == 503
        assert "console_session" not in response.headers.get("set-cookie", "")
        assert client.get("/admin/calls").status_code == 303
    assert events(owner_engine) == []
