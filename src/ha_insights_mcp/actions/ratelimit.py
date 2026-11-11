"""Sliding-window rate limit for executed actions: stops runaway agent loops."""

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self, limit: int, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: deque[float] = deque()

    def _expire(self, now: float) -> None:
        while self._events and now - self._events[0] >= self._window:
            self._events.popleft()

    def try_acquire(self) -> bool:
        now = self._clock()
        self._expire(now)
        if len(self._events) >= self._limit:
            return False
        self._events.append(now)
        return True

    def retry_after(self) -> float:
        """Seconds until the next slot frees up (0 if one is free now)."""
        now = self._clock()
        self._expire(now)
        if len(self._events) < self._limit:
            return 0.0
        return self._window - (now - self._events[0])
