import ast
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Three digits, two digits, four digits, not part of a longer run of digits. Matched on raw
# bytes with NUL bytes removed, so UTF-16 text is caught as well as UTF-8.
SSN_SHAPE = re.compile(rb"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")

# Every way SQLAlchemy builds an engine. Only ENGINE_MODULE may use them, so that every engine
# gets the same safety options.
ENGINE_CONSTRUCTORS = {
    "create_engine",
    "create_async_engine",
    "engine_from_config",
    "async_engine_from_config",
    "create_mock_engine",
}
ENGINE_MODULE = "app/db/session.py"

# The marker names are assembled here so that this file does not match its own scan.
SKIP = "sk" + "ip"
XFAIL = "x" + "fail"
SKIP_OR_XFAIL = re.compile(
    rf"\bmark\.(?:{SKIP}|{SKIP}if|{XFAIL})\b"
    rf"|\bpytest\.(?:{SKIP}|{XFAIL}|importor{SKIP})\b"
    rf"|\bunittest\.{SKIP}"
)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    assert executable is not None, "git must be on PATH"
    return subprocess.run(
        [executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def check_ignore(*args: str) -> subprocess.CompletedProcess[str]:
    return git("check-ignore", *args, ".env")


def tracked_files() -> list[Path]:
    listing = git("ls-files", "-z")
    assert listing.returncode == 0, listing.stderr
    files = [REPO_ROOT / name for name in listing.stdout.split("\0") if name]
    assert files, "git ls-files returned nothing"
    return files


def files_matching(pattern: re.Pattern[str], suffix: str | None = None) -> list[str]:
    return [
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if (suffix is None or path.suffix == suffix)
        and pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    ]


def test_env_file_is_gitignored() -> None:
    # Exit 0 from the quiet form means ignored. The verbose form also exits 0 when the
    # matching pattern is a negation, so it cannot be used for the decision.
    decision = check_ignore("--quiet")
    assert decision.returncode == 0, decision.stderr

    # The rule must come from the repo's own .gitignore, not a developer's global excludes.
    # Verbose output is "<source>:<line>:<pattern>\t<path>".
    match = check_ignore("--verbose").stdout.split("\t")[0]
    source, _line, pattern = match.split(":", 2)
    assert source == ".gitignore", match
    assert not pattern.startswith("!")


def without_nul(data: bytes) -> bytes:
    return data.replace(b"\x00", b"")


def test_no_ssn_shaped_literals_in_repo() -> None:
    sample = "-".join(["9" * 3, "8" * 2, "7" * 4])
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
        assert SSN_SHAPE.search(without_nul(f"ssn: {sample}".encode(encoding))), encoding
    assert not SSN_SHAPE.search(b"phone 555-201-3344, date 2026-12-03")

    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if SSN_SHAPE.search(without_nul(path.read_bytes()))
    ]
    assert offenders == []


def test_no_skip_or_xfail_markers() -> None:
    for sample in (
        f"@pytest.mark.{SKIP}",
        f"@pytest.mark.{SKIP}if(True, reason='x')",
        f"@pytest.mark.{XFAIL}",
        f"pytest.{SKIP}('x')",
        f"pytest.importor{SKIP}('x')",
        f"@unittest.{SKIP}('x')",
    ):
        assert SKIP_OR_XFAIL.search(sample), sample

    assert files_matching(SKIP_OR_XFAIL, suffix=".py") == []


def engine_constructor_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            uses += [alias.name for alias in node.names if alias.name in ENGINE_CONSTRUCTORS]
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ENGINE_CONSTRUCTORS:
                uses.append(name)
    return uses


def test_engines_are_built_only_in_the_session_module() -> None:
    assert engine_constructor_uses("import sqlalchemy as sa\nsa.create_engine('x')\n")
    assert engine_constructor_uses("from sqlalchemy import create_engine as make\n")

    users = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if path.suffix == ".py" and engine_constructor_uses(path.read_text(encoding="utf-8"))
    }

    assert users == {ENGINE_MODULE}


