"""The config the voice service runs on: read from config_changes at start-up and every three
seconds into memory, and read by call handling from memory only. The kill switch refuses new
calls, and audits the refusals at most once a minute per reason; the office override changes the
answer for calls that start after it."""

import ast
import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from admin_client import (
    ADMIN_USER,
    OWN_ORIGIN,
    PASSWORD,
    Clock,
    admin_settings,
    admin_with_clock,
    client_from,
    events,
    try_sign_in,
)
from conftest import TEST_AUDIT_KEY, TEST_IDENTITY_KEY, VOICE_SECRETS, offline_settings
from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
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
from app.api.offer import CallRefusals, build_router
from app.audit.log import AuditLog
from app.audit.queue import AuditQueue
from app.config import StartupSettings
from app.db.session import create_database_engine, create_session_factory
from app.main import create_app
from app.runtime_config import (
    READ_SECONDS,
    ConfigPoll,
    LiveConfig,
    Values,
    read_config_bounded,
    taking_calls,
)
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
# In the stalled-database test a read of the database lasts STALL_SECONDS before it is given
# up, and a call site that waited on one would take at least that long; one that waits on
# nothing returns within a quarter of it, however busy the machine. The poll reads often.
STALL_SECONDS = 2.0
RETURN_BOUND_SECONDS = STALL_SECONDS / 4
FAST_POLL_SECONDS = 0.05
# How long a new thread takes to start on a machine that is very busy.
SLOW_THREAD_START_SECONDS = 0.3
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


def without_waiting[Result](step: Coroutine[Any, Any, Result]) -> Result:
    """What the coroutine returns, run in one step: it must end without suspending once, so
    it never waited on anything, the database included."""
    try:
        step.send(None)
    except StopIteration as ended:
        return cast(Result, ended.value)
    # Stopped mid-wait: a step that cleans up with another wait refuses to be closed.
    with contextlib.suppress(RuntimeError):
        step.close()
    raise AssertionError("the call site waited")


def route(router: APIRouter, path: str) -> APIRoute:
    [found] = [each for each in router.routes if isinstance(each, APIRoute) and each.path == path]
    return found


def offer_request() -> Request:
    """An offer as /offer's handler is given it; with calls off it is refused unread."""
    return Request({"type": "http", "method": "POST", "path": "/offer", "headers": []})


def next_read(client: TestClient, app: FastAPI) -> None:
    """The app's config poll's next read, made now, on the app's own loop."""
    assert client.portal is not None
    client.portal.call(app.state.poll.read)


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
        # Through the table and the poll, even in one process: the poll's next read applies it
        # (its cadence is test_changes_apply_within_the_seconds_promised's).
        next_read(client, app)
        assert config.values == Values(calls="off", office="schedule")

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
        next_read(client, app)
        assert config.values == Values(calls="on", office="schedule")
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
        """A call starts, and /offer answers, on the values in memory, without waiting on
        anything, while the poll's reads and the refusal's audit write go to the database
        `engine` reaches."""
        poll = ConfigPoll(
            create_session_factory(engine),
            live,
            every=FAST_POLL_SECONDS,
            read_seconds=STALL_SECONDS,
        )
        refusals = AuditQueue(create_session_factory(engine), AUDIT, now=lambda: CLOSED_AT)
        router = build_router(
            admin_settings(database),
            CallRegistry(max_calls=3, max_seconds=600),
            opener,
            live,
            CallRefusals(refusals),
        )
        app = FastAPI()
        app.include_router(router)
        deps = deps_with(engine, live, now=lambda: CLOSED_AT)
        try:
            await poll.start()
            # A read is given up, and the last good values stay.
            await asyncio.sleep(STALL_SECONDS + 0.5)
            assert live.values == Values(calls="off", office="open")
            # The call sites never wait: each runs to its end in one step, without once
            # handing the loop over, so it cannot have waited on the database or anything else.
            # A call starts on the values in memory: the office forced open at a closed hour.
            state, script = without_waiting(start_call(new_call_id(), deps))
            assert (state.office_open, script) == (True, ScriptId.GREETING)
            refused = without_waiting(route(router, "/offer").endpoint(offer_request()))
            assert (refused.status_code, json.loads(refused.body)) == (503, UNAVAILABLE)
            # And through the whole stack, in less time than one wait on the database.
            (state, script), took = await timed(start_call(new_call_id(), deps))
            assert (state.office_open, script) == (True, ScriptId.GREETING)
            assert took < RETURN_BOUND_SECONDS, took
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


