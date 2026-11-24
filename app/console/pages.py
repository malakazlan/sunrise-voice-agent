"""The console's pages: sign-in and sign-out, Call history, Call detail, Requests and the Audit
log, server-rendered, under /admin.

Every page but the sign-in page and the static files needs a live session (app/console/
sessions.py); without one a page answers with the way to the sign-in page, and reads nothing.
Every page view reads through AdminAccess.read, with one function of app/api/reads.py or the
audit log's reader, so it writes exactly one audit event, in the transaction that read; if the
event cannot be written the page shows its error state and no data. A filter or a page of rows
asked for by htmx (the HX-Request header) gets the table's card alone. Every form sent (sign-in,
sign-out, the check of the chain) carries an anti-forgery token, and one from another site is
refused. Logs carry event names, actions and exception class names; never a value.
"""

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api import reads
from app.api.admin import AdminAccess, Reader, VerificationRunningError
from app.audit.log import CALL_ID, Action, ResourceType
from app.audit.read import recent_events
from app.config import Settings
from app.console import views
from app.console.security import ConsoleHeaders, from_another_site, read_form, tokens_match
from app.console.sessions import ChainCheck, Session, SessionTable
from app.domain.office_hours import is_open

logger = logging.getLogger(__name__)

PREFIX = "/admin"
SIGN_IN = f"{PREFIX}/login"
FIRST_PAGE = f"{PREFIX}/calls"
SESSION_COOKIE = "console_session"
LOGIN_COOKIE = "console_login"
# Rows on one page of a list.
PAGE_SIZE = 50
HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

FAILED = "The user name or password is not right."
LIMITED = "Too many failed sign-ins. Wait a minute, then try again."
UNAUDITED = "Signing in is not possible right now: the audit log could not record it. Try again."


class NotSignedInError(Exception):
    """The request carries no live session."""


class BadQueryError(Exception):
    """The address asks for a filter or a page the console does not know."""


def _templates() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters |= dict(
        when=views.when,
        clock=views.clock,
        day=views.day,
        short_id=views.short_id,
    )
    environment.globals |= dict(
        END_REASONS=views.END_REASONS,
        REQUEST_TYPES=views.REQUEST_TYPES,
        REQUEST_FILTERS=views.REQUEST_FILTERS,
        EVENT_ICONS=views.EVENT_ICONS,
        ACTIONS=sorted(action.value for action in Action),
        ZONE=views.ZONE,
        length=views.length,
        transcript_lines=views.transcript_lines,
        pieces=views.pieces,
    )
    return environment


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    """The address's parameters, each given at most once and known to the page; an empty value
    is no value (an empty filter field)."""
    items = request.query_params.multi_items()
    names = [name for name, _ in items]
    if len(set(names)) != len(names) or not set(names) <= allowed:
        raise BadQueryError
    return {name: value for name, value in items if value != ""}


def _page(values: dict[str, str]) -> reads.Page:
    try:
        return reads.Page(limit=PAGE_SIZE + 1, before=values.get("before"))
    except ValidationError:
        raise BadQueryError from None


def _older(rows: list[dict[str, Any]], key: str) -> tuple[list[dict[str, Any]], int | None]:
    """The rows of one page, and where the next page starts if there is one."""
    if len(rows) > PAGE_SIZE:
        return rows[:PAGE_SIZE], rows[PAGE_SIZE - 1][key]
    return rows, None


