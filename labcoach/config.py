"""환경 변수 기반 설정 (LABCOACH_*).

.env 파일이 있으면 읽되, 이미 설정된 환경 변수는 덮어쓰지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from readhorizon.config import VALID_EFFORTS, _bool_env, _int_env, _load_dotenv

MODES = ("malware", "attack", "defense", "general")
LEVELS = ("beginner", "intermediate", "advanced")

# 창 제목으로 VM 창을 찾을 때 쓰는 기본 문자열 (대소문자 무시, 부분 일치)
DEFAULT_WINDOW_HINTS = ("VirtualBox", "VMware")


def parse_region(value: str) -> tuple[int, int, int, int] | None:
    """'x,y,w,h' → (x, y, w, h). 비었거나 형식이 틀리면 None."""
    parts = [p.strip() for p in (value or "").split(",")]
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def parse_regions(value: str) -> list[tuple[int, int, int, int]]:
    """'x,y,w,h;x,y,w,h' → 영역 목록. 캡처 영역 기준 좌표(픽셀)."""
    regions = []
    for chunk in (value or "").split(";"):
        region = parse_region(chunk)
        if region:
            regions.append(region)
    return regions


@dataclass(frozen=True)
class Settings:
    llm_mode: str  # "live" | "demo"
    model: str = "claude-opus-5-5"
    # 실시간 응답이 중요해 기본은 low. 화면이 복잡하거나 설명이 얕으면 medium 이상으로 올린다
    effort: str = "low"
    mode: str = "malware"
    level: str = "beginner"
    # 자동 분석 간격(초)과, 화면이 이만큼(0~255 평균 밝기 차) 바뀌어야 다시 분석하는 기준
    interval_sec: int = 8
    change_threshold: float = 6.0
    # Claude로 보내기 전 캡처 이미지의 긴 변 최대 픽셀(작을수록 빠르고 저렴, 글자 판독은 떨어짐)
    max_image_edge: int = 1568
    window_hints: tuple[str, ...] = DEFAULT_WINDOW_HINTS
    region: tuple[int, int, int, int] | None = None
    # 보내기 전에 검게 칠할 영역(캡처 영역 기준 픽셀). 호스트 알림, 계정명 등이 보이는 곳
    mask_regions: list[tuple[int, int, int, int]] = field(default_factory=list)
    # 공인 IP를 가린다(사설·실습 대역 10.x, 172.16~31.x, 192.168.x 는 학습에 필요해 그대로 둔다)
    mask_public_ip: bool = True
    tts: bool = True
    tts_voice: str = ""
    tts_rate: int = 180
    hotkeys: bool = True
    # 최근 몇 단계의 자막을 다음 요청 맥락으로 보낼지 (화면 이미지는 보내지 않는다)
    history_steps: int = 6
    data_dir: Path = Path("data/labcoach")
    retention_days: int = 30
    notice_every_days: int = 30
    save_frames: bool = False

    @property
    def collection_log(self) -> Path:
        return self.data_dir / "collection.jsonl"


def _choice(name: str, default: str, choices) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in choices else default


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None:
        _load_dotenv(env_file)

    has_credentials = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    mode = os.getenv("LABCOACH_LLM_MODE", "").strip().lower()
    if mode not in {"live", "demo"}:
        mode = "live" if has_credentials else "demo"

    hints = tuple(h.strip() for h in os.getenv("LABCOACH_WINDOW_HINTS", "").split(",") if h.strip())
    try:
        threshold = float(os.getenv("LABCOACH_CHANGE_THRESHOLD", "") or 6.0)
    except ValueError:
        threshold = 6.0

    return Settings(
        llm_mode=mode,
        model=os.getenv("LABCOACH_MODEL", "").strip() or "claude-opus-5-5",
        effort=_choice("LABCOACH_EFFORT", "low", VALID_EFFORTS),
        mode=_choice("LABCOACH_MODE", "malware", MODES),
        level=_choice("LABCOACH_LEVEL", "beginner", LEVELS),
        interval_sec=min(600, max(2, _int_env("LABCOACH_INTERVAL_SEC", 8))),
        change_threshold=min(255.0, max(0.0, threshold)),
        max_image_edge=min(2576, max(512, _int_env("LABCOACH_MAX_IMAGE_EDGE", 1568))),
        window_hints=hints or DEFAULT_WINDOW_HINTS,
        region=parse_region(os.getenv("LABCOACH_REGION", "")),
        mask_regions=parse_regions(os.getenv("LABCOACH_MASK_REGIONS", "")),
        mask_public_ip=_bool_env("LABCOACH_MASK_PUBLIC_IP", True),
        tts=_bool_env("LABCOACH_TTS", True),
        tts_voice=os.getenv("LABCOACH_TTS_VOICE", "").strip(),
        tts_rate=min(400, max(80, _int_env("LABCOACH_TTS_RATE", 180))),
        hotkeys=_bool_env("LABCOACH_HOTKEYS", True),
        history_steps=min(20, max(0, _int_env("LABCOACH_HISTORY_STEPS", 6))),
        data_dir=Path(os.getenv("LABCOACH_DATA_DIR", "").strip() or "data/labcoach"),
        retention_days=max(1, _int_env("LABCOACH_RETENTION_DAYS", 30)),
        notice_every_days=max(1, _int_env("LABCOACH_NOTICE_EVERY_DAYS", 30)),
        save_frames=_bool_env("LABCOACH_SAVE_FRAMES", False),
    )
