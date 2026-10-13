"""저장 화면 오프라인 점검(check-page)·정리(sanitize) 테스트 — 실제 사이트 접속 없음."""

import os
from pathlib import Path

import pytest

from exam_booker.check_page import mask_text, sanitize_html

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "saved_qnet_like.html"
PREFS = "상암고 | 2026-11-21 | 09:00\n상암고 | 2026-11-21\n개포고"


def test_sanitize_removes_scripts_tokens_and_personal_data():
    clean = sanitize_html(FIXTURE.read_text(encoding="utf-8"))
    for gone in ("<script", "fetch(", "SECRET-TOKEN-123", "홍길동", "010-1234-5678", "test@example.com",
                 "onload=", "onclick=", "javascript:", "http-equiv=\"refresh\"", "900101"):
        assert gone not in clean, gone
    assert "상암고등학교" in clean and "잔여 4" in clean  # 일정 정보는 남김


def test_mask_text():
    assert mask_text("김철수님 900101-1234567") == "***님 ******-*******"


sync_api = pytest.importorskip("playwright.sync_api")


def test_check_page_offline_recommends_and_blocks_network():
    from exam_booker.check_page import check_page

    try:
        r = check_page(FIXTURE, PREFS, site="qnet", executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Chromium 실행 불가: {e}")
    # 1순위(09:00)는 잔여석 칸이 '0'뿐 → 마감으로 판단, 2순위(13:00 잔여 4) 추천
    assert r.best and "13:00" in r.best["text"]
    assert [x["rank"] for x in r.rows][:3] == ["full", "best", "full"]
    assert r.apply_buttons == ["원서접수"]
    assert r.blocked_requests and all("q-net.or.kr" in u for u in r.blocked_requests)
    out = r.render()
    assert "홍길동" not in out and "실제 사이트에는 접속하지 않았습니다" in out


def test_booker_row_texts_reads_seat_column():
    from exam_booker.booker import row_texts
    from exam_booker.matching import is_full

    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium 실행 불가: {e}")
        ctx = b.new_context()
        ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("file:") else r.abort())
        page = ctx.new_page()
        page.goto(FIXTURE.as_uri())
        texts = row_texts(page.locator("table.list tbody tr"))
        b.close()
    assert [is_full(t, ["마감"]) for t in texts] == [True, False, True]
