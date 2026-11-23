import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.voice.turn import TurnCredential, browser_ice_servers, derive_credential, issue_credential

FAKE_SECRET = "fake-turn-secret-for-tests"
TOKEN = "fake-access-token-for-tests"


def voice_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "demo",
        "demo_access_token": TOKEN,
        "turn_host": "turn.example.test",
        "turn_shared_secret": FAKE_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_credential_matches_known_vector() -> None:
    # Computed outside the code under test:
    #   printf '%s' 1700000660 \
    #     | openssl dgst -sha1 -hmac fake-turn-secret-for-tests -binary | base64
    expected = TurnCredential(username="1700000660", password="CsrEKteAi5eqjcV6SCLsuZnbgZk=")

    assert derive_credential(SecretStr(FAKE_SECRET), expires_at=1700000660) == expected


def test_credential_expires_after_the_longest_call() -> None:
    settings = voice_settings(max_call_seconds=600)

    credential = issue_credential(settings, now=1700000000.9)

    # Expiry = now (whole seconds) + max_call_seconds + 60 second margin.
    assert credential.username == "1700000660"
    assert credential.password == "CsrEKteAi5eqjcV6SCLsuZnbgZk="


def test_browser_ice_servers_point_at_turn_host() -> None:
    settings = voice_settings(max_call_seconds=600)

    assert browser_ice_servers(settings, now=1700000000) == [
        {
            "urls": [
                "stun:turn.example.test:3478",
                "turn:turn.example.test:3478?transport=udp",
                "turn:turn.example.test:3478?transport=tcp",
            ],
            "username": "1700000660",
            "credential": "CsrEKteAi5eqjcV6SCLsuZnbgZk=",
        }
    ]


def test_no_ice_servers_without_turn() -> None:
    settings = Settings(_env_file=None, app_env="dev")

    assert browser_ice_servers(settings, now=1700000000) == []


def test_secret_never_leaves_server(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    settings = voice_settings()
    auth = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(create_app(settings)) as client:
        answers = [
            client.get("/ice-servers", headers=auth),
            client.get("/ice-servers"),
            client.get("/ice-servers", headers={"Authorization": f"Bearer {FAKE_SECRET}"}),
            client.post("/offer", json={"sdp": "v=0", "type": "offer"}),
        ]

    assert answers[0].status_code == 200
    for response in answers:
        assert FAKE_SECRET not in response.text
        assert TOKEN not in response.text
        assert FAKE_SECRET not in str(response.headers)
    assert FAKE_SECRET not in caplog.text
    assert TOKEN not in caplog.text
    credential = issue_credential(settings, now=1700000000)
    assert credential.password not in repr(credential)
    assert FAKE_SECRET not in repr(credential)
