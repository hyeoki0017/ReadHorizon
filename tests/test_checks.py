"""번역 자동 검사와 번역 부분 나누기."""

from __future__ import annotations

import pytest

from readhorizon.analysis.checks import numbers, translation_checks, translation_segments
from readhorizon.analysis.engine import AnalysisEngine, AnalysisRequestError, WorkContext, split_parts
from readhorizon.analysis.llm import LLMResult, RawBlock, RawCitation
from readhorizon.analysis.presets import find_genre, parse_translate_option, translate_option
from readhorizon.ingest.normalize import Paragraph


def _paras(*texts: str) -> list[Paragraph]:
    return [Paragraph(i, f"{i + 1}문단", None, t) for i, t in enumerate(texts)]


def _seg(text: str, *indices: int) -> dict:
    citations = [{"source": "work", "paragraphs": [i], "verified": True} for i in indices]
    return {"text": text, "citations": citations}


def test_numbers_ignore_positions_single_digits_and_separators():
    assert numbers("- [3문단] 1,000명이 1994년에 3.5% 늘었다. 2연 4행, 세 사람(3)") == ["1000", "1994", "3.5"]


def test_vocabulary_section_is_not_part_of_the_translation():
    segments = [_seg("- [1문단] 번역", 0), _seg("\n## 어휘 풀이\n- 우러러: 올려다보며", 1), _seg("더 있음", 1)]
    body = translation_segments(segments)
    assert [s["text"] for s in body] == ["- [1문단] 번역"]
    checks = translation_checks(segments, _paras("첫 문단", "둘째 문단"), "en")
    assert checks["missing_paragraphs"] == [{"index": 1, "label": "2문단"}]
    assert checks["items"][0]["status"] == "warn" and "2문단" in checks["items"][0]["detail"]


def test_number_mismatches_are_reported_both_ways():
    paragraphs = _paras("1919년 3월 1일에 33인이 모였다.", "모두 3.5배")
    segments = [_seg("- [1문단] In 1919, 33 people gathered.", 0), _seg("- [2문단] 350 percent in 2020", 1)]
    checks = translation_checks(segments, paragraphs, "en")
    assert checks["missing_numbers"] == [{"value": "3.5", "label": "2문단", "index": 1}]
    assert checks["extra_numbers"] == ["2020", "350"]
    item = next(i for i in checks["items"] if i["id"] == "numbers")
    assert item["status"] == "warn" and "3.5(2문단)" in item["detail"] and "2020" in item["detail"]
    assert checks["ok"] is False and checks["length_ratio"] is None  # 영어 번역은 분량 비교를 하지 않는다


def test_clean_modern_translation_passes_and_short_one_is_flagged():
    paragraphs = _paras("죽는 날까지 하늘을 우러러 한 점 부끄럼이 없기를", "잎새에 이는 바람에도 나는 괴로워했다")
    good = [_seg("- [1문단] 죽는 날까지 하늘을 올려다보며 조금의 부끄러움도 없기를 바라며", 0), _seg("- [2문단] 나뭇잎을 흔드는 바람에도 나는 괴로워했다", 1)]
    checks = translation_checks(good, paragraphs, "modern")
    assert checks["ok"] is True and [i["id"] for i in checks["items"]] == ["coverage", "length"]

    short = [_seg("- [1문단] 부끄럼 없기를", 0), _seg("- [2문단] 괴로움", 1)]
    flagged = translation_checks(short, paragraphs, "modern")
    assert flagged["length_ratio"] < 0.4 and flagged["items"][-1]["status"] == "warn"


def test_translate_option_parsing():
    assert parse_translate_option("en") == ("en", 1)
    assert parse_translate_option("modern:3") == ("modern", 3)
    assert parse_translate_option("xx:0") == ("easy", 1)
    assert parse_translate_option("ko:abc") == ("ko", 1)
    assert parse_translate_option(None) == ("easy", 1)
    assert translate_option("en", 2) == "en:2"


def test_split_parts_keeps_order_and_never_splits_a_paragraph():
    paragraphs = _paras("가" * 400, "나" * 400, "다" * 400, "라" * 1200, "마" * 10)
    parts = split_parts(paragraphs, 1000)
    assert [[p.index for p in part] for part in parts] == [[0, 1], [2], [3], [4]]
    assert split_parts([], 1000) == []


class PartLLM:
    mode = "live"
    model = "fake"

    def __init__(self):
        self.calls = []

    def generate(self, system, docs, task, meta):
        self.calls.append({"docs": docs, "task": task})
        work = docs[0]
        blocks = [RawBlock("## 영어 번역\n")]
        for pos, block in enumerate(work.blocks):
            blocks.append(RawBlock("- "))
            blocks.append(RawBlock("translated", [RawCitation("content_block_location", block, 0, pos, pos + 1)]))
            blocks.append(RawBlock("\n"))
        return LLMResult(blocks, "end_turn", "fake")


def test_engine_translates_one_part_without_excerpting():
    cat, gen = find_genre("custom", "general")
    paragraphs = _paras(*[f"{n}번 문단 " + "글" * 390 for n in range(1, 8)])
    ctx = WorkContext("긴 글", None, cat, gen, paragraphs, None)
    llm = PartLLM()
    engine = AnalysisEngine(llm, None, fulltext_char_limit=500, translate_part_chars=1000)

    result = engine.analyze(ctx, "translate", "en:2")
    sent = llm.calls[0]["docs"][0]
    assert sent.index_map == [2, 3] and sent.title.endswith("(2/4부분)")
    assert "[범위] 전체 4부분 중 2부분(3문단~4문단)" in llm.calls[0]["task"]
    assert result["excerpted"] is False and result["option"] == "en:2"
    assert result["checks"]["ok"] is True
    assert engine.translate_parts(paragraphs)[1] == {"part": 2, "from": "3문단", "to": "4문단", "chars": 792}

    with pytest.raises(AnalysisRequestError, match="4부분"):
        engine.analyze(ctx, "translate", "en:5")
