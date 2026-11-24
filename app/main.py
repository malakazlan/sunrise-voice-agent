from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.offer import OpenCall, build_router
from app.config import Settings
from app.guard.logging import configure_logging
from app.voice.calls import CallRegistry
from app.voice.pipeline import open_echo_call

CALL_PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"


def create_app(settings: Settings | None = None, open_call: OpenCall | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    configure_logging(settings)

    if open_call is None:
        open_call = partial(open_echo_call, settings)
    calls = CallRegistry(
        max_calls=settings.max_concurrent_calls, max_seconds=settings.max_call_seconds
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await calls.close()

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

    return app
