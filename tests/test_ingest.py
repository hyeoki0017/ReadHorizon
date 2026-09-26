from __future__ import annotations

import io
import zipfile

import docx
import pytest

from readhorizon.ingest.normalize import looks_like_verse, normalize_text
from readhorizon.ingest.parsers import ParseError, decode_text, parse_upload


def _minimal_pdf(text: str) -> bytes:
    """외부 도구 없이 텍스트 한 줄짜리 PDF를 만든다(ASCII 전용)."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def _hwpx(paragraphs: list[str], nested: str | None = None) -> bytes:
    ns = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"'
    body = "".join(f"<hp:p><hp:run><hp:t>{p}</hp:t></hp:run></hp:p>" for p in paragraphs)
    if nested:
        body += (
            "<hp:p><hp:run><hp:t>표 앞</hp:t><hp:tbl><hp:tr><hp:tc><hp:subList>"
            f"<hp:p><hp:run><hp:t>{nested}</hp:t></hp:run></hp:p>"
            "</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>"
        )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><hs:sec {ns}>{body}</hs:sec>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", xml)
    return buffer.getvalue()


def test_decode_text_utf8_and_cp949():
    assert decode_text("가나다".encode("utf-8-sig")) == "가나다"
    assert decode_text("한글 파일".encode("cp949")) == "한글 파일"


def test_parse_txt_uses_filename_as_title():
    parsed = parse_upload("서시.txt", "죽는 날까지".encode())
    assert parsed.text == "죽는 날까지"
    assert parsed.title_hint == "서시"


def test_parse_docx_marks_headings():
    document = docx.Document()
    document.add_heading("제1장", level=1)
    document.add_paragraph("첫 문단입니다.")
    buffer = io.BytesIO()
    document.save(buffer)
    parsed = parse_upload("a.docx", buffer.getvalue())
    paragraphs = normalize_text(parsed.text, verse=False)
    assert [(p.section, p.text) for p in paragraphs] == [("제1장", "첫 문단입니다.")]


def test_parse_pdf_extracts_text():
    parsed = parse_upload("a.pdf", _minimal_pdf("Hello ReadHorizon"))
    assert "Hello ReadHorizon" in parsed.text


def test_parse_hwpx_paragraphs_and_nested_table():
    parsed = parse_upload("a.hwpx", _hwpx(["첫째 문단.", "둘째 문단."], nested="표 안 문단."))
    lines = parsed.text.split("\n\n")
    assert lines[:2] == ["첫째 문단.", "둘째 문단."]
    assert "표 안 문단." in lines
    assert "표 앞" in lines  # 표를 담은 바깥 문단의 텍스트는 따로 남는다


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("a.hwp", b"\xd0\xcf\x11\xe0", "HWPX"),
        ("a.exe", b"MZ", "지원하지 않는"),
        ("a.pdf", b"not a pdf", "PDF"),
        ("a.hwpx", b"not a zip", "HWPX"),
    ],
)
def test_parse_errors_have_korean_guidance(name, data, message):
    with pytest.raises(ParseError, match=message):
        parse_upload(name, data)


def test_verse_is_split_into_stanza_and_line_labels(seosi_text):
    paragraphs = normalize_text(seosi_text, verse=True)
    assert len(paragraphs) == 9
    assert paragraphs[0].label == "1연 1행"
    assert paragraphs[7].label == "1연 8행"
    assert paragraphs[8].label == "2연 1행"
    assert paragraphs[8].text == "오늘 밤에도 별이 바람에 스치운다."


def test_single_stanza_verse_uses_line_labels_only():
    paragraphs = normalize_text("가\n나\n다", verse=True)
    assert [p.label for p in paragraphs] == ["1행", "2행", "3행"]


def test_prose_joins_hard_wrapped_lines():
    text = "그는 오래도록 창밖을\n바라보았다.\n\n다음 날 아침이\n밝았다."
    paragraphs = normalize_text(text, verse=False)
    assert [p.text for p in paragraphs] == ["그는 오래도록 창밖을 바라보았다.", "다음 날 아침이 밝았다."]
    assert [p.label for p in paragraphs] == ["1문단", "2문단"]


def test_prose_one_paragraph_per_line_when_lines_end_sentences():
    paragraphs = normalize_text("첫 문장이다.\n“둘째 대사다.”\n셋째 문장이다.", verse=False)
    assert len(paragraphs) == 3


def test_markdown_headings_become_sections():
    paragraphs = normalize_text("# 1장\n본문 하나.\n\n## 2장\n본문 둘.", verse=False)
    assert [(p.section, p.text) for p in paragraphs] == [("1장", "본문 하나."), ("2장", "본문 둘.")]


def test_looks_like_verse(seosi_text):
    assert looks_like_verse(seosi_text)
    prose = "이것은 한 줄이 매우 긴 산문 문단으로, 시처럼 짧게 끊기지 않고 길게 이어지는 문장입니다. " * 3
    assert not looks_like_verse("\n".join([prose, prose, prose]))


def test_long_paragraphs_are_split_at_sentence_boundaries():
    sentence = "가" * 150 + "다. "
    text = sentence * 40  # 한 문단 약 6,000자
    paragraphs = normalize_text(text, verse=False)
    assert len(paragraphs) >= 3
    assert all(len(p.text) <= 2000 for p in paragraphs)
    assert all(p.text.endswith("다.") for p in paragraphs)
    assert [p.label for p in paragraphs[:2]] == ["1문단", "2문단"]


def test_unbroken_long_text_is_hard_split():
    paragraphs = normalize_text("가" * 4500, verse=False)
    assert [len(p.text) for p in paragraphs] == [2000, 2000, 500]


def test_nfd_and_control_characters_are_normalized():
    import unicodedata

    text = unicodedata.normalize("NFD", "별을 노래하는\x00 마음으로\u200b")
    paragraphs = normalize_text(text, verse=False)
    assert paragraphs[0].text == "별을 노래하는 마음으로"


def test_archaic_hangul_survives_normalization():
    line = "ᄒᆞᆫ 잔 ᄆᆞᆨ걸리"
    assert normalize_text(line, verse=True)[0].text == line


def test_garbage_pdf_raises_parse_error():
    with pytest.raises(ParseError, match="PDF"):
        parse_upload("broken.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 50)


def test_zip_bomb_guard_for_docx(monkeypatch):
    from readhorizon.ingest import parsers

    document = docx.Document()
    document.add_paragraph("본문" * 100)
    buffer = io.BytesIO()
    document.save(buffer)
    monkeypatch.setattr(parsers, "MAX_ZIP_UNPACKED_BYTES", 1000)
    with pytest.raises(ParseError, match="너무 큽니다"):
        parse_upload("a.docx", buffer.getvalue())


def test_windows_path_filename_uses_basename():
    parsed = parse_upload("C:\\Users\\학생\\자료\\서시.txt", "내용".encode())
    assert parsed.title_hint == "서시"


@pytest.mark.parametrize(
    "payload",
    [
        # 엔티티 폭탄(billion laughs)
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        '<hs:sec xmlns:hs="s"><p><t>&lol2;</t></p></hs:sec>',
        # 외부 엔티티(XXE)
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<hs:sec xmlns:hs="s"><p><t>&xxe;</t></p></hs:sec>',
    ],
)
def test_hwpx_rejects_dtd_entities(payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Contents/section0.xml", payload)
    with pytest.raises(ParseError, match="HWPX"):
        parse_upload("attack.hwpx", buffer.getvalue())
