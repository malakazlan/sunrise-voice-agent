"""The server's entry point: `uvicorn app.server:create_app --factory`. APP_ROLE picks what the
process serves. The admin role is built from the admin API alone (app/api/service.py) and never
imports the calls' application (app/main.py), the voice pipeline or Pipecat; every other role is
the calls' application, which mounts the admin API beside the calls for `all`."""

import logging

from fastapi import FastAPI

from app.api.service import create_admin_app
from app.config import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    if settings.app_role == "admin":
        app = create_admin_app(settings)
    else:
        # Imported here, so that the admin role never loads the calls.
        from app.main import create_app as create_calls_app

        app = create_calls_app(settings)
    # The other role's settings, given by a platform that gives every container every
    # variable and not loaded (app/config.py): named once, by variable name only.
    if settings.ignored_settings:
        names = ",".join(name.upper() for name in settings.ignored_settings)
        logger.warning("other_role_settings_ignored names=%s", names)
    return app
