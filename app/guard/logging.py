"""The only place log output is configured. Every handler formats with MaskingFormatter, so
every line that leaves the process has its identifiers masked (app/guard/mask.py), whoever
logged it: the app, uvicorn, Alembic, or Pipecat through loguru. Warnings and uncaught
exceptions, in the main thread or another, are logged too, and so masked."""

import logging
import sys
import threading
from types import TracebackType
from typing import TYPE_CHECKING

from loguru import logger as loguru_logger

from app.config import DatabaseSettings
from app.guard.mask import mask

if TYPE_CHECKING:
    from loguru import Record

logger = logging.getLogger(__name__)

# The handler configure_logging puts on the root logger; a later call replaces it.
HANDLER_NAME = "app.guard.logging"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
# Pipecat logs through loguru, and its WebRTC stack through these loggers. Below WARNING they
# write signaling detail and candidate addresses, so only their warnings and errors are kept.
WEBRTC_LOGGERS = ("aioice", "aiortc")
# The vendors' clients: below WARNING they write the request bodies (the whole conversation sent
# to the model) and websocket frames (transcripts). Only their warnings and errors are kept.
VENDOR_LOGGERS = ("anthropic", "httpx2", "httpx", "httpcore", "websockets", "deepgram")
THIRD_PARTY_FLOOR = logging.WARNING
# Pipecat's modules that quote the text being spoken in their records (pipecat-ai 1.17.0): the
# text-to-speech service warns with a line it will not speak, the frame sequencer with the words
# it drops. Their records are not written; one at warning or above leaves a line of ours.
SPEECH_TEXT_MODULES = frozenset(
    {
        "pipecat.services.tts_service",
        "pipecat.utils.context.aggregated_frame_sequencer",
        "pipecat.utils.context.word_completion_tracker",
    }
)


def _carries_no_speech_text(record: "Record") -> bool:
    """Whether a loguru record may be written. One of SPEECH_TEXT_MODULES' is not; one at
    warning or above leaves a line of the application's own in its place: its level and its
    logger, nothing of its message."""
    if record["name"] not in SPEECH_TEXT_MODULES:
        return True
    if record["level"].no >= logging.WARNING:
        logger.warning(
            "pipecat_record_withheld level=%s logger=%s", record["level"].name, record["name"]
        )
    return False


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


def _log_uncaught(
    exc_type: type[BaseException], exc_value: BaseException, exc_traceback: TracebackType | None
) -> None:
    logger.critical("uncaught_exception", exc_info=(exc_type, exc_value, exc_traceback))


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    logger.error("uncaught_thread_exception", exc_info=args.exc_value)


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

    for name in (*WEBRTC_LOGGERS, *VENDOR_LOGGERS):
        logging.getLogger(name).setLevel(third_party_level)
    # A handler that fails to write would otherwise print the raw message and its arguments.
    logging.raiseExceptions = False
    # A record that finds no handler (its logger does not pass records up) goes here.
    logging.lastResort = handler

    # Output that Python would otherwise print itself, unmasked.
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_exception

    # loguru's own sink writes unmasked text to stderr. diagnose=False keeps the values of
    # local variables out of its tracebacks.
    loguru_logger.remove()
    loguru_logger.add(
        handler,
        level=third_party_level,
        format="{message}",
        diagnose=False,
        filter=_carries_no_speech_text,
    )
