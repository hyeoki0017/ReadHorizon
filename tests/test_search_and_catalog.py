from __future__ import annotations

import json

import pytest

from readhorizon.catalog.bibliographic import normalize_isbn, parse_seoji_response, suggest_from_kdc
from readhorizon.references.search import BM25, tokenize
from readhorizon.references.store import ReferenceImportError, ReferenceIndex, chunk_text, import_references


def test_tokenize_korean_bigrams_and_latin_words():
    assert tokenize("별을 노래") == ["별을", "노래"]
    assert tokenize("하늘바람") == ["하늘", "늘바", "바람"]
    assert tokenize("BM25 Search") == ["bm25", "search"]


def test_tokenize_keeps_archaic_hangul():
    assert "ᄒᆞ" in "".join(tokenize("ᄒᆞᄂᆞᆯ"))


def test_bm25_ranks_relevant_document_first():
    docs = ["별을 노래하는 마음으로", "진달래꽃 아름 따다", "오늘 밤에도 별이 바람에 스치운다"]
    top = BM25(docs).top("바람에 스치는 별", 2)
    assert top[0][0] == 2


def test_bm25_returns_nothing_without_overlap():
    assert BM25(["가나다"]).top("zzz", 3) == []


@pytest.mark.parametrize(
    ("kdc", "expected"),
    [
        ("811.6", ("literature", "poem")),
        ("812", ("literature", "drama")),
        ("813.7", ("literature", "novel")),
        ("814", ("literature", "essay")),
        ("843", ("literature", "novel")),
        ("810", ("literature", None)),
        ("325.1", ("study", "textbook")),
        (None, (None, None)),
        ("abc", (None, None)),
    ],
)
def test_suggest_from_kdc(kdc, expected):
    assert suggest_from_kdc(kdc) == expected


def test_parse_seoji_response_is_defensive():
    payload = {
        "TOTAL_COUNT": "2",
        "docs": [
            {"TITLE": " 하늘과 바람과 별과 시 ", "AUTHOR": "윤동주 지음", "PUBLISHER": "정음사",
             "EA_ISBN": "9788900000000", "KDC": "811.6", "PAGE": "120", "PUBLISH_PREDATE": "19480130"},
            {"title": "소문자 키", "kdc": "813"},
            {"AUTHOR": "제목 없음"},
            "잘못된 항목",
        ],
    }
    records = parse_seoji_response(payload)
    assert [r.title for r in records] == ["하늘과 바람과 별과 시", "소문자 키"]
    assert records[0].suggested_genre == "poem"
    assert records[1].suggested_genre == "novel"
    assert parse_seoji_response({}) == []


def test_normalize_isbn():
    assert normalize_isbn("978-89-374-6010-2") == "9788937460102"
    assert normalize_isbn("89-374-6010-x") == "893746010X"
    assert normalize_isbn("123") is None


def test_chunk_text_respects_size():
    text = "\n\n".join(["가" * 300] * 5)
    chunks = chunk_text(text, size=700)
    assert len(chunks) == 3
    assert all(len(c) <= 700 for c in chunks)


def test_import_requires_source_tier_license(store, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"title": "t", "source": "s", "tier": "A", "text": "본문"}]), encoding="utf-8")
    with pytest.raises(ReferenceImportError, match="license"):
        import_references(store, bad)

    wrong_tier = tmp_path / "tier.json"
    wrong_tier.write_text(
        json.dumps([{"title": "t", "source": "s", "tier": "S", "license": "CC0", "text": "본문"}]), encoding="utf-8"
    )
    with pytest.raises(ReferenceImportError, match="tier"):
        import_references(store, wrong_tier)


def test_reference_index_filters_other_works(store, tmp_path):
    path = tmp_path / "refs.json"
    path.write_text(
        json.dumps(
            [
                {"title": "서시 해설", "source": "예시", "tier": "B", "license": "CC0", "work_title": "서시",
                 "text": "「서시」는 부끄럼 없는 삶에 대한 다짐을 노래한다."},
                {"title": "진달래꽃 해설", "source": "예시", "tier": "B", "license": "CC0", "work_title": "진달래꽃",
                 "text": "「진달래꽃」은 부끄럼 없는 이별의 정한을 노래한다."},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert import_references(store, path) == 2
    index = ReferenceIndex(store)
    hits = index.search("부끄럼 없는 삶 다짐 노래", "서시")
    assert hits and all(h.title == "서시 해설" for h in hits)
    assert index.search("부끄럼 없는", "「진달래꽃」")[0].title == "진달래꽃 해설"
