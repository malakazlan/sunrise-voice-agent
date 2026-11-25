import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no type information
from conftest import OFFLINE_DB_ENV, VOICE_SECRETS
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.config import (
    ADMIN_FIELDS,
    STARTUP_CHECK_FIELDS,
    VOICE_FIELDS,
    DeploymentSettings,
    Settings,
    StartupSettings,
)
from app.db.session import create_owner_sync_engine
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"

# The application's settings, written out by hand: the runtime role, where the database is,
# and nothing of the owner.
APPLICATION_FIELDS = {
    "app_env",
    "app_role",
    "log_level",
    "postgres_host",
    "postgres_port",
    "postgres_db",
    "app_db_user",
    "app_db_password",
    "audit_hmac_key",
    "identity_hash_key",
    "simulated_now",
    "max_concurrent_calls",
    "max_call_seconds",
    "turn_shared_secret",
    "turn_host",
    "call_access_token",
    "admin_password",
    "admin_user",
    "console_origin",
    "anthropic_api_key",
    "deepgram_api_key",
    "llm_model",
    "elevenlabs_api_key",
    "elevenlabs_voice_id",
    "tts_model",
    "vad_stop_seconds",
    "idle_seconds",
}
# The start-up steps' settings: the same database, the runtime role they set up, the owner.
STARTUP_FIELDS = {
    "app_env",
    "log_level",
    "postgres_host",
    "postgres_port",
    "postgres_db",
    "app_db_user",
    "app_db_password",
    "postgres_user",
    "postgres_password",
}
OWNER_FIELDS = {"postgres_user", "postgres_password"}
# Without a default, but required by a rule rather than by having none: not given to the
# admin role.
REQUIRED_BUT_BY_THE_ADMIN = {"identity_hash_key"}
# The admin's sign-in and the console's origin: the admin role's, never the voice role's.
ADMIN_ONLY_FIELDS = {"admin_user", "admin_password", "console_origin"}
# The admin role's settings: the database as the runtime role, the audit key, the clock and the
# calls' longest (which the live calls follow), the admin's sign-in and the console's origin.
# No key of a vendor, no TURN secret, no access token, no identity key.
ADMIN_ROLE_FIELDS = {
    "app_env",
    "app_role",
    "log_level",
    "postgres_host",
    "postgres_port",
    "postgres_db",
    "app_db_user",
    "app_db_password",
    "audit_hmac_key",
    "simulated_now",
    "max_call_seconds",
    "llm_model",
    "max_concurrent_calls",
    "admin_user",
    "admin_password",
    "console_origin",
}


def every_variable() -> set[str]:
    return {name.upper() for name in (*Settings.model_fields, *StartupSettings.model_fields)}


def test_application_settings_hold_no_owner_identity() -> None:
    assert set(Settings.model_fields) == APPLICATION_FIELDS
    assert set(StartupSettings.model_fields) == STARTUP_FIELDS
    assert not OWNER_FIELDS & set(Settings.model_fields)
    assert not any("owner" in name for name in Settings.model_fields)
    assert not hasattr(Settings, "owner_database_url")
    assert OWNER_FIELDS <= set(StartupSettings.model_fields)


