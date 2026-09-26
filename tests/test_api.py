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


def _multipart(fields: dict) -> dict:
    """브라우저 FormData와 같은 multipart 형식으로 보낸다(파일이 아닌 필드는 (None, 값))."""
    return {k: (None, v) for k, v in fields.items()}


def _create(client, headers, **fields):
    data = {"category": "literature", "genre": "poem", **fields}
    return client.post("/api/works", files=_multipart(data), headers=headers)


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
    assert translate["option"] == "en:1" and translate["part"] == 1 and translate["parts"] == 1
    assert translate["checks"]["items"][0] == {
        "id": "coverage", "status": "ok", "label": "문단 누락", "detail": "모든 부분(9개)에 원문 인용이 붙었습니다",
    }

    detail = client.get(f"/api/works/{work_id}", headers=workspace).json()
    assert {(a["kind"], a["option"]) for a in detail["analyses"]} == {("overview", ""), ("translate", "en:1")}
    assert detail["translate_parts"] == [{"part": 1, "from": "1연 1행", "to": "2연 1행", "chars": sum(len(p["text"]) for p in detail["paragraphs"])}]


def test_long_translation_is_split_into_parts(settings, store, workspace):
    from dataclasses import replace

    app = create_app(replace(settings, translate_part_chars=1000), llm=DemoClient(), store=store)
    client = TestClient(app)
    text = "\n\n".join(f"{n}번째 문단입니다. 1990년에서 20{n:02d}년까지의 일을 적습니다. " * 6 for n in range(1, 13))
    work_id = client.post(
        "/api/works", files=_multipart({"category": "custom", "genre": "general", "title": "긴 글", "text": text}), headers=workspace
    ).json()["work"]["id"]

    parts = client.get(f"/api/works/{work_id}", headers=workspace).json()["translate_parts"]
    assert len(parts) > 1 and parts[0]["from"] == "1문단" and all(p["chars"] <= 1000 for p in parts)

    second = client.post(f"/api/works/{work_id}/analyze", json={"kind": "translate", "option": "modern:2"}, headers=workspace).json()
    assert second["option"] == "modern:2" and second["part"] == 2 and second["parts"] == len(parts)
    cited = {i for s in second["segments"] for c in s["citations"] if c["source"] == "work" for i in c["paragraphs"]}
    assert cited and min(cited) > 0  # 1부분 문단은 보내지 않는다
    assert second["part_range"]["from"] == parts[1]["from"]

    missing = client.post(f"/api/works/{work_id}/analyze", json={"kind": "translate", "option": "modern:99"}, headers=workspace)
    assert missing.status_code == 422 and "부분" in missing.json()["detail"]
    detail = client.get(f"/api/works/{work_id}", headers=workspace).json()
    assert [a["option"] for a in detail["analyses"]] == ["modern:2"]


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


# ── 배포 전 점검에서 보강한 동작 ────────────────────────────────


def test_large_pasted_text_over_starlette_default_field_limit(client, workspace):
    """붙여넣은 원문이 1MB(Starlette 기본 필드 한도)를 넘어도 업로드 한도 안이면 받는다."""
    paragraph = "가나다라마바사 아자차카타파하 문장입니다. " * 30
    text = "\n\n".join([paragraph] * 900)
    assert 1024 * 1024 < len(text.encode()) < 2 * 1024 * 1024
    response = client.post(
        "/api/works", files=_multipart({"category": "literature", "genre": "novel", "text": text}), headers=workspace
    )
    assert response.status_code == 201, response.text
    assert len(response.json()["paragraphs"]) >= 900


