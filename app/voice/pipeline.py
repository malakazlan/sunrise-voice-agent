"""A call: the caller's audio through the caller gate, voice activity detection and Deepgram's
speech-to-text to the call's agent (app/agent/turn.py), its lines through ElevenLabs' text-to-
speech back to the caller, and what came of each turn to the call page as a data-channel
message.

The agent processor is a thin adapter. It starts the call when the caller connects (`start_call`,
which writes call_started), gathers the final transcripts of an utterance until speech-to-text
says the utterance is finalized, and queues it. One task of its own takes the queue in order, one
turn at a time, so the audio path never waits on the model, a tool or the database. Each line the
agent releases goes, by another task, to text-to-speech between its two marks
(app/voice/speech.py), which say when the caller has heard it. The raw transcript goes to the
agent and nowhere else: not downstream, not to the page, not to a log. The page gets what the
model was given (the guard's text, or nothing for a swallowed turn), the lines said, the name of
the output guard's rule when it withheld part of the reply (never the withheld text) and the
timings, from the moment the caller stopped speaking: the silence voice activity detection waits
for, speech-to-text, the model, and the first audio of the reply.

While the caller is muted (the opening line, the closed line, the emergency line) nothing they
say is a turn or an interruption. Otherwise, when they start speaking over a line, speech out is
interrupted: what they did not hear leaves the conversation (`heard`).

The call ends once: after the line that ends it has played (emergency, transfer, goodbye,
after-hours close: the phase says which), after the goodbye of IDLE_SECONDS twice without the
caller speaking (once, the agent asks whether they are still there) or of MAX_CALL_SECONDS, when
the caller hangs up, and with `error` on a turn that fails or a pipeline that ends without the
call deciding to. The idle timer runs only while nothing is said and no turn runs. Ending is
`end` of the agent, then an EndWorkerFrame: the transport sends what is left, then closes.

Logs carry event names, the call id, line ids, counts and milliseconds; never text.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    EndWorkerFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFinalizedFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
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
from app.agent.scripts import ScriptId
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import Line, Speak, TurnResult, start_call
from app.audit.log import Reason
from app.config import Settings
from app.voice.speech import CallerGate, LineMark, Playback, PlayoutTracker, elevenlabs_tts
from app.voice.transport import audio_transport, open_connection, server_ice_servers

logger = logging.getLogger(__name__)

# A call whose media path is not up by then is ended and its slot given back.
CONNECT_TIMEOUT_SECONDS = 30.0
# How long past MAX_CALL_SECONDS the call registry lets a call run before it stops it itself:
# the call's own goodbye comes first, after the line playing, then the transport's closing
# silence.
END_GRACE_SECONDS = 30


class Agent(Protocol):
    """What the adapter needs of the call's agent (app/agent/turn.py's CallAgent)."""

    @property
    def ending(self) -> Reason | None: ...

    async def handle(self, transcript: str, speak: Speak | None = None) -> TurnResult: ...

    async def prompt(self, script: ScriptId, speak: Speak) -> Line: ...

    def heard(self, line_id: int, words: int | None = None) -> None: ...

    async def end(self, reason: Reason) -> None: ...


# Starts the call: its agent and its opening line.
StartCall = Callable[[], Awaitable[tuple[Agent, Line]]]


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
    # monotonic instants: when voice activity detection said the caller had stopped speaking
    # (if it did) and when the transcript was final.
    speech_end: float | None
    transcribed: float


@dataclass(frozen=True, slots=True)
class _Connected:
    pass


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class AgentProcessor(FrameProcessor):
    """Starts the call when the caller connects, turns each finished utterance into a turn of
    its agent, one at a time, speaks its lines, keeps its timers and ends it."""

    def __init__(
        self,
        call_id: str,
        start: StartCall,
        playback: Playback,
        *,
        idle_seconds: float,
        max_seconds: float,
        vad_stop_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__()
        self._call_id = call_id
        self._start = start
        self._playback = playback
        self._idle_seconds = idle_seconds
        self._max_seconds = max_seconds
        self._vad_stop_seconds = vad_stop_seconds
        self._clock = clock
        self._sleep = sleep
        self._queue: asyncio.Queue[_Utterance | _Connected] = asyncio.Queue()
        self._heard: list[str] = []
        self._speech_end: float | None = None
        self._agent: Agent | None = None
        self._turns: asyncio.Task[None] | None = None
        self._speaker: asyncio.Task[None] | None = None
        self._idle: asyncio.Task[None] | None = None
        self._max: asyncio.Task[None] | None = None
        self._closer: asyncio.Task[None] | None = None
        # Why the call ends, once that is decided; whether it has ended.
        self._closing: Reason | None = None
        self._ended = False
        self._busy = False
        self._caller_speaking = False
        self._idle_prompted = False
        self._turn_count = 0
        # The turn whose first line has not been released yet, with when its caller stopped
        # speaking; then the same by the id of that line, until its first audio.
        self._awaiting_line: tuple[int, float] | None = None
        self._first_lines: dict[int, tuple[int, float]] = {}
        playback.listen(started=self._line_started, over=self._line_over)

    def connected(self) -> None:
        """The caller has connected: the call starts, ahead of any turn."""
        self._queue.put_nowait(_Connected())

    async def hang_up(self) -> None:
        """The caller has gone: the call ends now, for the reason already decided if it was
        ending, otherwise because the caller hung up."""
        await self._end_now(Reason.CALLER_HUNG_UP)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._turns = self.create_task(self._take_turns(), "agent_turns")
            self._speaker = self.create_task(self._send_lines(), "agent_lines")
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            # A pipeline that ends without the call having decided to is an error.
            await self._end_now(Reason.ERROR)
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, VADUserStartedSpeakingFrame | VADUserStoppedSpeakingFrame) or (
            isinstance(frame, TranscriptionFrame | TranscriptionFinalizedFrame)
        ):
            await self._caller(frame)
            return
        if isinstance(frame, InterimTranscriptionFrame):
            return
        await self.push_frame(frame, direction)

    async def _caller(self, frame: Frame) -> None:
        """What voice activity detection and speech-to-text report of the caller. While the
        caller is muted, none of it counts: no interruption, no turn."""
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._caller_speaking = False
        if self._playback.muted:
            self._heard = []
            self._speech_end = None
            return
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._caller_speaking = True
            self._idle_prompted = False
            self._stop_idle()
            if self._playback.interruptible:
                await self._interrupt()
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._speech_end is None:
                self._speech_end = self._clock()
            self._rearm_idle()
            return
        if isinstance(frame, TranscriptionFrame):
            self._heard.append(frame.text)
            if frame.finalized:
                self._utterance_done()
            return
        self._utterance_done()

    def _utterance_done(self) -> None:
        text = " ".join(self._heard).strip()
        self._heard = []
        if text and self._closing is None:
            self._stop_idle()
            self._queue.put_nowait(_Utterance(text, self._speech_end, self._clock()))
            self._speech_end = None

    async def _interrupt(self) -> None:
        cut = await self._playback.interrupt()
        await self.push_frame(InterruptionFrame())
        logger.info(
            "line_interrupted call_id=%s lines=%d words=%d",
            self._call_id,
            len(cut),
            sum(words for _line, words in cut),
        )
        await self._send({"type": "interrupted", "lines": len(cut)})

    # --- lines -----------------------------------------------------------------------------

    def _speak(self, line: Line) -> None:
        """A line the agent released: to be spoken after those before it."""
        self._stop_idle()
        if self._awaiting_line is not None:
            self._first_lines[line.id] = self._awaiting_line
            self._awaiting_line = None
        self._playback.release(line)

    async def _send_lines(self) -> None:
        """Each line to text-to-speech, between its marks, as long as it is still to be
        spoken: an interruption ends a line that has not been sent in full."""
        while True:
            line = await self._playback.next_to_send()
            for frame in (
                LineMark(line_id=line.id, end=False),
                TTSSpeakFrame(line.text, append_to_context=False),
                LineMark(line_id=line.id, end=True),
            ):
                if not self._playback.released(line.id):
                    break
                frame.interruptible = line.interruptible
                await self.push_frame(frame)

    async def _line_started(self, line_id: int) -> None:
        first = self._first_lines.pop(line_id, None)
        if first is None:
            return
        turn, caller_end = first
        first_audio_ms = _ms(self._clock() - caller_end)
        logger.info(
            "turn_first_audio call_id=%s turn=%d first_audio_ms=%d",
            self._call_id,
            turn,
            first_audio_ms,
        )
        await self._send({"type": "first_audio", "turn": turn, "first_audio_ms": first_audio_ms})

    async def _line_over(self, line_id: int, words: int | None) -> None:
        self._first_lines.pop(line_id, None)
        if self._agent is not None:
            self._agent.heard(line_id, words)
        self._rearm_idle()

    # --- turns -----------------------------------------------------------------------------

    async def _take_turns(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, _Connected):
                await self._open()
                continue
            if self._agent is None or self._closing is not None:
                continue
            self._turn_count += 1
            caller_end = self._caller_end(item)
            self._awaiting_line = (self._turn_count, caller_end)
            self._busy = True
            self._stop_idle()
            try:
                result = await self._agent.handle(item.text, self._speak)
            except Exception as error:
                # An unknown state ends the call rather than carry on from it.
                logger.error("turn_failed call_id=%s error=%s", self._call_id, type(error).__name__)
                self._close(Reason.ERROR)
                return
            finally:
                self._busy = False
                self._awaiting_line = None
            await self._report(item, caller_end, result)
            ending = self._agent.ending
            if ending is not None:
                self._close(ending)
                return
            self._rearm_idle()

    async def _open(self) -> None:
        try:
            agent, opening = await self._start()
        except Exception as error:
            logger.error(
                "call_start_failed call_id=%s error=%s", self._call_id, type(error).__name__
            )
            self._closing = Reason.ERROR
            await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)
            return
        self._agent = agent
        self._playback.release(opening, opening=True)
        await self._send({"type": "opening", "lines": [opening.text]})
        self._max = self.create_task(self._at_most(), "call_max_duration")

    def _caller_end(self, heard: _Utterance) -> float:
        """When the caller stopped speaking: before voice activity detection's wait, if it
        reported the end of their speech."""
        if heard.speech_end is None:
            return heard.transcribed
        return heard.speech_end - self._vad_stop_seconds

    async def _report(self, heard: _Utterance, caller_end: float, result: TurnResult) -> None:
        done = self._clock()
        timings = {
            "vad_ms": None if heard.speech_end is None else _ms(self._vad_stop_seconds),
            "stt_ms": None
            if heard.speech_end is None
            else _ms(heard.transcribed - heard.speech_end),
            "first_token_ms": result.timings.first_token_ms,
            "last_token_ms": result.timings.last_token_ms,
            "tool_rounds": result.timings.tool_rounds,
            "total_ms": _ms(done - caller_end),
        }
        logger.info(
            "turn_measured call_id=%s turn=%d action=%s vad_ms=%s stt_ms=%s first_token_ms=%s"
            " last_token_ms=%s tool_rounds=%d total_ms=%d hand_off=%s",
            self._call_id,
            self._turn_count,
            result.action.value,
            timings["vad_ms"],
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
                "turn": self._turn_count,
                "action": result.action.value,
                "shown": result.shown,
                "lines": list(result.lines),
                "withheld": None if result.withheld is None else result.withheld.value,
                "hand_off": result.hand_off,
                "timings": timings,
            }
        )

    # --- timers ----------------------------------------------------------------------------

    def _quiet(self) -> bool:
        """Whether the call waits for the caller: started, not ending, no turn running or
        queued, the caller not speaking and every line over."""
        return (
            self._agent is not None
            and self._closing is None
            and not self._busy
            and not self._caller_speaking
            and self._queue.empty()
            and self._playback.quiet
        )

    def _stop_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _rearm_idle(self) -> None:
        self._stop_idle()
        if self._quiet():
            self._idle = self.create_task(self._idle_after(), "call_idle")

    async def _idle_after(self) -> None:
        await self._sleep(self._idle_seconds)
        self._idle = None
        if self._agent is None or not self._quiet():
            return
        if self._idle_prompted:
            logger.info("call_idle call_id=%s", self._call_id)
            self._close(Reason.IDLE_TIMEOUT, goodbye=True)
            return
        self._idle_prompted = True
        logger.info("idle_prompt call_id=%s", self._call_id)
        line = await self._agent.prompt(ScriptId.STILL_THERE, self._speak)
        await self._send({"type": "said", "lines": [line.text]})

    async def _at_most(self) -> None:
        await self._sleep(self._max_seconds)
        self._max = None
        logger.info("call_max_duration call_id=%s", self._call_id)
        self._close(Reason.MAX_DURATION, goodbye=True)

    # --- the end ---------------------------------------------------------------------------

    def _close(self, reason: Reason, *, goodbye: bool = False) -> None:
        """Decide that the call ends, for `reason`: no more turns; the goodbye first if asked
        for, once every line is over; the end once the last line is over."""
        if self._closing is not None:
            return
        self._closing = reason
        current = asyncio.current_task()
        for task in (self._turns, self._idle, self._max):
            if task is not None and task is not current:
                task.cancel()
        self._idle = None
        self._closer = self.create_task(self._closed_after(reason, goodbye), "call_closing")

    async def _closed_after(self, reason: Reason, goodbye: bool) -> None:
        logger.info("call_closing call_id=%s reason=%s", self._call_id, reason.value)
        if goodbye and self._agent is not None:
            await self._playback.all_over()
            line = await self._agent.prompt(ScriptId.GOODBYE, self._speak)
            await self._send({"type": "said", "lines": [line.text]})
        await self._playback.all_over()
        await self._finish(reason)
        await self._send({"type": "ended", "reason": reason.value})
        await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    async def _end_now(self, reason: Reason) -> None:
        """End the call at once: for the reason already decided, if any."""
        self._closing = self._closing or reason
        current = asyncio.current_task()
        for task in (self._turns, self._speaker, self._idle, self._max, self._closer):
            if task is not None and task is not current and not task.done():
                await self.cancel_task(task)
        self._idle = None
        await self._finish(self._closing)

    async def _finish(self, reason: Reason) -> None:
        if self._ended or self._agent is None:
            return
        self._ended = True
        try:
            await self._agent.end(reason)
        except Exception as error:
            logger.error("call_end_failed call_id=%s error=%s", self._call_id, type(error).__name__)
            return
        logger.info("call_life_ended call_id=%s reason=%s", self._call_id, reason.value)

    async def _send(self, message: dict[str, Any]) -> None:
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))


class AgentCall:
    def __init__(
        self,
        call_id: str,
        connection: SmallWebRTCConnection,
        connect_timeout: float,
        agent: AgentProcessor,
        playback: Playback,
        vad: SileroVADAnalyzer,
        stt: FrameProcessor,
        tts: FrameProcessor,
    ) -> None:
        self._call_id = call_id
        self._connection = connection
        self._connect_timeout = connect_timeout
        self._connected = asyncio.Event()
        self._stopped = False
        self._agent = agent
        transport = audio_transport(connection)
        pipeline = Pipeline(
            [
                transport.input(),
                CallerGate(playback),
                VADProcessor(vad_analyzer=vad),
                stt,
                agent,
                tts,
                transport.output(),
                PlayoutTracker(playback),
            ]
        )
        # No RTVI (the page reads its own messages) and no idle timeout: the call ends itself,
        # and the call registry bounds its lifetime. A processor that can no longer work
        # (speech-to-text or text-to-speech that cannot connect) ends the call.
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
        self._agent.connected()

    async def _on_client_disconnected(
        self, transport: SmallWebRTCTransport, client: SmallWebRTCConnection
    ) -> None:
        logger.info("call_disconnected call_id=%s", self._call_id)
        await self._agent.hang_up()
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
    """A call needs the model, speech-to-text and speech out; without their settings it is
    refused."""


async def open_agent_call(
    settings: Settings,
    deps: Deps,
    model: Model | None,
    call_id: str,
    sdp: str,
    sdp_type: str,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    speech_to_text: Callable[[SecretStr], FrameProcessor] = deepgram_stt,
    text_to_speech: Callable[[Settings], FrameProcessor] = elevenlabs_tts,
) -> AgentCall:
    if (
        model is None
        or settings.deepgram_api_key is None
        or settings.elevenlabs_api_key is None
        or settings.elevenlabs_voice_id is None
    ):
        raise VendorsNotConfiguredError("a call needs every vendor's settings")
    stt = speech_to_text(settings.deepgram_api_key)
    tts = text_to_speech(settings)
    # Loading the VAD's model reads a file; off the event loop, so other calls' audio flows.
    vad = await asyncio.to_thread(
        SileroVADAnalyzer, params=VADParams(stop_secs=settings.vad_stop_seconds)
    )
    ice_servers = server_ice_servers(settings, now=time.time())
    # Pipecat's own connect timeout closes the peer connection from inside the task it then
    # cancels (pipecat-ai 1.17.0), leaving the close unfinished when TURN is in use, so every
    # later close waits forever. It is set past the longest call so that it never fires; this
    # call ends itself after connect_timeout instead.
    connection = await open_connection(
        sdp, sdp_type, ice_servers, connection_timeout_secs=2 * settings.max_call_seconds
    )
    playback = Playback()
    agent = AgentProcessor(
        call_id,
        partial(start_call, call_id, deps, model, register_tools()),
        playback,
        idle_seconds=settings.idle_seconds,
        max_seconds=settings.max_call_seconds,
        vad_stop_seconds=settings.vad_stop_seconds,
    )
    try:
        return AgentCall(call_id, connection, connect_timeout, agent, playback, vad, stt, tts)
    except BaseException:
        await connection.disconnect()
        raise