@pytest.mark.usefixtures("offline_env")
def test_one_env_file_serves_both_settings_classes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Each class reads its own keys from the shared .env and leaves the other's.
    for name in (
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "AUDIT_HMAC_KEY",
        "TURN_HOST",
        "TURN_SHARED_SECRET",
    ):
        monkeypatch.delenv(name)
    (tmp_path / ".env").write_text(
        "APP_ENV=dev\nPOSTGRES_USER=owner_from_file\nPOSTGRES_PASSWORD=owner-file-password\n"
        "AUDIT_HMAC_KEY=audit-key-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    application = Settings()
    startup = StartupSettings()

    assert application.audit_hmac_key.get_secret_value() == "audit-key-from-file"
    assert not hasattr(application, "postgres_password")
    assert "owner-file-password" not in repr(application.model_dump())
    assert startup.postgres_user == "owner_from_file"
    assert not hasattr(startup, "audit_hmac_key")

    # A key that belongs to neither is still refused by both.
    (tmp_path / ".env").write_text("APP_ENV=dev\nPOSTGRES_USERR=typo\n", encoding="utf-8")
    for settings_class in (Settings, StartupSettings):
        with pytest.raises(ValidationError) as excinfo:
            settings_class()
        assert [(error["type"], error["loc"]) for error in excinfo.value.errors()][-1] == (
            "extra_forbidden",
            ("postgres_userr",),
        )

    # Only the file is shared: the owner's identity passed to the application is refused.
    with pytest.raises(ValidationError) as passed:
        Settings(_env_file=None, postgres_password="owner-passed-directly")  # type: ignore[call-arg]
    assert ("extra_forbidden", ("postgres_password",)) in [
        (error["type"], error["loc"]) for error in passed.value.errors()
    ]


def test_settings_check_reports_the_startup_settings(tmp_path: Path) -> None:
    # `python -m app.config`, the start-up service's first step: the start-up steps' settings
    # and every application setting, checked together as the role that holds them all. Only
    # what the interpreter needs to start is kept from this environment.
    base = {key: value for key, value in os.environ.items() if key in ("PATH", "SYSTEMROOT")}
    base["PYTHONPATH"] = str(REPO_ROOT)

    def check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "app.config"],
            cwd=tmp_path,
            env={**base, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    refused = check({"APP_ENV": "prod"})
    assert refused.returncode == 1
    for name in sorted(STARTUP_FIELDS - {"app_env", "log_level", "postgres_port"}):
        assert name in refused.stderr, name
    for name in ("audit_hmac_key", "turn_shared_secret", "anthropic_api_key"):
        assert name in refused.stderr, name
    assert re.search(r"validation errors? for DeploymentSettings$", refused.stderr, re.MULTILINE)

    accepted = check({**OFFLINE_DB_ENV, "APP_ENV": "dev"})
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stderr == ""
    # An APP_ROLE given to it changes nothing: every setting is checked as the role all holds,
    # which a role holding fewer secrets would not refuse.
    as_all = check({**OFFLINE_DB_ENV, "APP_ENV": "dev", "APP_ROLE": "admin"})
    assert as_all.returncode == 0, as_all.stderr
    unset = {key: value for key, value in OFFLINE_DB_ENV.items() if key != "IDENTITY_HASH_KEY"}
    missing = check({**unset, "APP_ENV": "dev", "APP_ROLE": "admin"})
    assert missing.returncode == 1
    assert "identity_hash_key" in missing.stderr


# The image serves: uvicorn, started straight by Docker, so it is PID 1 and receives signals.
SERVE_COMMAND = (
    'CMD ["uvicorn", "app.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000",'
    ' "--workers", "1", "--no-access-log"]'
)
# The start-up steps, which the one-shot start-up service runs; any failure stops it there.
STARTUP_COMMAND = (
    'command: ["sh", "-c", "python -m app.config && alembic upgrade head'
    ' && python -m app.db.roles && python -m app.db.seed"]'
)


def compose_service(name: str) -> list[str]:
    """The lines of one service in deploy/docker-compose.yml, stripped."""
    return [line.strip() for line in compose_service_lines(name)]


def test_the_app_image_serves_and_a_startup_service_prepares() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert [line for line in dockerfile.splitlines() if line.startswith("CMD")] == [SERVE_COMMAND]
    assert "env -u" not in dockerfile

    startup = compose_service("startup")
    assert STARTUP_COMMAND in startup
    assert 'restart: "no"' in startup
    app = compose_service("app")
    assert not any(line.startswith("command:") for line in app)
    # The app waits for the start-up service to have exited with 0; a failure keeps it down.
    depends = app[app.index("depends_on:") :]
    assert depends[:3] == ["depends_on:", "startup:", "condition: service_completed_successfully"]
    # Published on loopback only, whatever the host port; APP_HOST_PORT picks it, default 8000.
    published = app[app.index("ports:") + 1 :]
    entries = published[: next(i for i, line in enumerate(published) if not line.startswith("- "))]
    assert entries == ['- "127.0.0.1:${APP_HOST_PORT:-8000}:8000"']

    # The admin API: the same image and command, after the start-up too, on its own loopback
    # port. The name `console` stays free for the console's own service.
    admin = compose_service("admin-api")
    assert "build: *app-image" in admin
    assert not any(line.startswith("command:") for line in admin)
    depends = admin[admin.index("depends_on:") :]
    assert depends[:3] == ["depends_on:", "startup:", "condition: service_completed_successfully"]
    published = admin[admin.index("ports:") + 1 :]
    entries = published[: next(i for i, line in enumerate(published) if not line.startswith("- "))]
    assert entries == ['- "127.0.0.1:${ADMIN_API_HOST_PORT:-8001}:8000"']
    # The service named console is the Next.js app (console/), never the API's image.
    console = compose_service("console")
    assert "context: ../console" in console
    assert not any("app-image" in line for line in console)


def compose_service_lines(name: str) -> list[str]:
    """The raw lines of one service in deploy/docker-compose.yml, its own header excluded."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:") + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index] and not lines[index].startswith("    ")
        ),
        len(lines),
    )
    return lines[start:end]


ANCHOR = re.compile(r"^x-[a-z-]+: &(?P<anchor>[a-z-]+)$")
MAPPING_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Z_]+):")
MERGE = re.compile(r"^ *<<: *(?P<anchors>.+)$")


def compose_anchors() -> dict[str, set[str]]:
    """The variable names each top-level `x-…: &anchor` mapping of the Compose file holds."""
    anchors: dict[str, set[str]] = {}
    current: set[str] | None = None
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if match := ANCHOR.match(line):
            current = anchors.setdefault(match["anchor"], set())
        elif current is not None and line.startswith("  ") and (key := MAPPING_KEY.match(line)):
            current.add(key["key"])
        elif line and not line.startswith(" "):
            current = None
    return anchors


def compose_environment(name: str) -> set[str]:
    """The variable names a service's environment gets, by direct entry or merged anchors."""
    anchors = compose_anchors()
    lines = compose_service_lines(name)
    names: set[str] = set()
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("environment:"):
            inline = stripped.removeprefix("environment:").strip()
            if inline:
                names |= anchors[inline.removeprefix("*")]
            inside = not inline
            continue
        if inside and line.startswith("      "):
            if merge := MERGE.match(line):
                for anchor in merge["anchors"].strip("[]").split(","):
                    names |= anchors[anchor.strip().removeprefix("*")]
            elif key := MAPPING_KEY.match(line):
                names.add(key["key"])
        else:
            inside = False
    return names


def test_compose_gives_each_service_exactly_its_settings_variables() -> None:
    owner = {name.upper() for name in OWNER_FIELDS}
    assert compose_anchors().keys() >= {"database-credentials", "runtime-database"}

    # The roles' fields, as app/config.py declares them, and as they are written out here. The
    # start-up check holds every setting of both classes but the role, which it sets itself.
    assert VOICE_FIELDS == APPLICATION_FIELDS - ADMIN_ONLY_FIELDS
    assert ADMIN_FIELDS == ADMIN_ROLE_FIELDS
    assert ADMIN_FIELDS <= set(Settings.model_fields)
    assert STARTUP_CHECK_FIELDS == (APPLICATION_FIELDS | STARTUP_FIELDS) - {"app_role"}
    assert set(DeploymentSettings.model_fields) == APPLICATION_FIELDS | STARTUP_FIELDS

    assert compose_environment("app") == {name.upper() for name in VOICE_FIELDS}
    # One variable that is no setting, read by uvicorn itself: which proxies' forwarded
    # addresses to trust. The admin API's alone, its sign-in limit counts by address.
    assert compose_environment("admin-api") == {name.upper() for name in ADMIN_FIELDS} | {
        "FORWARDED_ALLOW_IPS"
    }
    assert "FORWARDED_ALLOW_IPS: ${FORWARDED_ALLOW_IPS:-127.0.0.1}" in compose_service("admin-api")
    for service in ("db", "startup", "app", "coturn"):
        assert "FORWARDED_ALLOW_IPS" not in compose_environment(service), service
    assert "FORWARDED_ALLOW_IPS" not in every_variable()
    assert compose_environment("startup") == {name.upper() for name in STARTUP_CHECK_FIELDS}
    assert compose_environment("db") == {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"}
    # Each service runs the role it is given the fields of.
    assert "APP_ROLE: voice" in compose_service("app")
    assert "APP_ROLE: admin" in compose_service("admin-api")
    # The host ports are Compose's alone: in no container and no settings class.
    for service in ("db", "startup", "app", "admin-api", "coturn"):
        for port in ("APP_HOST_PORT", "ADMIN_API_HOST_PORT"):
            assert port not in compose_environment(service), (service, port)
    assert not {"APP_HOST_PORT", "ADMIN_API_HOST_PORT"} & every_variable()

    # No other route either: no env_file anywhere in the app or the admin service, and the
    # owner's names nowhere in their text (not even inside another variable's value).
    for service in ("app", "admin-api"):
        lines = compose_service_lines(service)
        assert not any(line.strip().startswith("env_file") for line in lines), service
        assert not any(name in line for line in lines for name in owner), service
        assert not owner & compose_environment(service), service


def test_shared_secrets_are_refused_at_startup() -> None:
    # The one-shot start-up check is the one process given every secret of the deployment: it
    # refuses the admin password equal to any other, naming both settings and no value. The
    # long-running roles each hold too few of them to see it.
    shared = "-".join(["shared", "secret", "not", "for", "logs", "x" * 8])
    distinct: dict[str, Any] = {
        "postgres_password": "-".join(["owner", "password", "of", "this", "test"]),
        "app_db_password": "-".join(["runtime", "password", "of", "this", "test"]),
        "audit_hmac_key": "-".join(["audit", "key", "of", "this", "test", "y" * 10]),
        "identity_hash_key": "-".join(["identity", "key", "of", "this", "test", "z" * 10]),
        "turn_shared_secret": "-".join(["turn", "secret", "of", "this", "test", "w" * 12]),
        "call_access_token": "-".join(["access", "token", "of", "this", "test", "v" * 12]),
        "anthropic_api_key": "-".join(["anthropic", "key", "of", "this", "test"]),
        "deepgram_api_key": "-".join(["deepgram", "key", "of", "this", "test"]),
        "elevenlabs_api_key": "-".join(["elevenlabs", "key", "of", "this", "test"]),
    }
    base: dict[str, Any] = {
        "app_env": "prod",
        "postgres_host": "db.invalid",
        "postgres_db": "sunrise",
        "postgres_user": "sunrise",
        "app_db_user": "sunrise_app",
        "turn_host": VOICE_SECRETS["TURN_HOST"],
        "elevenlabs_voice_id": VOICE_SECRETS["ELEVENLABS_VOICE_ID"],
        "admin_user": "clinic_admin",
        "admin_password": shared,
    }
    accepted = DeploymentSettings(_env_file=None, **base, **distinct)
    assert accepted.postgres_user == "sunrise"
    assert accepted.app_role == "all"
    for name in distinct:
        with pytest.raises(ValidationError) as refused:
            DeploymentSettings(_env_file=None, **base, **{**distinct, name: shared})
        text = str(refused.value)
        assert re.search(
            rf"admin_password must differ from {name}|{name} must differ from admin_password", text
        ), name
        assert shared not in text, name

    # Not only the admin password: every secret differs from every other, the call access token
    # and the TURN secret among them, each pair named and no value shown.
    others = dict(distinct, admin_password="-".join(["admin", "password", "of", "this", "test"]))
    names = sorted(others)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            values = {**base, **others, first: shared, second: shared}
            with pytest.raises(ValidationError) as refused:
                DeploymentSettings(_env_file=None, **values)
            text = str(refused.value)
            assert re.search(
                rf"{first} must differ from {second}|{second} must differ from {first}", text
            ), (first, second)
            assert shared not in text, (first, second)


def test_ci_checks_each_service_against_its_role() -> None:
    # CI compares the running containers, and Compose's resolved configuration, with the
    # fields of each service (.github/workflows/ci.yml): the admin API's and the start-up's.
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'echo "admin-api $(configured admin-api)"' in workflow
    assert '"admin-api": {name.upper() for name in ADMIN_FIELDS} | {"FORWARDED_ALLOW_IPS"}' in (
        workflow
    )
    assert '("admin-api", ADMIN_FIELDS | {"forwarded_allow_ips"})' in workflow
    # The start-up check reports as the deployment's settings, and a failed start-up keeps the
    # admin API down as it keeps the app down.
    assert 'grep -q "1 validation error for DeploymentSettings"' in workflow
    assert "StartupSettings" not in workflow
    assert 'echo "admin-api container: $state"' in workflow
    # The checks are Python passed in single quotes: a quote inside one ends the shell string.
    scripts = re.findall(r" -c '\n(.*?)\n *'", workflow, re.DOTALL)
    assert len(scripts) >= 3
    for script in scripts:
        assert "'" not in script, script[:60]
    assert '"app": {name.upper() for name in VOICE_FIELDS}' in workflow
    assert '("app", VOICE_FIELDS)' in workflow
    assert '"startup": {name.upper() for name in STARTUP_CHECK_FIELDS}' in workflow
    assert '("startup", STARTUP_CHECK_FIELDS)' in workflow
    # No settings class's fields as a whole any more: each service is checked by its own list,
    # and the console's service is not one of them.
    assert "model_fields" not in workflow
    assert "configured console" not in workflow
    assert '("console",' not in workflow


def readme_configuration() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "\n## Configuration\n" in readme
    return readme.split("\n## Configuration\n", 1)[1].split("\n## ", 1)[0]


def test_readme_variable_tables_match_the_settings() -> None:
    # R2: the README's table of variables is every setting, and its table of services gives
    # each Python service exactly the fields CI checks it against.
    section = readme_configuration()
    variables = set(re.findall(r"^\| `([A-Z_]+)` \|", section, re.MULTILINE))
    assert variables == every_variable()
    services = {
        service: set(re.findall(r"`([A-Z_]+)`", names))
        for service, names in re.findall(r"^\| `([a-z-]+)` \| (.*) \|$", section, re.MULTILINE)
    }
    assert services["app"] == {name.upper() for name in VOICE_FIELDS}
    assert services["admin-api"] == {name.upper() for name in ADMIN_FIELDS} | {
        "FORWARDED_ALLOW_IPS"
    }
    assert services["coturn"] == {"TURN_HOST", "TURN_SHARED_SECRET", "MAX_CONCURRENT_CALLS"}
    assert services["console"] == {"ADMIN_API_ORIGIN"}
    assert services["db"] == {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"}


def test_the_console_gets_only_the_admin_api_origin() -> None:
    # The admin console (console/) holds no secret: its one setting is the admin API's origin, and
    # neither that nor its host port reaches any other service.
    assert compose_environment("console") == {"ADMIN_API_ORIGIN"}
    console = compose_service("console")
    assert console[console.index("ports:") + 1] == '- "127.0.0.1:${CONSOLE_HOST_PORT:-3000}:3000"'
    assert not any(line.startswith(("depends_on", "env_file")) for line in console)
    for service in ("db", "startup", "app", "coturn"):
        assert not {"ADMIN_API_ORIGIN", "CONSOLE_HOST_PORT"} & compose_environment(service), service
    # Nothing depends on it, in either form Compose takes depends_on (a list or a mapping).
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert "console" in services
    for service, definition in services.items():
        assert "console" not in (definition.get("depends_on") or []), service


@pytest.mark.usefixtures("offline_env")
def test_defaults_apply_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")

    settings = Settings(_env_file=None)

    assert settings.app_env == "prod"
    assert settings.log_level == "INFO"
    assert settings.postgres_port == 5432


@pytest.mark.usefixtures("db_env")
def test_missing_app_env_is_rejected() -> None:
    with pytest.raises(ValidationError, match="app_env") as excinfo:
        Settings(_env_file=None)

    assert [error["type"] for error in excinfo.value.errors()] == ["missing"]


@pytest.mark.usefixtures("db_env")
def test_unknown_app_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("literal_error", ("app_env",))]


@pytest.mark.usefixtures("offline_env")
def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "TRACE")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("literal_error", ("log_level",))]


