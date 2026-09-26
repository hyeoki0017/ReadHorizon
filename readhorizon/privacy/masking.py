"""민감정보 자동 마스킹.

저장하거나 외부(LLM API)로 보내기 전에 모든 사용자 입력에 적용한다.
원래 값은 어디에도 남기지 않고, 탐지된 '항목과 개수'만 기록한다(수집 항목 고지용).

오탐을 줄이기 위해 숫자 경계를 확인한다. ISBN·연도·날짜는 가리지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (코드, 화면 표시 이름, 정규식) — 앞의 패턴이 먼저 적용된다.
PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "resident_id",
        "주민·외국인등록번호",
        # 하이픈 없는 13자리도 가린다. ISBN-13(978/979…)은 월 자리가 88·89·98·99라 걸리지 않는다.
        re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-?\s?[1-8]\d{6}(?!\d)"),
    ),
    ("card", "카드번호", re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)")),
    ("mobile", "휴대전화번호", re.compile(r"(?<!\d)01[016789][-. ]?\d{3,4}[-. ]?\d{4}(?!\d)")),
    (
        "phone",
        "전화번호",
        re.compile(r"(?<!\d)0(?:2|[3-6][1-5]|70)[-.) ]\s?\d{3,4}[-. ]\d{4}(?!\d)"),
    ),
    ("email", "이메일", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
]

LABELS = {code: label for code, label, _ in PATTERNS}


@dataclass
class MaskResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def mask(text: str | None) -> MaskResult:
    if not text:
        return MaskResult(text or "")
    counts: dict[str, int] = {}
    for code, label, pattern in PATTERNS:
        text, n = pattern.subn(f"[마스킹:{label}]", text)
        if n:
            counts[code] = counts.get(code, 0) + n
    return MaskResult(text, counts)


def merge_counts(*results: MaskResult) -> dict[str, int]:
    merged: dict[str, int] = {}
    for result in results:
        for code, n in result.counts.items():
            merged[code] = merged.get(code, 0) + n
    return merged
