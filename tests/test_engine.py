"""분석 엔진: 요청 구성(Claude API 형식)과 응답 후처리(인용 검증·근거율)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from readhorizon.analysis.engine import AnalysisEngine, AnalysisRequestError, WorkContext, select_paragraphs
from readhorizon.analysis.grounding import is_supported, summarize
from readhorizon.analysis.llm import (
    FALLBACK_BETA,
    ClaudeClient,
    DocSpec,
    LLMRefusal,
    LLMResult,
    RawBlock,
    RawCitation,
    parse_message,
)
from readhorizon.analysis.presets import find_genre
from readhorizon.ingest.normalize import Paragraph, normalize_text


def _ctx(text: str | None, intro: str | None = None, category: str = "literature", genre: str = "poem") -> WorkContext:
    cat, gen = find_genre(category, genre)
    paragraphs = normalize_text(text, verse=gen.verse == "yes") if text else []
    return WorkContext("서시", "윤동주", cat, gen, paragraphs, intro)


class RecordingLLM:
    """요청을 기록하고, 미리 정한 블록을 돌려준다."""

    mode = "live"
    model = "fake"

    def __init__(self, blocks_factory):
        self.blocks_factory = blocks_factory
        self.calls = []

    def generate(self, system, docs, task, meta):
        self.calls.append({"system": system, "docs": docs, "task": task, "meta": meta})
        return LLMResult(self.blocks_factory(docs), "end_turn", "fake")


def test_claude_request_shape():
    client = ClaudeClient("claude-opus-5", "high", client=object())
    docs = [
        DocSpec(role="work", title="작품 원문: 서시", blocks=["[1행] 가", "[2행] 나"], index_map=[0, 1], cache=True),
        DocSpec(role="intro", title="사용자 제공 소개", text="소개글입니다.", tier="C"),
    ]
    request = client.build_request("SYS", docs, "요청")
    content = request["messages"][0]["content"]

    assert request["model"] == "claude-opus-5"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "high"}
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert "output_config" in request and "format" not in request["output_config"]  # citations와 structured outputs 동시 사용 금지

    work, intro, task = content
    assert work["source"] == {"type": "content", "content": [{"type": "text", "text": "[1행] 가"}, {"type": "text", "text": "[2행] 나"}]}
    assert work["citations"] == {"enabled": True} and work["cache_control"] == {"type": "ephemeral"}
    assert intro["source"] == {"type": "text", "media_type": "text/plain", "data": "소개글입니다."}
    assert intro["citations"] == {"enabled": True} and "cache_control" not in intro
    assert task == {"type": "text", "text": "요청"}


def test_claude_request_skips_unsupported_params_for_other_models():
    request = ClaudeClient("claude-haiku-4-5", "high", client=object()).build_request("S", [], "t")
    assert "thinking" not in request and "fallbacks" not in request and "betas" not in request


def test_parse_message_maps_citation_types():
    message = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0, iterations=[SimpleNamespace(type="fallback_message")]),
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="화자는 ", citations=None),
            SimpleNamespace(type="text", text="부끄럼 없기를 바란다", citations=[
                SimpleNamespace(type="content_block_location", cited_text="[1연 2행] 한 점 부끄럼이 없기를,",
                                document_index=0, start_block_index=1, end_block_index=2),
                SimpleNamespace(type="char_location", cited_text="소개", document_index=1,
                                start_char_index=0, end_char_index=2),
            ]),
        ],
    )
    result = parse_message(message)
    assert [b.text for b in result.blocks] == ["화자는 ", "부끄럼 없기를 바란다"]
    assert [(c.type, c.start, c.end) for c in result.blocks[1].citations] == [
        ("content_block_location", 1, 2), ("char_location", 0, 2)
    ]
    assert result.fallback_used is True


def test_parse_message_refusal():
    with pytest.raises(LLMRefusal):
        parse_message(SimpleNamespace(stop_reason="refusal", content=[], usage=None, model="m"))


def test_engine_verifies_citations_and_computes_badge(seosi_text):
    def blocks(docs):
        work = docs[0]
        return [
            RawBlock("## 화자와 상황\n"),
            RawBlock("화자는 부끄럼 없는 삶을 소망한다.", [
                RawCitation("content_block_location", work.blocks[1], 0, 1, 2),
            ]),
            RawBlock(" 이는 해석이다.\n"),
            RawBlock("가짜 인용.", [RawCitation("content_block_location", "존재하지 않는 문장", 0, 0, 1)]),
        ]

    llm = RecordingLLM(blocks)
    result = AnalysisEngine(llm, None, 200_000).analyze(_ctx(seosi_text), "overview")

    cited = result["segments"][1]["citations"][0]
    assert cited["paragraphs"] == [1] and cited["labels"] == ["1연 2행"]
    assert cited["verified"] is True and cited["cited_text"] == "한 점 부끄럼이 없기를,"
    assert result["segments"][3]["citations"][0]["verified"] is False
    assert result["summary"]["verified_count"] == 1 and result["summary"]["unverified_count"] == 1
    assert 0 < result["summary"]["ratio"] < 1
    assert result["badge"]["level"] in {"medium", "high", "low"}

    call = llm.calls[0]
    assert call["docs"][0].cache is True
    assert call["docs"][0].blocks[0] == "[1연 1행] 죽는 날까지 하늘을 우러러"
    assert "## 화자와 상황" in call["task"] and "작품 정보" in call["task"]


def test_engine_rejects_invalid_citation_ranges(seosi_text):
    llm = RecordingLLM(lambda docs: [RawBlock("x", [RawCitation("content_block_location", "x", 0, 5, 99)])])
    result = AnalysisEngine(llm, None, 200_000).analyze(_ctx(seosi_text), "flow")
    assert result["segments"][0]["citations"] == []


def test_intro_only_allows_guide_but_not_interpretation():
    llm = RecordingLLM(lambda docs: [RawBlock("소개글에 따르면 ", []), RawBlock("시집이다", [
        RawCitation("char_location", "유고 시집이다.", 0, 0, 8)
    ])])
    engine = AnalysisEngine(llm, None, 200_000)
    ctx = _ctx(None, intro="유고 시집이다. 별과 바람을 노래한다.")

    with pytest.raises(AnalysisRequestError, match="읽기 가이드"):
        engine.analyze(ctx, "overview")

    result = engine.analyze(ctx, "guide")
    assert result["badge"]["level"] == "intro"
    assert [d.role for d in llm.calls[0]["docs"]] == ["intro"]
    assert "원문이 없습니다" in llm.calls[0]["task"]
    citation = result["segments"][1]["citations"][0]
    assert citation["source"] == "intro" and citation["verified"] is True
    assert result["summary"]["ratio"] == 0  # 소개글 인용은 근거율에 넣지 않는다


def test_intro_is_passed_as_separate_c_tier_document(seosi_text):
    llm = RecordingLLM(lambda docs: [RawBlock("결과")])
    AnalysisEngine(llm, None, 200_000).ask(_ctx(seosi_text, intro="소개"), "주제는?")
    roles = [d.role for d in llm.calls[0]["docs"]]
    assert roles == ["work", "intro"]
    assert llm.calls[0]["docs"][1].tier == "C"


def test_select_paragraphs_excerpts_long_works():
    paragraphs = [Paragraph(i, f"{i + 1}문단", None, f"문단 {i} " + "가" * 100) for i in range(100)]
    selected, excerpted = select_paragraphs(paragraphs, 1000, None)
    assert excerpted and 0 < len(selected) < 100
    assert sum(len(p.text) for p in selected) <= 1000

    paragraphs[57] = Paragraph(57, "58문단", None, "별이 바람에 스치운다 " + "나" * 90)
    selected, _ = select_paragraphs(paragraphs, 1000, "별이 바람에 스치운다")
    assert 57 in [p.index for p in selected]

    short = paragraphs[:3]
    assert select_paragraphs(short, 10_000, None) == (short, False)


def test_excerpted_work_maps_citations_back_to_original_indices():
    paragraphs = [f"문단 {i} 내용입니다." for i in range(40)]
    paragraphs[30] = "핵심 단서가 여기에 있습니다."
    text = "\n\n".join(paragraphs)
    cat, gen = find_genre("literature", "novel")
    ctx = WorkContext("긴 소설", None, cat, gen, normalize_text(text, verse=False), None)

    def blocks(docs):
        work = docs[0]
        pos = next(i for i, b in enumerate(work.blocks) if "핵심 단서" in b)
        return [RawBlock("단서", [RawCitation("content_block_location", work.blocks[pos], 0, pos, pos + 1)])]

    result = AnalysisEngine(RecordingLLM(blocks), None, 120).ask(ctx, "핵심 단서는 어디에?")
    assert result["excerpted"] is True
    assert result["segments"][0]["citations"][0]["paragraphs"] == [30]


def test_summary_ignores_headings_and_notes():
    segments = [
        {"text": "> 안내문\n## 제목\n", "citations": []},
        {"text": "근거 있는 문장.", "citations": [{"verified": True, "source": "work", "tier": None}]},
        {"text": " 근거 없는 문장.", "citations": []},
    ]
    summary = summarize(segments)
    assert summary["ratio"] == pytest.approx(len("근거있는문장.") / len("근거있는문장.근거없는문장."), abs=0.01)


def test_is_supported_ignores_whitespace():
    assert is_supported("한 점  부끄럼이", "[1연 2행] 한 점 부끄럼이 없기를,")
    assert not is_supported("", "아무거나")
    assert not is_supported("없는 문장", "다른 문장")


def test_select_paragraphs_never_returns_empty():
    giant = [Paragraph(0, "1문단", None, "가" * 10_000), Paragraph(1, "2문단", None, "나" * 10_000)]
    selected, excerpted = select_paragraphs(giant, 5000, None)
    assert excerpted and [p.index for p in selected] == [0]
    selected, _ = select_paragraphs(giant, 5000, "전혀 없는 질문")
    assert selected


def test_select_paragraphs_keeps_relevant_paragraph_even_if_neighbor_is_large():
    paragraphs = [Paragraph(i, f"{i + 1}문단", None, "다" * 100) for i in range(50)]
    paragraphs[9] = Paragraph(9, "10문단", None, "라" * 1900)  # 관련 문단 바로 앞의 큰 문단
    paragraphs[10] = Paragraph(10, "11문단", None, "별이 바람에 스치운다")
    selected, _ = select_paragraphs(paragraphs, 1000, "별이 바람에 스치운다")
    assert 10 in [p.index for p in selected]