@pytest.mark.anyio
async def test_call_routes_never_hold_the_loop(
    silent_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a busy machine a new thread is slow to start, and the thread that starts it waits
    # meanwhile. A route or a dependency handed to the thread pool would make the event loop
    # wait so, and every running call's audio with it; with the database stalled as well.
    started: list[str] = []
    start = threading.Thread.start

    def slow_start(thread: threading.Thread) -> None:
        started.append(thread.name)
        time.sleep(SLOW_THREAD_START_SECONDS)
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", slow_start)
    settings = offline_settings(
        postgres_host="127.0.0.1",
        postgres_port=silent_server,
        app_role="all",
        admin_user=ADMIN_USER,
        admin_password=PASSWORD,
    )
    app = create_app(settings, open_call=Opener(), config=taking_calls())
    worst = 0.0

    async def probe() -> None:
        nonlocal worst
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(0.005)
            worst = max(worst, loop.time() - before - 0.005)

    probing = asyncio.ensure_future(probe())
    token = {"Authorization": f"Bearer {VOICE_SECRETS['DEMO_ACCESS_TOKEN']}"}
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url=OWN_ORIGIN) as client:
            answers = [
                await client.get("/health"),
                await client.get("/"),
                await client.get("/ice-servers", headers=token),
                await client.post("/offer", json=OFFER, headers=token),
                await client.get("/admin/api/session"),
            ]
    finally:
        probing.cancel()
        # The app was never started: what its lifespan would close is closed here.
        await app.state.calls.close()
        await app.state.model.close()
    assert [answer.status_code for answer in answers] == [200, 200, 200, 200, 401]
    assert started == []
    assert worst < SLOW_THREAD_START_SECONDS / 2, worst


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


