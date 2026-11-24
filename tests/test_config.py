import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import OFFLINE_DB_ENV, VOICE_SECRETS
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.config import Settings, StartupSettings
from app.db.session import create_owner_sync_engine
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The application's settings, written out by hand: the runtime role, where the database is,
# and nothing of the owner.
APPLICATION_FIELDS = {
    "app_env",
    "log_level",
    "postgres_host",
    "postgres_port",
    "postgres_db",
    "app_db_user",
    "app_db_password",
    "audit_hmac_key",
    "simulated_now",
    "max_concurrent_calls",
    "max_call_seconds",
    "turn_shared_secret",
    "turn_host",
    "demo_access_token",
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


def test_settings_check_reports_both_classes(tmp_path: Path) -> None:
    # `python -m app.config`, the container's first step. Only what the interpreter needs to
    # start is kept from this environment.
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
    for name in (
        *sorted(STARTUP_FIELDS - {"app_env", "log_level", "postgres_port"}),
        "audit_hmac_key",
        "turn_shared_secret",
        "turn_host",
        "demo_access_token",
    ):
        assert name in refused.stderr, name
    assert re.search(r"validation errors? for StartupSettings$", refused.stderr, re.MULTILINE)
    assert re.search(r"validation errors? for Settings$", refused.stderr, re.MULTILINE)

    accepted = check({**OFFLINE_DB_ENV, "APP_ENV": "dev"})
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stderr == ""


# The container's start command: the start-up steps, then uvicorn without the owner's variables.
START_COMMAND = re.compile(r'^CMD \["sh", "-c", "(?P<command>[^"]*)"\]$', re.MULTILINE)


def test_container_start_drops_the_owner_identity() -> None:
    match = START_COMMAND.search((REPO_ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert match is not None
    steps = match["command"].split(" && ")

    assert steps[:4] == [
        "python -m app.config",
        "alembic upgrade head",
        "python -m app.db.roles",
        "python -m app.db.seed",
    ]
    [serve] = steps[4:]
    words = serve.split()
    # exec: uvicorn replaces the shell, so it is PID 1 and receives the container's signals.
    assert words[:2] == ["exec", "env"]
    removed = {words[index + 1] for index, word in enumerate(words) if word == "-u"}
    assert removed == {name.upper() for name in OWNER_FIELDS}
    assert removed == {name.upper() for name in StartupSettings.model_fields} - {
        name.upper() for name in Settings.model_fields
    }
    assert words[2 + 2 * len(removed)] == "uvicorn"


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
            if not field.is_required():
                assert getattr(example, name) == field.default, name


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
    assert settings.demo_access_token is None
    assert settings.turn_host is None
    assert settings.turn_shared_secret is None


# The order Settings checks the voice fields in.
VOICE_FIELD_ORDER = ["TURN_SHARED_SECRET", "TURN_HOST", "DEMO_ACCESS_TOKEN"]


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize(
    "missing",
    [["DEMO_ACCESS_TOKEN"], ["TURN_HOST"], ["TURN_SHARED_SECRET"], VOICE_FIELD_ORDER],
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

    with pytest.raises(ValidationError, match="demo_access_token"):
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

    with pytest.raises(ValidationError, match="demo_access_token") as excinfo:
        Settings(_env_file=None)

    assert excinfo.value.error_count() == 1


@pytest.mark.usefixtures("db_env")
@pytest.mark.parametrize("present", ["TURN_HOST", "TURN_SHARED_SECRET"])
def test_turn_host_and_secret_come_together(monkeypatch: pytest.MonkeyPatch, present: str) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", VOICE_SECRETS["DEMO_ACCESS_TOKEN"])
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
@pytest.mark.parametrize("key", ["DEMO_ACCESS_TOKEN", "TURN_SHARED_SECRET"])
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
@pytest.mark.parametrize("key", ["DEMO_ACCESS_TOKEN", "TURN_SHARED_SECRET", "TURN_HOST"])
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

    assert (settings.demo_access_token, settings.turn_host, settings.turn_shared_secret) == (
        None,
        None,
        None,
    )


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
        ("DEMO_ACCESS_TOKEN", Settings),
        ("TURN_SHARED_SECRET", Settings),
        ("AUDIT_HMAC_KEY", Settings),
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
def test_audit_key_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    placeholder = example_value("AUDIT_HMAC_KEY")
    # The placeholder is long enough to pass the length rule, so only its own check stops it.
    assert len(placeholder) >= 32

    # Required in every environment; an empty value is no key.
    for app_env in ("dev", "demo", "prod"):
        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.delenv("AUDIT_HMAC_KEY")
        with pytest.raises(ValidationError) as missing:
            Settings(_env_file=None)
        assert [(error["type"], error["loc"]) for error in missing.value.errors()] == [
            ("missing", ("audit_hmac_key",))
        ]

        monkeypatch.setenv("AUDIT_HMAC_KEY", "")
        with pytest.raises(ValidationError) as empty:
            Settings(_env_file=None)
        assert [error["loc"] for error in empty.value.errors()] == [("audit_hmac_key",)]

        # Passed directly, an empty key reaches the validator itself.
        with pytest.raises(ValidationError, match="must not be empty") as empty_argument:
            Settings(_env_file=None, audit_hmac_key="")
        assert [error["loc"] for error in empty_argument.value.errors()] == [("audit_hmac_key",)]

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
        monkeypatch.setenv("AUDIT_HMAC_KEY", key)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)

        assert [error["loc"] for error in excinfo.value.errors()] == [("audit_hmac_key",)]
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
        monkeypatch.setenv("AUDIT_HMAC_KEY", key)

        settings = Settings(_env_file=None)

        assert settings.audit_hmac_key.get_secret_value() == key
        if len(key) > 1:
            for rendered in (repr(settings), str(settings), repr(settings.model_dump())):
                assert key not in rendered

    # A rejected setting next to a valid key does not echo the key either.
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("AUDIT_HMAC_KEY", thirty_two)
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
