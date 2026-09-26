"""정답률 벤치마크(수능형 선다형 문항).

문항 파일(JSON 배열) 형식:
  {"id", "title", "author"(선택), "category"(기본 study), "genre"(기본 csat_literature),
   "passage", "question", "choices": [5개], "answer": 1~5, "source": "출처·이용 조건"}

실제 기출 문항은 저작권·이용 조건을 확인한 뒤 따로 준비한다(저장소에는 포함하지 않음).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .analysis.engine import AnalysisEngine, WorkContext
from .analysis.presets import find_genre
from .ingest.normalize import looks_like_verse, normalize_text

CIRCLED = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5}
ANSWER_RE = re.compile(r"정답\s*[:：]\s*([1-5①-⑤])")


class BenchmarkError(ValueError):
    pass


def parse_answer(text: str) -> int | None:
    matches = ANSWER_RE.findall(text or "")
    if not matches:
        return None
    last = matches[-1]
    return CIRCLED.get(last) or int(last)


def load_items(path: Path) -> list[dict]:
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"문항 파일을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(items, list) or not items:
        raise BenchmarkError("문항 파일은 비어 있지 않은 JSON 배열이어야 합니다.")
    for i, item in enumerate(items):
        missing = [k for k in ("id", "passage", "question", "choices", "answer") if k not in item]
        if missing:
            raise BenchmarkError(f"{i + 1}번째 문항에 {', '.join(missing)} 항목이 없습니다.")
        if not isinstance(item["choices"], list) or len(item["choices"]) != 5:
            raise BenchmarkError(f"{i + 1}번째 문항의 선택지는 5개여야 합니다.")
        if item["answer"] not in (1, 2, 3, 4, 5):
            raise BenchmarkError(f"{i + 1}번째 문항의 정답은 1~5 중 하나여야 합니다.")
    return items


def run_benchmark(engine: AnalysisEngine, items: list[dict]) -> dict:
    rows = []
    for item in items:
        found = find_genre(item.get("category", "study"), item.get("genre", "csat_literature"))
        if found is None:
            raise BenchmarkError(f"문항 {item['id']}: 카테고리·장르가 올바르지 않습니다.")
        category, genre = found
        verse = genre.verse == "yes" or (genre.verse == "auto" and looks_like_verse(item["passage"]))
        ctx = WorkContext(
            item.get("title") or str(item["id"]),
            item.get("author"),
            category,
            genre,
            normalize_text(item["passage"], verse),
            None,
        )
        question = item["question"] + "\n" + "\n".join(f"{n}. {c}" for n, c in enumerate(item["choices"], start=1))
        result = engine.ask(ctx, question, multiple_choice=True)
        answer_text = "".join(s["text"] for s in result["segments"])
        predicted = parse_answer(answer_text)
        rows.append(
            {
                "id": item["id"],
                "expected": item["answer"],
                "predicted": predicted,
                "correct": predicted == item["answer"],
                "grounding_ratio": result["summary"]["ratio"],
                "unverified_citations": result["summary"]["unverified_count"],
            }
        )
    total = len(rows)
    answered = sum(1 for r in rows if r["predicted"] is not None)
    correct = sum(1 for r in rows if r["correct"])
    return {
        "total": total,
        "answered": answered,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "mean_grounding_ratio": round(sum(r["grounding_ratio"] for r in rows) / total, 3) if total else 0.0,
        "items": rows,
    }
