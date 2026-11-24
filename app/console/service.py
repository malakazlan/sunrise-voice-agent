"""Where the admin API and the console are put together, for each role that serves them:
mounted beside the calls (APP_ROLE=all, app/main.py), or as an application of their own that
starts without any key of the calls (APP_ROLE=console, app/server.py)."""

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI
from starlette.routing import Route

from app.api.admin import PREFIX as API_PREFIX
from app.api.admin import AdminAccess, SessionFactory, build_admin_app
from app.audit.log import AuditLog
from app.config import Settings
from app.console.pages import PREFIX, build_console, to_first_page
from app.console.security import ConsoleHeaders
from app.console.sessions import SessionTable
from app.db.session import create_database_engine, create_session_factory
from app.domain.office_hours import clinic_now
from app.guard.logging import configure_logging

logger = logging.getLogger(__name__)


def mount_admin(
    app: FastAPI,
    settings: Settings,
    sessions: SessionFactory,
    audit: AuditLog,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """The admin API under /admin/api and the console under /admin, sharing one sign-in check
    and its limiter, and one verification of the chain at a time. `clock` times the console's
    sessions."""
    access = AdminAccess(settings, sessions, audit)
    app.mount(API_PREFIX, build_admin_app(access))
    app.router.routes.append(
        Route(PREFIX, endpoint=ConsoleHeaders(to_first_page), include_in_schema=False)
    )
    console = build_console(
        settings, access, partial(clinic_now, settings), SessionTable(clock=clock)
    )
    app.mount(PREFIX, console)


def create_console_app(settings: Settings) -> FastAPI:
    """The console role: the admin API, the console and /health; nothing of the calls."""
    if settings.app_role != "console":
        raise ValueError("the console application is built for the console role only")
    configure_logging(settings)
    engine = create_database_engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Sunrise admin console",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    mount_admin(app, settings, create_session_factory(engine), AuditLog(settings.audit_hmac_key))
    logger.info("console_started")
    return app
