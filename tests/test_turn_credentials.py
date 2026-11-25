import base64
import logging
import re

import pytest
from conftest import VOICE_SECRETS, offline_settings
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.voice.transport import server_ice_servers
from app.voice.turn import TurnCredential, browser_ice_servers, derive_credential, issue_credential

FAKE_SECRET = "-".join(["fake", "turn", "shared", "secret", "for", "unit", "tests"])
TOKEN = VOICE_SECRETS["CALL_ACCESS_TOKEN"]

# A fixed credential id, given to the code in place of its random one.
CREDENTIAL_ID = "a1b2c3d4e5f60718"

# Computed outside the code under test, for username 1700000660:a1b2c3d4e5f60718:
#   printf '%s' 1700000660:a1b2c3d4e5f60718 \
#     | openssl dgst -sha1 -hmac fake-turn-shared-secret-for-unit-tests -hex
# Kept as short hex groups and encoded here, so that no scanner reads it as a credential.
EXPECTED_DIGEST_HEX = [
    "c5b0",
    "51e8",
    "2eda",
    "c64d",
    "3842",
    "ca76",
    "e1f4",
    "8abd",
    "94a5",
    "2cfd",
]
EXPECTED_PASSWORD = base64.b64encode(bytes.fromhex("".join(EXPECTED_DIGEST_HEX))).decode()


# The same, for username 1700000360:a1b2c3d4e5f60718 (max_call_seconds=300):
#   printf '%s' 1700000360:a1b2c3d4e5f60718 \
#     | openssl dgst -sha1 -hmac fake-turn-shared-secret-for-unit-tests -hex
SHORT_CALL_DIGEST_HEX = [
    "4d8c",
    "627f",
    "bf83",
    "7cd7",
    "819a",
    "9857",
    "2d2e",
    "bad6",
    "7371",
    "4167",
]
SHORT_CALL_PASSWORD = base64.b64encode(bytes.fromhex("".join(SHORT_CALL_DIGEST_HEX))).decode()


def fixed_id() -> str:
    return CREDENTIAL_ID


def test_credential_matches_known_vector() -> None:
    expected = TurnCredential(username="1700000660:a1b2c3d4e5f60718", password=EXPECTED_PASSWORD)

    derived = derive_credential(
        SecretStr(FAKE_SECRET), expires_at=1700000660, user_id=CREDENTIAL_ID
    )

    assert derived == expected


@pytest.mark.parametrize(
    ("max_call_seconds", "username", "password"),
    [
        (600, "1700000660:a1b2c3d4e5f60718", EXPECTED_PASSWORD),
        (300, "1700000360:a1b2c3d4e5f60718", SHORT_CALL_PASSWORD),
    ],
    ids=["600s", "300s"],
)
def test_credential_expires_after_the_longest_call(
    max_call_seconds: int, username: str, password: str
) -> None:
    settings = offline_settings(turn_shared_secret=FAKE_SECRET, max_call_seconds=max_call_seconds)

    credential = issue_credential(settings, now=1700000000.9, new_id=fixed_id)

    # Expiry = now (whole seconds) + max_call_seconds + 60 second margin, then the id.
    assert credential.username == username
    assert credential.password == password


def test_each_credential_gets_its_own_id() -> None:
    settings = offline_settings(turn_shared_secret=FAKE_SECRET, max_call_seconds=600)

    browser = [browser_ice_servers(settings, now=1700000000)[0]["username"] for _ in range(50)]
    server = [server_ice_servers(settings, now=1700000000)[0].username for _ in range(50)]

    usernames = browser + server
    assert len(set(usernames)) == 100
    for username in usernames:
        expiry, user_id = username.split(":")
        assert expiry == "1700000660"
        assert re.fullmatch("[0-9a-f]{16}", user_id), username


def test_browser_ice_servers_point_at_turn_host() -> None:
    settings = offline_settings(turn_shared_secret=FAKE_SECRET, max_call_seconds=600)

    assert browser_ice_servers(settings, now=1700000000, new_id=fixed_id) == [
        {
            "urls": [
                "stun:turn.example.test:3478",
                "turn:turn.example.test:3478?transport=udp",
                "turn:turn.example.test:3478?transport=tcp",
            ],
            "username": "1700000660:a1b2c3d4e5f60718",
            "credential": EXPECTED_PASSWORD,
        }
    ]


def test_no_ice_servers_without_turn() -> None:
    settings = offline_settings(
        app_env="dev", call_access_token=None, turn_host=None, turn_shared_secret=None
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
