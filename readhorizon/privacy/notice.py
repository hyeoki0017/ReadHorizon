"""수집 항목 고지.

실제 값이 아니라 '어떤 항목을 왜, 얼마나 오래' 수집하는지와
최근 30일 동안 자동 마스킹된 민감정보의 '항목별 개수'만 알린다.
"""

from __future__ import annotations

from .masking import LABELS


def build_notice(masking_counts: dict[str, int], retention_days: int, bib_enabled: bool, llm_mode: str) -> dict:
    external = []
    if llm_mode == "live":
        external.append(
            {
                "recipient": "Anthropic (Claude API)",
                "items": "마스킹된 원문, 사용자 제공 책 소개, 질문, 번역 용어집, 텍스트 추출용 이미지·스캔 PDF(메타데이터 제거)",
                "purpose": "텍스트 분석·질의응답·이미지 글자 추출",
            }
        )
    if bib_enabled:
        external.append(
            {
                "recipient": "국립중앙도서관 (ISBN 서지정보 API)",
                "items": "서지 검색어(제목) 또는 ISBN",
                "purpose": "작품 식별·서지정보 조회",
            }
        )
    return {
        "collected_items": [
            {"item": "작품 원문·제목·저자", "purpose": "분석 대상", "retention": f"마지막 이용 후 {retention_days}일"},
            {"item": "책 소개 텍스트·출처 URL (선택)", "purpose": "읽기 가이드·작품 식별 보조", "retention": f"마지막 이용 후 {retention_days}일"},
            {"item": "텍스트 추출용 이미지·스캔 PDF (선택)", "purpose": "글자 추출(OCR)", "retention": "저장하지 않음 · 위치 정보 등 메타데이터 제거 후 전송"},
            {"item": "질문 내용·분석 결과", "purpose": "결과 다시 보기", "retention": f"마지막 이용 후 {retention_days}일"},
            {"item": "번역 용어집(작품별 용어·번역어·메모)", "purpose": "번역어를 모든 부분에서 같게 유지", "retention": f"마지막 이용 후 {retention_days}일"},
            {"item": "읽는 사람 설정(독자 수준·설명 언어)", "purpose": "설명 수준 맞춤", "retention": "이 브라우저에만 저장 · 서버는 요청을 처리할 때만 사용"},
            {"item": "자료실 ID (브라우저가 만든 무작위 값)", "purpose": "내 자료 구분", "retention": "브라우저 저장소를 지우면 연결이 끊김"},
            {"item": "접속 IP", "purpose": "과도한 요청 제한(비용·서비스 보호)", "retention": "메모리에서 최근 1분만 사용, 저장하지 않음"},
        ],
        "not_collected": ["이름", "이메일", "전화번호", "계정 정보", "위치 정보"],
        "masking_period_days": 30,
        "masking_detected": [
            {"category": code, "label": LABELS.get(code, code), "count": count}
            for code, count in sorted(masking_counts.items())
        ],
        "masking_rules": list(LABELS.values()),
        "external_processing": external,
        "policies": [
            "업로드한 원문과 책 소개는 내 자료실에서만 쓰이며, 공용 DB나 AI 학습에 쓰이지 않습니다.",
            "민감정보(주민·외국인등록번호, 카드번호, 전화번호, 이메일)는 저장·전송 전에 자동으로 가려집니다.",
            "서점 페이지를 자동 수집(크롤링)하지 않습니다. 책 소개는 사용자가 직접 붙여넣은 경우에만 씁니다.",
            "이미지에서 추출한 텍스트는 사용자가 원본과 대조해 확인한 뒤에만 작품으로 저장합니다. 이미지 자체는 저장하지 않습니다.",
        ],
    }
