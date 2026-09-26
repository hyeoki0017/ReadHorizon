"""실제 Anthropic SDK로 로컬 가짜 서버와 통신해, 요청 직렬화와 스트리밍 응답(인용 포함) 파싱을 확인한다.

네트워크나 API 키 없이 SDK 경로(beta.messages.stream → get_final_message → parse_message)를 검증한다.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from readhorizon.analysis.engine import AnalysisEngine, WorkContext
from readhorizon.analysis.llm import FALLBACK_BETA, ClaudeClient, LLMError, LLMRefusal
from readhorizon.analysis.presets import find_genre
from readhorizon.ingest.normalize import normalize_text


def _sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events)


def _stream_events(stop_reason: str = "end_turn") -> list[dict]:
    message = {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1},
    }
    citation = {
        "type": "content_block_location", "cited_text": "[1연 2행] 한 점 부끄럼이 없기를,", "document_index": 0,
        "document_title": "작품 원문: 서시", "start_block_index": 1, "end_block_index": 2,
    }
    return [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "## 화자와 상황\n화자는 "}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "", "citations": []}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "citations_delta", "citation": citation}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "부끄럼 없는 삶을 바란다"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]


def _ocr_events() -> list[dict]:
    payload = json.dumps(
        {"has_text": True, "text": "죽는 날까지 하늘을 우러[?]", "layout": "가로쓰기 1단", "uncertain_notes": ["끝 글자 흐림"]},
        ensure_ascii=False,
    )
    message = {
        "id": "msg_ocr", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 900, "output_tokens": 1},
    }
    return [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": payload[:20]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": payload[20:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"},
    ]


class FakeAnthropic(BaseHTTPRequestHandler):
    requests: list[dict] = []
    mode = "ok"

    def log_message(self, *args):  # 테스트 출력 조용히
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAnthropic.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if FakeAnthropic.mode == "auth":
            payload = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if FakeAnthropic.mode == "ocr":
            data = _sse(_ocr_events())
        else:
            data = _sse(_stream_events("refusal" if FakeAnthropic.mode == "refusal" else "end_turn"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_server():
    FakeAnthropic.requests = []
    FakeAnthropic.mode = "ok"
    server = HTTPServer(("127.0.0.1", 0), FakeAnthropic)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _engine(base_url: str) -> AnalysisEngine:
    sdk = anthropic.Anthropic(api_key="test-key", base_url=base_url, max_retries=0)
    return AnalysisEngine(ClaudeClient("claude-opus-5", "high", client=sdk), None, 200_000)


def _ctx(seosi_text: str) -> WorkContext:
    category, genre = find_genre("literature", "poem")
    return WorkContext("서시", "윤동주", category, genre, normalize_text(seosi_text, verse=True), "소개글입니다.")


def test_sdk_round_trip_with_citations(fake_server, seosi_text):
    result = _engine(fake_server).analyze(_ctx(seosi_text), "overview")

    sent = FakeAnthropic.requests[0]
    assert sent["path"].startswith("/v1/messages")
    assert FALLBACK_BETA in sent["headers"].get("anthropic-beta", "")
    body = sent["body"]
    assert body["fallbacks"] == "default" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "high"}
    work_doc, intro_doc = body["messages"][0]["content"][:2]
    assert work_doc["source"]["type"] == "content" and len(work_doc["source"]["content"]) == 9
    assert work_doc["cache_control"] == {"type": "ephemeral"}
    assert intro_doc["source"]["type"] == "text" and intro_doc["citations"] == {"enabled": True}

    citation = result["segments"][1]["citations"][0]
    assert citation["labels"] == ["1연 2행"] and citation["verified"] is True
    assert result["model"] == "claude-opus-5" and result["mode"] == "live"
    assert result["badge"]["level"] in {"high", "medium"}


def test_sdk_refusal_is_reported(fake_server, seosi_text):
    FakeAnthropic.mode = "refusal"
    with pytest.raises(LLMRefusal):
        _engine(fake_server).ask(_ctx(seosi_text), "질문")


def test_sdk_auth_error_is_translated(fake_server, seosi_text):
    FakeAnthropic.mode = "auth"
    with pytest.raises(LLMError, match="인증"):
        _engine(fake_server).ask(_ctx(seosi_text), "질문")


def test_sdk_ocr_round_trip_with_structured_output(fake_server):
    from readhorizon.ocr import OCRInputs, run_ocr

    FakeAnthropic.mode = "ocr"
    sdk = anthropic.Anthropic(api_key="test-key", base_url=fake_server, max_retries=0)
    llm = ClaudeClient("claude-opus-5", "high", client=sdk, ocr_effort="medium")
    result = run_ocr(llm, OCRInputs([b"\xff\xd8fakejpeg"], None, 1), scope="passage", verse_hint=True)

    body = FakeAnthropic.requests[0]["body"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium" and body["stream"] is True
    image = body["messages"][0]["content"][1]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert result["text"] == "죽는 날까지 하늘을 우러[?]" and result["uncertain_count"] == 1
    assert result["layout"] == "가로쓰기 1단" and result["mode"] == "live"
