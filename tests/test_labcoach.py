"""LabCoach: 좌표 파싱, 민감정보 마스킹·수집 기록, 도구 로그 발췌, 세션·요청 구성, SDK 왕복, 렌더링."""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import anthropic
import pytest
from PIL import Image

from labcoach.capture import ChangeDetector, prepare
from labcoach.coach import DEMO_STEP, ClaudeCoach, DemoCoach, LLMError, LLMRefusal, build_request, parse_step_message
from labcoach.config import Settings, parse_region, parse_regions
from labcoach.privacy import CollectionLog, format_notice, mask_text
from labcoach.render import arrow_head, render
from labcoach.schema import STEP_SCHEMA, parse_step, scale_annotation
from labcoach.session import Session
from labcoach.toollogs import excerpt, summarize_regshot


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(llm_mode="demo", data_dir=tmp_path / "lc", tts=False, hotkeys=False)


def _step_data(**overrides) -> dict:
    data = json.loads(json.dumps(DEMO_STEP))
    data.update(overrides)
    return data


# ---- 스키마·좌표


def test_schema_requires_every_property():
    assert set(STEP_SCHEMA["required"]) == set(STEP_SCHEMA["properties"])
    item = STEP_SCHEMA["properties"]["annotations"]["items"]
    assert set(item["required"]) == set(item["properties"])


def test_parse_step_clamps_and_repairs_annotations():
    step = parse_step(
        _step_data(
            annotations=[
                {"kind": "box", "x": 900, "y": 800, "x2": 100, "y2": 1500, "text": "뒤집힌 상자", "tone": "attack"},
                {"kind": "arrow", "x": 10, "y": 10, "x2": 10, "y2": 10, "text": "길이 0", "tone": "info"},
                {"kind": "box", "x": 10, "y": 10, "x2": 12, "y2": 400, "text": "너무 좁음", "tone": "info"},
                {"kind": "circle", "x": -5, "y": "abc", "x2": 0, "y2": 0, "text": "알 수 없는 종류", "tone": "pink"},
            ],
            phase="not-a-phase",
            confidence="certain",
        )
    )
    assert [a.text for a in step.annotations] == ["뒤집힌 상자", "알 수 없는 종류"]
    box, label = step.annotations
    assert (box.x, box.y, box.x2, box.y2) == (100, 800, 900, 1000)
    assert (label.kind, label.x, label.y, label.tone) == ("label", 0, 0, "info")
    assert step.phase == "unknown" and step.phase_label == "판단 보류"
    assert step.confidence == "medium"


