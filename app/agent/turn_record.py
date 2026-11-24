"""Each caller turn's row of call_turns (app/db/models.py), recorded without ever holding up a
call.

The voice adapter (app/voice/pipeline.py) hands each turn's row to `TurnRecorder.put` once it
has the turn's timings: the ones it already sends the page and logs as `turn_measured`, and the
first audio of the reply when it comes. `put` is a plain function: it stamps the row with the
call's clock, puts it on a bounded in-memory queue (QUEUE_ROWS) and returns; it never waits and
never raises. When the queue is full the row is dropped and counted, and the first row dropped
while it stays full is logged, once `put` has returned. One background task, started
by the first row that finds none running and ending once the queue is empty, writes the rows in
batches of at most BATCH_ROWS, one insert each. A batch's write is given up after
`write_seconds`, its clean-up included, as the call events' writes are (app/agent/endings.py); a
batch that fails or is given up is logged and its rows are counted as dropped. Each row carries
what its hand-over cost the call (`record_us`) and how many rows the recorder had dropped before
it (`dropped_before`), so that both can be read from the table. `drain` waits for the writer, for
tests and for shutdown; given a limit, it then stops the writer and drops and counts what is
left.

Logs carry event names, call ids, turn numbers, counts and exception class names; never text.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.state import Phase
from app.db.models import CallTurn
from app.guard.input import GuardAction

logger = logging.getLogger(__name__)

# The rows held for the writer at most; past it a row is dropped.
QUEUE_ROWS = 1000
# The rows one insert writes at most.
BATCH_ROWS = 100
# A batch's write is given up after this many seconds: it is cancelled, then cancelled again
# every CANCEL_INTERVAL_SECONDS until it has ended (the driver, interrupted a second time while it
# waits on the server, closes the connection).
WRITE_SECONDS = 2.0
CANCEL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnRow:
    """What a turn's row records: ids, registry members and milliseconds. The caller is verified
    exactly when there is a patient id."""

    call_id: str
    turn: int
    phase: Phase
    patient_id: str | None
    guard_action: GuardAction
    last_tool: str | None
    tool_rounds: int
    vad_ms: int | None
    stt_ms: int | None
    first_token_ms: int | None
    last_token_ms: int | None
    first_audio_ms: int | None
    total_ms: int


@dataclass(slots=True)
class _Queued:
    row: TurnRow
    at: datetime
    dropped_before: int
    # Set by `put` once the row is on the queue. The writer reads it only after an await, so
    # never before it is set.
    record_us: int = 0


class TurnRecorder:
    """The queue of turn rows and its writer, shared by every call of the process."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        now: Callable[[], datetime],
        *,
        capacity: int = QUEUE_ROWS,
        write_seconds: float = WRITE_SECONDS,
    ) -> None:
        self._sessions = sessions
        self._now = now
        self._write_seconds = write_seconds
        self._queue: asyncio.Queue[_Queued] = asyncio.Queue(maxsize=capacity)
        self._writer: asyncio.Task[None] | None = None
        self._dropped = 0
        # Whether the last row handed over found the queue full.
        self._full = False

    @property
    def dropped(self) -> int:
        """The rows dropped since the recorder was made: the queue was full, or their write
        failed."""
        return self._dropped

    def put(self, row: TurnRow) -> None:
        """Hand a turn's row over, to be written later. Never waits and never raises: a row that
        cannot be queued is dropped and counted."""
        started = time.perf_counter_ns()
        try:
            loop = asyncio.get_running_loop()
            queued = _Queued(row=row, at=self._now(), dropped_before=self._dropped)
            self._queue.put_nowait(queued)
            if self._writer is None or self._writer.done():
                self._writer = loop.create_task(self._write_all())
        except asyncio.QueueFull:
            self._dropped += 1
            # Once while the queue stays full, and after the hand-over: a log line costs the
            # event loop about as much as the hand-over's whole budget.
            if not self._full:
                self._full = True
                loop.call_soon(
                    partial(
                        logger.warning,
                        "call_turn_dropped call_id=%s turn=%d dropped=%d",
                        row.call_id,
                        row.turn,
                        self._dropped,
                    )
                )
            return
        except Exception as error:
            self._dropped += 1
            logger.error("call_turn_not_queued error=%s", type(error).__name__)
            return
        self._full = False
        queued.record_us = (time.perf_counter_ns() - started) // 1000

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until every row queued has been written or dropped; with a `timeout`, at most
        that many seconds: then the writer is stopped, and the rows still queued or being
        written are dropped and counted."""
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
        batch: list[_Queued] = []
        try:
            while not self._queue.empty():
                size = min(BATCH_ROWS, self._queue.qsize())
                batch = [self._queue.get_nowait() for _ in range(size)]
                await self._write_bounded(batch)
                batch = []
        except asyncio.CancelledError:
            left = len(batch) + self._queue.qsize()
            while not self._queue.empty():
                self._queue.get_nowait()
            self._dropped += left
            logger.error("call_turns_dropped_at_shutdown rows=%d", left)
            raise

    async def _write_bounded(self, batch: list[_Queued]) -> None:
        """Write the batch, giving it up after `write_seconds`; a failure is logged and its rows
        counted as dropped."""
        write = asyncio.ensure_future(self._insert(batch))
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
                "call_turns_write_failed rows=%d error=%s", len(batch), type(error).__name__
            )
            return
        logger.debug("call_turns_written rows=%d", len(batch))

    async def _insert(self, batch: list[_Queued]) -> None:
        values = [
            {
                "call_id": queued.row.call_id,
                "turn": queued.row.turn,
                "at": queued.at,
                "phase": queued.row.phase.value,
                "verified": queued.row.patient_id is not None,
                "patient_id": queued.row.patient_id,
                "guard_action": queued.row.guard_action.value,
                "last_tool": queued.row.last_tool,
                "tool_rounds": queued.row.tool_rounds,
                "vad_ms": queued.row.vad_ms,
                "stt_ms": queued.row.stt_ms,
                "first_token_ms": queued.row.first_token_ms,
                "last_token_ms": queued.row.last_token_ms,
                "first_audio_ms": queued.row.first_audio_ms,
                "total_ms": queued.row.total_ms,
                "record_us": queued.record_us,
                "dropped_before": queued.dropped_before,
            }
            for queued in batch
        ]
        async with self._sessions() as session:
            await session.execute(
                insert(CallTurn)
                .values(values)
                .on_conflict_do_nothing(index_elements=["call_id", "turn"])
            )
            await session.commit()
