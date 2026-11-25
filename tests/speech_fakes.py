"""What surrounds the voice adapter in a call, without a vendor or the network: the text-to-speech
service and the transport's output (one fake that plays each line when a test says so), a clock
whose timers fire when a test moves it, and a pipeline to run a call's processors in."""

import asyncio
import heapq
import itertools
from collections import deque
from collections.abc import Callable
from functools import partial
from typing import Any

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import QueuedFrameProcessor
from pipecat.utils.text.base_text_aggregator import TextType
from pipecat.workers.runner import WorkerRunner

from app.agent.model import Model
from app.agent.tool_registry import Deps
from app.agent.tools import register_tools
from app.agent.turn import start_call
from app.agent.turn_record import TurnRecorder
from app.voice.pipeline import AgentProcessor
from app.voice.speech import CallerGate, LineMark, Playback, PlayoutTracker

# How long a test waits for something the pipeline is to do before it fails.
WAIT_SECONDS = 5.0


class FakeTime:
    """A clock that stands still, and sleeps that end when the test moves it past them."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        timer: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + seconds, next(self._order), timer))
        await timer

    @property
    def waiting(self) -> int:
        """The sleeps still waiting."""
        return sum(1 for _at, _n, timer in self._timers if not timer.done())

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        while self._timers and self._timers[0][0] <= self.now:
            _at, _n, timer = heapq.heappop(self._timers)
            if not timer.done():
                timer.set_result(None)
        await settle()


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(20):
        await asyncio.sleep(0)


class FakeVoice(FrameProcessor):
    """The text-to-speech service and the transport's output. Each line (its start mark, its
    TTSSpeakFrame, its end mark) waits, in order, until the test plays it; with `autoplay` each
    plays in full as it arrives. What comes out is what the real pair sends downstream: the
    start mark, TTSStartedFrame, audio, a TTSTextFrame per word, TTSStoppedFrame, the end mark.
    An interruption drops the interruptible lines waiting and the rest of an interruptible line
    playing, as the service and the transport do."""

    def __init__(self, *, autoplay: bool = True) -> None:
        super().__init__()
        self.autoplay = autoplay
        # Every TTSSpeakFrame's text, and whether it could be interrupted, as it arrived.
        self.spoken: list[tuple[str, bool]] = []
        # The words played, in order.
        self.played: list[str] = []
        self._building: list[Frame] = []
        self._waiting: deque[list[Frame]] = deque()
        self._playing: tuple[list[Frame], list[str], str] | None = None
        self._contexts = itertools.count(1)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LineMark) and not frame.end:
            self._building = [frame]
            return
        if isinstance(frame, TTSSpeakFrame):
            self.spoken.append((frame.text, frame.interruptible))
            self._building.append(frame)
            return
        if isinstance(frame, LineMark):
            self._building.append(frame)
            self._waiting.append(self._building)
            self._building = []
            if self.autoplay:
                await self.play_all()
            return
        if isinstance(frame, InterruptionFrame):
            self._waiting = deque(line for line in self._waiting if not line[0].interruptible)
            if self._playing is not None and self._playing[0][0].interruptible:
                self._playing = None
        await self.push_frame(frame, direction)

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    async def start(self, words: int) -> None:
        """Start the next line: its mark, its first audio and its first `words` words."""
        assert self._playing is None and self._waiting, "no line to start"
        line = self._waiting.popleft()
        start, speak = line[0], line[1]
        assert isinstance(speak, TTSSpeakFrame)
        context = f"context-{next(self._contexts)}"
        self._playing = (line, speak.text.split(), context)
        for frame in (
            start,
            TTSStartedFrame(context_id=context),
            TTSAudioRawFrame(b"\x01\x00" * 160, 16000, 1, context_id=context),
        ):
            frame.interruptible = start.interruptible
            await self.push_frame(frame)
        await self.say(words)

    async def say(self, words: int) -> None:
        """Play the next `words` words of the line playing."""
        assert self._playing is not None
        line, left, context = self._playing
        for word in left[:words]:
            frame = TTSTextFrame(word, TextType.WORD, context_id=context)
            frame.interruptible = line[0].interruptible
            self.played.append(word)
            await self.push_frame(frame)
        del left[:words]

    async def finish(self) -> None:
        """Play the rest of the line playing, its end and its end mark."""
        assert self._playing is not None
        line, left, context = self._playing
        await self.say(len(left))
        for frame in (TTSStoppedFrame(context_id=context), line[2]):
            frame.interruptible = line[0].interruptible
            await self.push_frame(frame)
        self._playing = None

    async def play_all(self) -> None:
        while self._waiting:
            await self.start(0)
            await self.finish()


class CallPipeline:
    """A call's processors between a source and a sink, run by a pipeline worker as the call
    runs them, with the caller gate first and the playout tracker last. `down` and `up` are
    the frames that reached the ends."""

    def __init__(self, processor: FrameProcessor, playback: Playback, voice: FakeVoice) -> None:
        self.down: list[Frame] = []
        self.up: list[Frame] = []
        self._down: asyncio.Queue[Frame] = asyncio.Queue()
        self._up: asyncio.Queue[Frame] = asyncio.Queue()
        source = QueuedFrameProcessor(queue=self._up, queue_direction=FrameDirection.UPSTREAM)
        sink = QueuedFrameProcessor(queue=self._down, queue_direction=FrameDirection.DOWNSTREAM)
        pipeline = Pipeline(
            [source, CallerGate(playback), processor, voice, PlayoutTracker(playback), sink]
        )
        # As a call's worker: a processor that can no longer work ends the pipeline.
        self.worker = PipelineWorker(
            pipeline,
            cancel_on_idle_timeout=False,
            enable_rtvi=False,
            idle_timeout_secs=None,
            processor_unusable_policy=ProcessorUnusablePolicy.END,
        )
        self._runner = WorkerRunner(handle_sigint=False)
        self._started = asyncio.Event()
        self.running: asyncio.Task[None] | None = None

        self.worker.add_event_handler("on_pipeline_started", self._on_started)

    async def _on_started(self, _worker: Any, _frame: Frame) -> None:
        self._started.set()

    async def __aenter__(self) -> "CallPipeline":
        await self._runner.add_workers(self.worker)
        self.running = asyncio.ensure_future(self._runner.run())
        await asyncio.wait_for(self._started.wait(), WAIT_SECONDS)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.running is not None
        if not self.running.done():
            await self.worker.queue_frame(EndFrame())
        await asyncio.wait_for(self.running, WAIT_SECONDS)
        self._collect()

    async def send(self, *frames: Frame) -> None:
        for frame in frames:
            await self.worker.queue_frame(frame)
        await settle()

    def _collect(self) -> None:
        while not self._down.empty():
            self.down.append(self._down.get_nowait())
        while not self._up.empty():
            self.up.append(self._up.get_nowait())

    @property
    def ended(self) -> bool:
        """Whether the pipeline has ended."""
        assert self.running is not None
        return self.running.done()

    def messages(self) -> list[dict[str, Any]]:
        """The page's messages, in order."""
        self._collect()
        return [f.message for f in self.down if isinstance(f, OutputTransportMessageUrgentFrame)]

    def frames[F: Frame](self, kind: type[F]) -> list[F]:
        self._collect()
        return [f for f in self.down if isinstance(f, kind)]


