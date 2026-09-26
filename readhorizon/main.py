"""FastAPI 앱: REST API + 정적 웹 화면."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .analysis.engine import AnalysisEngine, AnalysisRequestError, WorkContext
from .analysis.llm import ClaudeClient, DemoClient, LLMClient, LLMError, LLMRefusal
from .analysis.presets import KINDS, TRANSLATE_TARGETS, categories_payload, find_genre
from .catalog.bibliographic import BibliographicError, NationalLibraryClient
from .config import Settings, load_settings
from .db import Store
from .ingest.normalize import looks_like_verse, normalize_text
from .ingest.parsers import ParseError, parse_upload
from .privacy.masking import mask, merge_counts
from .privacy.notice import build_notice
from .references.store import ReferenceIndex

WORKSPACE_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MULTIPLE_CHOICE_RE = re.compile(r"[①②③④⑤]|^\s*[1-5][.)]\s", re.MULTILINE)
STATIC_DIR = Path(__file__).parent / "static"
MAX_INTRO_CHARS = 5000
MAX_TEXT_CHARS = 2_000_000
BIB_KEYS = ("title", "author", "publisher", "isbn", "kdc", "pages", "publish_date")
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class AnalyzeBody(BaseModel):
    kind: str
    option: str = ""
    refresh: bool = False


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


def workspace_id(x_workspace_id: str | None = Header(default=None)) -> str:
    value = (x_workspace_id or "").strip().lower()
    if not WORKSPACE_RE.match(value):
        raise HTTPException(status_code=400, detail="자료실 ID가 올바르지 않습니다. 페이지를 새로고침해 주세요.")
    return value


def _clean_bib(raw: str) -> dict | None:
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="서지정보 형식이 올바르지 않습니다.")
    if not isinstance(data, dict):
        return None
    cleaned = {k: str(data[k])[:300] for k in BIB_KEYS if data.get(k) not in (None, "")}
    return cleaned or None


def _paragraph_dicts(paragraphs) -> list[dict]:
    return [{"index": p.index, "label": p.label, "section": p.section, "text": p.text} for p in paragraphs]


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    store: Store | None = None,
    bib_client: NationalLibraryClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    if llm is None:
        llm = ClaudeClient(settings.model, settings.effort) if settings.llm_mode == "live" else DemoClient()
    if bib_client is None and settings.nl_cert_key:
        bib_client = NationalLibraryClient(settings.nl_cert_key)
    engine = AnalysisEngine(llm, ReferenceIndex(store), settings.fulltext_char_limit)
    store.purge_inactive(settings.retention_days)

    app = FastAPI(title="ReadHorizon", version=__version__)
    app.state.settings, app.state.store, app.state.engine = settings, store, engine

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(ParseError)
    @app.exception_handler(AnalysisRequestError)
    @app.exception_handler(LLMRefusal)
    async def unprocessable(_: Request, exc: Exception):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(LLMError)
    @app.exception_handler(BibliographicError)
    async def upstream_failed(_: Request, exc: Exception):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def load_context(ws: str, work_id: str) -> tuple[dict, WorkContext]:
        work = store.get_work(ws, work_id)
        if work is None:
            raise HTTPException(status_code=404, detail="작품을 찾을 수 없습니다.")
        found = find_genre(work["category"], work["genre"])
        if found is None:
            raise HTTPException(status_code=500, detail="작품 분류 정보가 손상되었습니다.")
        category, genre = found
        paragraphs = store.get_paragraphs(work_id)
        return work, WorkContext(work["title"], work["author"], category, genre, paragraphs, work["intro_text"])

    # ── 기본 정보 ─────────────────────────────────────────
    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "mode": llm.mode,
            "model": getattr(llm, "model", None),
            "bib_enabled": bib_client is not None,
            "retention_days": settings.retention_days,
        }

    @app.get("/api/categories")
    def categories() -> dict:
        return {
            "categories": categories_payload(),
            "kinds": {k: v["label"] for k, v in KINDS.items()},
            "translate_targets": TRANSLATE_TARGETS,
        }

    @app.get("/api/bib/search")
    def bib_search(q: str = "", isbn: str = "") -> dict:
        if bib_client is None:
            return {"enabled": False, "results": []}
        records = bib_client.search(query=q or None, isbn=isbn or None)
        return {"enabled": True, "results": [r.to_dict() for r in records]}

    @app.get("/api/references")
    def references() -> dict:
        return {"references": store.list_references()}

    @app.get("/api/privacy/notice")
    def privacy_notice(ws: str = Depends(workspace_id)) -> dict:
        return build_notice(store.masking_summary(ws, 30), settings.retention_days, bib_client is not None, llm.mode)

    # ── 작품 ──────────────────────────────────────────────
    @app.post("/api/works", status_code=201)
    def create_work(
        ws: str = Depends(workspace_id),
        category: str = Form(...),
        genre: str = Form(...),
        title: str = Form(""),
        author: str = Form(""),
        text: str = Form(""),
        intro_text: str = Form(""),
        intro_source_url: str = Form(""),
        bib_json: str = Form(""),
        file: UploadFile | None = File(None),
    ) -> dict:
        found = find_genre(category, genre)
        if found is None:
            raise HTTPException(status_code=422, detail="카테고리 또는 장르가 올바르지 않습니다.")
        _, genre_preset = found

        raw_text, title_hint, filename = "", None, None
        if file is not None and file.filename:
            if text.strip():
                raise HTTPException(status_code=422, detail="파일 업로드와 원문 붙여넣기 중 하나만 사용해 주세요.")
            limit = settings.max_upload_mb * 1024 * 1024
            data = file.file.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다(최대 {settings.max_upload_mb}MB).")
            parsed = parse_upload(file.filename, data)
            raw_text, title_hint, filename = parsed.text, parsed.title_hint, Path(file.filename).name[:200]
        else:
            raw_text = text
        if len(raw_text) > MAX_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="원문이 너무 깁니다. 나눠서 올려 주세요.")
        if len(intro_text) > MAX_INTRO_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"책 소개는 {MAX_INTRO_CHARS}자까지 붙여넣을 수 있습니다. 작품 원문은 원문 입력란을 이용해 주세요.",
            )
        url = intro_source_url.strip()
        if url and (not url.startswith(("https://", "http://")) or len(url) > 500):
            raise HTTPException(status_code=422, detail="출처 URL은 http(s)로 시작하는 500자 이하 주소여야 합니다.")

        masked_text, masked_intro = mask(raw_text), mask(intro_text.strip())
        masked_title, masked_author = mask(title.strip()), mask(author.strip())

        verse = genre_preset.verse == "yes" or (genre_preset.verse == "auto" and looks_like_verse(masked_text.text))
        paragraphs = normalize_text(masked_text.text, verse) if masked_text.text.strip() else []
        if raw_text.strip() and not paragraphs:
            raise HTTPException(status_code=422, detail="원문에서 텍스트를 찾지 못했습니다.")
        if not paragraphs and not masked_intro.text:
            raise HTTPException(status_code=422, detail="원문 파일, 원문 붙여넣기, 책 소개 중 하나는 입력해 주세요.")

        bib = _clean_bib(bib_json)
        final_title = (
            masked_title.text
            or (bib or {}).get("title")
            or title_hint
            or (paragraphs[0].text[:30] if paragraphs else "")
            or "제목 없음"
        )[:200]
        work_id = store.create_work(
            ws,
            title=final_title,
            author=(masked_author.text or (bib or {}).get("author") or None),
            category=category,
            genre=genre,
            verse=verse,
            source_kind="upload" if filename else ("paste" if paragraphs else "intro_only"),
            source_filename=filename,
            intro_text=masked_intro.text or None,
            intro_source_url=url or None,
            bib=bib,
            paragraphs=paragraphs,
            mask_counts=merge_counts(masked_text, masked_intro, masked_title, masked_author),
        )
        return work_detail(work_id, ws)

    @app.get("/api/works")
    def list_works(ws: str = Depends(workspace_id)) -> dict:
        return {"works": store.list_works(ws)}

    @app.get("/api/works/{work_id}")
    def work_detail(work_id: str, ws: str = Depends(workspace_id)) -> dict:
        work = store.get_work(ws, work_id)
        if work is None:
            raise HTTPException(status_code=404, detail="작품을 찾을 수 없습니다.")
        return {
            "work": work,
            "paragraphs": _paragraph_dicts(store.get_paragraphs(work_id)),
            "analyses": store.list_analyses(work_id),
            "questions": store.list_questions(work_id),
        }

    @app.delete("/api/works/{work_id}", status_code=204)
    def delete_work(work_id: str, ws: str = Depends(workspace_id)) -> Response:
        if not store.delete_work(ws, work_id):
            raise HTTPException(status_code=404, detail="작품을 찾을 수 없습니다.")
        return Response(status_code=204)

    @app.post("/api/works/{work_id}/analyze")
    def analyze(work_id: str, body: AnalyzeBody, ws: str = Depends(workspace_id)) -> dict:
        _, ctx = load_context(ws, work_id)
        option = ""
        if body.kind == "translate":
            option = body.option if body.option in TRANSLATE_TARGETS else "easy"
        if not body.refresh:
            cached = store.get_analysis(work_id, body.kind, option)
            if cached is not None:
                return {**cached, "cached": True}
        result = engine.analyze(ctx, body.kind, option)
        store.save_analysis(work_id, body.kind, result["option"], result)
        return {**result, "cached": False}

    @app.post("/api/works/{work_id}/ask")
    def ask(work_id: str, body: AskBody, ws: str = Depends(workspace_id)) -> dict:
        _, ctx = load_context(ws, work_id)
        masked = mask(body.question)
        result = engine.ask(ctx, masked.text, bool(MULTIPLE_CHOICE_RE.search(masked.text)))
        qid = store.save_question(ws, work_id, masked.text, result, masked.counts)
        return {"id": qid, "question": masked.text, "result": result}

    # ── 웹 화면 ───────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app
