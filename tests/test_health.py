import importlib
import logging
import sys
from pathlib import Path

import pytest
from conftest import VOICE_SECRETS
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main
from app.config import AppEnv, LogLevel, Settings
from app.main import create_app

DOCS_PATHS = ["/docs", "/redoc", "/openapi.json"]


def make_settings(app_env: AppEnv, log_level: LogLevel = "INFO") -> Settings:
    if app_env == "dev":
        return Settings(_env_file=None, app_env=app_env, log_level=log_level)
    return Settings(
        _env_file=None,
        app_env=app_env,
        log_level=log_level,
        demo_access_token=VOICE_SECRETS["DEMO_ACCESS_TOKEN"],
        turn_host=VOICE_SECRETS["TURN_HOST"],
        turn_shared_secret=VOICE_SECRETS["TURN_SHARED_SECRET"],
    )


def make_client(app_env: AppEnv) -> TestClient:
    return TestClient(create_app(make_settings(app_env)))


def test_health_returns_only_status() -> None:
    response = make_client("prod").get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_served_in_dev(path: str) -> None:
    assert make_client("dev").get(path).status_code == 200


@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_hidden_outside_dev(app_env: AppEnv, path: str) -> None:
    assert make_client(app_env).get(path).status_code == 404


@pytest.mark.parametrize(
    ("log_level", "expected"),
    [("DEBUG", logging.DEBUG), ("ERROR", logging.ERROR)],
)
def test_log_level_is_applied(log_level: LogLevel, expected: int) -> None:
    root = logging.getLogger()
    original = root.level
    try:
        create_app(make_settings("prod", log_level))
        assert root.level == expected
    finally:
        root.setLevel(original)


@pytest.mark.usefixtures("clean_env")
def test_import_does_not_read_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_ENVV=prod\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # Import app.main and everything it imports afresh. The originals are put back after
    # the test, both in sys.modules and as attributes of the app package.
    for name in ("main", "config"):
        monkeypatch.setattr(app, name, getattr(app, name))
        monkeypatch.delitem(sys.modules, f"app.{name}")

    module = importlib.import_module("app.main")

    assert "app" not in vars(module)
    with pytest.raises(ValidationError, match="app_envv"):
        module.create_app()


@pytest.mark.usefixtures("voice_env")
def test_factory_reads_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "prod")

    client = TestClient(create_app())

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 404
