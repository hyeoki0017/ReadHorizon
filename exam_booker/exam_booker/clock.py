"""접수 시작 시각 맞추기.

내 PC 시계는 수 초씩 틀릴 수 있으므로, 접수 사이트 응답의 Date 헤더로
'서버 시각 - 내 시각' 차이를 재서 보정한다(초 단위 정밀도).
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def measure_server_offset(url: str, timeout: float = 5.0) -> float | None:
    """서버 시각 - 로컬 시각(초). 실패하면 None."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 사용자가 설정한 접수 사이트
            t1 = time.time()
            header = resp.headers.get("Date")
    except Exception:  # noqa: BLE001 - 네트워크 문제면 보정 없이 진행
        return None
    return offset_from_date_header(header, (t0 + t1) / 2)


def offset_from_date_header(header: str | None, local_ts: float) -> float | None:
    if not header:
        return None
    try:
        server = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    # Date 헤더는 초 단위로 잘려 있으므로 평균 0.5초를 더해 준다.
    return server.timestamp() + 0.5 - local_ts


def seconds_until(target: datetime, offset: float = 0.0, now: Callable[[], float] = time.time) -> float:
    """서버 기준으로 target까지 남은 초(음수면 이미 지남)."""
    return target.timestamp() - (now() + offset)


def wait_until(
    target: datetime,
    offset: float = 0.0,
    on_tick: Callable[[float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> None:
    """target 시각(서버 기준)까지 기다린다. 멀리 있을 땐 길게, 가까우면 짧게 잔다."""
    last_report = None
    while True:
        remaining = seconds_until(target, offset, now)
        if remaining <= 0:
            return
        if on_tick and (last_report is None or last_report - remaining >= 60 or remaining < 10 <= last_report):
            on_tick(remaining)
            last_report = remaining
        sleep(min(max(remaining - 0.05, 0.01), 30.0 if remaining > 60 else 0.2))
