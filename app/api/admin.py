"""The admin API: read-only views of the requests, the messages, the calls and the audit log,
and the admin console's reads (app/audit/console.py): the calls in progress, a call's turns, the
appointments, the slots and the summaries of the guardrails and the calls' numbers, behind HTTP
Basic sign-in. app/main.py mounts it at PREFIX only when ADMIN_PASSWORD is set. The calls in
progress, the slots' default day and the summaries' days follow the clinic's clock.

Every successful read appends one audit event naming the admin, in the transaction that read
the rows, and the rows leave only once that transaction has committed. A call that is not
there, or an id that is not a call id, is answered as an unknown path is, and not audited. The
list of calls carries no transcript; only the read of one call does. Every text value from the
database passes `mask` on its way out, transcripts included, and a callback number shows only
its last four digits, whatever its shape. Every response carries `Cache-Control: no-store`,
errors included. One verification of the chain runs at a time.
"""

import hashlib
import hmac
import logging
import re
from base64 import b64decode
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.audit import console
from app.audit.log import ADMIN_PREFIX, CALL_ID, Action, AuditLog, Event, Outcome, ResourceType
from app.audit.read import MAX_PAGE, MAX_SEQ, recent_events
from app.config import Settings
from app.db import models
from app.domain.office_hours import clinic_now
from app.guard.mask import mask
from app.guard.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

PREFIX = "/admin/api"
DEFAULT_PAGE = 50
# The largest value of a Postgres integer, the type of request and message ids.
MAX_ROW_ID = 2**31 - 1
# A Basic header for a 32-character user name and a long password is well under this. A longer
# header is refused before it is decoded or hashed.
MAX_AUTHORIZATION_LENGTH = 1024
SIGN_IN_FAILURES = 5
SIGN_IN_WINDOW_SECONDS = 60
MAX_TRACKED_ADDRESSES = 10_000
CHALLENGE = 'Basic realm="admin", charset="UTF-8"'
# A verification asked for while another runs is refused; this is when to ask again.
VERIFY_RETRY_SECONDS = 5
DIGIT = re.compile(r"\d")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The days a summary or the slots cover: a week unless asked.
DEFAULT_DAYS = 7
MAX_SLOT_DAYS = 14
MAX_SUMMARY_DAYS = 30

REQUEST_COLUMNS = (
    models.Request.id,
    models.Request.created_at,
    models.Request.call_id,
    models.Request.type,
    models.Request.summary,
    models.Request.patient_id,
)
MESSAGE_COLUMNS = (
    models.Message.id,
    models.Message.created_at,
    models.Message.call_id,
    models.Message.kind,
    models.Message.name,
    models.Message.callback_number,
    models.Message.body,
    models.Message.patient_id,
)
CALL_COLUMNS = (
    models.CallSession.id,
    models.CallSession.call_id,
    models.CallSession.started_at,
    models.CallSession.ended_at,
    models.CallSession.office_open,
    models.CallSession.verified,
    models.CallSession.patient_id,
    models.CallSession.end_reason,
    models.CallSession.turn_count,
)
# The same body as a path that does not exist.
NOT_FOUND = "Not Found"


class Page(BaseModel):
    """A page of requests or messages: ids are Postgres integers."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    before: int | None = Field(default=None, ge=1, le=MAX_ROW_ID)


class AuditPage(Page):
    """A page of audit events: seqs are bigints."""

    before: int | None = Field(default=None, ge=1, le=MAX_SEQ)
    call_id: str | None = None
    action: Action | None = None

    @field_validator("call_id")
    @classmethod
    def _call_id_shape(cls, value: str | None) -> str | None:
        if value is not None and not CALL_ID.fullmatch(value):
            raise ValueError("call_id does not have the shape of a call id")
        return value


class NoQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestPage(Page):
    """A page of requests, optionally of one known type."""

    type: str | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str | None) -> str | None:
        if value is not None and value not in models.REQUEST_TYPES:
            raise ValueError("type is not a request type")
        return value


class TurnPage(BaseModel):
    """A page of one call's turns, in order, after a turn number."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    after: int | None = Field(default=None, ge=1, le=MAX_ROW_ID)


