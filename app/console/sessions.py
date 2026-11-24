"""The console's sessions, held in memory: a restart signs everyone out.

A session id is 32 random bytes, URL-safe; the browser holds it in a cookie and the table holds
only its SHA-256 digest, so neither a lookup nor a dump of the table handles the id itself. A
session ends 30 minutes after it was last used or 8 hours after it began, whichever is first.
At most MAX_SESSIONS are kept; when a new one comes and there is no room, the one used longest
ago ends. One process, one event loop: the methods run without awaiting, so no lock is needed.
"""

import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 32
ID_BYTES = 32


def _digest(session_id: str) -> bytes:
    return hashlib.sha256(session_id.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class ChainCheck:
    """What the admin's last check of the chain found: `intact`, `broken` (with the first
    break), `running` (another check was), or `failed` (it could not run)."""

    outcome: Literal["intact", "broken", "running", "failed"]
    at: datetime
    checked: int = 0
    breaks: int = 0
    first_seq: int | None = None
    first_kind: str | None = None
    truncated: bool = False


@dataclass(slots=True)
class Session:
    """One signed-in admin. `token` is the session's anti-forgery token; `chain_check` what
    the admin's last check of the chain found, which the audit page shows."""

    user: str
    token: str
    started: float
    last_used: float
    chain_check: ChainCheck | None = None


class SessionTable:
    def __init__(
        self, *, max_sessions: int = MAX_SESSIONS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self._max = max_sessions
        self._clock = clock
        # Digest of the id -> session; the one used longest ago first.
        self._sessions: OrderedDict[bytes, Session] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    def open(self, user: str) -> tuple[str, Session]:
        """A new session for the user, and its id."""
        now = self._clock()
        self._drop_expired(now)
        while len(self._sessions) >= self._max:
            self._sessions.popitem(last=False)
        session_id = secrets.token_urlsafe(ID_BYTES)
        session = Session(
            user=user, token=secrets.token_urlsafe(ID_BYTES), started=now, last_used=now
        )
        self._sessions[_digest(session_id)] = session
        return session_id, session

    def find(self, session_id: str | None) -> Session | None:
        """The live session with this id, now marked used; None for any other id."""
        if not session_id:
            return None
        key = _digest(session_id)
        session = self._sessions.get(key)
        if session is None:
            return None
        now = self._clock()
        if self._expired(session, now):
            del self._sessions[key]
            return None
        session.last_used = now
        self._sessions.move_to_end(key)
        return session

    def close(self, session_id: str) -> None:
        self._sessions.pop(_digest(session_id), None)

    def _expired(self, session: Session, now: float) -> bool:
        return now - session.last_used >= IDLE_SECONDS or now - session.started >= ABSOLUTE_SECONDS

    def _drop_expired(self, now: float) -> None:
        for key in [key for key, session in self._sessions.items() if self._expired(session, now)]:
            del self._sessions[key]
