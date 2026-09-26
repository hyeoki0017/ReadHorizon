"""분석 엔진: 문서 구성 → LLM 호출 → 인용 검증 → 신뢰도 배지.

문서 순서(인용 번호 = 이 순서): [작품 원문] + [참고자료...] + [사용자 제공 소개]
작품 원문 문서에만 cache_control을 걸어 시스템 프롬프트 + 원문까지를 캐시한다.

번역·풀이는 발췌하지 않는다. 결과가 원문 분량만큼 길어 한 번의 응답 한도를 넘기 쉬우므로, 원문을 앞에서부터
연속된 부분(기본 6,000자)으로 나누고 요청마다 한 부분을 빠짐없이 옮긴다. 결과에는 누락·숫자 검사를 붙인다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..ingest.normalize import Paragraph, total_chars
from ..references.search import BM25
from ..references.store import ReferenceIndex
from ..trust import INTRO_LABEL, INTRO_TIER, analysis_badge, tier_label
from . import prompts
from .checks import translation_checks
from .glossary import GLOSSARY_SCHEMA, GLOSSARY_SYSTEM, MAX_EXTRACT, glossary_prompt, normalize_entries
from .grounding import is_supported, strip_label, summarize
from .llm import DocSpec, LLMClient, LLMResult
from .presets import (
    KINDS,
    TRANSLATE_TARGETS,
    Category,
    Genre,
    ReaderProfile,
    parse_translate_option,
    translate_option,
)

DEFAULT_TRANSLATE_PART_CHARS = 6000


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
            # 관련 문단을 먼저 넣고, 남는 한도 안에서 앞뒤 문단을 붙인다
            for neighbor in (idx, idx - 1, idx + 1):
                if 0 <= neighbor < len(paragraphs) and neighbor not in chosen:
                    size = len(paragraphs[neighbor].text)
                    if budget + size > limit:
                        continue
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
    if not chosen:
        chosen.add(0)  # 한 문단이 한도보다 길어도 빈 문서를 보내지 않는다(문단은 정규화 단계에서 2,000자 이하로 나뉨)
    return [paragraphs[i] for i in sorted(chosen)], True


def split_parts(paragraphs: list[Paragraph], part_chars: int) -> list[list[Paragraph]]:
    """문단 순서를 유지하며 part_chars 안쪽의 연속된 부분으로 나눈다. 문단은 쪼개지 않는다."""
    parts: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    size = 0
    for p in paragraphs:
        if current and size + len(p.text) > part_chars:
            parts.append(current)
            current, size = [], 0
        current.append(p)
        size += len(p.text)
    if current:
        parts.append(current)
    return parts


def relevant_entries(entries: list[dict], paragraphs: list[Paragraph]) -> list[dict]:
    """용어집 중 이번 부분 원문에 나오는 용어만 요청문에 넣는다(요청문 길이·비용 절약)."""
    text = "".join(p.text for p in paragraphs)
    return [e for e in entries if e.get("term") and e["term"] in text]


class AnalysisEngine:
    def __init__(
        self,
        llm: LLMClient,
        references: ReferenceIndex | None,
        fulltext_char_limit: int,
        translate_part_chars: int = DEFAULT_TRANSLATE_PART_CHARS,
        system_prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.references = references
        self.limit = fulltext_char_limit
        self.part_chars = translate_part_chars
        # 모범 예시가 들어간 고정 시스템 프롬프트(작품 원문과 함께 캐시된다)
        self.system_prompt = system_prompt or prompts.SYSTEM_PROMPT

    def translate_parts(self, paragraphs: list[Paragraph]) -> list[dict]:
        """화면에서 번역 범위를 고를 수 있도록 부분 목록을 돌려준다."""
        return [
            {"part": n, "from": part[0].label, "to": part[-1].label, "chars": sum(len(p.text) for p in part)}
            for n, part in enumerate(split_parts(paragraphs, self.part_chars), start=1)
        ]

    # ── 문서 구성 ─────────────────────────────────────────
    def _documents(
        self,
        ctx: WorkContext,
        query: str | None,
        ref_query: str,
        part: tuple[list[Paragraph], int, int] | None = None,
    ) -> tuple[list[DocSpec], bool]:
        """part = (부분 문단, 부분 번호, 전체 부분 수). 주어지면 발췌하지 않고 그 부분을 그대로 보낸다."""
        docs: list[DocSpec] = []
        excerpted = False
        if ctx.paragraphs:
            if part is not None:
                selected, number, count = part
                suffix = f" ({number}/{count}부분)" if count > 1 else ""
            else:
                selected, excerpted = select_paragraphs(ctx.paragraphs, self.limit, query)
                suffix = " (발췌)" if excerpted else ""
            docs.append(
                DocSpec(
                    role="work",
                    title=f"작품 원문: {ctx.title}{suffix}",
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
    def analyze(
        self,
        ctx: WorkContext,
        kind: str,
        option: str = "",
        profile: ReaderProfile | None = None,
        glossary: dict | None = None,
    ) -> dict:
        """profile: 독자 수준·설명 언어. glossary: 번역 용어집 {"entries": [...], "version": str}(번역에만 쓴다)."""
        profile = profile or ReaderProfile()
        if kind not in KINDS:
            raise AnalysisRequestError("알 수 없는 분석 종류입니다.")
        if ctx.intro_only and KINDS[kind]["needs_text"]:
            raise AnalysisRequestError(
                "원문이 없어 이 분석은 할 수 없습니다. 소개글만으로는 '읽기 가이드'만 제공합니다. "
                "작품 해석이 필요하면 원문을 올려 주세요."
            )
        if kind == "translate":
            return self._translate(ctx, option, profile, glossary)

        info = prompts.work_info_line(ctx.title, ctx.author, ctx.category, ctx.genre, profile)
        if kind == "overview":
            task, sections = prompts.overview_task(info, ctx.genre), list(ctx.genre.sections)
        elif kind == "flow":
            task, sections = prompts.flow_task(info), ["부분별 흐름", "전체 흐름"]
        else:
            task, sections = prompts.guide_task(info, ctx.intro_only), list(prompts.GUIDE_SECTIONS)

        ref_query = f"{ctx.title} {ctx.author or ''} {ctx.genre.label} {' '.join(sections)}"
        docs, excerpted = self._documents(ctx, None, ref_query)
        if not docs:
            raise AnalysisRequestError("분석할 원문이나 소개글이 없습니다.")
        result = self.llm.generate(self.system_prompt, docs, task, {"kind": kind, "sections": sections})
        extra = {"kind": kind, "option": profile.suffix, "profile": profile.to_dict()}
        return self._finish(ctx, result, docs, excerpted, extra)

    def _translate(self, ctx: WorkContext, option: str, profile: ReaderProfile, glossary: dict | None) -> dict:
        target, number = parse_translate_option(option)
        parts = split_parts(ctx.paragraphs, self.part_chars)
        if number > len(parts):
            raise AnalysisRequestError(f"{number}부분은 없습니다. 이 작품의 번역 범위는 {len(parts)}부분입니다.")
        selected = parts[number - 1]
        span = f"{selected[0].label}~{selected[-1].label}" if len(selected) > 1 else selected[0].label
        scope = (
            f"[범위] 전체 {len(parts)}부분 중 {number}부분({span})입니다. 제공된 범위만 옮깁니다."
            if len(parts) > 1
            else ""
        )
        entries = list((glossary or {}).get("entries") or [])
        info = prompts.work_info_line(ctx.title, ctx.author, ctx.category, ctx.genre, profile)
        task = prompts.translate_task(info, target, scope, profile.lang, relevant_entries(entries, selected))
        sections = [TRANSLATE_TARGETS[target], prompts.VOCAB_HEADINGS[profile.lang]]
        ref_query = f"{ctx.title} {ctx.author or ''} {ctx.genre.label} {' '.join(sections)}"
        docs, _ = self._documents(ctx, None, ref_query, part=(selected, number, len(parts)))
        result = self.llm.generate(self.system_prompt, docs, task, {"kind": "translate", "sections": sections})
        extra = {
            "kind": "translate",
            "option": translate_option(target, number) + profile.suffix,
            "profile": profile.to_dict(),
            "glossary": {"version": (glossary or {}).get("version"), "count": len(entries)},
            "target": target,
            "part": number,
            "parts": len(parts),
            "part_range": {"from": selected[0].label, "to": selected[-1].label},
        }
        finished = self._finish(ctx, result, docs, False, extra)
        finished["checks"] = translation_checks(finished["segments"], selected, target, entries)
        return finished

    def extract_glossary(self, ctx: WorkContext, target: str, part: int, existing: list[dict]) -> tuple[list[dict], dict]:
        """한 부분에서 번역어를 정해 둘 용어를 뽑는다. 이미 있는 용어는 제외하고 새 용어만 돌려준다."""
        if target not in TRANSLATE_TARGETS:
            raise AnalysisRequestError("알 수 없는 번역 대상입니다.")
        if not ctx.paragraphs:
            raise AnalysisRequestError("원문이 없어 용어를 뽑을 수 없습니다.")
        parts = split_parts(ctx.paragraphs, self.part_chars)
        if not 1 <= part <= len(parts):
            raise AnalysisRequestError(f"{part}부분은 없습니다. 이 작품의 번역 범위는 {len(parts)}부분입니다.")
        selected = parts[part - 1]
        result = self.llm.complete_json(
            GLOSSARY_SYSTEM,
            glossary_prompt(ctx.title, target, selected, existing),
            GLOSSARY_SCHEMA,
            {"kind": "glossary", "paragraphs": [p.text for p in selected]},
        )
        source = "".join(p.text for p in selected)
        known = {e["term"] for e in existing}
        # 원문에 없는 용어(모델이 지어낸 말)와 이미 정한 용어는 뺀다
        new = [e for e in normalize_entries(result.data.get("entries"), "auto") if e["term"] in source and e["term"] not in known]
        meta = {"model": result.model, "fallback_used": result.fallback_used, "usage": result.usage, "mode": self.llm.mode}
        return new[:MAX_EXTRACT], meta

    def ask(
        self, ctx: WorkContext, question: str, multiple_choice: bool = False, profile: ReaderProfile | None = None
    ) -> dict:
        profile = profile or ReaderProfile()
        question = question.strip()
        if not question:
            raise AnalysisRequestError("질문을 입력해 주세요.")
        info = prompts.work_info_line(ctx.title, ctx.author, ctx.category, ctx.genre, profile)
        task = prompts.ask_task(info, question)
        if ctx.intro_only:
            task += "\n- 작품 원문이 없습니다. 소개글에 있는 내용만 '소개글에 따르면'으로 한정해 답하고, 작품 해석은 하지 않습니다."
        docs, excerpted = self._documents(ctx, question, f"{ctx.title} {question}")
        if not docs:
            raise AnalysisRequestError("분석할 원문이나 소개글이 없습니다.")
        meta = {"kind": "ask", "question": question, "multiple_choice": multiple_choice}
        result = self.llm.generate(self.system_prompt, docs, task, meta)
        return self._finish(
            ctx, result, docs, excerpted, {"kind": "ask", "question": question, "profile": profile.to_dict()}
        )
