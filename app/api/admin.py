"""The admin API: read-only views of the requests, the messages, the calls and the audit log,
and the console's reads (app/audit/console.py, app/api/reads.py): the calls in progress and
their live stream, a call's turns and detail, the requests' queue, the appointments, the slots,
the summaries of the guardrails and the calls' numbers, and the config. app/api/service.py
mounts it at PREFIX only when ADMIN_PASSWORD is set. The calls in progress, the slots' default
day and the summaries' days follow the clinic's clock.

Three writes (app/api/writes.py): a request of the queue marked handled, a callback's details
shown, a config key changed. Each takes a browser session only, never Basic, with its
anti-forgery token and from the console's origin, and a JSON body of at most MAX_BODY_BYTES
with no field it does not know; each writes its audit event in the transaction of the change,
and nothing is changed or returned without it. The callback's details are the one answer with
values unmasked, at most REVEALS_PER_WINDOW times a session in REVEAL_WINDOW_SECONDS. A config
change reaches the calls within APPLIES_WITHIN_SECONDS while the database answers
(app/runtime_config.py).

Two ways to sign in, one check (AdminAccess): HTTP Basic, for scripts, which reads only; or a
browser session (POST /session, app/api/sessions.py), held in a cookie, whose state-changing
requests carry its anti-forgery token and come from the console's origin (app/api/security.py).
A failed sign-in of either kind counts against the address; the same limit refuses both. The
live stream takes a session only.

Every successful read appends one audit event naming the admin, in the transaction that read
the rows, and the rows leave only once that transaction has committed. A call that is not
there, or an id that is not a call id, is answered as an unknown path is, and not audited. The
list of calls carries no transcript; only the reads of one call do. Every text value from the
database passes `mask` on its way out, transcripts included, and a callback number shows only
its last four digits, whatever its shape. Every answer carries the admin headers, errors and
preflights included. One verification of the chain runs at a time.
"""

import hashlib
import hmac
import json
import logging
import re
import time
from base64 import b64decode
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

from app.api import writes
from app.api.live import LiveStream, LiveStreams
from app.api.reads import (
    DEFAULT_PAGE,
    MAX_ROW_ID,
    TODO,
    AuditPage,
    Page,
    QueueState,
    call,
    call_detail,
    calls,
    masked,
    messages,
    request_queue,
    requests,
    shown_mark,
)
from app.api.security import (
    CSRF_HEADER,
    AdminHeaders,
    ConsoleCors,
    from_console,
    tokens_match,
)
from app.api.sessions import MAX_SESSIONS, Session, SessionTable
from app.audit import console
from app.audit.log import (
    ADMIN_PREFIX,
    CALL_ID,
    ROW_ID,
    Action,
    AuditLog,
    Event,
    Outcome,
    Reason,
    ResourceType,
    Verification,
)
from app.audit.read import MAX_PAGE, recent_events
from app.config import SPEECH_TO_TEXT, TEXT_TO_SPEECH, Settings
from app.db import models
from app.domain.clinic import CLINIC_TZ
from app.domain.office_hours import SCHEDULE, clinic_now, office_open
from app.guard.rate_limit import RateLimiter
from app.runtime_config import (
    APPLIES_WITHIN_SECONDS,
    ReadOutage,
    Values,
    read_config,
    read_config_bounded,
    values_of,
)

logger = logging.getLogger(__name__)