def test_pasted_text_over_upload_limit_is_rejected_in_korean(client, workspace):
    text = "가" * (900 * 1024)  # UTF-8 약 2.6MB > 업로드 한도 2MB, 요청 전체 한도(3MB) 이내
    response = client.post(
        "/api/works", files=_multipart({"category": "literature", "genre": "novel", "text": text}), headers=workspace
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "입력이 너무 큽니다(최대 2MB)."


def test_request_body_size_limit(client, workspace):
    body = b"x" * (3 * 1024 * 1024 + 10)
    response = client.post(
        "/api/works", content=body, headers={**workspace, "Content-Type": "application/octet-stream"}
    )
    assert response.status_code == 413


def test_chunked_request_without_length_is_rejected(client, workspace):
    response = client.post(
        "/api/works", content=iter([b"category=literature"]),
        headers={**workspace, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 411


def test_inputs_are_nfc_normalized_and_cleaned(client, workspace):
    import unicodedata

    nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731
    response = _create(
        client, workspace, title=nfd("서시\n") + "\x00", author=nfd(" 윤동주\t"),
        text=nfd("죽는 날까지 하늘을 우러러\n한 점 부끄럼이 없기를,\n잎새에 이는 바람에도"),
    )
    body = response.json()
    assert body["work"]["title"] == "서시" and body["work"]["author"] == "윤동주"
    assert body["paragraphs"][0]["text"] == "죽는 날까지 하늘을 우러러"


def test_uploaded_filename_is_normalized(client, workspace):
    import unicodedata

    name = unicodedata.normalize("NFD", "진달래꽃.txt")
    files = {"file": (name, io.BytesIO("나 보기가 역겨워\n가실 때에는".encode()), "text/plain")}
    body = client.post("/api/works", data={"category": "literature", "genre": "poem"}, files=files, headers=workspace).json()
    assert body["work"]["title"] == "진달래꽃" and body["work"]["source_filename"] == "진달래꽃.txt"


def test_uppercase_https_source_url_is_accepted(client, workspace):
    response = _create(client, workspace, intro_text="소개", intro_source_url="HTTPS://example.com/a")
    assert response.status_code == 201


def _limited_client(settings, store, **limits):
    from dataclasses import replace

    return TestClient(create_app(settings=replace(settings, **limits), llm=DemoClient(), store=store))


def test_llm_calls_are_rate_limited_per_client(settings, store, workspace, seosi_text):
    client = _limited_client(settings, store, llm_per_minute=2, llm_daily=0)
    work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    url = f"/api/works/{work_id}/analyze"
    assert client.post(url, json={"kind": "overview"}, headers=workspace).status_code == 200
    # 저장된 결과를 돌려주는 요청은 호출 한도에 세지 않는다
    assert client.post(url, json={"kind": "overview"}, headers=workspace).json()["cached"] is True
    assert client.post(url, json={"kind": "flow"}, headers=workspace).status_code == 200
    limited = client.post(url, json={"kind": "guide"}, headers=workspace)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1 and "초 뒤" in limited.json()["detail"]
    blocked_ask = client.post(f"/api/works/{work_id}/ask", json={"question": "주제는?"}, headers=workspace)
    assert blocked_ask.status_code == 429


def test_daily_llm_limit(settings, store, workspace, seosi_text):
    client = _limited_client(settings, store, llm_per_minute=0, llm_daily=1)
    work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    assert client.post(f"/api/works/{work_id}/ask", json={"question": "화자는?"}, headers=workspace).status_code == 200
    response = client.post(f"/api/works/{work_id}/ask", json={"question": "주제는?"}, headers=workspace)
    assert response.status_code == 429 and "오늘" in response.json()["detail"]


def test_work_creation_is_rate_limited(settings, store, workspace):
    client = _limited_client(settings, store, create_per_minute=1)
    assert _create(client, workspace, intro_text="소개").status_code == 201
    assert _create(client, workspace, intro_text="소개").status_code == 429


def test_invalid_kind_rejected_before_llm_call(client, workspace, seosi_text):
    work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    response = client.post(f"/api/works/{work_id}/analyze", json={"kind": "nope"}, headers=workspace)
    assert response.status_code == 422


def test_whitespace_question_is_rejected(client, workspace, seosi_text):
    work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    response = client.post(f"/api/works/{work_id}/ask", json={"question": "   \n"}, headers=workspace)
    assert response.status_code == 422


def test_work_deleted_during_analysis_returns_404(settings, store, workspace, seosi_text):
    class DeletingLLM(DemoClient):
        def generate(self, system, docs, task, meta):
            store.delete_work(workspace["X-Workspace-Id"], self.work_id)
            return super().generate(system, docs, task, meta)

    llm = DeletingLLM()
    client = TestClient(create_app(settings=settings, llm=llm, store=store))
    llm.work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    assert client.post(f"/api/works/{llm.work_id}/analyze", json={"kind": "overview"}, headers=workspace).status_code == 404

    llm.work_id = _create(client, workspace, text=seosi_text).json()["work"]["id"]
    assert client.post(f"/api/works/{llm.work_id}/ask", json={"question": "화자는?"}, headers=workspace).status_code == 404


def test_privacy_notice_mentions_transient_ip_use(client, workspace):
    items = [i["item"] for i in client.get("/api/privacy/notice", headers=workspace).json()["collected_items"]]
    assert "접속 IP" in items
    assert "번역 용어집(작품별 용어·번역어·메모)" in items and "읽는 사람 설정(독자 수준·설명 언어)" in items
