"""The live calls stream: what it sends, when, for how long, to how many, and what it holds."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from admin_client import (
    ADMIN_USER,
    COOKIE,
    OWN_ORIGIN,
    PASSWORD,
    SESSION_ROUTE,
    STREAM_ROUTE,
    Clock,
    admin_events,
    admin_settings,
    admin_with_clock,
    append_events,
    call_started,
    cid,
    client_from,
)
from sqlalchemy import Engine, text

from app.api import live
from app.api.live import LiveStream, LiveStreams
from app.api.sessions import IDLE_SECONDS
from app.audit import console
from app.config import StartupSettings
from app.db.session import create_database_engine

T0 = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
# Ten minutes after the call started: it is live.
NOW = "2026-11-24T15:40:00Z"
INSERT_TURN = text(
    "INSERT INTO call_turns (call_id, turn, at, phase, verified, patient_id, guard_action,"
    " last_tool, tool_rounds, vad_ms, stt_ms, first_token_ms, last_token_ms, first_audio_ms,"
    " total_ms, record_us, dropped_before) VALUES (:call_id, 1, :at, 'intake', false, NULL,"
    " 'pass', NULL, 0, 500, 120, 300, 900, 1500, 1400, 40, 0)"
)
WITHOUT_TURN = {
    "call_id": cid(1),
    "started_at": "2026-11-24T15:30:00+00:00",
    "turn": None,
    "phase": None,
    "verified": None,
    "patient_id": None,
    "last_tool": None,
    "vad_ms": None,
    "stt_ms": None,
    "first_token_ms": None,
    "last_token_ms": None,
    "first_audio_ms": None,
    "total_ms": None,
}
WITH_TURN = {
    **WITHOUT_TURN,
    "turn": 1,
    "phase": "intake",
    "verified": False,
    "vad_ms": 500,
    "stt_ms": 120,
    "first_token_ms": 300,
    "last_token_ms": 900,
    "first_audio_ms": 1500,
    "total_ms": 1400,
}


def parsed(body: str) -> list[tuple[str, Any]]:
    """The stream as (event, data) pairs, a comment as ("comment", its text)."""
    found: list[tuple[str, Any]] = []
    assert body.endswith("\n\n")
    for block in body[:-2].split("\n\n"):
        lines = block.split("\n")
        if lines[0].startswith(":"):
            assert len(lines) == 1, block
            found.append(("comment", lines[0]))
            continue
        assert lines[0].startswith("event: "), block
        assert len(lines) == 2 and lines[1].startswith("data: "), block
        found.append((lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])))
    return found


class Exchange:
    """One request to the ASGI app, run in the test's own loop: its answer as it comes, and a
    disconnect the test sends when it chooses. With `stall`, the client stops reading after
    the first part of the body."""

    def __init__(self, app: Any, method: str, path: str, headers: dict[str, str], **options: Any):
        self.app = app
        self.method = method
        self.path = path
        self.headers = {"host": "testserver", **{k.lower(): v for k, v in headers.items()}}
        self.body: bytes = options.get("body", b"")
        self.stall: bool = options.get("stall", False)
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.ended = False
        self.started = asyncio.Event()
        self.gone = asyncio.Event()

    async def run(self) -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": self.method,
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in self.headers.items()],
            "client": ("192.0.2.70", 50000),
            "server": ("testserver", 80),
        }
        requested = False

        async def receive() -> dict[str, Any]:
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": self.body, "more_body": False}
            await self.gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.response_headers = {k.decode(): v.decode() for k, v in message["headers"]}
                self.started.set()
                return
            if self.stall and self.chunks:
                await asyncio.Event().wait()
            if message.get("body"):
                self.chunks.append(message["body"])
            if not message.get("more_body", False):
                self.ended = True

        await self.app(scope, receive, send)

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode()


async def asgi_sign_in(app: Any, origin: str = OWN_ORIGIN) -> str:
    exchange = Exchange(
        app,
        "POST",
        SESSION_ROUTE,
        {"origin": origin, "content-type": "application/json"},
        body=json.dumps({"user": ADMIN_USER, "password": PASSWORD}).encode(),
    )
    await exchange.run()
    assert exchange.status == 200
    cookie = exchange.response_headers["set-cookie"]
    return cookie.split(";", 1)[0].split("=", 1)[1]


def stream_of(app: Any, session_id: str, origin: str | None = None, **options: Any) -> Exchange:
    headers = {"cookie": f"{COOKIE}={session_id}"}
    if origin is not None:
        headers["origin"] = origin
    return Exchange(app, "GET", STREAM_ROUTE, headers, **options)


async def yielding_sleep(_seconds: float) -> None:
    """Time does not move: the streams run until the test ends them."""
    await asyncio.sleep(0.001)


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.001)


# R5
def test_stream_bounds(
    database: StartupSettings, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    append_events(database, [call_started(1)], T0)
    settings = admin_settings(database, simulated_now=NOW)

    # Five minutes of a stream, on the test's clock: what it sends, and when it reads.
    clock = Clock()
    engine = create_database_engine(settings)
    reads: list[float] = []
    held: list[int] = []
    failing = False
    original = console.live_calls

    async def counted(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        reads.append(clock.now)
        if failing:
            raise ConnectionError("the database is away")
        return await original(*args, **kwargs)

    monkeypatch.setattr(console, "live_calls", counted)

    async def sleep(seconds: float) -> None:
        nonlocal failing
        # Between ticks no connection is checked out of the engine.
        held.append(engine.pool.checkedout())  # type: ignore[attr-defined]
        await clock.sleep(seconds)
        if clock.now == 1010:
            with owner_engine.begin() as connection:
                connection.execute(INSERT_TURN, {"call_id": cid(1), "at": T0})
        failing = 1020 <= clock.now < 1024

    app = admin_with_clock(settings, clock, sleep=sleep, engine=engine)
    with client_from(app) as client:
        client.post(
            SESSION_ROUTE,
            json={"user": ADMIN_USER, "password": PASSWORD},
            headers={"Origin": OWN_ORIGIN},
        )
        with client.stream("GET", STREAM_ROUTE) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream"
            assert response.headers["x-accel-buffering"] == "no"
            assert response.headers["cache-control"] == "no-store"
            body = response.read().decode()
        # The stream never kept the session alive: 30 minutes from the sign-in, less the five.
        assert client.get(SESSION_ROUTE).json()["expires_in"] == IDLE_SECONDS - 300

    sent = parsed(body)
    comment = ("comment", ": keep-alive")
    assert sent == [
        # At once, then only when the list changes: the turn written at 10 s.
        ("calls", [WITHOUT_TURN]),
        ("calls", [WITH_TURN]),
        comment,
        # The reads at 20 and 22 s fail: one pause, then the list again once a read works.
        ("paused", {}),
        ("calls", [WITH_TURN]),
        # A comment every 15 s, and after five minutes the page is told to reconnect.
        *[comment] * 18,
        ("renew", {}),
    ]
    # One read at the opening and one every 2 s after it, until the five minutes are up.
    assert reads == [1000 + 2 * tick for tick in range(150)]
    assert held and set(held) == {0}
    # One audit event for the stream, none for its ticks.
    assert admin_events(owner_engine) == [
        ("admin_signed_in", "session", None, "success"),
        ("admin_read_live_calls", "call", None, "success"),
    ]

    # The session ends while the stream is open: it says so and closes. Opened 5 s before the
    # idle limit, the stream finds it gone on the tick after, so it never kept it alive.
    clock = Clock()
    app = admin_with_clock(settings, clock)
    with client_from(app) as client:
        client.post(
            SESSION_ROUTE,
            json={"user": ADMIN_USER, "password": PASSWORD},
            headers={"Origin": OWN_ORIGIN},
        )
        clock.now += IDLE_SECONDS - 5
        with client.stream("GET", STREAM_ROUTE) as response:
            body = response.read().decode()
        assert parsed(body) == [("calls", [WITH_TURN]), ("signed_out", {})]
        assert clock.now == 1000 + IDLE_SECONDS + 1
        assert client.get(SESSION_ROUTE).status_code == 401

    # At most five open streams in the process, one per session: a newer one closes the older.
    async def five_and_one() -> None:
        app = admin_with_clock(settings, Clock(), sleep=yielding_sleep)
        sessions = [await asgi_sign_in(app) for _ in range(6)]
        streams = [stream_of(app, session_id) for session_id in sessions[:5]]
        tasks = [asyncio.create_task(stream.run()) for stream in streams]
        await until(lambda: all(stream.chunks for stream in streams))
        assert [stream.status for stream in streams] == [200] * 5

        sixth = stream_of(app, sessions[5])
        await asyncio.wait_for(sixth.run(), timeout=10)
        assert sixth.status == 429
        assert json.loads(sixth.text) == {"detail": "Too many live streams"}

        again = stream_of(app, sessions[0])
        task = asyncio.create_task(again.run())
        await until(lambda: bool(again.chunks))
        assert again.status == 200
        await asyncio.wait_for(tasks[0], timeout=10)
        assert streams[0].ended
        # The older is told why it ends, so its page does not reconnect and take it back.
        assert parsed(streams[0].text) == [("calls", [WITH_TURN]), ("replaced", {})]
        # The older's place was the newer's: the sixth session still has none.
        refused = stream_of(app, sessions[5])
        await asyncio.wait_for(refused.run(), timeout=10)
        assert refused.status == 429

        # A client gone frees its place.
        streams[1].gone.set()
        await asyncio.wait_for(tasks[1], timeout=10)
        sixth = stream_of(app, sessions[5])
        sixth_task = asyncio.create_task(sixth.run())
        await until(lambda: bool(sixth.chunks))
        assert sixth.status == 200

        for stream in [*streams, again, sixth]:
            stream.gone.set()
        await asyncio.wait_for(asyncio.gather(*tasks, task, sixth_task), timeout=10)

    asyncio.run(five_and_one(), loop_factory=asyncio.SelectorEventLoop)
    # Five streams, the newer one of the first session, and the sixth once there was room.
    assert [row[0] for row in admin_events(owner_engine)].count("admin_read_live_calls") == 2 + 7


def test_a_client_that_stops_reading_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "WRITE_SECONDS", 0.05)

    async def scenario() -> None:
        streams = LiveStreams()
        stop = streams.open(b"session")
        assert stop is not None
        clock = Clock()

        async def read() -> str:
            return "[]"

        stream = LiveStream(
            "[]",
            read=read,
            signed_in=lambda: True,
            stop=stop,
            release=lambda: streams.close(b"session", stop),
            clock=clock,
            sleep=clock.sleep,
        )
        exchange = Exchange(stream, "GET", STREAM_ROUTE, {}, stall=True)
        # The first comment cannot be written: the stream gives up on the client.
        await asyncio.wait_for(exchange.run(), timeout=10)
        assert parsed(exchange.text) == [("calls", [])]
        assert not exchange.ended
        assert len(streams) == 0

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


def test_stream_places() -> None:
    streams = LiveStreams()
    stops = [streams.open(bytes([number])) for number in range(5)]
    assert all(stop is not None for stop in stops)
    assert streams.open(b"another") is None
    newer = streams.open(bytes([0]))
    assert newer is not None and stops[0] is not None and stops[0].is_set()
    assert len(streams) == 5
    # The older stream leaving does not free the newer's place.
    streams.close(bytes([0]), stops[0])
    assert len(streams) == 5
    streams.close(bytes([0]), newer)
    assert len(streams) == 4


async def failing_once(reads: list[int]) -> str:
    reads.append(1)
    if len(reads) == 2:
        raise ConnectionError("the database is away")
    return "[]"


def test_the_first_read_after_a_pause_sends_the_list() -> None:
    # The list did not change while the reads failed; the page shows it as paused all the
    # same, so the first read that works sends it again.
    async def scenario() -> list[str]:
        streams = LiveStreams()
        stop = streams.open(b"session")
        assert stop is not None
        clock = Clock()
        reads: list[int] = []
        stream = LiveStream(
            "[]",
            read=lambda: failing_once(reads),
            signed_in=lambda: True,
            stop=stop,
            release=lambda: streams.close(b"session", stop),
            clock=clock,
            sleep=clock.sleep,
        )
        exchange = Exchange(stream, "GET", STREAM_ROUTE, {})
        await asyncio.wait_for(exchange.run(), timeout=10)
        return [event for event, _ in parsed(exchange.text) if event != "comment"]

    events = asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)
    assert events == ["calls", "paused", "calls", "renew"]


def test_a_failed_opening_gives_its_place_back(
    database: StartupSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first list cannot be made into an event (a value of a type the admin is not shown):
    # the opening fails, and its place in the process is free again.
    broken = True
    original = console.live_calls

    async def unshowable(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if broken:
            return [{"call_id": object()}]
        return await original(*args, **kwargs)

    monkeypatch.setattr(console, "live_calls", unshowable)

    async def scenario() -> None:
        nonlocal broken
        app = admin_with_clock(admin_settings(database), Clock(), sleep=yielding_sleep)
        sessions = [await asgi_sign_in(app) for _ in range(6)]
        for session_id in sessions[:5]:
            with pytest.raises(TypeError):
                await asyncio.wait_for(stream_of(app, session_id).run(), timeout=10)
        broken = False
        opened = stream_of(app, sessions[5])
        task = asyncio.create_task(opened.run())
        await until(lambda: bool(opened.chunks))
        assert opened.status == 200
        opened.gone.set()
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


def test_a_request_without_the_console_origin_leaves_the_stream_alone(
    database: StartupSettings, owner_engine: Engine
) -> None:
    # With CONSOLE_ORIGIN set, a request carrying the cookie but no Origin is refused before
    # anything: it cannot take the console's open stream away, and writes no event.
    console_origin = "http://console.test"

    async def scenario() -> None:
        app = admin_with_clock(
            admin_settings(database, console_origin=console_origin), Clock(), sleep=yielding_sleep
        )
        session_id = await asgi_sign_in(app, console_origin)
        open_stream = stream_of(app, session_id, console_origin)
        task = asyncio.create_task(open_stream.run())
        await until(lambda: bool(open_stream.chunks))
        before = admin_events(owner_engine)
        bare = stream_of(app, session_id)
        await asyncio.wait_for(bare.run(), timeout=10)
        assert bare.status == 401
        await asyncio.sleep(0.05)
        assert not open_stream.ended and not task.done()
        assert admin_events(owner_engine) == before
        open_stream.gone.set()
        await asyncio.wait_for(task, timeout=10)
        assert [event for event, _ in parsed(open_stream.text)] == ["calls"]

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)
