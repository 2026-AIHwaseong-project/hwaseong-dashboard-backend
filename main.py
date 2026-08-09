#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
화성시 버스 대시보드 — 원커멘드 실행

    python main.py                # 기본 (0.0.0.0:8000)
    python main.py --port 8080    # 포트 변경
    python main.py --reload       # 개발 모드 (코드 변경 시 자동 재시작)
    python main.py --regen        # 정적 JSON 강제 재생성 후 기동
    python main.py --setup        # 정적 JSON 생성만 (서버 미기동)
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC_META = ROOT / "server" / "static" / "meta.json"
LOAD_SCRIPT  = ROOT / "analysis" / "05_load.py"

BANNER = """
╔══════════════════════════════════════════════════════╗
║      화성시 버스 수요·공급 미스매칭 대시보드          ║
║      AI화성챌린지 2026 · 과제 23번 (교통분야)         ║
╚══════════════════════════════════════════════════════╝"""


def _check_deps():
    missing = []
    for pkg in ("fastapi", "uvicorn", "numpy", "pandas", "sklearn"):
        try:
            __import__(pkg if pkg != "sklearn" else "sklearn")
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[오류] 필수 패키지 미설치: {', '.join(missing)}")
        print("       pip install -r requirements.txt  를 먼저 실행하세요.")
        sys.exit(1)


def _regen_static(force: bool = False):
    if force or not STATIC_META.exists():
        tag = "강제 재생성" if force else "최초 생성"
        print(f"[setup] server/static/ {tag} 중 (05_load.py 실행)...")
        result = subprocess.run(
            [sys.executable, str(LOAD_SCRIPT)],
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            print("[오류] 정적 JSON 생성 실패. 위 오류 메시지를 확인하세요.")
            sys.exit(1)
        print("[setup] 완료\n")
    else:
        print("[setup] server/static/ 이미 존재 — 건너뜀 (--regen 으로 강제 재생성)")


def _start_server(host: str, port: int, reload: bool):
    try:
        import uvicorn
    except ImportError:
        print("[오류] uvicorn 미설치: pip install uvicorn[standard]")
        sys.exit(1)

    print(BANNER)
    print(f"\n  API 서버  : http://{host}:{port}")
    print(f"  Docs      : http://{host}:{port}/docs")
    print(f"  정적 JSON : http://{host}:{port}/data/grid_am.json")
    print(f"  개발 모드 : {'ON' if reload else 'OFF'}")
    print("\n  Ctrl+C 로 종료\n")

    uvicorn.run(
        "server.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=1,          # 05_simulate.py 프로세스 공유 불가
        log_level="info",
    )


def main():
    parser = argparse.ArgumentParser(
        description="화성시 버스 대시보드 서버",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host",   default="0.0.0.0",  help="바인드 호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port",   type=int, default=8000, help="포트 (기본: 8000)")
    parser.add_argument("--reload", action="store_true", help="개발 모드 — 코드 변경 시 자동 재시작")
    parser.add_argument("--regen",  action="store_true", help="정적 JSON 강제 재생성 후 기동")
    parser.add_argument("--setup",  action="store_true", help="정적 JSON 생성만 (서버 미기동)")
    args = parser.parse_args()

    _check_deps()
    _regen_static(force=args.regen or args.setup)

    if args.setup:
        print("[setup] --setup 완료. 서버를 시작하려면 python main.py 를 실행하세요.")
        return

    _start_server(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
