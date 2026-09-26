"""FastAPI 앱: REST API + 정적 웹 화면."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path, PurePath

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .analysis.engine import AnalysisEngine, AnalysisRequestError, WorkContext
from .analysis.llm import ClaudeClient, DemoClient, LLMClient, LLMError, LLMRefusal
from .analysis.presets import KINDS, TRANSLATE_TARGETS, categories_payload, find_genre
from .catalog.bibliographic import BibliographicError, NationalLibraryClient
from .config import Settings, load_settings
from .db import Store
from .ingest.normalize import clean_line, clean_text, looks_like_verse, normalize_text
from .ingest.parsers import ParseError, parse_upload
from .ocr import OCRInputError, prepare_inputs, run_ocr
from .privacy.masking import mask, merge_counts
from .privacy.notice import build_notice
from .ratelimit import RateLimiter
from .references.store import ReferenceIndex

WORKSPACE_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MULTIPLE_CHOICE_RE = re.compile(r"[①②③④⑤]|^\s*[1-5][.)]\s", re.MULTILINE)
STATIC_DIR = Path(__file__).parent / "static"
MAX_INTRO_CHARS = 5000
MAX_TEXT_CHARS = 2_000_000
FORM_OVERHEAD_BYTES = 1024 * 1024  # 파일 외 입력값·멀티파트 경계용 여유
FORM_FIELDS = (
    "category", "genre", "title", "author", "text", "intro_text", "intro_source_url", "bib_json", "origin",
)
VERSE_GENRES = {"poem", "csat_literature"}
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


class RequestSizeLimitMiddleware:
    """본문이 있는 요청의 크기를 Content-Length로 먼저 확인한다(대용량 업로드로 디스크·메모리를 채우는 것을 막음).

    길이를 밝히지 않은 chunked 요청은 받지 않는다. 브라우저의 fetch/FormData는 항상 Content-Length를 보낸다.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH"):
            length: int | None = None
            chunked = False
            for name, value in scope["headers"]:
                if name == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        return await self._reject(scope, receive, send, 400, "요청 길이 정보가 올바르지 않습니다.")
                elif name == b"transfer-encoding" and b"chunked" in value.lower():
                    chunked = True
            if length is None and chunked:
                return await self._reject(scope, receive, send, 411, "요청 길이(Content-Length)가 필요합니다.")
            if length is not None and length > self.max_bytes:
                limit_mb = self.max_bytes // (1024 * 1024)
                return await self._reject(scope, receive, send, 413, f"요청이 너무 큽니다(최대 약 {limit_mb}MB).")
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, status: int, detail: str) -> None:
        await JSONResponse(status_code=status, content={"detail": detail})(scope, receive, send)


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
        raise HTTPException(status_code=422, detail="서지정보 형식이 올바르지 않습니다.") from None
    if not isinstance(data, dict):
        return None
    cleaned = {k: clean_line(str(data[k]))[:300] for k in BIB_KEYS if data.get(k) not in (None, "")}
    return {k: v for k, v in cleaned.items() if v} or None


def _paragraph_dicts(paragraphs) -> list[dict]:
    return [{"index": p.index, "label": p.label, "section": p.section, "text": p.text} for p in paragraphs]