# Ways to reach the driver connection under SQLAlchemy, or to open one without it. Errors from
# such a connection bypass the engine's scrubbing, so only these files may use them.
RAW_CONNECTION_ACCESS = {
    "raw_connection",
    "get_raw_connection",
    "driver_connection",
    "dbapi_connection",
}
DRIVER_CONNECTION_CLASSES = {"Connection", "AsyncConnection"}
# CI's check of the app container (not shipped in the image) opens plain logins on purpose:
# it tries, as an intruder would, every value the container holds as the owner's password.
PROCESS_CHECK = "scripts/check_app_processes.py"
RAW_CONNECTION_ALLOWED = {ENGINE_MODULE, "tests/conftest.py", PROCESS_CHECK}


def raw_connection_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in RAW_CONNECTION_ACCESS:
            uses.append(node.attr)
        elif isinstance(node, ast.Attribute) and node.attr == "connect":
            target = node.value
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name == "psycopg" or name in DRIVER_CONNECTION_CLASSES:
                uses.append(f"{name}.connect")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("psycopg"):
            uses += [
                alias.name
                for alias in node.names
                if alias.name == "connect" or alias.name in DRIVER_CONNECTION_CLASSES
            ]
    return uses


def test_raw_driver_connections_only_in_allowed_modules() -> None:
    for sample in (
        "engine.raw_connection()\n",
        "await connection.get_raw_connection()\n",
        "connection.connection.driver_connection\n",
        "record.dbapi_connection\n",
        "import psycopg\npsycopg.connect('x')\n",
        "from psycopg import AsyncConnection\n",
        "await psycopg.AsyncConnection.connect('x')\n",
        "from psycopg import connect as open_it\n",
    ):
        assert raw_connection_uses(sample), sample
    assert not raw_connection_uses("from psycopg import Error, errors\nengine.connect()\n")

    users = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if path.suffix == ".py" and raw_connection_uses(path.read_text(encoding="utf-8"))
    }

    assert users <= RAW_CONNECTION_ALLOWED, users - RAW_CONNECTION_ALLOWED


# app/audit/ is the only code that writes or verifies the audit log. Everywhere else under app/
# the model may not be named and the table's name may not appear in a string (SQL text, a
# metadata lookup). The model's own definition, the migrations that create the table and the
# runtime role's privilege declaration (which names every table) are the only other places that
# name them. Like the trigger scan below, this is a lint against accidents: a name assembled at
# runtime gets past it. The control is the database role.
AUDIT_MODEL = "AuditEvent"
AUDIT_TABLE = "audit_events"
AUDIT_PACKAGE = "app/audit/"
AUDIT_NAMED_IN = ("app/db/models.py", "app/db/migrations/versions/", "app/db/grants.py")


def audit_table_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == AUDIT_MODEL:
            uses.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr == AUDIT_MODEL:
            uses.append(f".{node.attr}")
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] == AUDIT_MODEL:
            uses.append(f"import {node.name}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and AUDIT_TABLE in node.value.lower()
        ):
            uses.append(repr(node.value))
    return uses


def test_audit_table_is_written_only_by_the_audit_module() -> None:
    for sample in (
        "from app.db.models import AuditEvent\n",
        "from app.db.models import AuditEvent as Row\n",
        "from app.db import models\ninsert(models.AuditEvent)\n",
        "session.add(AuditEvent(seq=1))\n",
        "text('INSERT INTO audit_events VALUES (1)')\n",
        "text('insert into AUDIT_EVENTS values (1)')\n",
        "Base.metadata.tables['audit_events']\n",
    ):
        assert audit_table_uses(sample), sample
    assert not audit_table_uses("from app.audit.log import AuditLog, Event\n")

    sources = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in tracked_files()
        if path.suffix == ".py" and path.relative_to(REPO_ROOT).as_posix().startswith("app/")
    }
    users = {name for name, source in sources.items() if audit_table_uses(source)}

    assert "app/audit/log.py" in users
    outside = {
        name
        for name in users
        if not name.startswith(AUDIT_PACKAGE) and not name.startswith(AUDIT_NAMED_IN)
    }
    assert outside == set()


# append refuses to run inside a nested transaction, but a savepoint written as SQL text is
# invisible to it: `SAVEPOINT s`, append, `ROLLBACK TO SAVEPOINT s`, commit stores the action
# without its event. So no string under app/ may hold either statement; nested transactions go
# through SQLAlchemy, where append sees them. A lint against accidents, like the scans around it.
RAW_SAVEPOINT = re.compile(r"\bsavepoint\b|\brollback\s+to\b", re.IGNORECASE)


