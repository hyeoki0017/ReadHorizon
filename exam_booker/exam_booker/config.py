"""설정 파일(YAML) 읽기와 검증.

사이트마다 화면 구조가 다르므로 '어디를 누를지'(CSS 선택자)와
'무엇을 원하는지'(희망 날짜·시간·시험장 우선순위)를 모두 설정으로 받는다.
비밀번호 같은 개인정보는 설정에 넣지 않는다 — 로그인은 사용자가 직접 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .sites import SITES, apply_preset

KST = timezone(timedelta(hours=9))

# 사이트에 부담을 주거나 차단당하지 않도록 새로고침 간격의 하한을 둔다.
MIN_POLL_INTERVAL = 1.0


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Preference:
    """희망 조건 하나. 비워 둔 항목은 '아무거나 괜찮음'으로 본다."""

    venue: str = ""
    date: str = ""
    time: str = ""

    def keywords(self) -> list[str]:
        return [k for k in (self.venue, self.date, self.time) if k]

    def label(self) -> str:
        return " / ".join(self.keywords()) or "(조건 없음 — 아무 자리나)"


@dataclass(frozen=True)
class Selectors:
    apply_button: str
    slot_row: str
    slot_choose: str
    confirm_button: str = ""
    before_slots: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoginConfig:
    """자동 로그인 설정. 아이디·비밀번호 자체는 여기 없고, 암호화 보관함의 항목 이름만 가리킨다."""

    credential: str
    id_field: str
    pw_field: str
    submit_button: str = ""
    logged_in_marker: str = ""
    vault: str = "credentials.enc"
    lead_minutes: float = 5.0
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class Config:
    login_url: str
    apply_url: str
    open_at: datetime
    preferences: tuple[Preference, ...]
    selectors: Selectors
    full_keywords: tuple[str, ...] = ("마감", "매진", "불가")
    lead_seconds: float = 2.0
    poll_interval: float = 1.0
    give_up_after_minutes: float = 30.0
    auto_confirm: bool = False
    profile_dir: str = "browser_profile"
    sync_server_time: bool = True
    login: LoginConfig | None = None
    site: str = ""
    # 자동 모드 허용 근거(기관 서면 답변 날짜·번호 등). 비어 있으면 실제 사이트에서 자동 모드 실행 거부
    site_permission: str = ""
    extra: dict = field(default_factory=dict)


def _parse_open_at(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except ValueError as e:
            raise ConfigError(f"open_at 형식이 잘못됐습니다 (예: 2026-11-02 10:00:00): {value!r}") from e
    # 시간대를 안 적으면 한국 시간으로 본다.
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


def _str_tuple(value, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name}은(는) 문자열 목록이어야 합니다.")
    return tuple(value)


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("설정 파일 최상위는 키-값 형태여야 합니다.")
    try:
        raw = apply_preset(raw)
    except KeyError as e:
        raise ConfigError(f"알 수 없는 site: {e.args[0]!r} (가능: {', '.join(SITES)})") from None

    for key in ("apply_url", "open_at", "preferences", "selectors"):
        if key not in raw:
            raise ConfigError(f"필수 항목이 없습니다: {key}")

    prefs_raw = raw["preferences"]
    if not isinstance(prefs_raw, list) or not prefs_raw:
        raise ConfigError("preferences에 희망 조건을 1개 이상 우선순위 순으로 적어 주세요.")
    prefs = []
    for i, p in enumerate(prefs_raw, 1):
        if not isinstance(p, dict):
            raise ConfigError(f"preferences {i}번째 항목은 venue/date/time 키를 가진 형태여야 합니다.")
        unknown = set(p) - {"venue", "date", "time"}
        if unknown:
            raise ConfigError(f"preferences {i}번째 항목에 알 수 없는 키: {sorted(unknown)}")
        prefs.append(Preference(**{k: str(v).strip() for k, v in p.items() if v is not None}))

    s = raw["selectors"]
    if not isinstance(s, dict):
        raise ConfigError("selectors는 키-값 형태여야 합니다.")
    for key in ("apply_button", "slot_row", "slot_choose"):
        if not s.get(key):
            raise ConfigError(f"selectors.{key}가 필요합니다.")
    selectors = Selectors(
        apply_button=s["apply_button"],
        slot_row=s["slot_row"],
        slot_choose=s["slot_choose"],
        confirm_button=s.get("confirm_button") or "",
        before_slots=_str_tuple(s.get("before_slots"), "selectors.before_slots"),
        blockers=_str_tuple(s.get("blockers"), "selectors.blockers"),
    )

    poll = float(raw.get("poll_interval", 1.0))
    if poll < MIN_POLL_INTERVAL:
        raise ConfigError(
            f"poll_interval은 {MIN_POLL_INTERVAL}초 이상이어야 합니다 "
            "(너무 잦은 새로고침은 사이트 차단·이용 제한 사유가 됩니다)."
        )

    auto_confirm = bool(raw.get("auto_confirm", False))
    if auto_confirm and not selectors.confirm_button:
        raise ConfigError("auto_confirm을 켜려면 selectors.confirm_button이 필요합니다.")

    login = _parse_login(raw.get("login"))

    return Config(
        login=login,
        site=str(raw.get("site") or ""),
        site_permission=str(raw.get("site_permission") or "").strip(),
        login_url=str(raw.get("login_url") or raw["apply_url"]),
        apply_url=str(raw["apply_url"]),
        open_at=_parse_open_at(raw["open_at"]),
        preferences=tuple(prefs),
        selectors=selectors,
        full_keywords=_str_tuple(raw.get("full_keywords", ["마감", "매진", "불가"]), "full_keywords"),
        lead_seconds=float(raw.get("lead_seconds", 2.0)),
        poll_interval=poll,
        give_up_after_minutes=float(raw.get("give_up_after_minutes", 30.0)),
        auto_confirm=auto_confirm,
        profile_dir=str(raw.get("profile_dir", "browser_profile")),
        sync_server_time=bool(raw.get("sync_server_time", True)),
    )


def _parse_login(value) -> LoginConfig | None:
    if value is None or value is False:
        return None
    if not isinstance(value, dict):
        raise ConfigError("login은 키-값 형태여야 합니다.")
    if value.get("enabled") is False:
        return None
    for key in ("credential", "id_field", "pw_field"):
        if not value.get(key):
            raise ConfigError(f"login.{key}가 필요합니다.")
    for key in ("username", "password", "user_id", "id", "pw", "passwd", "secret"):
        if key in value:
            raise ConfigError(
                f"login.{key}: 설정 파일에 아이디·비밀번호를 적지 마세요. "
                "'python -m exam_booker vault add <이름>'으로 암호화 보관함에 저장합니다."
            )
    lead = float(value.get("lead_minutes", 5.0))
    if lead < 0:
        raise ConfigError("login.lead_minutes는 0 이상이어야 합니다.")
    return LoginConfig(
        credential=str(value["credential"]),
        id_field=str(value["id_field"]),
        pw_field=str(value["pw_field"]),
        submit_button=str(value.get("submit_button") or ""),
        logged_in_marker=str(value.get("logged_in_marker") or ""),
        vault=str(value.get("vault") or "credentials.enc"),
        lead_minutes=lead,
        timeout_seconds=float(value.get("timeout_seconds", 30.0)),
    )


def is_practice_url(url: str) -> bool:
    """모의 사이트(파일·내 PC)인지 — 연습에는 허용 근거가 필요 없다."""
    u = url.lower()
    return u.startswith("file://") or u.startswith(("http://localhost", "http://127.0.0.1"))


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"설정 파일이 없습니다: {p} (config.example.yaml을 복사해 만드세요)")
    with p.open(encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))