@pytest.mark.usefixtures("offline_env")
def test_unknown_key_in_env_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nAPP_ENVV=prod\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="app_envv") as excinfo:
        Settings()

    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


@pytest.mark.usefixtures("db_env")
def test_old_access_token_name_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # R1: the call page's access token is CALL_ACCESS_TOKEN, and its old name is no alias. A .env
    # that still sets the old name is refused as an unknown key by both settings classes, and
    # the error for the token that is now missing names the new variable. The old name is
    # assembled here so that no tracked file holds it.
    old_name = "DEMO" + "_ACCESS_TOKEN"
    token = "-".join(["call", "token", "of", "this", "test"] + ["u" * 4] * 3)
    (tmp_path / ".env").write_text(f"{old_name}={token}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "prod")
    for key, value in VOICE_SECRETS.items():
        if not key.endswith("_ACCESS_TOKEN"):
            monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as refused:
        Settings()
    assert [(error["type"], error["loc"], error["msg"]) for error in refused.value.errors()] == [
        (
            "value_error",
            ("call_access_token",),
            "Value error, call_access_token is required unless app_env is dev",
        ),
        ("extra_forbidden", (old_name.lower(),), "Extra inputs are not permitted"),
    ]
    assert token not in str(refused.value)

    with pytest.raises(ValidationError) as refused:
        StartupSettings()
    assert [(error["type"], error["loc"]) for error in refused.value.errors()] == [
        ("extra_forbidden", (old_name.lower(),))
    ]

    # Set in the environment instead, the old name is not read: the token is missing.
    (tmp_path / ".env").unlink()
    monkeypatch.setenv(old_name, token)
    with pytest.raises(ValidationError) as refused:
        Settings()
    assert [error["loc"] for error in refused.value.errors()] == [("call_access_token",)]


@pytest.mark.usefixtures("offline_env")
def test_missing_env_file_is_fine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "demo")

    settings = Settings()

    assert settings.app_env == "demo"
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("offline_env")
def test_env_file_value_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().log_level == "DEBUG"


@pytest.mark.usefixtures("offline_env")
def test_environment_variable_beats_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "prod")

    assert Settings().app_env == "prod"


