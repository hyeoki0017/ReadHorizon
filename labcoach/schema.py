"""Claude가 돌려주는 '코칭 단계'의 형식(구조화 출력 JSON 스키마)과 파싱·좌표 변환.

좌표는 캡처 이미지 기준 0~1000 정규화 값이다(이미지 크기·화면 배율과 무관).
오버레이는 이를 캡처 영역의 화면 좌표로 바꿔 그린다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COORD_MAX = 1000
MAX_ANNOTATIONS = 8
MAX_FLOW = 8
MAX_ROWS = 12

ANNOTATION_KINDS = ("arrow", "box", "label", "highlight")
TONES = ("info", "attack", "defense", "warning")
FLOW_KINDS = ("process", "registry", "file", "network", "stack", "memory", "api_call", "event_log", "other")
PHASES = (
    "setup",
    "recon",
    "initial_access",
    "execution",
    "persistence",
    "privilege_escalation",
    "defense_evasion",
    "credential_access",
    "discovery",
    "lateral_movement",
    "command_and_control",
    "exfiltration",
    "impact",
    "static_analysis",
    "dynamic_analysis",
    "debugging",
    "memory_forensics",
    "network_analysis",
    "detection",
    "hardening",
    "incident_response",
    "unknown",
)
PHASE_LABELS = {
    "setup": "환경 준비",
    "recon": "정찰",
    "initial_access": "초기 침투",
    "execution": "실행",
    "persistence": "지속성 확보",
    "privilege_escalation": "권한 상승",
    "defense_evasion": "방어 회피",
    "credential_access": "자격 증명 접근",
    "discovery": "내부 탐색",
    "lateral_movement": "내부 확산",
    "command_and_control": "명령·제어(C2)",
    "exfiltration": "유출",
    "impact": "영향",
    "static_analysis": "정적 분석",
    "dynamic_analysis": "동적 분석",
    "debugging": "디버깅",
    "memory_forensics": "메모리 포렌식",
    "network_analysis": "네트워크 분석",
    "detection": "탐지",
    "hardening": "보안 강화",
    "incident_response": "사고 대응",
    "unknown": "판단 보류",
}

_STR = {"type": "string"}
_INT = {"type": "integer"}

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "screen_summary": _STR,
        "phase": {"type": "string", "enum": list(PHASES)},
        "subtitle": _STR,
        "narration": _STR,
        "annotations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ANNOTATION_KINDS)},
                    "x": _INT,
                    "y": _INT,
                    "x2": _INT,
                    "y2": _INT,
                    "text": _STR,
                    "tone": {"type": "string", "enum": list(TONES)},
                },
                "required": ["kind", "x", "y", "x2", "y2", "text", "tone"],
                "additionalProperties": False,
            },
        },
        "flow": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": list(FLOW_KINDS)}, "label": _STR, "detail": _STR},
                "required": ["kind", "label", "detail"],
                "additionalProperties": False,
            },
        },
        "stack": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"location": _STR, "value": _STR, "meaning": _STR},
                "required": ["location", "value", "meaning"],
                "additionalProperties": False,
            },
        },
        "registry": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": _STR, "value": _STR, "meaning": _STR},
                "required": ["key", "value", "meaning"],
                "additionalProperties": False,
            },
        },
        "mitre": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": _STR, "name": _STR},
                "required": ["id", "name"],
                "additionalProperties": False,
            },
        },
        "next_steps": {"type": "array", "items": _STR},
        "defense_tips": {"type": "array", "items": _STR},
        "safety_warnings": {"type": "array", "items": _STR},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "screen_summary",
        "phase",
        "subtitle",
        "narration",
        "annotations",
        "flow",
        "stack",
        "registry",
        "mitre",
        "next_steps",
        "defense_tips",
        "safety_warnings",
        "confidence",
    ],
    "additionalProperties": False,
}


@dataclass
class Annotation:
    kind: str
    x: int
    y: int
    x2: int
    y2: int
    text: str
    tone: str = "info"


@dataclass
class FlowNode:
    kind: str
    label: str
    detail: str = ""


@dataclass
class Row:
    """스택·레지스트리 표의 한 줄 (first = 위치/키, second = 값)."""

    first: str
    second: str
    meaning: str


@dataclass
class CoachStep:
    screen_summary: str
    phase: str
    subtitle: str
    narration: str
    annotations: list[Annotation] = field(default_factory=list)
    flow: list[FlowNode] = field(default_factory=list)
    stack: list[Row] = field(default_factory=list)
    registry: list[Row] = field(default_factory=list)
    mitre: list[tuple[str, str]] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    defense_tips: list[str] = field(default_factory=list)
    safety_warnings: list[str] = field(default_factory=list)
    confidence: str = "medium"
    model: str = ""
    demo: bool = False
    fallback_used: bool = False
    usage: dict = field(default_factory=dict)

    @property
    def phase_label(self) -> str:
        return PHASE_LABELS.get(self.phase, self.phase)


def _clamp(value, low: int = 0, high: int = COORD_MAX) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = 0
    return max(low, min(high, number))


def _text(value, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strings(values, limit: int = 6, size: int = 300) -> list[str]:
    out = []
    for value in values or []:
        text = _text(value, size)
        if text:
            out.append(text)
    return out[:limit]


def parse_step(data: dict) -> CoachStep:
    """구조화 출력 JSON → CoachStep. 범위를 벗어난 좌표·알 수 없는 값은 고쳐서 받는다(그리기 오류 방지)."""
    annotations = []
    for item in (data.get("annotations") or [])[: MAX_ANNOTATIONS * 2]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") if item.get("kind") in ANNOTATION_KINDS else "label"
        ann = Annotation(
            kind=kind,
            x=_clamp(item.get("x")),
            y=_clamp(item.get("y")),
            x2=_clamp(item.get("x2")),
            y2=_clamp(item.get("y2")),
            text=_text(item.get("text"), 160),
            tone=item.get("tone") if item.get("tone") in TONES else "info",
        )
        if kind in ("box", "highlight"):
            # 모서리 순서가 뒤바뀌어도 정상 사각형으로
            ann.x, ann.x2 = sorted((ann.x, ann.x2))
            ann.y, ann.y2 = sorted((ann.y, ann.y2))
            if ann.x2 - ann.x < 5 or ann.y2 - ann.y < 5:
                continue
        if kind == "arrow" and (ann.x, ann.y) == (ann.x2, ann.y2):
            continue
        annotations.append(ann)

    flow = [
        FlowNode(i.get("kind") if i.get("kind") in FLOW_KINDS else "other", _text(i.get("label"), 80), _text(i.get("detail"), 200))
        for i in data.get("flow") or []
        if isinstance(i, dict) and _text(i.get("label"))
    ]
    stack = [
        Row(_text(i.get("location"), 60), _text(i.get("value"), 120), _text(i.get("meaning"), 200))
        for i in data.get("stack") or []
        if isinstance(i, dict)
    ]
    registry = [
        Row(_text(i.get("key"), 200), _text(i.get("value"), 200), _text(i.get("meaning"), 200))
        for i in data.get("registry") or []
        if isinstance(i, dict) and _text(i.get("key"))
    ]
    mitre = [
        (_text(i.get("id"), 20), _text(i.get("name"), 80))
        for i in data.get("mitre") or []
        if isinstance(i, dict) and _text(i.get("id"))
    ]
    phase = data.get("phase") if data.get("phase") in PHASES else "unknown"
    confidence = data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "medium"
    return CoachStep(
        screen_summary=_text(data.get("screen_summary"), 600),
        phase=phase,
        subtitle=_text(data.get("subtitle"), 220),
        narration=_text(data.get("narration"), 600),
        annotations=annotations[:MAX_ANNOTATIONS],
        flow=flow[:MAX_FLOW],
        stack=stack[:MAX_ROWS],
        registry=registry[:MAX_ROWS],
        mitre=mitre[:6],
        next_steps=_strings(data.get("next_steps")),
        defense_tips=_strings(data.get("defense_tips")),
        safety_warnings=_strings(data.get("safety_warnings"), 4),
        confidence=confidence,
    )


def to_pixels(value: int, size: int) -> int:
    """0~1000 정규화 좌표 → 0~size 픽셀."""
    return round(value * size / COORD_MAX)


def scale_annotation(ann: Annotation, width: int, height: int) -> tuple[int, int, int, int]:
    """캡처 영역(폭·높이, 픽셀) 안의 좌표로 바꾼다. 반환: (x, y, x2, y2)."""
    return (to_pixels(ann.x, width), to_pixels(ann.y, height), to_pixels(ann.x2, width), to_pixels(ann.y2, height))
