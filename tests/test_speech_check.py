"""Why speech out refused: once a call has ended with `error` because speech out could no longer
work, one small request to ElevenLabs' plain text-to-speech endpoint, in the background, and
one log line with its status and ElevenLabs' error code from a fixed set. ElevenLabs is a local
fake here (an httpx2 mock transport): nothing reaches the network."""

import asyncio
import json
import logging

import httpx2
import pytest
from conftest import VOICE_SECRETS, offline_settings
from pipecat.frames.frames import EndFrame
from speech_fakes import WAIT_SECONDS, CallPipeline, FakeTime, FakeVoice, until
from test_pipeline import CALL_ID, OPENING, Withheld

from app.agent.scripts import ScriptId
from app.agent.turn import Line
from app.audit.log import Reason
from app.guard.logging import configure_logging
from app.voice.pipeline import Agent, AgentProcessor
from app.voice.speech import Playback
from app.voice.speech_check import CHECK_DEADLINE_SECONDS, SpeechCheck

pytestmark = pytest.mark.anyio

KEY = VOICE_SECRETS["ELEVENLABS_API_KEY"]
VOICE = VOICE_SECRETS["ELEVENLABS_VOICE_ID"]

# ElevenLabs' answer on the founder's call, its message replaced by a marker.
PAID_PLAN = {
    "detail": {
        "type": "payment_required",
        "code": "paid_plan_required",
        "message": "marker-vendor-message about the plan",
        "status": "payment_required",
        "request_id": "marker-request-id",
    }
}


class Vendor:
    """ElevenLabs as a local fake: each request gets the next answer; the requests are kept."""

    def __init__(self, *answers: httpx2.Response | Exception | asyncio.Event) -> None:
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, asyncio.Event):
            await answer.wait()
            raise AssertionError("a hanging request is given up first")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


def check_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "app.voice.speech_check"]


async def test_only_codes_are_logged(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(offline_settings())
    capsys.readouterr()
    caplog.set_level(logging.DEBUG)
    answers: list[tuple[httpx2.Response | Exception, str]] = [
        (httpx2.Response(402, json=PAID_PLAN), "speech_refused status=402 code=paid_plan_required"),
        (
            httpx2.Response(
                404,
                json={
                    "detail": {
                        "type": "not_found",
                        "code": "voice_not_found",
                        "message": f"marker voice {VOICE} was not found",
                    }
                },
            ),
            "speech_refused status=404 code=voice_not_found",
        ),
        (
            httpx2.Response(
                401,
                json={
                    "detail": {
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                        "message": "marker Invalid API key",
                    }
                },
            ),
            "speech_refused status=401 code=invalid_api_key",
        ),
        # A code ElevenLabs may add one day; and its legacy shape, with no code.
        (
            httpx2.Response(400, json={"detail": {"code": "marker_new_code", "message": "x"}}),
            "speech_refused status=400 code=other",
        ),
        (
            httpx2.Response(401, json={"detail": {"status": "invalid_api_key"}}),
            "speech_refused status=401 code=other",
        ),
        # Not JSON; JSON of another shape; a body that holds the key, as a code and as text.
        (
            httpx2.Response(502, content=b"<html>marker-gateway down</html>"),
            "speech_refused status=502 code=other",
        ),
        (
            httpx2.Response(422, json={"detail": [{"msg": "marker-validation"}]}),
            "speech_refused status=422 code=other",
        ),
        (
            httpx2.Response(403, json={"detail": {"code": KEY, "message": f"marker {KEY}"}}),
            "speech_refused status=403 code=other",
        ),
        (
            httpx2.Response(403, json={"detail": {"code": ["voice_not_found"]}}),
            "speech_refused status=403 code=other",
        ),
        (httpx2.Response(200, content=b"\xff\xfb audio"), "speech_check_ok"),
        (
            httpx2.ConnectError(f"marker cannot reach {VOICE}"),
            "speech_check_failed error=ConnectError",
        ),
    ]
    expected = [line for _answer, line in answers]

    for answer, _line in answers:
        vendor = Vendor(answer)
        check = SpeechCheck(offline_settings(), transport=vendor.transport())
        check.start()
        await check.drain()
        (request,) = vendor.requests
        # ElevenLabs' plain text-to-speech endpoint, the configured voice and model, two words.
        assert request.method == "POST"
        assert str(request.url) == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}"
        assert request.headers["xi-api-key"] == KEY
        assert json.loads(request.content) == {
            "text": "Speech check.",
            "model_id": "eleven_flash_v2_5",
        }

    assert check_lines(caplog) == expected
    written = capsys.readouterr().err
    for line in expected:
        assert line in written
    for secret in (KEY, VOICE, "marker", "Speech check"):
        assert secret not in caplog.text, secret
        assert secret not in written, secret


def adapter(agent: Agent, check: SpeechCheck, time: FakeTime) -> tuple[AgentProcessor, Playback]:
    async def start() -> tuple[Agent, Line]:
        return agent, Line(id=0, text=OPENING, script=ScriptId.GREETING)

    playback = Playback()
    processor = AgentProcessor(
        CALL_ID,
        start,
        playback,
        idle_seconds=30,
        max_seconds=600,
        vad_stop_seconds=0.5,
        speech_refused=check.start,
        clock=time.clock,
        sleep=time.sleep,
    )
    return processor, playback


async def test_check_is_off_the_call_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    never = asyncio.Event()
    vendor = Vendor(never, httpx2.Response(402, json=PAID_PLAN))
    now = [1000.0]
    check = SpeechCheck(
        offline_settings(), transport=vendor.transport(), deadline=0.2, clock=lambda: now[0]
    )
    agent = Withheld()
    processor, playback = adapter(agent, check, FakeTime())
    voice = FakeVoice()

    # Speech out is written off: the call ends with `error` while ElevenLabs has not answered
    # the check, and nothing of the call waits for it.
    async with CallPipeline(processor, playback, voice) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await voice.push_error("marker refused", force_treat_as_permanent=True)
        await until(lambda: call.ended, "the call to end")
        assert agent.ended is Reason.ERROR
        assert len(vendor.requests) == 1
        assert check_lines(caplog) == []

    # Given up after its deadline, and logged.
    await asyncio.wait_for(check.drain(), WAIT_SECONDS)
    assert check_lines(caplog) == ["speech_check_failed error=TimeoutError"]
    assert CHECK_DEADLINE_SECONDS == 5

    # At most one request a minute, whatever the calls: a second one within the minute asks
    # nothing; one after it asks again.
    now[0] += 59
    check.start()
    await asyncio.wait_for(check.drain(), WAIT_SECONDS)
    assert len(vendor.requests) == 1
    now[0] += 2
    check.start()
    await asyncio.wait_for(check.drain(), WAIT_SECONDS)
    assert len(vendor.requests) == 2
    assert check_lines(caplog)[-1] == "speech_refused status=402 code=paid_plan_required"

    # A pipeline that ends for any other reason asks nothing.
    other = SpeechCheck(offline_settings(), transport=Vendor().transport())
    processor, playback = adapter(Withheld(), other, FakeTime())
    async with CallPipeline(processor, playback, FakeVoice()) as call:
        processor.connected()
        await until(lambda: playback.quiet, "the opening line")
        await call.worker.queue_frame(EndFrame())
        await until(lambda: call.ended, "the call to end")
    await other.drain()
    assert len(check_lines(caplog)) == 2
