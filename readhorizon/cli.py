"""명령줄 도구.

  python -m readhorizon serve [--host 127.0.0.1] [--port 8000]
  python -m readhorizon import-references samples/references.sample.json
  python -m readhorizon purge
  python -m readhorizon eval samples/eval_items.sample.json
  python -m readhorizon eval-ocr samples/ocr/manifest.sample.json
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
        engine = AnalysisEngine(llm, ReferenceIndex(store), settings.fulltext_char_limit)
        try:
            report = run_benchmark(engine, load_items(args.path))
        except BenchmarkError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
