"""Claude 호출: 캡처 이미지 + 맥락 → 코칭 단계(CoachStep).

ReadHorizon의 ClaudeClient와 같은 방식(beta 스트리밍, adaptive thinking, 거절 시 서버 측 대체 모델)을 쓰고,
응답은 구조화 출력(STEP_SCHEMA)으로 받는다.
"""

from __future__ import annotations

import base64
import json
from typing import Protocol

from readhorizon.analysis.llm import ADAPTIVE_PREFIXES, FALLBACK_BETA, FALLBACK_DEFAULT_PREFIXES, LLMError, LLMRefusal, _usage

from .prompts import SYSTEM_PROMPT
from .schema import STEP_SCHEMA, CoachStep, parse_step

STEP_MAX_TOKENS = 16000

__all__ = ["ClaudeCoach", "DemoCoach", "LLMError", "LLMRefusal", "build_request", "parse_step_message"]


class Coach(Protocol):
    mode: str

    def explain(self, image_png: bytes, task: str) -> CoachStep: ...


def build_request(model: str, effort: str, image_png: bytes, task: str) -> dict:
    request: dict = {
        "model": model,
        "max_tokens": STEP_MAX_TOKENS,
        # 고정 지시문은 캐시(이미지·맥락은 매번 바뀌므로 그 뒤에 둔다)
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(image_png).decode()},
                    },
                    {"type": "text", "text": task},
                ],
            }
        ],
        "output_config": {"format": {"type": "json_schema", "schema": STEP_SCHEMA}},
    }
    if model.startswith(ADAPTIVE_PREFIXES) or model.startswith("claude-haiku-5"):
        request["thinking"] = {"type": "adaptive"}
        request["output_config"]["effort"] = effort
    if model.startswith(FALLBACK_DEFAULT_PREFIXES):
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def parse_step_message(message) -> CoachStep:
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        raise LLMRefusal("안전 정책에 따라 이 화면은 설명할 수 없습니다. 다른 단계로 넘어가거나 질문을 바꿔 보세요.")
    if stop == "max_tokens":
        raise LLMError("설명이 길어 끝까지 받지 못했습니다. 다시 시도해 주세요.")
    raw = "".join(b.text or "" for b in getattr(message, "content", None) or [] if getattr(b, "type", None) == "text")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LLMError("설명 응답을 해석하지 못했습니다. 다시 시도해 주세요.") from exc
    if not isinstance(data, dict):
        raise LLMError("설명 응답 형식이 올바르지 않습니다.")
    step = parse_step(data)
    step.usage, step.fallback_used = _usage(message)
    step.model = getattr(message, "model", "")
    return step


class ClaudeCoach:
    mode = "live"

    def __init__(self, model: str, effort: str, client=None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort

    def explain(self, image_png: bytes, task: str) -> CoachStep:
        import anthropic

        request = build_request(self.model, self.effort, image_png, task)
        try:
            with self._client.beta.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("Claude API 인증에 실패했습니다. ANTHROPIC_API_KEY를 확인해 주세요.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("요청이 많아 잠시 제한되었습니다. 자동 분석 간격(LABCOACH_INTERVAL_SEC)을 늘려 보세요.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"요청이 거부되었습니다: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API 오류({exc.status_code})가 발생했습니다. 잠시 후 다시 시도해 주세요.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Claude API에 연결할 수 없습니다. 호스트 PC의 네트워크를 확인해 주세요.") from exc
        except anthropic.APIError as exc:
            raise LLMError("Claude API 응답을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
        return parse_step_message(message)


DEMO_STEP = {
    "screen_summary": "(데모) Process Monitor에서 샘플 프로세스의 레지스트리 쓰기 이벤트가 강조된 화면이라고 가정한 예시입니다.",
    "phase": "persistence",
    "subtitle": "(데모) sample.exe가 Run 키에 자기 경로를 써서 재부팅 후에도 실행되게 만듭니다.",
    "narration": (
        "데모 설명입니다. 지금 보이는 RegSetValue 이벤트는 샘플이 자동 실행 키에 값을 쓰는 장면이에요. "
        "이런 동작을 지속성 확보라고 부르고, 방어 쪽에서는 Sysmon 이벤트 13번으로 잡을 수 있습니다."
    ),
    "annotations": [
        {"kind": "box", "x": 12, "y": 372, "x2": 660, "y2": 405, "text": "RegSetValue 이벤트", "tone": "attack"},
        {"kind": "arrow", "x": 640, "y": 600, "x2": 480, "y2": 410, "text": "Run 키 = 자동 실행", "tone": "warning"},
        {"kind": "label", "x": 60, "y": 760, "x2": 0, "y2": 0, "text": "Sysmon EID 13으로 탐지", "tone": "defense"},
    ],
    "flow": [
        {"kind": "process", "label": "explorer.exe", "detail": "사용자가 샘플 실행"},
        {"kind": "process", "label": "sample.exe", "detail": "샘플 프로세스 생성"},
        {"kind": "registry", "label": r"HKCU\...\Run", "detail": "자동 실행 값 등록"},
        {"kind": "network", "label": "10.0.2.15:4444", "detail": "(가정) 실습 대역 C2 접속 시도"},
    ],
    "stack": [],
    "registry": [
        {
            "key": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "value": r"Updater = C:\Users\[user]\AppData\Roaming\sample.exe",
            "meaning": "로그인할 때마다 샘플을 실행(지속성)",
        }
    ],
    "mitre": [{"id": "T1547.001", "name": "Registry Run Keys / Startup Folder"}],
    "next_steps": ["Regshot 2차 스냅샷으로 다른 변경 확인", "Wireshark/FakeNet에서 통신 시도 확인"],
    "defense_tips": ["Sysmon EID 13(Registry value set)에서 Run 키 경로 감시", "Autoruns로 등록된 자동 실행 항목 점검"],
    "safety_warnings": ["실습 VM 네트워크가 Host-only(또는 내부 네트워크)인지 확인하세요."],
    "confidence": "medium",
}


class DemoCoach:
    """API 키 없이 오버레이·패널·음성 흐름을 확인하기 위한 고정 응답. 실제 화면 분석이 아니다."""

    mode = "demo"
    model = "demo"

    def explain(self, image_png: bytes, task: str) -> CoachStep:
        step = parse_step(DEMO_STEP)
        step.model = self.model
        step.demo = True
        return step
