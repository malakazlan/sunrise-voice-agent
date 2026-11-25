"""Where the admin API is put together, for each role that serves it: mounted beside the calls
(APP_ROLE=all, app/main.py), or as an application of its own that starts without any key of the
calls (APP_ROLE=admin, app/server.py)."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.api.admin import PREFIX, AdminAccess, SessionFactory, build_admin_app
from app.api.security import AdminHeaders
from app.audit.log import AuditLog
from app.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.guard.logging import configure_logging

logger = logging.getLogger(__name__)

# Everything under it is the admin's: with the admin off, all of it is answered as not found.
ADMIN_ROOT = "/admin"


def mount_admin(
    app: FastAPI,
    settings: Settings,
    sessions: SessionFactory,
    audit: AuditLog,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The admin API under /admin/api, with one sign-in check and its limiter, the browser
    sessions, and one verification of the chain at a time. `clock` times the sessions and the
    live stream, and `sleep` waits between the stream's ticks."""
    access = AdminAccess(settings, sessions, audit, clock=clock)
    app.mount(PREFIX, build_admin_app(settings, access, clock=clock, sleep=sleep))


async def _not_found(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)


def create_admin_app(settings: Settings) -> FastAPI:
    """The admin role: the admin API and /health; nothing of the calls. Without the admin's
    password it still starts, and everything under /admin is not found."""
    if settings.app_role != "admin":
        raise ValueError("the admin application is built for the admin role only")
    configure_logging(settings)
    engine = create_database_engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Sunrise admin API",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if settings.admin_password is None:
        logger.warning("admin_off_no_password")
        off = AdminHeaders(_not_found)
        app.router.routes.append(Route(ADMIN_ROOT, endpoint=off, include_in_schema=False))
        app.mount(ADMIN_ROOT, off)
    else:
        mount_admin(
            app, settings, create_session_factory(engine), AuditLog(settings.audit_hmac_key)
        )
    logger.info("admin_api_started")
    return app
