import asyncio
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import pytest
from conftest import VOICE_SECRETS, close_unstarted, offline_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.turn_record import TurnRecorder
from app.api.offer import OpenCall
from app.audit.queue import AuditQueue
from app.config import Settings
from app.main import create_app
from app.runtime_config import taking_calls
from app.voice.calls import CallRegistry

TOKEN = VOICE_SECRETS["DEMO_ACCESS_TOKEN"]
OFFER = {"sdp": "v=0 fake-offer", "type": "offer"}
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BAD_AUTH = [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": f"Bearer {TOKEN}x"},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": TOKEN},
    {"Authorization": "Bearer "},
]


class FakeCall:
    """A call that stays up until it is stopped or the caller hangs up."""

    def __init__(self, fail_while_running: bool = False) -> None:
        self.stopped = False
        self._fail_while_running = fail_while_running
        self._ended: asyncio.Future[None] | None = None

    def _ended_future(self) -> asyncio.Future[None]:
        if self._ended is None:
            self._ended = asyncio.get_running_loop().create_future()
        return self._ended

    def answer(self) -> dict[str, str]:
        return {"sdp": "v=0 fake-answer", "type": "answer"}

    async def run(self) -> None:
        if self._fail_while_running:
            raise RuntimeError("pipeline broke")
        await self._ended_future()

    def hang_up(self) -> None:
        if not self._ended_future().done():
            self._ended_future().set_result(None)

    async def stop(self) -> None:
        self.stopped = True
        self.hang_up()


class Opener:
    """Stands in for the real call setup; records each call it opens."""

    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[FakeCall] = []
        self.attempts = 0
        self._fail_first = fail_first

    async def __call__(self, call_id: str, sdp: str, sdp_type: str) -> FakeCall:
        self.attempts += 1
        if self.attempts <= self._fail_first:
            raise ValueError("bad offer")
        call = FakeCall()
        self.calls.append(call)
        return call


def voice_settings(max_concurrent_calls: int = 3) -> Settings:
    return offline_settings(max_concurrent_calls=max_concurrent_calls)


def offer_app(settings: Settings, open_call: OpenCall | None = None) -> FastAPI:
    """The calls' application, with no database, its config already read: these tests run
    without one, so its reads fail and it keeps these values."""
    return create_app(settings, open_call=open_call, config=taking_calls())


def active_calls(app: FastAPI) -> int:
    calls: CallRegistry = app.state.calls
    return calls.active


# The largest offer body the app reads; written out here, not imported from the app.
BODY_LIMIT = 32 * 1024
STREAM_CHUNK = 16 * 1024
STREAM_GIVES_UP = 64 * 1024 * 1024


