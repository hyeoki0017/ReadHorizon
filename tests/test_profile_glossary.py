"""독자 수준·설명 언어, 모범 해설 예시, 번역 용어집."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from readhorizon.analysis import prompts
from readhorizon.analysis.checks import translation_checks
from readhorizon.analysis.engine import AnalysisEngine, AnalysisRequestError, WorkContext
from readhorizon.analysis.glossary import merge_entries, normalize_entries
from readhorizon.analysis.llm import DemoClient, JSONResult, LLMError, LLMResult, RawBlock, RawCitation
from readhorizon.analysis.presets import ReaderProfile, find_genre
from readhorizon.ingest.normalize import Paragraph
from readhorizon.main import create_app

from .conftest import SAMPLES


class RecordingLLM:
    """요청을 기록하고, 받은 원문 블록을 모두 인용하는 결과를 돌려준다."""

    mode = "live"
    model = "fake"

    def __init__(self, translation: str = "translated", entries: list[dict] | None = None):
        self.translation = translation
        self.entries = entries or []
        self.calls: list[dict] = []

    def generate(self, system, docs, task, meta):
        self.calls.append({"system": system, "task": task, "meta": meta})
        work = docs[0]
        blocks = [RawBlock("## 결과\n")]
        for pos, block in enumerate(work.blocks):
            blocks.append(RawBlock("- "))
            blocks.append(RawBlock(self.translation, [RawCitation("content_block_location", block, 0, pos, pos + 1)]))
            blocks.append(RawBlock("\n"))
        return LLMResult(blocks, "end_turn", "fake")

    def complete_json(self, system, prompt, schema, meta=None):
        self.calls.append({"system": system, "task": prompt, "meta": meta})
        return JSONResult({"entries": self.entries}, "fake", usage={"input_tokens": 10, "output_tokens": 5})


def _paras(*texts: str) -> list[Paragraph]:
    return [Paragraph(i, f"{i + 1}문단", None, t) for i, t in enumerate(texts)]


def _ctx(*texts: str) -> WorkContext:
    cat, gen = find_genre("custom", "general")
    return WorkContext("글", None, cat, gen, _paras(*texts), None)


# ── 독자 설정 ─────────────────────────────────────────────
def test_reader_profile_normalizes_and_builds_suffix():
    assert ReaderProfile.of("", "ko").suffix == "" and ReaderProfile.of(None, None).is_default
    assert ReaderProfile.of("high", "ko").suffix == "@high.ko"
    assert ReaderProfile.of("", "en").suffix == "@-.en"
    assert ReaderProfile.of("teacher", "fr") == ReaderProfile()


def test_profile_lines_go_into_the_request_not_the_system_prompt():
    llm = RecordingLLM()
    engine = AnalysisEngine(llm, None, 200_000)
    result = engine.analyze(_ctx("첫 문단입니다."), "flow", "", ReaderProfile.of("elementary", "en"))
    call = llm.calls[0]
    assert "[독자] 초등학생 —" in call["task"] and "[설명 언어] English" in call["task"]
    assert call["system"] == engine.system_prompt  # 시스템 프롬프트는 설정과 관계없이 같다(캐시 유지)
    assert result["option"] == "@elementary.en" and result["profile"] == {"audience": "elementary", "lang": "en"}

    default = engine.analyze(_ctx("첫 문단입니다."), "flow")
    assert default["option"] == "" and "[독자]" not in llm.calls[1]["task"]


def test_multiple_choice_prompt_asks_for_step_by_step_judgement():
    task = prompts.ask_task("[작품 정보]", "① 가 ② 나")
    assert "발문이 '적절한 것'과 '적절하지 않은 것'" in task and "원문으로 확인할 수 없음" in task
    assert "'정답: 번호'" in task


# ── 모범 예시 ─────────────────────────────────────────────
def test_default_exemplars_are_in_the_system_prompt():
    exemplars = prompts.load_exemplars()
    assert len(exemplars) >= 3
    assert prompts.SYSTEM_PROMPT.startswith(prompts.BASE_SYSTEM_PROMPT)
    assert all(e["answer"].strip() in prompts.SYSTEM_PROMPT for e in exemplars)
    assert prompts.build_system_prompt([]) == prompts.BASE_SYSTEM_PROMPT


def test_exemplars_do_not_overlap_with_evaluation_passages():
    """평가 문항과 같은 원문을 예시로 쓰면 점수가 부풀려진다."""
    passages = []
    for name in ("quality_items.sample.json", "eval_items.sample.json"):
        passages += [item["passage"] for item in json.loads((SAMPLES / name).read_text(encoding="utf-8"))]
    evaluated = "\n".join(passages)
    for exemplar in prompts.load_exemplars():
        for line in exemplar["passage"].splitlines():
            text = line.split("] ", 1)[-1].strip()
            if len(text) >= 8 and not text.startswith("[질문]"):
                assert text not in evaluated, (exemplar["id"], text)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "읽을 수 없습니다"),
        ('{"items": []}', "exemplars"),
        ('{"exemplars": [{"task": "a"}]}', "1번째"),
        (json.dumps({"exemplars": [{"task": "a", "audience": "b", "source": "c", "passage": "가" * 20_001, "answer": "d"}]}), "너무 깁니다"),
    ],
)
def test_load_exemplars_rejects_bad_files(tmp_path, content, message):
    path = tmp_path / "ex.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(prompts.ExemplarError, match=message):
        prompts.load_exemplars(path)


def test_custom_exemplars_path_is_used_by_the_app(settings, store, tmp_path):
    from dataclasses import replace

    path = tmp_path / "teacher.json"
    path.write_text(
        json.dumps({"exemplars": [{"task": "작품 분석", "audience": "중학생", "source": "교사 작성", "passage": "[1행] 예시", "answer": "## 교사 예시 답"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    app = create_app(replace(settings, exemplars_path=path), llm=DemoClient(), store=store)
    assert "## 교사 예시 답" in app.state.engine.system_prompt
    assert "엄마야 누나야" not in app.state.engine.system_prompt


# ── 용어집 ────────────────────────────────────────────────
def test_normalize_and_merge_entries():
    raw = [
        {"term": "  윤동주 ", "rendering": "Yun Dong-ju", "note": "시인", "kind": "인명"},
        {"term": "윤동주", "rendering": "중복"},
        {"term": "", "rendering": "빈 용어"},
        {"term": "별", "rendering": "star", "kind": "이상한 분류", "source": "auto"},
        "문자열",
    ]
    entries = normalize_entries(raw, "user")
    assert [(e["term"], e["kind"], e["source"]) for e in entries] == [("윤동주", "인명", "user"), ("별", "기타", "auto")]
    merged = merge_entries(entries, normalize_entries([{"term": "별", "rendering": "다른 번역"}, {"term": "바람", "rendering": "wind"}], "auto"))
    assert [(e["term"], e["rendering"]) for e in merged] == [("윤동주", "Yun Dong-ju"), ("별", "star"), ("바람", "wind")]


def test_translation_uses_only_relevant_glossary_entries_and_checks_them():
    llm = RecordingLLM(translation="The stars and the wind")
    engine = AnalysisEngine(llm, None, 200_000)
    glossary = {"version": "v1", "entries": [
        {"term": "별", "rendering": "stars"}, {"term": "바람", "rendering": "breeze"}, {"term": "하늘", "rendering": "sky"},
    ]}
    result = engine.analyze(_ctx("별이 바람에 스치운다."), "translate", "en", ReaderProfile.of("", "en"), glossary)
    task = llm.calls[0]["task"]
    assert "[용어집] 다음 번역어를 그대로 씁니다.\n- 별 → stars\n- 바람 → breeze" in task and "하늘" not in task
    assert "## Vocabulary" in task
    assert result["glossary"] == {"version": "v1", "count": 3}
    item = next(i for i in result["checks"]["items"] if i["id"] == "glossary")
    assert item["status"] == "warn" and "바람 → breeze" in item["detail"]
    assert result["checks"]["glossary_missed"] == [{"term": "바람", "rendering": "breeze"}]


def test_english_vocabulary_section_is_excluded_from_translation_checks():
    segments = [
        {"text": "- [1문단] Stars", "citations": [{"source": "work", "paragraphs": [0], "verified": True}]},
        {"text": "\n## Vocabulary\n- 2020: a year", "citations": [{"source": "work", "paragraphs": [1], "verified": True}]},
    ]
    checks = translation_checks(segments, _paras("별", "바람"), "en")
    assert checks["missing_paragraphs"] == [{"index": 1, "label": "2문단"}] and checks["extra_numbers"] == []


def test_extract_glossary_drops_invented_and_known_terms():
    llm = RecordingLLM(entries=[
        {"term": "윤동주", "rendering": "Yun Dong-ju", "note": "", "kind": "인명"},
        {"term": "별", "rendering": "star", "note": "", "kind": "기타"},
        {"term": "원문에없는말", "rendering": "x", "note": "", "kind": "기타"},
    ])
    engine = AnalysisEngine(llm, None, 200_000, translate_part_chars=1000)
    ctx = _ctx("윤동주는 별을 노래했다.", "가" * 990)
    new, meta = engine.extract_glossary(ctx, "en", 1, [{"term": "별", "rendering": "stars"}])
    assert [e["term"] for e in new] == ["윤동주"] and new[0]["source"] == "auto"
    assert "[이미 정한 번역어]\n- 별 → stars" in llm.calls[0]["task"] and llm.calls[0]["meta"]["kind"] == "glossary"
    assert meta["usage"]["input_tokens"] == 10
    with pytest.raises(AnalysisRequestError, match="2부분"):
        engine.extract_glossary(ctx, "en", 3, [])
    with pytest.raises(AnalysisRequestError, match="번역 대상"):
        engine.extract_glossary(ctx, "fr", 1, [])


def test_demo_client_only_supports_glossary_json():
    with pytest.raises(LLMError, match="데모"):
        DemoClient().complete_json("s", "p", {}, None)
    data = DemoClient().complete_json("s", "p", {}, {"kind": "glossary", "paragraphs": ["죽는 날까지 하늘을 우러러"]}).data
    assert [e["term"] for e in data["entries"]] == ["죽는", "날까지", "하늘을"]


# ── API ───────────────────────────────────────────────────
def _work(client, headers, text):
    fields = {"category": "custom", "genre": "general", "title": "용어 테스트", "text": text}
    return client.post("/api/works", files={k: (None, v) for k, v in fields.items()}, headers=headers).json()["work"]["id"]


def test_api_profile_options_are_cached_separately(settings, store, workspace):
    llm = RecordingLLM()
    client = TestClient(create_app(settings, llm=llm, store=store))
    meta = client.get("/api/categories").json()
    assert meta["audiences"]["high"] == "고등학생·수험생" and meta["explain_languages"] == {"ko": "한국어", "en": "English"}
    work_id = _work(client, workspace, "첫 문단입니다.\n\n둘째 문단입니다.")

    plain = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview"}, headers=workspace).json()
    high = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview", "audience": "high"}, headers=workspace).json()
    assert plain["option"] == "" and high["option"] == "@high.ko" and high["cached"] is False
    again = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview", "audience": "high"}, headers=workspace).json()
    assert again["cached"] is True and len(llm.calls) == 2
    translated = client.post(
        f"/api/works/{work_id}/analyze", json={"kind": "translate", "option": "en:1", "lang": "en"}, headers=workspace
    ).json()
    assert translated["option"] == "en:1@-.en"

    answer = client.post(f"/api/works/{work_id}/ask", json={"question": "요지는?", "audience": "expert"}, headers=workspace).json()
    assert answer["result"]["profile"]["audience"] == "expert" and "[독자] 대학생·연구자" in llm.calls[-1]["task"]


def test_api_glossary_edit_extract_and_translate(settings, store, workspace):
    llm = RecordingLLM(translation="Yun Dong-ju sang", entries=[{"term": "노래", "rendering": "song", "note": "", "kind": "기타"}])
    client = TestClient(create_app(settings, llm=llm, store=store))
    work_id = _work(client, workspace, "윤동주는 별을 노래했다.")
    url = f"/api/works/{work_id}/glossary"

    assert client.get(url, params={"target": "en"}, headers=workspace).json() == {"target": "en", "entries": [], "version": None, "updated_at": None}
    assert client.get(url, params={"target": "fr"}, headers=workspace).status_code == 422
    assert client.get(url, params={"target": "en"}, headers={"X-Workspace-Id": "00000000-0000-4000-8000-000000000000"}).status_code == 404

    saved = client.put(url, params={"target": "en"}, json={"entries": [
        {"term": "윤동주", "rendering": "Yun Dong-ju", "note": "문의 test@example.com"}, {"term": "", "rendering": "버림"},
    ]}, headers=workspace).json()
    assert [e["term"] for e in saved["entries"]] == ["윤동주"] and "[마스킹:" in saved["entries"][0]["note"]
    notice = client.get("/api/privacy/notice", headers=workspace).json()
    assert any(m["category"] == "email" for m in notice["masking_detected"])

    extracted = client.post(f"{url}/extract", json={"target": "en", "part": 1}, headers=workspace).json()
    assert extracted["added"] == 1 and [e["term"] for e in extracted["entries"]] == ["윤동주", "노래"]
    assert extracted["version"] != saved["version"]

    result = client.post(f"/api/works/{work_id}/analyze", json={"kind": "translate", "option": "en:1"}, headers=workspace).json()
    assert result["glossary"] == {"version": extracted["version"], "count": 2}
    assert "- 윤동주 → Yun Dong-ju" in llm.calls[-1]["task"]
    item = next(i for i in result["checks"]["items"] if i["id"] == "glossary")
    assert item["status"] == "warn" and "노래 → song" in item["detail"]

    # 다른 번역 대상의 용어집은 따로
    assert client.get(url, params={"target": "modern"}, headers=workspace).json()["entries"] == []
    too_many = client.put(url, params={"target": "en"}, json={"entries": [{"term": f"t{i}", "rendering": "r"} for i in range(201)]}, headers=workspace)
    assert too_many.status_code == 422
