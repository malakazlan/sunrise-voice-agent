"""Voice out: what the agent says goes to text-to-speech a whole guarded line at a time; the
greeting, the closed line and the emergency line play to the end whatever the caller does; any
other line stops when the caller speaks, and the conversation keeps only what was heard; and no
log line carries what is said. Text-to-speech and the transport's output are a fake that plays
lines when the test says (tests/speech_fakes.py); the model is scripted; nothing reaches a
vendor."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator

import pytest
from conftest import VOICE_SECRETS, offline_settings
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    TextFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from speech_fakes import (
    CallPipeline,
    FakeTime,
    FakeVoice,
    agent_call,
    caller_says,
    settle,
    until,
)
from test_turn import (
    ADVICE_REFUSAL,
    CLOSED,
    EMERGENCY,
    EVENING,
    FALLBACK,
    GREETING,
    ScriptedModel,
    cut_off,
    deps_on,
    new_call_id,
    says,
)

from app.agent.endings import drain_call_events
from app.agent.tool_registry import Deps
from app.agent.turn import Line, drain_guard_audits
from app.db.session import create_database_engine
from app.guard.logging import configure_logging
from app.voice.speech import elevenlabs_tts

pytestmark = pytest.mark.anyio

BLOCKED = "Let me see. You should stop taking marker-pills today. Then call us back."
LOUD = b"\x05\x10" * 160


@pytest.fixture
async def offline_deps() -> AsyncIterator[Deps]:
    """A call's dependencies on a database that is never reached: the call's events fail to be
    written, and are logged."""
    engine = create_database_engine(offline_settings())
    try:
        yield deps_on(engine)
    finally:
        await drain_guard_audits(timeout=1)
        await drain_call_events()
        await engine.dispose()


def texts(voice: FakeVoice) -> list[str]:
    return [text for text, _interruptible in voice.spoken]


# --- R1 ---------------------------------------------------------------------------------


async def test_only_guarded_sentences_are_spoken(offline_deps: Deps) -> None:
    model = ScriptedModel(says(BLOCKED), cut_off(), says("Is there anything else?"))
    time = FakeTime()
    processor, playback = agent_call(new_call_id(), offline_deps, model, time)
    voice = FakeVoice()

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: len(voice.spoken) == 1, "the greeting")
        await call.send(*caller_says("Should I stop my pills?"))
        await until(lambda: len(voice.spoken) == 3, "the guarded reply")
        await call.send(*caller_says("When is my next appointment?"))
        await until(lambda: len(voice.spoken) == 4, "the fallback")
        await call.send(*caller_says("Thanks."))
        await until(lambda: len(voice.spoken) == 5, "the last reply")

    # What passed the output guard, each line whole: the sentence before the blocked one, the
    # block's line in its place; a reply cut off mid-sentence is never spoken, the fallback is.
    assert texts(voice) == [
        GREETING,
        "Let me see.",
        ADVICE_REFUSAL,
        FALLBACK,
        "Is there anything else?",
    ]
    for text in texts(voice):
        assert text.endswith((".", "?", "!")), text
    # Nothing else that could be spoken went down: no model text, no tokens.
    assert [
        f for f in call.down if isinstance(f, TextFrame) and not isinstance(f, TTSTextFrame)
    ] == []
    assert len(call.frames(TTSSpeakFrame)) == 0  # every one was taken by text-to-speech
    for message in call.messages():
        for blocked in ("marker-pills", "stop taking", "Then call us back", "appointment is on"):
            assert blocked not in str(message)
    assert "stop" not in voice.played


# --- R2 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["greeting", "closed", "emergency"])
async def test_uninterruptible_lines(offline_deps: Deps, line: str) -> None:
    deps = offline_deps
    if line == "closed":
        deps = Deps(
            sessions=offline_deps.sessions,
            audit=offline_deps.audit,
            now=lambda: EVENING,
            claim_keys=offline_deps.claim_keys,
        )
    model = ScriptedModel(says("How can I help?"))
    time = FakeTime()
    processor, playback = agent_call(new_call_id(), deps, model, time)
    voice = FakeVoice(autoplay=False)
    expected = {"greeting": GREETING, "closed": CLOSED, "emergency": EMERGENCY}[line]

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the opening line")
        if line == "emergency":
            await voice.start(0)
            await voice.finish()
            await call.send(*caller_says("I think I'm having a heart attack"))
            await until(lambda: voice.waiting == 1, "the emergency line")
        assert voice.spoken[-1] == (expected, False)

        await voice.start(3)
        # The caller talks over it: the audio is silenced before voice activity detection and
        # speech-to-text, and what they report anyway is neither an interruption nor a turn.
        await call.send(InputAudioRawFrame(LOUD, 16000, 1))
        await call.send(*caller_says("Hello? Hello, can you hear me?"))
        await call.send(SleepFrame(sleep=0.1))
        assert call.frames(InterruptionFrame) == []
        assert model.requests == []
        heard = [f for f in call.frames(InputAudioRawFrame)]
        assert [f.audio for f in heard] == [bytes(len(LOUD))]
        await voice.finish()
        assert voice.played[-len(expected.split()) :] == expected.split()

        if line == "emergency":
            # The call ends after the line; nothing the caller said became a turn.
            await until(lambda: call.ended, "the call to end")
            assert model.requests == []
            return
        # Once it has played, the caller is heard again.
        await call.send(InputAudioRawFrame(LOUD, 16000, 1))
        await call.send(*caller_says("Hello."))
        await until(lambda: len(model.requests) == 1, "the caller's turn")
        assert [f.audio for f in call.frames(InputAudioRawFrame)][-1] == LOUD


# --- R3 ---------------------------------------------------------------------------------


async def test_interruption_drops_the_rest(offline_deps: Deps) -> None:
    model = ScriptedModel(says(BLOCKED), says("Sure, what is your question?"))
    time = FakeTime()
    processor, playback = agent_call(new_call_id(), offline_deps, model, time)
    voice = FakeVoice(autoplay=False)

    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        await voice.play_all()
        await call.send(*caller_says("Should I stop my pills?"))
        await until(lambda: voice.waiting == 2, "the reply's two lines")
        assert voice.spoken[-2:] == [("Let me see.", True), (ADVICE_REFUSAL, True)]

        # Two words in, the caller speaks: speech stops, the second line never plays.
        await voice.start(2)
        await call.send(VADUserStartedSpeakingFrame())
        await until(lambda: len(call.frames(InterruptionFrame)) == 1, "the interruption")
        assert voice.waiting == 0
        await call.send(*caller_says("Actually, I have another question.")[1:])
        await until(lambda: len(model.requests) == 2, "the next turn")
        await until(lambda: voice.waiting == 1, "the next reply")

        # A line released and cut before it has been sent at all is never sent.
        playback.release(Line(id=1000, text="Never said.", script=None))
        await playback.interrupt()
        await settle()
        assert "Never said." not in texts(voice)

    assert voice.played[-2:] == ["Let", "me"]
    # The conversation holds what the caller heard: two words of the first line, and nothing
    # of the line that never played.
    assert model.requests[1].messages[-3:] == [
        {"role": "user", "content": "Should I stop my pills?"},
        {"role": "assistant", "content": "Let me"},
        {"role": "user", "content": "Actually, I have another question."},
    ]
    assert ADVICE_REFUSAL not in str(model.requests[1].messages)
    assert {"type": "interrupted", "lines": 2} in call.messages()


# --- R12 --------------------------------------------------------------------------------


async def test_voice_logs_carry_no_text(
    offline_deps: Deps, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    # The application's own lines, through a call with a turn, a block and an interruption.
    caplog.set_level(logging.DEBUG)
    model = ScriptedModel(says(BLOCKED))
    time = FakeTime()
    processor, playback = agent_call(new_call_id(), offline_deps, model, time)
    voice = FakeVoice(autoplay=False)
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: voice.waiting == 1, "the greeting")
        await voice.play_all()
        await call.send(*caller_says("marker-caller-words about my pills"))
        await until(lambda: voice.waiting == 2, "the reply")
        await voice.start(2)
        await call.send(VADUserStartedSpeakingFrame())
        await until(lambda: len(call.frames(InterruptionFrame)) == 1, "the interruption")
    logged = re.sub(r"(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ", "", caplog.text)
    assert "line_interrupted" in logged
    for text in ("marker-caller", "pills", "Let me", "Thank you for", "give medical advice"):
        assert text not in logged, text

    # Pipecat's own records, through the application's logging, from the real ElevenLabs service
    # on a local socket that closes every connection: one line it cannot speak for want of
    # audio, one once it has been written off. Pipecat writes both records with the text in them.
    configure_logging(offline_settings())
    capsys.readouterr()
    server = await asyncio.start_server(close_at_once, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        tts = elevenlabs_tts(offline_settings(), url=url, stop_frame_timeout_s=0.2)
        await run_test(
            tts,
            frames_to_send=[TTSSpeakFrame("marker-spoken-one is said."), SleepFrame(sleep=0.6)],
        )
        tts = elevenlabs_tts(offline_settings(), url=url, stop_frame_timeout_s=0.2)
        await tts.set_usable(False)
        await run_test(
            tts,
            frames_to_send=[TTSSpeakFrame("marker-spoken-two is said."), SleepFrame(sleep=0.3)],
        )
    finally:
        server.close()
        await server.wait_closed()
    written = capsys.readouterr().err
    # Its records about the connection are written; the text is not.
    assert "ElevenLabsTTSService" in written
    for text in ("marker-spoken", "is said"):
        assert text not in written, text


async def close_at_once(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


def test_speech_out_options() -> None:
    service = elevenlabs_tts(offline_settings())

    # ElevenLabs' multi-stream WebSocket (pipecat-ai 1.17.0 builds the URL here): the voice in
    # the path, Flash v2.5, auto mode for whole sentences, English. The output format is set
    # when the pipeline starts.
    url = service._build_websocket_url()
    voice = VOICE_SECRETS["ELEVENLABS_VOICE_ID"]
    assert url.startswith(
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice}/multi-stream-input"
        "?model_id=eleven_flash_v2_5&output_format="
    )
    assert "&auto_mode=true" in url
    assert url.endswith("&language_code=en")
    # Voice settings are ElevenLabs' own defaults: none are sent.
    assert service._voice_settings is None