class SlotQuery(BaseModel):
    """The slots of 1 to MAX_SLOT_DAYS days from a date at the clinic (YYYY-MM-DD; today if
    not given)."""

    model_config = ConfigDict(extra="forbid")

    start: date | None = Field(default=None, alias="from")
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
    """A row as the API shows it: text masked, instants as ISO 8601 text, numbers and nulls as
    they are. Any other type stops the response."""
    shown: dict[str, Any] = {}
    for name, value in row.items():
        if isinstance(value, str):
            shown[name] = mask(value)
        elif isinstance(value, datetime):
            shown[name] = value.isoformat()
        elif value is None or isinstance(value, int):
            shown[name] = value
        else:
            raise TypeError(f"{name} has a type the admin API does not show")
    return shown


def _shown_callback(value: str | None) -> str | None:
    """A callback number as `***-***-` and its last four digits, however it was written;
    with fewer than four digits, nothing of it."""
    if value is None:
        return None
    digits = DIGIT.findall(value)
    return "***-***-" + ("".join(digits[-4:]) if len(digits) >= 4 else "****")


async def _newest(
    session: AsyncSession,
    columns: Sequence[InstrumentedAttribute[Any]],
    row_id: InstrumentedAttribute[int],
    page: Page,
    condition: ColumnElement[bool] | None = None,
) -> list[dict[str, Any]]:
    statement = select(*columns)
    if condition is not None:
        statement = statement.where(condition)
    if page.before is not None:
        statement = statement.where(row_id < page.before)
    rows = await session.execute(statement.order_by(row_id.desc()).limit(page.limit))
    return [dict(row._mapping) for row in rows]


async def _call(session: AsyncSession, call_id: str) -> dict[str, Any] | None:
    """The stored call with its transcript, or None."""
    statement = select(
        *CALL_COLUMNS,
        models.CallSession.transcript,
        models.CallSession.transcript_truncated,
        models.CallSession.names_masked,
    ).where(models.CallSession.call_id == call_id)
    row = (await session.execute(statement)).first()
    return None if row is None else dict(row._mapping)


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


