from datetime import datetime, timezone

import pytest

from exam_booker import clock
from exam_booker.config import KST, ConfigError, Preference, parse_config
from exam_booker.matching import is_full, normalize, pick_slot, row_matches

BASE = {
    "apply_url": "https://example.com/apply",
    "open_at": "2026-11-02 10:00:00",
    "preferences": [{"venue": "상암고", "date": "2026-11-21"}],
    "selectors": {"apply_button": "#a", "slot_row": "tr", "slot_choose": "button"},
}


def test_parse_defaults_to_kst_and_manual_confirm():
    cfg = parse_config(BASE)
    assert cfg.open_at == datetime(2026, 11, 2, 10, tzinfo=KST)
    assert cfg.auto_confirm is False
    assert cfg.login_url == BASE["apply_url"]


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"poll_interval": 0.2}, "poll_interval"),
        ({"preferences": []}, "preferences"),
        ({"preferences": [{"venu": "x"}]}, "알 수 없는 키"),
        ({"open_at": "내일 아침"}, "open_at"),
        ({"auto_confirm": True}, "confirm_button"),
    ],
)
def test_parse_rejects_bad_config(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({**BASE, **patch})


def test_missing_required_key():
    raw = dict(BASE)
    del raw["selectors"]
    with pytest.raises(ConfigError, match="selectors"):
        parse_config(raw)


def test_normalize_date_formats():
    assert normalize("2026. 11. 21") == normalize("2026-11-21") == normalize("2026/11/21")
    assert normalize("2026년 11월 21일") == "2026-11-21"
    assert row_matches("2026. 11. 21 (토) 09:00 서울 마포 상암고", Preference("상암고", "11월 21일", "09:00"))
    assert not row_matches("2026. 11. 22 09:00 서울 마포 상암고", Preference("상암고", "2026-11-21"))


@pytest.mark.parametrize(
    "text, full",
    [
        ("상암고 잔여 0", True),
        ("상암고 잔여 10", False),
        ("상암고 0석", True),
        ("상암고 10석", False),
        ("상암고 0/30", True),
        ("상암고 10/30", False),
        ("부평고 마감", True),
    ],
)
def test_is_full(text, full):
    assert is_full(text, ["마감"]) is full


def test_pick_slot_respects_priority_and_skips_full():
    rows = [
        "2026. 11. 21 09:00 서울 상암고 잔여 0",
        "2026. 11. 21 13:00 서울 상암고 잔여 3",
        "2026. 11. 22 09:00 서울 개포고 잔여 12",
    ]
    prefs = [Preference("상암고", "2026-11-21", "09:00"), Preference("개포고"), Preference("상암고")]
    idx, pref = pick_slot(rows, prefs, ["마감"])
    assert (idx, pref) == (2, prefs[1])  # 1순위는 만석 → 2순위 개포고
    assert pick_slot(rows, [Preference("부평고")], ["마감"]) is None


def test_offset_from_date_header():
    local = datetime(2026, 11, 2, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    off = clock.offset_from_date_header("Mon, 02 Nov 2026 01:00:03 GMT", local)
    assert off == pytest.approx(3.5)
    assert clock.offset_from_date_header(None, local) is None
    assert clock.offset_from_date_header("garbage", local) is None


def test_wait_until_uses_offset():
    t = [1000.0]
    target = datetime.fromtimestamp(1010, tz=timezone.utc)

    def sleep(s):
        t[0] += s

    clock.wait_until(target, offset=4.0, sleep=sleep, now=lambda: t[0])
    # 서버가 4초 빠르므로 로컬 기준 1006초 무렵 도달
    assert 1005.9 <= t[0] <= 1006.01
