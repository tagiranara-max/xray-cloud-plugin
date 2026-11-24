from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque


class RateLimiter:
    """Sliding-window limiter: at most max_requests per window_s seconds."""

    def __init__(self, max_requests: int = 300, window_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._stamps: Deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= self.window_s:
                self._stamps.popleft()
            if len(self._stamps) < self.max_requests:
                self._stamps.append(now)
                return
            self._sleep(self.window_s - (now - self._stamps[0]) + 0.001)
