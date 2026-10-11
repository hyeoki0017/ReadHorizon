"""브라우저 자동 조작 (Playwright).

원칙
- 로그인: 암호화 보관함의 아이디·비밀번호로 자동 입력(1회만 시도 — 계정 잠금 방지).
  실패하거나 자동 로그인 설정이 없으면 사람이 직접 로그인한다.
- 보안문자(CAPTCHA)·대기열·추가 인증은 사람이 처리한다. 감지되면 멈추고 알린다(우회하지 않음).
- 새로고침 간격은 설정 하한(1초) 이상만 허용한다.
- 최종 접수 확정·결제는 기본값으로 사람이 직접 누른다(auto_confirm: false).
- 사이트의 확인창(confirm/alert)은 '확인'으로 처리하고 내용을 기록한다.
  (Playwright 연결 중에는 처리하지 않으면 자동 '취소'되어 사용자가 누른 최종 접수까지 취소되기 때문)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from . import clock
from .config import Config
from .matching import pick_slot
from .vault import Credential, mask

Log = Callable[[str], None]
CredentialProvider = Callable[[], Credential]


def alert(log: Log, msg: str) -> None:
    log("\a" + msg)  # \a: 터미널 알림음


class Booker:
    def __init__(
        self,
        cfg: Config,
        context: BrowserContext,
        page: Page,
        log: Log = print,
        ask: Callable[[str], str] = input,
        sleep: Callable[[float], None] = time.sleep,
        credentials: CredentialProvider | None = None,
    ):
        self.cfg = cfg
        self.credentials = credentials
        self.context = context
        self.page = page
        self.log = log
        self.ask = ask
        self.sleep = sleep

    # ---- 준비 단계 -------------------------------------------------------
    def _on_dialog(self, dialog) -> None:
        self.log(f"  [확인창] {dialog.message.strip()[:80]} → 확인")
        try:
            dialog.accept()
        except Exception:  # noqa: BLE001,S110 - 이미 닫힌 창
            pass

    def _watch(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)

    @property
    def auto_login_enabled(self) -> bool:
        return self.cfg.login is not None and self.credentials is not None

    def login(self) -> None:
        if self.auto_login_enabled and self.auto_login():
            return
        self.page.goto(self.cfg.login_url)
        self.ask("\n브라우저 창에서 직접 로그인한 뒤, 여기서 Enter를 누르세요 ▶ ")

    def _is_logged_in(self, page: Page) -> bool:
        marker = self.cfg.login.logged_in_marker if self.cfg.login else ""
        if not marker:
            return False
        try:
            return page.locator(marker).first.is_visible()
        except Exception:  # noqa: BLE001 - 잘못된 선택자
            return False

    def auto_login(self) -> bool:
        """보관함 정보로 로그인 폼을 채워 1회 시도. 성공 여부를 돌려준다(실패 시 수동 로그인으로 넘어감)."""
        lc = self.cfg.login
        page = self.page
        page.goto(self.cfg.login_url, wait_until="domcontentloaded")
        if self._is_logged_in(page):
            self.log("이미 로그인돼 있습니다.")
            return True
        self.wait_blockers_cleared(page)
        try:
            cred = self.credentials()
        except Exception as e:  # noqa: BLE001
            alert(self.log, f"로그인 정보를 불러오지 못했습니다: {e}")
            return False
        try:
            id_box = page.locator(lc.id_field).first
            pw_box = page.locator(lc.pw_field).first
            id_box.wait_for(state="visible", timeout=10000)
            id_box.fill(cred.username)
            pw_box.fill(cred.password)
            self.log(f"자동 로그인 시도: {mask(cred.username)}")
            submit = page.locator(lc.submit_button).first if lc.submit_button else None
            if submit is not None and submit.count() and submit.is_visible():
                submit.click()
            else:
                pw_box.press("Enter")
        except PWError:
            alert(self.log, "로그인 입력칸을 찾지 못했습니다(login.id_field / pw_field 확인).")
            return False
        finally:
            del cred  # 평문 참조를 바로 끊는다

        deadline = time.time() + lc.timeout_seconds
        while time.time() < deadline:
            page = self._active_page()
            if self._is_logged_in(page):
                self.log("로그인 완료.")
                return True
            if self._visible_blocker(page):
                self.wait_blockers_cleared(page)  # 보안문자·추가 인증은 사람이
                deadline = time.time() + lc.timeout_seconds
            self.sleep(0.5)
        if not lc.logged_in_marker:
            self.log("로그인 확인 표시(login.logged_in_marker)가 없어 성공 여부를 알 수 없습니다.")
        alert(self.log, "자동 로그인을 확인하지 못했습니다. 계정 잠금 방지를 위해 다시 시도하지 않습니다.")
        return False

    def wait_for_open(self) -> None:
        offset = 0.0
        if self.cfg.sync_server_time:
            measured = clock.measure_server_offset(self.cfg.apply_url)
            if measured is None:
                self.log("서버 시각을 확인하지 못해 PC 시계를 기준으로 합니다.")
            else:
                offset = measured
                self.log(f"서버 시각 보정: PC 시계보다 {offset:+.1f}초")
        start = self.cfg.open_at - timedelta(seconds=self.cfg.lead_seconds)
        self.log(f"접수 시작 {self.cfg.open_at:%Y-%m-%d %H:%M:%S} (한국 시간), {self.cfg.lead_seconds:g}초 전부터 시도합니다.")
        clock.wait_until(start, offset, on_tick=lambda s: self.log(f"  시작까지 {s:,.0f}초"), sleep=self.sleep)

    # ---- 사람이 처리해야 하는 화면 --------------------------------------
    def _visible_blocker(self, page: Page) -> str | None:
        for sel in self.cfg.selectors.blockers:
            try:
                if page.locator(sel).first.is_visible():
                    return sel
            except Exception:  # noqa: BLE001,S112 - 잘못된 선택자 하나로 전체가 멈추지 않게
                continue
        return None

    def wait_blockers_cleared(self, page: Page) -> None:
        sel = self._visible_blocker(page)
        if not sel:
            return
        alert(self.log, f"보안문자/대기열 화면이 감지됐습니다({sel}). 브라우저에서 직접 처리해 주세요. 자동으로 기다립니다…")
        while self._visible_blocker(page):
            self.sleep(1.0)
        self.log("  처리 확인 — 계속 진행합니다.")

    # ---- 접수 버튼 → 일정 선택 -----------------------------------------
    def _active_page(self) -> Page:
        page = self.context.pages[-1]  # 새 창(팝업)으로 열리는 사이트 대응
        if page is not self.page:
            self._watch(page)
            self.page = page
        return page

    def click_apply_when_open(self, deadline: float) -> bool:
        sel = self.cfg.selectors.apply_button
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            page = self._active_page()
            if attempt > 1:
                try:
                    page.goto(self.cfg.apply_url, wait_until="domcontentloaded")
                except PWTimeout:
                    self.log("  페이지 응답 지연 — 다시 시도합니다.")
                    continue
            self.wait_blockers_cleared(page)
            btn = page.locator(sel).first
            try:
                if btn.is_visible() and btn.is_enabled():
                    btn.click()
                    self.log(f"접수 버튼 클릭 ({attempt}번째 시도)")
                    page.wait_for_timeout(300)
                    return True
            except PWTimeout:
                pass
            if attempt == 1 or attempt % 10 == 0:
                self.log(f"  아직 접수 전 — {self.cfg.poll_interval:g}초 간격으로 새로고침 중 ({attempt}회)")
            self.sleep(self.cfg.poll_interval)
        return False

    def choose_slot(self, deadline: float) -> bool:
        s = self.cfg.selectors
        rounds = 0
        while time.time() < deadline:
            rounds += 1
            page = self._active_page()
            self.wait_blockers_cleared(page)
            for sel in s.before_slots:
                try:
                    page.locator(sel).first.click(timeout=5000)
                except PWTimeout:
                    self.log(f"  사전 단계 선택자를 찾지 못했습니다: {sel}")
            try:
                page.locator(s.slot_row).first.wait_for(timeout=10000)
            except PWTimeout:
                self.log("  시험 일정 목록이 아직 보이지 않습니다.")
                self.sleep(self.cfg.poll_interval)
                continue
            rows = page.locator(s.slot_row)
            texts = [t.strip() for t in rows.all_inner_texts()]
            picked = pick_slot(texts, self.cfg.preferences, self.cfg.full_keywords)
            if picked is None:
                if rounds == 1:
                    alert(self.log, f"희망 조건에 맞는 빈자리가 없습니다(일정 {len(texts)}건). 취소 자리를 기다리며 계속 확인합니다.")
                self.sleep(self.cfg.poll_interval)
                if not s.before_slots:  # '조회' 버튼이 없으면 새로고침으로 목록 갱신
                    page.reload(wait_until="domcontentloaded")
                continue
            idx, pref = picked
            rank = self.cfg.preferences.index(pref) + 1
            self.log(f"선택: {rank}순위 [{pref.label()}] → {' '.join(texts[idx].split())[:100]}")
            rows.nth(idx).locator(s.slot_choose).first.click()
            page.wait_for_timeout(300)
            return True
        return False

    def finish(self) -> None:
        page = self._active_page()
        sel = self.cfg.selectors.confirm_button
        if self.cfg.auto_confirm and sel:
            page.locator(sel).first.click(timeout=10000)
            alert(self.log, "최종 접수 버튼까지 눌렀습니다. 결제·접수 완료 여부를 브라우저에서 꼭 확인하세요.")
        else:
            if sel:
                try:
                    page.locator(sel).first.evaluate("e => { e.style.outline = '4px solid red'; e.scrollIntoView(); }")
                except Exception:  # noqa: BLE001,S110 - 강조 표시는 실패해도 무방
                    pass
            alert(self.log, "자리를 선택했습니다! 내용 확인 후 최종 접수·결제는 브라우저에서 직접 진행하세요.")

    def keep_open(self) -> None:
        """사용자가 브라우저 창을 닫을 때까지 연결을 유지(확인창 처리를 계속하기 위해)."""
        self.log("\n남은 절차(최종 접수·결제)를 브라우저에서 마친 뒤 브라우저 창을 닫으면 프로그램이 끝납니다.")
        try:
            while self.context.pages:
                self.context.pages[-1].wait_for_timeout(500)
        except Exception:  # noqa: BLE001,S110 - 사용자가 창을 닫음
            pass

    # ---- 전체 흐름 -------------------------------------------------------
    def wait_for_login_time(self) -> None:
        """자동 로그인은 접수 직전에 해야 세션이 만료되지 않는다."""
        lead = self.cfg.login.lead_minutes if self.cfg.login else 0
        at = self.cfg.open_at - timedelta(minutes=lead)
        if clock.seconds_until(at) > 0:
            self.log(f"접수 {lead:g}분 전({at:%H:%M:%S})에 자동 로그인합니다. 창을 닫지 말고 기다려 주세요.")
            clock.wait_until(at, on_tick=lambda s: self.log(f"  로그인까지 {s:,.0f}초"), sleep=self.sleep)

    def run(self, skip_login: bool = False, skip_wait: bool = False) -> bool:
        self._watch(self.page)
        if not skip_login:
            if self.auto_login_enabled and not skip_wait:
                self.wait_for_login_time()
            self.login()
        if not skip_wait:
            self.wait_for_open()
        deadline = time.time() + self.cfg.give_up_after_minutes * 60
        self.page.goto(self.cfg.apply_url, wait_until="domcontentloaded")
        if not self.click_apply_when_open(deadline):
            alert(self.log, "제한 시간 안에 접수 버튼이 열리지 않았습니다.")
            return False
        if not self.choose_slot(deadline):
            alert(self.log, "제한 시간 안에 희망 조건의 자리를 잡지 못했습니다.")
            return False
        self.finish()
        return True
