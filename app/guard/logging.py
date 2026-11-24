"""The only place log output is configured. Every handler formats with MaskingFormatter, so
every line that leaves the process has its identifiers masked (app/guard/mask.py), whoever
logged it: the app, uvicorn, Alembic, or Pipecat through loguru."""

import logging

from loguru import logger as loguru_logger

from app.config import DatabaseSettings
from app.guard.mask import mask

# The handler configure_logging puts on the root logger; a later call replaces it.
HANDLER_NAME = "app.guard.logging"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
# Pipecat logs through loguru, and its WebRTC stack through these loggers. Below WARNING they
# write signaling detail and candidate addresses, so only their warnings and errors are kept.
WEBRTC_LOGGERS = ("aioice", "aiortc")
THIRD_PARTY_FLOOR = logging.WARNING


class MaskingFormatter(logging.Formatter):
    """Masks the message and the exception text, then adds the timestamp, level and logger
    name, which are never masked. Never raises: a record whose message cannot be built is
    written as an event with its type."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = f"log_message_unformattable type={type(record.msg).__name__}"
        # loguru puts the traceback in the message and sets exc_text to a blank line.
        exception = record.exc_text or (
            self.formatException(record.exc_info) if record.exc_info else ""
        )
        stack = self.formatStack(record.stack_info) if record.stack_info else ""
        parts = [mask(message), *(mask(part) for part in (exception, stack) if part.strip())]
        text = "\n".join(parts)
        return f"{self.formatTime(record)} {record.levelname} {record.name} {text}"


def configure_logging(settings: DatabaseSettings) -> None:
    """Send every log record to stderr through the masking formatter, at LOG_LEVEL. Called once
    by the application factory and by each start-up step (both settings classes are
    DatabaseSettings); a second call replaces the first."""
    level = logging.getLevelNamesMapping()[settings.log_level]
    third_party_level = max(level, THIRD_PARTY_FLOOR)
    formatter = MaskingFormatter()

    root = logging.getLogger()
    for previous in [handler for handler in root.handlers if handler.get_name() == HANDLER_NAME]:
        root.removeHandler(previous)
    handler = logging.StreamHandler()
    handler.set_name(HANDLER_NAME)
    root.addHandler(handler)
    root.setLevel(level)

    # Handlers that exist already (uvicorn's own, those of libraries) mask too.
    for each in (root, *logging.Logger.manager.loggerDict.values()):
        if isinstance(each, logging.Logger):
            for existing in each.handlers:
                existing.setFormatter(formatter)

    for name in UVICORN_LOGGERS:
        logging.getLogger(name).setLevel(level)
    # uvicorn writes its access log only when the access logger has a handler, on it or above.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False

    for name in WEBRTC_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level)
    # A handler that fails to write would otherwise print the raw message and its arguments.
    logging.raiseExceptions = False

    # loguru's own sink writes unmasked text to stderr. diagnose=False keeps the values of
    # local variables out of its tracebacks.
    loguru_logger.remove()
    loguru_logger.add(handler, level=third_party_level, format="{message}", diagnose=False)
