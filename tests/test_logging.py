import asyncio
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from conftest import OFFLINE_DB_ENV, REPO_ROOT, offline_settings
from loguru import logger as loguru_logger
from uvicorn.config import LOGGING_CONFIG
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from app.config import LogLevel
from app.guard.logging import MaskingFormatter
from app.main import create_app

# Identifier samples are assembled from short pieces, so that no tracked file holds one.
PHONE = "-".join(["555", "201", "33" + "44"])
SSN_DIGITS = "".join(["123", "45", "6789"])
DOB = "1985-03-14"
NUMERIC_DOB = "3/14/1985"

# The formatter's own timestamp, at the start of each record's first line.
TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}"

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
# The WebRTC stack under Pipecat.
WEBRTC_LOGGERS = ("aioice", "aiortc")

CONFIGURE = (
    "import sys\n"
    "from app.config import Settings\n"
    "from app.guard.logging import configure_logging\n"
    "configure_logging(Settings())\n"
)


@pytest.fixture
def logged(tmp_path: Path) -> Callable[..., str]:
    """`logged(script, *args)`: run the script in a fresh interpreter, after the application's
    logging configuration, from a directory without a .env; what it wrote to stderr. The
    script reads identifiers from its arguments, so its own source lines hold none."""

    def run(script: str, *args: str) -> str:
        path = tmp_path / "script.py"
        path.write_text(CONFIGURE + script, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(path), *args],
            cwd=tmp_path,
            env={
                **os.environ,
                **OFFLINE_DB_ENV,
                "PYTHONPATH": str(REPO_ROOT),
                "PYTHONIOENCODING": "utf-8",
                "APP_ENV": "dev",
                "LOG_LEVEL": "INFO",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        return result.stderr

    return run


def without_timestamps(output: str) -> list[str]:
    return [re.sub(rf"^{TIMESTAMP} ", "", line) for line in output.splitlines()]


def test_stdlib_records_are_masked(logged: Callable[..., str]) -> None:
    output = logged(
        "import logging\n"
        "log = logging.getLogger('app.example')\n"
        "phone, ssn, dob, numeric_dob = sys.argv[1:]\n"
        "log.info('caller dob ' + dob)\n"
        "log.info('caller phone %s', phone)\n"
        "log.info('born %(dob)s', {'dob': numeric_dob})\n"
        "log.info('lookup_started', extra={'phone': phone, 'ssn': ssn})\n"
        "log.info('first ' + dob + '\\nsecond ' + phone)\n"
        "log.warning(int(ssn))\n"
        "log.info('bad %d arguments', phone)\n"
        "try:\n"
        "    raise ValueError('no patient with phone ' + phone)\n"
        "except ValueError:\n"
        "    log.exception('lookup_failed')\n",
        PHONE,
        SSN_DIGITS,
        DOB,
        NUMERIC_DOB,
    )
    lines = without_timestamps(output)

    for expected in (
        "INFO app.example caller dob ****-**-14",
        "INFO app.example caller phone ***-***-3344",
        "INFO app.example born ****-**-14",
        "INFO app.example lookup_started",
        "INFO app.example first ****-**-14",
        "second ***-***-3344",
        "WARNING app.example ***-**-****",
        "INFO app.example log_message_unformattable type=str",
        "ERROR app.example lookup_failed",
        "ValueError: no patient with phone ***-***-3344",
    ):
        assert expected in lines, expected
    for identifier in (PHONE, SSN_DIGITS, DOB, NUMERIC_DOB):
        assert identifier not in output
    assert "Logging error" not in output


def test_loguru_records_are_masked(logged: Callable[..., str]) -> None:
    output = logged(
        "from loguru import logger\n"
        "phone, dob = sys.argv[1:]\n"
        "logger.info('below the cut-off ' + phone)\n"
        "logger.warning('caller phone {}', phone)\n"
        "logger.error('caller dob ' + dob)\n",
        PHONE,
        DOB,
    )
    lines = without_timestamps(output)

    assert "WARNING __main__ caller phone ***-***-3344" in lines
    assert "ERROR __main__ caller dob ****-**-14" in lines
    # Pipecat's records below WARNING stay out, and loguru's own sink is gone.
    assert "below the cut-off" not in output
    assert PHONE not in output
    assert DOB not in output
    assert len(lines) == 2


def test_tracebacks_are_masked(logged: Callable[..., str]) -> None:
    output = logged(
        "import logging\n"
        "from loguru import logger\n"
        "def lookup(caller_phone):\n"
        "    marker = 'local-value-marker'\n"
        "    if caller_phone and marker: raise LookupError('no match for ' + caller_phone)\n"
        "try:\n"
        "    lookup(sys.argv[1])\n"
        "except LookupError:\n"
        "    logging.getLogger('app.example').exception('lookup_failed')\n"
        "    logger.exception('pipecat_lookup_failed')\n",
        PHONE,
    )
    lines = without_timestamps(output)

    assert "ERROR app.example lookup_failed" in lines
    assert "ERROR __main__ pipecat_lookup_failed" in lines
    # The exception message, in both tracebacks.
    assert lines.count("LookupError: no match for ***-***-3344") == 2
    assert PHONE not in output
    # loguru does not print the values of local variables.
    assert "local-value-marker" not in output


def test_failed_write_prints_nothing(logged: Callable[..., str]) -> None:
    # Without the configuration, logging reports the failure on stderr with the raw message
    # and its arguments.
    output = logged(
        "import logging\n"
        "class Broken:\n"
        "    def write(self, text): raise OSError('stream closed')\n"
        "    def flush(self): pass\n"
        "logging.getLogger().handlers[0].stream = Broken()\n"
        "logging.getLogger('app.example').info('caller phone %s', sys.argv[1])\n",
        PHONE,
    )

    assert output == ""


def test_timestamp_survives(logged: Callable[..., str]) -> None:
    output = logged(
        "import logging\nlogging.getLogger('app.example').info('caller dob ' + sys.argv[1])\n",
        DOB,
    )

    assert re.fullmatch(rf"{TIMESTAMP} INFO app\.example caller dob \*\*\*\*-\*\*-14\n", output)


def test_second_configuration_replaces_the_first(logged: Callable[..., str]) -> None:
    output = logged(
        "import logging\n"
        "configure_logging(Settings())\n"
        "logging.getLogger('app.example').info('call_started')\n"
    )

    assert without_timestamps(output) == ["INFO app.example call_started"]


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put back the root level and the loggers that a test's configuration changes."""
    root = logging.getLogger()
    root_level = root.level
    state = [
        (logger, logger.level, logger.propagate, list(logger.handlers))
        for logger in map(logging.getLogger, (*UVICORN_LOGGERS, *WEBRTC_LOGGERS))
    ]
    try:
        yield
    finally:
        root.setLevel(root_level)
        for logger, level, propagate, handlers in state:
            logger.setLevel(level)
            logger.propagate = propagate
            logger.handlers[:] = handlers


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(
    ("log_level", "expected"),
    [("DEBUG", logging.WARNING), ("INFO", logging.WARNING), ("ERROR", logging.ERROR)],
)
def test_webrtc_stack_logs_warnings_only(log_level: LogLevel, expected: int) -> None:
    create_app(offline_settings(log_level=log_level))

    assert [logging.getLogger(name).level for name in WEBRTC_LOGGERS] == [expected, expected]


def uvicorn_config(log_level: LogLevel, log_config: dict[str, Any] | None) -> uvicorn.Config:
    """The app loaded as the container's uvicorn loads it: uvicorn's own logging set-up first,
    with its access log on, then the factory."""
    config = uvicorn.Config(
        app=lambda: create_app(offline_settings(log_level=log_level)),
        factory=True,
        log_config=log_config,
    )
    config.load()
    return config


def loguru_sink_handlers() -> list[object]:
    """The stdlib handler behind each of loguru's sinks, or the sink itself when it is not one.
    Read from loguru's internals (loguru 0.7.3, pinned), which has no public listing of sinks."""
    core = vars(loguru_logger)["_core"]
    sinks = [vars(handler)["_sink"] for handler in vars(core)["handlers"].values()]
    return [vars(sink).get("_handler", sink) for sink in sinks]


@pytest.mark.usefixtures("restore_logging")
def test_every_handler_masks() -> None:
    uvicorn_config("INFO", LOGGING_CONFIG)

    loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
    handlers = [
        handler
        for logger in loggers
        if isinstance(logger, logging.Logger)
        for handler in logger.handlers
    ]
    sinks = loguru_sink_handlers()

    # uvicorn's default handler is among them.
    assert any(handler in logging.getLogger("uvicorn").handlers for handler in handlers)
    assert [h for h in handlers if not isinstance(h.formatter, MaskingFormatter)] == []
    assert len(sinks) == 1
    assert [
        sink
        for sink in sinks
        if not (isinstance(sink, logging.Handler) and isinstance(sink.formatter, MaskingFormatter))
    ] == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("log_config", [LOGGING_CONFIG, None], ids=["uvicorn-logging", "none"])
@pytest.mark.parametrize(
    ("log_level", "expected"),
    [("DEBUG", logging.DEBUG), ("WARNING", logging.WARNING), ("ERROR", logging.ERROR)],
)
def test_uvicorn_loggers_follow_the_level(
    log_level: LogLevel, expected: int, log_config: dict[str, Any] | None
) -> None:
    # As a fresh process has it: the access logger passes its records up to the root.
    logging.getLogger("uvicorn.access").propagate = True

    config = uvicorn_config(log_level, log_config)

    assert {name: logging.getLogger(name).level for name in UVICORN_LOGGERS} == dict.fromkeys(
        UVICORN_LOGGERS, expected
    )
    assert logging.getLogger().level == expected
    # uvicorn decides per connection whether to write the access log: only if the access
    # logger has a handler, on it or above it.
    loop = asyncio.new_event_loop()
    try:
        protocol = H11Protocol(config, ServerState(), {}, _loop=loop)
    finally:
        loop.close()
    assert protocol.access_log is False