def build_console(
    settings: Settings, access: AdminAccess, now: Callable[[], datetime], table: SessionTable
) -> ASGIApp:
    templates = _templates()
    secure = settings.app_env != "dev"

    def render(name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        html = templates.get_template(name).render(**context)
        return HTMLResponse(html, status_code=status_code, headers={"Vary": "HX-Request"})

    def frame(session: Session, current: str) -> dict[str, Any]:
        """What every signed-in page shows around its content."""
        return {
            "user": session.user,
            "token": session.token,
            "office_open": is_open(now()),
            "current": current,
        }

    def htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    def current_session(request: Request) -> Session:
        session = table.find(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise NotSignedInError
        return session

    async def read[T](
        action: Action,
        resource_type: ResourceType,
        reader: Reader[T],
        resource_id: str | None = None,
    ) -> tuple[bool, T | None]:
        """Whether the read and its event went through, and what it found."""
        try:
            found = await access.read(action, resource_type, reader, resource_id)
        except Exception as error:
            logger.error(
                "console_read_failed action=%s error=%s", action.value, type(error).__name__
            )
            return False, None
        logger.info("console_page action=%s", action.value)
        return True, found

    def forbidden() -> HTMLResponse:
        logger.warning("console_forged_request")
        return render("forbidden.html", 403)

    async def form_with_token(request: Request, expected: str | None) -> bool:
        if from_another_site(request):
            return False
        form = await read_form(request)
        return form is not None and tokens_match(form.one(b"token"), expected)

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def to_sign_in(request: Request) -> Response:
        """The way to the sign-in page: a redirect, or for htmx the header it follows."""
        if htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": SIGN_IN})
        return RedirectResponse(SIGN_IN, status_code=303)

    @app.exception_handler(NotSignedInError)
    async def not_signed_in(request: Request, _error: Exception) -> Response:
        return to_sign_in(request)

    @app.exception_handler(BadQueryError)
    async def bad_query(request: Request, _error: Exception) -> Response:
        session = table.find(request.cookies.get(SESSION_COOKIE))
        if session is None:
            return to_sign_in(request)
        return render("error.html", 400, kind="bad_query", **frame(session, ""))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        if request.url.path.startswith(f"{PREFIX}/static/") or error.status_code != 404:
            return PlainTextResponse(str(error.detail), status_code=error.status_code)
        session = table.find(request.cookies.get(SESSION_COOKIE))
        if session is None:
            return to_sign_in(request)
        return render("error.html", 404, kind="not_found", **frame(session, ""))

    @app.get("/")
    async def console_root() -> RedirectResponse:
        return RedirectResponse(FIRST_PAGE, status_code=303)

    def sign_in_page(status_code: int = 200, message: str | None = None) -> HTMLResponse:
        token = secrets.token_urlsafe(32)
        response = render("login.html", status_code, token=token, message=message)
        response.set_cookie(
            LOGIN_COOKIE, token, path=SIGN_IN, secure=secure, httponly=True, samesite="strict"
        )
        return response

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        if table.find(request.cookies.get(SESSION_COOKIE)) is not None:
            return RedirectResponse(FIRST_PAGE, status_code=303)
        signed_out = request.query_params.get("signed_out") == "1"
        return sign_in_page(message="You are signed out." if signed_out else None)

    @app.post("/login")
    async def login(request: Request) -> Response:
        if from_another_site(request):
            return forbidden()
        form = await read_form(request)
        if form is None or not tokens_match(form.one(b"token"), request.cookies.get(LOGIN_COOKIE)):
            return forbidden()
        address = request.client.host if request.client is not None else ""
        if access.limited(address):
            return sign_in_page(429, LIMITED)
        if not access.signs_in(address, form.both(b"user", b"password")):
            return sign_in_page(401, FAILED)
        try:
            await access.record(Action.ADMIN_SIGNED_IN)
        except Exception as error:
            logger.error("console_sign_in_unaudited error=%s", type(error).__name__)
            return sign_in_page(503, UNAUDITED)
        session_id, _ = table.open(access.user)
        logger.info("console_signed_in")
        response = RedirectResponse(FIRST_PAGE, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            path=PREFIX,
            secure=secure,
            httponly=True,
            samesite="strict",
        )
        response.delete_cookie(
            LOGIN_COOKIE, path=SIGN_IN, secure=secure, httponly=True, samesite="strict"
        )
        return response

    @app.post("/logout")
    async def logout(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> Response:
        if not await form_with_token(request, session.token):
            return forbidden()
        table.close(request.cookies.get(SESSION_COOKIE, ""))
        try:
            await access.record(Action.ADMIN_SIGNED_OUT)
        except Exception as error:
            # The session has ended all the same: signing out never waits on the log.
            logger.error("console_sign_out_unaudited error=%s", type(error).__name__)
        logger.info("console_signed_out")
        response = RedirectResponse(f"{SIGN_IN}?signed_out=1", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE, path=PREFIX, secure=secure, httponly=True, samesite="strict"
        )
        return response

    @app.get("/calls")
    async def calls(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> HTMLResponse:
        values = _query(request, {"ended", "before"})
        ended = values.get("ended")
        if ended is not None and ended not in views.END_REASONS:
            raise BadQueryError
        page = _page(values)
        ok, rows = await read(
            Action.ADMIN_READ_CALLS,
            ResourceType.CALL,
            lambda db: reads.ended_calls(db, page, ended),
        )
        shown, older = _older(rows or [], "id")
        context = {
            **frame(session, "calls"),
            "ok": ok,
            "rows": shown,
            "ended": ended,
            "older": older,
            "paged": page.before is not None,
        }
        name = "_calls_card.html" if htmx(request) else "calls.html"
        return render(name, 200 if ok else 503, **context)

    @app.get("/calls/{call_id}")
    async def call_detail(
        request: Request, call_id: str, session: Annotated[Session, Depends(current_session)]
    ) -> HTMLResponse:
        _query(request, set())
        if not CALL_ID.fullmatch(call_id):
            return render("error.html", 404, kind="no_call", **frame(session, "calls"))
        ok, detail = await read(
            Action.ADMIN_READ_CALL_DETAIL,
            ResourceType.CALL,
            lambda db: reads.call_detail(db, call_id),
            resource_id=call_id,
        )
        if not ok:
            return render("error.html", 503, kind="unavailable", **frame(session, "calls"))
        if detail is None:
            return render("error.html", 404, kind="no_call", **frame(session, "calls"))
        return render("call.html", **frame(session, "calls"), call=detail)

    @app.get("/requests")
    async def requests(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> HTMLResponse:
        values = _query(request, {"type", "before"})
        kind = values.get("type")
        if kind is not None and kind not in views.REQUEST_TYPES:
            raise BadQueryError
        page = _page(values)
        ok, rows = await read(
            Action.ADMIN_READ_QUEUE,
            ResourceType.REQUEST,
            lambda db: reads.request_queue(db, page, kind),
        )
        shown, older = _older(rows or [], "id")
        context = {
            **frame(session, "requests"),
            "ok": ok,
            "rows": shown,
            "kind": kind,
            "older": older,
            "paged": page.before is not None,
        }
        name = "_requests_card.html" if htmx(request) else "requests.html"
        return render(name, 200 if ok else 503, **context)

    @app.get("/audit")
    async def audit(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> HTMLResponse:
        values = _query(request, {"call_id", "action", "before"})
        try:
            page = reads.AuditPage(
                limit=PAGE_SIZE + 1,
                before=values.get("before"),
                call_id=values.get("call_id"),
                action=values.get("action"),
            )
        except ValidationError:
            raise BadQueryError from None
        ok, rows = await read(
            Action.ADMIN_READ_AUDIT,
            ResourceType.AUDIT_EVENT,
            lambda db: recent_events(
                db,
                limit=page.limit,
                before=page.before,
                call_id=page.call_id,
                action=page.action,
            ),
        )
        shown, older = _older([reads.masked(row) for row in rows or []], "seq")
        context = {
            **frame(session, "audit"),
            "ok": ok,
            "rows": shown,
            "call_id": page.call_id,
            "action": None if page.action is None else page.action.value,
            "older": older,
            "paged": page.before is not None,
            "check": session.chain_check,
        }
        name = "_audit_card.html" if htmx(request) else "audit.html"
        return render(name, 200 if ok else 503, **context)

    @app.post("/audit/check")
    async def check_chain(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> Response:
        if not await form_with_token(request, session.token):
            return forbidden()
        at = datetime.now(UTC)
        try:
            verification = await access.verify()
        except VerificationRunningError:
            session.chain_check = ChainCheck(outcome="running", at=at)
        except Exception as error:
            logger.error(
                "console_read_failed action=%s error=%s",
                Action.ADMIN_VERIFY_AUDIT.value,
                type(error).__name__,
            )
            session.chain_check = ChainCheck(outcome="failed", at=at)
        else:
            first = verification.breaks[0] if verification.breaks else None
            session.chain_check = ChainCheck(
                outcome="intact" if verification.ok else "broken",
                at=at,
                checked=verification.checked,
                breaks=len(verification.breaks),
                first_seq=None if first is None else first.seq,
                first_kind=None if first is None else first.kind.value,
                truncated=verification.truncated,
            )
            logger.info("console_page action=%s", Action.ADMIN_VERIFY_AUDIT.value)
        if htmx(request):
            return render("_chain.html", check=session.chain_check, token=session.token)
        return RedirectResponse(f"{PREFIX}/audit", status_code=303)

    return ConsoleHeaders(app)


async def to_first_page(scope: Scope, receive: Receive, send: Send) -> None:
    """The console's bare address, /admin without the slash: on to the first page."""
    await RedirectResponse(FIRST_PAGE, status_code=303)(scope, receive, send)
