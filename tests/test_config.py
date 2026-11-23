import logging
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.mark.usefixtures("db_env")
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


@pytest.mark.usefixtures("db_env")
def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "TRACE")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("literal_error", ("log_level",))]


@pytest.mark.usefixtures("db_env")
def test_unknown_key_in_env_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nAPP_ENVV=prod\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="app_envv") as excinfo:
        Settings()

    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


@pytest.mark.usefixtures("db_env")
def test_missing_env_file_is_fine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "demo")

    settings = Settings()

    assert settings.app_env == "demo"
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("db_env")
def test_env_file_value_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().log_level == "DEBUG"


@pytest.mark.usefixtures("db_env")
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

    assert keys == {name.upper() for name in Settings.model_fields}


@pytest.mark.usefixtures("clean_env")
def test_env_example_loads_and_carries_defaults() -> None:
    assert ENV_EXAMPLE.is_file()

    example = Settings(_env_file=ENV_EXAMPLE)

    for name, field in Settings.model_fields.items():
        if not field.is_required():
            assert getattr(example, name) == field.default, name


@pytest.mark.usefixtures("db_env")
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

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = sorted((error["type"], error["loc"]) for error in excinfo.value.errors())
    assert errors == [
        ("missing", ("postgres_db",)),
        ("missing", ("postgres_host",)),
        ("missing", ("postgres_password",)),
        ("missing", ("postgres_user",)),
    ]


@pytest.mark.usefixtures("db_env")
def test_database_url_is_built_from_the_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "clinic")
    monkeypatch.setenv("POSTGRES_USER", "clinic_app")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word/1")

    url = Settings(_env_file=None).database_url

    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database, url.username) == (
        "db.example",
        6543,
        "clinic",
        "clinic_app",
    )
    assert url.password == "p@ss:word/1"


def test_database_password_is_hidden(
    server_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # The real test server, reached with a password it does not accept. The environment is
    # kept as it is: libpq on Windows needs it to run the login handshake.
    password = "not-for-logs-db-password"

    def build(port: object) -> Settings:
        return Settings(
            _env_file=None,
            app_env="prod",
            postgres_host=server_settings.postgres_host,
            postgres_port=port,
            postgres_db=server_settings.postgres_db,
            postgres_user=server_settings.postgres_user,
            postgres_password=password,
        )

    settings = build(server_settings.postgres_port)
    url = settings.database_url

    assert settings.postgres_password.get_secret_value() == password
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
    engine = create_engine(url, poolclass=NullPool)
    with pytest.raises(OperationalError) as connect_error:
        engine.connect()

    assert "password authentication failed" in str(connect_error.value)
    assert password not in str(connect_error.value)
    assert password not in repr(connect_error.value)
    assert password not in repr(engine)
    assert password not in caplog.text

    # A working connection logs at debug level; the real password is not in those lines.
    real_password = server_settings.postgres_password.get_secret_value()
    working = create_engine(server_settings.database_url, poolclass=NullPool)
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
