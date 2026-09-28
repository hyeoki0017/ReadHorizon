"""명령줄 도구.

  python -m readhorizon serve [--host 127.0.0.1] [--port 8000]
  python -m readhorizon import-references samples/references.sample.json
  python -m readhorizon purge
  python -m readhorizon eval samples/eval_items.sample.json
  python -m readhorizon eval-ocr samples/ocr/manifest.sample.json
  python -m readhorizon eval-quality samples/quality_items.sample.json [--out 결과.json] [--baseline 이전결과.json] [--revise]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="readhorizon", description="ReadHorizon 텍스트 분석 도우미")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="웹 서버 실행")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    imp = sub.add_parser("import-references", help="공용 참고자료 가져오기(JSON)")
    imp.add_argument("path", type=Path)

    sub.add_parser("purge", help="보관 기간이 지난 작품 삭제")

    ev = sub.add_parser("eval", help="선다형 문항 정답률 벤치마크")
    ev.add_argument("path", type=Path)

    ev_ocr = sub.add_parser("eval-ocr", help="이미지 글자 추출(OCR) 정확도(CER) 평가")
    ev_ocr.add_argument("path", type=Path)

    ev_quality = sub.add_parser("eval-quality", help="번역·해설 품질 평가(핵심 포인트·오류 분류, AI 채점)")
    ev_quality.add_argument("path", type=Path)
    ev_quality.add_argument("--out", type=Path, help="결과 JSON을 저장할 파일")
    ev_quality.add_argument("--baseline", type=Path, help="비교할 이전 결과 JSON(--out으로 저장한 파일)")
    ev_quality.add_argument("--no-judge", action="store_true", help="AI 채점 없이 자동 지표만 계산")
    ev_quality.add_argument("--revise", action="store_true", help="초안 → 심화 검토 → 검토 반영 재작성까지 거친 결과를 채점")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "serve":
        import uvicorn

        from .main import create_app

        # 접속 로그(IP 포함)는 RH_ACCESS_LOG=1일 때만 남긴다.
        # 리버스 프록시 뒤라면 FORWARDED_ALLOW_IPS 환경 변수로 프록시 주소를 지정한다(uvicorn 설정).
        uvicorn.run(
            create_app(settings),
            host=args.host,
            port=args.port,
            access_log=settings.access_log,
            proxy_headers=True,
        )
        return 0

    from .db import Store

    store = Store(settings.db_path)

    if args.command == "import-references":
        from .references.store import ReferenceImportError, import_references

        try:
            count = import_references(store, args.path)
        except ReferenceImportError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        print(f"참고자료 {count}건을 가져왔습니다.")
        return 0

    if args.command == "purge":
        removed = store.purge_inactive(settings.retention_days)
        print(f"마지막 이용 후 {settings.retention_days}일이 지난 작품 {removed}건을 삭제했습니다.")
        return 0

    if args.command == "eval":
        from .analysis.engine import AnalysisEngine
        from .analysis.llm import ClaudeClient, DemoClient
        from .evaluation import BenchmarkError, load_items, run_benchmark
        from .references.store import ReferenceIndex

        llm = ClaudeClient(settings.model, settings.effort) if settings.llm_mode == "live" else DemoClient()
        if llm.mode == "demo":
            print("주의: 데모 모드입니다. 실제 정답률을 재려면 ANTHROPIC_API_KEY를 설정하세요.", file=sys.stderr)
        engine = AnalysisEngine(llm, ReferenceIndex(store), settings.fulltext_char_limit, settings.translate_part_chars)
        try:
            report = run_benchmark(engine, load_items(args.path))
        except BenchmarkError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "eval-quality":
        return _eval_quality(args, settings, store)

    if args.command == "eval-ocr":
        from .analysis.llm import ClaudeClient, DemoClient
        from .ocr_eval import OCRBenchmarkError, load_manifest, run_ocr_benchmark

        llm = (
            ClaudeClient(settings.model, settings.effort, ocr_effort=settings.ocr_effort)
            if settings.llm_mode == "live"
            else DemoClient()
        )
        if llm.mode == "demo":
            print("주의: 데모 모드입니다. 실제 정확도를 재려면 ANTHROPIC_API_KEY를 설정하세요.", file=sys.stderr)
        try:
            report = run_ocr_benchmark(
                llm, load_manifest(args.path), args.path.parent, settings.ocr_max_images, settings.ocr_max_pages
            )
        except OCRBenchmarkError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    return 1


def _eval_quality(args, settings, store) -> int:
    from .analysis.engine import AnalysisEngine
    from .analysis.llm import ClaudeClient, DemoClient, LLMError
    from .quality_eval import QualityEvalError, compare, load_items, run_quality_eval
    from .references.store import ReferenceIndex

    live = settings.llm_mode == "live"
    llm = ClaudeClient(settings.model, settings.effort) if live else DemoClient()
    judge = None
    if live and not args.no_judge:
        judge = ClaudeClient(settings.judge_model or settings.model, settings.judge_effort)
    elif not live:
        print("주의: 데모 모드입니다. AI 채점은 건너뛰고 자동 지표만 계산합니다(ANTHROPIC_API_KEY 필요).", file=sys.stderr)
    engine = AnalysisEngine(llm, ReferenceIndex(store), settings.fulltext_char_limit, settings.translate_part_chars)

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))["summary"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"오류: 비교할 이전 결과를 읽을 수 없습니다: {exc}", file=sys.stderr)
            return 1
    try:
        items = load_items(args.path)
        if live:
            calls = len(items) * (1 + (1 if judge else 0) + (2 if args.revise else 0))
            print(f"문항 {len(items)}개 평가: Claude API를 최대 {calls}회 호출합니다(비용 발생).", file=sys.stderr)
        report = run_quality_eval(engine, judge, items, revise=args.revise)
    except (QualityEvalError, LLMError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if baseline is not None:
        report["baseline_delta"] = compare(report["summary"], baseline)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"결과를 {args.out}에 저장했습니다.", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
