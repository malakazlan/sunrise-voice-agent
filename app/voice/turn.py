"""Short-lived TURN credentials for coturn's shared-secret mode (use-auth-secret)."""

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from app.config import Settings

TURN_PORT = 3478
# Covers the time between the page fetching credentials and the call starting.
CREDENTIAL_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class TurnCredential:
    username: str
    password: str = field(repr=False)


def derive_credential(shared_secret: SecretStr, expires_at: int) -> TurnCredential:
    """coturn's scheme: the username is the expiry time, the password its HMAC-SHA1."""
    username = str(expires_at)
    digest = hmac.new(
        shared_secret.get_secret_value().encode(), username.encode(), hashlib.sha1
    ).digest()
    return TurnCredential(username=username, password=base64.b64encode(digest).decode())


def issue_credential(settings: Settings, now: float) -> TurnCredential:
    if settings.turn_shared_secret is None:
        raise ValueError("TURN is not configured")
    expires_at = int(now) + settings.max_call_seconds + CREDENTIAL_MARGIN_SECONDS
    return derive_credential(settings.turn_shared_secret, expires_at)


def turn_urls(turn_host: str) -> list[str]:
    return [
        f"turn:{turn_host}:{TURN_PORT}?transport=udp",
        f"turn:{turn_host}:{TURN_PORT}?transport=tcp",
    ]


def browser_ice_servers(settings: Settings, now: float) -> list[dict[str, Any]]:
    """The RTCPeerConnection iceServers list for the call page."""
    if settings.turn_host is None:
        return []
    credential = issue_credential(settings, now)
    return [
        {
            "urls": [f"stun:{settings.turn_host}:{TURN_PORT}", *turn_urls(settings.turn_host)],
            "username": credential.username,
            "credential": credential.password,
        }
    ]
