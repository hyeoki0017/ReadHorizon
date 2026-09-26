"""텍스트 → 공통 문서 구조(섹션 > 문단).

- 산문: 문단 단위. PDF처럼 줄이 강제로 끊긴 텍스트는 문장 끝이 아닌 줄을 다음 줄과 잇는다.
- 운문(시·시조 등): 행 단위로 나누고 '1연 2행'처럼 번호를 붙인다. 원문 인용을 행까지 정밀하게 하기 위해서다.
- 마크다운 헤딩('# 제목')은 섹션 제목으로 쓰고 문단에는 넣지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
# 문장이 끝난 줄로 보는 마지막 글자(따옴표·말줄임표 포함)
TERMINAL_CHARS = ".?!。…\"'”’」』)]"
INVISIBLE_RE = re.compile(r"[​‌‍﻿]")


@dataclass
class Paragraph:
    index: int
    label: str
    section: str | None
    text: str


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    return INVISIBLE_RE.sub("", text)


def _content_lines(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip() and not HEADING_RE.match(line)]


def looks_like_verse(text: str) -> bool:
    """짧은 행이 대부분이면 운문으로 본다(자동 판별용 휴리스틱)."""
    lines = _content_lines(clean_text(text))
    if len(lines) < 3:
        return False
    short = sum(1 for line in lines if len(line) <= 30)
    return short / len(lines) >= 0.8 and max(len(line) for line in lines) <= 60


def _blocks(text: str) -> list[tuple[str | None, list[str]]]:
    """빈 줄로 구분된 블록과 그 블록이 속한 섹션 제목."""
    blocks: list[tuple[str | None, list[str]]] = []
    section: str | None = None
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            blocks.append((section, current))
            current = []

    for line in text.split("\n"):
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            section = heading.group(1).strip()
            continue
        if not line.strip():
            flush()
            continue
        current.append(line.strip())
    flush()
    return blocks


def _join_prose_lines(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        buffer.append(line)
        if line[-1] in TERMINAL_CHARS:
            paragraphs.append(" ".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))
    return [re.sub(r"\s+", " ", p).strip() for p in paragraphs if p.strip()]


def normalize_text(text: str, verse: bool) -> list[Paragraph]:
    blocks = _blocks(clean_text(text))
    paragraphs: list[Paragraph] = []

    if verse:
        stanza_counts: dict[str | None, int] = {}
        per_section_blocks: dict[str | None, int] = {}
        for section, _ in blocks:
            per_section_blocks[section] = per_section_blocks.get(section, 0) + 1
        for section, lines in blocks:
            stanza = stanza_counts.get(section, 0) + 1
            stanza_counts[section] = stanza
            single_stanza = per_section_blocks[section] == 1
            for line_no, line in enumerate(lines, start=1):
                label = f"{line_no}행" if single_stanza else f"{stanza}연 {line_no}행"
                paragraphs.append(Paragraph(len(paragraphs), label, section, line))
        return paragraphs

    for section, lines in blocks:
        for text_para in _join_prose_lines(lines):
            paragraphs.append(Paragraph(len(paragraphs), f"{len(paragraphs) + 1}문단", section, text_para))
    return paragraphs


def total_chars(paragraphs: list[Paragraph]) -> int:
    return sum(len(p.text) for p in paragraphs)
