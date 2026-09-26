from __future__ import annotations

from datetime import datetime, timedelta, timezone

from readhorizon.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.now = datetime(2026, 9, 26, 23, 59, 0, tzinfo=timezone.utc)

    def mono(self) -> float:
        return self.t

    def utc(self) -> datetime:
        return self.now


def test_per_minute_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(2, clock=clock.mono, utcnow=clock.utc)
    assert limiter.check("a") is None
    assert limiter.check("a") is None
    wait = limiter.check("a")
    assert wait is not None and 1 <= wait <= 60
    assert limiter.check("b") is None  # 다른 클라이언트는 따로 센다
    clock.t += 61
    assert limiter.check("a") is None


def test_daily_limit_resets_at_utc_midnight():
    clock = FakeClock()
    limiter = RateLimiter(0, daily=1, clock=clock.mono, utcnow=clock.utc)
    assert limiter.check("a") is None
    wait = limiter.check("b")
    assert wait == 60  # 23:59:00 → 자정까지 60초
    clock.now += timedelta(minutes=2)
    assert limiter.check("b") is None


def test_disabled_limits_allow_everything():
    limiter = RateLimiter(0, daily=0)
    assert all(limiter.check("a") is None for _ in range(100))


def test_rejected_calls_do_not_consume_daily_quota():
    clock = FakeClock()
    limiter = RateLimiter(1, daily=2, clock=clock.mono, utcnow=clock.utc)
    assert limiter.check("a") is None
    assert limiter.check("a") is not None  # 분당 제한에 걸림 — 일일 한도는 쓰지 않는다
    assert limiter.check("b") is None
    assert limiter.check("c") is not None  # 일일 한도 2회 소진


def test_old_clients_are_pruned():
    clock = FakeClock()
    limiter = RateLimiter(1, clock=clock.mono, utcnow=clock.utc)
    for i in range(10_001):
        limiter.check(f"ip{i}")
    clock.t += 61
    limiter.check("new")
    assert len(limiter._hits) <= 2