def test_env_example_matches_settings() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    keys = {
        line.split("=", 1)[0].strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert keys == every_variable()


@pytest.mark.usefixtures("clean_env")
def test_env_example_loads_and_carries_defaults() -> None:
    assert ENV_EXAMPLE.is_file()

    for settings_class in (Settings, StartupSettings):
        example = settings_class(_env_file=ENV_EXAMPLE)

        for name, field in settings_class.model_fields.items():
            if not field.is_required() and name not in REQUIRED_BUT_BY_THE_ADMIN:
                assert getattr(example, name) == field.default, name
    # Required by every role but the admin's, which never holds it: the example sets it.
    identity_key = Settings(_env_file=ENV_EXAMPLE).identity_hash_key
    assert identity_key is not None
    assert identity_key.get_secret_value() == example_value("IDENTITY_HASH_KEY")


@pytest.mark.usefixtures("offline_env")
def test_rejection_does_not_echo_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "APP_ENV=demo\nAPP_ENVV=not-for-logs-1\nLOG_LEVEL=not-for-logs-2\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert excinfo.value.error_count() == 2
    assert "not-for-logs" not in str(excinfo.value)


@pytest.mark.usefixtures("clean_env")
def test_missing_database_settings_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    for key, value in VOICE_SECRETS.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = sorted((error["type"], error["loc"]) for error in excinfo.value.errors())
    assert errors == [
        ("missing", ("app_db_password",)),
        ("missing", ("app_db_user",)),
        ("missing", ("audit_hmac_key",)),
        ("missing", ("identity_hash_key",)),
        ("missing", ("postgres_db",)),
        ("missing", ("postgres_host",)),
    ]

    with pytest.raises(ValidationError) as startup:
        StartupSettings(_env_file=None)

    errors = sorted((error["type"], error["loc"]) for error in startup.value.errors())
    assert errors == [
        ("missing", ("app_db_password",)),
        ("missing", ("app_db_user",)),
        ("missing", ("postgres_db",)),
        ("missing", ("postgres_host",)),
        ("missing", ("postgres_password",)),
        ("missing", ("postgres_user",)),
    ]


@pytest.mark.usefixtures("offline_env")
def test_database_url_is_built_from_the_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "clinic")
    monkeypatch.setenv("POSTGRES_USER", "clinic_app")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word/long-enough-1")

    monkeypatch.setenv("APP_DB_USER", "clinic_runtime")
    monkeypatch.setenv("APP_DB_PASSWORD", "r@ntime:word/long-enough-2")

    startup = StartupSettings(_env_file=None)
    owner = startup.owner_database_url
    runtime = Settings(_env_file=None).runtime_database_url

    for url in (owner, runtime, startup.runtime_database_url):
        assert url.drivername == "postgresql+psycopg"
        assert (url.host, url.port, url.database) == ("db.example", 6543, "clinic")
    assert (owner.username, owner.password) == ("clinic_app", "p@ss:word/long-enough-1")
    assert (runtime.username, runtime.password) == ("clinic_runtime", "r@ntime:word/long-enough-2")
    assert startup.runtime_database_url == runtime


def test_database_password_is_hidden(
    server_settings: StartupSettings, caplog: pytest.LogCaptureFixture
) -> None:
    # The real test server, reached with a password it does not accept. The environment is
    # kept as it is: libpq on Windows needs it to run the login handshake.
    password = "not-for-logs-db-password"

    def build(port: object) -> StartupSettings:
        return StartupSettings(
            _env_file=None,
            app_env="prod",
            postgres_host=server_settings.postgres_host,
            postgres_port=port,
            postgres_db=server_settings.postgres_db,
            postgres_user=server_settings.postgres_user,
            postgres_password=password,
            app_db_user=server_settings.app_db_user,
            app_db_password="-".join(["runtime", "password", "of", "this", "test"]),
        )

    settings = build(server_settings.postgres_port)
    url = settings.owner_database_url

    assert url.password == password
    for rendered in (
        repr(settings),
        str(settings),
        repr(settings.model_dump()),
        repr(url),
        str(url),
    ):
        assert password not in rendered

    # A refused login: neither the error nor the engine's log lines carry the password.
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="sqlalchemy")
    engine = create_owner_sync_engine(settings, poolclass=NullPool)
    with pytest.raises(OperationalError) as connect_error:
        engine.connect()

    assert "password authentication failed" in str(connect_error.value)
    assert password not in str(connect_error.value)
    assert password not in repr(connect_error.value)
    assert password not in repr(engine)
    assert password not in caplog.text

    # A working connection logs at debug level; the real password is not in those lines.
    real_password = server_settings.postgres_password.get_secret_value()
    working = create_owner_sync_engine(server_settings, poolclass=NullPool)
    with working.connect() as connection:
        connection.execute(text("SELECT 1"))

    assert any(record.name.startswith("sqlalchemy") for record in caplog.records)
    assert real_password not in caplog.text
    assert real_password not in repr(working)

    # A rejected setting next to a valid password does not echo the password either.
    with pytest.raises(ValidationError) as rejected:
        build("not-a-port")

    assert [error["loc"] for error in rejected.value.errors()] == [("postgres_port",)]
    assert password not in str(rejected.value)


def example_value(name: str) -> str:
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{name} missing from .env.example")


@pytest.mark.usefixtures("offline_env")
def test_weak_database_password_refused_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    placeholder = example_value("POSTGRES_PASSWORD")
    # The placeholder is long enough to pass the length rule, so only its own check stops it.
    assert len(placeholder) >= 16

    refused = [
        ("prod", "fifteen-chars-x"),
        ("demo", "short"),
        ("prod", placeholder),
        ("demo", placeholder),
    ]
    for app_env, password in refused:
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv("POSTGRES_PASSWORD", password)

        with pytest.raises(ValidationError) as excinfo:
            StartupSettings(_env_file=None)

        assert [error["loc"] for error in excinfo.value.errors()] == [("postgres_password",)]
        assert password not in str(excinfo.value), (app_env, password)

    accepted = [
        ("prod", "sixteen-chars-xx"),
        ("dev", placeholder),
        ("dev", "short"),
    ]
    for app_env, password in accepted:
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv("POSTGRES_PASSWORD", password)

        settings = StartupSettings(_env_file=None)

        assert settings.owner_database_url.password == password


@pytest.mark.usefixtures("db_env")
def test_voice_defaults_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")

    settings = Settings(_env_file=None)

    assert settings.max_concurrent_calls == 3
    assert settings.max_call_seconds == 600
    assert settings.call_access_token is None
    assert settings.turn_host is None
    assert settings.turn_shared_secret is None
    assert settings.anthropic_api_key is None
    assert settings.deepgram_api_key is None
    assert settings.llm_model == "claude-haiku-5-5"


