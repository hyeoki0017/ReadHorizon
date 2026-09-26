"""API 통합 테스트(데모 LLM 사용)."""

from __future__ import annotations

import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from readhorizon.analysis.llm import DemoClient
from readhorizon.catalog.bibliographic import BibRecord
from readhorizon.main import create_app


def _create(client, headers, **fields):
    data = {"category": "literature", "genre": "poem", **fields}
    return client.post("/api/works", data=data, headers=headers)


def test_health_and_categories(client):
    health = client.get("/api/health").json()
    assert health["mode"] == "demo" and health["bib_enabled"] is False
    meta = client.get("/api/categories").json()
    assert [c["id"] for c in meta["categories"]] == ["literature", "study", "custom"]
    assert set(meta["kinds"]) == {"overview", "flow", "translate", "guide"}


def test_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_workspace_header_required(client):
    assert client.get("/api/works").status_code == 400
    assert client.get("/api/works", headers={"X-Workspace-Id": "../etc"}).status_code == 400


def test_create_work_from_paste_and_analyze(client, workspace, seosi_text):
    response = _create(client, workspace, title="서시", author="윤동주", text=seosi_text)
    assert response.status_code == 201, response.text
    body = response.json()
    work_id = body["work"]["id"]
    assert body["work"]["verse"] is True and body["work"]["source_kind"] == "paste"
    assert len(body["paragraphs"]) == 9

    result = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview"}, headers=workspace).json()
    assert result["mode"] == "demo" and result["cached"] is False
    work_citations = [c for s in result["segments"] for c in s["citations"] if c["source"] == "work"]
    assert work_citations and all(c["verified"] for c in work_citations)

    cached = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview"}, headers=workspace).json()
    assert cached["cached"] is True
    refreshed = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview", "refresh": True}, headers=workspace).json()
    assert refreshed["cached"] is False

    translate = client.post(f"/api/works/{work_id}/analyze", json={"kind": "translate", "option": "en"}, headers=workspace).json()
    assert translate["option"] == "en"

    detail = client.get(f"/api/works/{work_id}", headers=workspace).json()
    assert {(a["kind"], a["option"]) for a in detail["analyses"]} == {("overview", ""), ("translate", "en")}


def test_ask_saves_question_and_masks_it(client, workspace, seosi_text):
    work_id = _create(client, workspace, title="서시", text=seosi_text).json()["work"]["id"]
    response = client.post(
        f"/api/works/{work_id}/ask",
        json={"question": "별과 바람은 무엇을 뜻하나요? 답은 test@example.com 으로"},
        headers=workspace,
    )
    assert response.status_code == 200
    data = response.json()
    assert "test@example.com" not in data["question"] and "[마스킹:이메일]" in data["question"]
    assert any(c["source"] == "work" for s in data["result"]["segments"] for c in s["citations"])

    detail = client.get(f"/api/works/{work_id}", headers=workspace).json()
    assert len(detail["questions"]) == 1

    notice = client.get("/api/privacy/notice", headers=workspace).json()
    assert {"category": "email", "label": "이메일", "count": 1} in notice["masking_detected"]


def test_upload_file_and_masking(client, workspace):
    text = "첫 문단입니다. 연락처는 010-1234-5678 입니다.\n\n둘째 문단입니다."
    files = {"file": ("강의자료.txt", io.BytesIO(text.encode("utf-8")), "text/plain")}
    data = {"category": "custom", "genre": "lecture"}
    response = client.post("/api/works", data=data, files=files, headers=workspace)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["work"]["title"] == "강의자료" and body["work"]["source_kind"] == "upload"
    joined = " ".join(p["text"] for p in body["paragraphs"])
    assert "010-1234-5678" not in joined and "[마스킹:휴대전화번호]" in joined


def test_upload_rejects_hwp_and_large_files(client, workspace):
    files = {"file": ("a.hwp", io.BytesIO(b"\xd0\xcf\x11\xe0"), "application/octet-stream")}
    response = client.post("/api/works", data={"category": "literature", "genre": "novel"}, files=files, headers=workspace)
    assert response.status_code == 422 and "HWPX" in response.json()["detail"]

    big = io.BytesIO(b"a" * (2 * 1024 * 1024 + 10))
    response = client.post(
        "/api/works", data={"category": "literature", "genre": "novel"}, files={"file": ("big.txt", big, "text/plain")},
        headers=workspace,
    )
    assert response.status_code == 413


def test_intro_only_work(client, workspace):
    intro = "윤동주의 유고 시집. 별과 바람과 하늘을 노래한 시들이 실려 있다."
    body = _create(
        client, workspace, title="하늘과 바람과 별과 시", intro_text=intro,
        intro_source_url="https://example.com/book/1",
    ).json()
    work_id = body["work"]["id"]
    assert body["work"]["source_kind"] == "intro_only" and body["paragraphs"] == []

    blocked = client.post(f"/api/works/{work_id}/analyze", json={"kind": "overview"}, headers=workspace)
    assert blocked.status_code == 422 and "읽기 가이드" in blocked.json()["detail"]

    guide = client.post(f"/api/works/{work_id}/analyze", json={"kind": "guide"}, headers=workspace).json()
    assert guide["badge"]["level"] == "intro"
    assert any(c["source"] == "intro" for s in guide["segments"] for c in s["citations"])


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({}, 422),  # 원문·소개 없음
        ({"text": "가", "category": "nope"}, 422),
        ({"intro_text": "가" * 5001}, 422),
        ({"intro_text": "소개", "intro_source_url": "javascript:alert(1)"}, 422),
        ({"text": "   \n  "}, 422),
    ],
)
def test_create_work_validation(client, workspace, fields, status):
    assert _create(client, workspace, **fields).status_code == status


def test_workspaces_are_isolated(client, workspace, seosi_text):
    work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    other = {"X-Workspace-Id": str(uuid.uuid4())}
    assert client.get("/api/works", headers=other).json()["works"] == []
    assert client.get(f"/api/works/{work_id}", headers=other).status_code == 404
    assert client.post(f"/api/works/{work_id}/ask", json={"question": "?"}, headers=other).status_code == 404
    assert client.delete(f"/api/works/{work_id}", headers=other).status_code == 404

    assert client.delete(f"/api/works/{work_id}", headers=workspace).status_code == 204
    assert client.get(f"/api/works/{work_id}", headers=workspace).status_code == 404


def test_bib_search_disabled_without_key(client):
    assert client.get("/api/bib/search?q=서시").json() == {"enabled": False, "results": []}


def test_bib_search_and_bib_json_whitelist(settings, store, workspace, seosi_text):
    class FakeBib:
        def search(self, query=None, isbn=None, size=10):
            return [BibRecord("하늘과 바람과 별과 시", "윤동주", "정음사", "9788900000000", "811.6", "120", "1948",
                              "literature", "poem")]

    client = TestClient(create_app(settings=settings, llm=DemoClient(), store=store, bib_client=FakeBib()))
    results = client.get("/api/bib/search?q=하늘").json()
    assert results["enabled"] is True and results["results"][0]["suggested_genre"] == "poem"

    bib = {**results["results"][0], "evil": "<script>", "title": "하늘과 바람과 별과 시"}
    body = _create(client, workspace, text=seosi_text, bib_json=json.dumps(bib)).json()
    assert body["work"]["title"] == "하늘과 바람과 별과 시"  # 제목을 비우면 서지정보 제목 사용
    assert "evil" not in body["work"]["bib"] and body["work"]["bib"]["kdc"] == "811.6"
