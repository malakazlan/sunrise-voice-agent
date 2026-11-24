"""The key an identity claim is counted under, across calls.

`claim_key` is HMAC-SHA256 under `Settings.identity_hash_key` over the claimed name's
`name_key` and the ISO date of birth, joined by ":" (a name key is letters only, so the join
is unambiguous). Every spacing and case of one name gives one key; without the secret the key
says nothing about the name or the date. Neither is stored anywhere else.
"""

import hashlib
import hmac
from datetime import date

from pydantic import SecretStr

from app.domain.matching import name_key


class ClaimKeys:
    """Hashes claims under one key. Build it once from `Settings.identity_hash_key`."""

    __slots__ = ("_key",)

    def __init__(self, key: SecretStr) -> None:
        secret = key.get_secret_value()
        if not secret:
            raise ValueError("the identity hash key must not be empty")
        self._key = secret.encode("utf-8")

    def key_for(self, full_name: str, dob: date) -> str:
        """The claim's key, as lowercase hex. A full name that is not a name has none."""
        letters = name_key(full_name)
        if letters is None:
            raise ValueError("full_name is not a name")
        message = f"{letters}:{dob.isoformat()}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()
