"""실행: python -m exam_booker config.yaml [--check] [--now]"""

from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="exam_booker", description="자격증 시험 접수 자동 선택 도우미")
    ap.add_argument("config", help="설정 파일 경로 (config.example.yaml 참고)")
    ap.add_argument("--check", action="store_true", help="설정만 검사하고 끝냄")
    ap.add_argument("--now", action="store_true", help="시작 시각을 기다리지 않고 바로 시도 (연습·테스트용)")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2

    print(f"접수 시작: {cfg.open_at:%Y-%m-%d %H:%M:%S %Z}")
    for i, p in enumerate(cfg.preferences, 1):
        print(f"  {i}순위: {p.label()}")
    print(f"새로고침 간격 {cfg.poll_interval:g}초, 최종 확정 {'자동' if cfg.auto_confirm else '직접'}")
    if args.check:
        return 0

    from playwright.sync_api import sync_playwright

    from .booker import Booker

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            cfg.profile_dir,
            headless=False,
            no_viewport=True,
            # 설치된 Playwright 브라우저 대신 다른 Chromium/Chrome을 쓰려면 경로 지정
            executable_path=os.environ.get("EXAM_BOOKER_CHROMIUM") or None,
        )
        page = context.pages[0] if context.pages else context.new_page()
        booker = Booker(cfg, context, page)
        ok = booker.run(skip_wait=args.now)
        booker.keep_open()
        try:
            context.close()
        except Exception:  # noqa: BLE001,S110 - 이미 닫힘
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
