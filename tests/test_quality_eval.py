"""번역·해설 품질 평가: 문항 검증, 채점 결과 정리, 요약·비교, 명령줄(데모)."""

from __future__ import annotations

import json

import pytest

from readhorizon.analysis.engine import AnalysisEngine
from readhorizon.analysis.llm import JSONResult, LLMResult, RawBlock, RawCitation
from readhorizon.quality_eval import (
    JUDGE_SCHEMA,
    JUDGE_SYSTEM,
    QualityEvalError,
    compare,
    judge_prompt,
    load_items,
    run_quality_eval,
    score_judgement,
)

from .conftest import SAMPLES


class CitingLLM:
    """모든 블록을 인용하는 결과를 돌려준다(번역이면 빠짐없이 옮긴 것처럼)."""

    mode = "live"
    model = "gen-model"

    def generate(self, system, docs, task, meta):
        work = docs[0]
        blocks = [RawBlock("## 결과\n")]
        for pos, block in enumerate(work.blocks):
            blocks.append(RawBlock("- "))
            blocks.append(RawBlock(f"풀이 {pos + 1}", [RawCitation("content_block_location", block, 0, pos, pos + 1)]))
            blocks.append(RawBlock("\n"))
        return LLMResult(blocks, "end_turn", "gen-model", usage={"input_tokens": 100, "output_tokens": 50})


class FakeJudge:
    model = "judge-model"

    def __init__(self):
        self.prompts = []

    def complete_json(self, system, prompt, schema):
        assert system == JUDGE_SYSTEM and schema is JUDGE_SCHEMA
        self.prompts.append(prompt)
        data = {
            "key_points": [{"index": 1, "verdict": "yes", "evidence": "풀이 1"}, {"index": 2, "verdict": "partial", "evidence": ""}],
            "errors": [
                {"category": "오역", "severity": "major", "location": "1연 2행", "explanation": "소망을 서술로 옮김"},
                {"category": "어색함", "severity": "minor", "location": "2연 1행", "explanation": "어순"},
            ],
            "summary": "대체로 정확함",
        }
        return JSONResult(data, "judge-model", usage={"input_tokens": 300, "output_tokens": 80})


def _write(tmp_path, items):
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def _item(**over):
    base = {"id": "a", "title": "서시", "category": "literature", "genre": "poem", "passage": "하늘을 우러러\n부끄럼 없기를\n별을 노래하는 마음", "kind": "overview", "key_points": ["소망", "성찰", "다짐"]}
    return {**base, **over}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"key_points": []}, "key_points"),
        ({"key_points": ["", "b"]}, "key_points"),
        ({"kind": "summary"}, "kind"),
        ({"kind": "translate", "option": "fr"}, "번역 대상"),
        ({"passage": "  "}, "passage"),
        ({"genre": "unknown"}, "장르"),
    ],
)
def test_load_items_rejects_invalid(tmp_path, override, message):
    with pytest.raises(QualityEvalError, match=message):
        load_items(_write(tmp_path, [_item(**override)]))


def test_sample_items_are_valid():
    items = load_items(SAMPLES / "quality_items.sample.json")
    assert {i["kind"] for i in items} == {"overview", "translate", "flow"}


def test_score_judgement_defaults_missing_points_to_no_and_ignores_bad_entries():
    item = _item()
    data = {
        "key_points": [{"index": 1, "verdict": "yes", "evidence": "x"}, {"index": 9, "verdict": "yes", "evidence": ""}, {"index": 1, "verdict": "no", "evidence": ""}],
        "errors": [{"category": "누락", "severity": "major", "location": "", "explanation": ""}, {"category": "누락", "severity": "fatal"}],
        "summary": "",
    }
    scored = score_judgement(item, data)
    assert [p["verdict"] for p in scored["key_points"]] == ["yes", "no", "no"]
    assert scored["coverage"] == 0.333 and scored["error_points"] == 5 and scored["error_counts"] == {"누락": 1}