def _client_key(request: Request) -> str:
    # 리버스 프록시 뒤에서는 uvicorn의 --forwarded-allow-ips 설정에 따라 실제 접속 IP가 들어온다
    return request.client.host if request.client else "unknown"


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    store: Store | None = None,
    bib_client: NationalLibraryClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    if llm is None:
        llm = (
            ClaudeClient(settings.model, settings.effort, ocr_effort=settings.ocr_effort)
            if settings.llm_mode == "live"
            else DemoClient()
        )
    if bib_client is None and settings.nl_cert_key:
        bib_client = NationalLibraryClient(settings.nl_cert_key)
    engine = AnalysisEngine(llm, ReferenceIndex(store), settings.fulltext_char_limit)
    llm_limiter = RateLimiter(settings.llm_per_minute, settings.llm_daily)
    create_limiter = RateLimiter(settings.create_per_minute)
    upload_limit = settings.max_upload_mb * 1024 * 1024
    store.purge_if_due(settings.retention_days)

    app = FastAPI(title="ReadHorizon", version=__version__)
    app.state.settings, app.state.store, app.state.engine = settings, store, engine
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=upload_limit + FORM_OVERHEAD_BYTES)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(ParseError)
    @app.exception_handler(OCRInputError)
    @app.exception_handler(AnalysisRequestError)
    @app.exception_handler(LLMRefusal)
    async def unprocessable(_: Request, exc: Exception):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(LLMError)
    @app.exception_handler(BibliographicError)
    async def upstream_failed(_: Request, exc: Exception):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def enforce(limiter: RateLimiter, request: Request) -> None:
        wait = limiter.check(_client_key(request))
        if wait is None:
            return
        if wait > 60:
            detail = "오늘 처리할 수 있는 요청 한도에 도달했습니다. 내일 다시 시도해 주세요."
        else:
            detail = f"요청이 너무 잦습니다. {wait}초 뒤에 다시 시도해 주세요."
        raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(wait)})

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
        records = bib_client.search(query=clean_line(q) or None, isbn=isbn or None)
        return {"enabled": True, "results": [r.to_dict() for r in records]}

    @app.get("/api/references")
    def references() -> dict:
        return {"references": store.list_references()}

    @app.get("/api/privacy/notice")
    def privacy_notice(ws: str = Depends(workspace_id)) -> dict:
        return build_notice(store.masking_summary(ws, 30), settings.retention_days, bib_client is not None, llm.mode)

    # ── 작품 ──────────────────────────────────────────────
    def create_work_sync(ws: str, fields: dict[str, str], filename: str | None, data: bytes | None) -> dict:
        store.purge_if_due(settings.retention_days)
        found = find_genre(fields["category"], fields["genre"])
        if found is None:
            raise HTTPException(status_code=422, detail="카테고리 또는 장르가 올바르지 않습니다.")
        _, genre_preset = found

        raw_text, title_hint, source_filename = "", None, None
        if data is not None and filename:
            if fields["text"].strip():
                raise HTTPException(status_code=422, detail="파일 업로드와 원문 붙여넣기 중 하나만 사용해 주세요.")
            if len(data) > upload_limit:
                raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다(최대 {settings.max_upload_mb}MB).")
            parsed = parse_upload(filename, data)
            raw_text, title_hint = parsed.text, parsed.title_hint
            source_filename = PurePath(clean_line(filename).replace("\\", "/")).name[:200] or None
        else:
            raw_text = fields["text"]
        raw_text = clean_text(raw_text)
        if len(raw_text) > MAX_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="원문이 너무 깁니다. 나눠서 올려 주세요.")
        intro_text = clean_text(fields["intro_text"]).strip()
        if len(intro_text) > MAX_INTRO_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"책 소개는 {MAX_INTRO_CHARS}자까지 붙여넣을 수 있습니다. 작품 원문은 원문 입력란을 이용해 주세요.",
            )
        url = clean_line(fields["intro_source_url"])
        if url and (not url.lower().startswith(("https://", "http://")) or len(url) > 500):
            raise HTTPException(status_code=422, detail="출처 URL은 http(s)로 시작하는 500자 이하 주소여야 합니다.")

        masked_text, masked_intro = mask(raw_text), mask(intro_text)
        masked_title, masked_author = mask(clean_line(fields["title"])), mask(clean_line(fields["author"]))

        verse = genre_preset.verse == "yes" or (genre_preset.verse == "auto" and looks_like_verse(masked_text.text))
        paragraphs = normalize_text(masked_text.text, verse) if masked_text.text.strip() else []
        if raw_text.strip() and not paragraphs:
            raise HTTPException(status_code=422, detail="원문에서 텍스트를 찾지 못했습니다.")
        if not paragraphs and not masked_intro.text:
            raise HTTPException(status_code=422, detail="원문 파일, 원문 붙여넣기, 책 소개 중 하나는 입력해 주세요.")

        if source_filename:
            source_kind = "upload"
        elif paragraphs:
            source_kind = "ocr" if fields["origin"] == "ocr" else "paste"  # 이미지에서 추출해 사용자가 확인한 원문
        else:
            source_kind = "intro_only"

        bib = _clean_bib(fields["bib_json"])
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
            author=((masked_author.text or (bib or {}).get("author") or "")[:100] or None),
            category=fields["category"],
            genre=fields["genre"],
            verse=verse,
            source_kind=source_kind,
            source_filename=source_filename,
            intro_text=masked_intro.text or None,
            intro_source_url=url or None,
            bib=bib,
            paragraphs=paragraphs,
            mask_counts=merge_counts(masked_text, masked_intro, masked_title, masked_author),
        )
        return work_detail(work_id, ws)

    @app.post("/api/works", status_code=201)
    async def create_work(request: Request, ws: str = Depends(workspace_id)) -> dict:
        enforce(create_limiter, request)
        try:
            # 붙여넣은 원문도 업로드 한도까지 받도록 필드 크기 상한을 올린다(기본 1MB)
            form = await request.form(max_files=1, max_fields=20, max_part_size=upload_limit)
        except StarletteHTTPException as exc:
            if "maximum size" in str(exc.detail):
                raise HTTPException(
                    status_code=413, detail=f"입력이 너무 큽니다(최대 {settings.max_upload_mb}MB)."
                ) from None
            raise HTTPException(status_code=400, detail="요청 형식이 올바르지 않습니다.") from None
        try:
            fields = {k: v if isinstance(v := form.get(k), str) else "" for k in FORM_FIELDS}
            upload = form.get("file")
            filename, data = None, None
            if isinstance(upload, StarletteUploadFile) and upload.filename:
                filename, data = upload.filename, await upload.read(upload_limit + 1)
        finally:
            await form.close()
        return await run_in_threadpool(create_work_sync, ws, fields, filename, data)

    @app.get("/api/works")
    def list_works(ws: str = Depends(workspace_id)) -> dict:
        store.purge_if_due(settings.retention_days)
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
    def analyze(work_id: str, body: AnalyzeBody, request: Request, ws: str = Depends(workspace_id)) -> dict:
        _, ctx = load_context(ws, work_id)
        if body.kind not in KINDS:
            raise HTTPException(status_code=422, detail="알 수 없는 분석 종류입니다.")
        option = ""
        if body.kind == "translate":
            option = body.option if body.option in TRANSLATE_TARGETS else "easy"
        if not body.refresh:
            cached = store.get_analysis(work_id, body.kind, option)
            if cached is not None:
                return {**cached, "cached": True}
        enforce(llm_limiter, request)
        result = engine.analyze(ctx, body.kind, option)
        try:
            store.save_analysis(work_id, body.kind, result["option"], result)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=404, detail="분석 중에 작품이 삭제되었습니다.") from None
        return {**result, "cached": False}

    @app.post("/api/works/{work_id}/ask")
    def ask(work_id: str, body: AskBody, request: Request, ws: str = Depends(workspace_id)) -> dict:
        _, ctx = load_context(ws, work_id)
        masked = mask(clean_text(body.question).strip())
        if not masked.text:
            raise HTTPException(status_code=422, detail="질문을 입력해 주세요.")
        enforce(llm_limiter, request)
        result = engine.ask(ctx, masked.text, bool(MULTIPLE_CHOICE_RE.search(masked.text)))
        try:
            qid = store.save_question(ws, work_id, masked.text, result, masked.counts)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=404, detail="답변 중에 작품이 삭제되었습니다.") from None
        return {"id": qid, "question": masked.text, "result": result}

    # ── 이미지·스캔 PDF 텍스트 추출(OCR) ─────────────────
    @app.post("/api/ocr")
    async def ocr(request: Request, ws: str = Depends(workspace_id)) -> dict:
        """이미지·스캔 PDF에서 글자를 추출해 돌려준다. 아무것도 저장하지 않는다(가린 항목 개수만 기록)."""
        enforce(create_limiter, request)  # 이미지 해석(CPU)도 IP별로 제한한다. LLM 호출 제한은 아래에서 따로 적용
        try:
            form = await request.form(max_files=50, max_fields=10, max_part_size=64 * 1024)
        except StarletteHTTPException:
            raise HTTPException(status_code=400, detail="요청 형식이 올바르지 않습니다.") from None
        try:
            files: list[tuple[str, bytes]] = []
            for item in form.getlist("files"):
                if isinstance(item, StarletteUploadFile) and item.filename:
                    data = await item.read(upload_limit + 1)
                    if len(data) > upload_limit:
                        raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다(최대 {settings.max_upload_mb}MB).")
                    files.append((item.filename, data))
            scope = form.get("scope") if isinstance(form.get("scope"), str) else "all"
            genre = form.get("genre") if isinstance(form.get("genre"), str) else ""
        finally:
            await form.close()
        inputs = await run_in_threadpool(prepare_inputs, files, settings.ocr_max_images, settings.ocr_max_pages)
        enforce(llm_limiter, request)
        result = await run_in_threadpool(run_ocr, llm, inputs, scope, genre in VERSE_GENRES)
        await run_in_threadpool(store.log_masking, ws, result["masked"])
        return result

    # ── 웹 화면 ───────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app
