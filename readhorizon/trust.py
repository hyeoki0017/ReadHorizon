"""신뢰도 체계: 출처 등급, 근거 배지.

- 원문: 분석 대상 작품 자체(1차 근거)
- A: 공식·학술 (평가원 해설, 교과서·지도서, 동료 심사 논문, 공인 주석본)
- B: 공신력 있는 사전·백과·평론
- C: 참고(검증 약함) — 블로그, 사용자 제공 책 소개 등. 해석의 근거로 쓰지 않는다.
"""

from __future__ import annotations

TIERS = {
    "A": "공식·학술",
    "B": "사전·평론",
    "C": "참고(검증 약함)",
}

INTRO_TIER = "C"
INTRO_LABEL = "사용자 제공 소개"


def tier_label(tier: str) -> str:
    return TIERS.get(tier, "미분류")


def analysis_badge(ratio: float, intro_only: bool, excerpted: bool = False) -> dict:
    """분석 결과 전체에 붙이는 신뢰도 배지."""
    if intro_only:
        badge = {
            "level": "intro",
            "label": "소개글 기반 · 원문 없음",
            "description": "원문 없이 사용자가 붙여넣은 소개글만으로 만든 읽기 가이드입니다. 작품 해석으로 쓰지 마세요.",
        }
    elif ratio >= 0.6:
        badge = {
            "level": "high",
            "label": "원문 근거 충분",
            "description": "분석 문장 대부분이 원문 인용으로 뒷받침됩니다.",
        }
    elif ratio >= 0.3:
        badge = {
            "level": "medium",
            "label": "일부 근거",
            "description": "인용 표시가 없는 문장은 AI 해석이므로 원문과 대조해 보세요.",
        }
    else:
        badge = {
            "level": "low",
            "label": "AI 해석 비중 높음",
            "description": "원문 인용이 적습니다. 참고용으로만 쓰고 원문을 직접 확인하세요.",
        }
    if excerpted:
        badge["description"] += " (긴 작품이라 관련 문단만 골라 분석했습니다.)"
    return badge