def raw_savepoint_uses(source: str) -> list[str]:
    return [
        repr(node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and RAW_SAVEPOINT.search(node.value)
    ]


def test_no_raw_savepoints_under_app() -> None:
    for sample in (
        "await session.execute(text('SAVEPOINT s'))\n",
        "await session.execute(text('rollback to savepoint s'))\n",
        "connection.exec_driver_sql('Rollback  To s')\n",
        "sql = 'RELEASE SAVEPOINT s'\n",
    ):
        assert raw_savepoint_uses(sample), sample
    assert not raw_savepoint_uses("async with session.begin_nested():\n    pass\n")
    assert not raw_savepoint_uses("await session.rollback()\n")

    offenders = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if path.suffix == ".py"
        and path.relative_to(REPO_ROOT).as_posix().startswith("app/")
        and raw_savepoint_uses(path.read_text(encoding="utf-8"))
    }
    assert offenders == set()


# Two ways to switch off a table's triggers by name. Only the tamper fixture in tests/conftest.py
# may use them, and tests/test_roles.py and tests/test_role_routes.py, which show the runtime
# role is refused them, by its privileges and by role set-up. A lint
# against accidents, not a control: the table's owner can still drop the triggers, and a string
# assembled at runtime is not seen. The names are assembled here so that this file does not
# match its own scan.
TRIGGER_BYPASS = re.compile(
    "session_" + "replication_role" + r"|disable\s+" + "trigger", re.IGNORECASE
)


def test_trigger_bypass_only_in_the_tamper_fixture() -> None:
    for sample in (
        "SET LOCAL session_" + "replication_role = replica",
        "set SESSION_" + "REPLICATION_ROLE to replica",
        "ALTER TABLE audit_events DISABLE " + "TRIGGER ALL",
        "alter table audit_events disable  " + "trigger audit_events_refuse_update",
    ):
        assert TRIGGER_BYPASS.search(sample), sample

    assert set(files_matching(TRIGGER_BYPASS)) == {
        "tests/conftest.py",
        "tests/test_roles.py",
        "tests/test_role_routes.py",
    }


# The owner's settings class, engines and URL. Outside the tests only the modules that define
# them (settings, session) and the start-up steps (migrations, role set-up, seed) may name them;
# the application runs as the runtime role, and its container is never given the owner's
# variables (deploy/docker-compose.yml). Like the scans above, a lint against accidents:
# the runtime role's privileges are the control.
OWNER_NAMES = {
    "StartupSettings",
    "create_owner_engine",
    "create_owner_sync_engine",
    "owner_database_url",
}
STARTUP_MODULES = {"app/db/migrations/env.py", "app/db/roles.py", "app/db/seed.py"}
# The process check names the start-up settings to know which variables are the owner's.
OWNER_ALLOWED = {"app/config.py", ENGINE_MODULE, PROCESS_CHECK, *STARTUP_MODULES}


def owner_engine_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in OWNER_NAMES:
            uses.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in OWNER_NAMES:
            uses.append(f".{node.attr}")
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in OWNER_NAMES:
            uses.append(f"import {node.name}")
    return uses


def test_owner_engine_only_in_startup_modules() -> None:
    for sample in (
        "from app.db.session import create_owner_engine\n",
        "from app.db.session import create_owner_sync_engine as make\n",
        "from app.db import session\nsession.create_owner_engine(settings)\n",
        "engine = create_owner_engine(settings)\n",
        "url = settings.owner_database_url\n",
        "getter = create_owner_sync_engine\n",
        "from app.config import StartupSettings\n",
        "from app import config\nconfig.StartupSettings()\n",
    ):
        assert owner_engine_uses(sample), sample
    assert not owner_engine_uses(
        "from app.config import Settings\n"
        "from app.db.session import create_database_engine\n"
        "engine = create_database_engine(settings)\n"
        "url = settings.runtime_database_url\n"
    )

    users = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if path.suffix == ".py"
        and not path.relative_to(REPO_ROOT).as_posix().startswith("tests/")
        and owner_engine_uses(path.read_text(encoding="utf-8"))
    }

    # Every start-up step reaches the owner through these names, so the scan sees them.
    assert users >= STARTUP_MODULES
    assert users <= OWNER_ALLOWED, users - OWNER_ALLOWED


