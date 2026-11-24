"""The admin API: read-only views of the requests, the messages, the calls and the audit log,
behind HTTP Basic sign-in. app/console/service.py mounts it at PREFIX, beside the console, only
when ADMIN_PASSWORD is set.

Every successful read appends one audit event naming the admin, in the transaction that read
the rows, and the rows leave only once that transaction has committed. A call that is not
there, or an id that is not a call id, is answered as an unknown path is, and not audited. The
list of calls carries no transcript; only the read of one call does. Every text value from the
database passes `mask` on its way out, transcripts included, and a callback number shows only
its last four digits, whatever its shape (app/api/reads.py, which the console reads with too).
Every response carries `Cache-Control: no-store`, errors included. One verification of the
chain runs at a time, across the API and the console.
"""

import hashlib
import hmac
import logging
from base64 import b64decode
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.reads import (
    CALL_COLUMNS,
    MESSAGE_COLUMNS,
    REQUEST_COLUMNS,
    AuditPage,
    Page,
    call,
    masked,
    newest,
    shown_callback,
)
from app.audit.log import (
    ADMIN_PREFIX,
    CALL_ID,
    Action,
    AuditLog,
    Event,
    Outcome,
    ResourceType,
    Verification,
)
from app.audit.read import recent_events
from app.config import Settings
from app.db import models
from app.guard.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

PREFIX = "/admin/api"
# A Basic header for a 32-character user name and a long password is well under this. A longer
# header is refused before it is decoded or hashed.
MAX_AUTHORIZATION_LENGTH = 1024
SIGN_IN_FAILURES = 5
SIGN_IN_WINDOW_SECONDS = 60
MAX_TRACKED_ADDRESSES = 10_000
CHALLENGE = 'Basic realm="admin", charset="UTF-8"'
# A verification asked for while another runs is refused; this is when to ask again.
VERIFY_RETRY_SECONDS = 5
# The same body as a path that does not exist.
NOT_FOUND = "Not Found"

# Where the reads run, and a read: one of app/api/reads.py's functions given all but its
# session. None means it found nothing.
type SessionFactory = async_sessionmaker[AsyncSession]
type Reader[T] = Callable[[AsyncSession], Awaitable[T | None]]


class NoQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerificationRunningError(Exception):
    """Another verification of the chain is running."""


def _supplied_credentials(header: str | None) -> tuple[bytes, bytes] | None:
    """The user name and password of a Basic header, as bytes; None for anything else."""
    if header is None or len(header) > MAX_AUTHORIZATION_LENGTH:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = b64decode(encoded, validate=True)
    except ValueError:
        return None
    user, colon, password = decoded.partition(b":")
    if not colon:
        return None
    return user, password


def _credentials_match(
    user: bytes, password: bytes, expected_user: bytes, expected_password: bytes
) -> bool:
    """Both halves, always: each compared as a SHA-256 digest in constant time, so neither the
    content nor the length of either shows in the time taken, and combined without
    short-circuiting."""
    user_matches = hmac.compare_digest(hashlib.sha256(user).digest(), expected_user)
    password_matches = hmac.compare_digest(hashlib.sha256(password).digest(), expected_password)
    return user_matches & password_matches


def _masked(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as the API shows it: masked (app/api/reads.py), instants as ISO 8601 text."""
    return {
        name: value.isoformat() if isinstance(value, datetime) else value
        for name, value in masked(row).items()
    }


async def _audited[T](
    sessions: SessionFactory,
    audit: AuditLog,
    event: Event,
    read: Reader[T],
) -> T | None:
    """The read and its audit event in one transaction; nothing is returned unless both
    committed. A read that finds nothing (None) writes no event."""
    async with sessions() as session:
        result = await read(session)
        if result is not None:
            await audit.append(session, event, now=datetime.now(UTC))
            await session.commit()
    return result


async def _nothing_read(_session: AsyncSession) -> bool:
    """For an event that records an act and reads no row: the admin signing in or out."""
    return True


class AdminAccess:
    """What the admin API and the console share, built once per application: the admin's
    sign-in check with its failed-sign-in limiter, the reads with their audit events, and the
    one verification of the chain that may run at a time."""

    def __init__(self, settings: Settings, sessions: SessionFactory, audit: AuditLog) -> None:
        if settings.admin_user is None or settings.admin_password is None:
            raise ValueError("the admin's pages need admin_user and admin_password")
        self.user = settings.admin_user
        self.actor = f"{ADMIN_PREFIX}{settings.admin_user}"
        self._expected_user = hashlib.sha256(settings.admin_user.encode("utf-8")).digest()
        self._expected_password = hashlib.sha256(
            settings.admin_password.get_secret_value().encode("utf-8")
        ).digest()
        self._limiter = RateLimiter(
            limit=SIGN_IN_FAILURES,
            window_seconds=SIGN_IN_WINDOW_SECONDS,
            max_addresses=MAX_TRACKED_ADDRESSES,
        )
        self._sessions = sessions
        self._audit = audit
        self._verifying = False

    def limited(self, address: str) -> bool:
        """Whether the address has failed too often to be let try again yet."""
        if self._limiter.blocked(address):
            logger.warning("admin_sign_in_limited")
            return True
        return False

    def signs_in(self, address: str, credentials: tuple[bytes, bytes] | None) -> bool:
        """Whether the user name and password are the admin's. Anything else, missing
        credentials included, counts as a failure of the address and is logged by its count."""
        if credentials is not None and _credentials_match(
            *credentials, self._expected_user, self._expected_password
        ):
            return True
        failures = self._limiter.record_failure(address)
        logger.warning("admin_sign_in_failed failures=%d", failures)
        return False

    def _event(self, action: Action, resource_type: ResourceType, resource_id: str | None) -> Event:
        return Event(
            actor=self.actor,
            action=action,
            resource_type=resource_type,
            outcome=Outcome.SUCCESS,
            resource_id=resource_id,
        )

    async def read[T](
        self,
        action: Action,
        resource_type: ResourceType,
        read: Reader[T],
        resource_id: str | None = None,
    ) -> T | None:
        """The read with its audit event, in one transaction; None, and no event, when it
        found nothing. Any failure is raised, and nothing was read."""
        return await _audited(
            self._sessions, self._audit, self._event(action, resource_type, resource_id), read
        )

    async def record(self, action: Action) -> None:
        """An act of the admin on the console that reads nothing, audited."""
        await _audited(
            self._sessions,
            self._audit,
            self._event(action, ResourceType.CONSOLE, None),
            _nothing_read,
        )

    async def verify(self) -> Verification:
        """The whole chain, verified and audited. The whole chain is read, on the worker that
        may carry the calls: one run at a time, here and in the console, and another asked
        for meanwhile raises VerificationRunningError. The check and the claim have no await
        between them, so two requests cannot both pass."""
        if self._verifying:
            logger.warning("admin_verify_busy")
            raise VerificationRunningError
        self._verifying = True
        try:
            verification = await self.read(
                Action.ADMIN_VERIFY_AUDIT, ResourceType.AUDIT_EVENT, self._audit.verify
            )
        finally:
            self._verifying = False
        if verification is None:
            raise RuntimeError("a verification always reports")
        return verification


class _NoStore:
    """Sets `Cache-Control: no-store` on every response of the app it wraps, whatever made
    the response: a route, an error handler or the server-error fallback."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self._app(scope, receive, send_no_store)


async def _invalid_query(_request: Request, _error: Exception) -> JSONResponse:
    # A fixed body: FastAPI's own would echo the value it refused.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": "Invalid query"}
    )