# The order Settings checks the voice fields in.
VOICE_FIELD_ORDER = [
    "TURN_SHARED_SECRET",
    "TURN_HOST",
    "CALL_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
]
VENDOR_KEYS = ["ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY"]
SPEECH_SETTINGS = ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize(
    "missing",
    [["ANTHROPIC_API_KEY"], ["DEEPGRAM_API_KEY"], VENDOR_KEYS],
    ids=["anthropic", "deepgram", "both"],
)
def test_app_will_not_start_without_vendor_keys_outside_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: str, missing: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", app_env)
    for key, value in VOICE_SECRETS.items():
        if key not in missing:
            monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        create_app()

    assert [(error["loc"], error["msg"]) for error in excinfo.value.errors()] == [
        ((key.lower(),), f"Value error, {key.lower()} is required unless app_env is dev")
        for key in VENDOR_KEYS
        if key in missing
    ]
    for key in VENDOR_KEYS:
        assert VOICE_SECRETS[key] not in str(excinfo.value)


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize(
    "missing",
    [["ELEVENLABS_API_KEY"], ["ELEVENLABS_VOICE_ID"], SPEECH_SETTINGS],
    ids=["key", "voice", "both"],
)
def test_app_will_not_start_without_the_speech_key_and_voice_outside_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: str, missing: list[str]
) -> None:
    # R11: no default voice is guessed and no call goes out without the key.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", app_env)
    for key, value in VOICE_SECRETS.items():
        if key not in missing:
            monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        create_app()

    assert [(error["loc"], error["msg"]) for error in excinfo.value.errors()] == [
        ((key.lower(),), f"Value error, {key.lower()} is required unless app_env is dev")
        for key in SPEECH_SETTINGS
        if key in missing
    ]
    assert VOICE_SECRETS["ELEVENLABS_API_KEY"] not in str(excinfo.value)


@pytest.mark.usefixtures("db_env")
def test_speech_defaults_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")

    settings = Settings(_env_file=None)

    assert settings.elevenlabs_api_key is None
    assert settings.elevenlabs_voice_id is None
    # ElevenLabs' Flash v2.5: its lowest-latency model the multi-stream WebSocket serves.
    assert settings.tts_model == "eleven_flash_v2_5"
    assert settings.vad_stop_seconds == 0.5
    assert settings.idle_seconds == 30


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    ("key", "value", "accepted"),
    [
        ("TTS_MODEL", "eleven_flash_v2_5", True),
        ("TTS_MODEL", "eleven_flash_v2", True),
        ("TTS_MODEL", "eleven_multilingual_v2", False),
        ("TTS_MODEL", "eleven_v3", False),
        ("TTS_MODEL", "eleven_flash_v2_5&enable_logging=true", False),
        ("ELEVENLABS_VOICE_ID", "voice" + "q" * 15, True),
        ("ELEVENLABS_VOICE_ID", "voice/../../models", False),
        ("ELEVENLABS_VOICE_ID", "voice?model_id=x", False),
        ("ELEVENLABS_VOICE_ID", "v" * 65, False),
        ("VAD_STOP_SECONDS", "0.2", True),
        ("VAD_STOP_SECONDS", "2", True),
        ("VAD_STOP_SECONDS", "0.1", False),
        ("VAD_STOP_SECONDS", "2.5", False),
        ("IDLE_SECONDS", "5", True),
        ("IDLE_SECONDS", "300", True),
        ("IDLE_SECONDS", "4", False),
        ("IDLE_SECONDS", "301", False),
    ],
)
def test_speech_settings_take_only_their_shapes(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str, accepted: bool
) -> None:
    # The model and the voice go into the ElevenLabs URL: nothing but their own characters.
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(key, value)

    if accepted:
        taken = getattr(Settings(_env_file=None), key.lower())
        assert taken == (value if isinstance(taken, str) else type(taken)(value))
        return
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert [error["loc"] for error in excinfo.value.errors()] == [(key.lower(),)]


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    "model", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-4-8", "claude-3.5-x"]
)
def test_llm_model_takes_a_claude_model_id(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("LLM_MODEL", model)

    assert Settings(_env_file=None).llm_model == model


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    "model", ["gpt-5", "Claude-haiku-5-5", "claude-", "claude-haiku 5", "claude-" + "a" * 61]
)
def test_llm_model_refuses_anything_else(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("LLM_MODEL", model)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [(error["type"], error["loc"]) for error in excinfo.value.errors()] == [
        ("string_pattern_mismatch", ("llm_model",))
    ]


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize(
    "missing",
    [["CALL_ACCESS_TOKEN"], ["TURN_HOST"], ["TURN_SHARED_SECRET"], VOICE_FIELD_ORDER],
    ids=["token", "turn_host", "turn_secret", "all"],
)
def test_voice_secrets_required_outside_dev(
    monkeypatch: pytest.MonkeyPatch, app_env: str, missing: list[str]
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    for key, value in VOICE_SECRETS.items():
        if key not in missing:
            monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"], error["msg"]) for error in excinfo.value.errors()]
    assert errors == [
        (
            "value_error",
            (key.lower(),),
            f"Value error, {key.lower()} is required unless app_env is dev",
        )
        for key in VOICE_FIELD_ORDER
        if key in missing
    ]


@pytest.mark.usefixtures("db_env")
def test_app_will_not_start_without_voice_secrets_outside_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(ValidationError, match="call_access_token"):
        create_app()


@pytest.mark.usefixtures("db_env")
def test_dev_runs_without_voice_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")

    assert Settings(_env_file=None).app_env == "dev"


@pytest.mark.usefixtures("db_env")
def test_turn_without_access_token_is_rejected_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("TURN_HOST", VOICE_SECRETS["TURN_HOST"])
    monkeypatch.setenv("TURN_SHARED_SECRET", VOICE_SECRETS["TURN_SHARED_SECRET"])

    with pytest.raises(ValidationError, match="call_access_token") as excinfo:
        Settings(_env_file=None)

    assert excinfo.value.error_count() == 1


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("present", ["TURN_HOST", "TURN_SHARED_SECRET"])
def test_turn_host_and_secret_come_together(monkeypatch: pytest.MonkeyPatch, present: str) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CALL_ACCESS_TOKEN", VOICE_SECRETS["CALL_ACCESS_TOKEN"])
    monkeypatch.setenv(present, VOICE_SECRETS[present])

    with pytest.raises(ValidationError, match="turn_host") as excinfo:
        Settings(_env_file=None)

    assert excinfo.value.error_count() == 1


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    "turn_host", ["turn.example.test:3478", "turn:turn.example.test", "a b", "x?transport=tcp"]
)
def test_turn_host_must_be_a_bare_host_name(
    monkeypatch: pytest.MonkeyPatch, turn_host: str
) -> None:
    for key, value in VOICE_SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TURN_HOST", turn_host)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("string_pattern_mismatch", ("turn_host",))]


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    ("key", "value", "error_type"),
    [
        ("MAX_CONCURRENT_CALLS", "0", "greater_than"),
        # A sign is refused before the number is read, as coturn's entrypoint refuses it.
        ("MAX_CONCURRENT_CALLS", "-1", "value_error"),
        ("MAX_CALL_SECONDS", "0", "greater_than"),
        ("MAX_CALL_SECONDS", "-1", "greater_than"),
    ],
)
def test_call_limits_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str, error_type: str
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [(error_type, (key.lower(),))]


