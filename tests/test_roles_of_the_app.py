"""APP_ROLE: one image, three ways to run it. `all` serves everything; `voice` the calls and no
admin route; `admin` the admin API, and no call."""

import inspect
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from admin_client import ADMIN_USER, PASSWORD, SIGNED_IN, admin_settings, client_from
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY, VOICE_SECRETS
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from app import server
from app.config import Settings, StartupSettings
from app.main import create_app
from app.runtime_config import ConfigPoll

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_PATHS = [
    "/admin",
    "/admin/",
    "/admin/login",
    "/admin/api",
    "/admin/api/session",
    "/admin/api/requests",
    "/admin/api/queue",
    "/admin/api/audit/verify",
    "/admin/api/calls/live/stream",
    "/admin/api/config",
    "/admin/api/queue/requests/1/handled",
    "/admin/api/queue/requests/1/callback",
]
CALL_PATHS = ["/", "/ice-servers"]
ADMIN_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}
# What the admin process must never load: the voice pipeline, the agent's turn loop, Pipecat,
# the model's client and the transcript masker's NLP.
VOICE_MODULES = ("pipecat", "app.voice", "app.agent.turn", "app.agent.model", "anthropic", "spacy")
# What the admin role does not load, written out by hand: the calls' secrets, and TURN_HOST,
# which goes with the TURN secret. Logged by name, in this order, when given.
IGNORED_BY_THE_ADMIN = {
    "identity_hash_key",
    "turn_shared_secret",
    "turn_host",
    "call_access_token",
    "anthropic_api_key",
    "deepgram_api_key",
    "elevenlabs_api_key",
}
IGNORED_BY_THE_ADMIN_LOGGED = (
    "ANTHROPIC_API_KEY,CALL_ACCESS_TOKEN,DEEPGRAM_API_KEY,ELEVENLABS_API_KEY,"
    "IDENTITY_HASH_KEY,TURN_HOST,TURN_SHARED_SECRET"
)
# Strong enough for prod, made of short pieces.
PROD_APP_DB_PASSWORD = "-".join(["runtime", "role", "pass", "for", "prod"])
PROD_AUDIT_KEY = "-".join(["audit", "key", "of", "the", "role", "test", "x" * 8])


def errors(**values: Any) -> list[tuple[str, tuple[Any, ...]]]:
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, **values)
    return [(error["type"], error["loc"]) for error in refused.value.errors()]


def prod_admin(**overrides: Any) -> dict[str, Any]:
    """The admin role's settings for prod: the database, the audit key, the admin; nothing
    else."""
    values = {
        "app_env": "prod",
        "app_role": "admin",
        "postgres_host": "db.invalid",
        "postgres_db": "sunrise",
        "app_db_user": "sunrise_app",
        "app_db_password": PROD_APP_DB_PASSWORD,
        "audit_hmac_key": PROD_AUDIT_KEY,
        "admin_user": ADMIN_USER,
        "admin_password": PASSWORD,
    }
    values.update(overrides)
    return values


def admin_role(database: StartupSettings, **overrides: Any) -> Settings:
    return admin_settings(database, app_role="admin", identity_hash_key=None, **overrides)