def test_run_quality_eval_combines_judge_and_automatic_checks(tmp_path, seosi_text):
    items = [
        _item(id="ov", passage=seosi_text, key_points=["소망", "성찰"], reference="모범 해설", audience="고등학생"),
        _item(id="tr", passage=seosi_text, kind="translate", option="en", key_points=["소망", "바람"]),
    ]
    judge = FakeJudge()
    report = run_quality_eval(AnalysisEngine(CitingLLM(), None, 200_000), judge, items)

    first = report["items"][0]
    assert first["coverage"] == 0.75 and first["major_errors"] == 1 and first["error_points"] == 6
    assert first["checks"] is None and report["items"][1]["checks"]["ok"] is True
    assert report["items"][1]["option"] == "en:1"
    summary = report["summary"]
    assert summary["key_point_coverage"] == 0.75 and summary["error_points_per_item"] == 6
    assert summary["translation_checks_passed"] == "1/1" and set(summary["by_kind"]) == {"overview", "translate"}
    assert report["usage"]["generation"]["input_tokens"] == 200 and report["usage"]["judge"]["output_tokens"] == 160
    assert report["judge_model"] == "judge-model" and report["generator_model"] == "gen-model"

    prompt = judge.prompts[0]
    assert "<원문>\n[1연 1행] 죽는 날까지 하늘을 우러러" in prompt
    assert "풀이 1 [근거: 1연 1행]" in prompt
    assert "<모범 답안>\n모범 해설\n</모범 답안>" in prompt and "[대상 독자] 고등학생" in prompt
    assert "1. 소망\n2. 성찰" in prompt
    assert "영어 번역" in judge.prompts[1]


def test_long_translation_items_are_rejected_before_any_call(tmp_path):
    class NoCallLLM(CitingLLM):
        def generate(self, *args):
            raise AssertionError("API를 호출하면 안 됩니다")

    items = [_item(), _item(id="long", kind="translate", option="en", passage="\n\n".join(["가" * 900] * 3))]
    with pytest.raises(QualityEvalError, match="long"):
        run_quality_eval(AnalysisEngine(NoCallLLM(), None, 200_000, translate_part_chars=1000), None, items)


def test_compare_reports_changes():
    delta = compare({"key_point_coverage": 0.8, "error_points_per_item": 2.0, "major_errors": 1}, {"key_point_coverage": 0.6, "error_points_per_item": 3.5})
    assert delta == {
        "key_point_coverage": {"before": 0.6, "now": 0.8, "change": 0.2},
        "error_points_per_item": {"before": 3.5, "now": 2.0, "change": -1.5},
    }


def test_judge_prompt_marks_translation_target(seosi_text):
    from readhorizon.quality_eval import _context

    item = _item(passage=seosi_text, kind="translate", option="modern")
    ctx = _context(item)
    prompt = judge_prompt(item, ctx, {"segments": [{"text": "풀이", "citations": []}]})
    assert "[과제] 번역·풀이 — 현대어 풀이" in prompt and "<평가 대상>\n풀이\n</평가 대상>" in prompt


def test_cli_eval_quality_in_demo_mode(tmp_path, monkeypatch, capsys):
    from readhorizon.cli import main

    monkeypatch.chdir(tmp_path)  # 저장소의 .env를 읽지 않도록
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RH_LLM_MODE", "demo")
    monkeypatch.setenv("RH_DB_PATH", str(tmp_path / "db.sqlite"))
    out = tmp_path / "report.json"

    assert main(["eval-quality", str(SAMPLES / "quality_items.sample.json"), "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["generator_mode"] == "demo" and report["judge_model"] is None
    assert report["summary"]["judged"] == 0 and report["summary"]["translation_checks_passed"] == "2/2"
    assert "AI 채점은 건너뛰고" in capsys.readouterr().err

    assert main(["eval-quality", str(SAMPLES / "quality_items.sample.json"), "--baseline", str(out)]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["baseline_delta"]["mean_grounding_ratio"]["change"] == 0
