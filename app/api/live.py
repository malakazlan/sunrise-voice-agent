"""The live calls stream (GET /admin/api/calls/live/stream): server-sent events for a signed-in
browser, so the console sees calls start and end without asking again and again.

The stream opens with the calls in progress, read with the one audit event the stream writes.
Every TICK_SECONDS after that it looks at the session, without keeping it alive, and reads the
calls again in a transaction of its own, so no connection is held between ticks; a tick writes
no audit event. It sends:
    - `calls`, the list as JSON, at the opening and whenever the list differs from the last
      one sent (or a pause was sent since);
    - `paused` when a read fails, once until a read works again; the next tick tries again;
    - `signed_out` when the session has ended, and closes;
    - `renew` after STREAM_SECONDS, and closes: the page opens a new stream;
    - `replaced` when a newer stream of the same session opens, and closes: the page that
      holds the older does not reconnect, so two pages do not take the stream in turn;
    - a comment line every COMMENT_SECONDS, so that proxies keep the connection.
A process keeps at most MAX_STREAMS open, one per session: a newer stream of a session closes
the older. A write that does not complete within WRITE_SECONDS (the client stopped reading and
the send buffer is full) drops the client. Logs carry the
event names and exception class names; never a value.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable

from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
COMMENT_SECONDS = 15.0
STREAM_SECONDS = 5 * 60.0
MAX_STREAMS = 5
WRITE_SECONDS = 10.0
KEEP_ALIVE = b": keep-alive\n\n"


def _event(name: str, data: str = "{}") -> bytes:
    return f"event: {name}\ndata: {data}\n\n".encode()


class LiveStreams:
    """The streams open in this process: at most `max_streams`, one per session, each with the
    event that stops it. One process, one event loop: no lock is needed."""

    def __init__(self, max_streams: int = MAX_STREAMS) -> None:
        self._max = max_streams
        self._open: dict[bytes, asyncio.Event] = {}

    def __len__(self) -> int:
        return len(self._open)

    def open(self, key: bytes) -> asyncio.Event | None:
        """A place for a stream of the session `key`, and the event that will stop it; the
        session's older stream is stopped and gives up its place. None when there is no
        place."""
        older = self._open.pop(key, None)
        if older is not None:
            older.set()
        elif len(self._open) >= self._max:
            return None
        stop = asyncio.Event()
        self._open[key] = stop
        return stop

    def close(self, key: bytes, stop: asyncio.Event) -> None:
        """The stream that `stop` belongs to has ended; a newer stream of the session keeps its
        place."""
        if self._open.get(key) is stop:
            del self._open[key]


class LiveStream(StreamingResponse):
    """One browser's stream. `first` is the list read at the opening; `read` reads it again
    (JSON text, masked), `signed_in` looks at the session without keeping it alive, and
    `release` gives the stream's place back, whatever ended it."""

    def __init__(
        self,
        first: str,
        *,
        read: Callable[[], Awaitable[str]],
        signed_in: Callable[[], bool],
        stop: asyncio.Event,
        release: Callable[[], None],
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._read = read
        self._signed_in = signed_in
        self._stop = stop
        self._release = release
        self._clock = clock
        self._sleep = sleep
        self._events = self._run(first)
        # The type exactly as EventSource checks it, without the charset Starlette would add.
        super().__init__(
            self._events,
            headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._events.aclose()
            self._release()

    async def stream_response(self, send: Send) -> None:
        async def bounded(message: Message) -> None:
            async with asyncio.timeout(WRITE_SECONDS):
                await send(message)

        try:
            await super().stream_response(bounded)
        except TimeoutError:
            logger.warning("admin_stream_dropped")

    async def _pause(self, seconds: float) -> bool:
        """Wait the seconds out, or until the stream is stopped; whether it was."""
        if self._stop.is_set():
            return True
        sleeping = asyncio.ensure_future(self._sleep(max(0.0, seconds)))
        stopped = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sleeping, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            stopped.cancel()
        return self._stop.is_set()

    async def _run(self, first: str) -> AsyncGenerator[bytes]:
        yield _event("calls", first)
        # The list last sent, or None after a pause.
        last: str | None = first
        now = self._clock()
        end = now + STREAM_SECONDS
        next_tick = now + TICK_SECONDS
        next_comment = now + COMMENT_SECONDS
        while True:
            if await self._pause(min(next_tick, next_comment, end) - self._clock()):
                logger.info("admin_stream_ended reason=replaced")
                yield _event("replaced")
                return
            now = self._clock()
            if now >= end:
                logger.info("admin_stream_ended reason=renew")
                yield _event("renew")
                return
            if now >= next_comment:
                next_comment = now + COMMENT_SECONDS
                yield KEEP_ALIVE
            if now < next_tick:
                continue
            next_tick = now + TICK_SECONDS
            if not self._signed_in():
                logger.info("admin_stream_ended reason=signed_out")
                yield _event("signed_out")
                return
            shown: str | None
            try:
                shown = await self._read()
            except Exception as error:
                logger.warning("admin_stream_paused error=%s", type(error).__name__)
                shown = None
            if shown == last:
                continue
            last = shown
            yield _event("paused") if shown is None else _event("calls", shown)
