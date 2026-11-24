"""Counts failures per client address over a sliding window, in memory.

One process, one event loop: the methods run without awaiting, so no lock is needed. State is
lost on restart. At most `max_addresses` addresses are kept; when a new one comes and there is
no room, the address whose last failure is oldest is forgotten.
"""

import time
from collections import OrderedDict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_addresses: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_addresses < 1:
            raise ValueError("limit, window and address cap must be positive")
        self._limit = limit
        self._window = window_seconds
        self._max_addresses = max_addresses
        self._clock = clock
        # Address -> times of its failures inside the window, oldest first. The address that
        # failed longest ago comes first.
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def tracked(self) -> int:
        return len(self._failures)

    def _recent(self, address: str) -> deque[float] | None:
        times = self._failures.get(address)
        if times is None:
            return None
        start = self._clock() - self._window
        while times and times[0] <= start:
            times.popleft()
        if not times:
            del self._failures[address]
            return None
        return times

    def blocked(self, address: str) -> bool:
        """Whether the address has reached the limit inside the window."""
        times = self._recent(address)
        return times is not None and len(times) >= self._limit

    def record_failure(self, address: str) -> int:
        """Count a failure; returns the address's failures inside the window."""
        times = self._recent(address)
        if times is None:
            if len(self._failures) >= self._max_addresses:
                self._failures.popitem(last=False)
            times = self._failures[address] = deque(maxlen=self._limit)
        else:
            self._failures.move_to_end(address)
        times.append(self._clock())
        return len(times)
