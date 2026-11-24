"""The voice adapter: finished utterances become turns of the call's agent, one at a time, and
what came of each reaches the page as a data-channel message; the call's set-up. Nothing here
reaches a vendor: speech-to-text is replaced by a pass-through and the model is scripted."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection
from aiortc.mediastreams import AudioStreamTrack
from conftest import REPO_ROOT, VOICE_SECRETS, offline_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    InterimTranscriptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFinalizedFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pydantic import SecretStr
from test_turn import ADVICE_REFUSAL, ScriptedModel, deps_on, says

import app.voice.pipeline
import app.voice.transport
from app.agent.state import IdentityClaim, Phase, claim, move, new_call
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import CallAgent, TurnResult, TurnTimings
from app.db.session import create_database_engine
from app.guard.input import GuardAction
from app.guard.output import OutputRule
from app.main import create_app
from app.voice.calls import CallRegistry
from app.voice.pipeline import AgentProcessor, deepgram_stt, open_agent_call

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
CALL_ID = "0f1e2d3c4b5a46978a1b2c3d4e5f6a7b"
OPENING = "Thank you for calling."


def transcript(text: str, finalized: bool = False) -> TranscriptionFrame:
    return TranscriptionFrame(text, "", "2026-11-24T15:00:00Z", finalized=finalized)


def messages(frames: list[Frame]) -> list[dict[str, Any]]:
    return [
        frame.message for frame in frames if isinstance(frame, OutputTransportMessageUrgentFrame)
    ]


@pytest.fixture
async def offline_deps() -> AsyncIterator[Deps]:
    engine = create_database_engine(offline_settings())
    try:
        yield deps_on(engine)
    finally:
        await engine.dispose()


class Clock:
    """Each reading a tenth of a second after the one before."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        self.now += 0.1
        return self.now


def agent_in_intake(
    deps: Deps, model: ScriptedModel, clock: Clock, *, armed: bool = False
) -> CallAgent:
    state = move(new_call(call_id=CALL_ID, started_at=T0, office_open=True), Phase.INTAKE)
    if armed:
        state = claim(state, IdentityClaim(full_name="Sarah Johnson", dob=date(1985, 3, 14)))
    return CallAgent(state, deps, model, register_tools(), clock=clock)


@pytest.mark.anyio
async def test_finished_utterances_become_turns(
    offline_deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    model = ScriptedModel(says("How can I help with your appointment?"))
    clock = Clock()
    processor = AgentProcessor(CALL_ID, agent_in_intake(offline_deps, model, clock), OPENING, clock)
    processor.greet()

    down, _up = await run_test(
        processor,
        frames_to_send=[
            VADUserStoppedSpeakingFrame(),
            InterimTranscriptionFrame("I have a", "", "t"),
            transcript("I have a question"),
            transcript("about my appointment.", finalized=True),
            SleepFrame(sleep=0.3),
        ],
    )

    # The transcript reaches the agent, whole, and nothing downstream.
    assert model.requests[0].messages[-1] == {
        "role": "user",
        "content": "I have a question about my appointment.",
    }
    assert not [f for f in down if isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame))]
    assert messages(list(down)) == [
        {"type": "opening", "lines": [OPENING]},
        {
            "type": "turn",
            "action": "pass",
            "shown": "I have a question about my appointment.",
            "lines": ["How can I help with your appointment?"],
            "withheld": None,
            "hand_off": False,
            "timings": {
                # One clock, read in turn: speech end 100.1, transcript final 100.2, the turn's
                # start 100.3, first token 100.4, last token 100.5, reply sent 100.6.
                "stt_ms": 100,
                "first_token_ms": 100,
                "last_token_ms": 200,
                "tool_rounds": 0,
                "total_ms": 500,
            },
        },
    ]
    measured = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("turn_measured")
    ]
    assert measured == [
        f"turn_measured call_id={CALL_ID} action=pass stt_ms=100 first_token_ms=100"
        " last_token_ms=200 tool_rounds=0 total_ms=500 hand_off=False"
    ]
    assert "question" not in caplog.text
    assert "appointment" not in caplog.text


