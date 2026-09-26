"""이미지·스캔 PDF 텍스트 추출(OCR): 전처리, 요청 형식, 응답 해석, API, 정확도 평가."""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

from readhorizon import ocr
from readhorizon.analysis.llm import (
    FALLBACK_BETA,
    OCR_SCHEMA,
    ClaudeClient,
    DemoClient,
    LLMError,
    LLMRefusal,
    OCRResult,
    parse_ocr_message,
)
from readhorizon.main import create_app
from readhorizon.ocr import OCRInputError, OCRInputs, prepare_image, prepare_inputs, prepare_pdf, run_ocr
from readhorizon.ocr_eval import OCRBenchmarkError, cer, edit_distance, load_manifest, run_ocr_benchmark

from .conftest import SAMPLES


def _jpeg_with_metadata(size=(400, 200), orientation=6) -> bytes:
    image = Image.new("RGB", size, "white")
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation: 6 = 90도 회전 필요
    exif[0x013B] = "Hong Gildong"  # Artist
    exif[0x010E] = "student id 2026-0001"  # ImageDescription
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _pdf(pages: int, author: str | None = "홍길동") -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(200, 200)
    if author:
        writer.add_metadata({"/Author": author})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ── 전처리 ────────────────────────────────────────────────


def test_prepare_image_applies_orientation_and_strips_metadata():
    out = Image.open(io.BytesIO(prepare_image(_jpeg_with_metadata())))
    assert out.format == "JPEG"
    assert out.size == (200, 400)  # EXIF 방향을 반영해 세움
    assert len(out.getexif()) == 0  # 작성자·설명 등 메타데이터 없음


def test_prepare_image_limits_resolution():
    big = Image.new("RGB", (6000, 3000), "white")
    buffer = io.BytesIO()
    big.save(buffer, "PNG")
    out = Image.open(io.BytesIO(prepare_image(buffer.getvalue())))
    assert max(out.size) <= ocr.MAX_EDGE
    assert out.size[0] * out.size[1] <= ocr.MAX_PIXELS


def test_prepare_image_flattens_transparency_and_uses_first_gif_frame():
    rgba = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    buffer = io.BytesIO()
    rgba.save(buffer, "PNG")
    out = Image.open(io.BytesIO(prepare_image(buffer.getvalue())))
    assert out.mode == "RGB" and out.getpixel((10, 10)) == (255, 255, 255)

    frames = [Image.new("P", (30, 30), color) for color in (1, 2)]
    gif = io.BytesIO()
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    assert Image.open(io.BytesIO(prepare_image(gif.getvalue()))).format == "JPEG"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not an image", "읽을 수 없습니다"),
        (lambda: (lambda b: (Image.new("RGB", (10, 10)).save(b, "BMP"), b.getvalue())[1])(io.BytesIO()), "형식"),
    ],
)
def test_prepare_image_rejects_bad_input(data, message):
    payload = data() if callable(data) else data
    with pytest.raises(OCRInputError, match=message):
        prepare_image(payload)


def test_prepare_image_rejects_decompression_bomb(monkeypatch):
    monkeypatch.setattr(ocr, "MAX_SOURCE_PIXELS", 1000)
    with pytest.raises(OCRInputError, match="해상도"):
        prepare_image(_jpeg_with_metadata(size=(100, 100)))


def test_prepare_pdf_strips_metadata_and_checks_pages():
    data, pages = prepare_pdf(_pdf(3), max_pages=20)
    assert pages == 3
    assert not (PdfReader(io.BytesIO(data)).metadata or {}).get("/Author")
    with pytest.raises(OCRInputError, match="20쪽까지"):
        prepare_pdf(_pdf(21), max_pages=20)
    with pytest.raises(OCRInputError, match="PDF"):
        prepare_pdf(b"%PDF-1.4 broken", max_pages=20)