# app/domain/ decides office hours and identity from its arguments alone: no database, no
# network, no settings or other files. Everything it may import is listed; anything else fails.
DOMAIN_PACKAGE = "app.domain"
DOMAIN_DIR = REPO_ROOT / "app" / "domain"
DOMAIN_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "re",
    "typing",
    "unicodedata",
    "zoneinfo",
    "jellyfish",
    DOMAIN_PACKAGE,
}
# Builtins that read files, run code, or reach modules and attributes by name.
DOMAIN_FORBIDDEN_CALLS = {
    "open",
    "__import__",
    "exec",
    "eval",
    "compile",
    "getattr",
    "globals",
    "vars",
}
# Names that reach other modules, the environment or the builtins through an allowed module:
# as an attribute (dataclasses.sys), as a name, or as a from-import (from typing import sys).
DOMAIN_FORBIDDEN_NAMES = {
    "sys",
    "os",
    "modules",
    "environ",
    "__builtins__",
    "__import__",
    "importlib",
    "__loader__",
    "__spec__",
    "__dict__",
    "__getattribute__",
    "__subclasses__",
    "__class__",
    "__base__",
    "inspect",
    "linecache",
    "breakpoint",
}


def imported_modules(source: str, package: str) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level:
            parts = package.split(".")
            base = parts[: len(parts) - node.level + 1]
            modules.append(".".join([*base, *([node.module] if node.module else [])]))
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    return modules


def forbidden_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in DOMAIN_FORBIDDEN_CALLS:
            uses.append(f"{getattr(node.func, 'id', '')}()")
        elif isinstance(node, ast.Name) and node.id in DOMAIN_FORBIDDEN_NAMES:
            uses.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in DOMAIN_FORBIDDEN_NAMES:
            uses.append(f".{node.attr}")
        elif isinstance(node, ast.ImportFrom):
            uses += [alias.name for alias in node.names if alias.name in DOMAIN_FORBIDDEN_NAMES]
    return uses


def impure_imports(source: str, package: str = DOMAIN_PACKAGE) -> list[str]:
    disallowed = [
        module
        for module in imported_modules(source, package)
        if not any(
            module == allowed or module.startswith(f"{allowed}.")
            for allowed in DOMAIN_ALLOWED_IMPORTS
        )
    ]
    return disallowed + forbidden_uses(source)


