import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.call_record import drain_call_records
from app.agent.claims import ClaimKeys
from app.agent.endings import drain_call_events
from app.agent.model import anthropic_model
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import drain_guard_audits
from app.api.admin import PREFIX as ADMIN_PREFIX
from app.api.admin import build_admin_app
from app.api.offer import OpenCall, build_router
from app.audit.log import AuditLog
from app.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.domain.office_hours import clinic_now
from app.guard.logging import configure_logging
from app.voice.calls import CallRegistry
from app.voice.pipeline import END_GRACE_SECONDS, open_agent_call

logger = logging.getLogger(__name__)

CALL_PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"


def build_deps(settings: Settings, engine: AsyncEngine) -> Deps:
    """What tool dispatch needs, built once from the settings: sessions as the runtime role,
    the audit log, the clinic clock (simulated in dev and demo when set) and the claim hasher."""
    return Deps(
        sessions=create_session_factory(engine),
        audit=AuditLog(settings.audit_hmac_key),
        now=partial(clinic_now, settings),
        claim_keys=ClaimKeys(settings.identity_hash_key),
    )


def create_app(settings: Settings | None = None, open_call: OpenCall | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    configure_logging(settings)
    register_tools()

    engine = create_database_engine(settings)
    deps = build_deps(settings, engine)
    model = anthropic_model(settings)
    if open_call is None:
        open_call = partial(open_agent_call, settings, deps, model)
    # A call says goodbye and ends itself at MAX_CALL_SECONDS; the registry stops one that has
    # not, END_GRACE_SECONDS later.
    calls = CallRegistry(
        max_calls=settings.max_concurrent_calls,
        max_seconds=settings.max_call_seconds + END_GRACE_SECONDS,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await calls.close()
        # The call events and guard blocks still being written (each bounded) end before the
        # engine goes.
        await drain_call_events()
        await drain_guard_audits()
        # And the call records still being masked and stored.
        await drain_call_records()
        if model is not None:
            await model.close()
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
    app.state.deps = deps

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
            build_admin_app(settings, deps.sessions, deps.audit),
        )

    return app