def test_scale_annotation_maps_normalized_coordinates():
    step = parse_step(_step_data())
    box = step.annotations[0]
    assert scale_annotation(box, 1000, 500) == (box.x, box.y // 2, box.x2, round(box.y2 / 2))
    head = arrow_head(0, 0, 100, 0, 10)
    assert head[0] == (100, 0) and all(p[0] < 100 for p in head[1:])


def test_parse_region_helpers():
    assert parse_region("10, 20, 300, 200") == (10, 20, 300, 200)
    assert parse_region("1,2,3") is None and parse_region("0,0,0,10") is None
    assert parse_regions("0,0,10,10;bad;5,5,20,20") == [(0, 0, 10, 10), (5, 5, 20, 20)]


# ---- 민감정보


@pytest.mark.parametrize(
    ("text", "code", "secret"),
    [
        ("C2 서버 8.8.8.8 로 접속", "public_ip", "8.8.8.8"),
        ("MAC 08:00:27:ab:cd:ef 확인", "mac", "08:00:27:ab:cd:ef"),
        (r"경로 C:\Users\hong\AppData\Roaming\x.exe", "user_dir", "hong"),
        ("/home/analyst/samples", "user_dir", "analyst"),
        ("password=Sup3rS3cret! 로그인", "password", "Sup3rS3cret!"),
        ("key sk-ant-api03-abcdefghijklmnop", "secret", "sk-ant-api03-abcdefghijklmnop"),
        ("메일 analyst@example.com", "email", "analyst@example.com"),
    ],
)
def test_mask_text_lab_values(text, code, secret):
    result = mask_text(text)
    assert result.counts.get(code) == 1
    assert secret not in result.text


def test_mask_text_keeps_lab_addresses_and_versions():
    text = r"10.0.2.15 → 192.168.56.101:4444, 172.16.0.5, 버전 1.2.3.4567, C:\Users\Public\x, 127.0.0.1"
    result = mask_text(text)
    assert result.counts == {}
    assert result.text == text
    assert mask_text("8.8.8.8", mask_public_ip=False).counts == {}


def test_collection_log_records_items_only_and_notices(tmp_path):
    log = CollectionLog(tmp_path / "c.jsonl", retention_days=30)
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    log.record(["screenshot", "question", "screenshot"], {"public_ip": 2}, now=now - timedelta(days=40))
    log.record(["screenshot", "tool_log"], {"mac": 1}, now=now - timedelta(days=1))
    raw = (tmp_path / "c.jsonl").read_text(encoding="utf-8")
    assert "8.8.8.8" not in raw and '"items": ["question", "screenshot"]' in raw

    assert log.notice_due(30, now=now)
    log.mark_notice(now=now)
    assert not log.notice_due(30, now=now + timedelta(days=29))
    assert log.notice_due(30, now=now + timedelta(days=30))

    assert log.purge(now=now) == 1
    summary = log.summary(30, now=now)
    assert summary == {"days": 30, "requests": 1, "items": {"screenshot": 1, "tool_log": 1}, "masked": {"mac": 1}}
    assert log.last_notice() == now  # 고지 기록은 보관 기간과 무관하게 유지
    text = format_notice(summary, "live", 30)
    assert "Anthropic" in text and "MAC 주소: 1" in text and "VM 창 화면 캡처" in text


# ---- 도구 로그


def test_procmon_excerpt_filters_and_dedupes(tmp_path):
    rows = [
        '"Time of Day","Process Name","PID","Operation","Path","Result","Detail"',
        '"12:00:01","sample.exe","4321","RegQueryValue","HKCU\\Software\\X","SUCCESS",""',
        '"12:00:02","sample.exe","4321","RegSetValue","HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater","SUCCESS","Type: REG_SZ, Data: C:\\Users\\hong\\a.exe"',
        '"12:00:03","sample.exe","4321","RegSetValue","HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater","SUCCESS","Type: REG_SZ"',
        '"12:00:04","sample.exe","4321","TCP Connect","win10:49712 -> 203.0.113.9:443","SUCCESS","Length: 0"',
        '"12:00:05","sample.exe","4321","WriteFile","C:\\Temp\\drop.dll","ACCESS DENIED",""',
    ]
    path = tmp_path / "Logfile.CSV"
    path.write_bytes(b"\xef\xbb\xbf" + "\n".join(rows).encode())
    kind, body = excerpt(path)
    lines = body.splitlines()
    assert kind == "procmon"
    assert len(lines) == 3
    assert "RegSetValue" in lines[0] and "Data:" in lines[0]
    assert "TCP Connect" in lines[1]
    assert lines[2].endswith("[ACCESS DENIED]")


def test_procmon_large_file_reads_header_from_start(tmp_path, monkeypatch):
    import labcoach.toollogs as tl

    monkeypatch.setattr(tl, "TAIL_BYTES", 300)
    header = '"Time of Day","Process Name","PID","Operation","Path","Result","Detail"\n'
    filler = "".join(f'"12:00:{i:02d}","a.exe","1","RegQueryValue","HKLM\\\\X{i}","SUCCESS",""\n' for i in range(40))
    tail = '"12:01:00","evil.exe","9","Process Create","C:\\\\Windows\\\\cmd.exe","SUCCESS","PID: 10"\n'
    path = tmp_path / "big.csv"
    path.write_text(header + filler + tail, encoding="utf-8")
    kind, body = excerpt(path)
    assert kind == "procmon"
    assert "evil.exe(9) Process Create" in body


def test_regshot_utf16_excerpt(tmp_path):
    text = (
        "Regshot 1.9.0 x64 Unicode\nComments:\n----------------------------------\n"
        "Keys added: 1\n----------------------------------\nHKLM\\SYSTEM\\ControlSet001\\Services\\EvilSvc\n\n"
        "Values modified: 1\n----------------------------------\n"
        "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater: \"C:\\a.exe\"\n\n"
        "Total changes: 2\n"
    )
    path = tmp_path / "regshot.txt"
    path.write_bytes(text.encode("utf-16"))
    kind, body = excerpt(path)
    assert kind == "regshot"
    assert body.splitlines() == [
        "[Keys added] HKLM\\SYSTEM\\ControlSet001\\Services\\EvilSvc",
        '[Values modified] HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Updater: "C:\\a.exe"',
        "Total changes: 2",
    ]
    assert summarize_regshot("") == []
    assert excerpt(tmp_path / "없음.txt") == ("missing", "")


# ---- 캡처 가공·변화 감지


def test_prepare_blacks_out_and_downscales():
    image = Image.new("RGB", (2000, 1000), (255, 255, 255))
    png, size = prepare(image, [(0, 0, 1000, 1000)], max_edge=1000)
    assert size == (1000, 500)
    out = Image.open(__import__("io").BytesIO(png))
    assert out.getpixel((100, 100)) == (0, 0, 0)
    assert out.getpixel((900, 100)) == (255, 255, 255)
    assert not out.info  # 메타데이터 없음


def test_find_window_prefers_vm_screen_over_manager(monkeypatch):
    import sys

    from labcoach.capture import find_window

    def win(title, w, h, minimized=False):
        return SimpleNamespace(title=title, isMinimized=minimized, isVisible=True, box=SimpleNamespace(left=10, top=20, width=w, height=h))

    windows = [
        win("Oracle VirtualBox 관리자", 1900, 1000),
        win("Oracle VM VirtualBox Manager", 1900, 1000),
        win("LabCoach", 1900, 1000),
        win("Win10-분석 [실행 중] - Oracle VirtualBox", 1280, 800),
        win("Kali - VMware Workstation", 1600, 900, minimized=True),
        win("메모장", 1900, 1000),
    ]
    monkeypatch.setitem(sys.modules, "pywinctl", SimpleNamespace(getAllWindows=lambda: windows))
    target = find_window(("VirtualBox", "VMware"))
    assert target.title.startswith("Win10-분석") and target.region == (10, 20, 1280, 800)
    monkeypatch.setitem(sys.modules, "pywinctl", SimpleNamespace(getAllWindows=lambda: windows[:3]))
    assert find_window(("VirtualBox",)) is None


def test_change_detector():
    detector = ChangeDetector(threshold=5)
    a = Image.new("RGB", (640, 360), (20, 20, 20))
    assert detector.changed(a)
    detector.accept(a)
    assert not detector.changed(a.copy())
    b = a.copy()
    b.paste((240, 240, 240), (0, 0, 640, 120))
    assert detector.changed(b)


# ---- 세션


class FakeCoach:
    mode = "live"

    def __init__(self):
        self.calls = []

    def explain(self, image_png, task):
        self.calls.append((image_png, task))
        return parse_step(_step_data(subtitle=f"단계 {len(self.calls)}"))


def test_session_masks_context_and_logs_items(settings, tmp_path):
    log_file = tmp_path / "procmon.csv"
    log_file.write_text(
        '"Time of Day","Process Name","PID","Operation","Path","Result","Detail"\n'
        '"1","s.exe","1","TCP Connect","host:1 -> 8.8.4.4:443","SUCCESS",""\n',
        encoding="utf-8",
    )
    coach = FakeCoach()
    log = CollectionLog(settings.collection_log, 30)
    session = Session(settings, coach, log, [log_file])
    image = Image.new("RGB", (800, 600), (0, 0, 0))

    session.analyze(image, "Win10 [Running] - Oracle VirtualBox", "8.8.8.8 로 가는 건 뭐야? 메일 a@b.com")
    session.analyze(image)
    first, second = coach.calls[0][1], coach.calls[1][1]
    assert "8.8.8.8" not in first and "8.8.4.4" not in first and "a@b.com" not in first
    assert '<tool_log name="procmon.csv" kind="procmon">' in first
    assert "Oracle VirtualBox" in first and "학습자 질문" in first
    assert "직전 단계 자막" in second and "단계 1" in second
    assert Image.open(__import__("io").BytesIO(coach.calls[0][0])).size == (800, 600)

    summary = log.summary(30)
    assert summary["requests"] == 2
    assert summary["items"]["screenshot"] == 2 and summary["items"]["history"] == 1
    assert summary["masked"] == {"public_ip": 3, "email": 1}
    notes = session.notes_markdown()
    assert notes.count("## ") == 2 and "8.8.8.8" not in notes and "T1547.001" in notes


def test_demo_session_sends_and_logs_nothing(settings):
    log = CollectionLog(settings.collection_log, 30)
    session = Session(settings, DemoCoach(), log)
    step = session.analyze(Image.new("RGB", (100, 100)), "", "")
    assert step.demo
    assert not settings.collection_log.exists()


def test_render_draws_on_image(settings):
    image = Image.new("RGB", (640, 400), (255, 255, 255))
    out = render(image, DemoCoach().explain(b"", ""))
    assert out.size == image.size
    assert out.getpixel((320, 395)) != (255, 255, 255)  # 자막 띠


# ---- Claude 요청·응답


def test_build_request_shape():
    req = build_request("claude-opus-5-5", "low", b"png-bytes", "설명해 줘")
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["schema"] is STEP_SCHEMA
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    image, text = req["messages"][0]["content"]
    assert base64.b64decode(image["source"]["data"]) == b"png-bytes"
    assert text == {"type": "text", "text": "설명해 줘"}
    assert "thinking" not in build_request("claude-haiku-4-5", "low", b"", "x")


def _message(stop_reason="end_turn", text=None):
    block = SimpleNamespace(type="text", text=json.dumps(DEMO_STEP, ensure_ascii=False) if text is None else text)
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0, iterations=None)
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="thinking", thinking=""), block], usage=usage, model="m")