def settings_from_environment(
    monkeypatch: pytest.MonkeyPatch, database: StartupSettings, variables: dict[str, str]
) -> Settings:
    """A role's settings as its container reads them: from the environment alone, which holds
    the database as the runtime role, the audit key and the given variables, and no other
    setting."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    environment = {
        "APP_ENV": "dev",
        "POSTGRES_HOST": database.postgres_host,
        "POSTGRES_PORT": str(database.postgres_port),
        "POSTGRES_DB": database.postgres_db,
        "APP_DB_USER": database.app_db_user,
        "APP_DB_PASSWORD": database.app_db_password.get_secret_value(),
        "AUDIT_HMAC_KEY": TEST_AUDIT_KEY,
        **variables,
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def fresh_admin_process(tmp_path: Path, variables: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """A fresh interpreter, as the container starts it: prod, the admin role, its own variables
    and the given ones. The app is built; returns its output and what it reports: the modules
    imported, the app's paths, and the settings it holds a value for."""
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update({key.upper(): str(value) for key, value in prod_admin().items()})
    env.update(variables)
    script = (
        "import json, sys\n"
        "from app.config import Settings\n"
        "from app.server import create_app\n"
        "settings = Settings()\n"
        "app = create_app(settings)\n"
        "paths = sorted(getattr(route, 'path', '') for route in app.routes)\n"
        "held = sorted(name for name in Settings.model_fields"
        " if getattr(settings, name) is not None)\n"
        "print(json.dumps({'modules': sorted(sys.modules), 'paths': paths, 'held': held}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.strip().splitlines()[-1])
    loaded = [
        name
        for name in found["modules"]
        if any(name == banned or name.startswith(banned + ".") for banned in VOICE_MODULES)
    ]
    assert loaded == []
    assert "app.api.admin" in found["modules"]
    assert found["paths"] == ["/admin/api", "/health"]
    return result.stdout + result.stderr, found


@pytest.mark.usefixtures("clean_env")
def test_roles_are_settings() -> None:
    assert Settings(_env_file=None, **prod_admin()).app_role == "admin"
    # A role that is not one is refused, and the rules that depend on it then hold their most
    # demanding form. The admin role is no longer called the console.
    for role in ("console", "Admin", ""):
        assert ("literal_error", ("app_role",)) in errors(**prod_admin(app_role=role)), role
    # The admin role may start without the admin's sign-in (it then serves nothing under
    # /admin), and holds no secret of the calls: one given to it is not loaded, each on its own.
    off = Settings(_env_file=None, **prod_admin(admin_password=None, admin_user=None))
    assert off.admin_password is None
    assert off.ignored_settings == ()
    voice_secrets = {
        "identity_hash_key": "-".join(["identity", "key", "of", "the", "role", "test", "y" * 8]),
        "turn_shared_secret": VOICE_SECRETS["TURN_SHARED_SECRET"],
        "turn_host": VOICE_SECRETS["TURN_HOST"],
        "call_access_token": VOICE_SECRETS["CALL_ACCESS_TOKEN"],
        "anthropic_api_key": VOICE_SECRETS["ANTHROPIC_API_KEY"],
        "deepgram_api_key": VOICE_SECRETS["DEEPGRAM_API_KEY"],
        "elevenlabs_api_key": VOICE_SECRETS["ELEVENLABS_API_KEY"],
    }
    assert set(voice_secrets) == IGNORED_BY_THE_ADMIN
    for name, value in voice_secrets.items():
        admin = Settings(_env_file=None, **prod_admin(**{name: value}))
        assert getattr(admin, name) is None, name
        assert admin.ignored_settings == (name,), name
    # The voice role holds no admin sign-in: the password given to it is not loaded, and the
    # user alone switches nothing on.
    voice = {
        **{key.lower(): value for key, value in VOICE_SECRETS.items()},
        **prod_admin(app_role="voice", admin_user=None, admin_password=None),
        "identity_hash_key": voice_secrets["identity_hash_key"],
    }
    assert Settings(_env_file=None, **voice).app_role == "voice"
    given = Settings(
        _env_file=None, **{**voice, "admin_user": ADMIN_USER, "admin_password": PASSWORD}
    )
    assert given.admin_password is None
    assert given.ignored_settings == ("admin_password",)
    # Not even a password too weak for prod is looked at.
    weak = Settings(_env_file=None, **{**voice, "admin_password": "short"})
    assert weak.admin_password is None
    assert weak.ignored_settings == ("admin_password",)
    # Every role but the admin's needs the identity key, reported as missing.
    assert ("missing", ("identity_hash_key",)) in errors(**{**voice, "identity_hash_key": None})


def test_voice_role_serves_no_admin(database: StartupSettings) -> None:
    settings = admin_settings(database, app_role="voice", admin_user=None, admin_password=None)
    # Even given an admin password past the settings' own check, the voice role mounts none.
    forced = settings.model_copy(
        update={
            "admin_user": ADMIN_USER,
            "admin_password": admin_settings(database).admin_password,
        }
    )
    for app in (create_app(settings), create_app(forced)):
        with client_from(app) as client:
            for path in ADMIN_PATHS:
                assert client.get(path, headers=SIGNED_IN).status_code == 404, path
                assert client.post(path, json={}, headers=SIGNED_IN).status_code == 404, path
            assert client.get("/health").status_code == 200
            assert client.get("/").status_code == 200


def test_admin_role_serves_no_call(database: StartupSettings) -> None:
    settings = admin_role(database)
    app = server.create_app(settings)
    with client_from(app) as client:
        for path in CALL_PATHS:
            assert client.get(path).status_code == 404, path
        assert client.post("/offer", json={"sdp": "v=0", "type": "offer"}).status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/admin/api/requests", headers=SIGNED_IN).status_code == 200
        assert client.get("/admin/api/session").status_code == 401
    # The voice application refuses to be built for the admin role.
    with pytest.raises(ValueError, match="admin"):
        create_app(settings)


def test_admin_role_without_admin_password_is_off(
    database: StartupSettings, caplog: pytest.LogCaptureFixture
) -> None:
    # A deployment with the admin left off still comes up: the admin process answers /health,
    # serves nothing under /admin, with the admin headers, and says why once.
    caplog.set_level(logging.INFO)
    settings = admin_role(database, admin_user=None, admin_password=None)
    app = server.create_app(settings)
    with client_from(app) as client:
        assert client.get("/health").status_code == 200
        for path in ADMIN_PATHS:
            for method in ("GET", "POST", "DELETE", "OPTIONS"):
                response = client.request(method, path, headers=SIGNED_IN)
                assert response.status_code == 404, (method, path)
                for header, value in ADMIN_HEADERS.items():
                    assert response.headers.get(header) == value, (path, header)
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("admin_off_no_password") == 1


def test_only_the_calls_poll_the_config(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The admin process starts no poll of the config; the voice and the all roles start one
    # each, when they start.
    started: list[str] = []

    async def start(poll: ConfigPoll) -> None:
        started.append(type(poll).__name__)

    monkeypatch.setattr(ConfigPoll, "start", start)
    apps = {
        "admin": server.create_app(admin_role(database)),
        "voice": server.create_app(
            admin_settings(database, app_role="voice", admin_user=None, admin_password=None)
        ),
        "all": server.create_app(admin_settings(database)),
    }
    polls = {}
    for role, app in apps.items():
        started.clear()
        with client_from(app) as client:
            assert client.get("/health").status_code == 200
        polls[role] = len(started)
    assert polls == {"admin": 0, "voice": 1, "all": 1}


def is_coroutine(handler: object) -> bool:
    """Whether a route, dependency, exception handler or middleware runs on the event loop: a
    coroutine function (or async generator), or an object whose call is one. Anything else
    FastAPI and Starlette hand to a thread pool, whose new threads hold the loop as they
    start."""
    call = handler if inspect.isroutine(handler) else type(handler).__call__
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)


def dependencies(dependant: Dependant) -> Iterator[object]:
    for each in dependant.dependencies:
        if each.call is not None:
            yield each.call
        yield from dependencies(each)


def handlers(app: object) -> Iterator[tuple[str, object]]:
    """Everything the app runs for a request, by where it is: the routes, the mounted apps'
    routes, every dependency, the exception handlers and the middleware's dispatch. Wrappers
    of an app (the admin API's headers and CORS) are looked through."""
    while not isinstance(app, Starlette) and hasattr(app, "_app"):
        app = app._app
    if not isinstance(app, Starlette):
        yield "asgi app", app
        return
    for handler in app.exception_handlers.values():
        yield "exception handler", handler
    for middleware in app.user_middleware:
        if "dispatch" in middleware.kwargs:
            yield "middleware", middleware.kwargs["dispatch"]
    yield from routes("", app.routes)


def routes(prefix: str, found: list[Any]) -> Iterator[tuple[str, object]]:
    for route in found:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.endpoint
            for call in dependencies(route.dependant):
                yield f"{prefix}{route.path} dependency", call
        elif isinstance(route, Mount):
            yield from ((f"{prefix}{route.path}{path}", call) for path, call in handlers(route.app))
        elif isinstance(route, Route):
            yield prefix + route.path, route.endpoint
        else:
            # FastAPI 0.165 keeps an included router as one route of its own, with the
            # dependencies given at inclusion.
            context = route.include_context
            for given in context.dependencies:
                yield f"{prefix}{context.prefix} dependency", given.dependency
            yield from routes(prefix + context.prefix, route.original_router.routes)


def test_every_handler_is_a_coroutine(database: StartupSettings) -> None:
    # Nothing a request runs goes to a thread pool: a plain function would, and starting a
    # worker thread holds the event loop, the calls' audio with it, until the thread runs.
    apps = {
        "voice": create_app(
            admin_settings(database, app_role="voice", admin_user=None, admin_password=None)
        ),
        "all": create_app(admin_settings(database)),
        "admin": server.create_app(admin_role(database)),
    }
    checked = {}
    for role, app in apps.items():
        found = list(handlers(app))
        checked[role] = {path for path, _handler in found}
        plain = [path for path, handler in found if not is_coroutine(handler)]
        assert plain == [], (role, plain)
    # The walk reaches what matters: the calls' routes and their token check, the admin API
    # mounted beside them or alone, its reads' and writes' dependencies.
    assert {"/offer", "/offer dependency", "/ice-servers", "/health", "/"} <= checked["voice"]
    assert {"/offer", "/admin/api/queue", "/admin/api/queue dependency"} <= checked["all"]
    assert {
        "/health",
        "/admin/api/config",
        "/admin/api/queue/{source}/{row_id}/callback",
        "exception handler",
    } <= checked["admin"]


def test_all_role_serves_both(database: StartupSettings) -> None:
    app = server.create_app(admin_settings(database))
    with client_from(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/admin/api/requests", headers=SIGNED_IN).status_code == 200


def test_admin_role_starts_without_vendor_keys(tmp_path: Path) -> None:
    """A fresh interpreter, as the container starts it: prod, the admin role and only its own
    variables. The app is built, no module of the calls has been imported, and nothing is
    said to be ignored."""
    output, _found = fresh_admin_process(tmp_path, {})
    assert TEST_AUDIT_KEY not in output
    assert "other_role_settings_ignored" not in output


def test_voice_role_ignores_the_admin_password(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # R1. A platform that gives every container every variable gives the voice role the admin's
    # sign-in too: the voice role starts, holds no admin password, and serves nothing under
    # /admin. Its environment cannot switch the admin API on in the calls' process.
    caplog.set_level(logging.INFO)
    settings = settings_from_environment(
        monkeypatch,
        database,
        {
            "APP_ROLE": "voice",
            "IDENTITY_HASH_KEY": TEST_IDENTITY_KEY,
            "ADMIN_USER": ADMIN_USER,
            "ADMIN_PASSWORD": PASSWORD,
            "CONSOLE_ORIGIN": "https://console.example.test",
        },
    )
    assert settings.app_role == "voice"
    assert settings.admin_password is None
    app = server.create_app(settings)
    with client_from(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        for path in ADMIN_PATHS:
            assert client.get(path, headers=SIGNED_IN).status_code == 404, path
            assert client.post(path, json={}, headers=SIGNED_IN).status_code == 404, path
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("other_role_settings_ignored names=ADMIN_PASSWORD") == 1
    assert not any(PASSWORD in message for message in messages)


def test_admin_role_ignores_the_voice_secrets(
    database: StartupSettings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    # R2. Given every voice-only secret (and TURN_HOST, which goes with the TURN secret), the
    # admin role starts and holds none of them; its config page and its logs show none.
    caplog.set_level(logging.INFO)
    given = {**VOICE_SECRETS, "IDENTITY_HASH_KEY": TEST_IDENTITY_KEY}
    secrets = [value for key, value in given.items() if key.endswith(("_KEY", "_SECRET", "_TOKEN"))]
    assert len(secrets) == 6
    settings = settings_from_environment(
        monkeypatch,
        database,
        {"APP_ROLE": "admin", "ADMIN_USER": ADMIN_USER, "ADMIN_PASSWORD": PASSWORD, **given},
    )
    assert {name: getattr(settings, name) for name in IGNORED_BY_THE_ADMIN} == dict.fromkeys(
        IGNORED_BY_THE_ADMIN
    )
    app = server.create_app(settings)
    with client_from(app) as client:
        config = client.get("/admin/api/config", headers=SIGNED_IN)
        assert config.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count(f"other_role_settings_ignored names={IGNORED_BY_THE_ADMIN_LOGGED}") == 1
    for secret in secrets:
        assert secret not in config.text
        assert not any(secret in message for message in messages)

    # As the container starts it: prod, a fresh interpreter, every variable of the deployment.
    output, found = fresh_admin_process(tmp_path, given)
    assert not IGNORED_BY_THE_ADMIN & set(found["held"])
    assert output.count(f"other_role_settings_ignored names={IGNORED_BY_THE_ADMIN_LOGGED}") == 1
    for secret in secrets:
        assert secret not in output
