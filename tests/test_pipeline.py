"""The voice adapter: finished utterances become turns of the call's agent, one at a time, and
what came of each reaches the page as a data-channel message; the call's set-up. Nothing here
reaches a vendor: speech-to-text is replaced by a pass-through and the model is scripted."""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection
from aiortc.mediastreams import AudioStreamTrack
from conftest import REPO_ROOT, VOICE_SECRETS, offline_settings
from deepgram.core.jsonable_encoder import jsonable_encoder
from deepgram.core.query_encoder import encode_query
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFinalizedFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pydantic import SecretStr
from speech_fakes import CallPipeline, FakeTime, FakeVoice, until
from test_turn import ADVICE_REFUSAL, deps_on

import app.voice.pipeline
import app.voice.transport
from app.agent.call_record import drain_call_records
from app.agent.scripts import ScriptId
from app.agent.state import IdentityClaim, Phase, claim, move, new_call
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import CallAgent, Line, Speak, TurnResult, TurnTimings
from app.agent.turn_record import TurnRecorder
from app.audit.log import Reason
from app.config import Settings
from app.db.seed import load_seed_data
from app.db.session import create_database_engine, create_session_factory
from app.guard.input import GuardAction
from app.guard.output import OutputRule
from app.main import create_app
from app.runtime_config import taking_calls
from app.voice.calls import CallRegistry
from app.voice.pipeline import Agent, AgentProcessor, deepgram_stt, load_vad, open_agent_call
from app.voice.speech import Playback
from app.voice.speech_check import SpeechCheck
from app.voice.vocabulary import Vocabulary, keyterms
from evals.scripted import ScriptedModel, says

T0 = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
CALL_ID = "0f1e2d3c4b5a46978a1b2c3d4e5f6a7b"
OPENING = "Thank you for calling."


def transcript(text: str, finalized: bool = False) -> TranscriptionFrame:
    return TranscriptionFrame(text, "", "2026-11-24T15:00:00Z", finalized=finalized)


@pytest.fixture
async def offline_deps() -> AsyncIterator[Deps]:
    engine = create_database_engine(offline_settings())
    try:
        yield deps_on(engine)
    finally:
        # The record of a call the test ended is made, or given up, before the engine goes.
        await drain_call_records()
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


def adapter(
    agent: Agent, clock: Clock, refused: list[None] | None = None
) -> tuple[AgentProcessor, Playback]:
    """The voice adapter around an agent the test built, OPENING its opening line; each time it
    asks why speech out refused, `refused` gets an entry."""

    async def start() -> tuple[Agent, Line]:
        return agent, Line(id=0, text=OPENING, script=ScriptId.GREETING)

    asked = [] if refused is None else refused
    playback = Playback()
    processor = AgentProcessor(
        CALL_ID,
        start,
        playback,
        idle_seconds=30,
        max_seconds=600,
        vad_stop_seconds=0.5,
        speech_refused=lambda: asked.append(None),
        clock=clock,
        sleep=FakeTime().sleep,
    )
    return processor, playback


def turns(call: CallPipeline) -> list[dict[str, Any]]:
    return [m for m in call.messages() if m["type"] == "turn"]


@pytest.mark.anyio
async def test_the_vad_thread_starts_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pipecat gives each VAD a thread of its own for the model; started at the first
    # analysis, it would start on the event loop's thread, which waits for it. A call's VAD
    # comes with that thread running.
    vad = await asyncio.to_thread(load_vad, 0.5)
    vad.set_sample_rate(16000)
    started: list[str] = []
    start = threading.Thread.start

    def spied(thread: threading.Thread) -> None:
        started.append(thread.name)
        start(thread)

    try:
        with monkeypatch.context() as patched:
            patched.setattr(threading.Thread, "start", spied)
            await vad.analyze_audio(bytes(2 * vad.num_frames_required()))
    finally:
        await vad.cleanup()
    assert started == []