PREFIX = "/admin/api"
# The browser session's cookie. Outside dev, over HTTPS, a __Host- cookie: Secure, Path=/ and
# no Domain, so a sibling host of the site can neither plant nor overwrite it. In dev, over
# plain http, where a __Host- cookie cannot be set, it is sent back to this API's paths only.
COOKIE = "admin_session"
HOST_COOKIE = "__Host-admin_session"
# A Basic header for a 32-character user name and a long password is well under this. A longer
# header is refused before it is decoded or hashed.
MAX_AUTHORIZATION_LENGTH = 1024
# A session sign-in's or a write's JSON body; a longer one is refused unread.
MAX_BODY_BYTES = 1024
JSON_TYPE = "application/json"
SIGN_IN_FAILURES = 5
SIGN_IN_WINDOW_SECONDS = 60
MAX_TRACKED_ADDRESSES = 10_000
CHALLENGE = 'Basic realm="admin", charset="UTF-8"'
# A verification asked for while another runs is refused; this is when to ask again.
VERIFY_RETRY_SECONDS = 5
# A session is shown at most this many callbacks' details in a window of this many seconds.
REVEALS_PER_WINDOW = 30
REVEAL_WINDOW_SECONDS = 60
# The session's answer reads the config in at most this long.
SESSION_READ_SECONDS = 0.5
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The days a summary or the slots cover: a week unless asked.
DEFAULT_DAYS = 7
MAX_SLOT_DAYS = 14
# The days the slots may be read from: a date far past them overflows the window's end.
FIRST_SLOT_DAY = date(2000, 1, 1)
LAST_SLOT_DAY = date(2100, 12, 31)
MAX_SUMMARY_DAYS = 30
# The same body as a path that does not exist.
NOT_FOUND = "Not Found"
SIGN_IN_REQUIRED = "Sign-in required"

# Where the reads run, and a read: one of the data functions given all but its session. None
# means it found nothing.
type SessionFactory = async_sessionmaker[AsyncSession]
type Reader[T] = Callable[[AsyncSession], Awaitable[T | None]]


class NoQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _a_request_type(value: str | None) -> str | None:
    if value is not None and value not in models.REQUEST_TYPES:
        raise ValueError("type is not a request type")
    return value


class CallPage(Page):
    """A page of calls, optionally of those that ended one known way."""

    end_reason: str | None = None

    @field_validator("end_reason")
    @classmethod
    def _known_reason(cls, value: str | None) -> str | None:
        if value is not None and value not in models.CALL_END_REASONS:
            raise ValueError("end_reason is not one a call ends for")
        return value


class RequestPage(Page):
    """A page of requests, optionally of one known type."""

    type: str | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str | None) -> str | None:
        return _a_request_type(value)


class QueueQuery(BaseModel):
    """A page of the queue: of the requests still to do unless asked for those handled or all,
    optionally of one known type, after the request a page before ended with."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    before: int | None = Field(default=None, ge=1, le=MAX_ROW_ID)
    state: QueueState = TODO

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str | None) -> str | None:
        return _a_request_type(value)


class EmptyBody(NoQuery):
    """The body of a write that takes nothing: `{}`."""


class ConfigBody(BaseModel):
    """A config change: a known key, one of its values, and the admin's confirmation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    key: str
    value: str
    confirm: bool

    @model_validator(mode="after")
    def _known_setting_confirmed(self) -> "ConfigBody":
        if self.value not in models.CONFIG_VALUES.get(self.key, ()):
            raise ValueError("not a known key with one of its values")
        if self.confirm is not True:
            raise ValueError("a change needs confirm: true")
        return self


