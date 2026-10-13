"""저장한 접수 화면으로 '접수 보조' 동작을 오프라인 점검 — 실제 사이트에 접속하지 않는다.

사용자가 평소처럼 직접 접속해 저장한(Ctrl+S) 화면 파일을
1) 정리(스크립트·숨은 값·입력값 삭제, 이름·연락처 가림)하고
2) 내 PC 안에서만(외부 요청 전부 차단) 크롬 확장의 content.js를 그대로 실행해
어떤 줄이 초록색(추천)으로 표시되는지 보고한다.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .sites import SITES

EXT_DIR = Path(__file__).resolve().parents[1] / "extension"

# ---- 정리(개인정보·위험 요소 제거) ---------------------------------------------
_REMOVE_BLOCKS = re.compile(r"<(script|noscript|iframe|object|embed|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_REMOVE_SINGLE = re.compile(r"<(script|iframe|base|embed)\b[^>]*/?>", re.I)
_META_REFRESH = re.compile(r"<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh[^>]*>", re.I)
_HIDDEN_INPUT = re.compile(r"<input\b[^>]*type\s*=\s*['\"]?hidden[^>]*>", re.I)  # 세션 토큰 등
_ON_ATTR = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_VALUE_ATTR = re.compile(r"(<(?:input|textarea|option)\b[^>]*?)\s+value\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_TEXTAREA = re.compile(r"(<textarea\b[^>]*>).*?(</textarea\s*>)", re.I | re.S)
_JS_URL = re.compile(r"(href|src|action)\s*=\s*(['\"])\s*javascript:[^'\"]*\2", re.I)

PERSONAL_PATTERNS = [
    (re.compile(r"\d{6}\s*-\s*[1-8]\d{6}"), "******-*******"),  # 주민등록번호
    (re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}"), "010-****-****"),  # 휴대전화
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),  # 이메일
    (re.compile(r"(?<![가-힣])[가-힣]{2,4}(\s*)(님|회원님)"), r"***\1\2"),  # '홍길동님'
    (re.compile(r"(성명|이름|수험자|응시자|생년월일)(\s*[:：]?\s*)[^<\s]{1,20}"), r"\1\2***"),
]


def mask_text(text: str) -> str:
    for pat, rep in PERSONAL_PATTERNS:
        text = pat.sub(rep, text)
    return text


def sanitize_html(html: str) -> str:
    html = _REMOVE_BLOCKS.sub("", html)
    html = _REMOVE_SINGLE.sub("", html)
    html = _META_REFRESH.sub("", html)
    html = _HIDDEN_INPUT.sub("", html)
    html = _ON_ATTR.sub("", html)
    html = _JS_URL.sub(r'\1="#"', html)
    while True:  # 한 태그에 value가 여럿일 수 있음
        new = _VALUE_ATTR.sub(r"\1", html)
        if new == html:
            break
        html = new
    html = _TEXTAREA.sub(r"\1\2", html)
    return mask_text(html)


# ---- 오프라인 실행 -------------------------------------------------------------
@dataclass
class Report:
    site: str
    slot_row: str
    rows: list[dict] = field(default_factory=list)  # {text, rank}
    apply_buttons: list[str] = field(default_factory=list)
    blocker: bool = False
    overlay: str = ""
    tables: list[dict] = field(default_factory=list)
    blocked_requests: list[str] = field(default_factory=list)

    @property
    def best(self) -> dict | None:
        return next((r for r in self.rows if r["rank"] == "best"), None)

    def render(self) -> str:
        n_match = sum(r["rank"] in ("best", "match") for r in self.rows)
        n_full = sum(r["rank"] == "full" for r in self.rows)
        out = [
            f"사이트 기본값: {self.site} · 일정 행 선택자: {self.slot_row}",
            f"일정 행 {len(self.rows)}개 · 희망 조건 일치(빈자리) {n_match}개 · 마감 {n_full}개",
        ]
        if self.best:
            out.append(f"✅ 추천(초록): {self.best['text'][:90]}")
        else:
            out.append("❌ 추천할 줄이 없습니다.")
        for r in self.rows:
            if r["rank"] in ("match", "full"):
                out.append(f"   {'후보' if r['rank'] == 'match' else '마감'}: {r['text'][:90]}")
        out.append(f"원서접수 버튼 후보 {len(self.apply_buttons)}개" + (f": {', '.join(self.apply_buttons[:5])}" if self.apply_buttons else ""))
        out.append(f"보안문자·대기열 감지: {'있음' if self.blocker else '없음'}")
        if not self.rows or not self.best:
            out.append("\n[도움말] 일정 줄을 못 찾았거나 조건이 안 맞습니다.")
            out.append("  - 희망 조건 글자가 화면 표기와 같은지 확인 (예: '상암고' vs '상암고등학교' 일부)")
            if self.tables:
                out.append("  - 화면의 표 목록 → 확장 설정 '고급 > 일정 행 선택자'에 '<표> tbody tr' 형태로 입력:")
                out += [f"      {t['sel']} tbody tr   (행 {t['rows']}개, 머리글: {t['head']})" for t in self.tables[:8]]
        if self.blocked_requests:
            out.append(f"\n(외부 요청 {len(self.blocked_requests)}건 차단됨 — 실제 사이트에는 접속하지 않았습니다)")
        return "\n".join(out)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 접속 기록을 화면에 찍지 않음
        pass


_TABLES_JS = r"""
() => [...document.querySelectorAll('table')].map(t => ({
  sel: t.id ? '#' + CSS.escape(t.id) : (t.className ? 'table.' + [...t.classList].map(c => CSS.escape(c)).join('.') : 'table'),
  rows: t.querySelectorAll('tbody tr').length,
  head: [...t.querySelectorAll('th')].map(th => th.innerText.trim()).slice(0, 6).join(' | '),
})).filter(t => t.rows > 0)
"""


def check_page(
    snapshot: str | Path,
    prefs_text: str,
    site: str = "qnet",
    slot_row: str = "",
    executable_path: str | None = None,
) -> Report:
    from playwright.sync_api import sync_playwright

    if site not in SITES:
        raise ValueError(f"알 수 없는 site: {site} (가능: {', '.join(SITES)})")
    raw = Path(snapshot).read_text(encoding="utf-8", errors="replace")
    clean = sanitize_html(raw)

    # 확장 sites.js의 사이트 기본값을 그대로 쓰되, 모의 서버(127.0.0.1)에서 돌리므로 overrides.mock으로 넘긴다
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "page.html").write_text(clean, encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=tmp)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{srv.server_address[1]}"
        blocked: list[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=executable_path)
                ctx = browser.new_context(java_script_enabled=True)

                def only_local(route):
                    if route.request.url.startswith(origin):
                        route.continue_()
                    else:  # 저장 화면이 실제 사이트의 이미지·CSS·스크립트를 부르면 차단
                        blocked.append(route.request.url.split("?")[0])
                        route.abort()

                ctx.route("**/*", only_local)
                page = ctx.new_page()
                page.goto(f"{origin}/page.html", wait_until="domcontentloaded")
                page.add_script_tag(path=str(EXT_DIR / "sites.js"))
                site_js = page.evaluate("s => ExamSites.SITES[s]", site)
                override = {k: site_js[k] for k in ("slotRow", "applyTexts", "blockers", "fullKeywords")}
                if slot_row:
                    override["slotRow"] = slot_row
                settings = {
                    "enabled": True,
                    "openAt": "2000-01-01T00:00:00+09:00",
                    "prefsText": prefs_text,
                    "overrides": {"mock": override},
                    "consent": {"version": 1, "at": "offline-check"},
                }
                # 확장 API(chrome.*) 자리에 설정만 돌려주는 가짜 객체를 둔다
                stub_settings = json.dumps(settings, ensure_ascii=False)
                page.add_script_tag(
                    content="globalThis.chrome={storage:{local:{get:async()=>({settings:" + stub_settings + "})},"
                    "onChanged:{addListener(){}}},runtime:{sendMessage(){}}};"
                )
                for f in ("matching.js", "content.js"):
                    page.add_script_tag(path=str(EXT_DIR / f))
                page.wait_for_selector("#exam-assist-overlay", state="attached", timeout=5000)
                page.wait_for_timeout(1200)
                rows = page.eval_on_selector_all(
                    override["slotRow"],
                    "rs => rs.map(r => ({text: (r.innerText||'').replace(/\\s+/g,' ').trim(),"
                    " rank: r.getAttribute('data-eb-rank') || ''}))",
                )
                report = Report(
                    site=site,
                    slot_row=override["slotRow"],
                    rows=[{**r, "text": mask_text(r["text"])} for r in rows],
                    apply_buttons=page.eval_on_selector_all(
                        "[data-eb-apply]", "es => es.map(e => (e.innerText||e.value||e.alt||'').trim().slice(0,20))"
                    ),
                    overlay=page.evaluate("() => document.getElementById('exam-assist-overlay').shadowRoot.textContent"),
                    tables=page.evaluate(_TABLES_JS),
                    blocked_requests=blocked,
                )
                report.blocker = "보안문자" in report.overlay
                browser.close()
        finally:
            srv.shutdown()
    return report
