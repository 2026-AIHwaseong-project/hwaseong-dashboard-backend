# -*- coding: utf-8 -*-
"""E2E 용 공용 픽스처 — 백엔드를 실제 프로세스로 띄운다.

TestClient(테스트 A~G)는 ASGI 를 직접 호출하므로 브라우저가 붙을 수 없다.
E2E 는 진짜 HTTP 서버가 필요하다.
"""
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT.parent / "hwaseong-dashboard"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """uvicorn 을 띄우고 /api/v1/meta 가 응답할 때까지 기다린다."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120          # 포아송 회귀 로드에 시간이 걸린다
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("서버가 죽었습니다:\n" + proc.stdout.read().decode("utf-8", "replace"))
        try:
            urllib.request.urlopen(base + "/api/v1/meta", timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        proc.kill()
        raise RuntimeError("서버 기동 타임아웃")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def frontend_available():
    if not (FRONT / "index.html").exists():
        pytest.skip(f"프론트 저장소가 없습니다: {FRONT} "
                    "(백엔드와 같은 부모 폴더에 hwaseong-dashboard 를 클론하세요)")
    return FRONT
