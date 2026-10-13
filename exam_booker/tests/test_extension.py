"""크롬 확장(접수 보조 모드) 테스트.

1) JS 판정 로직이 Python(matching.py)과 같은 결과를 내는지 (node 필요)
2) 실제 Chromium에 확장을 올려 모의 접수 화면에서 '표시만 하고 클릭하지 않는지'
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from exam_booker.config import Preference
from exam_booker.matching import is_full, pick_slot, row_matches

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
MOCK = ROOT / "mock"

ROWS = [
    "2026. 11. 21 09:00 서울 마포 상암고 잔여 0",
    "2026. 11. 21 13:00 서울 마포 상암고 잔여 3",
    "2026. 11. 22 09:00 서울 강남 개포고 잔여 12",
    "2026. 11. 22 09:00 인천 부평 부평고 마감",
    "2026년 11월 23일 14:00 일산 백석고 0/30",
    "2026/11/23 14:00 일산 백석고 10/30",
]
PREFS = [
    {"venue": "상암고", "date": "2026-11-21", "time": "09:00"},
    {"venue": "백석고", "date": "11월 23일"},
    {"venue": "개포고", "date": "", "time": ""},
]
FULL = ["마감", "매진", "불가"]


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    return subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, cwd=EXT).stdout  # noqa: S603


def test_js_matching_equals_python():
    script = f"""
      const M = require('./matching.js');
      const rows = {json.dumps(ROWS, ensure_ascii=False)};
      const prefs = {json.dumps(PREFS, ensure_ascii=False)};
      const full = {json.dumps(FULL, ensure_ascii=False)};
      const out = {{
        full: rows.map(r => M.isFull(r, full)),
        match: prefs.map(p => rows.map(r => M.rowMatches(r, p))),
        pick: M.pickSlot(rows, prefs, full),
        parsed: M.parsePrefs("상암고 | 2026-11-21 | 09:00\\n# 주석\\n\\n개포고"),
      }};
      console.log(JSON.stringify(out));
    """
    js = json.loads(_node(script))
    py_prefs = [Preference(**p) for p in PREFS]
    assert js["full"] == [is_full(r, FULL) for r in ROWS]
    assert js["match"] == [[row_matches(r, p) for r in ROWS] for p in py_prefs]
    idx, pref = pick_slot(ROWS, py_prefs, FULL)
    assert (js["pick"]["index"], js["pick"]["rank"]) == (idx, py_prefs.index(pref) + 1)
    assert js["parsed"] == [
        {"venue": "상암고", "date": "2026-11-21", "time": "09:00"},
        {"venue": "개포고", "date": "", "time": ""},
    ]


def test_manifest_minimal_permissions():
    m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert set(m["permissions"]) == {"storage", "notifications"}
    assert "host_permissions" not in m
    matches = m["content_scripts"][0]["matches"]
    assert all(u.startswith("https://") for u in matches)  # 배포판에는 localhost 없음
    # 접수 사이트에서 실행되는 코드에는 클릭·입력·제출·원격 코드 실행 경로가 없어야 한다
    # (options.js는 확장 자체 설정 화면이라 자기 입력칸을 채운다 — 대상 아님)
    src = "".join((EXT / f).read_text(encoding="utf-8") for f in ("content.js", "background.js", "matching.js", "sites.js"))
    for bad in (".click(", ".submit(", ".value =", "eval(", "new Function", "location.reload", "XMLHttpRequest"):
        assert bad not in src, bad


# ---- 실제 브라우저 ------------------------------------------------------------
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def mock_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(MOCK))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def ext_context(tmp_path):
    ext = tmp_path / "ext"
    shutil.copytree(EXT, ext)
    m = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    m["content_scripts"][0]["matches"] += ["http://127.0.0.1/*"]  # 테스트에서만 모의 서버 허용
    (ext / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    with sync_api.sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(tmp_path / "profile"),
                headless=True,
                executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"],
            )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium 실행 불가: {e}")
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=10000)
        # 설치 직후 설정(동의) 화면이 자동으로 열린다 — 열린 것을 확인한 뒤 테스트는 새 탭을 쓴다
        helper = ctx.new_page()
        for _ in range(50):
            if any("options.html" in p.url for p in ctx.pages):
                break
            helper.wait_for_timeout(100)
        else:
            pytest.fail("설치 후 설정 화면이 열리지 않음")
        helper.close()
        yield ctx, sw
        ctx.close()


def _set_settings(sw, consent=True, open_at="2020-01-01T00:00:00+09:00"):
    settings = {
        "enabled": True,
        "openAt": open_at,
        "prefsText": "상암고 | 2026-11-21 | 09:00\n상암고 | 11월 21일\n개포고",
        "overrides": {},
    }
    if consent:
        settings["consent"] = {"version": 1, "at": "2026-10-11T00:00:00Z"}
    sw.evaluate("s => chrome.storage.local.set({settings: s})", settings)


def _overlay_text(page):
    return page.evaluate("() => document.getElementById('exam-assist-overlay')?.shadowRoot.textContent || ''")


def test_assist_highlights_best_slot_without_clicking(ext_context, mock_server):
    ctx, sw = ext_context
    _set_settings(sw)
    page = ctx.new_page()
    page.goto(f"{mock_server}/mock_site.html?open=0")
    page.wait_for_selector("#exam-assist-overlay", state="attached", timeout=10000)
    page.wait_for_selector("#apply[data-eb-apply]", timeout=5000)  # 원서접수 버튼 강조
    assert "직접 누르세요" in _overlay_text(page)

    # 사용자가 직접 누르는 동작
    page.click("#apply")
    page.click("#search")
    best = page.wait_for_selector("tr.slot[data-eb-rank=best]", timeout=5000)
    assert "13:00" in best.inner_text() and "상암고" in best.inner_text()  # 1순위(09:00)는 잔여 0 → 2순위
    ranks = page.eval_on_selector_all("tr.slot", "rs => rs.map(r => r.getAttribute('data-eb-rank'))")
    assert ranks == ["full", "best", "match", None]
    page.wait_for_function(
        "() => document.getElementById('exam-assist-overlay').shadowRoot.textContent.includes('2순위 빈자리')"
    )
    page.wait_for_timeout(1500)
    assert page.inner_text("#result") == ""  # 확장은 '선택'을 누르지 않았다
    assert page.is_hidden("#final")


def test_assist_detects_captcha_and_needs_consent(ext_context, mock_server):
    ctx, sw = ext_context
    _set_settings(sw)
    page = ctx.new_page()
    page.goto(f"{mock_server}/mock_site.html?open=0&captcha=1")
    page.wait_for_function(
        "() => (document.getElementById('exam-assist-overlay')?.shadowRoot.textContent || '').includes('보안문자')"
    )
    assert page.is_visible("#captcha")  # 우회하지 않음

    _set_settings(sw, consent=False)
    page.goto(f"{mock_server}/mock_site.html?open=0")
    page.wait_for_timeout(1500)
    assert page.query_selector("#exam-assist-overlay") is None  # 동의 전에는 동작하지 않음


def test_countdown_before_open(ext_context, mock_server):
    ctx, sw = ext_context
    _set_settings(sw, open_at="2099-01-01T10:00:00+09:00")
    page = ctx.new_page()
    page.goto(f"{mock_server}/mock_site.html?open=9999999999999")
    page.wait_for_function(
        "() => (document.getElementById('exam-assist-overlay')?.shadowRoot.textContent || '').includes('기다리는 중')"
    )
    assert page.query_selector("#apply[data-eb-apply]") is None  # 비활성 버튼은 강조하지 않음


def test_options_requires_consent_and_rejects_personal_data(ext_context):
    ctx, sw = ext_context
    ext_id = sw.url.split("/")[2]
    page = ctx.new_page()
    page.goto(f"chrome-extension://{ext_id}/options.html")
    page.fill("#openAt", "2026-11-02T10:00")
    page.fill("#prefs", "상암고 | 2026-11-21\n개포고")
    page.click("#save")
    assert "동의 항목" in page.inner_text("#status")
    assert sw.evaluate("() => chrome.storage.local.get('settings')") == {}

    for box in page.query_selector_all("[data-c]"):
        box.check()
    page.fill("#prefs", "상암고\n010-1234-5678")
    page.click("#save")
    assert "개인정보" in page.inner_text("#status")

    page.fill("#prefs", "상암고 | 2026-11-21\n개포고")
    page.click("#save")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('저장했습니다')")
    saved = sw.evaluate("() => chrome.storage.local.get('settings')")["settings"]
    assert saved["openAt"] == "2026-11-02T10:00:00+09:00"
    assert saved["consent"]["version"] == 1
    assert set(saved) == {"enabled", "openAt", "prefsText", "overrides", "consent"}  # 저장 항목 최소

    page.click("#wipe")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('삭제')")
    assert sw.evaluate("() => chrome.storage.local.get(null)") == {}
