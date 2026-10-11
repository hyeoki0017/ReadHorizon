"""사이트별 기본값(프리셋).

주의: 아래 주소·선택자는 일반적인 한국 공공기관 로그인 화면 구조에 맞춘 '출발점'이며
실제 사이트에서 검증된 값이 아니다. 접수 전에 `python -m exam_booker inspect <주소>`로
화면 구조를 확인하고 config.yaml에서 덮어쓴다. 설정에 적은 값이 항상 프리셋보다 우선한다.
"""

from __future__ import annotations

import copy

# 비밀번호 칸이 있는 폼 안에서만 찾도록 범위를 좁힌다(검색창 등 오입력 방지).
_FORM = "form:has(input[type=password])"
_GENERIC_LOGIN = {
    "id_field": f"{_FORM} input:is([type=text],[type=email],:not([type])):visible",
    "pw_field": f"{_FORM} input[type=password]:visible",
    # 못 찾으면 비밀번호 칸에서 Enter
    "submit_button": f"{_FORM} :is(button, a, input[type=submit], input[type=button], input[type=image]):visible"
    ":is(:has-text('로그인'), [value*='로그인'], [alt*='로그인'])",
    "logged_in_marker": ":is(a, button, span):has-text('로그아웃'):visible",
}
_COMMON_BLOCKERS = [
    "#NetFunnel_Loading_Popup",  # 접속 대기열(넷퍼넬)
    "iframe[src*='recaptcha']",
    "img[src*='captcha' i]:visible",
]

SITES: dict[str, dict] = {
    "qnet": {
        "name": "Q-Net (한국산업인력공단)",
        "login_url": "https://www.q-net.or.kr/",
        "login": {"credential": "qnet", **_GENERIC_LOGIN},
        "selectors": {"blockers": _COMMON_BLOCKERS},
    },
    "kca": {
        "name": "KCA 한국방송통신전파진흥원 국가기술자격 (CQ-Net)",
        "login_url": "https://www.cq.or.kr/",
        "login": {"credential": "kca", **_GENERIC_LOGIN},
        "selectors": {"blockers": _COMMON_BLOCKERS},
    },
    "kisa": {
        "name": "KISA 한국인터넷진흥원 (응시하는 자격의 접수 사이트 주소로 바꿔 주세요)",
        "login_url": "https://www.kisa.or.kr/",
        "login": {"credential": "kisa", **_GENERIC_LOGIN},
        "selectors": {"blockers": _COMMON_BLOCKERS},
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """override가 우선. 목록은 합치지 않고 통째로 바꾼다."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_preset(raw: dict) -> dict:
    site = raw.get("site")
    if not site:
        return raw
    if site not in SITES:
        raise KeyError(site)
    preset = {k: v for k, v in SITES[site].items() if k != "name"}
    return deep_merge(preset, raw)
