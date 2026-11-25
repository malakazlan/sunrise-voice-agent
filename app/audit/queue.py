"""Audit events written in the background, for a caller that must not wait for the write: a call
refused at `/offer` while calls are off (app/api/offer.py).

The same pattern as the turn recorder (app/agent/turn_record.py). `put` is a plain function: it
stamps the event with the clock, puts it on a bounded in-memory queue (capacity) and returns;
it never waits and never raises. When the queue is full the event is dropped and counted, and
the first event dropped while it stays full is logged, once `put` has returned. One background
task, started by the first event that finds none running and ending once the queue is empty,
appends the events in batches of at most BATCH_EVENTS, each batch in one transaction. A batch's
write is given up after `write_seconds`, its clean-up included; a batch that fails or is given
up is logged and its events counted as dropped. `drain` waits for the writer, for tests and for
shutdown; given a limit, it then stops the writer and drops and counts what is left.

Logs carry event names, actions, counts and exception class names.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.log import AuditLog, Event

logger = logging.getLogger(__name__)

# The events held for the writer at most; past it an event is dropped.
QUEUE_EVENTS = 100
# The events one transaction appends at most.
BATCH_EVENTS = 20
WRITE_SECONDS = 2.0
CANCEL_INTERVAL_SECONDS = 0.1


class AuditQueue:
    """The queue of events and its writer."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        audit: AuditLog,
        now: Callable[[], datetime],
        *,
        capacity: int = QUEUE_EVENTS,
        write_seconds: float = WRITE_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._audit = audit
        self._now = now
        self._write_seconds = write_seconds
        self._queue: asyncio.Queue[tuple[Event, datetime]] = asyncio.Queue(maxsize=capacity)
        self._writer: asyncio.Task[None] | None = None
        self._dropped = 0
        # Whether the last event handed over found the queue full.
        self._full = False

    @property
    def dropped(self) -> int:
        """The events dropped: the queue was full, or their write failed."""
        return self._dropped

    def put(self, event: Event) -> None:
        """Hand an event over, to be appended later. Never waits and never raises."""
        try:
            loop = asyncio.get_running_loop()
            self._queue.put_nowait((event, self._now()))
            if self._writer is None or self._writer.done():
                self._writer = loop.create_task(self._write_all())
        except asyncio.QueueFull:
            self._dropped += 1
            if not self._full:
                self._full = True
                loop.call_soon(
                    partial(
                        logger.warning,
                        "audit_event_dropped action=%s dropped=%d",
                        event.action.value,
                        self._dropped,
                    )
                )
            return
        except Exception as error:
            self._dropped += 1
            logger.error("audit_event_not_queued error=%s", type(error).__name__)
            return
        self._full = False

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until every event queued has been appended or dropped; with a `timeout`, at
        most that many seconds: then the writer is stopped, and what is left is dropped and
        counted."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self._writer is not None and not self._writer.done():
            left = None if deadline is None else max(0.0, deadline - loop.time())
            await asyncio.wait({self._writer}, timeout=left)
            if deadline is not None and loop.time() >= deadline:
                while not self._writer.done():
                    self._writer.cancel()
                    await asyncio.wait({self._writer}, timeout=CANCEL_INTERVAL_SECONDS)

    async def _write_all(self) -> None:
        batch: list[tuple[Event, datetime]] = []
        try:
            while not self._queue.empty():
                size = min(BATCH_EVENTS, self._queue.qsize())
                batch = [self._queue.get_nowait() for _ in range(size)]
                await self._write_bounded(batch)
                batch = []
        except asyncio.CancelledError:
            left = len(batch) + self._queue.qsize()
            while not self._queue.empty():
                self._queue.get_nowait()
            self._dropped += left
            logger.error("audit_events_dropped_at_shutdown events=%d", left)
            raise

    async def _write_bounded(self, batch: list[tuple[Event, datetime]]) -> None:
        write = asyncio.ensure_future(self._append(batch))
        try:
            await asyncio.wait({write}, timeout=self._write_seconds)
        finally:
            given_up = not write.done()
            while not write.done():
                write.cancel()
                await asyncio.wait({write}, timeout=CANCEL_INTERVAL_SECONDS)
        error: BaseException | None = TimeoutError() if given_up else None
        if error is None:
            error = asyncio.CancelledError() if write.cancelled() else write.exception()
        if error is not None:
            self._dropped += len(batch)
            logger.error(
                "audit_events_write_failed events=%d error=%s", len(batch), type(error).__name__
            )

    async def _append(self, batch: list[tuple[Event, datetime]]) -> None:
        async with self._sessions() as session:
            for event, at in batch:
                await self._audit.append(session, event, now=at)
            await session.commit()
