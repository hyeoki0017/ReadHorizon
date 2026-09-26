"""호출 제한(비용·서비스 보호).

- 클라이언트(접속 IP)별 분당 호출 수와, 프로세스 전체의 하루(UTC) 호출 수를 제한한다.
- IP는 메모리에서 최근 1분 동안만 쓰고 저장하지 않는다.
- 워커 프로세스마다 따로 센다. 여러 워커·여러 서버라면 리버스 프록시나 게이트웨이에서도 제한을 두는 것을 권장한다.
- 한도를 0으로 두면 해당 제한을 끈다.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

WINDOW_SECONDS = 60.0
MAX_TRACKED_CLIENTS = 10_000


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        daily: int = 0,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.per_minute = max(0, per_minute)
        self.daily = max(0, daily)
        self._clock = clock
        self._utcnow = utcnow
        self._hits: dict[str, deque[float]] = {}
        self._day = None
        self._day_count = 0
        self._lock = threading.Lock()

    def check(self, key: str) -> int | None:
        """허용하면 None, 제한하면 다시 시도할 수 있을 때까지의 초(올림)를 돌려준다."""
        if not self.per_minute and not self.daily:
            return None
        now = self._clock()
        with self._lock:
            if self.daily:
                today = self._utcnow().date()
                if today != self._day:
                    self._day, self._day_count = today, 0
                if self._day_count >= self.daily:
                    midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc)
                    return max(1, math.ceil((midnight - self._utcnow()).total_seconds()))
            if self.per_minute:
                if len(self._hits) > MAX_TRACKED_CLIENTS:
                    self._prune(now)
                window = self._hits.setdefault(key, deque())
                while window and now - window[0] >= WINDOW_SECONDS:
                    window.popleft()
                if len(window) >= self.per_minute:
                    return max(1, math.ceil(WINDOW_SECONDS - (now - window[0])))
                window.append(now)
            if self.daily:
                self._day_count += 1
        return None

    def _prune(self, now: float) -> None:
        stale = [k for k, w in self._hits.items() if not w or now - w[-1] >= WINDOW_SECONDS]
        for key in stale:
            del self._hits[key]