def build_admin_app(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], audit: AuditLog
) -> ASGIApp:
    if settings.admin_user is None or settings.admin_password is None:
        raise ValueError("the admin API needs admin_user and admin_password")
    expected_user = hashlib.sha256(settings.admin_user.encode("utf-8")).digest()
    expected_password = hashlib.sha256(
        settings.admin_password.get_secret_value().encode("utf-8")
    ).digest()
    actor = f"{ADMIN_PREFIX}{settings.admin_user}"
    limiter = RateLimiter(
        limit=SIGN_IN_FAILURES,
        window_seconds=SIGN_IN_WINDOW_SECONDS,
        max_addresses=MAX_TRACKED_ADDRESSES,
    )

    async def require_admin(request: Request) -> None:
        address = request.client.host if request.client is not None else ""
        # A limited address is refused before its credentials are looked at.
        if limiter.blocked(address):
            logger.warning("admin_sign_in_limited")
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed sign-ins",
                headers={"Retry-After": str(SIGN_IN_WINDOW_SECONDS)},
            )
        supplied = _supplied_credentials(request.headers.get("authorization"))
        if supplied is not None and _credentials_match(*supplied, expected_user, expected_password):
            return
        failures = limiter.record_failure(address)
        logger.warning("admin_sign_in_failed failures=%d", failures)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Sign-in required",
            headers={"WWW-Authenticate": CHALLENGE},
        )

    async def read_and_audit[T](
        action: Action,
        resource_type: ResourceType,
        read: Callable[[AsyncSession], Awaitable[T | None]],
        resource_id: str | None = None,
    ) -> T:
        """The read and its audit event in one transaction; nothing is returned unless both
        committed. A read that finds nothing (None) writes no event and is answered 404."""
        try:
            async with sessions() as session:
                result = await read(session)
                if result is not None:
                    await audit.append(
                        session,
                        Event(
                            actor=actor,
                            action=action,
                            resource_type=resource_type,
                            outcome=Outcome.SUCCESS,
                            resource_id=resource_id,
                        ),
                        now=datetime.now(UTC),
                    )
                    await session.commit()
        except Exception as error:
            logger.error("admin_read_failed action=%s error=%s", action.value, type(error).__name__)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
            ) from None
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        logger.info("admin_read action=%s", action.value)
        return result

    verifying = False

    app = FastAPI(
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(require_admin), Depends(_one_value_each)],
        exception_handlers={RequestValidationError: _invalid_query},
    )

    @app.get("/requests")
    async def read_requests(page: Annotated[RequestPage, Query()]) -> dict[str, Any]:
        of_type = None if page.type is None else models.Request.type == page.type
        rows = await read_and_audit(
            Action.ADMIN_READ_REQUESTS,
            ResourceType.REQUEST,
            lambda session: _newest(session, REQUEST_COLUMNS, models.Request.id, page, of_type),
        )
        return {"requests": [_masked(row) for row in rows]}

    @app.get("/messages")
    async def read_messages(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_MESSAGES,
            ResourceType.MESSAGE,
            lambda session: _newest(session, MESSAGE_COLUMNS, models.Message.id, page),
        )
        return {
            "messages": [
                {**_masked(row), "callback_number": _shown_callback(row["callback_number"])}
                for row in rows
            ]
        }

    @app.get("/calls")
    async def read_calls(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_CALLS,
            ResourceType.CALL,
            lambda session: _newest(session, CALL_COLUMNS, models.CallSession.id, page),
        )
        return {"calls": [_masked(row) for row in rows]}

    # Before /calls/{call_id}, which would take "live" for a call id.
    @app.get("/calls/live")
    async def read_live_calls(_query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        now = clinic_now(settings)
        rows = await read_and_audit(
            Action.ADMIN_READ_LIVE_CALLS,
            ResourceType.CALL,
            lambda session: console.live_calls(
                session, now=now, max_call_seconds=settings.max_call_seconds
            ),
        )
        return {"calls": [_masked(row) for row in rows]}

    @app.get("/calls/{call_id}/turns")
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

    @app.get("/appointments")
    async def read_appointments(page: Annotated[Page, Query()]) -> dict[str, Any]:
        rows = await read_and_audit(
            Action.ADMIN_READ_APPOINTMENTS,
            ResourceType.APPOINTMENT,
            lambda session: console.appointments(session, limit=page.limit, before=page.before),
        )
        return {"appointments": [_masked(row) for row in rows]}

    @app.get("/slots")
    async def read_slots(query: Annotated[SlotQuery, Query()]) -> dict[str, Any]:
        start = query.start or clinic_now(settings).date()
        rows, truncated = await read_and_audit(
            Action.ADMIN_READ_SLOTS,
            ResourceType.SLOT,
            lambda session: console.slots(session, start=start, days=query.days),
        )
        return {"slots": [_masked(row) for row in rows], "truncated": truncated}

    @app.get("/guardrails/summary")
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

    @app.get("/metrics/summary")
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

    @app.get("/calls/{call_id}")
    async def read_call(call_id: str, _query: Annotated[NoQuery, Query()]) -> dict[str, Any]:
        if not CALL_ID.fullmatch(call_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
        row = await read_and_audit(
            Action.ADMIN_READ_CALL,
            ResourceType.CALL,
            lambda session: _call(session, call_id),
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
        # The whole chain is read, on the worker that carries the calls: one run at a time.
        # The check and the claim have no await between them, so two requests cannot both pass.
        nonlocal verifying
        if verifying:
            logger.warning("admin_verify_busy")
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="A verification is already running",
                headers={"Retry-After": str(VERIFY_RETRY_SECONDS)},
            )
        verifying = True
        try:
            verification = await read_and_audit(
                Action.ADMIN_VERIFY_AUDIT, ResourceType.AUDIT_EVENT, audit.verify
            )
        finally:
            verifying = False
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
