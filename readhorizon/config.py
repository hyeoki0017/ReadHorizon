"""환경 변수 기반 설정.

.env 파일이 있으면 읽되, 이미 설정된 환경 변수는 덮어쓰지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    llm_mode: str  # "live" | "demo"
    model: str
    effort: str
    retention_days: int
    nl_cert_key: str | None
    max_upload_mb: int
    fulltext_char_limit: int
    # 호출 제한(0이면 끔): 분석·질문(LLM 호출) 접속 IP별 분당, 프로세스 전체 하루, 작품 등록 IP별 분당
    llm_per_minute: int = 10
    llm_daily: int = 1000
    create_per_minute: int = 30
    # 접속 로그(IP 포함)는 기본으로 남기지 않는다(개인정보 최소 수집)
    access_log: bool = False

    @property
    def bib_enabled(self) -> bool:
        return bool(self.nl_cert_key)


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None:
        _load_dotenv(env_file)

    has_credentials = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    mode = os.getenv("RH_LLM_MODE", "").strip().lower()
    if mode not in {"live", "demo"}:
        mode = "live" if has_credentials else "demo"

    effort = os.getenv("RH_EFFORT", "high").strip().lower()
    if effort not in VALID_EFFORTS:
        effort = "high"

    return Settings(
        db_path=Path(os.getenv("RH_DB_PATH", "data/readhorizon.db")),
        llm_mode=mode,
        model=os.getenv("RH_MODEL", "").strip() or "claude-opus-5",
        effort=effort,
        retention_days=max(1, _int_env("RH_RETENTION_DAYS", 180)),
        nl_cert_key=os.getenv("RH_NL_CERT_KEY", "").strip() or None,
        max_upload_mb=max(1, _int_env("RH_MAX_UPLOAD_MB", 20)),
        fulltext_char_limit=max(5000, _int_env("RH_FULLTEXT_CHAR_LIMIT", 200_000)),
        llm_per_minute=max(0, _int_env("RH_LLM_PER_MINUTE", 10)),
        llm_daily=max(0, _int_env("RH_LLM_DAILY_LIMIT", 1000)),
        create_per_minute=max(0, _int_env("RH_CREATE_PER_MINUTE", 30)),
        access_log=_bool_env("RH_ACCESS_LOG", False),
    )
