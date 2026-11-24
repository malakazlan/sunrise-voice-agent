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
RAW_CONNECTION_ALLOWED = {ENGINE_MODULE, "tests/conftest.py"}


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
# metadata lookup). The model's own definition and the migrations that create the table are
# the only other places that name them. Like the trigger scan below, this is a lint against
# accidents: a name assembled at runtime gets past it. The control is the database role.
AUDIT_MODEL = "AuditEvent"
AUDIT_TABLE = "audit_events"
AUDIT_PACKAGE = "app/audit/"
AUDIT_NAMED_IN = ("app/db/models.py", "app/db/migrations/versions/")


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
# may use the first; nothing may use the second. A lint against accidents, not a control: the
# table's owner can still drop the triggers, and a string assembled at runtime is not seen. The
# names are assembled here so that this file does not match its own scan.
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

    assert set(files_matching(TRIGGER_BYPASS)) == {"tests/conftest.py"}


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