class TurnPage(BaseModel):
    """A page of one call's turns, in order, after a turn number."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    after: int | None = Field(default=None, ge=1, le=MAX_ROW_ID)


class SlotQuery(BaseModel):
    """The slots of 1 to MAX_SLOT_DAYS days from a date at the clinic (YYYY-MM-DD, from
    FIRST_SLOT_DAY to LAST_SLOT_DAY; today if not given)."""

    model_config = ConfigDict(extra="forbid")

    start: date | None = Field(default=None, alias="from", ge=FIRST_SLOT_DAY, le=LAST_SLOT_DAY)
    days: int = Field(default=DEFAULT_DAYS, ge=1, le=MAX_SLOT_DAYS)

    @field_validator("start", mode="before")
    @classmethod
    def _a_date(cls, value: object) -> object:
        if value is not None and not (isinstance(value, str) and ISO_DATE.fullmatch(value)):
            raise ValueError("from must be a date: YYYY-MM-DD")
        return value


class SummaryQuery(BaseModel):
    """A summary of the last 1 to MAX_SUMMARY_DAYS days at the clinic, today included."""

    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=DEFAULT_DAYS, ge=1, le=MAX_SUMMARY_DAYS)


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


def _signed_credentials(body: bytes) -> tuple[bytes, bytes] | None:
    """The user name and password of a session sign-in: a JSON object of exactly `user` and
    `password`, both text; None for anything else."""
    try:
        sent = json.loads(body)
        if not (isinstance(sent, dict) and sent.keys() == {"user", "password"}):
            return None
        user, password = sent["user"], sent["password"]
        if not (isinstance(user, str) and isinstance(password, str)):
            return None
        return user.encode("utf-8"), password.encode("utf-8")
    except (ValueError, RecursionError):
        return None


def _credentials_match(
    user: bytes, password: bytes, expected_user: bytes, expected_password: bytes
) -> bool:
    """Both halves, always: each compared as a SHA-256 digest in constant time, so neither the
    content nor the length of either shows in the time taken, and combined without
    short-circuiting."""
    user_matches = hmac.compare_digest(hashlib.sha256(user).digest(), expected_user)
    password_matches = hmac.compare_digest(hashlib.sha256(password).digest(), expected_password)
    return user_matches & password_matches


def _json(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row a data function has masked, with its instants as ISO 8601 text."""
    return {
        name: value.isoformat() if isinstance(value, datetime) else value
        for name, value in row.items()
    }