def test_parse_step_message_errors():
    assert parse_step_message(_message()).registry[0].first.endswith("\\Run")
    with pytest.raises(LLMRefusal):
        parse_step_message(_message("refusal"))
    with pytest.raises(LLMError):
        parse_step_message(_message("max_tokens"))
    with pytest.raises(LLMError):
        parse_step_message(_message(text="{not json"))


class FakeAnthropic(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAnthropic.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = json.dumps(DEMO_STEP, ensure_ascii=False)
        message = {
            "id": "msg_lc", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1500, "output_tokens": 1},
        }
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": payload[:50]}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": payload[50:]}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 300}},
            {"type": "message_stop"},
        ]
        data = b"".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_claude_coach_sdk_round_trip():
    FakeAnthropic.requests = []
    server = HTTPServer(("127.0.0.1", 0), FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sdk = anthropic.Anthropic(api_key="test-key", base_url=f"http://127.0.0.1:{server.server_address[1]}", max_retries=0)
        step = ClaudeCoach("claude-opus-5-5", "low", client=sdk).explain(b"\x89PNG", "화면 설명")
    finally:
        server.shutdown()
    assert step.phase == "persistence" and step.mitre == [("T1547.001", "Registry Run Keys / Startup Folder")]
    assert step.usage["output_tokens"] == 300 and step.model == "claude-opus-5-5"
    sent = FakeAnthropic.requests[0]
    assert sent["path"].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in sent["headers"].get("anthropic-beta", "")
    assert sent["body"]["stream"] is True and sent["body"]["fallbacks"] == "default"
    assert sent["body"]["messages"][0]["content"][0]["type"] == "image"
