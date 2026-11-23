import asyncio
import hmac
from collections.abc import Awaitable, Callable

import pytest
from conftest import VOICE_SECRETS, offline_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
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


def active_calls(app: FastAPI) -> int:
    calls: CallRegistry = app.state.calls
    return calls.active


@pytest.mark.parametrize("headers", BAD_AUTH)
def test_offer_requires_access_token(headers: dict[str, str]) -> None:
    opener = Opener()
    app = create_app(voice_settings(), open_call=opener)

    with TestClient(app) as client:
        response = client.post("/offer", json=OFFER, headers=headers)

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "sdp" not in response.json()
        assert opener.attempts == 0
        assert active_calls(app) == 0


@pytest.mark.parametrize("headers", BAD_AUTH)
def test_ice_servers_require_access_token(headers: dict[str, str]) -> None:
    with TestClient(create_app(voice_settings())) as client:
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
    app = create_app(voice_settings(max_concurrent_calls=2), open_call=opener)

    with TestClient(app) as client:
        first = client.post("/offer", json=OFFER, headers=AUTH)
        second = client.post("/offer", json=OFFER, headers=AUTH)
        third = client.post("/offer", json=OFFER, headers=AUTH)

        assert [first.status_code, second.status_code, third.status_code] == [200, 200, 503]
        assert opener.attempts == 2
        assert active_calls(app) == 2

    assert all(call.stopped for call in opener.calls)


def test_offer_returns_the_answer() -> None:
    with TestClient(create_app(voice_settings(), open_call=Opener())) as client:
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
    app = create_app(voice_settings(max_concurrent_calls=1), open_call=opener)
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

    with TestClient(create_app(voice_settings(), open_call=opener)) as client:
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
    with TestClient(create_app(voice_settings(), open_call=Opener())) as client:
        refused = client.post("/offer", json=OFFER, headers={"Authorization": "Bearer nope"})
        accepted = client.post("/offer", json=OFFER, headers=AUTH)

    assert (refused.status_code, accepted.status_code) == (401, 200)


def test_dev_without_token_is_open() -> None:
    settings = offline_settings(
        app_env="dev", demo_access_token=None, turn_host=None, turn_shared_secret=None
    )

    with TestClient(create_app(settings, open_call=Opener())) as client:
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
    ],
)
def test_malformed_offer_is_rejected(body: dict[str, str]) -> None:
    opener = Opener()
    app = create_app(voice_settings(), open_call=opener)

    with TestClient(app) as client:
        response = client.post("/offer", json=body, headers=AUTH)
        assert response.status_code == 422
        assert opener.attempts == 0
        assert active_calls(app) == 0


def test_call_page_is_served() -> None:
    with TestClient(create_app(voice_settings())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for control in ('id="token"', 'id="call"', 'id="hangup"', 'id="state"', 'id="candidate"'):
        assert control in response.text
