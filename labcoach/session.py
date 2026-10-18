"""화면 한 장 → (가공·마스킹 → Claude → 기록) 흐름과 학습 노트. GUI와 무관해 명령줄·테스트에서도 쓴다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .capture import prepare
from .coach import Coach
from .config import Settings
from .privacy import CollectionLog, mask_text
from .prompts import build_task
from .schema import CoachStep
from .toollogs import excerpt


@dataclass
class Request:
    png: bytes
    size: tuple[int, int]
    task: str
    items: list[str]
    mask_counts: dict[str, int] = field(default_factory=dict)


def _merge(counts: dict[str, int], extra: dict[str, int]) -> None:
    for code, n in extra.items():
        counts[code] = counts.get(code, 0) + n


class Session:
    def __init__(self, settings: Settings, coach: Coach, log: CollectionLog | None = None, tool_logs: list[Path] | None = None) -> None:
        self.settings = settings
        self.coach = coach
        self.log = log
        self.tool_logs = [Path(p) for p in tool_logs or []]
        self.mode = settings.mode
        self.level = settings.level
        self.steps: list[tuple[datetime, CoachStep, str]] = []

    @property
    def history(self) -> list[str]:
        n = self.settings.history_steps
        if n <= 0:
            return []
        return [f"[{step.phase_label}] {step.subtitle}" for _, step, _ in self.steps[-n:] if step.subtitle]

    def build(self, image: Image.Image, window_title: str = "", question: str = "") -> Request:
        s = self.settings
        png, size = prepare(image, s.mask_regions, s.max_image_edge)
        counts: dict[str, int] = {}
        items = ["screenshot"]

        title = ""
        if window_title:
            masked = mask_text(window_title, s.mask_public_ip)
            title = masked.text
            _merge(counts, masked.counts)
            items.append("window_title")

        logs = []
        for path in self.tool_logs:
            kind, body = excerpt(path)
            if not body:
                continue
            masked = mask_text(body, s.mask_public_ip)
            _merge(counts, masked.counts)
            logs.append((path.name, kind, masked.text))
        if logs:
            items.append("tool_log")

        q = ""
        if question.strip():
            masked = mask_text(question.strip()[:1000], s.mask_public_ip)
            q = masked.text
            _merge(counts, masked.counts)
            items.append("question")

        history = self.history
        if history:
            items.append("history")
        task = build_task(self.mode, self.level, size[0], size[1], title, history, logs, q)
        return Request(png, size, task, items, counts)

    def analyze(self, image: Image.Image, window_title: str = "", question: str = "") -> CoachStep:
        request = self.build(image, window_title, question)
        return self.run(request, question)

    def run(self, request: Request, question: str = "") -> CoachStep:
        if self.log is not None and self.coach.mode == "live":
            # 실제로 외부에 보내는 경우만 기록한다(데모는 아무것도 보내지 않는다)
            self.log.record(request.items, request.mask_counts)
        if self.settings.save_frames:
            frames = self.settings.data_dir / "frames"
            frames.mkdir(parents=True, exist_ok=True)
            (frames / f"{datetime.now():%Y%m%d-%H%M%S-%f}.png").write_bytes(request.png)
        step = self.coach.explain(request.png, request.task)
        self.steps.append((datetime.now(timezone.utc), step, mask_text(question, self.settings.mask_public_ip).text if question else ""))
        return step

    def notes_markdown(self, title: str = "LabCoach 학습 노트") -> str:
        def cell(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", " ")

        lines = [f"# {title}", "", f"- 모드: {self.mode} · 수준: {self.level} · 단계 수: {len(self.steps)}", ""]
        for number, (ts, step, question) in enumerate(self.steps, start=1):
            local = ts.astimezone().strftime("%H:%M:%S")
            lines.append(f"## {number}. [{step.phase_label}] {step.subtitle}")
            lines.append(f"_{local} · 확신도 {step.confidence}{' · 데모' if step.demo else ''}_")
            lines.append("")
            if question:
                lines.append(f"**질문**: {question}")
                lines.append("")
            if step.screen_summary:
                lines.append(step.screen_summary)
                lines.append("")
            if step.narration and step.narration != step.subtitle:
                lines.append(f"> {step.narration}")
                lines.append("")
            if step.flow:
                lines.append("**흐름**: " + " → ".join(f"{n.label}" + (f" ({n.detail})" if n.detail else "") for n in step.flow))
                lines.append("")
            if step.stack:
                lines += ["| 스택·레지스터 | 값 | 의미 |", "|---|---|---|"]
                lines += [f"| {cell(r.first)} | {cell(r.second)} | {cell(r.meaning)} |" for r in step.stack]
                lines.append("")
            if step.registry:
                lines += ["| 레지스트리 키 | 값 | 의미 |", "|---|---|---|"]
                lines += [f"| {cell(r.first)} | {cell(r.second)} | {cell(r.meaning)} |" for r in step.registry]
                lines.append("")
            if step.mitre:
                lines.append("**MITRE ATT&CK**: " + ", ".join(f"{i} {n}" for i, n in step.mitre))
                lines.append("")
            for label, values in (("다음 단계", step.next_steps), ("방어 팁", step.defense_tips), ("안전 주의", step.safety_warnings)):
                if values:
                    lines.append(f"**{label}**")
                    lines += [f"- {v}" for v in values]
                    lines.append("")
        return "\n".join(lines).rstrip() + "\n"