def test_domain_is_pure() -> None:
    for sample in (
        "import sqlalchemy\n",
        "from sqlalchemy.ext.asyncio import AsyncSession\n",
        "import psycopg\n",
        "from app.db.models import Patient\n",
        "from app.db import session\n",
        "from app.config import Settings\n",
        "from app import config\n",
        "from .. import config\n",
        "from ..db.session import create_database_engine\n",
        "import pydantic_settings\n",
        "from dotenv import load_dotenv\n",
        "import os\n",
        "from pathlib import Path\n",
        "import socket\n",
        "import urllib.request\n",
        "import httpx2\n",
        "import asyncio\n",
        "import importlib\n",
        "open('settings.toml')\n",
        "__import__('socket')\n",
        "import collections\n",
        # Reaching other modules, the environment or the builtins through allowed modules.
        "import dataclasses\ndataclasses.sys.modules['app.config'].Settings()\n",
        "import typing\ntyping.__builtins__['open']('.env')\n",
        "from dataclasses import sys\n",
        "import zoneinfo\nzoneinfo._tzpath.os.environ['APP_ENV']\n",
        "import typing\ngetattr(typing, 'sys')\n",
        "globals()['__builtins__']\n",
        "import typing\nvars(typing)['sys']\n",
        "exec(compile('import socket', 'x', 'exec'))\n",
        "import re\nre.importlib\n",
        "typing.sys.modules['os'].environ\n",
        "dataclasses.inspect.os.environ\n",
        "getattr(__builtins__, 'open', None)\n",
        "import typing\ntyping.__loader__.load_module('os')\n",
        "import re\nre.__spec__.loader.exec_module(re)\n",
        "import datetime\ndatetime.__dict__['__builtins__']\n",
        "import typing\nobject.__getattribute__(typing, 'sys')\n",
        "().__class__.__base__.__subclasses__()\n",
        "import dataclasses\ndataclasses.inspect.getmodule(dataclasses)\n",
        "import typing\ntyping.linecache.getlines('.env')\n",
        # The re-review's six bypasses, verbatim.
        "breakpoint()\n",
        "__loader__.get_data('.env')\n",
        "[c for c in ().__class__.__base__.__subclasses__()"
        " if c.__name__ == 'FileIO'][0]('.env')\n",
        "dataclasses.inspect.linecache.getlines('.env')\n",
        "typing.__dict__['s'+'ys']\n",
        "typing.__getattribute__('s'+'ys')\n",
        # Caught only by the bare-name ban: the attribute (open) is not banned.
        "__builtins__.open('.env')\n",
    ):
        assert impure_imports(sample), sample
    assert not impure_imports(
        "from __future__ import annotations\n"
        "from collections.abc import Iterable\n"
        "from datetime import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "import jellyfish\n"
        "import re\n"
        "WORD = re.compile('[a-z]+')\n"
        "from app.domain.clinic import CLINIC_TZ\n"
        "from .clinic import CLINIC_TZ\n"
        "from . import clinic\n"
    )

    sources = sorted(DOMAIN_DIR.rglob("*.py"))
    assert {path.name for path in sources} >= {"clinic.py", "office_hours.py", "matching.py"}

    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): found
        for path in sources
        if (found := impure_imports(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


# Log output is configured in app/guard/logging.py only, where every handler gets the masking
# formatter: no other file may configure logging, build or add a handler or formatter, or add or
# remove loguru's sinks. Tests included. A lint against accidents, like the scans above: a name
# reached another way gets past it.
LOGGING_MODULE = "app/guard/logging.py"
LOGGING_SETUP_CALLS = {
    "basicConfig",
    "dictConfig",
    "fileConfig",
    "addHandler",
    "removeHandler",
    "setFormatter",
}
LOGGING_SETUP_SUFFIXES = ("Handler", "Formatter")
LOGURU_SETUP_CALLS = {"add", "remove", "configure"}


def is_loguru_logger(node: ast.expr, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "logger"
        and isinstance(node.value, ast.Name)
        and node.value.id == "loguru"
    )


def logging_setup_uses(source: str) -> list[str]:
    tree = ast.parse(source)
    loguru_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "loguru"
        for alias in node.names
        if alias.name == "logger"
    }
    uses = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
            if name in LOGGING_SETUP_CALLS or name.endswith(LOGGING_SETUP_SUFFIXES):
                uses.append(name)
            elif (
                isinstance(node.func, ast.Attribute)
                and name in LOGURU_SETUP_CALLS
                and is_loguru_logger(node.func.value, loguru_names)
            ):
                uses.append(f"loguru.{name}")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = getattr(base, "id", None) or getattr(base, "attr", None) or ""
                if name.endswith(LOGGING_SETUP_SUFFIXES):
                    uses.append(f"class {node.name}")
    return uses


def test_logging_is_configured_in_one_module() -> None:
    for sample in (
        "import logging\nlogging.basicConfig(level='INFO')\n",
        "from logging.config import dictConfig\ndictConfig({'version': 1})\n",
        "import logging.config\nlogging.config.fileConfig('alembic.ini')\n",
        "handler = logging.StreamHandler()\n",
        "from logging import FileHandler\nFileHandler('calls.log')\n",
        "logging.Formatter('%(message)s')\n",
        "logging.getLogger('app').addHandler(handler)\n",
        "logging.getLogger().removeHandler(handler)\n",
        "handler.setFormatter(formatter)\n",
        "class Plain(logging.Formatter):\n    pass\n",
        "class Quiet(Handler):\n    pass\n",
        "from loguru import logger\nlogger.add(sys.stderr)\n",
        "from loguru import logger as log\nlog.remove()\n",
        "import loguru\nloguru.logger.add(sys.stderr, level='DEBUG')\n",
        "from loguru import logger\nlogger.configure(handlers=[])\n",
    ):
        assert logging_setup_uses(sample), sample
    assert not logging_setup_uses(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "logger.info('call_started call_id=%s', call_id)\n"
        "seen.add(call_id)\n"
        "pending.remove(call_id)\n"
        "transport.add_event_handler('on_client_connected', handler)\n"
        "from loguru import logger as pipecat_logger\n"
        "pipecat_logger.warning('x')\n"
    )

    for sample in (
        "logger.handlers = []\n",
        "logging.getLogger('app').handlers[:] = [handler]\n",
        "logger.propagate = False\n",
        "logger.handlers.append(handler)\n",
        "logging.getLogger().handlers.insert(0, handler)\n",
        "del logger.handlers[0]\n",
    ):
        assert logger_state_uses(sample), sample
    assert not logger_state_uses(
        "handlers = []\nhandlers.append(x)\nif logger.propagate:\n    pass\n"
    )

    users = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if path.suffix == ".py" and logging_setup_uses(path.read_text(encoding="utf-8"))
    }
    # Changing a logger's handlers or whether it passes records up, under app/.
    state_users = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in tracked_files()
        if path.suffix == ".py"
        and path.relative_to(REPO_ROOT).as_posix().startswith("app/")
        and logger_state_uses(path.read_text(encoding="utf-8"))
    }

    assert users == {LOGGING_MODULE}
    assert state_users == {LOGGING_MODULE}


