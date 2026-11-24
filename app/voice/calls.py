"""Concurrent-call cap and call lifetime: every call gives its slot back however it ends."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger(__name__)

# How long ending a call may take before its slot is released regardless.
STOP_TIMEOUT_SECONDS = 10.0


class Call(Protocol):
    def answer(self) -> dict[str, str]: ...

    async def run(self) -> None:
        """Run until the caller leaves."""

    async def stop(self) -> None:
        """End the call and release its connection. Safe to call more than once."""


class CallRegistry:
    def __init__(
        self,
        *,
        max_calls: int,
        max_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        self._max_calls = max_calls
        self._max_seconds = max_seconds
        self._sleep = sleep
        self._stop_timeout = stop_timeout
        self._slots: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Calls still being ended; held so that the tasks are not collected mid-run.
        self._ending: set[asyncio.Task[None]] = set()

    @property
    def active(self) -> int:
        return len(self._slots)

    def reserve(self) -> str | None:
        """Take a slot for a new call, or return None when the cap is reached."""
        if len(self._slots) >= self._max_calls:
            return None
        call_id = uuid.uuid4().hex
        self._slots.add(call_id)
        return call_id

    def release(self, call_id: str) -> None:
        self._slots.discard(call_id)

    def start(self, call_id: str, call: Call) -> "asyncio.Task[None]":
        # Started eagerly, so supervision is already inside its try block when start() returns:
        # a task cancelled before its first step would skip the stop and the release.
        task = asyncio.Task(
            self._supervise(call_id, call), loop=asyncio.get_running_loop(), eager_start=True
        )
        self._tasks[call_id] = task
        return task

    async def _supervise(self, call_id: str, call: Call) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        ended_by = "shutdown"
        running = asyncio.ensure_future(call.run())
        limit = asyncio.ensure_future(self._sleep(self._max_seconds))
        logger.info("call_started call_id=%s active=%d", call_id, self.active)
        try:
            done, _pending = await asyncio.wait(
                {running, limit}, return_when=asyncio.FIRST_COMPLETED
            )
            if running not in done:
                ended_by = "max_duration"
            elif running.exception() is not None:
                ended_by = "error"
            else:
                ended_by = "finished"
        finally:
            limit.cancel()
            ending = asyncio.ensure_future(self._end(call, running))
            self._ending.add(ending)
            ending.add_done_callback(self._ending.discard)
            try:
                done, _pending = await asyncio.wait({ending}, timeout=self._stop_timeout)
                if not done:
                    ending.cancel()
                    logger.warning("call_stop_timed_out call_id=%s", call_id)
                    if not running.done():
                        running.cancel()
                        logger.warning("call_run_cancelled call_id=%s", call_id)
            finally:
                self.release(call_id)
                self._tasks.pop(call_id, None)
                logger.info(
                    "call_ended call_id=%s reason=%s seconds=%d active=%d",
                    call_id,
                    ended_by,
                    loop.time() - started,
                    self.active,
                )

    @staticmethod
    async def _end(call: Call, running: "asyncio.Future[None]") -> None:
        await call.stop()
        await asyncio.gather(running, return_exceptions=True)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