@pytest.mark.usefixtures("db_env")
def test_voice_secrets_are_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in VOICE_SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("APP_ENV", "prod")

    settings = Settings(_env_file=None)

    for shown in (repr(settings), str(settings), settings.model_dump_json()):
        assert "not-for-logs" not in shown

    # A rejected configuration must not echo the secrets it was given either.
    monkeypatch.delenv("TURN_HOST")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "not-for-logs" not in str(excinfo.value)
    assert "not-for-logs" not in repr(excinfo.value.errors())


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("key", ["CALL_ACCESS_TOKEN", "TURN_SHARED_SECRET"])
@pytest.mark.parametrize("app_env", ["dev", "demo", "prod"])
def test_short_secrets_are_refused(monkeypatch: pytest.MonkeyPatch, key: str, app_env: str) -> None:
    for name, value in VOICE_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APP_ENV", app_env)
    # 31 characters, one short of the minimum; built from pieces like the other fake values.
    short = "-".join(["short", "not", "for", "logs"]) + "z" * 13
    assert len(short) == 31
    monkeypatch.setenv(key, short)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [(error["type"], error["loc"]) for error in excinfo.value.errors()] == [
        ("too_short", (key.lower(),))
    ]
    assert short not in str(excinfo.value)
    assert short not in repr(excinfo.value)

    # 32 characters is enough.
    monkeypatch.setenv(key, short + "z")
    assert getattr(Settings(_env_file=None), key.lower()).get_secret_value() == short + "z"


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize("key", VOICE_FIELD_ORDER)
def test_empty_voice_values_count_as_missing_outside_dev(
    monkeypatch: pytest.MonkeyPatch, app_env: str, key: str
) -> None:
    for name, value in VOICE_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv(key, "")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [(error["loc"], error["msg"]) for error in excinfo.value.errors()] == [
        ((key.lower(),), f"Value error, {key.lower()} is required unless app_env is dev")
    ]


@pytest.mark.usefixtures("db_env")
def test_empty_voice_values_in_dev_leave_voice_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    for name in VOICE_SECRETS:
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert (
        settings.call_access_token,
        settings.turn_host,
        settings.turn_shared_secret,
        settings.anthropic_api_key,
        settings.deepgram_api_key,
        settings.elevenlabs_api_key,
        settings.elevenlabs_voice_id,
    ) == (None, None, None, None, None, None, None)


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    ("key", "highest"), [("MAX_CONCURRENT_CALLS", 20), ("MAX_CALL_SECONDS", 3600)]
)
def test_call_limits_have_upper_bounds(
    monkeypatch: pytest.MonkeyPatch, key: str, highest: int
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(key, str(highest))
    assert getattr(Settings(_env_file=None), key.lower()) == highest

    monkeypatch.setenv(key, str(highest + 1))
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("less_than_equal", (key.lower(),))]


