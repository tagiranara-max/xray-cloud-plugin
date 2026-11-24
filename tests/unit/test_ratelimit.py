from __future__ import annotations

from xray_cloud.ratelimit import RateLimiter


def test_ratelimiter_waits_when_window_full():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    rl = RateLimiter(max_requests=2, window_s=10.0, clock=lambda: now[0], sleep=sleep)
    rl.acquire()
    rl.acquire()
    assert slept == []
    rl.acquire()
    assert len(slept) == 1 and 10.0 <= slept[0] < 10.1


def test_ratelimiter_allows_after_window():
    now = [0.0]
    rl = RateLimiter(max_requests=1, window_s=5.0, clock=lambda: now[0],
                     sleep=lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    rl.acquire()
    now[0] = 5.0
    rl.acquire()
