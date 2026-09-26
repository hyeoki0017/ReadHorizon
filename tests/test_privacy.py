from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from readhorizon.ingest.normalize import Paragraph
from readhorizon.privacy.masking import mask


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("주민번호 900101-1234567 입니다", "resident_id"),
        ("외국인 등록 9001015234567", "resident_id"),
        ("연락처 010-1234-5678", "mobile"),
        ("연락처 01012345678", "mobile"),
        ("사무실 02-123-4567", "phone"),
        ("사무실 031) 123-4567", "phone"),
        ("메일 student.kim@example.ac.kr 로", "email"),
        ("카드 1234-5678-9012-3456", "card"),
    ],
)
def test_masks_sensitive_values(text, code):
    result = mask(text)
    assert result.counts == {code: 1}
    assert "[마스킹:" in result.text
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        assert digits not in "".join(ch for ch in result.text if ch.isdigit())


@pytest.mark.parametrize(
    "text",
    [
        "ISBN 978-89-374-6010-2",
        "1941년 11월 20일에 쓰였다",
        "2024-03-15 발표",
        "쪽수 312쪽, 가격 15,000원",
        "1948년 간행",
    ],
)
def test_does_not_mask_ordinary_numbers(text):
    result = mask(text)
    assert result.counts == {}
    assert result.text == text


def test_mask_empty():
    assert mask(None).text == ""
    assert mask("").total == 0


def test_purge_removes_inactive_works_only(store):
    ws = "11111111-1111-4111-8111-111111111111"
    kwargs = dict(
        author=None, category="literature", genre="poem", verse=True, source_kind="paste",
        source_filename=None, intro_text=None, intro_source_url=None, bib=None,
        paragraphs=[Paragraph(0, "1행", None, "가")], mask_counts={"email": 1},
    )
    old_id = store.create_work(ws, title="오래된 작품", **kwargs)
    new_id = store.create_work(ws, title="최근 작품", **kwargs)

    future = datetime.now(timezone.utc) + timedelta(days=181)
    with store.tx() as conn:
        conn.execute("UPDATE works SET last_accessed_at = ? WHERE id = ?", (future.isoformat(timespec="seconds"), new_id))

    assert store.purge_inactive(180, now=future) == 1
    assert store.get_work(ws, old_id, touch=False) is None
    assert store.get_work(ws, new_id, touch=False) is not None
    assert store.get_paragraphs(old_id) == []
