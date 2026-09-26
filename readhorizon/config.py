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
        fulltext_char_limit=max(1000, _int_env("RH_FULLTEXT_CHAR_LIMIT", 200_000)),
    )
