"""민감정보 최소 수집: 텍스트 마스킹, 이미지 영역 가리기, 수집 '항목' 기록과 30일 고지.

- 텍스트(질문·도구 로그·창 제목)는 ReadHorizon 공통 규칙(주민번호·카드·전화·이메일)에
  실습 환경에서 자주 보이는 값(공인 IP, MAC 주소, Windows 사용자 폴더 이름, API 키·비밀번호)을 더해 가린다.
- 이미지 안의 글자는 로컬에서 판독하지 않으므로, 가려야 할 곳은 마스크 영역으로 지정해 검게 칠한 뒤 보낸다.
- 수집 기록에는 실제 값 없이 '어떤 항목을 언제 보냈는지'와 마스킹 항목별 개수만 남긴다.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from readhorizon.privacy.masking import LABELS as BASE_LABELS
from readhorizon.privacy.masking import MaskResult
from readhorizon.privacy.masking import mask as base_mask

LAB_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "secret",
        "API 키·토큰",
        re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,})"),
    ),
    (
        "password",
        "비밀번호",
        re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|token)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"),
    ),
    ("mac", "MAC 주소", re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")),
    ("user_dir", "사용자 폴더 이름", re.compile(r"(?i)(\b[A-Z]:\\Users\\|/home/|/Users/)(?!Public\b|Default\b|\[)([^\\/\s\"']+)")),
]
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

LABELS = {**BASE_LABELS, **{code: label for code, label, _ in LAB_PATTERNS}, "public_ip": "공인 IP"}


def _is_public(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False  # 버전 번호 같은 숫자열
    return address.is_global


def mask_text(text: str | None, mask_public_ip: bool = True) -> MaskResult:
    result = base_mask(text)
    value, counts = result.text, dict(result.counts)
    for code, label, pattern in LAB_PATTERNS:
        if code == "password":
            value, n = pattern.subn(rf"\g<1>\g<2>[마스킹:{label}]", value)
        elif code == "user_dir":
            value, n = pattern.subn(rf"\g<1>[마스킹:{label}]", value)
        else:
            value, n = pattern.subn(f"[마스킹:{label}]", value)
        if n:
            counts[code] = counts.get(code, 0) + n
    if mask_public_ip:
        hits = 0

        def repl(match: re.Match) -> str:
            nonlocal hits
            if _is_public(match.group(0)):
                hits += 1
                return "[마스킹:공인 IP]"
            return match.group(0)

        value = IPV4.sub(repl, value)
        if hits:
            counts["public_ip"] = hits
    return MaskResult(value, counts)


def blackout(image, regions: list[tuple[int, int, int, int]]):
    """PIL 이미지에서 (x, y, w, h) 영역들을 검게 칠한 사본을 돌려준다."""
    if not regions:
        return image
    from PIL import ImageDraw

    out = image.copy()
    draw = ImageDraw.Draw(out)
    for x, y, w, h in regions:
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=(0, 0, 0))
    return out


# 외부(Claude API)로 보낼 수 있는 항목. 기록·고지는 이 이름으로만 한다.
ITEM_LABELS = {
    "screenshot": "VM 창 화면 캡처(축소·마스크 영역 가림, 저장 안 함)",
    "window_title": "창 제목(마스킹)",
    "tool_log": "분석 도구 로그 발췌(Procmon·Regshot 등, 마스킹)",
    "question": "학습자 질문(마스킹)",
    "history": "직전 단계 자막 요약(텍스트)",
}


class CollectionLog:
    """보낸 항목의 종류·시각·마스킹 개수만 JSON Lines로 남긴다(값은 남기지 않는다)."""

    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days

    def record(self, items: list[str], mask_counts: dict[str, int] | None = None, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": now.isoformat(timespec="seconds"), "items": sorted(set(items)), "masked": mask_counts or {}}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _entries(self) -> list[dict]:
        if not self.path.is_file():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                entry["_ts"] = datetime.fromisoformat(entry["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            entries.append(entry)
        return entries

    def purge(self, now: datetime | None = None) -> int:
        """보관 기간이 지난 기록을 지운다. 지운 줄 수를 돌려준다."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.retention_days)
        entries = self._entries()
        keep = [e for e in entries if e.get("kind") == "notice" or e["_ts"] >= cutoff]
        if len(keep) == len(entries):
            return 0
        self.path.write_text(
            "".join(json.dumps({k: v for k, v in e.items() if k != "_ts"}, ensure_ascii=False) + "\n" for e in keep),
            encoding="utf-8",
        )
        return len(entries) - len(keep)

    def summary(self, days: int = 30, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        items: Counter = Counter()
        masked: Counter = Counter()
        requests = 0
        for entry in self._entries():
            if entry.get("kind") == "notice" or entry["_ts"] < cutoff:
                continue
            requests += 1
            items.update(entry.get("items") or [])
            masked.update(entry.get("masked") or {})
        return {"days": days, "requests": requests, "items": dict(items), "masked": dict(masked)}

    def last_notice(self) -> datetime | None:
        notices = [e["_ts"] for e in self._entries() if e.get("kind") == "notice"]
        return max(notices) if notices else None

    def notice_due(self, every_days: int, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        last = self.last_notice()
        return last is None or now - last >= timedelta(days=every_days)

    def mark_notice(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 마지막 고지 기록은 하나만 유지
        lines = []
        if self.path.is_file():
            lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if '"kind": "notice"' not in ln]
        lines.append(json.dumps({"ts": now.isoformat(timespec="seconds"), "kind": "notice"}, ensure_ascii=False))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_notice(summary: dict, llm_mode: str, retention_days: int) -> str:
    """수집 '항목' 고지문(값은 포함하지 않는다)."""
    lines = [f"[LabCoach 수집 항목 고지 — 최근 {summary['days']}일]"]
    if llm_mode == "live":
        lines.append("외부 처리: Anthropic(Claude API) — 화면 설명 생성 목적, 요청 시에만 전송")
    else:
        lines.append("외부 처리: 없음(데모 모드)")
    lines.append(f"전송 요청 수: {summary['requests']}회")
    if summary["items"]:
        lines.append("전송한 항목:")
        for code, count in sorted(summary["items"].items()):
            lines.append(f"  - {ITEM_LABELS.get(code, code)}: {count}회")
    else:
        lines.append("전송한 항목: 없음")
    if summary["masked"]:
        lines.append("자동으로 가린 민감정보 항목(개수):")
        for code, count in sorted(summary["masked"].items()):
            lines.append(f"  - {LABELS.get(code, code)}: {count}")
    else:
        lines.append("자동으로 가린 민감정보: 없음")
    lines += [
        "저장하지 않는 것: 화면 이미지(LABCOACH_SAVE_FRAMES=1로 직접 켠 경우 제외), 마스킹 전 원래 값, 계정·위치 정보",
        f"이 기록(항목·시각·개수만)은 {retention_days}일 뒤 자동 삭제됩니다.",
    ]
    return "\n".join(lines)
