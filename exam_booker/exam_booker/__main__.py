"""실행

  python -m exam_booker run config.yaml [--check] [--now]   접수 도우미 실행
  python -m exam_booker vault add qnet                      로그인 정보 암호화 저장
  python -m exam_booker vault list | remove qnet            저장 목록(가림 처리) / 삭제
  python -m exam_booker inspect https://...                 화면 구조(선택자) 확인
  python -m exam_booker sites                               사이트 프리셋 목록
  python -m exam_booker cleanup [--vault-too]               접수 후 로그인 흔적 삭제
  python -m exam_booker check-page 저장화면.html --prefs "상암고|2026-11-21"
                                                            저장한 화면으로 보조 모드 오프라인 점검
  python -m exam_booker sanitize 저장화면.html -o 공유용.html  저장 화면에서 개인정보·스크립트 제거
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from pathlib import Path

from .config import ConfigError, is_practice_url, load_config
from .sites import SITES
from .vault import Credential, Vault, VaultError, mask

DEFAULT_VAULT = "credentials.enc"


def _disable_password_saving(profile_dir: str) -> None:
    """브라우저 자체 비밀번호 저장 기능을 끈다(보관함 밖에 평문 비밀번호가 남지 않게)."""
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    except (OSError, ValueError):
        prefs = {}
    prefs["credentials_enable_service"] = False
    prefs.setdefault("profile", {})["password_manager_enabled"] = False
    prefs["autofill"] = {**prefs.get("autofill", {}), "profile_enabled": False, "credit_card_enabled": False}
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")


def _launch(pw, profile_dir: str):
    _disable_password_saving(profile_dir)
    return pw.chromium.launch_persistent_context(
        profile_dir,
        headless=False,
        no_viewport=True,
        # 설치된 Playwright 브라우저 대신 다른 Chromium/Chrome을 쓰려면 경로 지정
        executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None,
    )


# ---- vault -----------------------------------------------------------------
def _open_or_create(path: str, create: bool) -> Vault:
    if Path(path).exists():
        return Vault.open(path, getpass.getpass("마스터 비밀번호: "))
    if not create:
        raise VaultError(f"보관함이 없습니다: {path}")
    print(f"새 보관함을 만듭니다: {path}")
    m1 = getpass.getpass("새 마스터 비밀번호(8자 이상, 잊으면 복구 불가): ")
    if m1 != getpass.getpass("한 번 더: "):
        raise VaultError("두 비밀번호가 다릅니다.")
    return Vault.create(path, m1)


def cmd_vault(args) -> int:
    try:
        v = _open_or_create(args.vault, create=args.action == "add")
    except VaultError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    try:
        if args.action == "add":
            if not args.site:
                print("사이트 이름을 적어 주세요 (예: vault add qnet)", file=sys.stderr)
                return 2
            user = input(f"[{args.site}] 아이디: ").strip()
            pw = getpass.getpass(f"[{args.site}] 비밀번호(화면에 표시되지 않음): ")
            v.put(args.site, Credential(user, pw))
            del pw
            print(f"저장했습니다: {args.site} ({mask(user)}) → {args.vault} (암호화됨)")
        elif args.action == "list":
            for site in v.sites():
                print(f"  {site:<10} {mask(v.get(site).username)}")
            if not v.sites():
                print("  (저장된 정보 없음)")
        elif args.action == "remove":
            print("삭제했습니다." if v.remove(args.site or "") else "해당 항목이 없습니다.")
    except VaultError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    finally:
        v.lock()
    return 0


# ---- run -------------------------------------------------------------------
def cmd_run(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2

    if cfg.site:
        print(f"사이트: {SITES[cfg.site]['name']}  (프리셋 선택자는 미검증 — inspect로 확인 권장)")
    print(f"접수 시작: {cfg.open_at:%Y-%m-%d %H:%M:%S %Z}")
    for i, p in enumerate(cfg.preferences, 1):
        print(f"  {i}순위: {p.label()}")
    if cfg.login:
        print(f"로그인: 자동 (보관함 항목 '{cfg.login.credential}', 접수 {cfg.login.lead_minutes:g}분 전)")
    else:
        print("로그인: 직접")
    print(f"새로고침 간격 {cfg.poll_interval:g}초, 최종 확정 {'자동' if cfg.auto_confirm else '직접'}")
    practice = is_practice_url(cfg.apply_url) and is_practice_url(cfg.login_url)
    if cfg.site_permission:
        print(f"자동 모드 허용 근거: {cfg.site_permission}")
    elif not practice:
        print(
            "\n[자동 모드 잠김] 실제 접수 사이트에서 자동 로그인·자동 클릭을 하려면 기관의 허용 근거가 필요합니다.\n"
            "  - 기관에 서면 질의 후 답변 날짜·번호를 config의 site_permission에 적으세요.\n"
            "  - 허용 전에는 크롬 확장 '접수 보조 모드'(extension/)를 쓰세요. 클릭·입력 없이 빈자리만 표시합니다.",
            file=sys.stderr,
        )
        return 2
    if args.check:
        return 0

    vault = None
    provider = None
    if cfg.login:
        if "pw:api" in os.environ.get("DEBUG", "") or os.environ.get("PWDEBUG"):
            print("오류: Playwright 디버그 로그(DEBUG=pw:api / PWDEBUG)가 켜져 있으면 입력한 비밀번호가 "
                  "로그에 남을 수 있어 자동 로그인을 거부합니다.", file=sys.stderr)
            return 2
        try:
            vault = Vault.open(cfg.login.vault, getpass.getpass("마스터 비밀번호: "))
            vault.get(cfg.login.credential)  # 지금 확인해 두고, 평문은 로그인 직전에 다시 복호화
        except VaultError as e:
            print(f"오류: {e}", file=sys.stderr)
            if vault:
                vault.lock()
            return 2
        site_key = cfg.login.credential
        provider = lambda: vault.get(site_key)  # noqa: E731

    from playwright.sync_api import sync_playwright

    from .booker import Booker

    try:
        with sync_playwright() as pw:
            context = _launch(pw, cfg.profile_dir)
            page = context.pages[0] if context.pages else context.new_page()
            booker = Booker(cfg, context, page, credentials=provider)
            ok = booker.run(skip_wait=args.now)
            if vault:
                vault.lock()  # 로그인 이후엔 필요 없음
            booker.keep_open()
            try:
                context.close()
            except Exception:  # noqa: BLE001,S110 - 이미 닫힘
                pass
    finally:
        if vault:
            vault.lock()
    return 0 if ok else 1


# ---- inspect ---------------------------------------------------------------
def cmd_inspect(args) -> int:
    from playwright.sync_api import sync_playwright

    from .inspect_page import describe

    with sync_playwright() as pw:
        context = _launch(pw, args.profile_dir)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url)
        while True:
            cmd = input("\n살펴볼 화면으로 이동한 뒤 Enter (끝내려면 q) ▶ ").strip().lower()
            if cmd == "q":
                break
            print(describe(context.pages[-1]))
        context.close()
    return 0


def cmd_cleanup(args) -> int:
    targets = [Path(args.profile_dir)] + ([Path(args.vault)] if args.vault_too else [])
    targets = [t for t in targets if t.exists()]
    if not targets:
        print("지울 것이 없습니다.")
        return 0
    print("다음을 영구 삭제합니다: " + ", ".join(map(str, targets)))
    if not args.yes and input("계속할까요? (y/N) ▶ ").strip().lower() != "y":
        print("취소했습니다.")
        return 1
    for t in targets:
        shutil.rmtree(t) if t.is_dir() else t.unlink()
    print("삭제했습니다. (로그인 쿠키" + (", 암호화 보관함" if args.vault_too else "") + ")")
    return 0


def cmd_check_page(args) -> int:
    from .check_page import check_page

    prefs = args.prefs.replace(";", "\n")
    if args.prefs_file:
        prefs = Path(args.prefs_file).read_text(encoding="utf-8")
    report = check_page(
        args.snapshot, prefs, site=args.site, slot_row=args.slot_row or "",
        executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None,
    )
    print(report.render())
    return 0 if report.best else 1


def cmd_sanitize(args) -> int:
    from .check_page import sanitize_html

    src = Path(args.snapshot)
    out = Path(args.output) if args.output else src.with_name(src.stem + ".clean.html")
    out.write_text(sanitize_html(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    print(f"정리본: {out}\n공유 전 파일을 열어 이름·수험번호 등 남은 개인정보가 없는지 꼭 한 번 더 확인하세요.")
    return 0


def cmd_sites(_args) -> int:
    for key, s in SITES.items():
        print(f"  {key:<6} {s['name']}\n         {s['login_url']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 예전 사용법 호환: python -m exam_booker config.yaml
    if argv and argv[0] not in {"run", "vault", "inspect", "sites", "cleanup", "check-page", "sanitize", "-h", "--help"}:
        argv.insert(0, "run")

    ap = argparse.ArgumentParser(prog="exam_booker", description="자격증 시험 1인 접수 도우미 (Q-Net·KCA·KISA)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="접수 도우미 실행")
    r.add_argument("config")
    r.add_argument("--check", action="store_true", help="설정만 검사")
    r.add_argument("--now", action="store_true", help="시각을 기다리지 않고 바로 시도(연습용)")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("vault", help="로그인 정보 암호화 보관함")
    v.add_argument("action", choices=["add", "list", "remove"])
    v.add_argument("site", nargs="?", help="항목 이름 (예: qnet, kca, kisa)")
    v.add_argument("--vault", default=DEFAULT_VAULT, help=f"보관함 파일 (기본 {DEFAULT_VAULT})")
    v.set_defaults(func=cmd_vault)

    i = sub.add_parser("inspect", help="화면 구조(선택자) 확인")
    i.add_argument("url")
    i.add_argument("--profile-dir", default="browser_profile")
    i.set_defaults(func=cmd_inspect)

    c = sub.add_parser("cleanup", help="접수 후 로그인 흔적(브라우저 쿠키, 선택 시 보관함) 삭제")
    c.add_argument("--profile-dir", default="browser_profile")
    c.add_argument("--vault", default=DEFAULT_VAULT)
    c.add_argument("--vault-too", action="store_true", help="암호화 보관함도 삭제")
    c.add_argument("--yes", action="store_true", help="확인 없이 삭제")
    c.set_defaults(func=cmd_cleanup)

    k = sub.add_parser("check-page", help="저장한 화면으로 보조 모드 오프라인 점검(사이트 접속 없음)")
    k.add_argument("snapshot", help="브라우저에서 Ctrl+S로 저장한 HTML")
    k.add_argument("--prefs", default="", help="희망 조건. 줄 구분은 ';' (예: \"상암고|2026-11-21;개포고\")")
    k.add_argument("--prefs-file", help="희망 조건 파일(한 줄에 '시험장 | 날짜 | 시간')")
    k.add_argument("--site", default="qnet", choices=["qnet", "kca", "kisa"])
    k.add_argument("--slot-row", help="일정 행 선택자 직접 지정")
    k.set_defaults(func=cmd_check_page)

    z = sub.add_parser("sanitize", help="저장 화면에서 개인정보·스크립트 제거(공유용)")
    z.add_argument("snapshot")
    z.add_argument("-o", "--output")
    z.set_defaults(func=cmd_sanitize)

    s = sub.add_parser("sites", help="사이트 프리셋 목록")
    s.set_defaults(func=cmd_sites)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
