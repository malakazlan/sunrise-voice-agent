"""Requests from another origin: the admin API answers CONSOLE_ORIGIN, and no other."""

from typing import Any

import pytest
from admin_client import (
    OWN_ORIGIN,
    SESSION_ROUTE,
    SIGNED_IN,
    STREAM_ROUTE,
    Clock,
    admin_settings,
    admin_with_clock,
    client_from,
)
from conftest import TEST_AUDIT_KEY
from httpx2 import Response
from pydantic import ValidationError

from app.config import Settings, StartupSettings

CONSOLE_ORIGIN = "https://console.example.test"
ADMIN_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}
OTHER_ORIGINS = [
    "https://evil.example.test",
    # A sibling on the same site, and the console's origin changed in any one part.
    "https://other.example.test",
    "http://console.example.test",
    "https://console.example.test:8443",
    "https://console.example.test.evil.test",
    "https://CONSOLE.example.test",
    "https://console.example.test/",
    OWN_ORIGIN,
    "null",
    "*",
]


def cors_headers(response: Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower().startswith("access-control-")
    }


def preflight(client: Any, origin: str, method: str = "DELETE", headers: str = "") -> Response:
    sent = {"Origin": origin, "Access-Control-Request-Method": method}
    if headers:
        sent["Access-Control-Request-Headers"] = headers
    response: Response = client.options(SESSION_ROUTE, headers=sent)
    return response


def settings_errors(**values: Any) -> list[tuple[str, ...]]:
    try:
        Settings(_env_file=None, **values)
    except ValidationError as refused:
        return [tuple(str(part) for part in error["loc"]) for error in refused.errors()]
    return []


# R4
def test_only_the_console_origin_is_answered(database: StartupSettings) -> None:
    app = admin_with_clock(admin_settings(database, console_origin=CONSOLE_ORIGIN), Clock())
    with client_from(app) as client:
        # A read, and a sign-in, from the console: its origin alone, with credentials.
        for response in (
            client.get("/admin/api/requests", headers={**SIGNED_IN, "Origin": CONSOLE_ORIGIN}),
            client.get(SESSION_ROUTE, headers={"Origin": CONSOLE_ORIGIN}),
            client.post(
                SESSION_ROUTE,
                json={"user": "nobody", "password": "x"},
                headers={"Origin": CONSOLE_ORIGIN},
            ),
        ):
            # Retry-After too, so the console can read when to try a refused sign-in again.
            assert cors_headers(response) == {
                "access-control-allow-origin": CONSOLE_ORIGIN,
                "access-control-allow-credentials": "true",
                "access-control-expose-headers": "Retry-After",
            }, response.status_code
            assert "Origin" in [part.strip() for part in response.headers["vary"].split(",")]

        # The browser asks first for a sign-out with its token: the preflight is answered for
        # the console only, cached for ten minutes, with the admin headers.
        answered = preflight(client, CONSOLE_ORIGIN, "DELETE", "content-type, x-csrf-token")
        assert answered.status_code == 204
        assert cors_headers(answered) == {
            "access-control-allow-origin": CONSOLE_ORIGIN,
            "access-control-allow-credentials": "true",
            "access-control-allow-methods": "GET, POST, DELETE",
            "access-control-allow-headers": "Content-Type, X-CSRF-Token",
            "access-control-max-age": "600",
        }
        for name, value in ADMIN_HEADERS.items():
            assert answered.headers[name] == value, name
        # A method or a header it does not allow is not answered.
        for method, headers in (("PUT", ""), ("PATCH", ""), ("DELETE", "x-other")):
            refused = preflight(client, CONSOLE_ORIGIN, method, headers)
            assert refused.status_code == 400, (method, headers)
            assert cors_headers(refused) == {}, (method, headers)

        # Any other origin gets no CORS header at all, on a read, a preflight or an error.
        for origin in OTHER_ORIGINS:
            for response in (
                client.get("/admin/api/requests", headers={**SIGNED_IN, "Origin": origin}),
                client.get("/admin/api/requests", headers={"Origin": origin}),
                client.get(STREAM_ROUTE, headers={"Origin": origin}),
                preflight(client, origin, "GET"),
                preflight(client, origin, "DELETE", "x-csrf-token"),
            ):
                assert cors_headers(response) == {}, (origin, response.status_code)
                assert response.headers["cache-control"] == "no-store", origin

    # Without CONSOLE_ORIGIN, no CORS header for anyone.
    plain = admin_with_clock(admin_settings(database), Clock())
    with client_from(plain) as client:
        for origin in [CONSOLE_ORIGIN, *OTHER_ORIGINS]:
            for response in (
                client.get("/admin/api/requests", headers={**SIGNED_IN, "Origin": origin}),
                preflight(client, origin, "DELETE", "x-csrf-token"),
            ):
                assert cors_headers(response) == {}, origin

    # The setting is one exact origin: a scheme, a host and an optional port, nothing else;
    # plain http only in dev.
    base = {
        "postgres_host": "db.invalid",
        "postgres_db": "sunrise",
        "app_db_user": "sunrise_app",
        "app_db_password": "-".join(["runtime", "role", "pass", "for", "prod"]),
        "audit_hmac_key": TEST_AUDIT_KEY,
        "app_role": "admin",
    }
    for app_env in ("dev", "demo", "prod"):
        for accepted in (
            "https://console.example.org",
            "https://console.example.org:8443",
            "https://localhost:3000",
            "https://127.0.0.1:3000",
        ):
            assert settings_errors(**base, app_env=app_env, console_origin=accepted) == [], (
                app_env,
                accepted,
            )
    for accepted in ("http://localhost:3000", "http://console.example.org"):
        assert settings_errors(**base, app_env="dev", console_origin=accepted) == []
    for app_env, wrong in [
        *[(env, "http://console.example.org") for env in ("demo", "prod")],
        *[
            ("dev", value)
            for value in (
                "*",
                "https://*.example.org",
                "https://*",
                "https://console.example.org/",
                "https://console.example.org/admin",
                "https://console.example.org?x=1",
                "https://console.example.org#x",
                "https://user@console.example.org",
                "https://console.example.org:0",
                "https://console.example.org:65536",
                "https://console.example.org:",
                # A scheme's own port: a browser never sends it, so sign-in would never work.
                "https://console.example.org:443",
                "http://localhost:80",
                "https://CONSOLE.example.org",
                "https://console..example.org",
                "https://-console.example.org",
                "ftp://console.example.org",
                "console.example.org",
                "https://console.example.org https://evil.example.org",
                "https://console.example.org,https://evil.example.org",
                "null",
            )
        ],
    ]:
        assert settings_errors(**base, app_env=app_env, console_origin=wrong) == [
            ("console_origin",)
        ], (app_env, wrong)


@pytest.mark.usefixtures("clean_env")
def test_console_origin_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "APP_ENV": "dev",
        "APP_ROLE": "admin",
        "POSTGRES_HOST": "db.invalid",
        "POSTGRES_DB": "sunrise",
        "APP_DB_USER": "sunrise_app",
        "APP_DB_PASSWORD": "x",
        "AUDIT_HMAC_KEY": TEST_AUDIT_KEY,
        "CONSOLE_ORIGIN": CONSOLE_ORIGIN,
    }.items():
        monkeypatch.setenv(name, value)
    assert Settings(_env_file=None).console_origin == CONSOLE_ORIGIN
    monkeypatch.setenv("CONSOLE_ORIGIN", "")
    assert Settings(_env_file=None).console_origin is None
