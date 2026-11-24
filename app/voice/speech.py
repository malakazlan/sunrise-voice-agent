"""Speech out: ElevenLabs' text-to-speech, and what the caller has heard of each line.

The agent's lines (app/agent/turn.py) reach text-to-speech one at a time, each between two
`LineMark` frames. Pipecat's text-to-speech service keeps frames it does not speak in order with
its audio, and the transport's output sends them on only once the audio before them has been
written to the caller, so the marks come out of the transport when the line starts playing and
when it has ended: in full, or with no audio at all when the service could not speak it (it
gives up on a line that brings no audio after a few seconds). `PlayoutTracker`, after the
transport's output, reads them, and the words the service reports playing, into `Playback`.

`Playback` holds the lines released and not yet over, the words heard of the one playing, and
the lines waiting to be sent. The call starts muted: until its opening line has been released and
has ended, and while any line that may not be interrupted (the greeting, the closed line, the
emergency line) is released and not over, `CallerGate`, first after the transport's input, gives
voice activity detection and speech-to-text silence in place of the caller's audio. An
interruption ends every interruptible line released: the one playing with the words heard so
far, the others with none.

Logs carry event names, the call id, line ids and counts; never text.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pipecat.frames.frames import (
    DataFrame,
    Frame,
    InputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language

from app.agent.turn import Line
from app.config import Settings

# Called when a line's first audio has played, with its id.
Started = Callable[[int], Awaitable[None]]
# Called when a line is over, with its id and the words heard: None for all of it.
Over = Callable[[int, int | None], Awaitable[None]]


def elevenlabs_tts(settings: Settings, **options: Any) -> ElevenLabsTTSService:
    """ElevenLabs' streaming text-to-speech over its multi-stream WebSocket, with the voice and
    the model of the settings, in English. Whole lines are sent, so ElevenLabs' auto mode is on
    (Pipecat's default for sentence-sized input). Voice settings are ElevenLabs' defaults. The
    service does not push the text it is given as text: it reports the words as their audio
    plays. `options` go to the service (tests point it at a socket of their own)."""
    if settings.elevenlabs_api_key is None or settings.elevenlabs_voice_id is None:
        raise ValueError("speech out needs the ElevenLabs key and voice")
    return ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key.get_secret_value(),
        settings=ElevenLabsTTSService.Settings(
            voice=settings.elevenlabs_voice_id,
            model=settings.tts_model,
            language=Language.EN,
        ),
        **options,
    )


@dataclass
class LineMark(DataFrame):
    """Where a line starts (`end` False) or ends in what is sent to the caller."""

    line_id: int
    end: bool


class Playback:
    """The lines released and not yet over, in order, and what has been heard of them."""

    def __init__(self) -> None:
        self._released: dict[int, Line] = {}
        self._to_send: deque[Line] = deque()
        self._sendable = asyncio.Event()
        self._over_event = asyncio.Event()
        self._opened = False
        self._playing: int | None = None
        self._sounded = False
        self._words = 0
        self._contexts: dict[str, int] = {}
        self._on_started: Started = _ignore_started
        self._on_over: Over = _ignore_over

    def listen(self, *, started: Started, over: Over) -> None:
        self._on_started = started
        self._on_over = over

    @property
    def muted(self) -> bool:
        """Whether the caller is not to be heard: before the opening line has been released,
        and while a line that may not be interrupted is released and not over."""
        return not self._opened or any(not line.interruptible for line in self._released.values())

    @property
    def quiet(self) -> bool:
        """Whether the opening line has been released and every line is over."""
        return self._opened and not self._released

    @property
    def interruptible(self) -> bool:
        """Whether a line the caller may interrupt is released and not over."""
        return any(line.interruptible for line in self._released.values())

    def release(self, line: Line, *, opening: bool = False) -> None:
        """A line to be spoken, after those released before it."""
        self._opened = self._opened or opening
        self._released[line.id] = line
        self._to_send.append(line)
        self._sendable.set()

    def released(self, line_id: int) -> bool:
        """Whether the line is released and not over."""
        return line_id in self._released

    async def next_to_send(self) -> Line:
        while not self._to_send:
            self._sendable.clear()
            await self._sendable.wait()
        return self._to_send.popleft()

    async def all_over(self) -> None:
        """Wait until every line released is over."""
        while self._released:
            self._over_event.clear()
            await self._over_event.wait()

    async def interrupt(self) -> list[tuple[int, int]]:
        """End every interruptible line released: the one playing with the words heard so far,
        the others with none: those not sent in full are not sent on. The line ids and words,
        in order."""
        cut = [
            (line.id, self._words if line.id == self._playing else 0)
            for line in self._released.values()
            if line.interruptible
        ]
        for line_id, words in cut:
            await self._over(line_id, words)
        return cut

    async def _over(self, line_id: int, words: int | None) -> None:
        del self._released[line_id]
        if line_id == self._playing:
            self._playing = None
        self._contexts = {c: line for c, line in self._contexts.items() if line != line_id}
        self._over_event.set()
        await self._on_over(line_id, words)

    # What the transport has played, from PlayoutTracker.

    def starting(self, line_id: int) -> None:
        if line_id in self._released:
            self._playing = line_id
            self._sounded = False
            self._words = 0

    def context(self, context_id: str) -> None:
        if self._playing is not None:
            self._contexts[context_id] = self._playing

    async def sounded(self) -> None:
        if self._playing is not None and not self._sounded:
            self._sounded = True
            await self._on_started(self._playing)

    def word(self, context_id: str) -> None:
        line_id = self._contexts.get(context_id)
        if line_id is not None and line_id == self._playing:
            self._words += 1

    async def ended(self, line_id: int) -> None:
        if line_id in self._released:
            await self._over(line_id, None)


async def _ignore_started(_line_id: int) -> None:
    return None


async def _ignore_over(_line_id: int, _words: int | None) -> None:
    return None


class CallerGate(FrameProcessor):
    """First after the transport's input: while the caller is muted, their audio goes on as
    silence of the same length, so nothing they say reaches voice activity detection or
    speech-to-text."""

    def __init__(self, playback: Playback) -> None:
        super().__init__()
        self._playback = playback

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and self._playback.muted:
            silence = InputAudioRawFrame(
                audio=bytes(len(frame.audio)),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )
            silence.transport_source = frame.transport_source
            frame = silence
        await self.push_frame(frame, direction)


class PlayoutTracker(FrameProcessor):
    """Last, after the transport's output: the line marks, the start of each line's audio and
    the words, as the transport plays them, into the call's Playback."""

    def __init__(self, playback: Playback) -> None:
        super().__init__()
        self._playback = playback

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, LineMark):
                if frame.end:
                    await self._playback.ended(frame.line_id)
                else:
                    self._playback.starting(frame.line_id)
                return
            if isinstance(frame, TTSStartedFrame) and frame.context_id is not None:
                self._playback.context(frame.context_id)
            elif isinstance(frame, TTSAudioRawFrame):
                await self._playback.sounded()
            elif isinstance(frame, TTSTextFrame) and frame.context_id is not None:
                self._playback.word(frame.context_id)
        await self.push_frame(frame, direction)
