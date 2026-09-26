"""LLM 호출 계층.

- ClaudeClient: Claude API(Messages, Citations). 작품 원문은 '문단 = 블록'인 custom content 문서로 보내
  인용 위치를 문단 번호로 돌려받는다. 원문 문서에는 prompt caching을 걸어 같은 작품의 반복 질문 비용을 줄인다.
- DemoClient: API 키가 없을 때 화면 흐름을 확인하기 위한 결정적 응답. 실제 분석이 아니며 결과에 그렇게 표시한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..references.search import BM25
from .grounding import strip_label

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# fallbacks="default" 를 지원하는 모델 (그 외 모델은 거절 시 그대로 오류 처리)
FALLBACK_DEFAULT_PREFIXES = ("claude-opus-5", "claude-fable-5-1")
# adaptive thinking + effort 를 지원하는 모델
ADAPTIVE_PREFIXES = (
    "claude-opus-5",
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)
MAX_TOKENS = 32000


@dataclass
class DocSpec:
    role: str  # "work" | "reference" | "intro"
    title: str
    blocks: list[str] | None = None  # work: 문단별 텍스트(custom content)
    text: str | None = None  # reference/intro: 일반 텍스트
    index_map: list[int] | None = None  # work: 블록 순서 → 원래 문단 번호
    tier: str | None = None
    ref_id: str | None = None
    display_title: str | None = None
    cache: bool = False


@dataclass
class RawCitation:
    type: str
    cited_text: str
    document_index: int
    start: int | None
    end: int | None


@dataclass
class RawBlock:
    text: str
    citations: list[RawCitation] = field(default_factory=list)


@dataclass
class LLMResult:
    blocks: list[RawBlock]
    stop_reason: str | None
    model: str
    fallback_used: bool = False
    usage: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    """사용자에게 보여줄 한국어 메시지를 담는다."""


class LLMRefusal(LLMError):
    pass


class LLMClient(Protocol):
    mode: str

    def generate(self, system: str, docs: list[DocSpec], task: str, meta: dict) -> LLMResult: ...


class ClaudeClient:
    mode = "live"

    def __init__(self, model: str, effort: str, client=None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort

    def build_request(self, system: str, docs: list[DocSpec], task: str) -> dict:
        content: list[dict] = []
        for doc in docs:
            block: dict = {"type": "document", "title": doc.title[:200], "citations": {"enabled": True}}
            if doc.blocks is not None:
                block["source"] = {"type": "content", "content": [{"type": "text", "text": t} for t in doc.blocks]}
            else:
                block["source"] = {"type": "text", "media_type": "text/plain", "data": doc.text or ""}
            if doc.cache:
                block["cache_control"] = {"type": "ephemeral"}
            content.append(block)
        content.append({"type": "text", "text": task})

        request: dict = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": system}],
            "messages": [{"role": "user", "content": content}],
        }
        if self.model.startswith(ADAPTIVE_PREFIXES):
            request["thinking"] = {"type": "adaptive"}
            request["output_config"] = {"effort": self.effort}
        if self.model.startswith(FALLBACK_DEFAULT_PREFIXES):
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def generate(self, system: str, docs: list[DocSpec], task: str, meta: dict) -> LLMResult:
        import anthropic

        request = self.build_request(system, docs, task)
        try:
            with self._client.beta.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("Claude API 인증에 실패했습니다. ANTHROPIC_API_KEY를 확인해 주세요.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("요청이 많아 잠시 제한되었습니다. 잠시 후 다시 시도해 주세요.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"분석 요청이 거부되었습니다: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API 오류({exc.status_code})가 발생했습니다. 잠시 후 다시 시도해 주세요.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Claude API에 연결할 수 없습니다. 네트워크를 확인해 주세요.") from exc
        return parse_message(message)


def parse_message(message) -> LLMResult:
    if getattr(message, "stop_reason", None) == "refusal":
        raise LLMRefusal("안전 정책에 따라 이 요청은 분석할 수 없습니다. 다른 부분이나 질문으로 시도해 주세요.")

    blocks: list[RawBlock] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue  # thinking / fallback 블록 등은 화면에 보이지 않는다
        citations: list[RawCitation] = []
        for c in getattr(block, "citations", None) or []:
            kind = getattr(c, "type", None)
            if kind == "content_block_location":
                start, end = c.start_block_index, c.end_block_index
            elif kind == "char_location":
                start, end = c.start_char_index, c.end_char_index
            elif kind == "page_location":
                start, end = c.start_page_number, c.end_page_number
            else:
                continue
            citations.append(RawCitation(kind, c.cited_text or "", c.document_index, start, end))
        blocks.append(RawBlock(block.text or "", citations))

    usage_obj = getattr(message, "usage", None)
    iterations = getattr(usage_obj, "iterations", None) or []
    fallback_used = any(getattr(item, "type", None) == "fallback_message" for item in iterations)
    usage = {
        key: getattr(usage_obj, key, None)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    }
    return LLMResult(blocks, message.stop_reason, getattr(message, "model", ""), fallback_used, usage)


DEMO_NOTICE = "> 데모 모드: 실제 AI 분석이 아닙니다. ANTHROPIC_API_KEY를 설정하면 실제 분석이 실행됩니다.\n\n"


class DemoClient:
    """API 키 없이 인용·검증·화면 흐름을 확인하기 위한 결정적 응답기."""

    mode = "demo"
    model = "demo"

    def generate(self, system: str, docs: list[DocSpec], task: str, meta: dict) -> LLMResult:
        work_index = next((i for i, d in enumerate(docs) if d.role == "work"), None)
        intro_index = next((i for i, d in enumerate(docs) if d.role == "intro"), None)
        blocks = [RawBlock(DEMO_NOTICE)]
        kind = meta.get("kind")

        def cite_work(pos: int, text: str) -> RawBlock:
            doc = docs[work_index]
            return RawBlock(text, [RawCitation("content_block_location", doc.blocks[pos], work_index, pos, pos + 1)])

        def short(value: str, n: int = 28) -> str:
            value = strip_label(value)
            return value if len(value) <= n else value[:n] + "…"

        if work_index is None:
            # 원문 없이 소개글만 있는 경우: 소개글 첫 문장을 인용한 읽기 가이드
            blocks.append(RawBlock("## 소개글 요약\n소개글에 따르면 "))
            if intro_index is not None:
                intro = docs[intro_index].text or ""
                end = next((i + 1 for i, ch in enumerate(intro) if ch in ".!?"), min(len(intro), 60))
                blocks.append(RawBlock(short(intro[:end], 60), [RawCitation("char_location", intro[:end], intro_index, 0, end)]))
            blocks.append(RawBlock("\n\n## 읽기 전 생각해 볼 질문\n- (데모) 원문을 올리면 작품에 근거한 질문을 만들어 드립니다.\n"))
            return LLMResult(blocks, "end_turn", self.model)

        doc = docs[work_index]
        count = len(doc.blocks)

        if kind == "ask":
            ranked = BM25(doc.blocks).top(meta.get("question", ""), 2) or [(0, 0.0)]
            blocks.append(RawBlock("질문과 가장 관련 있는 원문은 다음과 같습니다.\n"))
            for pos, _ in ranked:
                blocks.append(RawBlock("- "))
                blocks.append(cite_work(pos, short(doc.blocks[pos])))
                blocks.append(RawBlock("\n"))
            if meta.get("multiple_choice"):
                blocks.append(RawBlock("\n정답: 판단 불가(데모)\n"))
        elif kind in ("flow", "translate"):
            heading = "## 문단 흐름" if kind == "flow" else "## 번역·풀이"
            blocks.append(RawBlock(heading + "\n"))
            for pos in range(min(count, 12)):
                blocks.append(RawBlock("- "))
                blocks.append(cite_work(pos, short(doc.blocks[pos])))
                blocks.append(RawBlock(" — (데모) 이 부분의 요지가 여기에 표시됩니다.\n"))
        else:
            for i, title in enumerate(meta.get("sections") or ["요지"]):
                pos = i % count
                blocks.append(RawBlock(f"## {title}\n(데모) 이 항목의 분석이 여기에 표시됩니다. 관련 원문: "))
                blocks.append(cite_work(pos, short(doc.blocks[pos])))
                blocks.append(RawBlock("\n\n"))
        return LLMResult(blocks, "end_turn", self.model)
