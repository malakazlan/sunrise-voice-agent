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
# Builtins that read files or load modules by name.
DOMAIN_FORBIDDEN_CALLS = {"open", "__import__", "exec", "eval"}


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
        elif (
            isinstance(node, ast.Call) and getattr(node.func, "id", None) in DOMAIN_FORBIDDEN_CALLS
        ):
            modules.append(f"{getattr(node.func, 'id', '')}()")
    return modules


def impure_imports(source: str, package: str = DOMAIN_PACKAGE) -> list[str]:
    return [
        module
        for module in imported_modules(source, package)
        if not any(
            module == allowed or module.startswith(f"{allowed}.")
            for allowed in DOMAIN_ALLOWED_IMPORTS
        )
    ]


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
    ):
        assert impure_imports(sample), sample
    assert not impure_imports(
        "from __future__ import annotations\n"
        "from collections.abc import Iterable\n"
        "from datetime import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "import jellyfish\n"
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
