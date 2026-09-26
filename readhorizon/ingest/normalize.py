"""텍스트 → 공통 문서 구조(섹션 > 문단).

- 모든 입력은 유니코드 NFC로 정규화한다(macOS 등에서 오는 NFD 한글을 완성형으로 맞춰 검색·대조가 어긋나지 않게).
  옛한글처럼 완성형이 없는 자모 조합은 NFC에서도 그대로 남는다.
- 산문: 문단 단위. PDF처럼 줄이 강제로 끊긴 텍스트는 문장 끝이 아닌 줄을 다음 줄과 잇는다.
- 운문(시·시조 등): 행 단위로 나누고 '1연 2행'처럼 번호를 붙인다. 원문 인용을 행까지 정밀하게 하기 위해서다.
- 마크다운 헤딩('# 제목')은 섹션 제목으로 쓰고 문단에는 넣지 않는다.
- 너무 긴 문단은 문장 경계에서 나눈다(인용 정밀도와 긴 작품 발췌를 위해).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
# 문장이 끝난 줄로 보는 마지막 글자(따옴표·말줄임표 포함)
TERMINAL_CHARS = ".?!。…\"'”’」』)]"
INVISIBLE_RE = re.compile(
    "[\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}\N{ZERO WIDTH NO-BREAK SPACE}]"
)
# 줄바꿈·탭을 뺀 제어 문자
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SENTENCE_END_RE = re.compile(r"(?<=[.?!。…])\s+")
MAX_PARAGRAPH_CHARS = 2000


@dataclass
class Paragraph:
    index: int
    label: str
    section: str | None
    text: str


def clean_text(text: str | None) -> str:
    """여러 줄 텍스트 정리: NFC, 줄바꿈 통일, 보이지 않는 문자·제어 문자 제거."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\N{NO-BREAK SPACE}", " ")
    return CONTROL_RE.sub("", INVISIBLE_RE.sub("", text))


def clean_line(text: str | None) -> str:
    """한 줄 입력(제목·저자 등) 정리: clean_text + 공백 하나로 합치기."""
    return re.sub(r"\s+", " ", clean_text(text)).strip()


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


def split_long(text: str, limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    """limit보다 긴 문단을 문장 경계에서 나눈다. 한 문장이 limit보다 길면 글자 수로 자른다."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in SENTENCE_END_RE.split(text):
        while len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return [p.strip() for p in pieces if p.strip()]


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
                for piece in split_long(line):
                    paragraphs.append(Paragraph(len(paragraphs), label, section, piece))
        return paragraphs

    for section, lines in blocks:
        for joined in _join_prose_lines(lines):
            for piece in split_long(joined):
                paragraphs.append(Paragraph(len(paragraphs), f"{len(paragraphs) + 1}문단", section, piece))
    return paragraphs


def total_chars(paragraphs: list[Paragraph]) -> int:
    return sum(len(p.text) for p in paragraphs)
