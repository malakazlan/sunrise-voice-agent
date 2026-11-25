"""The config the voice service runs on: read from config_changes at start-up and every five
seconds into memory, and read by call handling from memory only. The kill switch refuses new
calls; the office override changes the answer for calls that start after it."""

import ast
import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx2
import pytest
from admin_client import (
    OWN_ORIGIN,
    Clock,
    admin_settings,
    admin_with_clock,
    client_from,
    events,
    try_sign_in,
)
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_call_endings import CLOSED_AT, OPEN_AT, StallingProxy, new_call_id, timed
from test_offer import OFFER, Opener, active_calls

from app.agent.claims import ClaimKeys
from app.agent.endings import drain_call_events, start_call
from app.agent.scripts import ScriptId
from app.agent.tool_registry import Deps
from app.api import admin as admin_module
from app.api.offer import build_router
from app.audit.log import AuditLog
from app.audit.queue import AuditQueue
from app.config import StartupSettings
from app.db.session import create_database_engine, create_session_factory
from app.main import create_app
from app.runtime_config import ConfigPoll, LiveConfig, Values
from app.voice.calls import CallRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT = AuditLog(SecretStr(TEST_AUDIT_KEY))
CONFIG = "/admin/api/config"
UNAVAILABLE = {"reason": "unavailable"}
# What the call page says when the answer is that one, written out here by hand.
CALL_PAGE_LINE = (
    "The clinic's phone assistant is unavailable right now. Please call the front desk."
    " If this is an emergency, call 911."
)
# The refusal's event: the agent refused a call, for the kill switch, naming nothing.
REFUSED_EVENT = (None, "agent", "call_refused", "call", None, "refused", "calls_off")
# A call site that reads memory only returns well within this.
RETURN_BOUND_SECONDS = 0.2
# How often the poll reads in the stalled-database test: often, so that it fails many times.
FAST_POLL_SECONDS = 0.05
# The functions that read config_changes.
TABLE_READS = {"read_config", "read_config_bounded"}
INSERT_CHANGE = text(
    "INSERT INTO config_changes (key, value, changed_by) VALUES (:key, :value, 'clinic_admin')"
)


def wait_for(condition: Callable[[], bool], seconds: float) -> float:
    """Seconds until the condition held; fails after `seconds`."""
    started = time.monotonic()
    while not condition():
        assert time.monotonic() - started < seconds, "the condition never held"
        time.sleep(0.02)
    return time.monotonic() - started


def change(key: str, value: str) -> dict[str, object]:
    return {"key": key, "value": value, "confirm": True}


def deps_with(
    engine: AsyncEngine, config: LiveConfig, now: Callable[[], datetime] = lambda: OPEN_AT
) -> Deps:
    return Deps(
        sessions=create_session_factory(engine),
        audit=AUDIT,
        now=now,
        claim_keys=ClaimKeys(SecretStr(TEST_IDENTITY_KEY)),
        config=config,
    )


# R7
def test_kill_switch_refuses_new_calls_only(
    database: StartupSettings, owner_engine: Engine
) -> None:
    opener = Opener()
    app = create_app(admin_settings(database), open_call=opener)
    with client_from(app) as client:
        config: LiveConfig = app.state.config
        # Read at start-up: no row yet, so the defaults.
        assert config.values == Values(calls="on", office="schedule")
        assert client.post("/offer", json=OFFER).status_code == 200
        [running] = opener.calls

        signed = try_sign_in(client)
        write = {"X-CSRF-Token": signed.json()["csrf_token"], "Origin": OWN_ORIGIN}
        off = client.post(CONFIG, json=change("calls", "off"), headers=write)
        assert off.status_code == 201
        assert off.json()["applies_within_seconds"] == 5
        # Through the table and the poll, even in one process: within five seconds.
        took = wait_for(lambda: config.values == Values(calls="off", office="schedule"), 7)
        assert took <= 5.5, took

        refused = client.post("/offer", json=OFFER)
        assert (refused.status_code, refused.json()) == (503, UNAVAILABLE)
        assert opener.attempts == 1
        # The call already connected goes on.
        assert active_calls(app) == 1
        assert not running.stopped
        # The refusal is audited, in the background, with no caller data.
        wait_for(lambda: REFUSED_EVENT in [row[1:] for row in events(owner_engine)], 5)

        # The call page turns that answer into the clinic's line.
        page = client.get("/").text
        assert CALL_PAGE_LINE in page
        assert 'reason === "unavailable"' in page

        assert client.post(CONFIG, json=change("calls", "on"), headers=write).status_code == 201
        wait_for(lambda: config.values == Values(calls="on", office="schedule"), 7)
        assert client.post("/offer", json=OFFER).status_code == 200
        assert active_calls(app) == 2
    assert [row[1:] for row in events(owner_engine)].count(REFUSED_EVENT) == 1