def _masked(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as the API shows it: masked (app/api/reads.py), instants as ISO 8601 text."""
    return _json(masked(row))


async def _audited[T](
    sessions: SessionFactory,
    audit: AuditLog,
    event: Callable[[T], Event],
    read: Reader[T],
) -> T | None:
    """The read or the write and its audit event, made from what it found, in one transaction;
    nothing is returned unless both committed. One that finds nothing (None) writes no
    event."""
    async with sessions() as session:
        result = await read(session)
        if result is not None:
            await audit.append(session, event(result), now=datetime.now(UTC))
            await session.commit()
    return result


async def _nothing_read(_session: AsyncSession) -> bool:
    """For an event that records an act and reads no row: the admin signing in or out."""
    return True


class AdminAccess:
    """The admin's one way in, built once per application: the sign-in check with its
    failed-sign-in limiter, the browser sessions, the reads with their audit events, and the
    one verification of the chain that may run at a time. `clock` times the sessions."""

    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        audit: AuditLog,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if settings.admin_user is None or settings.admin_password is None:
            raise ValueError("the admin API needs admin_user and admin_password")
        self.user = settings.admin_user
        self.actor = f"{ADMIN_PREFIX}{settings.admin_user}"
        self.signed_in = SessionTable(clock=clock)
        self.secure = settings.app_env != "dev"
        self.cookie, self.cookie_path = (HOST_COOKIE, "/") if self.secure else (COOKIE, PREFIX)
        self._console_origin = settings.console_origin
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
        self._config_outage = ReadOutage("admin_config")
        # The callback details each session has been shown in the last REVEAL_WINDOW_SECONDS.
        self.reveals = RateLimiter(
            limit=REVEALS_PER_WINDOW,
            window_seconds=REVEAL_WINDOW_SECONDS,
            max_addresses=MAX_SESSIONS,
            clock=clock,
        )

    def limited(self, address: str) -> bool:
        """Whether the address has failed too often to be let try again yet."""
        if self._limiter.blocked(address):
            logger.warning("admin_sign_in_limited")
            return True
        return False

    def signs_in(self, address: str, credentials: tuple[bytes, bytes] | None) -> bool:
        """Whether the user name and password presented are the admin's. Anything else,
        credentials that cannot be read included, counts as a failure of the address and is
        logged by its count. A request that presents none is not asked here."""
        if credentials is not None and _credentials_match(
            *credentials, self._expected_user, self._expected_password
        ):
            return True
        failures = self._limiter.record_failure(address)
        logger.warning("admin_sign_in_failed failures=%d", failures)
        return False

    def browser_session(
        self, request: Request, *, touch: bool = True
    ) -> tuple[str, Session] | None:
        """The live session the request's cookie names, and its id; marked used unless `touch`
        is false. None when there is none, and for a request that does not come from the
        console's origin: its cookie is not honoured. With CONSOLE_ORIGIN set the request must
        name it; without, it names the API's own origin or none (a browser's same-origin
        read)."""
        bare = "origin" not in request.headers and self._console_origin is None
        if not (bare or from_console(request, self._console_origin)):
            return None
        session_id = request.cookies.get(self.cookie)
        session = self.signed_in.find(session_id, touch=touch)
        if session_id is None or session is None:
            return None
        return session_id, session

    def event(
        self,
        action: Action,
        resource_type: ResourceType,
        resource_id: str | None,
        *,
        outcome: Outcome = Outcome.SUCCESS,
        reason: Reason | None = None,
    ) -> Event:
        """An event of the admin's."""
        return Event(
            actor=self.actor,
            action=action,
            resource_type=resource_type,
            outcome=outcome,
            resource_id=resource_id,
            reason=reason,
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
        event = self.event(action, resource_type, resource_id)
        return await _audited(self._sessions, self._audit, lambda _found: event, read)

    async def write[T](self, write: Reader[T], event: Callable[[T], Event]) -> T | None:
        """The write with its audit event, made from what the write did, in one transaction;
        None, and no event, when it found nothing to write to. Any failure is raised, and
        nothing was written."""
        return await _audited(self._sessions, self._audit, event, write)

    async def config(self) -> Values | None:
        """Calls on or off and the office override as the table has them now, in one read of
        its own given up after SESSION_READ_SECONDS; None when it cannot be read. The first
        failure of an outage is logged, not every one."""
        try:
            found = await read_config_bounded(self._sessions, SESSION_READ_SECONDS)
        except Exception as error:
            self._config_outage.failed(error)
            return None
        self._config_outage.ended()
        return values_of(found)

    async def read_again[T](self, read: Reader[T]) -> T | None:
        """A read the audit event of an earlier read already covers (the live stream's ticks),
        in a transaction of its own."""
        async with self._sessions() as session:
            return await read(session)

    async def record(self, action: Action) -> None:
        """An act of the admin that reads nothing, audited: signing in or out."""
        event = self.event(action, ResourceType.SESSION, None)
        await _audited(self._sessions, self._audit, lambda _found: event, _nothing_read)

    async def verify(self) -> Verification:
        """The whole chain, verified and audited. The whole chain is read, on the worker that
        may carry the calls: one run at a time, and another asked for meanwhile raises
        VerificationRunningError. The check and the claim have no await between them, so two
        requests cannot both pass."""
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


async def _invalid_query(_request: Request, _error: Exception) -> JSONResponse:
    # A fixed body: FastAPI's own would echo the value it refused.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": "Invalid query"}
    )


async def _one_value_each(request: Request) -> None:
    # A coroutine: FastAPI would hand a plain function to its thread pool, whose new threads
    # hold the event loop while they start, the calls' loop too in APP_ROLE=all.
    if len(request.query_params.multi_items()) != len(request.query_params):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid query")


def _address(request: Request) -> str:
    return request.client.host if request.client is not None else ""


def _sign_in_required(request: Request, *, basic: bool) -> HTTPException:
    """401. The Basic prompt only where Basic opens the route and only for a request without an
    Origin header: a browser sends one, and must never show the prompt."""
    challenged = basic and "origin" not in request.headers
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail=SIGN_IN_REQUIRED,
        headers={"WWW-Authenticate": CHALLENGE} if challenged else None,
    )


def _forbidden() -> HTTPException:
    logger.warning("admin_forged_request")
    return HTTPException(status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _limited() -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many failed sign-ins",
        headers={"Retry-After": str(SIGN_IN_WINDOW_SECONDS)},
    )


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")


