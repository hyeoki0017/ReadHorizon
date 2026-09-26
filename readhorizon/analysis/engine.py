"""분석 엔진: 문서 구성 → LLM 호출 → 인용 검증 → 신뢰도 배지.

문서 순서(인용 번호 = 이 순서): [작품 원문] + [참고자료...] + [사용자 제공 소개]
작품 원문 문서에만 cache_control을 걸어 시스템 프롬프트 + 원문까지를 캐시한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..ingest.normalize import Paragraph, total_chars
from ..references.search import BM25
from ..references.store import ReferenceIndex
from ..trust import INTRO_LABEL, INTRO_TIER, analysis_badge, tier_label
from . import prompts
from .grounding import is_supported, strip_label, summarize
from .llm import DocSpec, LLMClient, LLMResult
from .presets import KINDS, TRANSLATE_TARGETS, Category, Genre


class AnalysisRequestError(ValueError):
    """잘못된 분석 요청(사용자에게 보여줄 메시지)."""


@dataclass
class WorkContext:
    title: str
    author: str | None
    category: Category
    genre: Genre
    paragraphs: list[Paragraph]
    intro_text: str | None

    @property
    def intro_only(self) -> bool:
        return not self.paragraphs


def block_text(p: Paragraph) -> str:
    return f"[{p.label}] {p.text}"


def select_paragraphs(paragraphs: list[Paragraph], limit: int, query: str | None) -> tuple[list[Paragraph], bool]:
    """긴 작품은 글자 수 한도 안에서 문단을 고른다. 질문이 있으면 관련 문단과 앞뒤 문단을 우선한다."""
    if total_chars(paragraphs) <= limit:
        return paragraphs, False

    chosen: set[int] = set()
    budget = 0
    if query:
        ranked = BM25([p.text for p in paragraphs]).top(query, len(paragraphs))
        for idx, _ in ranked:
            for neighbor in (idx - 1, idx, idx + 1):
                if 0 <= neighbor < len(paragraphs) and neighbor not in chosen:
                    size = len(paragraphs[neighbor].text)
                    if budget + size > limit:
                        break
                    chosen.add(neighbor)
                    budget += size
            if budget >= limit:
                break
    if not chosen:
        average = max(1, total_chars(paragraphs) // len(paragraphs))
        stride = max(1, math.ceil(len(paragraphs) / max(1, limit // average)))
        for idx in range(0, len(paragraphs), stride):
            size = len(paragraphs[idx].text)
            if budget + size > limit:
                break
            chosen.add(idx)
            budget += size
    return [paragraphs[i] for i in sorted(chosen)], True


class AnalysisEngine:
    def __init__(self, llm: LLMClient, references: ReferenceIndex | None, fulltext_char_limit: int) -> None:
        self.llm = llm
        self.references = references
        self.limit = fulltext_char_limit

    # ── 문서 구성 ─────────────────────────────────────────
    def _documents(self, ctx: WorkContext, query: str | None, ref_query: str) -> tuple[list[DocSpec], bool]:
        docs: list[DocSpec] = []
        excerpted = False
        if ctx.paragraphs:
            selected, excerpted = select_paragraphs(ctx.paragraphs, self.limit, query)
            docs.append(
                DocSpec(
                    role="work",
                    title=f"작품 원문: {ctx.title}" + (" (발췌)" if excerpted else ""),
                    blocks=[block_text(p) for p in selected],
                    index_map=[p.index for p in selected],
                    display_title=ctx.title,
                    cache=True,
                )
            )
        if self.references is not None:
            for hit in self.references.search(ref_query, ctx.title):
                docs.append(
                    DocSpec(
                        role="reference",
                        title=f"참고자료 [{hit.tier}등급: {tier_label(hit.tier)}] {hit.title} — {hit.source}",
                        text=hit.text,
                        tier=hit.tier,
                        ref_id=hit.ref_id,
                        display_title=f"{hit.title} — {hit.source}",
                    )
                )
        if ctx.intro_text:
            docs.append(
                DocSpec(
                    role="intro",
                    title=f"{INTRO_LABEL} ({INTRO_TIER}등급 · 홍보 목적일 수 있음)",
                    text=ctx.intro_text,
                    tier=INTRO_TIER,
                    display_title=INTRO_LABEL,
                )
            )
        return docs, excerpted

    # ── 응답 후처리 ───────────────────────────────────────
    @staticmethod
    def _segments(result: LLMResult, docs: list[DocSpec], ctx: WorkContext) -> list[dict]:
        labels = {p.index: p.label for p in ctx.paragraphs}
        segments: list[dict] = []
        for block in result.blocks:
            citations: list[dict] = []
            for raw in block.citations:
                if not 0 <= raw.document_index < len(docs):
                    continue
                doc = docs[raw.document_index]
                if doc.role == "work" and raw.type == "content_block_location" and doc.blocks is not None:
                    start, end = raw.start or 0, raw.end or 0
                    if not (0 <= start < end <= len(doc.blocks)):
                        continue
                    indices = doc.index_map[start:end] if doc.index_map else list(range(start, end))
                    citations.append(
                        {
                            "source": "work",
                            "paragraphs": indices,
                            "labels": [labels.get(i, str(i)) for i in indices],
                            "cited_text": strip_label(raw.cited_text),
                            "verified": is_supported(raw.cited_text, "".join(doc.blocks[start:end])),
                            "tier": None,
                            "tier_label": "원문",
                        }
                    )
                elif doc.role in ("reference", "intro") and raw.type == "char_location" and doc.text is not None:
                    start, end = raw.start or 0, raw.end or 0
                    span = doc.text[start:end] if 0 <= start < end <= len(doc.text) else ""
                    citations.append(
                        {
                            "source": doc.role,
                            "ref_id": doc.ref_id,
                            "title": doc.display_title,
                            "cited_text": raw.cited_text,
                            "verified": bool(span) and is_supported(raw.cited_text, span),
                            "tier": doc.tier,
                            "tier_label": INTRO_LABEL if doc.role == "intro" else f"{doc.tier}등급 · {tier_label(doc.tier)}",
                        }
                    )
            if block.text or citations:
                segments.append({"text": block.text, "citations": citations})
        return segments

    def _finish(self, ctx: WorkContext, result: LLMResult, docs: list[DocSpec], excerpted: bool, extra: dict) -> dict:
        segments = self._segments(result, docs, ctx)
        summary = summarize(segments)
        return {
            **extra,
            "segments": segments,
            "summary": summary,
            "badge": analysis_badge(summary["ratio"], ctx.intro_only, excerpted),
            "excerpted": excerpted,
            "truncated": result.stop_reason == "max_tokens",
            "model": result.model,
            "fallback_used": result.fallback_used,
            "mode": self.llm.mode,
            "usage": result.usage,
        }

    # ── 공개 메서드 ───────────────────────────────────────
    def analyze(self, ctx: WorkContext, kind: str, option: str = "") -> dict:
        if kind not in KINDS:
            raise AnalysisRequestError("알 수 없는 분석 종류입니다.")
        if ctx.intro_only and KINDS[kind]["needs_text"]:
            raise AnalysisRequestError(
                "원문이 없어 이 분석은 할 수 없습니다. 소개글만으로는 '읽기 가이드'만 제공합니다. "
                "작품 해석이 필요하면 원문을 올려 주세요."
            )
        if kind == "translate" and option not in TRANSLATE_TARGETS:
            option = "easy"

        info = prompts.work_info_line(ctx.title, ctx.author, ctx.category, ctx.genre)
        if kind == "overview":
            task, sections = prompts.overview_task(info, ctx.genre), list(ctx.genre.sections)
        elif kind == "flow":
            task, sections = prompts.flow_task(info), ["부분별 흐름", "전체 흐름"]
        elif kind == "translate":
            task, sections = prompts.translate_task(info, option), [TRANSLATE_TARGETS[option], "어휘 풀이"]
        else:
            task, sections = prompts.guide_task(info, ctx.intro_only), list(prompts.GUIDE_SECTIONS)

        ref_query = f"{ctx.title} {ctx.author or ''} {ctx.genre.label} {' '.join(sections)}"
        docs, excerpted = self._documents(ctx, None, ref_query)
        if not docs:
            raise AnalysisRequestError("분석할 원문이나 소개글이 없습니다.")
        result = self.llm.generate(prompts.SYSTEM_PROMPT, docs, task, {"kind": kind, "sections": sections})
        return self._finish(ctx, result, docs, excerpted, {"kind": kind, "option": option})

    def ask(self, ctx: WorkContext, question: str, multiple_choice: bool = False) -> dict:
        question = question.strip()
        if not question:
            raise AnalysisRequestError("질문을 입력해 주세요.")
        info = prompts.work_info_line(ctx.title, ctx.author, ctx.category, ctx.genre)
        task = prompts.ask_task(info, question)
        if ctx.intro_only:
            task += "\n- 작품 원문이 없습니다. 소개글에 있는 내용만 '소개글에 따르면'으로 한정해 답하고, 작품 해석은 하지 않습니다."
        docs, excerpted = self._documents(ctx, question, f"{ctx.title} {question}")
        if not docs:
            raise AnalysisRequestError("분석할 원문이나 소개글이 없습니다.")
        meta = {"kind": "ask", "question": question, "multiple_choice": multiple_choice}
        result = self.llm.generate(prompts.SYSTEM_PROMPT, docs, task, meta)
        return self._finish(ctx, result, docs, excerpted, {"kind": "ask", "question": question})
