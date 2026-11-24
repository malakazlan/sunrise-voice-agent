"""What the console's tests share: the settings, a client, signing in, and the audit events the
database holds."""

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app.config import Settings, StartupSettings

ADMIN_USER = "clinic_admin"
# Built from short pieces, like every fake secret in the tests.
PASSWORD = "-".join(["console", "pass", "not", "for", "logs"])
ACTOR = f"admin:{ADMIN_USER}"
TOKEN_FIELD = re.compile(r'<input type="hidden" name="token" value="([^"]+)">')

EVENTS = text(
    "SELECT seq, call_id, actor, action, resource_type, resource_id, outcome, reason"
    " FROM audit_events ORDER BY seq"
)


def cid(number: int) -> str:
    """A call id in the format calls are given (uuid4 hex), distinct per number."""
    return f"{number:012x}4{number:03x}8{number:015x}"


def console_settings(database: StartupSettings, **overrides: Any) -> Settings:
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
def client_from(
    app: FastAPI, address: str = "203.0.113.5", base_url: str = "http://testserver"
) -> Iterator[TestClient]:
    """A client at one address, on a selector loop (psycopg's async mode needs one), that
    does not follow redirects, so each answer is seen as it is. An https base URL sends the
    cookies marked Secure."""
    with TestClient(
        app,
        client=(address, 50000),
        base_url=base_url,
        follow_redirects=False,
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as client:
        yield client


def form_token(response: Response) -> str:
    """The anti-forgery token of the page's first form."""
    match = TOKEN_FIELD.search(response.text)
    assert match is not None, response.status_code
    return match[1]


def try_sign_in(client: TestClient, user: str = ADMIN_USER, password: str = PASSWORD) -> Response:
    """The sign-in form, filled in and sent with its token, as a browser would."""
    token = form_token(client.get("/admin/login"))
    return client.post("/admin/login", data={"user": user, "password": password, "token": token})


def sign_in(client: TestClient) -> None:
    response = try_sign_in(client)
    assert response.status_code == 303, response.status_code
    assert response.headers["location"] == "/admin/calls"


def events(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(EVENTS)]


def admin_events(engine: Engine) -> list[tuple[Any, ...]]:
    """The events an admin wrote: action, resource type, resource id and outcome."""
    return [(row[3], row[4], row[5], row[6]) for row in events(engine) if row[2] == ACTOR]