def _sends_json(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return content_type.split(";")[0].strip().lower() == JSON_TYPE


async def _bounded_body(request: Request) -> bytes | None:
    """The request's body; None when it is longer than MAX_BODY_BYTES, read no further."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def _too_large() -> HTTPException:
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="Request too large")


async def _write_body[Body: BaseModel](request: Request, model: type[Body]) -> Body:
    """A write's body: a JSON object of `model`'s fields and no other, within MAX_BODY_BYTES.
    Anything else is refused with a fixed answer that echoes nothing of it."""
    if not _sends_json(request):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Content-Type must be application/json"
        )
    body = await _bounded_body(request)
    if body is None:
        raise _too_large()
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid request"
        ) from None


def _queue_row(source: str, row_id: str) -> int:
    """The id of a row of the queue, from a path that names one of its sources and a row id of
    the declared shape; anything else is answered as an unknown path is."""
    if source not in models.QUEUE_SOURCES or not ROW_ID.fullmatch(row_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return int(row_id)


def build_admin_app(
    settings: Settings,
    access: AdminAccess,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]],
) -> ASGIApp:
    """The admin API. `clock` and `sleep` time the live stream; the sessions' clock is
    `access`'s."""
    streams = LiveStreams()

    async def require_admin(request: Request) -> None:
        """A browser session, or else HTTP Basic, as the read routes take them. Only
        credentials presented count against the address: a request with none is refused and
        not counted, so a page that makes a browser send such requests locks no one out."""
        if access.browser_session(request) is not None:
            return
        header = request.headers.get("authorization")
        if header is None:
            raise _sign_in_required(request, basic=True)
        address = _address(request)
        # A limited address is refused before its credentials are looked at.
        if access.limited(address):
            raise _limited()
        if access.signs_in(address, _supplied_credentials(header)):
            return
        raise _sign_in_required(request, basic=True)

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
            raise _unavailable() from None
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        logger.info("admin_read action=%s", action.value)
        return result

    def writing_session(request: Request) -> tuple[str, Session]:
        """The session a change of state is made in, and its id: a browser session only, never
        Basic, whose request carries the session's anti-forgery token and comes from the
        console's origin."""
        found = access.browser_session(request)
        if found is None:
            raise _sign_in_required(request, basic=False)
        token_matches = tokens_match(request.headers.get(CSRF_HEADER), found[1].token)
        if not (token_matches and from_console(request, settings.console_origin)):
            raise _forbidden()
        return found

    async def write_and_audit[T](
        action: Action, write: Reader[T], event: Callable[[T], Event]
    ) -> T:
        """The write and its audit event (AdminAccess.write); one that finds nothing to write
        to is answered 404, and any failure 503."""
        try:
            result = await access.write(write, event)
        except Exception as error:
            logger.error(
                "admin_write_failed action=%s error=%s", action.value, type(error).__name__
            )
            raise _unavailable() from None
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        logger.info("admin_write action=%s", action.value)
        return result

    async def shown_session(session: Session) -> dict[str, Any]:
        """What a signed-in browser is told: who, the session's anti-forgery token, the seconds
        left if it is not used, whether calls are on (None when the config cannot be read),
        and whether the office is open for a call starting now: the agent's clock, or the
        override staff set; the clock alone when the config cannot be read."""
        config = await access.config()
        office = SCHEDULE if config is None else config.office
        return {
            "user": session.user,
            "csrf_token": session.token,
            "expires_in": access.signed_in.seconds_left(session),
            "calls": None if config is None else config.calls,
            "office_open": office_open(clinic_now(settings), office),
            "time_zone": CLINIC_TZ.key,
        }

    async def live_calls(session: AsyncSession) -> list[dict[str, Any]]:
        return await console.live_calls(
            session, now=clinic_now(settings), max_call_seconds=settings.max_call_seconds
        )

    def live_json(rows: list[dict[str, Any]]) -> str:
        return json.dumps([_masked(row) for row in rows], separators=(",", ":"))

    app = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(_one_value_each)],
        exception_handlers={RequestValidationError: _invalid_query},
    )

    @app.post("/session")
    async def sign_in(request: Request, _query: Annotated[NoQuery, Query()]) -> JSONResponse:
        # No token yet: the console's origin and a JSON body keep a form on another page out.
        if not (from_console(request, settings.console_origin) and _sends_json(request)):
            raise _forbidden()
        address = _address(request)
        if access.limited(address):
            raise _limited()
        body = await _bounded_body(request)
        if body is None:
            raise _too_large()
        if not access.signs_in(address, _signed_credentials(body)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sign-in failed")
        # No event, no session.
        try:
            await access.record(Action.ADMIN_SIGNED_IN)
        except Exception as error:
            logger.error("admin_sign_in_unaudited error=%s", type(error).__name__)
            raise _unavailable() from None
        replaced = request.cookies.get(access.cookie)
        if replaced:
            access.signed_in.close(replaced)
        session_id, session = access.signed_in.open(access.user)
        logger.info("admin_signed_in")
        response = JSONResponse(await shown_session(session))
        response.set_cookie(
            access.cookie,
            session_id,
            path=access.cookie_path,
            secure=access.secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/session")
    async def read_session(request: Request, _query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        # Asking does not keep the session alive.
        found = access.browser_session(request, touch=False)
        if found is None:
            raise _sign_in_required(request, basic=False)
        return await shown_session(found[1])

    @app.delete("/session")
    async def sign_out(request: Request, _query: Annotated[NoQuery, Query()]) -> Response:
        session_id, _session = writing_session(request)
        # Signing out always signs out: the session ends whatever the audit log says, and an
        # event it could not take is missing from it, which the log says by name.
        access.signed_in.close(session_id)
        try:
            await access.record(Action.ADMIN_SIGNED_OUT)
        except Exception as error:
            logger.error("admin_sign_out_unaudited error=%s", type(error).__name__)
        logger.info("admin_signed_out")
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            access.cookie,
            path=access.cookie_path,
            secure=access.secure,
            httponly=True,
            samesite="strict",
        )
        return response

    # Before /calls/{call_id}, which would take "live" for a call id. A session only, never
    # Basic, and never kept alive by the stream.
    @app.get("/calls/live/stream")
    async def live_stream(request: Request, _query: Annotated[NoQuery, Query()]) -> Response:
        found = access.browser_session(request, touch=False)
        if found is None:
            raise _sign_in_required(request, basic=False)
        session_id = found[0]
        key = access.signed_in.key(session_id)
        stop = streams.open(key)
        if stop is None:
            logger.warning("admin_stream_refused")
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many live streams")

        async def read() -> str:
            return live_json(await access.read_again(live_calls) or [])

        # From the place taken to the answer returned, any failure gives the place back.
        try:
            first = await read_and_audit(
                Action.ADMIN_READ_LIVE_CALLS, ResourceType.CALL, live_calls
            )
            stream = LiveStream(
                live_json(first),
                read=read,
                signed_in=lambda: access.signed_in.find(session_id, touch=False) is not None,
                stop=stop,
                release=lambda: streams.close(key, stop),
                clock=clock,
                sleep=sleep,
            )
        except BaseException:
            streams.close(key, stop)
            raise
        logger.info("admin_stream_opened")
        return stream

    # The writes: a session only, with its token, from the console's origin; each with its
    # audit event in its transaction.
    @app.post("/queue/{source}/{row_id}/handled")
    async def mark_handled(
        request: Request, source: str, row_id: str, _query: Annotated[NoQuery, Query()]
    ) -> JSONResponse:
        _session_id, session = writing_session(request)
        row = _queue_row(source, row_id)
        await _write_body(request, EmptyBody)
        now = datetime.now(UTC)
        mark = await write_and_audit(
            Action.ADMIN_MARKED_HANDLED,
            lambda db: writes.mark_handled(db, source, row, session.user, now),
            lambda found: access.event(
                Action.ADMIN_MARKED_HANDLED,
                ResourceType.REQUEST,
                row_id,
                outcome=Outcome.SUCCESS if found.new else Outcome.ALREADY,
            ),
        )
        return JSONResponse(
            {"source": source, "id": row, "handled": shown_mark(mark.by, mark.at)},
            status_code=status.HTTP_201_CREATED if mark.new else status.HTTP_200_OK,
        )

    # The one route that answers with values unmasked: the caller's name and number, for one
    # row, read and audited in one transaction, and logged by event name only.
    @app.post("/queue/{source}/{row_id}/callback")
    async def reveal_callback(
        request: Request, source: str, row_id: str, _query: Annotated[NoQuery, Query()]
    ) -> dict[str, Any]:
        session_id, _session = writing_session(request)
        # Each session's reveals are counted by the digest that names it, never its id.
        counted = access.signed_in.key(session_id).hex()
        if access.reveals.blocked(counted):
            logger.warning("admin_reveal_limited")
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={"Retry-After": str(REVEAL_WINDOW_SECONDS)},
            )
        access.reveals.record_failure(counted)
        row = _queue_row(source, row_id)
        await _write_body(request, EmptyBody)
        callbacks = await write_and_audit(
            Action.ADMIN_REVEALED_CALLBACK,
            lambda db: writes.callback_details(db, source, row),
            lambda _found: access.event(
                Action.ADMIN_REVEALED_CALLBACK, ResourceType.REQUEST, row_id
            ),
        )
        return {"source": source, "id": row, "callbacks": callbacks}

    @app.post("/config")
    async def change_config(request: Request, _query: Annotated[NoQuery, Query()]) -> JSONResponse:
        _session_id, session = writing_session(request)
        body = await _write_body(request, ConfigBody)
        now = datetime.now(UTC)
        change = await write_and_audit(
            Action.ADMIN_CHANGED_CONFIG,
            lambda db: writes.change_config(db, body.key, body.value, session.user, now),
            lambda found: access.event(
                Action.ADMIN_CHANGED_CONFIG,
                ResourceType.CONFIG,
                body.key,
                outcome=Outcome.SUCCESS if found.changed else Outcome.UNCHANGED,
                reason=Reason(f"{body.key}_{body.value}"),
            ),
        )
        return JSONResponse(
            {
                "key": body.key,
                **_json(asdict(change.setting)),
                "applies_within_seconds": APPLIES_WITHIN_SECONDS,
            },
            status_code=status.HTTP_201_CREATED if change.changed else status.HTTP_200_OK,
        )

    reads = APIRouter(dependencies=[Depends(require_admin)])

    @reads.get("/config")
    async def read_settings(_query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        found = await read_and_audit(Action.ADMIN_READ_CONFIG, ResourceType.CONFIG, read_config)
        return {
            "config": {key: _json(asdict(setting)) for key, setting in found.items()},
            "applies_within_seconds": APPLIES_WITHIN_SECONDS,
            # The admin process's own settings: in Compose both processes are given the same.
            "facts": {
                "environment": settings.app_env,
                "model": settings.llm_model,
                "speech_to_text": SPEECH_TO_TEXT,
                "text_to_speech": TEXT_TO_SPEECH,
                "max_concurrent_calls": settings.max_concurrent_calls,
                "max_call_seconds": settings.max_call_seconds,
                "test_clock": settings.simulated_now is not None,
            },
        }

    @reads.get("/requests")
    async def read_requests(page: Annotated[RequestPage, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_REQUESTS,
            ResourceType.REQUEST,
            lambda session: requests(session, page, page.type),
        )
        return {"requests": [_json(row) for row in rows]}

    @reads.get("/messages")
    async def read_messages(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_MESSAGES,
            ResourceType.MESSAGE,
            lambda session: messages(session, page),
        )
        return {"messages": [_json(row) for row in rows]}

    @reads.get("/queue")
    async def read_queue(query: Annotated[QueueQuery, Query()]) -> dict[str, Any]:
        page = Page(before=query.before)
        rows = await read_and_audit(
            Action.ADMIN_READ_QUEUE,
            ResourceType.REQUEST,
            lambda session: request_queue(session, page, query.type, query.state),
        )
        return {"requests": [_json(row) for row in rows]}

    @reads.get("/calls")
    async def read_calls(page: Annotated[CallPage, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_CALLS,
            ResourceType.CALL,
            lambda session: calls(session, page, page.end_reason),
        )
        return {"calls": [_json(row) for row in rows]}

    # Before /calls/{call_id}, which would take "live" for a call id.
    @reads.get("/calls/live")
    async def read_live_calls(_query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(Action.ADMIN_READ_LIVE_CALLS, ResourceType.CALL, live_calls)
        return {"calls": [_masked(row) for row in rows]}

    @reads.get("/calls/{call_id}/turns")
    async def read_call_turns(call_id: str, page: Annotated[TurnPage, Query()]) -> dict[str, Any]:
        if not CALL_ID.fullmatch(call_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        rows = await read_and_audit(
            Action.ADMIN_READ_CALL_TURNS,
            ResourceType.CALL,
            lambda session: console.call_turns(
                session, call_id, limit=page.limit, after=page.after
            ),
            resource_id=call_id,
        )
        return {"turns": [_masked(row) for row in rows]}

    @reads.get("/calls/{call_id}/detail")
    async def read_call_detail(call_id: str, _query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        if not CALL_ID.fullmatch(call_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        detail = await read_and_audit(
            Action.ADMIN_READ_CALL_DETAIL,
            ResourceType.CALL,
            lambda session: call_detail(session, call_id),
            resource_id=call_id,
        )
        return {
            "call": _json(detail["call"]),
            "events": [_json(event) for event in detail["events"]],
        }

    @reads.get("/appointments")
    async def read_appointments(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_APPOINTMENTS,
            ResourceType.APPOINTMENT,
            lambda session: console.appointments(session, limit=page.limit, before=page.before),
        )
        return {"appointments": [_masked(row) for row in rows]}

    @reads.get("/slots")
    async def read_slots(query: Annotated[SlotQuery, Query()]) -> dict[str, Any]:
        start = query.start or clinic_now(settings).date()
        rows, truncated = await read_and_audit(
            Action.ADMIN_READ_SLOTS,
            ResourceType.SLOT,
            lambda session: console.slots(session, start=start, days=query.days),
        )
        return {"slots": [_masked(row) for row in rows], "truncated": truncated}

    @reads.get("/guardrails/summary")
    async def read_guardrails(query: Annotated[SummaryQuery, Query()]) -> dict[str, Any]:
        since = console.window_start(clinic_now(settings), query.days)
        rows = await read_and_audit(
            Action.ADMIN_READ_GUARDRAILS,
            ResourceType.AUDIT_EVENT,
            lambda session: console.guardrail_counts(session, since=since),
        )
        return {
            "days": query.days,
            "since": since.isoformat(),
            "counts": [_masked(row) for row in rows],
        }

    @reads.get("/metrics/summary")
    async def read_metrics(query: Annotated[SummaryQuery, Query()]) -> dict[str, Any]:
        since = console.window_start(clinic_now(settings), query.days)
        summary = await read_and_audit(
            Action.ADMIN_READ_METRICS,
            ResourceType.CALL,
            lambda session: console.metrics(session, since=since),
        )
        lists = ("calls_per_day", "ended", "latency")
        return {
            "days": query.days,
            "since": since.isoformat(),
            **{name: [_masked(row) for row in summary[name]] for name in lists},
            **_masked({name: value for name, value in summary.items() if name not in lists}),
        }

    @reads.get("/calls/{call_id}")
    async def read_call(call_id: str, _query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        if not CALL_ID.fullmatch(call_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        row = await read_and_audit(
            Action.ADMIN_READ_CALL,
            ResourceType.CALL,
            lambda session: call(session, call_id),
            resource_id=call_id,
        )
        return {"call": _json(row)}

    @reads.get("/audit")
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

    @reads.get("/audit/verify")
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
            raise _unavailable() from None
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

    app.include_router(reads)
    return AdminHeaders(ConsoleCors(app, settings.console_origin))