def _one_value_each(request: Request) -> None:
    if len(request.query_params.multi_items()) != len(request.query_params):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid query")


def build_admin_app(access: AdminAccess) -> ASGIApp:
    async def require_admin(request: Request) -> None:
        address = request.client.host if request.client is not None else ""
        # A limited address is refused before its credentials are looked at.
        if access.limited(address):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed sign-ins",
                headers={"Retry-After": str(SIGN_IN_WINDOW_SECONDS)},
            )
        supplied = _supplied_credentials(request.headers.get("authorization"))
        if access.signs_in(address, supplied):
            return
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Sign-in required",
            headers={"WWW-Authenticate": CHALLENGE},
        )

    async def read_and_audit[T](
        action: Action,
        resource_type: ResourceType,
        read: Reader[T],
        resource_id: str | None = None,
    ) -> T:
        """The read and its audit event (AdminAccess.read); a read that finds nothing is
        answered 404, and any failure 503."""
        try:
            result = await access.read(action, resource_type, read, resource_id)
        except Exception as error:
            logger.error("admin_read_failed action=%s error=%s", action.value, type(error).__name__)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
            ) from None
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        logger.info("admin_read action=%s", action.value)
        return result

    app = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(require_admin), Depends(_one_value_each)],
        exception_handlers={RequestValidationError: _invalid_query},
    )

    @app.get("/requests")
    async def read_requests(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_REQUESTS,
            ResourceType.REQUEST,
            lambda session: newest(session, REQUEST_COLUMNS, models.Request.id, page),
        )
        return {"requests": [_masked(row) for row in rows]}

    @app.get("/messages")
    async def read_messages(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_MESSAGES,
            ResourceType.MESSAGE,
            lambda session: newest(session, MESSAGE_COLUMNS, models.Message.id, page),
        )
        return {
            "messages": [
                {**_masked(row), "callback_number": shown_callback(row["callback_number"])}
                for row in rows
            ]
        }

    @app.get("/calls")
    async def read_calls(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_CALLS,
            ResourceType.CALL,
            lambda session: newest(session, CALL_COLUMNS, models.CallSession.id, page),
        )
        return {"calls": [_masked(row) for row in rows]}

    @app.get("/calls/{call_id}")
    async def read_call(call_id: str, _query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        if not CALL_ID.fullmatch(call_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        row = await read_and_audit(
            Action.ADMIN_READ_CALL,
            ResourceType.CALL,
            lambda session: call(session, call_id),
            resource_id=call_id,
        )
        return {"call": _masked(row)}

    @app.get("/audit")
    async def read_audit(page: Annotated[AuditPage, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_AUDIT,
            ResourceType.AUDIT_EVENT,
            lambda session: recent_events(
                session,
                limit=page.limit,
                before=page.before,
                call_id=page.call_id,
                action=page.action,
            ),
        )
        return {"events": [_masked(row) for row in rows]}

    @app.get("/audit/verify")
    async def verify_audit(_query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        try:
            verification = await access.verify()
        except VerificationRunningError:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="A verification is already running",
                headers={"Retry-After": str(VERIFY_RETRY_SECONDS)},
            ) from None
        except Exception as error:
            logger.error(
                "admin_read_failed action=%s error=%s",
                Action.ADMIN_VERIFY_AUDIT.value,
                type(error).__name__,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
            ) from None
        logger.info("admin_read action=%s", Action.ADMIN_VERIFY_AUDIT.value)
        return {
            "ok": verification.ok,
            "checked": verification.checked,
            "breaks": [
                _masked({"seq": found.seq, "kind": found.kind.value})
                for found in verification.breaks
            ],
            "truncated": verification.truncated,
        }

    return _NoStore(app)