def test_session_reports_calls_and_the_effective_office(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # The clinic's clock at a closed hour. The admin process has no poll: it reads the table
    # when asked, in one short read for both keys.
    settings = admin_settings(database, simulated_now=CLOSED_AT.isoformat())
    app = admin_with_clock(settings, Clock())
    limits: list[float] = []

    async def timed_read(sessions: Any, seconds: float) -> Any:
        limits.append(seconds)
        return await read_config_bounded(sessions, seconds)

    monkeypatch.setattr(admin_module, "read_config_bounded", timed_read)

    def shown() -> tuple[object, object]:
        answer = client.get("/admin/api/session")
        assert answer.status_code == 200
        return answer.json()["calls"], answer.json()["office_open"]

    with client_from(app) as client:
        signed = try_sign_in(client)
        assert (signed.json()["calls"], signed.json()["office_open"]) == ("on", False)
        assert set(signed.json()) == {
            "user",
            "csrf_token",
            "expires_in",
            "calls",
            "office_open",
            "time_zone",
        }
        write = {"X-CSRF-Token": signed.json()["csrf_token"], "Origin": OWN_ORIGIN}
        assert shown() == ("on", False)
        assert client.post(CONFIG, json=change("office", "open"), headers=write).status_code == 201
        assert client.post(CONFIG, json=change("calls", "off"), headers=write).status_code == 201
        assert shown() == ("off", True)
        assert limits and set(limits) == {0.5}

        # Reads that fail still answer: calls unknown, the office on its hours; and the outage
        # is logged once, however many ask.
        async def fails(*_args: object, **_kwargs: object) -> None:
            raise OSError("down")

        monkeypatch.setattr(admin_module, "read_config_bounded", fails)
        caplog.clear()
        assert [shown() for _ in range(5)] == [(None, False)] * 5
        failed = [r for r in caplog.records if r.getMessage().startswith("admin_config_read")]
        assert [r.getMessage() for r in failed] == ["admin_config_read_failed error=OSError"]

        # Once it reads again, it says so once, and the next outage is logged again.
        monkeypatch.setattr(admin_module, "read_config_bounded", timed_read)
        assert [shown() for _ in range(2)] == [("off", True)] * 2
        monkeypatch.setattr(admin_module, "read_config_bounded", fails)
        assert shown() == (None, False)
    messages = [r.getMessage() for r in caplog.records if "admin_config_read" in r.getMessage()]
    assert messages == [
        "admin_config_read_failed error=OSError",
        "admin_config_read_recovered",
        "admin_config_read_failed error=OSError",
    ]


# Item 1 of the rework: refused calls do not flood the audit log.
@pytest.mark.anyio
async def test_refused_calls_are_audited_once_a_minute_per_reason(
    runtime: AsyncEngine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    clock = Clock()
    live = LiveConfig()
    queue = AuditQueue(create_session_factory(runtime), AUDIT, now=lambda: CLOSED_AT)
    refusals = CallRefusals(queue, clock=clock)
    app = FastAPI()
    settings = offline_settings(
        app_env="dev", demo_access_token=None, turn_host=None, turn_shared_secret=None
    )
    app.include_router(
        build_router(settings, CallRegistry(max_calls=3, max_seconds=600), Opener(), live, refusals)
    )

    def refused_rows() -> list[object]:
        return [row[7] for row in events(owner_engine) if row[3] == "call_refused"]

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=OWN_ORIGIN) as client:
        # 1,000 refusals inside a minute, for two reasons: one event each, at once.
        for _ in range(500):
            assert (await client.post("/offer", json=OFFER)).status_code == 503
        live.values = Values(calls="off", office="schedule")
        for _ in range(500):
            clock.now += 0.1
            assert (await client.post("/offer", json=OFFER)).status_code == 503
        await queue.drain()
        assert refused_rows() == ["config_unknown", "calls_off"]
        assert not [r for r in caplog.records if r.getMessage().startswith("calls_refused")]

        # A minute after the last event of its reason, the next one is audited, and the ones in
        # between are counted in one line.
        clock.now += 60
        assert (await client.post("/offer", json=OFFER)).status_code == 503
        await queue.drain()
    assert refused_rows() == ["config_unknown", "calls_off", "calls_off"]
    refusals.close()
    counted = [r.getMessage() for r in caplog.records if r.getMessage().startswith("calls_refused")]
    assert counted == [
        "calls_refused reason=calls_off count=499",
        "calls_refused reason=config_unknown count=499",
    ]


# Item 5 of the rework: the promise in the answer holds.
@pytest.mark.anyio
async def test_changes_apply_within_the_seconds_promised(runtime: AsyncEngine) -> None:
    slept: list[float] = []
    reads = asyncio.Event()

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            reads.set()
            await asyncio.Event().wait()

    poll = ConfigPoll(create_session_factory(runtime), LiveConfig(), sleep=sleep)
    await poll.start()
    await asyncio.wait_for(reads.wait(), timeout=5)
    await poll.stop()
    # A read every three seconds, each given up after one: a change written now is read within
    # four, under the five the admin API promises (`applies_within_seconds`).
    assert slept == [3, 3, 3]
    assert max(slept) + READ_SECONDS <= 5


# Item 6 of the rework: the config is never left out.
def test_deps_need_the_config() -> None:
    with pytest.raises(TypeError, match="config"):
        Deps(  # type: ignore[call-arg]
            sessions=None,  # type: ignore[arg-type]
            audit=AUDIT,
            now=lambda: OPEN_AT,
            claim_keys=ClaimKeys(SecretStr(TEST_IDENTITY_KEY)),
        )