def offer_scope(headers: dict[str, str], content_length: int | None) -> dict[str, Any]:
    raw_headers = [(b"content-type", b"application/json")]
    raw_headers += [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    if content_length is not None:
        raw_headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/offer",
        "raw_path": b"/offer",
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


async def post_stream(
    app: FastAPI, headers: dict[str, str], content_length: int | None = None
) -> tuple[int | None, int]:
    """POST /offer with a body that never ends, straight through ASGI.

    Returns the response status (None if the app gave no answer within 10 seconds) and how
    many body bytes the app pulled. The stream stops sending after 64 MiB and then waits
    forever, so an app that buffers the whole body never answers.
    """
    consumed = 0
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if consumed >= STREAM_GIVES_UP:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        consumed += STREAM_CHUNK
        return {"type": "http.request", "body": b"[" * STREAM_CHUNK, "more_body": True}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    task = asyncio.ensure_future(app(offer_scope(headers, content_length), receive, send))
    done, _pending = await asyncio.wait({task}, timeout=10)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    starts = [message["status"] for message in sent if message["type"] == "http.response.start"]
    return (starts[0] if starts else None), consumed


# Bodies a caller without the token may send; none of them may be read or parsed.
TOKENLESS_BODIES = {
    "offer": json.dumps(OFFER).encode(),
    "oversized": b"x" * (4 * BODY_LIMIT),
    "malformed": b'{"sdp": "v=0", "type": ',
    "nested": b"[" * (BODY_LIMIT // 2 - 8) + b"]" * (BODY_LIMIT // 2 - 8),
}


@pytest.mark.parametrize("body", list(TOKENLESS_BODIES), ids=list(TOKENLESS_BODIES))
@pytest.mark.parametrize("headers", BAD_AUTH)
def test_offer_requires_access_token(headers: dict[str, str], body: str) -> None:
    opener = Opener()
    app = offer_app(voice_settings(), open_call=opener)

    with TestClient(app) as client:
        response = client.post(
            "/offer",
            content=TOKENLESS_BODIES[body],
            headers={**headers, "Content-Type": "application/json"},
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "sdp" not in response.json()
        assert opener.attempts == 0
        assert active_calls(app) == 0

    # A body that never ends is not read at all before the token is checked.
    status, consumed = asyncio.run(post_stream(app, headers))
    assert (status, consumed) == (401, 0)


@pytest.mark.parametrize("headers", BAD_AUTH)
def test_ice_servers_require_access_token(headers: dict[str, str]) -> None:
    with TestClient(offer_app(voice_settings())) as client:
        refused = client.get("/ice-servers", headers=headers)
        allowed = client.get("/ice-servers", headers=AUTH)

    assert refused.status_code == 401
    assert "credential" not in refused.text
    assert "turn:" not in refused.text
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    [server] = allowed.json()["iceServers"]
    assert server["username"]
    assert server["credential"]


def test_call_cap_is_enforced() -> None:
    opener = Opener()
    app = offer_app(voice_settings(max_concurrent_calls=2), open_call=opener)

    with TestClient(app) as client:
        first = client.post("/offer", json=OFFER, headers=AUTH)
        second = client.post("/offer", json=OFFER, headers=AUTH)
        third = client.post("/offer", json=OFFER, headers=AUTH)

        assert [first.status_code, second.status_code, third.status_code] == [200, 200, 503]
        assert opener.attempts == 2
        assert active_calls(app) == 2

    assert all(call.stopped for call in opener.calls)


def test_offer_returns_the_answer() -> None:
    with TestClient(offer_app(voice_settings(), open_call=Opener())) as client:
        response = client.post("/offer", json=OFFER, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["sdp"] == "v=0 fake-answer"
    assert body["type"] == "answer"
    assert len(body["call_id"]) == 32


async def never(_seconds: float) -> None:
    await asyncio.Event().wait()


def run_registry(
    scenario: Callable[[CallRegistry], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = never,
    max_calls: int = 1,
) -> None:
    async def main() -> None:
        registry = CallRegistry(max_calls=max_calls, max_seconds=600, sleep=sleep)
        try:
            await scenario(registry)
        finally:
            await registry.close()

    asyncio.run(main())


def test_slot_released_on_end_and_on_failure() -> None:
    # Setup fails half-way: the slot taken for the call is given back.
    opener = Opener(fail_first=1)
    app = offer_app(voice_settings(max_concurrent_calls=1), open_call=opener)
    with TestClient(app) as client:
        failed = client.post("/offer", json=OFFER, headers=AUTH)
        assert failed.status_code == 400
        assert active_calls(app) == 0
        retried = client.post("/offer", json=OFFER, headers=AUTH)
        assert retried.status_code == 200

    # The caller hangs up, or the pipeline breaks: the slot is given back and the call stopped.
    async def ends(registry: CallRegistry) -> None:
        for call in (FakeCall(), FakeCall(fail_while_running=True)):
            call_id = registry.reserve()
            assert call_id is not None
            assert registry.reserve() is None
            task = registry.start(call_id, call)
            call.hang_up()
            await asyncio.wait_for(task, timeout=5)
            assert registry.active == 0
            assert call.stopped

    run_registry(ends)


def test_call_is_ended_at_max_duration() -> None:
    waited: list[float] = []
    deadline = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)
        await deadline.wait()

    async def outlives_limit(registry: CallRegistry) -> None:
        call = FakeCall()
        call_id = registry.reserve()
        assert call_id is not None
        task = registry.start(call_id, call)
        await asyncio.sleep(0)
        assert registry.active == 1
        assert not call.stopped

        deadline.set()
        await asyncio.wait_for(task, timeout=5)

        assert call.stopped
        assert registry.active == 0

    run_registry(outlives_limit, sleep=fake_sleep)
    assert waited == [600]


def test_registry_close_stops_every_call() -> None:
    calls = [FakeCall(), FakeCall()]

    async def shutdown(registry: CallRegistry) -> None:
        for call in calls:
            call_id = registry.reserve()
            assert call_id is not None
            registry.start(call_id, call)
        await asyncio.sleep(0)
        await registry.close()
        assert registry.active == 0

    run_registry(shutdown, max_calls=2)
    assert all(call.stopped for call in calls)


def test_token_compare_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[bytes, bytes]] = []
    verdict = {"value": False}

    def spy(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return verdict["value"]

    monkeypatch.setattr(hmac, "compare_digest", spy)
    opener = Opener()

    with TestClient(offer_app(voice_settings(), open_call=opener)) as client:
        # The right token is refused when the constant-time compare says no...
        assert client.post("/offer", json=OFFER, headers=AUTH).status_code == 401
        assert client.get("/ice-servers", headers=AUTH).status_code == 401
        # ...and a wrong one accepted when it says yes: the compare alone decides.
        verdict["value"] = True
        wrong = {"Authorization": "Bearer wrong-token"}
        assert client.post("/offer", json=OFFER, headers=wrong).status_code == 200

    assert compared[0] == (TOKEN.encode(), TOKEN.encode())
    assert compared[-1] == (b"wrong-token", TOKEN.encode())


def test_wrong_then_right_token_works() -> None:
    with TestClient(offer_app(voice_settings(), open_call=Opener())) as client:
        refused = client.post("/offer", json=OFFER, headers={"Authorization": "Bearer nope"})
        accepted = client.post("/offer", json=OFFER, headers=AUTH)

    assert (refused.status_code, accepted.status_code) == (401, 200)


def test_dev_without_token_is_open() -> None:
    settings = offline_settings(
        app_env="dev", demo_access_token=None, turn_host=None, turn_shared_secret=None
    )

    with TestClient(offer_app(settings, open_call=Opener())) as client:
        offer = client.post("/offer", json=OFFER)
        ice = client.get("/ice-servers")

    assert offer.status_code == 200
    assert ice.json() == {"iceServers": []}


@pytest.mark.parametrize(
    "body",
    [
        {"sdp": "v=0", "type": "answer"},
        {"sdp": "v=0"},
        {"sdp": "v=0" + "a" * 20_000, "type": "offer"},
        {"sdp": "", "type": "offer"},
        {"sdp": "v=0", "type": "offer", "extra": "field"},
    ],
)
def test_malformed_offer_is_rejected(body: dict[str, str]) -> None:
    opener = Opener()
    app = offer_app(voice_settings(), open_call=opener)

    with TestClient(app) as client:
        response = client.post("/offer", json=body, headers=AUTH)
        assert response.status_code == 422
        assert opener.attempts == 0
        assert active_calls(app) == 0


def test_call_page_is_served() -> None:
    with TestClient(offer_app(voice_settings())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for control in ('id="token"', 'id="call"', 'id="hangup"', 'id="state"', 'id="candidate"'):
        assert control in response.text


def test_offer_body_is_capped() -> None:
    opener = Opener()
    app = offer_app(voice_settings(), open_call=opener)

    # A body over the limit is refused, whether it is sent whole or never ends.
    with TestClient(app) as client:
        response = client.post(
            "/offer",
            content=b"x" * (BODY_LIMIT + 1),
            headers={**AUTH, "Content-Type": "application/json"},
        )
    assert response.status_code == 413

    status, consumed = asyncio.run(post_stream(app, AUTH))
    assert status == 413
    # Read up to the limit and at most one chunk more, never the whole stream.
    assert consumed <= BODY_LIMIT + STREAM_CHUNK

    # A declared length over the limit is refused before any of the body is read.
    status, consumed = asyncio.run(post_stream(app, AUTH, content_length=BODY_LIMIT + 1))
    assert (status, consumed) == (413, 0)

    assert opener.attempts == 0
    assert active_calls(app) == 0


async def post_chunked(app: FastAPI, body: bytes) -> int | None:
    """POST /offer with the token, streamed in 1 KiB chunks and no declared length."""
    chunks = [body[start : start + 1024] for start in range(0, len(body), 1024)]
    starts: list[int] = []

    async def receive() -> dict[str, Any]:
        if not chunks:
            await asyncio.Event().wait()
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            starts.append(message["status"])

    task = asyncio.ensure_future(app(offer_scope(AUTH, None), receive, send))
    await asyncio.wait({task}, timeout=10)
    return starts[0] if starts else None


def padded_offer(size: int) -> bytes:
    """A valid offer, padded with JSON whitespace to exactly `size` bytes."""
    body = json.dumps(OFFER).encode()
    return body + b" " * (size - len(body))


def test_offer_body_cap_boundary() -> None:
    app = offer_app(voice_settings(max_concurrent_calls=1), open_call=Opener())

    async def main() -> tuple[int | None, int | None]:
        at_cap = await post_chunked(app, padded_offer(BODY_LIMIT))
        over_cap = await post_chunked(app, padded_offer(BODY_LIMIT + 1))
        return at_cap, over_cap

    assert len(padded_offer(BODY_LIMIT)) == BODY_LIMIT
    assert asyncio.run(main()) == (200, 413)
    close_unstarted(app)


def test_empty_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Configured empty: the app will not start with it.
    with pytest.raises(ValidationError) as excinfo:
        offline_settings(demo_access_token="")
    assert [error["loc"] for error in excinfo.value.errors()] == [("demo_access_token",)]

    # Supplied empty: refused before any comparison is made.
    compared: list[tuple[bytes, bytes]] = []

    def spy(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return True

    monkeypatch.setattr(hmac, "compare_digest", spy)
    opener = Opener()
    with TestClient(offer_app(voice_settings(), open_call=opener)) as client:
        for empty in ("Bearer ", "Bearer", "Bearer  "):
            headers = {"Authorization": empty}
            assert client.post("/offer", json=OFFER, headers=headers).status_code == 401
            assert client.get("/ice-servers", headers=headers).status_code == 401
    assert compared == []
    assert opener.attempts == 0

    # Both empty: there is no configuration in which an empty token opens a call.
    with pytest.raises(ValidationError):
        offline_settings(app_env="dev", demo_access_token="")


def test_slot_released_when_stop_hangs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.voice.calls")

    class StuckCall(FakeCall):
        """Neither its stop nor its run ever finishes on its own."""

        run_cancelled = False

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.run_cancelled = True
                raise

        async def stop(self) -> None:
            self.stopped = True
            await asyncio.Event().wait()

    async def main() -> None:
        registry = CallRegistry(max_calls=1, max_seconds=600, sleep=never, stop_timeout=0.1)
        call = StuckCall()
        call_id = registry.reserve()
        assert call_id is not None
        task = registry.start(call_id, call)
        await asyncio.sleep(0)
        closing = asyncio.ensure_future(registry.close())
        done, _pending = await asyncio.wait({closing, task}, timeout=5)
        assert done == {closing, task}
        assert call.stopped
        assert registry.active == 0
        # The call's own run task is cancelled too, not left running behind the released slot.
        await asyncio.sleep(0)
        assert call.run_cancelled
        assert f"call_stop_timed_out call_id={call_id}" in caplog.text
        assert f"call_run_cancelled call_id={call_id}" in caplog.text

    asyncio.run(main())


def test_slot_released_when_setup_is_cancelled() -> None:
    entered = asyncio.Event()

    async def slow_open(call_id: str, sdp: str, sdp_type: str) -> FakeCall:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("never reached")

    app = offer_app(voice_settings(max_concurrent_calls=1), open_call=slow_open)
    body = json.dumps(OFFER).encode()

    async def main() -> None:
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                await asyncio.Event().wait()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: MutableMapping[str, Any]) -> None:
            pass

        task = asyncio.ensure_future(app(offer_scope(AUTH, len(body)), receive, send))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert active_calls(app) == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert active_calls(app) == 0

    asyncio.run(main())
    close_unstarted(app)


async def post_offer(app: FastAPI) -> int:
    """POST /offer with a valid body and the token, straight through ASGI."""
    body = json.dumps(OFFER).encode()
    delivered = False
    starts: list[int] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            await asyncio.Event().wait()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            starts.append(message["status"])

    await app(offer_scope(AUTH, len(body)), receive, send)
    return starts[0]


def test_offer_refused_until_the_config_is_read(caplog: pytest.LogCaptureFixture) -> None:
    # No database, so the config is never read: an unknown config takes no call, and the
    # offer is refused before any of its body is read.
    caplog.set_level(logging.INFO)
    opener = Opener()
    app = create_app(voice_settings(), open_call=opener)
    with TestClient(app) as client:
        refused = client.post("/offer", json=OFFER, headers=AUTH)
        assert client.post("/offer", json=OFFER).status_code == 401
    assert (refused.status_code, refused.json()) == (503, {"reason": "unavailable"})
    assert asyncio.run(post_stream(app, AUTH)) == (503, 0)
    assert opener.attempts == 0
    assert "call_refused reason=config_unknown" in caplog.text


def test_shutdown_drains_both_queues_within_one_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # The turn rows and the refused calls' events both stalled: each queue uses all the time
    # it is given. Shutdown gives them one limit together, so the container's grace period
    # holds.
    given: list[float | None] = []

    async def stalled(_queue: object, timeout: float | None = None) -> None:
        given.append(timeout)
        assert timeout is not None
        await asyncio.sleep(timeout)

    monkeypatch.setattr(TurnRecorder, "drain", stalled)
    monkeypatch.setattr(AuditQueue, "drain", stalled)
    app = offer_app(voice_settings())

    async def main() -> float:
        async with app.router.lifespan_context(app):
            stopping = time.monotonic()
        return time.monotonic() - stopping

    took = asyncio.run(main())
    assert given == [5, 5]
    assert took < 6, took


def test_shutdown_ends_active_calls() -> None:
    opener = Opener()
    app = offer_app(voice_settings(), open_call=opener)

    # The app's lifespan runs on this loop, which stays up after the shutdown, so only the
    # shutdown itself can have ended the calls.
    async def main() -> None:
        async with app.router.lifespan_context(app):
            assert [await post_offer(app), await post_offer(app)] == [200, 200]
            assert active_calls(app) == 2
            assert not any(call.stopped for call in opener.calls)

        assert [call.stopped for call in opener.calls] == [True, True]
        assert active_calls(app) == 0

    asyncio.run(main())