# R8
@pytest.mark.anyio
async def test_calls_do_not_wait_for_config(
    database: StartupSettings, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with owner_engine.begin() as connection:
        connection.execute(INSERT_CHANGE, {"key": "calls", "value": "off"})
        connection.execute(INSERT_CHANGE, {"key": "office", "value": "open"})
    proxy = StallingProxy(database.postgres_host, database.postgres_port)
    port = await proxy.start()
    live = LiveConfig()
    opener = Opener()

    async def calls_go_on(engine: AsyncEngine) -> None:
        """A call starts, and /offer answers, at once, on the values in memory, while the
        poll's reads and the refusal's audit write go to the database `engine` reaches."""
        poll = ConfigPoll(create_session_factory(engine), live, every=FAST_POLL_SECONDS)
        refusals = AuditQueue(create_session_factory(engine), AUDIT, now=lambda: CLOSED_AT)
        app = FastAPI()
        registry = CallRegistry(max_calls=3, max_seconds=600)
        app.include_router(build_router(admin_settings(database), registry, opener, live, refusals))
        deps = deps_with(engine, live, now=lambda: CLOSED_AT)
        try:
            await poll.start()
            # The poll's reads are given up, again and again, and the last good values stay.
            await asyncio.sleep(2.5)
            assert live.values == Values(calls="off", office="open")
            # A call starts on the values in memory: the office forced open at a closed hour.
            (state, script), took = await timed(start_call(new_call_id(), deps))
            assert (state.office_open, script) == (True, ScriptId.GREETING)
            assert took < RETURN_BOUND_SECONDS, took
            # /offer refuses at once, without waiting on the refusal's audit write.
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=OWN_ORIGIN) as client:
                answer, took = await timed(client.post("/offer", json=OFFER))
            assert (answer.status_code, answer.json()) == (503, UNAVAILABLE)
            assert took < RETURN_BOUND_SECONDS, took
        finally:
            # The poll stops cleanly: it never raised, it only logged.
            await poll.stop()
            await refusals.drain(timeout=5)
            await drain_call_events()
            await engine.dispose()

    stalled = create_database_engine(
        database.model_copy(update={"postgres_host": "127.0.0.1", "postgres_port": port})
    )
    try:
        # Read at start-up through the relay; then the database stops answering.
        first = ConfigPoll(create_session_factory(stalled), live)
        await first.start()
        await first.stop()
        assert live.values == Values(calls="off", office="open")
        proxy.stall()
        await calls_go_on(stalled)
    finally:
        await stalled.dispose()
        await proxy.close()
    # Then it is down: nothing listens where it was.
    await calls_go_on(
        create_database_engine(
            database.model_copy(update={"postgres_host": "127.0.0.1", "postgres_port": port})
        )
    )
    assert opener.attempts == 0
    # Each poll said once that it could not read, however often it tried.
    failures = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("runtime_config_read_failed")
    ]
    assert len(failures) == 2, failures

    # Call handling has no way to the table: only the poll and the admin API (its reads, and
    # its change, which reads the value it replaces) read it.
    readers = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            named = {getattr(node, "id", None), getattr(node, "attr", None)}
            if isinstance(node, ast.Name | ast.Attribute) and named & TABLE_READS:
                readers.add(path.relative_to(REPO_ROOT).as_posix())
    assert readers == {"app/runtime_config.py", "app/api/admin.py", "app/api/writes.py"}


# R9
@pytest.mark.anyio
async def test_office_override(runtime: AsyncEngine) -> None:
    clock = {"now": OPEN_AT}
    live = LiveConfig(Values(calls="on", office="schedule"))
    deps = deps_with(runtime, live, now=lambda: clock["now"])

    async def office_open_at(at: datetime) -> bool:
        clock["now"] = at
        state, script = await start_call(new_call_id(), deps)
        assert script is (ScriptId.GREETING if state.office_open else ScriptId.CLOSED)
        return state.office_open

    try:
        # `schedule` is the clock's answer; `open` and `closed` force it.
        expected = {"schedule": (True, False), "open": (True, True), "closed": (False, False)}
        for office, answers in expected.items():
            live.values = Values(calls="on", office=office)
            assert (await office_open_at(OPEN_AT), await office_open_at(CLOSED_AT)) == answers

        # A running call keeps the answer it started with; the next call takes the new one.
        live.values = Values(calls="on", office="open")
        clock["now"] = CLOSED_AT
        running, _script = await start_call(new_call_id(), deps)
        live.values = Values(calls="on", office="closed")
        assert running.office_open
        assert not await office_open_at(CLOSED_AT)

        # Before the first read, the clock decides.
        live.values = None
        assert (await office_open_at(OPEN_AT), await office_open_at(CLOSED_AT)) == (True, False)
    finally:
        await drain_call_events()


def test_session_reports_the_effective_office(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The clinic's clock at a closed hour. The admin process has no poll: it reads the table
    # when asked.
    settings = admin_settings(database, simulated_now=CLOSED_AT.isoformat())
    app = admin_with_clock(settings, Clock())
    with client_from(app) as client:
        signed = try_sign_in(client)
        assert signed.json()["office_open"] is False
        write = {"X-CSRF-Token": signed.json()["csrf_token"], "Origin": OWN_ORIGIN}
        assert client.get("/admin/api/session").json()["office_open"] is False
        assert client.post(CONFIG, json=change("office", "open"), headers=write).status_code == 201
        assert client.get("/admin/api/session").json()["office_open"] is True

        # A read that fails still answers, with the schedule's answer.
        async def fails(*_args: object, **_kwargs: object) -> None:
            raise OSError("down")

        monkeypatch.setattr(admin_module, "read_config_bounded", fails)
        answer = client.get("/admin/api/session")
        assert (answer.status_code, answer.json()["office_open"]) == (200, False)