@pytest.mark.anyio
async def test_a_swallowed_turn_reaches_the_page_as_nothing(offline_deps: Deps) -> None:
    model = ScriptedModel()
    clock = Clock()
    processor = AgentProcessor(
        CALL_ID, agent_in_intake(offline_deps, model, clock, armed=True), OPENING, clock
    )

    down, _up = await run_test(
        processor,
        frames_to_send=[transcript("three three", finalized=True), SleepFrame(sleep=0.3)],
    )

    (turn,) = messages(list(down))
    assert (turn["action"], turn["shown"], turn["lines"]) == ("swallow", None, [])
    assert "three" not in json.dumps(turn)
    assert model.requests == []


@pytest.mark.anyio
async def test_turns_wait_for_the_finalized_transcript(offline_deps: Deps) -> None:
    model = ScriptedModel(says("One."), says("Two."))
    clock = Clock()
    processor = AgentProcessor(CALL_ID, agent_in_intake(offline_deps, model, clock), OPENING, clock)

    down, _up = await run_test(
        processor,
        frames_to_send=[
            transcript("first part"),
            transcript("still first", finalized=True),
            # Deepgram had already sent the text when it confirmed the finalize.
            transcript("second"),
            TranscriptionFinalizedFrame(),
            # A finalize with nothing heard is no turn.
            TranscriptionFinalizedFrame(),
            SleepFrame(sleep=0.3),
        ],
    )

    assert [m["shown"] for m in messages(list(down))] == ["first part still first", "second"]
    assert [m["timings"]["stt_ms"] for m in messages(list(down))] == [None, None]


class Withheld:
    """A turn whose reply the output guard blocked."""

    async def handle(self, transcript: str) -> TurnResult:
        return TurnResult(
            action=GuardAction.PASS,
            shown=transcript,
            lines=(ADVICE_REFUSAL,),
            hand_off=False,
            timings=TurnTimings(first_token_ms=1, last_token_ms=2, tool_rounds=0),
            withheld=OutputRule.MEDICAL_ADVICE,
        )


@pytest.mark.anyio
async def test_a_withheld_reply_reaches_the_page_by_its_rule() -> None:
    processor = AgentProcessor(CALL_ID, Withheld(), OPENING, Clock())

    down, _up = await run_test(
        processor,
        frames_to_send=[transcript("Is it serious?", finalized=True), SleepFrame(sleep=0.3)],
    )

    (turn,) = messages(list(down))
    assert (turn["lines"], turn["withheld"]) == ([ADVICE_REFUSAL], "medical_advice")
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "(a reply was withheld: ${message.withheld})" in page


class Broken:
    async def handle(self, transcript: str) -> TurnResult:
        raise RuntimeError("an unknown state")


