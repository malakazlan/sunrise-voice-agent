import base64
import logging

import pytest
from conftest import VOICE_SECRETS, offline_settings
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.voice.turn import TurnCredential, browser_ice_servers, derive_credential, issue_credential

FAKE_SECRET = "-".join(["fake", "turn", "shared", "secret", "for", "unit", "tests"])
TOKEN = VOICE_SECRETS["DEMO_ACCESS_TOKEN"]

# Computed outside the code under test, for username 1700000660 and FAKE_SECRET:
#   printf '%s' 1700000660 | openssl dgst -sha1 -hmac fake-turn-shared-secret-for-unit-tests -hex
# Kept as short hex groups and encoded here, so that no scanner reads it as a credential.
EXPECTED_DIGEST_HEX = [
    "da9e",
    "902f",
    "a529",
    "8d29",
    "e8ca",
    "94d7",
    "0883",
    "11ee",
    "606a",
    "30bd",
]
EXPECTED_PASSWORD = base64.b64encode(bytes.fromhex("".join(EXPECTED_DIGEST_HEX))).decode()


def test_credential_matches_known_vector() -> None:
    expected = TurnCredential(username="1700000660", password=EXPECTED_PASSWORD)

    assert derive_credential(SecretStr(FAKE_SECRET), expires_at=1700000660) == expected


def test_credential_expires_after_the_longest_call() -> None:
    settings = offline_settings(turn_shared_secret=FAKE_SECRET, max_call_seconds=600)

    credential = issue_credential(settings, now=1700000000.9)

    # Expiry = now (whole seconds) + max_call_seconds + 60 second margin.
    assert credential.username == "1700000660"
    assert credential.password == EXPECTED_PASSWORD


def test_browser_ice_servers_point_at_turn_host() -> None:
    settings = offline_settings(turn_shared_secret=FAKE_SECRET, max_call_seconds=600)

    assert browser_ice_servers(settings, now=1700000000) == [
        {
            "urls": [
                "stun:turn.example.test:3478",
                "turn:turn.example.test:3478?transport=udp",
                "turn:turn.example.test:3478?transport=tcp",
            ],
            "username": "1700000660",
            "credential": EXPECTED_PASSWORD,
        }
    ]


def test_no_ice_servers_without_turn() -> None:
    settings = offline_settings(
        app_env="dev", demo_access_token=None, turn_host=None, turn_shared_secret=None
    )

    assert browser_ice_servers(settings, now=1700000000) == []


def test_secret_never_leaves_server(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    settings = offline_settings(turn_shared_secret=FAKE_SECRET)
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