COTURN_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "deploy" / "coturn" / "validation-cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize(
    "case", COTURN_CASES, ids=[f"{case['name']}={case['value']!r}" for case in COTURN_CASES]
)
def test_app_and_coturn_accept_the_same_values(
    monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    # The coturn side of the same table runs in CI (scripts/check_coturn_entrypoint.py).
    for name, value in VOICE_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv(case["name"], case["value"])

    if case["accepted"]:
        settings = Settings(_env_file=None)
        if "calls" in case:
            assert settings.max_concurrent_calls == case["calls"]
    else:
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        assert [error["loc"] for error in excinfo.value.errors()] == [(case["name"].lower(),)]
        if len(case["value"]) >= 4:
            assert case["value"] not in str(excinfo.value)


# Long enough and of allowed characters for every secret, so only the shared rule decides.
NINE_DISTINCT = "abcdefghi" * 4
TEN_DISTINCT = "abcdefghij" * 4


@pytest.mark.usefixtures("offline_env")
@pytest.mark.parametrize(
    ("name", "settings_class"),
    [
        ("POSTGRES_PASSWORD", StartupSettings),
        ("APP_DB_PASSWORD", Settings),
        ("APP_DB_PASSWORD", StartupSettings),
        ("CALL_ACCESS_TOKEN", Settings),
        ("TURN_SHARED_SECRET", Settings),
        ("AUDIT_HMAC_KEY", Settings),
        ("IDENTITY_HASH_KEY", Settings),
        ("ANTHROPIC_API_KEY", Settings),
        ("DEEPGRAM_API_KEY", Settings),
    ],
)
def test_secrets_need_ten_distinct_characters_outside_dev(
    monkeypatch: pytest.MonkeyPatch, name: str, settings_class: type[Settings | StartupSettings]
) -> None:
    assert (len(set(NINE_DISTINCT)), len(set(TEN_DISTINCT))) == (9, 10)
    padded = [" " + TEN_DISTINCT, TEN_DISTINCT + " ", TEN_DISTINCT + "\n"]
    refused = [NINE_DISTINCT, "ab" * 20, *padded]
    for app_env in ("demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        for value in refused:
            monkeypatch.setenv(name, value)

            with pytest.raises(ValidationError) as excinfo:
                settings_class(_env_file=None)

            assert [error["loc"] for error in excinfo.value.errors()] == [(name.lower(),)], value
            assert value.strip() not in str(excinfo.value)

        monkeypatch.setenv(name, TEN_DISTINCT)
        settings = settings_class(_env_file=None)
        assert getattr(settings, name.lower()).get_secret_value() == TEN_DISTINCT

    # In dev the shared rule does not apply; each setting's own rules still do.
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(name, NINE_DISTINCT)
    settings = settings_class(_env_file=None)
    assert getattr(settings, name.lower()).get_secret_value() == NINE_DISTINCT


@pytest.mark.usefixtures("offline_env")
def test_runtime_database_identity_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_user = OFFLINE_DB_ENV["POSTGRES_USER"]
    owner_password = OFFLINE_DB_ENV["POSTGRES_PASSWORD"]
    runtime_user = OFFLINE_DB_ENV["APP_DB_USER"]
    runtime_password = OFFLINE_DB_ENV["APP_DB_PASSWORD"]
    assert (runtime_user, runtime_password) != (owner_user, owner_password)
    # The runtime identity's own rules hold in both classes; the rules that compare it with
    # the owner's can only hold where the owner is known, the start-up settings.
    both = (Settings, StartupSettings)

    def errors_for(
        settings_class: type[Settings | StartupSettings], **values: str
    ) -> list[tuple[str | int, ...]]:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError) as excinfo:
            settings_class(_env_file=None)
        for value in values.values():
            if len(value) >= 4:
                assert value not in str(excinfo.value), value
        return [error["loc"] for error in excinfo.value.errors()]

    # Both are required in every environment; an empty password is no password.
    for settings_class in both:
        for app_env in ("dev", "demo", "prod"):
            monkeypatch.setenv("APP_ENV", app_env)
            monkeypatch.delenv("APP_DB_USER")
            monkeypatch.delenv("APP_DB_PASSWORD")
            with pytest.raises(ValidationError) as missing:
                settings_class(_env_file=None)
            assert sorted((error["type"], error["loc"]) for error in missing.value.errors()) == [
                ("missing", ("app_db_password",)),
                ("missing", ("app_db_user",)),
            ]
            monkeypatch.setenv("APP_DB_USER", runtime_user)
            with pytest.raises(ValidationError, match="must not be empty") as empty:
                settings_class(_env_file=None, app_db_password="")
            assert [error["loc"] for error in empty.value.errors()] == [("app_db_password",)]
            monkeypatch.setenv("APP_DB_PASSWORD", runtime_password)

    # The user name: lower-case letters, digits and underscores, at most 63 (Postgres cuts
    # longer names), not in Postgres's reserved pg_ prefix.
    for settings_class in both:
        for app_env in ("dev", "demo", "prod"):
            monkeypatch.setenv("APP_ENV", app_env)
            for refused in (
                "Sunrise_app",
                "sunrise-app",
                "sunrise app",
                'sunrise_app"',
                "sunrise_app;",
                "sünrise_app",
                "pg_sunrise",
                "a" * 64,
            ):
                assert errors_for(settings_class, APP_DB_USER=refused) == [("app_db_user",)], (
                    app_env,
                    refused,
                )
            for accepted in ("sunrise_app", "app2", "_app", "a" * 63):
                monkeypatch.setenv("APP_DB_USER", accepted)
                assert settings_class(_env_file=None).app_db_user == accepted
            monkeypatch.setenv("APP_DB_USER", runtime_user)

    # The owner's name and password are never the runtime role's, in any environment. The
    # start-up settings refuse them on the owner's fields, which they hold.
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        assert errors_for(StartupSettings, APP_DB_USER=owner_user) == [("postgres_user",)]
        monkeypatch.setenv("APP_DB_USER", runtime_user)
        assert errors_for(StartupSettings, APP_DB_PASSWORD=owner_password) == [
            ("postgres_password",)
        ]
        monkeypatch.setenv("APP_DB_PASSWORD", runtime_password)

    # Outside dev it is as strong as the owner's: 16 characters, 10 different ones, no
    # surrounding whitespace, not a placeholder from .env.example.
    for settings_class in both:
        for app_env in ("demo", "prod"):
            monkeypatch.setenv("APP_ENV", app_env)
            for refused in (
                "fifteen-chars-x",
                NINE_DISTINCT,
                " " + TEN_DISTINCT,
                example_value("APP_DB_PASSWORD"),
                example_value("POSTGRES_PASSWORD"),
            ):
                assert errors_for(settings_class, APP_DB_PASSWORD=refused) == [
                    ("app_db_password",)
                ], refused
            monkeypatch.setenv("APP_DB_PASSWORD", "sixteen-chars-xx")
            settings = settings_class(_env_file=None)
            assert settings.app_db_password.get_secret_value() == "sixteen-chars-xx"
        monkeypatch.setenv("APP_ENV", "dev")
        monkeypatch.setenv("APP_DB_PASSWORD", "short")
        assert settings_class(_env_file=None).app_db_password.get_secret_value() == "short"
        monkeypatch.setenv("APP_DB_PASSWORD", runtime_password)

    # Neither the password nor the owner's is shown by the settings or their URLs.
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("POSTGRES_PASSWORD", "owner-password-long-enough")
    startup = StartupSettings(_env_file=None)
    application = Settings(_env_file=None)
    runtime_url = application.runtime_database_url
    owner_url = startup.owner_database_url
    assert (runtime_url.username, runtime_url.password) == (runtime_user, runtime_password)
    assert (owner_url.username, owner_url.password) == (owner_user, "owner-password-long-enough")
    for url in (runtime_url, owner_url):
        assert (url.drivername, url.host, url.port, url.database) == (
            "postgresql+psycopg",
            OFFLINE_DB_ENV["POSTGRES_HOST"],
            5432,
            OFFLINE_DB_ENV["POSTGRES_DB"],
        )
    for rendered in (
        repr(startup),
        str(startup),
        repr(startup.model_dump()),
        repr(application),
        repr(application.model_dump()),
        repr(runtime_url),
        str(runtime_url),
        repr(owner_url),
        str(owner_url),
    ):
        assert runtime_password not in rendered
        assert "owner-password-long-enough" not in rendered
    # The application's settings were built from the same environment and hold no owner.
    assert "owner-password-long-enough" not in repr(application.model_dump())
    assert not OWNER_FIELDS & set(application.model_dump())


@pytest.mark.usefixtures("offline_env")
@pytest.mark.parametrize("name", ["AUDIT_HMAC_KEY", "IDENTITY_HASH_KEY"])
def test_hmac_key_rules(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    placeholder = example_value(name)
    # The placeholder is long enough to pass the length rule, so only its own check stops it.
    assert len(placeholder) >= 32

    # Required in every environment; an empty value is no key.
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.delenv(name)
        with pytest.raises(ValidationError) as missing:
            Settings(_env_file=None)
        assert [(error["type"], error["loc"]) for error in missing.value.errors()] == [
            ("missing", (name.lower(),))
        ]

        monkeypatch.setenv(name, "")
        with pytest.raises(ValidationError) as empty:
            Settings(_env_file=None)
        assert [error["loc"] for error in empty.value.errors()] == [(name.lower(),)]

        # Passed directly, an empty key reaches the validator itself.
        with pytest.raises(ValidationError, match="must not be empty") as empty_argument:
            Settings.model_validate({"_env_file": None, name.lower(): ""})
        assert [error["loc"] for error in empty_argument.value.errors()] == [(name.lower(),)]

    thirty_one = "boundary-audit-key-" + "x" * 12
    thirty_two = thirty_one + "y"
    assert (len(thirty_one), len(thirty_two)) == (31, 32)

    refused = [
        ("prod", thirty_one),
        ("demo", thirty_one),
        ("demo", "short-audit-key"),
        ("prod", placeholder),
        ("demo", placeholder),
    ]
    for app_env, key in refused:
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv(name, key)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)

        assert [error["loc"] for error in excinfo.value.errors()] == [(name.lower(),)]
        assert key not in str(excinfo.value), (app_env, key)

    accepted = [
        ("prod", thirty_two),
        ("demo", thirty_two),
        ("dev", placeholder),
        ("dev", thirty_one),
        ("dev", "k"),
    ]
    for app_env, key in accepted:
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv(name, key)

        settings = Settings(_env_file=None)

        assert getattr(settings, name.lower()).get_secret_value() == key
        if len(key) > 1:
            for rendered in (repr(settings), str(settings), repr(settings.model_dump())):
                assert key not in rendered

    # A rejected setting next to a valid key does not echo the key either.
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv(name, thirty_two)
    monkeypatch.setenv("LOG_LEVEL", "TRACE")
    with pytest.raises(ValidationError) as other:
        Settings(_env_file=None)
    assert [error["loc"] for error in other.value.errors()] == [("log_level",)]
    assert thirty_two not in str(other.value)


@pytest.mark.usefixtures("offline_env")
def test_simulated_now_refused_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    for value in ("2026-11-24T10:00:00", "2026-11-24T15:00:00Z", "2026-11-24T10:00:00-05:00"):
        monkeypatch.setenv("SIMULATED_NOW", value)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)

        assert [error["loc"] for error in excinfo.value.errors()] == [("simulated_now",)]
        assert value not in str(excinfo.value)
        assert "2026-11-24" not in str(excinfo.value)

    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert sorted(error["loc"] for error in excinfo.value.errors()) == [
        ("app_env",),
        ("simulated_now",),
    ]


@pytest.mark.usefixtures("offline_env")
def test_simulated_now_allowed_in_dev_and_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMULATED_NOW", "2026-11-24T15:00:00Z")
    for app_env in ("dev", "demo"):
        monkeypatch.setenv("APP_ENV", app_env)

        settings = Settings(_env_file=None)

        assert settings.simulated_now == datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


@pytest.mark.usefixtures("offline_env")
def test_simulated_now_without_zone_is_clinic_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "demo")
    rows = [
        ("2026-11-24T08:59:00", datetime(2026, 11, 24, 13, 59, tzinfo=UTC)),  # EST
        ("2026-07-01T09:00:00", datetime(2026, 7, 1, 13, 0, tzinfo=UTC)),  # EDT
    ]
    for value, expected in rows:
        monkeypatch.setenv("SIMULATED_NOW", value)

        simulated = Settings(_env_file=None).simulated_now

        assert simulated is not None
        assert simulated.utcoffset() is not None
        assert simulated == expected


