"""What the admin API's session, CORS and stream tests share: the settings, a client, a clock
the test moves, signing in from a browser, and the audit events the database holds."""

import asyncio
import base64
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.service import mount_admin
from app.audit.log import Action, AuditLog, Event, Outcome, ResourceType
from app.config import Settings, StartupSettings
from app.db.session import create_database_engine, create_session_factory

ADMIN_USER = "clinic_admin"
# Built from short pieces, like every fake secret in the tests.
PASSWORD = "-".join(["admin", "pass", "not", "for", "logs"])
ACTOR = f"admin:{ADMIN_USER}"
COOKIE = "admin_session"
SESSION_ROUTE = "/admin/api/session"
STREAM_ROUTE = "/admin/api/calls/live/stream"
# The API's own origin, as the test client reaches it.
OWN_ORIGIN = "http://testserver"

AUDIT = AuditLog(SecretStr(TEST_AUDIT_KEY))

EVENTS = text(
    "SELECT seq, call_id, actor, action, resource_type, resource_id, outcome, reason"
    " FROM audit_events ORDER BY seq"
)


def cid(number: int) -> str:
    """A call id in the format calls are given (uuid4 hex), distinct per number."""
    return f"{number:012x}4{number:03x}8{number:015x}"


def basic(user: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


SIGNED_IN = basic(ADMIN_USER, PASSWORD)


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


class Clock:
    """A monotonic clock the test moves by hand, and a sleep that moves it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def admin_with_clock(
    settings: Settings,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    """The admin API mounted as the roles mount it, with its clock and its sleep in the
    test's hands."""
    app = FastAPI()
    if engine is None:
        engine = create_database_engine(settings)
    mount_admin(
        app,
        settings,
        create_session_factory(engine),
        AUDIT,
        clock=clock,
        sleep=clock.sleep if sleep is None else sleep,
    )
    return app


@contextmanager
def client_from(
    app: FastAPI, address: str = "203.0.113.5", base_url: str = "http://testserver"
) -> Iterator[TestClient]:
    """A client at one address, on a selector loop (psycopg's async mode needs one). An https
    base URL sends the cookies marked Secure."""
    with TestClient(
        app,
        client=(address, 50000),
        base_url=base_url,
        follow_redirects=False,
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as client:
        yield client


def try_sign_in(
    client: TestClient,
    user: str = ADMIN_USER,
    password: str = PASSWORD,
    origin: str = OWN_ORIGIN,
) -> Response:
    """The console's sign-in, as a browser sends it."""
    return client.post(
        SESSION_ROUTE, json={"user": user, "password": password}, headers={"Origin": origin}
    )


def sign_in(client: TestClient, origin: str = OWN_ORIGIN) -> str:
    """Sign in from a browser with no cookie; the new session's id."""
    client.cookies.clear()
    response = try_sign_in(client, origin=origin)
    assert response.status_code == 200, response.status_code
    return client.cookies[COOKIE]


def sign_out(client: TestClient, token: str, origin: str = OWN_ORIGIN) -> Response:
    return client.delete(SESSION_ROUTE, headers={"X-CSRF-Token": token, "Origin": origin})


def call_started(number: int) -> Event:
    return Event(
        actor="agent",
        action=Action.CALL_STARTED,
        resource_type=ResourceType.CALL,
        outcome=Outcome.SUCCESS,
        call_id=cid(number),
        resource_id=cid(number),
    )


def append_events(database: StartupSettings, items: list[Event], at: datetime) -> None:
    """Append events as the runtime role, through the audit log, a second apart from `at`."""

    async def run() -> None:
        engine = create_database_engine(database)
        try:
            async with create_session_factory(engine)() as session:
                for offset, item in enumerate(items):
                    await AUDIT.append(session, item, now=at + timedelta(seconds=offset))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)


def events(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS)]


def admin_events(engine: Engine) -> list[tuple[Any, ...]]:
    """The events an admin wrote: action, resource type, resource id and outcome."""
    return [(row[3], row[4], row[5], row[6]) for row in events(engine) if row[2] == ACTOR]
