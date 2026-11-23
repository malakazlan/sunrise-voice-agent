import logging

from fastapi import FastAPI

from app.config import Settings


def create_app(settings: Settings) -> FastAPI:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger().setLevel(settings.log_level)

    docs_enabled = settings.app_env == "dev"
    app = FastAPI(
        title="Sunrise voice agent",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def __getattr__(name: str) -> FastAPI:
    # uvicorn resolves "app.main:app" through this hook, so settings are read when the
    # server starts rather than whenever the module is imported (tests import it too).
    if name == "app":
        return create_app(Settings())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
