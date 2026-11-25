import importlib
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import close_unstarted, offline_settings
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main
from app.config import AppEnv, LogLevel, Settings
from app.main import create_app

DOCS_PATHS = ["/docs", "/redoc", "/openapi.json"]


def health_settings(app_env: AppEnv, log_level: LogLevel = "INFO") -> Settings:
    # The health check never connects, so the database values only have to be present.
    return offline_settings(app_env=app_env, log_level=log_level)


@contextmanager
def make_client(app_env: AppEnv) -> Iterator[TestClient]:
    """A client of an app that is never started; what it holds is closed after."""
    app = create_app(health_settings(app_env))
    try:
        yield TestClient(app)
    finally:
        close_unstarted(app)


def test_health_returns_only_status() -> None:
    with make_client("prod") as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_served_in_dev(path: str) -> None:
    with make_client("dev") as client:
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("app_env", ["demo", "prod"])
@pytest.mark.parametrize("path", DOCS_PATHS)
def test_docs_hidden_outside_dev(app_env: AppEnv, path: str) -> None:
    with make_client(app_env) as client:
        assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    ("log_level", "expected"),
    [("DEBUG", logging.DEBUG), ("ERROR", logging.ERROR)],
)
def test_log_level_is_applied(log_level: LogLevel, expected: int) -> None:
    root = logging.getLogger()
    original = root.level
    try:
        close_unstarted(create_app(health_settings("prod", log_level)))
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


@pytest.mark.usefixtures("offline_env")
def test_factory_reads_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "prod")

    app = create_app()
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 404
    close_unstarted(app)