@pytest.mark.anyio
async def test_finished_utterances_become_turns(
    offline_deps: Deps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    model = ScriptedModel(says("How can I help with your appointment?"))
    processor, playback = adapter(agent_in_intake(offline_deps, model, Clock()), Clock())
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        await voice.play_all()
        await call.send(
            VADUserStoppedSpeakingFrame(),
            InterimTranscriptionFrame("I have a", "", "t"),
            transcript("I have a question"),
            transcript("about my appointment.", finalized=True),
        )
        await until(lambda: len(turns(call)) == 1, "the turn's message")
        # The reply plays once the turn's message is out, so the clock is read in this order.
        await voice.play_all()
        await until(lambda: len(call.messages()) == 3, "the first audio")

    # The transcript reaches the agent, whole, and nothing downstream.
    assert model.requests[0].messages[-1] == {
        "role": "user",
        "content": "I have a question about my appointment.",
    }
    assert not [
        f for f in call.down if isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame))
    ]
    assert call.messages()[:3] == [
        {"type": "opening", "lines": [OPENING]},
        {
            "type": "turn",
            "turn": 1,
            "action": "pass",
            "shown": "I have a question about my appointment.",
            "lines": ["How can I help with your appointment?"],
            "withheld": None,
            "hand_off": False,
            "timings": {
                # The adapter's clock, read in turn: speech end 100.1, transcript final 100.2,
                # the turn's report 100.3, the reply's first audio 100.4. The caller stopped
                # speaking the detector's wait (0.5 s) before it said so: at 99.6. The agent's
                # own clock: the turn's start 100.1, first token 100.2, last token 100.3.
                "vad_ms": 500,
                "stt_ms": 100,
                "first_token_ms": 100,
                "last_token_ms": 200,
                "tool_rounds": 0,
                "total_ms": 700,
            },
        },
        {"type": "first_audio", "turn": 1, "first_audio_ms": 800},
    ]
    measured = [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn_")]
    assert measured == [
        f"turn_measured call_id={CALL_ID} turn=1 action=pass vad_ms=500 stt_ms=100"
        " first_token_ms=100 last_token_ms=200 tool_rounds=0 total_ms=700 hand_off=False",
        f"turn_first_audio call_id={CALL_ID} turn=1 first_audio_ms=800",
    ]
    assert "question" not in caplog.text
    assert "appointment" not in caplog.text


@pytest.mark.anyio
async def test_a_swallowed_turn_reaches_the_page_as_nothing(offline_deps: Deps) -> None:
    model = ScriptedModel()
    processor, playback = adapter(
        agent_in_intake(offline_deps, model, Clock(), armed=True), Clock()
    )

    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(transcript("three three", finalized=True))
        await until(lambda: len(turns(call)) == 1, "the turn's message")

    (turn,) = turns(call)
    assert (turn["action"], turn["shown"], turn["lines"]) == ("swallow", None, [])
    assert "three" not in json.dumps(turn)
    assert model.requests == []


@pytest.mark.anyio
async def test_turns_wait_for_the_finalized_transcript(offline_deps: Deps) -> None:
    model = ScriptedModel(says("One."), says("Two."))
    processor, playback = adapter(agent_in_intake(offline_deps, model, Clock()), Clock())

    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(
            transcript("first part"),
            transcript("still first", finalized=True),
            # Deepgram had already sent the text when it confirmed the finalize.
            transcript("second"),
            TranscriptionFinalizedFrame(),
            # A finalize with nothing heard is no turn.
            TranscriptionFinalizedFrame(),
        )
        await until(lambda: len(turns(call)) == 2, "two turns")

    assert [m["shown"] for m in turns(call)] == ["first part still first", "second"]
    assert [m["timings"]["stt_ms"] for m in turns(call)] == [None, None]


class Withheld:
    """A turn whose reply the output guard blocked."""

    ending: Reason | None = None

    def __init__(self) -> None:
        self.ended: Reason | None = None

    async def handle(self, transcript: str, speak: Speak | None = None) -> TurnResult:
        return TurnResult(
            action=GuardAction.PASS,
            shown=transcript,
            lines=(ADVICE_REFUSAL,),
            hand_off=False,
            timings=TurnTimings(first_token_ms=1, last_token_ms=2, tool_rounds=0),
            withheld=OutputRule.MEDICAL_ADVICE,
            phase=Phase.SERVING,
            patient_id="P001",
            last_tool=None,
        )

    async def prompt(self, script: ScriptId, speak: Speak) -> Line:
        raise AssertionError("no line is asked for")

    def heard(self, line_id: int, words: int | None = None) -> None:
        return None

    async def end(self, reason: Reason) -> None:
        self.ended = reason

    def is_emergency(self, transcript: str) -> bool:
        return False

    async def say_emergency(self, speak: Speak) -> Line:
        raise AssertionError("no emergency is said")


@pytest.mark.anyio
async def test_a_withheld_reply_reaches_the_page_by_its_rule() -> None:
    processor, playback = adapter(Withheld(), Clock())

    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(transcript("Is it serious?", finalized=True))
        await until(lambda: len(turns(call)) == 1, "the turn")

    (turn,) = turns(call)
    assert (turn["lines"], turn["withheld"]) == ([ADVICE_REFUSAL], "medical_advice")
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "a reply was withheld: ${message.withheld}" in page


class Broken(Withheld):
    async def handle(self, transcript: str, speak: Speak | None = None) -> TurnResult:
        raise RuntimeError("an unknown state")


@pytest.mark.anyio
async def test_a_failed_turn_ends_the_call(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    agent = Broken()
    processor, playback = adapter(agent, Clock())

    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(transcript("hello there", finalized=True))
        await until(lambda: call.ended, "the call to end")

    assert agent.ended is Reason.ERROR
    assert [m for m in call.messages() if m["type"] == "ended"] == [
        {"type": "ended", "reason": "error", "hint": "The call ended because of an error."}
    ]
    assert f"turn_failed call_id={CALL_ID} error=RuntimeError" in caplog.text
    assert "an unknown state" not in caplog.text


def ended_before_close(call: CallPipeline) -> list[dict[str, Any]]:
    """The page's `ended` messages, each checked to have gone down before the end of the
    pipeline."""
    end = next(i for i, f in enumerate(call.down) if isinstance(f, EndFrame))
    told = [
        (i, f.message)
        for i, f in enumerate(call.down)
        if isinstance(f, OutputTransportMessageUrgentFrame) and f.message["type"] == "ended"
    ]
    assert all(i < end for i, _message in told)
    return [message for _i, message in told]


@pytest.mark.anyio
async def test_the_page_is_told_why_a_call_ended() -> None:
    # Speech out written off: the call ends with `error`, the page is told it was speech, and
    # the check of why is asked for once.
    refused: list[None] = []
    agent = Withheld()
    processor, playback = adapter(agent, Clock(), refused)
    voice = FakeVoice()
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        # As ElevenLabs' service reports it, with its own text.
        await voice.push_error("marker-vendor-text", force_treat_as_permanent=True)
        await until(lambda: call.ended, "the call to end")
    assert agent.ended is Reason.ERROR
    assert ended_before_close(call) == [
        {
            "type": "ended",
            "reason": "error",
            "hint": "The speech service refused to speak. Check the voice id and the plan.",
        }
    ]
    assert refused == [None]
    assert "marker" not in json.dumps(call.messages())

    # A pipeline that ends for another reason: the page is told, with the general hint, and
    # nothing is checked.
    refused = []
    processor, playback = adapter(Withheld(), Clock(), refused)
    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
    assert ended_before_close(call) == [
        {"type": "ended", "reason": "error", "hint": "The call ended because of an error."}
    ]
    assert refused == []

    # A call that ends itself is told its reason once, and no hint.
    processor, playback = adapter(Closing(), Clock(), refused)
    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.send(transcript("Goodbye.", finalized=True))
        await until(lambda: call.ended, "the call to end")
    assert ended_before_close(call) == [{"type": "ended", "reason": "completed"}]
    assert refused == []

    # The page shows the reason and the hint as text, and keeps them when the connection drops.
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'show("ended", ended);' in page
    assert 'const ended = typeof message.hint === "string"' in page
    hang_up = page[page.index("const hangUp = () => {") :]
    assert '"ended"' not in hang_up[: hang_up.index("};")]


class Closing(Withheld):
    """A turn that ends the call: its last line said, the call completes."""

    ending: Reason | None = None

    async def handle(self, transcript: str, speak: Speak | None = None) -> TurnResult:
        self.ending = Reason.COMPLETED
        return await super().handle(transcript, speak)


def test_speech_to_text_options() -> None:
    seed_medications = [
        medication["name"]
        for patient in load_seed_data()["patients"]
        for medication in patient["medications"]
    ]
    service = deepgram_stt(SecretStr(VOICE_SECRETS["DEEPGRAM_API_KEY"]), keyterms(seed_medications))

    # What goes to Deepgram as query parameters (pipecat-ai 1.17.0 builds them here), but the
    # sample rate, which the pipeline sets when it starts. Nothing redacted; the clinic's own
    # words boosted.
    options = service._build_connect_kwargs()
    assert {key: value for key, value in options.items() if key != "sample_rate"} == {
        "model": "nova-3-general",
        "language": "en-US",
        "smart_format": "true",
        "numerals": "false",
        "punctuate": "true",
        "keyterm": [
            "Albuterol",
            "Atorvastatin",
            "Lee",
            "Levothyroxine",
            "Lisinopril",
            "Metformin",
            "Patel",
            "Sunrise Family Clinic",
        ],
        "interim_results": "true",
        "detect_entities": "false",
        "diarize": "false",
        "dictation": "false",
        "encoding": "linear16",
        "channels": "1",
        "multichannel": "false",
        "mip_opt_out": "true",
    }
    # Deepgram's SDK (deepgram-sdk 7.19.0) sends a list as one parameter per term, in order;
    # the clinic's name is one term.
    query = urlencode(encode_query(jsonable_encoder({"keyterm": options["keyterm"]})) or [])
    name = "keyterm"
    assert parse_qsl(query, strict_parsing=True) == [
        (name, "Albuterol"),
        (name, "Atorvastatin"),
        (name, "Lee"),
        (name, "Levothyroxine"),
        (name, "Lisinopril"),
        (name, "Metformin"),
        (name, "Patel"),
        (name, "Sunrise Family Clinic"),
    ]
    # Audio stops at speech-to-text; the agent's processor gets only frames about it.
    assert service._audio_passthrough is False


# --- set-up and lifetime of a call --------------------------------------------------------


class PassThrough(FrameProcessor):
    """Speech-to-text's place in the pipeline, without a vendor."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def no_vendor(_key: SecretStr, _keyterms: Sequence[str]) -> FrameProcessor:
    return PassThrough()


def no_speech_out(_settings: Settings) -> FrameProcessor:
    return PassThrough()


DEV_WITHOUT_VOICE: dict[str, Any] = {
    "app_env": "dev",
    "call_access_token": None,
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
            settings = offline_settings(**DEV_WITHOUT_VOICE)
            call = await open_agent_call(
                settings,
                deps_on(engine),
                ScriptedModel(),
                Vocabulary(),
                SpeechCheck(settings),
                CALL_ID,
                await offer_without_candidates(),
                "offer",
                connect_timeout=0.5,
                speech_to_text=no_vendor,
                text_to_speech=no_speech_out,
                turns=TurnRecorder(create_session_factory(engine), lambda: T0),
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
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE), config=taking_calls())

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
    fastapi_app = create_app(offline_settings(**DEV_WITHOUT_VOICE), config=taking_calls())

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": offer, "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert len(opened) == 1
    assert opened[0].pc.connectionState == "closed"


@pytest.mark.parametrize(
    "missing",
    ["anthropic_api_key", "deepgram_api_key", "elevenlabs_api_key", "elevenlabs_voice_id"],
)
def test_no_call_without_both_vendor_keys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    # Allowed in dev, and refused at set-up, before any peer connection exists.
    caplog.set_level(logging.INFO)
    opened = record_connections(monkeypatch)
    offer = asyncio.run(offer_without_candidates())
    fastapi_app = create_app(
        offline_settings(**DEV_WITHOUT_VOICE, **{missing: None}), config=taking_calls()
    )

    with TestClient(fastapi_app) as client:
        response = client.post("/offer", json={"sdp": offer, "type": "offer"})

        assert response.status_code == 400
        assert active_calls(fastapi_app) == 0

    assert opened == []
    assert "call_setup_failed" in caplog.text
    assert "error=VendorsNotConfiguredError" in caplog.text
