"""업로드 파일 → 텍스트.

지원: .txt .md .pdf .docx .hwpx
- 제목(헤딩)은 마크다운 형식('# 제목')으로 바꿔 두어, 정규화 단계에서 섹션으로 인식한다.
- .hwp(한글 5.0 바이너리)와 스캔 PDF(텍스트 없음)는 아직 지원하지 않으며, 대안을 안내한다.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from xml.etree import ElementTree

SUPPORTED_EXTENSIONS = (".txt", ".md", ".pdf", ".docx", ".hwpx")
MAX_HWPX_UNPACKED_BYTES = 64 * 1024 * 1024


class ParseError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 한국어 메시지를 담는다."""


@dataclass
class ParsedText:
    text: str
    title_hint: str | None


def decode_text(data: bytes) -> str:
    """UTF-8(BOM 포함) 우선, 실패하면 국내 윈도 파일에 흔한 CP949로 해석한다."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("텍스트 인코딩을 인식할 수 없습니다. UTF-8로 저장한 뒤 다시 올려 주세요.")


def parse_upload(filename: str, data: bytes) -> ParsedText:
    suffix = PurePath(filename).suffix.lower()
    stem = PurePath(filename).stem or None

    if suffix in (".txt", ".md"):
        return ParsedText(decode_text(data), stem)
    if suffix == ".pdf":
        return ParsedText(_parse_pdf(data), stem)
    if suffix == ".docx":
        return ParsedText(_parse_docx(data), stem)
    if suffix == ".hwpx":
        return ParsedText(_parse_hwpx(data), stem)
    if suffix == ".hwp":
        raise ParseError(
            "HWP(한글 5.0) 파일은 아직 지원하지 않습니다. 한글에서 '다른 이름으로 저장 → HWPX' "
            "또는 PDF로 저장한 뒤 올려 주세요."
        )
    raise ParseError(
        f"지원하지 않는 파일 형식입니다({suffix or '확장자 없음'}). "
        f"지원 형식: {', '.join(SUPPORTED_EXTENSIONS)}"
    )


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ParseError("암호가 걸린 PDF는 읽을 수 없습니다. 암호를 해제한 뒤 올려 주세요.")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except PdfReadError as exc:
        raise ParseError("PDF 파일을 읽을 수 없습니다. 파일이 손상되지 않았는지 확인해 주세요.") from exc

    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        raise ParseError(
            "PDF에서 텍스트를 찾지 못했습니다. 스캔본(이미지) PDF로 보입니다. "
            "이미지 인식(OCR)은 다음 단계에서 지원할 예정입니다."
        )
    return text


def _parse_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx는 손상 파일에 다양한 예외를 던진다
        raise ParseError("DOCX 파일을 읽을 수 없습니다. 파일이 손상되지 않았는지 확인해 주세요.") from exc

    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            lines.append("")
            continue
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style.lower().startswith("heading") or style.startswith("제목"):
            lines.extend(["", f"# {text}", ""])
        else:
            lines.extend([text, ""])
    return "\n".join(lines)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _hwpx_paragraphs(root: ElementTree.Element) -> list[str]:
    """<p> 단위로 <t> 텍스트를 모은다. 표 안의 문단처럼 중첩된 <p>는 따로 센다."""
    paragraphs: list[str] = []

    def walk(element: ElementTree.Element, buffer: list[str]) -> None:
        name = _local_name(element.tag)
        if name == "p":
            own: list[str] = []
            for child in element:
                walk(child, own)
            text = "".join(own).strip()
            if text:
                paragraphs.append(text)
            return
        if name == "t":
            if element.text:
                buffer.append(element.text)
            for child in element:  # <tab/> 같은 인라인 요소 뒤의 텍스트
                if _local_name(child.tag) == "tab":
                    buffer.append(" ")
                if child.tail:
                    buffer.append(child.tail)
            return
        for child in element:
            walk(child, buffer)

    walk(root, [])
    return paragraphs


def _parse_hwpx(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError("HWPX 파일을 열 수 없습니다. 파일이 손상되지 않았는지 확인해 주세요.") from exc

    sections = []
    for name in archive.namelist():
        match = re.fullmatch(r"Contents/section(\d+)\.xml", name)
        if match:
            sections.append((int(match.group(1)), name))
    if not sections:
        raise ParseError("HWPX 파일에서 본문을 찾지 못했습니다.")

    # 압축 폭탄 방지: 본문 XML의 압축 해제 크기 합계를 제한한다.
    unpacked = sum(archive.getinfo(name).file_size for _, name in sections)
    if unpacked > MAX_HWPX_UNPACKED_BYTES:
        raise ParseError("HWPX 본문이 너무 큽니다. 파일을 나눠서 올려 주세요.")

    paragraphs: list[str] = []
    for _, name in sorted(sections):
        try:
            root = ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError as exc:
            raise ParseError("HWPX 본문 구조를 해석할 수 없습니다.") from exc
        paragraphs.extend(_hwpx_paragraphs(root))

    text = "\n\n".join(paragraphs)
    if not text.strip():
        raise ParseError("HWPX 파일에 텍스트가 없습니다.")
    return text
