"""접수 사이트 화면 구조 살펴보기 — 선택자(selectors)를 맞추는 데 쓴다.

입력칸의 '값'은 절대 읽지 않는다(이름·종류·자리표시 글자만 출력).
"""

from __future__ import annotations

from playwright.sync_api import Page

_JS = r"""
() => {
  const sel = (e) => {
    if (e.id) return '#' + CSS.escape(e.id);
    const tag = e.tagName.toLowerCase();
    const name = e.getAttribute('name');
    if (name) return `${tag}[name="${name}"]`;
    const cls = [...e.classList].slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
    return tag + cls;
  };
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => (e.innerText || e.value || e.alt || e.title || '').trim().replace(/\s+/g, ' ').slice(0, 30);
  const inputs = [...document.querySelectorAll('input, select, textarea')]
    .filter(e => vis(e) && e.type !== 'hidden')
    .map(e => ({sel: sel(e), type: e.type || e.tagName.toLowerCase(), hint: e.placeholder || e.title || e.getAttribute('aria-label') || ''}));
  const buttons = [...document.querySelectorAll('button, a, input[type=submit], input[type=button], input[type=image]')]
    .filter(e => vis(e) && /로그인|접수|선택|조회|검색|확인|신청|다음|결제/.test(txt(e)))
    .map(e => ({sel: sel(e), text: txt(e)}));
  const tables = [...document.querySelectorAll('table')].filter(vis)
    .map(t => ({sel: sel(t), rows: t.querySelectorAll('tbody tr').length,
                head: [...t.querySelectorAll('th')].map(th => th.innerText.trim()).slice(0, 8).join(' | ')}));
  const frames = [...document.querySelectorAll('iframe')].map(f => f.src).filter(Boolean);
  return {inputs, buttons: buttons.slice(0, 60), tables, frames};
}
"""


def describe(page: Page) -> str:
    info = page.evaluate(_JS)
    out = [f"주소: {page.url}", f"제목: {page.title()}", "", "[입력칸] (값은 표시하지 않음)"]
    out += [f"  {i['type']:<10} {i['sel']}  {i['hint']}" for i in info["inputs"]] or ["  (없음)"]
    out += ["", "[버튼·링크] (로그인/접수/선택/조회 등)"]
    out += [f"  {b['text']:<16} {b['sel']}" for b in info["buttons"]] or ["  (없음)"]
    out += ["", "[표] slot_row 후보 → '<표 선택자> tbody tr'"]
    out += [f"  {t['sel']}  행 {t['rows']}개  머리글: {t['head']}" for t in info["tables"]] or ["  (없음)"]
    if info["frames"]:
        out += ["", "[iframe] 화면이 iframe 안에 있으면 해당 주소를 직접 열어 확인하세요."]
        out += [f"  {f}" for f in info["frames"]]
    return "\n".join(out)
