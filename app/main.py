import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.agent.tools import register_tools
from app.api.admin import PREFIX as ADMIN_PREFIX
from app.api.admin import build_admin_app
from app.api.offer import OpenCall, build_router
from app.audit.log import AuditLog
from app.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.guard.logging import configure_logging
from app.voice.calls import CallRegistry
from app.voice.pipeline import open_echo_call

logger = logging.getLogger(__name__)

CALL_PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"


def create_app(settings: Settings | None = None, open_call: OpenCall | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    configure_logging(settings)
    register_tools()

    if open_call is None:
        open_call = partial(open_echo_call, settings)
    calls = CallRegistry(
        max_calls=settings.max_concurrent_calls, max_seconds=settings.max_call_seconds
    )
    # The application's database engine: the runtime role. It connects on first use.
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await calls.close()
        await engine.dispose()

    docs_enabled = settings.app_env == "dev"
    app = FastAPI(
        title="Sunrise voice agent",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.calls = calls

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def call_page() -> FileResponse:
        return FileResponse(CALL_PAGE, media_type="text/html")

    app.include_router(build_router(settings, calls, open_call))

    if settings.admin_password is None:
        logger.warning("admin_api_off")
    else:
        app.mount(
            ADMIN_PREFIX,
            build_admin_app(settings, sessions, AuditLog(settings.audit_hmac_key)),
        )

    return app
