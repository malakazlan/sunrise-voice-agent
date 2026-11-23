import logging

import pytest
from fastapi.testclient import TestClient

from app.config import AppEnv, LogLevel, Settings
from app.main import create_app

DOCS_PATHS = ["/docs", "/redoc", "/openapi.json"]


def make_client(app_env: AppEnv) -> TestClient:
    settings = Settings(_env_file=None, app_env=app_env, log_level="INFO")
    return TestClient(create_app(settings))


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
        create_app(Settings(_env_file=None, app_env="prod", log_level=log_level))
        assert root.level == expected
    finally:
        root.setLevel(original)
