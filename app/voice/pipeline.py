"""A call: the caller's audio through voice activity detection and Deepgram's speech-to-text
to the call's agent (app/agent/turn.py), and what came of each turn to the call page as a
data-channel message. Nothing is spoken back yet.

The agent processor is a thin adapter: it gathers the final transcripts of an utterance until
the speech-to-text service says the utterance is finalized, and queues it. One task of its own
takes the queue in order, one turn at a time, so the audio path never waits on the model, a
tool or the database. The raw transcript goes to the agent and nowhere else: not downstream,
not to the page, not to a log. The page gets what the model was given (the guard's text, or
nothing for a swallowed turn), the lines said and the timings.

Logs carry event names, the call id, counts and milliseconds; never text.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    EndWorkerFrame,
    Frame,
    InterimTranscriptionFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFinalizedFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr

from app.agent.model import Model
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import TurnResult, start_call
from app.config import Settings
from app.voice.transport import audio_transport, open_connection, server_ice_servers

logger = logging.getLogger(__name__)

# A call whose media path is not up by then is ended and its slot given back.
CONNECT_TIMEOUT_SECONDS = 30.0
# The silence after which the caller is taken to have stopped speaking, and the speech-to-text
# service is asked to finalize what it heard. Shorter cuts a pause between spoken digits into
# two turns (the SSN step takes them across turns); longer adds to every reply's wait.
STOP_SECS = 0.8


class Turns(Protocol):
    async def handle(self, transcript: str) -> TurnResult: ...


def deepgram_stt(api_key: SecretStr) -> DeepgramSTTService:
    """Deepgram's streaming speech-to-text, its options chosen for digits and names:
    smart formatting writes numbers as numerals ("three three four four" becomes 3344) and
    dates as dates ("March 14, 1985"), which the input guard reads either way; numerals is
    left off, smart formatting covers it; a spelled name stays letters ("J O H N S O N").
    The call opts out of Deepgram's model improvement programme. Audio stops here."""
    return DeepgramSTTService(
        api_key=api_key.get_secret_value(),
        mip_opt_out=True,
        audio_passthrough=False,
        settings=DeepgramSTTService.Settings(
            model="nova-3-general",
            language=Language.EN_US,
            smart_format=True,
            numerals=False,
            punctuate=True,
        ),
    )


@dataclass(frozen=True, slots=True)
class _Utterance:
    text: str = field(repr=False)
    # monotonic instants: when the caller stopped speaking (if the VAD said so) and when the
    # transcript was final.
    speech_end: float | None
    transcribed: float


@dataclass(frozen=True, slots=True)
class _Opening:
    line: str = field(repr=False)


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class AgentProcessor(FrameProcessor):
    """Turns each finished utterance into a turn of the call's agent, one at a time, and sends
    what came of it to the page."""

    def __init__(
        self,
        call_id: str,
        turns: Turns,
        opening: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._call_id = call_id
        self._turns = turns
        self._opening = opening
        self._clock = clock
        self._queue: asyncio.Queue[_Utterance | _Opening] = asyncio.Queue()
        self._heard: list[str] = []
        self._speech_end: float | None = None
        self._worker: asyncio.Task[None] | None = None

    def greet(self) -> None:
        """Send the opening line, ahead of any turn."""
        self._queue.put_nowait(_Opening(self._opening))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._worker = self.create_task(self._take_turns(), "agent_turns")
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_turns()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._speech_end is None:
            self._speech_end = self._clock()
        if isinstance(frame, TranscriptionFrame):
            self._heard.append(frame.text)
            if frame.finalized:
                self._utterance_done()
            return
        if isinstance(frame, TranscriptionFinalizedFrame):
            self._utterance_done()
            return
        if isinstance(frame, InterimTranscriptionFrame):
            return
        await self.push_frame(frame, direction)

    def _utterance_done(self) -> None:
        text = " ".join(self._heard).strip()
        self._heard = []
        if text:
            self._queue.put_nowait(_Utterance(text, self._speech_end, self._clock()))
            self._speech_end = None

    async def _stop_turns(self) -> None:
        if self._worker is not None:
            await self.cancel_task(self._worker)
            self._worker = None

    async def _take_turns(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, _Opening):
                await self._send({"type": "opening", "lines": [item.line]})
                continue
            try:
                result = await self._turns.handle(item.text)
            except Exception as error:
                # An unknown state stops the call rather than carry on from it.
                logger.error("turn_failed call_id=%s error=%s", self._call_id, type(error).__name__)
                await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)
                return
            await self._report(item, result)

    async def _report(self, heard: _Utterance, result: TurnResult) -> None:
        done = self._clock()
        timings = {
            "stt_ms": None
            if heard.speech_end is None
            else _ms(heard.transcribed - heard.speech_end),
            "first_token_ms": result.timings.first_token_ms,
            "last_token_ms": result.timings.last_token_ms,
            "tool_rounds": result.timings.tool_rounds,
            "total_ms": _ms(done - (heard.speech_end or heard.transcribed)),
        }
        logger.info(
            "turn_measured call_id=%s action=%s stt_ms=%s first_token_ms=%s last_token_ms=%s"
            " tool_rounds=%d total_ms=%d hand_off=%s",
            self._call_id,
            result.action.value,
            timings["stt_ms"],
            timings["first_token_ms"],
            timings["last_token_ms"],
            timings["tool_rounds"],
            timings["total_ms"],
            result.hand_off,
        )
        await self._send(
            {
                "type": "turn",
                "action": result.action.value,
                "shown": result.shown,
                "lines": list(result.lines),
                "hand_off": result.hand_off,
                "timings": timings,
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))


