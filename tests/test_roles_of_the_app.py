"""APP_ROLE: one image, three ways to run it. `all` serves everything; `voice` the calls and no
admin route; `admin` the admin API, and no call."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from admin_client import ADMIN_USER, PASSWORD, SIGNED_IN, admin_settings, client_from
from conftest import TEST_AUDIT_KEY, VOICE_SECRETS
from pydantic import ValidationError

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


@pytest.mark.usefixtures("clean_env")
def test_roles_are_settings() -> None:
    assert Settings(_env_file=None, **prod_admin()).app_role == "admin"
    # A role that is not one is refused, and the rules that depend on it then hold their most
    # demanding form. The admin role is no longer called the console.
    for role in ("console", "Admin", ""):
        assert ("literal_error", ("app_role",)) in errors(**prod_admin(app_role=role)), role
    # The admin role may start without the admin's sign-in (it then serves nothing under
    # /admin), and holds no secret of the calls.
    off = Settings(_env_file=None, **prod_admin(admin_password=None, admin_user=None))
    assert off.admin_password is None
    voice_secrets = {
        "identity_hash_key": "-".join(["identity", "key", "of", "the", "role", "test", "y" * 8]),
        "turn_shared_secret": VOICE_SECRETS["TURN_SHARED_SECRET"],
        "call_access_token": VOICE_SECRETS["CALL_ACCESS_TOKEN"],
        "anthropic_api_key": VOICE_SECRETS["ANTHROPIC_API_KEY"],
        "deepgram_api_key": VOICE_SECRETS["DEEPGRAM_API_KEY"],
        "elevenlabs_api_key": VOICE_SECRETS["ELEVENLABS_API_KEY"],
    }
    for name, value in voice_secrets.items():
        found = errors(**prod_admin(**{name: value}))
        assert ("value_error", (name,)) in found, name
    # The voice role holds no admin sign-in.
    voice = {
        **{key.lower(): value for key, value in VOICE_SECRETS.items()},
        **prod_admin(app_role="voice", admin_user=None, admin_password=None),
        "identity_hash_key": voice_secrets["identity_hash_key"],
    }
    assert Settings(_env_file=None, **voice).app_role == "voice"
    found = errors(**{**voice, "admin_user": ADMIN_USER, "admin_password": PASSWORD})
    assert found == [("value_error", ("admin_password",))]
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


def test_all_role_serves_both(database: StartupSettings) -> None:
    app = server.create_app(admin_settings(database))
    with client_from(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/admin/api/requests", headers=SIGNED_IN).status_code == 200


def test_admin_role_starts_without_vendor_keys(tmp_path: Path) -> None:
    """A fresh interpreter, as the container starts it: prod, the admin role and only its own
    variables. The app is built, and no module of the calls has been imported."""
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update({key.upper(): str(value) for key, value in prod_admin().items()})
    script = (
        "import json, sys\n"
        "from app.server import create_app\n"
        "app = create_app()\n"
        "paths = sorted(getattr(route, 'path', '') for route in app.routes)\n"
        "print(json.dumps({'modules': sorted(sys.modules), 'paths': paths}))\n"
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
    assert TEST_AUDIT_KEY not in result.stdout + result.stderr
