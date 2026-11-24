"""APP_ROLE: one image, three ways to run it. `all` serves everything; `voice` the calls and no
admin page or admin API; `console` the admin pages and the admin API, and no call."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_AUDIT_KEY, VOICE_SECRETS
from console_client import ADMIN_USER, PASSWORD, client_from, console_settings
from pydantic import ValidationError

from app import server
from app.config import Settings, StartupSettings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_PATHS = [
    "/admin",
    "/admin/",
    "/admin/login",
    "/admin/calls",
    "/admin/requests",
    "/admin/audit",
    "/admin/static/console.css",
    "/admin/api/requests",
    "/admin/api/audit/verify",
]
CALL_PATHS = ["/", "/ice-servers"]
# What the console process must never load: the voice pipeline, the agent's turn loop, Pipecat,
# the model's client and the transcript masker's NLP.
VOICE_MODULES = ("pipecat", "app.voice", "app.agent.turn", "app.agent.model", "anthropic", "spacy")
# Strong enough for prod, made of short pieces.
PROD_APP_DB_PASSWORD = "-".join(["runtime", "role", "pass", "for", "prod"])
PROD_AUDIT_KEY = "-".join(["audit", "key", "of", "the", "role", "test", "x" * 8])


def errors(**values: Any) -> list[tuple[str, tuple[Any, ...]]]:
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, **values)
    return [(error["type"], error["loc"]) for error in refused.value.errors()]


def prod_console(**overrides: Any) -> dict[str, Any]:
    """A console's settings for prod: the database, the audit key, the admin; nothing else."""
    values = {
        "app_env": "prod",
        "app_role": "console",
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


@pytest.mark.usefixtures("clean_env")
def test_roles_are_settings() -> None:
    assert Settings(_env_file=None, **prod_console()).app_role == "console"
    # A role that is not one is refused, and the rules that depend on it then hold their most
    # demanding form.
    assert ("literal_error", ("app_role",)) in errors(**prod_console(app_role="admin"))
    # The console needs the admin's sign-in, and holds no secret of the calls.
    assert errors(**prod_console(admin_password=None, admin_user=None)) == [
        ("value_error", ("admin_password",))
    ]
    voice_secrets = {
        "identity_hash_key": "-".join(["identity", "key", "of", "the", "role", "test", "y" * 8]),
        "turn_shared_secret": VOICE_SECRETS["TURN_SHARED_SECRET"],
        "demo_access_token": VOICE_SECRETS["DEMO_ACCESS_TOKEN"],
        "anthropic_api_key": VOICE_SECRETS["ANTHROPIC_API_KEY"],
        "deepgram_api_key": VOICE_SECRETS["DEEPGRAM_API_KEY"],
        "elevenlabs_api_key": VOICE_SECRETS["ELEVENLABS_API_KEY"],
    }
    for name, value in voice_secrets.items():
        found = errors(**prod_console(**{name: value}))
        assert ("value_error", (name,)) in found, name
    # The voice role holds no admin sign-in.
    voice = {
        **{key.lower(): value for key, value in VOICE_SECRETS.items()},
        **prod_console(app_role="voice", admin_user=None, admin_password=None),
        "identity_hash_key": voice_secrets["identity_hash_key"],
    }
    assert Settings(_env_file=None, **voice).app_role == "voice"
    found = errors(**{**voice, "admin_user": ADMIN_USER, "admin_password": PASSWORD})
    assert found == [("value_error", ("admin_password",))]
    # Every role but the console needs the identity key, reported as missing.
    assert ("missing", ("identity_hash_key",)) in errors(**{**voice, "identity_hash_key": None})


def test_voice_role_serves_no_admin(database: StartupSettings) -> None:
    settings = console_settings(database, app_role="voice", admin_user=None, admin_password=None)
    # Even given an admin password past the settings' own check, the voice role mounts none.
    forced = settings.model_copy(
        update={
            "admin_user": ADMIN_USER,
            "admin_password": console_settings(database).admin_password,
        }
    )
    for app in (create_app(settings), create_app(forced)):
        with client_from(app) as client:
            for path in ADMIN_PATHS:
                assert client.get(path, auth=(ADMIN_USER, PASSWORD)).status_code == 404, path
            assert client.get("/health").status_code == 200
            assert client.get("/").status_code == 200


def test_console_role_serves_no_call(database: StartupSettings) -> None:
    settings = console_settings(database, app_role="console", identity_hash_key=None)
    app = server.create_app(settings)
    with client_from(app) as client:
        for path in CALL_PATHS:
            assert client.get(path).status_code == 404, path
        assert client.post("/offer", json={"sdp": "v=0", "type": "offer"}).status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/admin/login").status_code == 200
        assert client.get("/admin/api/requests", auth=(ADMIN_USER, PASSWORD)).status_code == 200
        assert client.get("/admin/calls").status_code == 303
    # The voice application refuses to be built for the console role.
    with pytest.raises(ValueError, match="console"):
        create_app(settings)


def test_all_role_serves_both(database: StartupSettings) -> None:
    app = server.create_app(console_settings(database))
    with client_from(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/admin/login").status_code == 200
        assert client.get("/admin/api/requests", auth=(ADMIN_USER, PASSWORD)).status_code == 200


def test_console_role_starts_without_vendor_keys(tmp_path: Path) -> None:
    """A fresh interpreter, as the container starts it: prod, the console role and only its
    own variables. The app is built, and no module of the calls has been imported."""
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update({key.upper(): str(value) for key, value in prod_console().items()})
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
    assert "app.console.pages" in found["modules"]
    assert found["paths"] == ["/admin", "/admin", "/admin/api", "/health"]
    assert TEST_AUDIT_KEY not in result.stdout + result.stderr