class AgentCall:
    def __init__(
        self,
        call_id: str,
        connection: SmallWebRTCConnection,
        connect_timeout: float,
        agent: AgentProcessor,
        vad: SileroVADAnalyzer,
        stt: FrameProcessor,
    ) -> None:
        self._call_id = call_id
        self._connection = connection
        self._connect_timeout = connect_timeout
        self._connected = asyncio.Event()
        self._stopped = False
        self._agent = agent
        transport = audio_transport(connection)
        pipeline = Pipeline(
            [transport.input(), VADProcessor(vad_analyzer=vad), stt, agent, transport.output()]
        )
        # No RTVI (the page reads its own messages) and no idle timeout: the call registry
        # owns the call's lifetime. A processor that can no longer work (speech-to-text that
        # cannot connect) ends the call.
        self._worker = PipelineWorker(
            pipeline,
            enable_rtvi=False,
            idle_timeout_secs=None,
            processor_unusable_policy=ProcessorUnusablePolicy.END,
        )
        self._runner = WorkerRunner(handle_sigint=False)
        transport.add_event_handler("on_client_connected", self._on_client_connected)
        transport.add_event_handler("on_client_disconnected", self._on_client_disconnected)

    async def _on_client_connected(
        self, transport: SmallWebRTCTransport, client: SmallWebRTCConnection
    ) -> None:
        logger.info("call_connected call_id=%s", self._call_id)
        self._connected.set()
        self._agent.greet()

    async def _on_client_disconnected(
        self, transport: SmallWebRTCTransport, client: SmallWebRTCConnection
    ) -> None:
        logger.info("call_disconnected call_id=%s", self._call_id)
        await self._runner.cancel()

    def answer(self) -> dict[str, str]:
        answer = self._connection.get_answer()
        return {"sdp": answer["sdp"], "type": answer["type"]}

    async def run(self) -> None:
        await self._runner.add_workers(self._worker)
        # run() clears the runner's cancel request, so a stop that came first must win here.
        if self._stopped:
            return
        running = asyncio.ensure_future(self._runner.run())
        connected = asyncio.ensure_future(self._connected.wait())
        try:
            done, _pending = await asyncio.wait(
                {running, connected},
                timeout=self._connect_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.info("call_not_connected call_id=%s", self._call_id)
                await self._runner.cancel()
            await running
        finally:
            connected.cancel()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self._runner.cancel()
        await self._connection.disconnect()


class VendorsNotConfiguredError(RuntimeError):
    """A call needs the model and speech-to-text; without their keys it is refused."""


async def open_agent_call(
    settings: Settings,
    deps: Deps,
    model: Model | None,
    call_id: str,
    sdp: str,
    sdp_type: str,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    speech_to_text: Callable[[SecretStr], FrameProcessor] = deepgram_stt,
) -> AgentCall:
    if model is None or settings.deepgram_api_key is None:
        raise VendorsNotConfiguredError("a call needs both vendor keys")
    turns, opening = start_call(call_id, deps, model, register_tools())
    stt = speech_to_text(settings.deepgram_api_key)
    # Loading the VAD's model reads a file; off the event loop, so other calls' audio flows.
    vad = await asyncio.to_thread(SileroVADAnalyzer, params=VADParams(stop_secs=STOP_SECS))
    ice_servers = server_ice_servers(settings, now=time.time())
    # Pipecat's own connect timeout closes the peer connection from inside the task it then
    # cancels (pipecat-ai 1.17.0), leaving the close unfinished when TURN is in use, so every
    # later close waits forever. It is set past the longest call so that it never fires; this
    # call ends itself after connect_timeout instead.
    connection = await open_connection(
        sdp, sdp_type, ice_servers, connection_timeout_secs=2 * settings.max_call_seconds
    )
    try:
        return AgentCall(
            call_id, connection, connect_timeout, AgentProcessor(call_id, turns, opening), vad, stt
        )
    except BaseException:
        await connection.disconnect()
        raise