def agent_call(
    call_id: str,
    deps: Deps,
    model: Model,
    time: FakeTime,
    *,
    idle_seconds: float = 30.0,
    max_seconds: float = 600.0,
    turns: TurnRecorder | None = None,
) -> tuple[AgentProcessor, Playback]:
    """The voice adapter of a call whose agent is the real one, its timers on `time`; its turn
    rows go to `turns`, if given. Speech out is the fake's, which ElevenLabs never refuses:
    nothing is checked."""
    playback = Playback()
    processor = AgentProcessor(
        call_id,
        partial(start_call, call_id, deps, model, register_tools()),
        playback,
        idle_seconds=idle_seconds,
        max_seconds=max_seconds,
        vad_stop_seconds=0.5,
        speech_refused=_never_checked,
        clock=time.clock,
        sleep=time.sleep,
        turns=turns,
    )
    return processor, playback


def _never_checked() -> None:
    raise AssertionError("speech out was not refused")


def caller_says(text: str) -> list[Frame]:
    """A caller's utterance as speech-to-text and voice activity detection report it."""
    return [
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
        TranscriptionFrame(text, "", "2026-11-24T15:00:00Z", finalized=True),
    ]


def caller_talks_over(text: str = "Hold on please") -> list[Frame]:
    """A caller starting to speak over a line: the sound, then the first words speech-to-text
    hears of it, before the utterance is finished."""
    return [
        VADUserStartedSpeakingFrame(),
        InterimTranscriptionFrame(text, "", "2026-11-24T15:00:00Z"),
    ]


async def until(condition: Callable[[], bool], what: str) -> None:
    """Wait until the condition holds; fail after WAIT_SECONDS."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while not condition():
        assert loop.time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)
