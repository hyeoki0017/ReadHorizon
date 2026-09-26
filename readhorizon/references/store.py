"""공용 참고자료: 가져오기(출처·등급·라이선스 필수)와 검색.

공용 참고자료에는 퍼블릭 도메인이거나 이용 허락(라이선스)을 확보한 자료만 넣는다.
사용자가 올린 원문이나 책 소개는 여기에 넣지 않는다(자료실 전용).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..db import Store
from ..ingest.normalize import clean_line, clean_text
from ..trust import TIERS
from .search import BM25

CHUNK_CHARS = 700
MIN_SCORE = 1.0


class ReferenceImportError(ValueError):
    pass


def normalize_title(value: str | None) -> str:
    return re.sub(r"[\s「」『』<>《》\"'“”‘’()\[\]]", "", clean_line(value)).lower()


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 1 > size:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def _field(entry: dict, key: str) -> str:
    value = entry.get(key)
    return clean_line(str(value)) if value is not None else ""


def validate_entry(entry: object, position: int) -> dict:
    where = f"{position + 1}번째 항목"
    if not isinstance(entry, dict):
        raise ReferenceImportError(f"{where}: 항목은 JSON 객체여야 합니다.")
    for key in ("title", "source", "tier", "license", "text"):
        if not _field(entry, key):
            raise ReferenceImportError(f"{where}: '{key}'가 비어 있습니다. 출처·등급·라이선스는 필수입니다.")
    tier = _field(entry, "tier").upper()
    if tier not in TIERS:
        raise ReferenceImportError(f"{where}: tier는 A, B, C 중 하나여야 합니다.")
    url = _field(entry, "url") or None
    if url and not url.lower().startswith(("https://", "http://")):
        raise ReferenceImportError(f"{where}: url은 http(s) 주소여야 합니다.")
    return {
        "title": _field(entry, "title"),
        "author": _field(entry, "author") or None,
        "source": _field(entry, "source"),
        "url": url,
        "tier": tier,
        "license": _field(entry, "license"),
        "work_title": _field(entry, "work_title") or None,
        "text": clean_text(str(entry["text"])),
    }


def import_references(store: Store, path: Path) -> int:
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceImportError(f"참고자료 파일을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(entries, list):
        raise ReferenceImportError("참고자료 파일은 항목들의 JSON 배열이어야 합니다.")
    validated = [validate_entry(e, i) for i, e in enumerate(entries)]
    for item in validated:
        store.add_reference(
            title=item["title"],
            author=item["author"],
            source=item["source"],
            url=item["url"],
            tier=item["tier"],
            license=item["license"],
            work_title=item["work_title"],
            chunks=chunk_text(item["text"]),
        )
    return len(validated)


@dataclass
class ReferenceHit:
    ref_id: str
    title: str
    source: str
    url: str | None
    tier: str
    license: str
    text: str
    score: float


class ReferenceIndex:
    """참고자료 BM25 색인. 참고자료가 바뀌면 다시 만든다.

    (지문, 조각 목록, 색인)을 한 튜플로 통째로 바꿔, 동시에 검색하는 스레드가 서로 다른 버전을 섞어 보지 않게 한다.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._snapshot: tuple[tuple | None, list[dict], BM25 | None] = (None, [], None)

    def _current(self) -> tuple[list[dict], BM25 | None]:
        fingerprint = self.store.reference_fingerprint()
        cached_fingerprint, chunks, bm25 = self._snapshot
        if fingerprint != cached_fingerprint or bm25 is None:
            chunks = self.store.all_reference_chunks()
            bm25 = BM25([f"{c['title']} {c['work_title'] or ''} {c['text']}" for c in chunks])
            self._snapshot = (fingerprint, chunks, bm25)
        return chunks, bm25

    def search(self, query: str, work_title: str, k: int = 4) -> list[ReferenceHit]:
        chunks, bm25 = self._current()
        if not chunks or bm25 is None:
            return []
        target = normalize_title(work_title)
        hits: list[ReferenceHit] = []
        # 다른 작품 자료를 걸러 낸 뒤에도 k개를 채울 수 있도록 전체 순위를 본다
        for i, score in bm25.top(query, len(chunks)):
            chunk = chunks[i]
            linked = normalize_title(chunk["work_title"])
            if linked and linked != target:
                continue  # 다른 작품에 대한 자료는 인용하지 않는다
            if not linked and score < MIN_SCORE:
                continue  # 특정 작품에 묶이지 않은 일반 자료는 관련도가 충분할 때만
            hits.append(
                ReferenceHit(
                    chunk["ref_id"], chunk["title"], chunk["source"], chunk["url"],
                    chunk["tier"], chunk["license"], chunk["text"], score,
                )
            )
            if len(hits) >= k:
                break
        return hits