@pytest.mark.anyio
async def test_a_failed_turn_ends_the_call(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    processor = AgentProcessor(CALL_ID, Broken(), OPENING, Clock())

    _down, up = await run_test(
        processor,
        frames_to_send=[transcript("hello there", finalized=True), SleepFrame(sleep=0.3)],
        expected_up_frames=[EndWorkerFrame],
    )

    assert f"turn_failed call_id={CALL_ID} error=RuntimeError" in caplog.text
    assert "an unknown state" not in caplog.text


def test_speech_to_text_options() -> None:
    service = deepgram_stt(SecretStr(VOICE_SECRETS["DEEPGRAM_API_KEY"]))

    # What goes to Deepgram as query parameters (pipecat-ai 1.17.0 builds them here), but the
    # sample rate, which the pipeline sets when it starts. Nothing redacted or boosted.
    options = service._build_connect_kwargs()
    assert {key: value for key, value in options.items() if key != "sample_rate"} == {
        "model": "nova-3-general",
        "language": "en-US",
        "smart_format": "true",
        "numerals": "false",
        "punctuate": "true",
        "interim_results": "true",
        "detect_entities": "false",
        "diarize": "false",
        "dictation": "false",
        "encoding": "linear16",
        "channels": "1",
        "multichannel": "false",
        "mip_opt_out": "true",
    }
    # Audio stops at speech-to-text; the agent's processor gets only frames about it.
    assert service._audio_passthrough is False


# --- set-up and lifetime of a call --------------------------------------------------------


class PassThrough(FrameProcessor):
    """Speech-to-text's place in the pipeline, without a vendor."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def no_vendor(_key: SecretStr) -> FrameProcessor:
    return PassThrough()


DEV_WITHOUT_VOICE: dict[str, Any] = {
    "app_env": "dev",
    "demo_access_token": None,
    "turn_host": None,
    "turn_shared_secret": None,
}


async def offer_without_candidates() -> str:
    caller = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        caller.addTransceiver(AudioStreamTrack(), direction="sendrecv")
        await caller.setLocalDescription(await caller.createOffer())
        lines = caller.localDescription.sdp.splitlines()
        return "\r\n".join(line for line in lines if not line.startswith("a=candidate:")) + "\r\n"
    finally:
        await caller.close()


def test_call_that_never_connects_ends_itself() -> None:
    # A real offer with its candidates removed: the server can never reach the caller.
    # Only local sockets are opened; no ICE servers are configured.
    async def scenario() -> None:
        engine = create_database_engine(offline_settings())
        try:
            call = await open_agent_call(
                offline_settings(**DEV_WITHOUT_VOICE),
                deps_on(engine),
                ScriptedModel(),
                CALL_ID,
                await offer_without_candidates(),
                "offer",
                connect_timeout=0.5,
                speech_to_text=no_vendor,
            )
            # asyncio.wait, not wait_for: Pipecat's runner swallows cancellation, so a
            # cancelled run() would look like one that ended by itself.
            running = asyncio.ensure_future(call.run())
            ended, _ = await asyncio.wait({running}, timeout=15)
            stopping = asyncio.ensure_future(call.stop())
            stopped, _ = await asyncio.wait({stopping, running}, timeout=15)

            assert running in ended, "a call that never connects must end itself"
            assert stopping in stopped
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def record_connections(monkeypatch: pytest.MonkeyPatch) -> list[SmallWebRTCConnection]:
    """Every peer connection the app opens, so a test can see whether it was closed."""
    opened: list[SmallWebRTCConnection] = []

    class Recorded(SmallWebRTCConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)

    monkeypatch.setattr(app.voice.transport, "SmallWebRTCConnection", Recorded)
    return opened


def active_calls(fastapi_app: FastAPI) -> int:
    calls: CallRegistry = fastapi_app.state.calls
    return calls.active


def test_connection_closed_when_answer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # The peer connection exists, then answering the offer fails on a malformed SDP.
    opened = record_connections(monkeypatch)
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE))

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": "v=0 not-an-sdp", "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert len(opened) == 1
    assert opened[0].pc.connectionState == "closed"


def test_connection_closed_when_call_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # The offer is answered, then building the call around the connection fails.
    opened = record_connections(monkeypatch)

    def broken_call(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("call setup failed")

    monkeypatch.setattr(app.voice.pipeline, "AgentCall", broken_call)
    offer = asyncio.run(offer_without_candidates())
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE))

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": offer, "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert len(opened) == 1
    assert opened[0].pc.connectionState == "closed"


@pytest.mark.parametrize("missing", ["anthropic_api_key", "deepgram_api_key"])
def test_no_call_without_both_vendor_keys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    # Allowed in dev, and refused at set-up, before any peer connection exists.
    caplog.set_level(logging.INFO)
    opened = record_connections(monkeypatch)
    offer = asyncio.run(offer_without_candidates())
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE, **{missing: None}))

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": offer, "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert opened == []
    assert "call_setup_failed" in caplog.text
    assert "error=VendorsNotConfiguredError" in caplog.text