LOGGER_STATE = {"handlers", "propagate"}
HANDLER_LIST_CALLS = {"append", "extend", "insert"}


def logger_state_uses(source: str) -> list[str]:
    uses = []
    for node in ast.walk(ast.parse(source)):
        changed = isinstance(getattr(node, "ctx", None), ast.Store | ast.Del)
        if changed and isinstance(node, ast.Attribute) and node.attr in LOGGER_STATE:
            uses.append(f".{node.attr} =")
        elif (
            changed
            and isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "handlers"
        ):
            uses.append(".handlers[...] =")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in HANDLER_LIST_CALLS
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "handlers"
        ):
            uses.append(f".handlers.{node.func.attr}")
    return uses


# Ways to write to the console without logging, so without masking. app/config.py's main
# reports settings errors (field names and fixed messages, never values) before any logging
# configuration exists; that one use is named here.
TRACEBACK_PRINTERS = {"print_exc", "print_exception", "print_stack"}
CONSOLE_STREAMS = {"stdout", "stderr"}
DIRECT_OUTPUT_ALLOWED = {("app/config.py", "sys.stderr.write", "main")}


def direct_output(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import | ast.ImportFrom):
        modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
        if "pprint" in modules or getattr(node, "module", None) == "pprint":
            return "pprint"
        return None
    if not isinstance(node, ast.Call):
        return None
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if isinstance(node.func, ast.Name) and name == "print":
        return "print"
    if name in TRACEBACK_PRINTERS:
        return f"traceback.{name}"
    stream = node.func.value if isinstance(node.func, ast.Attribute) else None
    if (
        name == "write"
        and isinstance(stream, ast.Attribute)
        and stream.attr in CONSOLE_STREAMS
        and getattr(stream.value, "id", None) == "sys"
    ):
        return f"sys.{stream.attr}.write"
    return None


def direct_output_uses(source: str) -> list[tuple[str, str]]:
    """Each direct write to the console, with the name of the function it is in."""
    uses = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else function
            )
            found = direct_output(child)
            if found is not None:
                uses.append((found, inner))
            visit(child, inner)

    visit(ast.parse(source), "")
    return uses


def test_no_print_in_app() -> None:
    for sample in (
        "print('call started')\n",
        "def f() -> None:\n    print()\n",
        "import traceback\ntraceback.print_exc()\n",
        "from traceback import print_exception\nprint_exception(error)\n",
        "traceback.print_stack()\n",
        "import pprint\n",
        "from pprint import pp\n",
        "sys.stdout.write('x')\n",
        "sys.stderr.write('x')\n",
    ):
        assert direct_output_uses(sample), sample
    assert not direct_output_uses(
        "logger.info('x')\nprinter.print('x')\nfile.write('x')\nbuffer.stdout.write('x')\n"
    )
    assert direct_output_uses("def main() -> None:\n    sys.stderr.write('x')\n") == [
        ("sys.stderr.write", "main")
    ]

    found = {
        path.relative_to(REPO_ROOT).as_posix(): direct_output_uses(path.read_text(encoding="utf-8"))
        for path in tracked_files()
        if path.suffix == ".py" and path.relative_to(REPO_ROOT).as_posix().startswith("app/")
    }
    offenders = {
        name: [use for use in uses if (name, *use) not in DIRECT_OUTPUT_ALLOWED]
        for name, uses in found.items()
    }

    assert {name: uses for name, uses in offenders.items() if uses} == {}
    # The named exception is still there; if it goes, so should its entry.
    assert ("sys.stderr.write", "main") in found["app/config.py"]
