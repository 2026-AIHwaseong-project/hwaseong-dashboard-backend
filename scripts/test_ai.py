#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 보고서 API 테스트 스크립트

서버를 발표장에서 켜기 전에 .env 의 AI 설정이 실제로 동작하는지 확인한다.

    python scripts/test_ai.py                    # 키 있는 프로바이더 전부 직접 호출
    python scripts/test_ai.py --provider gemini  # 특정 프로바이더만
    python scripts/test_ai.py --provider gemini --model gemini-3.5-flash
    python scripts/test_ai.py --server           # 실행 중인 서버로 /reports/draft 왕복
    python scripts/test_ai.py --server --url https://xxxx.ngrok-free.app

두 가지 검사 방식:
  직접 호출(기본) — 서버 없이 SDK 로 키·모델만 검증한다. 실패 원인이
                    키 문제인지 서버 문제인지 바로 갈린다.
  --server        — 실행 중인 서버의 GET /providers 와 POST /reports/draft 를
                    호출해 보고서 JSON 생성까지 전체 경로를 검증한다.

종료 코드: 실패한 검사 수 (0 = 전부 통과 또는 검사 대상 없음)
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_server_module():
    """server/main.py 를 모듈로 로드한다 (.env 로드 포함).

    lifespan 은 실행되지 않으므로 무거운 데이터 로드 없이
    _PROVIDERS / _detect_provider / _default_model / _call_ai 만 재사용한다.
    로직을 복사하지 않아야 서버와 테스트가 어긋나지 않는다.
    """
    spec = importlib.util.spec_from_file_location("hw_server", ROOT / "server" / "main.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def mask(v):
    """키를 통째로 출력하지 않는다 — 앞 8자와 길이만."""
    if not v:
        return "─ 없음"
    return f"{v[:8]}… ({len(v)}자)"


def _env_file_values():
    """.env 파일이 직접 담고 있는 값. 셸/시스템 환경변수와 출처를 구분하기 위한 것."""
    try:
        from dotenv import dotenv_values
        return dotenv_values(ROOT / ".env")
    except ImportError:
        return {}


def print_status(m):
    envfile = _env_file_values()
    print("─── AI 설정 상태 ──────────────────────────────────────────")
    for name, cfg in m._PROVIDERS.items():
        key = os.environ.get(cfg["env"])
        # 어디서 온 키인지 표시한다. .env 에 없는데 잡혀 있으면 셸/시스템
        # 환경변수(예: Windows 사용자 환경변수)다 — .env 를 고쳐도 안 바뀌고,
        # load_dotenv 는 이미 있는 환경변수를 덮어쓰지 않으므로 그쪽이 이긴다.
        if not key:
            src = ""
        elif envfile.get(cfg["env"]) == key:
            src = "[.env]"
        else:
            src = "[셸/시스템 환경변수 ⚠️]"
        print(f"  {name:<7} {cfg['env']:<18} {mask(key):<24} {src:<24} 기본모델 {m._default_model(name)}")
    if any(os.environ.get(c["env"]) and envfile.get(c["env"]) != os.environ.get(c["env"])
           for c in m._PROVIDERS.values()):
        print("  ⚠️ 셸/시스템 환경변수 키는 .env 보다 우선합니다. 지우려면 (Windows PowerShell):")
        print("     [Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $null, 'User')")
        print("     실행 후 터미널을 새로 여세요.")
    print(f"  AI_PROVIDER = {os.environ.get('AI_PROVIDER') or '(미설정)'}")
    print(f"  AI_MODEL    = {os.environ.get('AI_MODEL') or '(미설정)'}")
    auto = m._detect_provider()
    print(f"  provider=auto → {auto or '없음 → 규칙 기반 초안으로 폴백'}")
    print()
    return auto


# ── 1. 직접 호출 (SDK) ────────────────────────────────────────────────────────
def test_direct(m, provider, model=None):
    """SDK 로 최소 프롬프트 1회 호출. True=성공 / False=실패 / None=키 없어 건너뜀."""
    cfg = m._PROVIDERS[provider]
    if not os.environ.get(cfg["env"]):
        print(f"  [{provider:<7}] 건너뜀 — {cfg['env']} 미설정")
        return None
    use_model = model or m._default_model(provider)
    t0 = time.time()
    try:
        text = m._call_ai(provider, use_model, "연결 테스트입니다. 정확히 '정상' 두 글자만 답하세요.")
        dt = time.time() - t0
        print(f"  [{provider:<7}] OK  {dt:5.1f}s  {use_model}  → {text.strip()[:40]!r}")
        return True
    except Exception as e:
        dt = time.time() - t0
        detail = getattr(e, "detail", None) or str(e)
        print(f"  [{provider:<7}] 실패 {dt:5.1f}s  {use_model}")
        print(f"             {detail}")
        return False


# ── 2. 서버 왕복 (/providers + /reports/draft) ───────────────────────────────
def _http(url, body=None, timeout=180):
    headers = {"Content-Type": "application/json"}
    if "ngrok" in url:
        headers["ngrok-skip-browser-warning"] = "true"  # 무료 터널 경고 페이지 우회
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def test_server(base, provider, model):
    base = base.rstrip("/")
    print(f"─── 서버 왕복 테스트: {base} ──────────────────────────────")

    # ① GET /providers — 서버가 보는 키 상태
    try:
        pv = _http(f"{base}/api/v1/providers", timeout=15)
    except (urllib.error.URLError, OSError) as e:
        print(f"  서버 연결 실패: {e}")
        print("  서버가 켜져 있는지, --url 이 맞는지 확인하세요.")
        return 1
    avail = [p["id"] for p in pv["providers"] if p["available"]]
    print(f"  /providers → 사용 가능: {avail or '없음'}"
          f" | auto 기본: {pv.get('configuredDefault') or '규칙 기반 초안'}")

    # ② POST /reports/draft — 어느 프로바이더를 돌릴지
    if provider == "all":
        targets = [(p, None) for p in avail] or [("auto", None)]
    else:
        targets = [(provider, model)]

    failures = 0
    for prov, mod in targets:
        body = {"period": "am", "provider": prov, "context": {}}
        if mod:
            body["model"] = mod
        t0 = time.time()
        try:
            r = _http(f"{base}/api/v1/reports/draft", body)
            dt = time.time() - t0
            ai = r.get("isAiGenerated", True)  # 규칙 기반 초안만 False 를 싣는다
            tag = "AI" if ai else "규칙 기반(폴백)"
            print(f"  [{prov:<7}] OK  {dt:5.1f}s  {r.get('model') or '-'}  [{tag}]")
            print(f"             제목: {r.get('title', '')[:44]}"
                  f" | 섹션 {len(r.get('sections', []))}개 · 표 {len(r.get('tables', []))}개")
            if prov != "auto" and not ai:
                # 명시 지정했는데 폴백이 나오면 키가 서버에 안 실린 것
                print(f"             ⚠️ {prov} 를 지정했는데 폴백 초안이 왔습니다 — 서버 쪽 .env 확인")
                failures += 1
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            print(f"  [{prov:<7}] 실패 HTTP {e.code} — {detail}")
            failures += 1
        except (urllib.error.URLError, OSError) as e:
            print(f"  [{prov:<7}] 실패 — {e}")
            failures += 1
    return failures


def main():
    ap = argparse.ArgumentParser(
        description="AI 보고서 API 테스트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예)  python scripts/test_ai.py\n"
               "     python scripts/test_ai.py --provider gemini --model gemini-3.5-flash\n"
               "     python scripts/test_ai.py --server --url https://xxxx.ngrok-free.app",
    )
    ap.add_argument("--provider", default="all",
                    choices=["all", "auto", "claude", "openai", "gemini"],
                    help="검사할 프로바이더 (기본: 키 있는 것 전부)")
    ap.add_argument("--model", default=None, help="모델 ID 재지정 (--provider 와 함께)")
    ap.add_argument("--server", action="store_true",
                    help="SDK 직접 호출 대신 실행 중인 서버로 /reports/draft 왕복")
    ap.add_argument("--url", default="http://localhost:8000",
                    help="--server 대상 주소 (기본: http://localhost:8000)")
    args = ap.parse_args()

    m = load_server_module()  # .env 로드 포함
    auto = print_status(m)

    if args.server:
        sys.exit(test_server(args.url, args.provider, args.model))

    # 직접 호출 모드
    if args.provider == "auto":
        if auto is None:
            print("키가 하나도 없어 테스트할 프로바이더가 없습니다. .env 를 설정하세요 (.env.example 참고).")
            sys.exit(0)
        targets = [auto]
    elif args.provider == "all":
        targets = list(m._PROVIDERS)
    else:
        targets = [args.provider]

    print("─── 직접 호출 테스트 (SDK) ────────────────────────────────")
    results = [test_direct(m, p, args.model if len(targets) == 1 else None) for p in targets]
    tested = [r for r in results if r is not None]
    print()
    if not tested:
        print("키가 하나도 없어 실제 호출은 못 했습니다. .env 를 설정하세요 (.env.example 참고).")
        print("서버는 키 없이도 규칙 기반 초안으로 동작합니다 (--server 로 확인 가능).")
        sys.exit(0)
    failed = tested.count(False)
    print(f"결과: {len(tested) - failed}/{len(tested)} 통과")
    sys.exit(failed)


if __name__ == "__main__":
    main()
