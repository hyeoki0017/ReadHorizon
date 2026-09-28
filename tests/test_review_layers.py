"""번역 3층 보기(직역·의역·주석)와 심화 검토(초안 → 검토 → 수정)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from readhorizon.analysis import prompts
from readhorizon.analysis.checks import translation_checks
from readhorizon.analysis.engine import AnalysisEngine, AnalysisRequestError, WorkContext
from readhorizon.analysis.llm import (
    MAX_TOKENS,
    TRANSLATE_MAX_TOKENS,
    ClaudeClient,
    DemoClient,
    DocSpec,
    JSONResult,
    LLMResult,
    RawBlock,
    RawCitation,
)
from readhorizon.analysis.presets import ReaderProfile, find_genre, is_layered, parse_translate_option, translate_option
from readhorizon.analysis.review import normalize_issues, revision_lines
from readhorizon.ingest.normalize import Paragraph
from readhorizon.quality_eval import run_quality_eval


def _paras(*texts: str) -> list[Paragraph]:
    return [Paragraph(i, f"{i + 1}문단", None, t) for i, t in enumerate(texts)]


def _ctx(*texts: str, intro: str | None = None) -> WorkContext:
    cat, gen = find_genre("custom", "general")
    return WorkContext("글", None, cat, gen, _paras(*texts) if texts else [], intro)


class ReviewLLM:
    """원문 블록을 모두 인용하는 결과를 만들고, 검토 요청에는 정해 둔 문제 목록을 돌려준다."""

    mode = "live"
    model = "fake"

    def __init__(self, issues=None):
        self.issues = issues if issues is not None else []
        self.calls: list[dict] = []

    def generate(self, system, docs, task, meta):
        self.calls.append({"task": task, "meta": meta})
        work = docs[0]
        blocks = [RawBlock("## 결과\n")]
        for pos, block in enumerate(work.blocks):
            blocks.append(RawBlock("- "))
            blocks.append(RawBlock("풀이", [RawCitation("content_block_location", block, 0, pos, pos + 1)]))
            blocks.append(RawBlock("\n"))
        return LLMResult(blocks, "end_turn", "fake", usage={"input_tokens": 100, "output_tokens": 40})

    def complete_json(self, system, prompt, schema, meta=None):
        self.calls.append({"task": prompt, "meta": meta, "system": system})
        return JSONResult({"issues": self.issues, "summary": "요약"}, "fake", usage={"input_tokens": 50, "output_tokens": 20})


# ── 3층 번역 ──────────────────────────────────────────────
def test_layered_option_parsing():
    assert parse_translate_option("en:2:layers") == ("en", 2) and is_layered("en:2:layers")
    assert not is_layered("en:2") and not is_layered("easy:1:layers") and not is_layered("en:2:other")
    assert translate_option("en", 2, True) == "en:2:layers" and translate_option("easy", 1, True) == "easy:1"


def test_layered_prompt_uses_labels_for_the_explanation_language():
    ko = prompts.translate_task("[작품 정보]", "modern", layered=True)
    assert "### [위치]" in ko and "- **직역**: " in ko and "- **의역**: " in ko and "- **주석**: " in ko
    en = prompts.translate_task("[작품 정보]", "en", lang="en", layered=True)
    assert "- **Literal**: " in en and "- **Notes**: " in en and "## Vocabulary" in en
    assert "직역" not in prompts.translate_task("[작품 정보]", "en")


def test_engine_layered_translation_marks_result():
    llm = ReviewLLM()
    result = AnalysisEngine(llm, None, 200_000).analyze(_ctx("첫 문단", "둘째 문단"), "translate", "en:1:layers")
    assert llm.calls[0]["meta"]["layered"] is True and "직역·의역·주석" in llm.calls[0]["task"]
    assert result["option"] == "en:1:layers" and result["layered"] is True


def test_note_lines_are_left_out_of_number_and_length_checks():
    paragraphs = _paras("임금이 궁을 떠났다")
    segments = [
        {"text": "### [1문단]\n- **직역**: 임금이 궁궐을 떠났다", "citations": [{"source": "work", "paragraphs": [0], "verified": True}]},
        {"text": "\n- **의역**: 임금이 궁궐을 떠나 피란했다\n- **주석**: 1592년 임진왜란 때의 일로 보인다\n", "citations": []},
    ]
    checks = translation_checks(segments, paragraphs, "modern")
    assert checks["extra_numbers"] == [] and checks["ok"] is True


def test_translation_requests_get_a_larger_output_budget():
    sent = []

    class Stream:
        def __init__(self, request):
            sent.append(request)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return SimpleNamespace(content=[], stop_reason="end_turn", model="claude-opus-5", usage=None)

    stub = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **request: Stream(request))))
    client = ClaudeClient("claude-opus-5", "high", client=stub)
    docs = [DocSpec(role="work", title="원문", blocks=["[1문단] 가"], index_map=[0], cache=True)]
    client.generate("SYS", docs, "요청", {"kind": "translate"})
    client.generate("SYS", docs, "요청", {"kind": "overview"})
    assert [r["max_tokens"] for r in sent] == [TRANSLATE_MAX_TOKENS, MAX_TOKENS]


# ── 심화 검토 ──────────────────────────────────────────────
def test_normalize_issues_maps_locations_and_puts_major_first():
    paragraphs = _paras("가", "나")
    data = {"issues": [
        {"location": "1문단", "category": "어색함", "severity": "minor", "explanation": "a", "suggestion": ""},
        {"location": "[2문단]", "category": "오역", "severity": "major", "explanation": "b", "suggestion": "고친 문장"},
        {"location": "3문단", "category": "모름", "severity": "major", "explanation": "c", "suggestion": ""},
        {"location": "", "category": "누락", "severity": "fatal", "explanation": "d", "suggestion": ""},
    ]}
    issues = normalize_issues(data, paragraphs)
    assert [(i["location"], i["paragraph"], i["severity"]) for i in issues] == [("2문단", 1, "major"), ("1문단", 0, "minor")]
    lines = revision_lines(issues)
    assert "[이전 결과 검토]" in lines and "- [2문단] 오역(major): b → 제안: 고친 문장" in lines
    assert revision_lines([]) == ""


def test_engine_review_uses_the_translated_part_and_revision_rewrites_it():
    issues = [{"location": "3문단", "category": "누락", "severity": "major", "explanation": "빠짐", "suggestion": "넣기"}]
    llm = ReviewLLM(issues)
    engine = AnalysisEngine(llm, None, 200_000, translate_part_chars=1000)
    ctx = _ctx("가" * 600, "나" * 600, "다" * 300)
    draft = engine.analyze(ctx, "translate", "en:2", ReaderProfile.of("high", "ko"))

    review = engine.review(ctx, draft)
    prompt = llm.calls[-1]["task"]
    assert "[2문단]" in prompt and "[1문단]" not in prompt  # 2부분(2~3문단)만 대조한다
    assert "[독자] 고등학생·수험생" in prompt and llm.calls[-1]["meta"]["kind"] == "review"
    assert review["issues"][0]["paragraph"] == 2 and review["major"] == 1 and review["usage"]["input_tokens"] == 50

    revised = engine.analyze(ctx, "translate", "en:2", ReaderProfile.of("high", "ko"), None, review["issues"])
    assert "[이전 결과 검토]" in llm.calls[-1]["task"] and "- [3문단] 누락(major): 빠짐" in llm.calls[-1]["task"]
    assert revised["revision"] == {"issues": 1, "major": 1} and "revision" not in draft

    with pytest.raises(AnalysisRequestError, match="원문이 없어"):
        engine.review(_ctx(intro="소개"), {"kind": "guide", "segments": []})


def test_demo_review_returns_an_example_issue():
    data = DemoClient().complete_json("s", "p", {}, {"kind": "review", "labels": ["1연 1행"]}).data
    assert data["issues"][0]["location"] == "1연 1행" and "데모" in data["summary"]


def test_api_review_then_revise(client, workspace, seosi_text):
    fields = {"category": "literature", "genre": "poem", "title": "서시", "text": seosi_text}
    work_id = client.post("/api/works", files={k: (None, v) for k, v in fields.items()}, headers=workspace).json()["work"]["id"]
    body = {"kind": "translate", "option": "modern:1:layers", "audience": "middle"}

    assert client.post(f"/api/works/{work_id}/review", json=body, headers=workspace).status_code == 404
    assert client.post(f"/api/works/{work_id}/analyze", json={**body, "revise": True}, headers=workspace).status_code == 422

    draft = client.post(f"/api/works/{work_id}/analyze", json=body, headers=workspace).json()
    assert draft["option"] == "modern:1:layers@middle.ko" and draft["layered"] is True
    assert any("**직역**" in s["text"] for s in draft["segments"])

    reviewed = client.post(f"/api/works/{work_id}/review", json=body, headers=workspace).json()
    assert reviewed["review"]["issues"][0]["location"] == "1연 1행" and reviewed["review"]["issues"][0]["paragraph"] == 0
    stored = client.post(f"/api/works/{work_id}/analyze", json=body, headers=workspace).json()
    assert stored["cached"] is True and stored["review"]["mode"] == "demo"

    revised = client.post(f"/api/works/{work_id}/analyze", json={**body, "revise": True}, headers=workspace).json()
    assert revised["cached"] is False and revised["revision"] == {"issues": 1, "major": 0} and "review" not in revised
    again = client.post(f"/api/works/{work_id}/analyze", json=body, headers=workspace).json()
    assert again["revision"]["issues"] == 1 and "review" not in again


def test_quality_eval_with_revise_reviews_and_rewrites(seosi_text):
    issues = [{"location": "1연 1행", "category": "오역", "severity": "major", "explanation": "뜻이 바뀜", "suggestion": ""}]
    llm = ReviewLLM(issues)
    items = [{"id": "a", "category": "literature", "genre": "poem", "passage": seosi_text, "kind": "translate", "option": "en", "layered": True, "key_points": ["x"]}]
    report = run_quality_eval(AnalysisEngine(llm, None, 200_000), None, items, revise=True)
    row = report["items"][0]
    assert row["option"] == "en:1:layers" and row["review"] == {"issues": 1, "major": 1, "revised": True}
    assert report["summary"]["revised_items"] == 1 and report["revise"] is True
    assert report["usage"]["generation"]["input_tokens"] == 200 and report["usage"]["review"]["input_tokens"] == 50
    assert sum(1 for c in llm.calls if c["meta"] and c["meta"].get("kind") == "translate") == 2

    clean = run_quality_eval(AnalysisEngine(ReviewLLM([]), None, 200_000), None, items, revise=True)
    assert clean["items"][0]["review"]["revised"] is False and clean["summary"]["revised_items"] == 0
