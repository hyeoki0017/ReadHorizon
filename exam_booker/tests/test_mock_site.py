"""모의 접수 사이트(mock/mock_site.html)로 전체 흐름을 실제 브라우저에서 확인."""

import os
import threading
import time
from pathlib import Path

import pytest

from exam_booker.config import parse_config

sync_api = pytest.importorskip("playwright.sync_api")

MOCK = (Path(__file__).resolve().parents[1] / "mock" / "mock_site.html").as_uri()


def make_cfg(url, prefs, auto_confirm=False):
    return parse_config(
        {
            "apply_url": url,
            "open_at": "2026-01-01 00:00:00",
            "poll_interval": 1.0,
            "give_up_after_minutes": 0.5,
            "auto_confirm": auto_confirm,
            "sync_server_time": False,
            "preferences": prefs,
            "selectors": {
                "apply_button": "#apply",
                "before_slots": ["#search"],
                "slot_row": "#slots tbody tr.slot",
                "slot_choose": "button.choose",
                "confirm_button": "#final",
                "blockers": ["#captcha"],
            },
        }
    )


@pytest.fixture
def browser_page():
    from exam_booker.booker import Booker  # noqa: F401 - import 확인

    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium 실행 불가: {e}")
        context = browser.new_context()
        yield context, context.new_page()
        browser.close()


def run(cfg, context, page):
    from exam_booker.booker import Booker

    logs = []
    ok = Booker(cfg, context, page, log=logs.append, ask=lambda _: "").run(skip_login=True, skip_wait=True)
    return ok, logs


def test_waits_for_open_then_picks_best_available(browser_page):
    context, page = browser_page
    open_ms = int((time.time() + 2.5) * 1000)  # 2.5초 뒤 접수 시작
    cfg = make_cfg(
        f"{MOCK}?open={open_ms}",
        [
            {"venue": "상암고", "date": "2026-11-21", "time": "09:00"},  # 잔여 0 → 건너뜀
            {"venue": "상암고", "date": "11월 21일"},  # 13:00 잔여 3 → 선택
            {"venue": "개포고"},
        ],
    )
    ok, logs = run(cfg, context, page)
    assert ok
    assert "13:00 서울 마포 상암고" in page.inner_text("#result")
    assert "접수 완료" not in page.inner_text("#result")  # 최종 확정은 사람 몫
    assert any("아직 접수 전" in m for m in logs)
    assert any("2순위" in m for m in logs)


def test_auto_confirm_clicks_final(browser_page):
    context, page = browser_page
    cfg = make_cfg(f"{MOCK}?open=0", [{"venue": "개포고"}], auto_confirm=True)
    ok, _ = run(cfg, context, page)
    assert ok
    assert "개포고 / 접수 완료" in page.inner_text("#result")


def test_pauses_on_captcha_until_human_clears_it(browser_page):
    context, page = browser_page
    cfg = make_cfg(f"{MOCK}?open=0&captcha=1", [{"venue": "개포고"}])

    # 사람이 3초 뒤 보안문자를 처리하는 상황을 흉내 (스레드에서 페이지를 직접 만질 수 없어 플래그만 세움)
    done = threading.Event()
    threading.Timer(3.0, done.set).start()

    from exam_booker.booker import Booker

    logs = []

    def sleep(s):
        if done.is_set() and page.locator("#captcha").is_visible():
            page.click("#captcha-ok")
        page.wait_for_timeout(int(s * 1000))

    start = time.time()
    ok = Booker(cfg, context, page, log=logs.append, ask=lambda _: "", sleep=sleep).run(skip_login=True, skip_wait=True)
    assert ok
    assert time.time() - start >= 3.0
    assert any("보안문자" in m for m in logs)


def test_gives_up_when_no_preferred_slot(browser_page):
    context, page = browser_page
    cfg = make_cfg(f"{MOCK}?open=0", [{"venue": "부평고"}])  # 마감뿐
    cfg = cfg.__class__(**{**cfg.__dict__, "give_up_after_minutes": 0.05})
    ok, logs = run(cfg, context, page)
    assert not ok
    assert any("빈자리가 없습니다" in m for m in logs)
