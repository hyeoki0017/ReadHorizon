from __future__ import annotations

import json

import pytest

from readhorizon.analysis.engine import AnalysisEngine
from readhorizon.analysis.llm import LLMResult, RawBlock, RawCitation
from readhorizon.evaluation import BenchmarkError, load_items, parse_answer, run_benchmark

from .conftest import SAMPLES


@pytest.mark.parametrize(
    ("text", "expected"),
    [("…따라서 정답: 2", 2), ("정답 : ③", 3), ("정답: 1\n수정 — 정답: 4", 4), ("모르겠습니다", None)],
)
def test_parse_answer(text, expected):
    assert parse_answer(text) == expected


class AnswerLLM:
    mode = "live"
    model = "fake"

    def __init__(self, answers):
        self.answers = list(answers)

    def generate(self, system, docs, task, meta):
        assert meta["multiple_choice"] is True and "1. " in task and "5. " in task
        work = docs[0]
        return LLMResult(
            [RawBlock("근거", [RawCitation("content_block_location", work.blocks[0], 0, 0, 1)]),
             RawBlock(f"\n정답: {self.answers.pop(0)}")],
            "end_turn",
            "fake",
        )


def test_run_benchmark_on_sample_items():
    items = load_items(SAMPLES / "eval_items.sample.json")
    report = run_benchmark(AnalysisEngine(AnswerLLM([2, 3]), None, 200_000), items)
    assert report["total"] == 2 and report["answered"] == 2
    assert report["correct"] == 1 and report["accuracy"] == 0.5
    assert report["items"][0]["correct"] is True and report["items"][1]["predicted"] == 3


def test_load_items_validation(tmp_path):
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"id": 1, "passage": "p", "question": "q", "choices": ["a"], "answer": 1}]))
    with pytest.raises(BenchmarkError, match="5개"):
        load_items(path)
