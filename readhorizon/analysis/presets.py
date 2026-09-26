"""카테고리·장르별 분석 프리셋.

카테고리 1(문학)·2(학습·연구)는 장르에 맞는 분석 항목을 정하고,
카테고리 3(사용자 지정)은 기사·강의자료 같은 개인 자료를 일반 분석 항목으로 다룬다.
사용자 지정 자료는 해당 사용자의 자료실 안에서만 쓰인다(공용 DB·모델 학습에 섞지 않음).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Genre:
    id: str
    label: str
    description: str
    verse: str  # "yes" | "no" | "auto" — 행 단위(운문)로 나눌지
    sections: tuple[str, ...]
    extra_rules: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    description: str
    genres: tuple[Genre, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        "literature",
        "문학",
        "소설 · 시 · 희곡 · 수필",
        (
            Genre(
                "novel",
                "소설",
                "줄거리, 인물, 시점, 갈등, 주제",
                "no",
                ("줄거리 흐름", "인물과 관계", "시점과 서술 방식", "갈등 구조", "배경의 의미", "주제(해석)"),
            ),
            Genre(
                "poem",
                "시",
                "화자, 시어·이미지, 정서, 표현법, 주제",
                "yes",
                ("화자와 상황", "시어와 이미지", "정서와 태도의 변화", "표현법과 운율", "주제(해석)"),
            ),
            Genre(
                "drama",
                "희곡·시나리오",
                "인물, 갈등, 지시문·대사, 주제",
                "no",
                ("등장인물과 관계", "갈등과 전개", "지시문·대사의 기능", "장면 구성의 효과", "주제(해석)"),
            ),
            Genre(
                "essay",
                "수필",
                "경험과 깨달음, 구조, 표현, 주제",
                "no",
                ("글쓴이의 경험과 깨달음", "글의 구조", "표현상 특징", "주제(해석)"),
            ),
        ),
    ),
    Category(
        "study",
        "학습·연구",
        "수능 · 논문 · 교재",
        (
            Genre(
                "csat_reading",
                "수능 독서(비문학)",
                "문단별 중심 내용, 전개 방식, 핵심 개념",
                "no",
                ("문단별 중심 내용", "글의 전개 방식", "핵심 개념과 관계", "출제 포인트"),
                ("수능 국어 독서 영역의 관점에서 분석한다.", "출제 포인트는 원문 근거가 있는 것만 제시한다."),
            ),
            Genre(
                "csat_literature",
                "수능 문학(현대·고전)",
                "갈래 특징, 화자·시점, 표현, 주제, 출제 포인트",
                "auto",
                ("갈래와 특징", "화자 또는 시점", "표현상 특징", "주제(해석)", "출제 포인트"),
                (
                    "수능 국어 문학 영역의 관점에서 분석한다.",
                    "고전 작품이면 어려운 시어의 현대어 뜻을 함께 밝힌다.",
                ),
            ),
            Genre(
                "paper",
                "논문",
                "연구 질문, 방법, 결과, 한계",
                "no",
                ("연구 질문과 목적", "연구 방법", "핵심 결과", "한계와 후속 과제", "핵심 용어"),
            ),
            Genre(
                "textbook",
                "교재",
                "핵심 개념, 관계, 요약, 확인 질문",
                "no",
                ("핵심 개념", "개념 간 관계", "요약", "확인 질문"),
            ),
        ),
    ),
    Category(
        "custom",
        "사용자 지정",
        "기사 · 강의 자료 · 기타 (내 자료실 전용)",
        (
            Genre(
                "article",
                "기사",
                "요지, 사실과 의견, 근거와 논리",
                "no",
                ("요지", "사실과 의견의 구분", "근거와 논리 흐름", "핵심 용어"),
            ),
            Genre(
                "lecture",
                "강의 자료",
                "핵심 개념, 구조, 예상 질문",
                "no",
                ("핵심 개념", "구조 요약", "예상 질문"),
            ),
            Genre(
                "general",
                "일반 텍스트",
                "요지, 문단 흐름, 핵심 용어",
                "auto",
                ("요지", "문단 흐름", "핵심 용어"),
            ),
        ),
    ),
)

# 분석 종류: 모든 장르 공통
KINDS: dict[str, dict] = {
    "overview": {"label": "작품 분석", "needs_text": True},
    "flow": {"label": "문단 흐름", "needs_text": True},
    "translate": {"label": "번역·풀이", "needs_text": True},
    "guide": {"label": "읽기 가이드", "needs_text": False},
}

TRANSLATE_TARGETS: dict[str, str] = {
    "modern": "현대어 풀이(고전 → 현대 한국어)",
    "easy": "쉬운 말로 풀어쓰기",
    "en": "영어 번역",
    "ko": "한국어 번역",
}


# 독자 수준: 같은 원문도 읽는 사람에 따라 낱말·문장·개념어 풀이를 다르게 한다(PRD 4장 사용자 모드)
AUDIENCES: dict[str, dict[str, str]] = {
    "elementary": {
        "label": "초등학생",
        "guide": "초등학교 고학년이 이해할 수 있는 쉬운 낱말과 짧은 문장으로 씁니다. 어려운 말은 바로 뒤 괄호에 쉬운 말로 풀어 줍니다. 한자어·개념어는 꼭 필요할 때만 쓰고 뜻을 붙입니다.",
    },
    "middle": {
        "label": "중학생",
        "guide": "중학생 수준의 낱말로 씁니다. 화자·시점·비유 같은 개념어는 처음 나올 때 한 줄로 풀이합니다.",
    },
    "high": {
        "label": "고등학생·수험생",
        "guide": "고등학교 국어 교육과정의 개념어를 그대로 쓰고, 수능이 묻는 방식(근거가 되는 문장, 선택지 판단에 쓰이는 표현)과 연결해 설명합니다.",
    },
    "general": {
        "label": "일반 독자",
        "guide": "배경지식이 없는 성인 독자를 기준으로 씁니다. 전문 용어는 풀어 쓰고, 각 부분이 글 전체에서 하는 역할을 짚습니다.",
    },
    "expert": {
        "label": "대학생·연구자",
        "guide": "전공 용어를 그대로 쓰고 원어를 괄호로 병기합니다. 논지 구조·방법·한계와 해석상의 쟁점을 구체적으로 다룹니다.",
    },
}
EXPLAIN_LANGUAGES: dict[str, str] = {"ko": "한국어", "en": "English"}


@dataclass(frozen=True)
class ReaderProfile:
    """읽는 사람 설정. audience가 빈 문자열이면 지정 안 함."""

    audience: str = ""
    lang: str = "ko"

    @classmethod
    def of(cls, audience: str | None = "", lang: str | None = "ko") -> ReaderProfile:
        return cls(audience if audience in AUDIENCES else "", lang if lang in EXPLAIN_LANGUAGES else "ko")

    @property
    def is_default(self) -> bool:
        return not self.audience and self.lang == "ko"

    @property
    def suffix(self) -> str:
        """저장된 분석 결과를 설정별로 구분하는 옵션 꼬리표. 기본 설정이면 빈 문자열(이전 결과와 호환)."""
        return "" if self.is_default else f"@{self.audience or '-'}.{self.lang}"

    def to_dict(self) -> dict:
        return {"audience": self.audience, "lang": self.lang}


def parse_translate_option(option: str | None) -> tuple[str, int]:
    """'en', 'en:2'(대상:부분 번호) → (대상, 부분). 모르는 대상은 쉬운 말 풀이, 잘못된 번호는 1부분."""
    target, _, part = (option or "").partition(":")
    if target not in TRANSLATE_TARGETS:
        target = "easy"
    try:
        number = int(part) if part else 1
    except ValueError:
        number = 1
    return target, max(1, number)


def translate_option(target: str, part: int) -> str:
    return f"{target}:{part}"


def find_genre(category_id: str, genre_id: str) -> tuple[Category, Genre] | None:
    for category in CATEGORIES:
        if category.id != category_id:
            continue
        for genre in category.genres:
            if genre.id == genre_id:
                return category, genre
    return None


def categories_payload() -> list[dict]:
    return [
        {
            "id": c.id,
            "label": c.label,
            "description": c.description,
            "genres": [
                {"id": g.id, "label": g.label, "description": g.description, "verse": g.verse}
                for g in c.genres
            ],
        }
        for c in CATEGORIES
    ]
