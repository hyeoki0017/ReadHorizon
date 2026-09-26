"""OCR 정확도 평가: 글자 오류율(CER, Character Error Rate).

CER = 편집 거리(추출 결과, 정답) ÷ 정답 글자 수. 공백·줄바꿈은 빼고 NFC로 맞춘 뒤 비교한다.
[?] 표시는 지우고, [?추정] 표시는 추정 글자만 남겨 비교한다(판독 불가 글자는 오류로 센다).

평가 목록(JSON 배열) 형식 — 경로는 목록 파일 기준 상대 경로:
  {"id": "...", "files": ["1쪽.jpg", "2쪽.jpg"] 또는 ["스캔.pdf"],
   "truth": "정답.txt" 또는 "truth_text": "정답 문자열",
   "scope": "all"|"passage", "verse": true|false, "note": "옛한글 등 메모"}

옛한글·한자 판독 품질은 실제 스캔본과 정답 텍스트를 모아 이 도구로 측정한다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .analysis.llm import LLMClient
from .ocr import OCRInputError, prepare_inputs, run_ocr

MAX_COMPARE_CHARS = 6000
_GUESS_RE = re.compile(r"\[\?([^\[\]\n]{0,10})\]")
_SPACE_RE = re.compile(r"\s+")


class OCRBenchmarkError(ValueError):
    pass


def normalize_for_cer(text: str) -> str:
    text = _GUESS_RE.sub(lambda m: m.group(1), unicodedata.normalize("NFC", text or ""))
    return _SPACE_RE.sub("", text)


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def cer(prediction: str, truth: str) -> float:
    pred, gold = normalize_for_cer(prediction), normalize_for_cer(truth)
    if not gold:
        raise OCRBenchmarkError("정답 텍스트가 비어 있습니다.")
    if len(pred) > MAX_COMPARE_CHARS or len(gold) > MAX_COMPARE_CHARS:
        raise OCRBenchmarkError(f"비교할 텍스트가 너무 깁니다. 항목을 {MAX_COMPARE_CHARS}자 이하(한두 쪽)로 나눠 주세요.")
    return edit_distance(pred, gold) / len(gold)


def load_manifest(path: Path) -> list[dict]:
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OCRBenchmarkError(f"평가 목록을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(items, list) or not items:
        raise OCRBenchmarkError("평가 목록은 비어 있지 않은 JSON 배열이어야 합니다.")
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict) or "id" not in item or not item.get("files"):
            raise OCRBenchmarkError(f"{i}번째 항목에 id와 files가 필요합니다.")
        if "truth" not in item and "truth_text" not in item:
            raise OCRBenchmarkError(f"{i}번째 항목에 truth(정답 파일) 또는 truth_text가 필요합니다.")
    return items


def run_ocr_benchmark(llm: LLMClient, items: list[dict], base_dir: Path, max_images: int, max_pages: int) -> dict:
    rows = []
    for item in items:
        try:
            files = [(name, (base_dir / name).read_bytes()) for name in item["files"]]
            truth = item.get("truth_text") or (base_dir / item["truth"]).read_text(encoding="utf-8")
        except OSError as exc:
            raise OCRBenchmarkError(f"항목 {item['id']}: 파일을 읽을 수 없습니다: {exc}") from exc
        try:
            inputs = prepare_inputs(files, max_images, max_pages)
        except OCRInputError as exc:
            raise OCRBenchmarkError(f"항목 {item['id']}: {exc}") from exc
        result = run_ocr(llm, inputs, item.get("scope", "all"), bool(item.get("verse")))
        rows.append(
            {
                "id": item["id"],
                "note": item.get("note", ""),
                "cer": round(cer(result["text"], truth), 4),
                "uncertain_count": result["uncertain_count"],
                "truth_chars": len(normalize_for_cer(truth)),
            }
        )
    total = len(rows)
    return {
        "total": total,
        "mean_cer": round(sum(r["cer"] for r in rows) / total, 4) if total else 0.0,
        "items": rows,
    }