def test_prepare_inputs_rules():
    image = _jpeg_with_metadata()
    assert prepare_inputs([("a.jpg", image), ("b.jpg", image)], 10, 20).pages == 2
    single_pdf = prepare_inputs([("scan.pdf", _pdf(2))], 10, 20)
    assert single_pdf.pdf is not None and single_pdf.images == [] and single_pdf.pages == 2
    with pytest.raises(OCRInputError, match="HEIC"):
        prepare_inputs([("photo.HEIC", b"....")], 10, 20)
    with pytest.raises(OCRInputError, match="하나만"):
        prepare_inputs([("scan.pdf", _pdf(1)), ("a.jpg", image)], 10, 20)
    with pytest.raises(OCRInputError, match="10장까지"):
        prepare_inputs([(f"{i}.jpg", image) for i in range(11)], 10, 20)
    with pytest.raises(OCRInputError, match="올려 주세요"):
        prepare_inputs([], 10, 20)


# ── 요청·응답 ─────────────────────────────────────────────


def test_ocr_task_scope_and_verse_hint():
    assert "선택지" in ocr.ocr_task("passage", False)
    assert "전체" in ocr.ocr_task("all", False)
    assert "행과 연" in ocr.ocr_task("all", True)


def test_claude_ocr_request_shape():
    client = ClaudeClient("claude-opus-5", "high", client=object(), ocr_effort="medium")
    request = client.build_ocr_request([b"img1", b"img2"], None, "SYS", "TASK")
    content = request["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "1쪽:" and content[1]["source"]["media_type"] == "image/jpeg"
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": OCR_SCHEMA}, "effort": "medium"}
    assert request["thinking"] == {"type": "adaptive"}
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert all("citations" not in c for c in content)  # 구조화 출력과 인용은 함께 쓸 수 없다

    pdf_request = client.build_ocr_request([], b"%PDF", "SYS", "TASK")
    assert pdf_request["messages"][0]["content"][0]["type"] == "document"


def _message(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5",
        usage=None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


def test_parse_ocr_message():
    payload = {"has_text": True, "text": "하늘을 우러[?]", "layout": "가로쓰기 1단", "uncertain_notes": ["마지막 글자 흐림"]}
    result = parse_ocr_message(_message(json.dumps(payload, ensure_ascii=False)))
    assert result.text == "하늘을 우러[?]" and result.layout == "가로쓰기 1단" and result.notes == ["마지막 글자 흐림"]

    with pytest.raises(LLMError, match="나눠서"):
        parse_ocr_message(_message('{"has_text": true, "text": "잘린', stop_reason="max_tokens"))
    with pytest.raises(LLMRefusal):
        parse_ocr_message(_message("", stop_reason="refusal"))
    with pytest.raises(LLMError, match="해석"):
        parse_ocr_message(_message("JSON 아님"))


class FakeOCR:
    mode = "live"
    model = "fake"

    def __init__(self, text: str, has_text: bool = True):
        self.text, self.has_text = text, has_text
        self.calls = []

    def transcribe(self, images, pdf, system, task):
        self.calls.append({"images": images, "pdf": pdf, "system": system, "task": task})
        return OCRResult(self.has_text, self.text, "가로쓰기", ["흐린 곳 있음"], "fake")


def test_run_ocr_masks_and_counts_uncertain_marks():
    llm = FakeOCR("별을 노래[?]는 마음\x00으로\n연락처 010-1234-5678\n[?괴]로워했다")
    result = run_ocr(llm, OCRInputs([b"jpeg"], None, 1), scope="passage", verse_hint=True)
    assert "010-1234-5678" not in result["text"] and result["masked"] == {"mobile": 1}
    assert "\x00" not in result["text"]
    assert result["uncertain_count"] == 2 and len(result["uncertain_snippets"]) == 2
    assert "선택지" in llm.calls[0]["task"] and "행과 연" in llm.calls[0]["task"]
    assert "옛한글" in llm.calls[0]["system"]


# ── API ───────────────────────────────────────────────────


def _ocr_client(settings, store, llm=None, **overrides):
    return TestClient(create_app(settings=replace(settings, **overrides), llm=llm or DemoClient(), store=store))


def test_ocr_endpoint_demo_flow_and_registration(settings, store, workspace):
    client = _ocr_client(settings, store)
    files = [("files", ("page1.jpg", _jpeg_with_metadata(), "image/jpeg"))]
    response = client.post("/api/ocr", files=files, data={"scope": "all", "genre": "poem"}, headers=workspace)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "demo" and result["pages"] == 1 and result["uncertain_count"] == 1
    assert client.get("/api/works", headers=workspace).json()["works"] == []  # 추출만으로는 저장하지 않음

    body = client.post(
        "/api/works",
        files={"category": (None, "literature"), "genre": (None, "poem"), "text": (None, result["text"]),
               "origin": (None, "ocr")},
        headers=workspace,
    ).json()
    assert body["work"]["source_kind"] == "ocr"


def test_ocr_endpoint_logs_masking_and_enforces_llm_limit(settings, store, workspace):
    client = _ocr_client(settings, store, llm=FakeOCR("메일 a@b.com"), llm_per_minute=1, llm_daily=0)
    files = [("files", ("p.png", _jpeg_with_metadata(), "image/png"))]
    first = client.post("/api/ocr", files=files, headers=workspace)
    assert first.status_code == 200 and "[마스킹:이메일]" in first.json()["text"]
    notice = client.get("/api/privacy/notice", headers=workspace).json()
    assert {"category": "email", "label": "이메일", "count": 1} in notice["masking_detected"]
    assert client.post("/api/ocr", files=files, headers=workspace).status_code == 429


@pytest.mark.parametrize(
    ("files", "status", "message"),
    [
        ([], 422, "올려 주세요"),
        ([("files", ("x.bmp", b"BMxxxx", "image/bmp"))], 422, "읽을 수 없습니다"),
        ([("files", ("scan.pdf", _pdf(21), "application/pdf"))], 422, "20쪽까지"),
        ([("files", (f"{i}.jpg", b"x", "image/jpeg")) for i in range(11)], 422, "10장까지"),
    ],
)
def test_ocr_endpoint_rejects_bad_uploads(settings, store, workspace, files, status, message):
    client = _ocr_client(settings, store)
    response = client.post("/api/ocr", files=files or {"scope": (None, "all")}, headers=workspace)
    assert response.status_code == status and message in response.json()["detail"]


def test_ocr_requires_workspace(settings, store):
    client = _ocr_client(settings, store)
    assert client.post("/api/ocr", files={"scope": (None, "all")}).status_code == 400
    assert client.post("/api/ocr", files={"scope": (None, "all")}, headers={"X-Workspace-Id": str(uuid.uuid4())}).status_code == 422


def test_privacy_notice_lists_ocr_images(client, workspace):
    notice = client.get("/api/privacy/notice", headers=workspace).json()
    items = {i["item"]: i for i in notice["collected_items"]}
    assert "저장하지 않음" in items["텍스트 추출용 이미지·스캔 PDF (선택)"]["retention"]


# ── 정확도 평가(CER) ──────────────────────────────────────


def test_cer_and_edit_distance():
    assert edit_distance("kitten", "sitting") == 3
    assert cer("죽는 날까지", "죽는날까지") == 0.0  # 공백 무시
    assert cer("죽는 날꺄지", "죽는 날까지") == pytest.approx(1 / 5)
    assert cer("하늘을 우러[?러]", "하늘을 우러러") == 0.0  # 추정 글자는 그 글자로 비교
    assert cer("하늘을 우러[?]", "하늘을 우러러") == pytest.approx(1 / 6)  # 판독 불가는 오류
    with pytest.raises(OCRBenchmarkError):
        cer("가", "")


def test_run_ocr_benchmark_on_sample_manifest():
    manifest = SAMPLES / "ocr" / "manifest.sample.json"
    items = load_manifest(manifest)
    truth = (SAMPLES / "works" / "서시_윤동주.txt").read_text(encoding="utf-8")
    report = run_ocr_benchmark(FakeOCR(truth.replace("괴로워", "괴로위")), items, manifest.parent, 10, 20)
    assert report["total"] == 1
    assert 0 < report["mean_cer"] < 0.05
    assert report["items"][0]["truth_chars"] > 0


def test_load_manifest_validation(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps([{"id": 1, "files": ["a.png"]}]), encoding="utf-8")
    with pytest.raises(OCRBenchmarkError, match="truth"):
        load_manifest(path)


def test_ocr_image_preparation_is_rate_limited_per_client(settings, store, workspace):
    client = _ocr_client(settings, store, create_per_minute=1, llm_per_minute=0, llm_daily=0)
    files = [("files", ("p.jpg", _jpeg_with_metadata(), "image/jpeg"))]
    assert client.post("/api/ocr", files=files, headers=workspace).status_code == 200
    assert client.post("/api/ocr", files=files, headers=workspace).status_code == 429
