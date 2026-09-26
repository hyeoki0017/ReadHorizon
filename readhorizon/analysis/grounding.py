"""근거 검증.

1) 인용 실재 확인: 모델이 돌려준 인용문이 가리키는 위치의 원문에 실제로 있는지 대조한다.
2) 근거율: 분석 본문을 문장(목록 항목) 단위로 나눠, 검증된 인용이 붙은 문장의 글자 비율을 구한다.
   작품 원문과 A·B 등급 참고자료 인용만 근거로 센다(C 등급·사용자 제공 소개는 제외).
   헤딩과 '>' 안내문은 계산에서 뺀다.
"""

from __future__ import annotations

import re

_SPACE_RE = re.compile(r"\s+")
_LABEL_RE = re.compile(r"^\[[^\]]{1,20}\]\s*")
_SENTENCE_END_RE = re.compile(r"[.!?。…](?=\s)")
GROUNDING_TIERS = {None, "A", "B"}  # None = 작품 원문


def squash(text: str) -> str:
    return _SPACE_RE.sub("", text or "")


def strip_label(text: str) -> str:
    return _LABEL_RE.sub("", text or "", count=1)


def is_supported(cited_text: str, source_text: str) -> bool:
    cited = squash(cited_text)
    return bool(cited) and cited in squash(source_text)


def counts_as_grounding(citation: dict) -> bool:
    return citation["verified"] and citation["source"] != "intro" and citation.get("tier") in GROUNDING_TIERS


def _units(text: str) -> list[tuple[int, int]]:
    """본문을 문장 단위 (시작, 끝) 구간으로 나눈다. 헤딩·안내문 줄은 제외."""
    units: list[tuple[int, int]] = []
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ">")):
            start = 0
            for match in _SENTENCE_END_RE.finditer(line):
                units.append((offset + start, offset + match.end()))
                start = match.end()
            if line[start:].strip():
                units.append((offset + start, offset + len(line)))
        offset += len(line) + 1
    return units


def summarize(segments: list[dict]) -> dict:
    full = ""
    grounded_ranges: list[tuple[int, int]] = []
    citations = verified = 0
    for segment in segments:
        start = len(full)
        full += segment["text"]
        cits = segment.get("citations") or []
        citations += len(cits)
        verified += sum(1 for c in cits if c["verified"])
        if any(counts_as_grounding(c) for c in cits):
            grounded_ranges.append((start, len(full)))

    total = grounded = 0
    for start, end in _units(full):
        chars = len(squash(re.sub(r"^\s*(?:[-*]\s*)+", "", full[start:end])))
        if not chars:
            continue
        total += chars
        if any(g_start < end and start < g_end for g_start, g_end in grounded_ranges):
            grounded += chars

    ratio = (grounded / total) if total else 0.0
    return {
        "total_chars": total,
        "grounded_chars": grounded,
        "ratio": round(ratio, 3),
        "citation_count": citations,
        "verified_count": verified,
        "unverified_count": citations - verified,
    }