@pytest.mark.usefixtures("offline_env")
def test_simulated_now_inside_a_clock_change_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "demo")
    # 02:30 on 2026-03-08 does not exist in New York; 01:30 on 2026-11-01 happens twice.
    for value in ("2026-03-08T02:30:00", "2026-11-01T01:30:00"):
        monkeypatch.setenv("SIMULATED_NOW", value)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)

        assert [error["loc"] for error in excinfo.value.errors()] == [("simulated_now",)]
        assert value not in str(excinfo.value)

    # With an explicit offset the same wall time is unambiguous.
    monkeypatch.setenv("SIMULATED_NOW", "2026-11-01T01:30:00-05:00")
    assert Settings(_env_file=None).simulated_now == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


@pytest.mark.usefixtures("offline_env")
def test_blank_simulated_now_means_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=prod\nSIMULATED_NOW=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().simulated_now is None


@pytest.mark.usefixtures("offline_env")
def test_simulated_now_accepts_only_an_iso_date_and_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "demo")
    refused = [
        "0",
        "1764000000",
        "2026-11-24",
        "2026-11-24T10",
        "20261124T103000",
        "2026-11-24T10:30:00+05",
        "tomorrow at 10",
    ]
    for value in refused:
        monkeypatch.setenv("SIMULATED_NOW", value)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)

        assert [error["loc"] for error in excinfo.value.errors()] == [("simulated_now",)], value
        if len(value) >= 8:
            assert value not in str(excinfo.value)

    monkeypatch.delenv("SIMULATED_NOW")
    for not_text in (0, 1764000000, datetime(2026, 11, 24, 15, 0, tzinfo=UTC)):
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None, simulated_now=not_text)

        assert [error["loc"] for error in excinfo.value.errors()] == [("simulated_now",)]

    accepted = [
        ("2026-11-24T10:30:00", datetime(2026, 11, 24, 15, 30, tzinfo=UTC)),
        ("2026-11-24 10:30:00", datetime(2026, 11, 24, 15, 30, tzinfo=UTC)),
        ("2026-11-24T10:30", datetime(2026, 11, 24, 15, 30, tzinfo=UTC)),
        ("2026-11-24T15:30:00Z", datetime(2026, 11, 24, 15, 30, tzinfo=UTC)),
        ("2026-11-24T10:30:00.5-05:00", datetime(2026, 11, 24, 15, 30, 0, 500000, tzinfo=UTC)),
    ]
    for value, expected in accepted:
        monkeypatch.setenv("SIMULATED_NOW", value)

        assert Settings(_env_file=None).simulated_now == expected, value


ADMIN_PASSWORD = "-".join(["admin", "pass", "not", "for", "logs"])


def admin_errors(monkeypatch: pytest.MonkeyPatch, **values: str) -> list[tuple[Any, str]]:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    for value in values.values():
        if len(value) >= 4:
            assert value.strip() not in str(excinfo.value), value
    return [(error["loc"], error["msg"]) for error in excinfo.value.errors()]


@pytest.mark.usefixtures("offline_env")
def test_admin_settings_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off by default: neither is required.
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        settings = Settings(_env_file=None)
        assert (settings.admin_user, settings.admin_password) == (None, None)

    # A password needs a user, in every environment; a user alone leaves admin off.
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.delenv("ADMIN_USER", raising=False)
        assert admin_errors(monkeypatch, ADMIN_PASSWORD=ADMIN_PASSWORD) == [
            (("admin_user",), "Value error, admin_user is required when admin_password is set")
        ]
        monkeypatch.delenv("ADMIN_PASSWORD")
        monkeypatch.setenv("ADMIN_USER", "clinic_admin")
        assert Settings(_env_file=None).admin_user == "clinic_admin"
        monkeypatch.delenv("ADMIN_USER")

    # The user name: the audit log's admin name, 1 to 32 letters and underscores, starting
    # with a letter.
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        for refused in (
            "admin-1",
            "1admin",
            "_admin",
            "admin user",
            "admin:root",
            "admin;",
            "admin'",
            "ädmin",
            "a" * 33,
        ):
            assert admin_errors(monkeypatch, ADMIN_USER=refused) == [
                (
                    ("admin_user",),
                    "Value error, admin_user must be 1 to 32 letters and underscores,"
                    " starting with a letter",
                )
            ], refused
        for accepted in ("a", "Z" * 32, "clinic_admin", "Front_Desk"):
            monkeypatch.setenv("ADMIN_USER", accepted)
            assert Settings(_env_file=None).admin_user == accepted

    # Outside dev the password is as strong as a database password: 16 characters, 10
    # different ones, no surrounding whitespace.
    monkeypatch.setenv("ADMIN_USER", "clinic_admin")
    for app_env in ("demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        for refused in ("fifteen-chars-x", NINE_DISTINCT, " " + TEN_DISTINCT, TEN_DISTINCT + " "):
            assert [loc for loc, _ in admin_errors(monkeypatch, ADMIN_PASSWORD=refused)] == [
                ("admin_password",)
            ], refused
        for accepted in ("sixteen-chars-xx", TEN_DISTINCT):
            monkeypatch.setenv("ADMIN_PASSWORD", accepted)
            settings = Settings(_env_file=None)
            assert settings.admin_password is not None
            assert settings.admin_password.get_secret_value() == accepted
            assert accepted not in repr(settings) + str(settings.model_dump())
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    settings = Settings(_env_file=None)
    assert settings.admin_password is not None
    assert settings.admin_password.get_secret_value() == "short"

    # Passed directly, an empty password reaches the validator, in every environment.
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        with pytest.raises(ValidationError, match="must not be empty") as empty:
            Settings(_env_file=None, admin_password="")
        assert [error["loc"] for error in empty.value.errors()] == [("admin_password",)]


@pytest.mark.usefixtures("offline_env")
def test_admin_password_differs_from_every_other_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every secret field of Settings, found from the class, so a new one is covered as soon as
    # it is declared; written out here so that a new one is also noticed.
    secret_fields = {
        name
        for name, field in Settings.model_fields.items()
        if SecretStr in (field.annotation, *get_args(field.annotation))
    }
    assert secret_fields - {"admin_password"} == {
        "app_db_password",
        "audit_hmac_key",
        "identity_hash_key",
        "turn_shared_secret",
        "call_access_token",
        "anthropic_api_key",
        "deepgram_api_key",
        "elevenlabs_api_key",
    }
    monkeypatch.setenv("ADMIN_USER", "clinic_admin")
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        for name in (
            "APP_DB_PASSWORD",
            "AUDIT_HMAC_KEY",
            "IDENTITY_HASH_KEY",
            "TURN_SHARED_SECRET",
            "CALL_ACCESS_TOKEN",
            "ANTHROPIC_API_KEY",
            "DEEPGRAM_API_KEY",
        ):
            assert admin_errors(monkeypatch, ADMIN_PASSWORD=os.environ[name]) == [
                ((), f"Value error, admin_password must differ from {name.lower()}")
            ], (app_env, name)
        monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
        assert Settings(_env_file=None).admin_user == "clinic_admin"


@pytest.mark.usefixtures("offline_env")
def test_identity_key_differs_from_the_audit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = "-".join(["one", "key", "for", "both", "uses", "x" * 12])
    assert len(shared) >= 32
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.setenv("AUDIT_HMAC_KEY", shared)
        monkeypatch.setenv("IDENTITY_HASH_KEY", shared)
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        assert [error["loc"] for error in excinfo.value.errors()] == [("identity_hash_key",)]
        assert shared not in str(excinfo.value)
        assert "audit_hmac_key" in str(excinfo.value)
        monkeypatch.setenv("IDENTITY_HASH_KEY", shared + "z")
        identity_key = Settings(_env_file=None).identity_hash_key
        assert identity_key is not None
        assert identity_key.get_secret_value() == shared + "z"
