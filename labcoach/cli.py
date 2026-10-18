"""명령줄 도구.

  python -m labcoach run [--region x,y,w,h] [--log Procmon.csv] [--notes 노트.md] [--auto] [--mode malware] [--demo]
  python -m labcoach once [--image 화면.png] [--question "..."] [--render 결과.png] [--json]
  python -m labcoach windows
  python -m labcoach notice [--days 30]
  python -m labcoach purge
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .config import LEVELS, MODES, Settings, load_settings, parse_region
from .privacy import CollectionLog, format_notice


def _coach(settings: Settings):
    from .coach import ClaudeCoach, DemoCoach

    if settings.llm_mode == "live":
        return ClaudeCoach(settings.model, settings.effort)
    return DemoCoach()


def _housekeeping(settings: Settings, log: CollectionLog, out=sys.stderr) -> None:
    """보관 기간이 지난 수집 기록을 지우고, 고지 주기(기본 30일)가 되면 수집 항목을 알린다."""
    log.purge()
    if log.notice_due(settings.notice_every_days):
        print(format_notice(log.summary(settings.notice_every_days), settings.llm_mode, settings.retention_days), file=out)
        print(file=out)
        log.mark_notice()


def _apply_overrides(settings: Settings, args) -> Settings:
    changes = {}
    if getattr(args, "mode", None):
        changes["mode"] = args.mode
    if getattr(args, "level", None):
        changes["level"] = args.level
    if getattr(args, "demo", False):
        changes["llm_mode"] = "demo"
    if getattr(args, "region", None):
        region = parse_region(args.region)
        if region is None:
            raise SystemExit("--region 형식은 x,y,폭,높이 입니다 (예: 0,0,1280,800)")
        changes["region"] = region
    if getattr(args, "no_voice", False):
        changes["tts"] = False
    return dataclasses.replace(settings, **changes) if changes else settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labcoach", description="LabCoach 보안 실습 실시간 학습 오버레이")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--mode", choices=MODES, help="malware(악성코드 분석) | attack(공격 실습) | defense(방어 실습) | general")
        p.add_argument("--level", choices=LEVELS, help="beginner | intermediate | advanced")
        p.add_argument("--log", action="append", type=Path, default=[], help="함께 볼 도구 로그(Procmon CSV, Regshot 결과 등). 여러 번 지정 가능")
        p.add_argument("--demo", action="store_true", help="API 키가 있어도 데모 응답 사용(아무것도 전송하지 않음)")

    run = sub.add_parser("run", help="오버레이와 조작 패널 실행")
    common(run)
    run.add_argument("--region", help="캡처 영역 x,y,폭,높이(물리 픽셀). 없으면 VirtualBox/VMware 창을 자동으로 찾음")
    run.add_argument("--notes", type=Path, help="학습 노트(Markdown) 저장 경로")
    run.add_argument("--auto", action="store_true", help="시작하자마자 자동 분석 켜기")
    run.add_argument("--no-voice", action="store_true", help="음성 끄기")

    once = sub.add_parser("once", help="화면(또는 이미지 파일) 한 장을 분석해 결과 출력")
    common(once)
    once.add_argument("--image", type=Path, help="분석할 스크린샷 파일. 없으면 지금 화면을 캡처")
    once.add_argument("--region", help="캡처 영역 x,y,폭,높이")
    once.add_argument("--question", default="", help="함께 물어볼 질문")
    once.add_argument("--render", type=Path, help="주석을 그린 이미지를 이 경로에 저장")
    once.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")

    sub.add_parser("windows", help="자동으로 찾을 VM 창 후보 보기")
    notice = sub.add_parser("notice", help="최근 수집 항목 고지 보기")
    notice.add_argument("--days", type=int, default=30)
    sub.add_parser("purge", help="보관 기간이 지난 수집 기록 삭제")

    args = parser.parse_args(argv)
    settings = _apply_overrides(load_settings(), args)
    log = CollectionLog(settings.collection_log, settings.retention_days)

    if args.command == "notice":
        log.purge()
        print(format_notice(log.summary(args.days), settings.llm_mode, settings.retention_days))
        log.mark_notice()
        return 0

    if args.command == "purge":
        print(f"삭제한 기록: {log.purge()}건")
        return 0

    if args.command == "windows":
        from .capture import find_window

        target = find_window(settings.window_hints)
        if target is None:
            print("VM 창을 찾지 못했습니다(pywinctl 미설치 또는 창 없음). --region 으로 영역을 지정하세요.")
            return 1
        x, y, w, h = target.region
        print(f"{target.title}\n  영역: {x},{y},{w},{h}")
        return 0

    _housekeeping(settings, log)
    from .session import Session

    session = Session(settings, _coach(settings), log, args.log)

    if args.command == "once":
        return _once(args, settings, session)

    try:
        from .overlay import run_gui
    except ImportError as exc:
        print(f"오버레이를 실행하려면 추가 패키지가 필요합니다: pip install -r requirements-labcoach.txt ({exc.name})", file=sys.stderr)
        return 2
    from .tts import Speaker

    speaker = Speaker(settings.tts_voice, settings.tts_rate, settings.tts)
    if settings.llm_mode == "demo":
        print("데모 모드: ANTHROPIC_API_KEY가 없어 고정 예시 설명을 보여 줍니다(화면은 전송하지 않음).", file=sys.stderr)
    return run_gui(session, speaker, args.notes, settings.region, args.auto)


def _once(args, settings: Settings, session) -> int:
    from PIL import Image

    from .coach import LLMError

    title = ""
    if args.image:
        image = Image.open(args.image)
        image.load()
        image = image.convert("RGB")
    else:
        from .capture import Target, find_window, grab

        target = Target("지정 영역", settings.region) if settings.region else find_window(settings.window_hints)
        if target is None:
            print("VM 창을 찾지 못했습니다. VM 창을 띄우거나 --region x,y,폭,높이 또는 --image 로 지정하세요.", file=sys.stderr)
            return 1
        title = target.title if target.title != "지정 영역" else ""
        image = grab(target.region)
    try:
        step = session.analyze(image, title, args.question)
    except LLMError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.render:
        from .render import render

        render(image, step).save(args.render)
    if args.json:
        data = dataclasses.asdict(step)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if step.demo:
            print("(데모 모드: 실제 화면 분석이 아닙니다)")
        print(f"[{step.phase_label}] {step.subtitle}")
        if step.screen_summary:
            print(f"\n{step.screen_summary}")
        if step.flow:
            print("\n흐름: " + " → ".join(n.label for n in step.flow))
        for label, rows in (("스택", step.stack), ("레지스트리", step.registry)):
            for r in rows:
                print(f"{label}: {r.first} = {r.second} — {r.meaning}")
        for label, values in (("다음 단계", step.next_steps), ("방어 팁", step.defense_tips), ("안전 주의", step.safety_warnings)):
            for v in values:
                print(f"{label}: {v}")
        if args.render:
            print(f"\n주석 이미지: {args.render}")
    return 0
