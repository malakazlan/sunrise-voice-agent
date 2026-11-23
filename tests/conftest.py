import os

import pytest

# Obviously fake values that let a demo or prod configuration validate.
VOICE_SECRETS = {
    "DEMO_ACCESS_TOKEN": "token-not-for-logs",
    "TURN_HOST": "turn.example.test",
    "TURN_SHARED_SECRET": "turn-secret-not-for-logs",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every environment variable for the duration of the test."""
    for key in list(os.environ):
        monkeypatch.delenv(key)


@pytest.fixture
def voice_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean environment holding only the voice secrets a demo or prod app needs."""
    for key, value in VOICE_SECRETS.items():
        monkeypatch.setenv(key, value)
