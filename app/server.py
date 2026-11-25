"""The server's entry point: `uvicorn app.server:create_app --factory`. APP_ROLE picks what the
process serves. The admin role is built from the admin API alone (app/api/service.py) and never
imports the calls' application (app/main.py), the voice pipeline or Pipecat; every other role is
the calls' application, which mounts the admin API beside the calls for `all`."""

from fastapi import FastAPI

from app.api.service import create_admin_app
from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    if settings.app_role == "admin":
        return create_admin_app(settings)
    # Imported here, so that the admin role never loads the calls.
    from app.main import create_app as create_calls_app

    return create_calls_app(settings)
