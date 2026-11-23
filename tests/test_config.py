from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.mark.usefixtures("clean_env")
def test_defaults_apply_without_env_file() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "dev"
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("clean_env")
def test_unknown_app_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError, match="app_env"):
        Settings(_env_file=None)


@pytest.mark.usefixtures("clean_env")
def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "TRACE")

    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None)


@pytest.mark.usefixtures("clean_env")
def test_unknown_key_in_env_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\nAPP_ENVV=prod\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError, match="app_envv") as excinfo:
        Settings()

    assert [error["type"] for error in excinfo.value.errors()] == ["extra_forbidden"]


@pytest.mark.usefixtures("clean_env")
def test_missing_env_file_gives_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.app_env == "dev"
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("clean_env")
def test_env_file_value_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENV=demo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().app_env == "demo"


@pytest.mark.usefixtures("clean_env")
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
def test_env_example_values_are_the_defaults() -> None:
    assert ENV_EXAMPLE.is_file()
    assert Settings(_env_file=ENV_EXAMPLE) == Settings(_env_file=None)


@pytest.mark.usefixtures("clean_env")
def test_rejection_does_not_echo_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "APP_ENVV=not-for-logs-1\nLOG_LEVEL=not-for-logs-2\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert excinfo.value.error_count() == 2
    assert "not-for-logs" not in str(excinfo.value)
