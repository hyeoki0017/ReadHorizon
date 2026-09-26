"""국립중앙도서관 ISBN 서지정보 Open API 클라이언트.

- 서점 페이지 크롤링은 하지 않는다(약관·DB권 문제). 공공 API만 쓴다.
- 외부로 보내는 값은 검색어(제목) 또는 ISBN뿐이다.
- 응답 필드 이름은 기관 문서 기준(TITLE, AUTHOR, PUBLISHER, EA_ISBN, KDC, PAGE, PUBLISH_PREDATE)이며,
  대소문자 차이에 대비해 키를 대문자로 맞춰 읽는다. 실제 키 발급 후 첫 호출에서 필드 매핑을 확인할 것.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

SEOJI_ENDPOINT = "https://www.nl.go.kr/seoji/SearchApi.do"

KDC_LITERATURE_GENRES = {"1": "poem", "2": "drama", "3": "novel", "4": "essay"}


class BibliographicError(RuntimeError):
    pass


@dataclass
class BibRecord:
    title: str
    author: str | None
    publisher: str | None
    isbn: str | None
    kdc: str | None
    pages: str | None
    publish_date: str | None
    suggested_category: str | None
    suggested_genre: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def suggest_from_kdc(kdc: str | None) -> tuple[str | None, str | None]:
    """KDC 분류번호로 카테고리·장르를 추천한다. 8xx 문학: 8X1 시, 8X2 희곡, 8X3 소설, 8X4 수필."""
    match = re.match(r"\s*(\d{3})", kdc or "")
    if not match:
        return None, None
    code = match.group(1)
    if code[0] == "8":
        return "literature", KDC_LITERATURE_GENRES.get(code[2])
    return "study", "textbook"


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def parse_seoji_response(payload: dict) -> list[BibRecord]:
    docs = payload.get("docs") or payload.get("DOCS") or []
    records: list[BibRecord] = []
    for raw in docs:
        if not isinstance(raw, dict):
            continue
        doc = {str(k).upper(): v for k, v in raw.items()}
        title = _clean(doc.get("TITLE"))
        if not title:
            continue
        kdc = _clean(doc.get("KDC"))
        category, genre = suggest_from_kdc(kdc)
        records.append(
            BibRecord(
                title=title,
                author=_clean(doc.get("AUTHOR")),
                publisher=_clean(doc.get("PUBLISHER")),
                isbn=_clean(doc.get("EA_ISBN")) or _clean(doc.get("SET_ISBN")),
                kdc=kdc,
                pages=_clean(doc.get("PAGE")),
                publish_date=_clean(doc.get("PUBLISH_PREDATE")),
                suggested_category=category,
                suggested_genre=genre,
            )
        )
    return records


def normalize_isbn(value: str) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", value or "")
    return digits.upper() if len(digits) in (10, 13) else None


class NationalLibraryClient:
    def __init__(self, cert_key: str, timeout: float = 8.0) -> None:
        self.cert_key = cert_key
        self.timeout = timeout

    def search(self, query: str | None = None, isbn: str | None = None, size: int = 10) -> list[BibRecord]:
        params = {
            "cert_key": self.cert_key,
            "result_style": "json",
            "page_no": "1",
            "page_size": str(max(1, min(size, 20))),
        }
        if isbn:
            normalized = normalize_isbn(isbn)
            if not normalized:
                raise BibliographicError("ISBN 형식이 올바르지 않습니다(10자리 또는 13자리).")
            params["isbn"] = normalized
        elif query and query.strip():
            params["title"] = query.strip()[:100]
        else:
            raise BibliographicError("검색어 또는 ISBN을 입력해 주세요.")

        url = f"{SEOJI_ENDPOINT}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310 (고정 https 엔드포인트)
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise BibliographicError("국립중앙도서관 서지 검색에 실패했습니다. 잠시 후 다시 시도해 주세요.") from exc
        return parse_seoji_response(payload if isinstance(payload, dict) else {})
