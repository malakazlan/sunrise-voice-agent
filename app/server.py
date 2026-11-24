"""The server's entry point: `uvicorn app.server:create_app --factory`. APP_ROLE picks what the
process serves. The console role is built from app/console/ alone and never imports the calls'
application (app/main.py), the voice pipeline or Pipecat; every other role is the calls'
application, which mounts the admin API and the console beside the calls for `all`."""

from fastapi import FastAPI

from app.config import Settings
from app.console.service import create_console_app


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    if settings.app_role == "console":
        return create_console_app(settings)
    # Imported here, so that the console role never loads the calls.
    from app.main import create_app as create_calls_app

    return create_calls_app(settings)
