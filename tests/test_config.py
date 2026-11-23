from pathlib import Path

import pytest
from conftest import VOICE_SECRETS
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.mark.usefixtures("voice_env")
def test_defaults_apply_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")

    settings = Settings(_env_file=None)

    assert settings.app_env == "prod"
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("clean_env")
def test_missing_app_env_is_rejected() -> None:
    with pytest.raises(ValidationError, match="app_env") as excinfo:
        Settings(_env_file=None)

    assert [error["type"] for error in excinfo.value.errors()] == ["missing"]


@pytest.mark.usefixtures("clean_env")
def test_unknown_app_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("literal_error", ("app_env",))]


@pytest.mark.usefixtures("voice_env")
def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "TRACE")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("literal_error", ("log_level",))]


@pytest.mark.usefixtures("voice_env")
def test_unknown_key_in_env_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nAPP_ENVV=prod\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="app_envv") as excinfo:
        Settings()

    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


@pytest.mark.usefixtures("voice_env")
def test_missing_env_file_is_fine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "demo")

    settings = Settings()

    assert settings.app_env == "demo"
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("voice_env")
def test_env_file_value_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().log_level == "DEBUG"


@pytest.mark.usefixtures("voice_env")
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


@pytest.mark.usefixtures("clean_env")
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


@pytest.mark.usefixtures("clean_env")
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


@pytest.mark.usefixtures("clean_env")
def test_app_will_not_start_without_voice_secrets_outside_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(ValidationError, match="demo_access_token"):
        create_app()


@pytest.mark.usefixtures("clean_env")
def test_dev_runs_without_voice_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")

    assert Settings(_env_file=None).app_env == "dev"


@pytest.mark.usefixtures("clean_env")
def test_turn_without_access_token_is_rejected_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("TURN_HOST", VOICE_SECRETS["TURN_HOST"])
    monkeypatch.setenv("TURN_SHARED_SECRET", VOICE_SECRETS["TURN_SHARED_SECRET"])

    with pytest.raises(ValidationError, match="demo_access_token") as excinfo:
        Settings(_env_file=None)

    assert excinfo.value.error_count() == 1


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize("present", ["TURN_HOST", "TURN_SHARED_SECRET"])
def test_turn_host_and_secret_come_together(monkeypatch: pytest.MonkeyPatch, present: str) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", VOICE_SECRETS["DEMO_ACCESS_TOKEN"])
    monkeypatch.setenv(present, VOICE_SECRETS[present])

    with pytest.raises(ValidationError, match="turn_host") as excinfo:
        Settings(_env_file=None)

    assert excinfo.value.error_count() == 1


@pytest.mark.usefixtures("clean_env")
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


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize("key", ["MAX_CONCURRENT_CALLS", "MAX_CALL_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_call_limits_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    errors = [(error["type"], error["loc"]) for error in excinfo.value.errors()]
    assert errors == [("greater_than", (key.lower(),))]


@pytest.mark.usefixtures("clean_env")
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


@pytest.mark.usefixtures("voice_env")
def test_rejection_does_not_echo_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "APP_ENV=demo\nAPP_ENVV=not-for-logs-1\nLOG_LEVEL=not-for-logs-2\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert excinfo.value.error_count() == 2
    assert "not-for-logs" not in str(excinfo.value)
