"""시험 일정 행(텍스트)과 희망 조건을 맞춰 보는 순수 로직 (브라우저 없이 테스트 가능)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .config import Preference

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """공백 차이와 날짜 구분자(., /, -) 차이를 없앤다: '2026. 11. 21' == '2026-11-21'."""
    t = _WS.sub("", text or "")
    t = re.sub(r"(\d)[./년-](?=\d)", r"\1-", t)
    t = re.sub(r"(\d)[월](?=\d)", r"\1-", t)
    return t.replace("일", "").lower()


def row_matches(row_text: str, pref: Preference) -> bool:
    norm = normalize(row_text)
    return all(normalize(k) in norm for k in pref.keywords())


def is_full(row_text: str, full_keywords: Sequence[str]) -> bool:
    if any(normalize(k) in normalize(row_text) for k in full_keywords if k):
        return True
    # '잔여 0', '0석', '0/30' 처럼 남은 자리가 0으로 표시된 경우 (날짜 변환 전 원문 기준)
    compact = _WS.sub("", row_text or "")
    return bool(re.search(r"잔여:?0(?!\d)|(?<!\d)0석|(?<![\d.])0/\d+", compact))


def pick_slot(
    rows: Sequence[str],
    preferences: Sequence[Preference],
    full_keywords: Sequence[str],
) -> tuple[int, Preference] | None:
    """우선순위가 가장 높은 희망 조건에 맞고 자리가 남은 첫 행 (행 번호, 조건)."""
    for pref in preferences:
        for i, text in enumerate(rows):
            if row_matches(text, pref) and not is_full(text, full_keywords):
                return i, pref
    return None
