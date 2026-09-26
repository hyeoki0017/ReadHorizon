"""번역·풀이 결과 자동 검사(코드 규칙, 모델 호출 없음).

인용 대조(grounding.py)는 "인용문이 원문에 있는가"만 본다. 여기서는 번역이 원문을 빠짐없이, 숫자를 바꾸지 않고
옮겼는지를 확인한다. 모두 '확인 필요' 신호이며 오류 판정이 아니다.

1) 문단 누락: 번역 본문(어휘 풀이 앞까지)의 원문 인용이 한 번도 가리키지 않은 문단
2) 숫자: 원문의 두 자리 이상 숫자·소수가 번역에 없거나, 원문에 없는 숫자가 번역에 새로 나타남
   (한 자리 숫자는 'three'·'셋'처럼 풀어 쓰는 일이 많아 보지 않는다)
3) 분량: 현대어 풀이·쉬운 말 풀이가 원문보다 지나치게 짧음(일부를 건너뛰었을 가능성)
4) 용어집: 이 부분 원문에 나온 용어의 번역어가 번역에 없음(영어·한국어 번역만. 풀이는 활용형이 바뀌어 보지 않는다)
"""

from __future__ import annotations

import re

from ..ingest.normalize import Paragraph
from .grounding import squash

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
# 위치 표시([3문단], [2연 1행], 3문단)와 가림·판독 표시 속 숫자는 비교에서 뺀다
POSITION_RE = re.compile(r"\[[^\]\n]{1,20}\]|\d+\s*(?:문단|연|행)")
VOCAB_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(?:어휘\s*풀이|vocabulary)", re.MULTILINE | re.IGNORECASE)
HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s.*$", re.MULTILINE)
# 같은 언어 안의 풀이에만 분량 비율을 본다(언어가 바뀌면 글자 수를 비교할 수 없음)
MIN_LENGTH_RATIO = {"modern": 0.4, "easy": 0.3}
GLOSSARY_CHECK_TARGETS = {"en", "ko"}
MAX_LISTED = 12


def numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in NUMBER_RE.finditer(POSITION_RE.sub(" ", text or "")):
        token = match.group(0).replace(",", "")
        if "." in token or len(token) >= 2:
            found.append(token)
    return found


def translation_segments(segments: list[dict]) -> list[dict]:
    """'어휘 풀이' 제목 앞까지의 조각. 어휘 풀이의 인용은 번역 범위 판단에 쓰지 않는다."""
    body: list[dict] = []
    for seg in segments:
        text = seg.get("text") or ""
        heading = VOCAB_HEADING_RE.search(text)
        if heading:
            if text[: heading.start()].strip():
                body.append({"text": text[: heading.start()], "citations": []})
            break
        body.append(seg)
    return body


def _listed(values: list[str]) -> str:
    shown = ", ".join(values[:MAX_LISTED])
    return shown + (f" 외 {len(values) - MAX_LISTED}개" if len(values) > MAX_LISTED else "")


def _fold(text: str) -> str:
    return squash(text).casefold()


def translation_checks(
    segments: list[dict], paragraphs: list[Paragraph], target: str, glossary: list[dict] | None = None
) -> dict:
    body = translation_segments(segments)
    text = "".join(seg.get("text") or "" for seg in body)

    cited = {
        index
        for seg in body
        for c in seg.get("citations") or []
        if c.get("source") == "work"
        for index in c.get("paragraphs") or []
    }
    missing = [p for p in paragraphs if p.index not in cited]
    items = [
        {
            "id": "coverage",
            "status": "warn" if missing else "ok",
            "label": "문단 누락",
            "detail": f"옮기지 않은 것으로 보이는 부분: {_listed([p.label for p in missing])}"
            if missing
            else f"모든 부분({len(paragraphs)}개)에 원문 인용이 붙었습니다",
        }
    ]

    out_numbers = set(numbers(text))
    source_numbers: set[str] = set()
    missing_numbers: list[dict] = []
    for p in paragraphs:
        for value in numbers(p.text):
            if value not in out_numbers and value not in source_numbers:
                missing_numbers.append({"value": value, "label": p.label, "index": p.index})
            source_numbers.add(value)
    extra_numbers = sorted(out_numbers - source_numbers)
    if missing_numbers or extra_numbers:
        parts = []
        if missing_numbers:
            parts.append("번역에 없는 숫자: " + _listed([f"{m['value']}({m['label']})" for m in missing_numbers]))
        if extra_numbers:
            parts.append("원문에 없는 숫자: " + _listed(extra_numbers))
        items.append({"id": "numbers", "status": "warn", "label": "숫자", "detail": " · ".join(parts)})
    elif source_numbers:
        items.append({"id": "numbers", "status": "ok", "label": "숫자", "detail": f"원문 숫자 {len(source_numbers)}개가 모두 있습니다"})

    ratio = None
    if target in MIN_LENGTH_RATIO:
        source_chars = sum(len(squash(p.text)) for p in paragraphs)
        out_chars = len(squash(POSITION_RE.sub("", HEADING_LINE_RE.sub("", text))))
        if source_chars:
            ratio = round(out_chars / source_chars, 2)
            short = ratio < MIN_LENGTH_RATIO[target]
            items.append(
                {
                    "id": "length",
                    "status": "warn" if short else "ok",
                    "label": "분량",
                    "detail": f"풀이 분량이 원문의 {round(ratio * 100)}%입니다"
                    + (" — 일부를 건너뛰었을 수 있습니다" if short else ""),
                }
            )

    glossary_missed: list[dict] = []
    if glossary and target in GLOSSARY_CHECK_TARGETS:
        source = "".join(p.text for p in paragraphs)
        used = [e for e in glossary if e.get("term") and e.get("rendering") and e["term"] in source]
        if used:
            folded = _fold(text)
            glossary_missed = [{"term": e["term"], "rendering": e["rendering"]} for e in used if _fold(e["rendering"]) not in folded]
            items.append(
                {
                    "id": "glossary",
                    "status": "warn" if glossary_missed else "ok",
                    "label": "용어집",
                    "detail": "용어집과 다르게 옮긴 것으로 보이는 용어: "
                    + _listed([f"{m['term']} → {m['rendering']}" for m in glossary_missed])
                    if glossary_missed
                    else f"이 부분에 나온 용어 {len(used)}개를 용어집대로 옮겼습니다",
                }
            )

    return {
        "ok": all(item["status"] == "ok" for item in items),
        "glossary_missed": glossary_missed,
        "items": items,
        "missing_paragraphs": [{"index": p.index, "label": p.label} for p in missing],
        "missing_numbers": missing_numbers,
        "extra_numbers": extra_numbers,
        "length_ratio": ratio,
    }
