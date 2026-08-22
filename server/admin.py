# -*- coding: utf-8 -*-
"""관리자 콘솔 API — 파라미터 오버라이드 + 데이터 최신화

설계 원칙 (schema_ops.sql 의 소유권 분리를 파일로 이식):
  · 오버라이드 정본은 var/admin/params_override.json 하나 — server/static(배치 소유)에는
    절대 쓰지 않는다. 배치(05_load)를 몇 번 돌려도 관리자 값이 살아남아야 한다.
  · 이력은 var/admin/params_history.jsonl append-only. 삭제 없음 — 되돌리기도 새 줄.
    reason 필수(admin_grid_override 의 reason NOT NULL 과 같은 이유 — "왜 사람이
    고쳤는가"가 기록에 남아야 한다).
  · 값 반영은 apply_runtime_params() 한 함수 — 서버 상수(COST_KRW)·시뮬 엔진 모듈
    속성·메모리 meta 를 **한 번에** 갱신한다. 나눠서 하면 같은 화면에 클라 합계와
    서버 breakdown 이 다른 비용으로 표시된다(기존 테스트가 못 잡는 사고).
  · 재적재(reload)·재계산(refresh) 직후에도 반드시 apply_runtime_params() 를 다시
    부른다 — 새로 import 된 시뮬 모듈은 기본값으로 돌아가 있기 때문이다.

인증: ADMIN_TOKEN 환경변수 + Authorization: Bearer.
  ⚠️ 토큰이 **비어 있으면 인증 없이 열린다**(내부망·단일 운영자 전제의 기본값).
     공개 인터넷에 노출된 서버라면 반드시 ADMIN_TOKEN 을 설정할 것 — 없으면
     누구나 단가·모델 상수를 바꾸고 재계산을 트리거할 수 있다(workers=1 이라
     재계산 반복만으로 전 API 가 멎는다). 기동 로그와 화면 배너로 경고한다.
  실패 잠금은 전역 카운터다. IP별 잠금은 행사장 NAT 오차단으로 롤백된 전례가 있다
  (main.py 의 레이트리밋 이력 참고).

편집 범위: 런타임(A계급)만 — 시뮬·추천이 요청 시점에 읽는 값이라 재계산이 필요 없다.
  기준선에 구워지는 상수(가중치·컷·감쇠)는 읽기 전용으로만 노출한다. 런타임에 바꾸면
  05_simulate 의 기준선과 화면(grid JSON)이 즉시 갈라지고, 파이프라인 쪽만 바꾸면
  기준선 assert 로 서버가 아예 안 뜬다.
"""
import asyncio
import collections
import copy
import hmac
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
VAR = Path(os.environ.get("HW_VAR_DIR", str(ROOT / "var")))
ADMIN_DIR = VAR / "admin"
OVERRIDE_PATH = ADMIN_DIR / "params_override.json"
HISTORY_PATH = ADMIN_DIR / "params_history.jsonl"

# main.py 가 lifespan 에서 주입한다 (순환 import 방지).
_ctx: dict = {"DATA": None, "COST_KRW": None, "PERIODS": None, "build_snapshot": None}


def init(DATA, COST_KRW, PERIODS, build_snapshot):
    _ctx.update(DATA=DATA, COST_KRW=COST_KRW, PERIODS=PERIODS, build_snapshot=build_snapshot)


def _load_params_module():
    """analysis/params.py 를 별도 모듈 객체로 로드 — 상수 정본.
    (여기서 로드한 사본은 오버라이드를 굽기 전 기본값 참조용으로도 쓰므로
    시뮬 엔진의 params 인스턴스와 별개다.)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hw_params_admin", ROOT / "analysis" / "params.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PARAMS = _load_params_module()


# ─── 파라미터 레지스트리 ────────────────────────────────────────────────────────
# scope: runtime  = 요청 시점에 읽혀 즉시 반영 (편집 가능)
#        pipeline = 계약 JSON 에 구워짐 — 재계산 경로가 오버라이드를 읽는 P3 에서 개방
# 기본값은 코드 정본과 같아야 한다. 어긋나면 기동 시 드리프트 검사가 잡는다.
SPECS = {
    "sim.headwayMult": dict(
        label="배차 증편 배수", unit="×", type="float", min=1.01, max=2.0, default=1.43,
        scope="runtime", group="effect",
        note="증편 1회 = 배차간격 ×0.70 (운행횟수 ×1.43). 마을버스 실측 배차 근거.",
        applies="시뮬레이션·추천의 증편 효과 (요청 시점 반영)"),
    "sim.fstar.am": dict(label="신설 주입량 f* — 출근", unit="회/창", type="float", min=0.0, max=20.0,
                         default=4.8, scope="runtime", group="effect",
                         note="마을버스 120노선 실측 배차 중앙값(출퇴근 25분)에서 환산.",
                         applies="정류장 신설 배치의 출근 시간대 효과"),
    "sim.fstar.day": dict(label="신설 주입량 f* — 낮", unit="회/창", type="float", min=0.0, max=20.0,
                          default=8.0, scope="runtime", group="effect",
                          note="평일 60분 배차 → 8시간 창 환산.", applies="정류장 신설 배치의 낮 효과"),
    "sim.fstar.pm": dict(label="신설 주입량 f* — 퇴근", unit="회/창", type="float", min=0.0, max=20.0,
                         default=4.8, scope="runtime", group="effect",
                         note="출퇴근 25분 배차 환산.", applies="정류장 신설 배치의 퇴근 효과"),
    "sim.fstar.night": dict(label="신설 주입량 f* — 심야", unit="회/창", type="float", min=0.0, max=20.0,
                            default=0.0, scope="runtime", group="effect",
                            note="0 이 설계 의도 — 심야 신설이 즉시 효과를 낸다는 실측 근거가 없다.",
                            applies="정류장 신설 배치의 심야 효과"),
    "sim.phi.am": dict(label="똑버스 주입량 φ — 출근", unit="회/창", type="float", min=0.0, max=20.0,
                       default=2.4, scope="runtime", group="effect",
                       note="차량 1대 1.2회/h → 2시간 창 환산.", applies="똑버스 배치의 출근 효과"),
    "sim.phi.day": dict(label="똑버스 주입량 φ — 낮", unit="회/창", type="float", min=0.0, max=20.0,
                        default=9.6, scope="runtime", group="effect",
                        note="1.2회/h × 8시간 창.", applies="똑버스 배치의 낮 효과"),
    "sim.phi.pm": dict(label="똑버스 주입량 φ — 퇴근", unit="회/창", type="float", min=0.0, max=20.0,
                       default=2.4, scope="runtime", group="effect",
                       note="1.2회/h × 2시간 창.", applies="똑버스 배치의 퇴근 효과"),
    "sim.phi.night": dict(label="똑버스 주입량 φ — 심야", unit="회/창", type="float", min=0.0, max=20.0,
                          default=2.4, scope="runtime", group="effect",
                          note="1.2회/h × 2시간 창.", applies="똑버스 배치의 심야 효과"),
    "cost.stop.krw": dict(label="정류장 신설 단가", unit="원", type="int",
                          min=1_000_000, max=500_000_000, default=42_000_000,
                          scope="runtime", group="cost",
                          note="1회성 시설비(내용연수 10년). 공개단가 미확인 — 가정값.",
                          applies="시뮬레이션 비용·추천 순위(비용효율)·화면 단가 표기"),
    "cost.drt.krw": dict(label="똑버스 단가", unit="원/년", type="int",
                         min=10_000_000, max=2_000_000_000, default=180_000_000,
                         scope="runtime", group="cost",
                         note="경기교통공사 표준운송원가 대비 6~12% 보수적.",
                         applies="시뮬레이션 비용·추천 순위·화면 단가 표기"),
    "cost.freq.krw": dict(label="배차 증편 단가", unit="원/년", type="int",
                          min=10_000_000, max=2_000_000_000, default=95_000_000,
                          scope="runtime", group="cost",
                          note="화성시 마을버스 연간 운송원가 대비 1.8% 이내 대조.",
                          applies="시뮬레이션 비용·추천 순위·화면 단가 표기"),
    "cost.defaultBudget": dict(label="기본 예산 한도", unit="원", type="int",
                               min=100_000_000, max=1_000_000_000_000, default=3_000_000_000,
                               scope="runtime", group="cost",
                               note="시연용 기본값. 화면에서 건별로 바꿀 수 있다.",
                               applies="예산 미지정 요청의 기본값·시뮬 화면 초기 예산"),
    "rec.maxPlacements": dict(label="추천 최대 배치 건수", unit="건", type="int", min=1, max=50,
                              default=10, scope="runtime", group="cost",
                              note="화면은 자체 기본 10건을 명시 전송한다 — 이 값은 API 직접 호출과 "
                                   "향후 화면 연동에 적용.",
                              applies="maxPlacements 미지정 추천 요청의 기본값"),
    "model.busTripRate": dict(label="인구→통행 환산계수", unit="통행/인·일", type="float",
                              min=0.05, max=1.0, default=0.25,
                              scope="pipeline", group="model",
                              note="전수단 원단위 2.5 × 버스 분담률 0.10 — 가정값. "
                                   "격자 JSON 에 구워지므로 재계산 경로가 오버라이드를 읽는 "
                                   "단계(P3)에서 개방.",
                              applies="잠재수요 KPI(flowTripsPerDay) — 재계산 필요"),
    "model.minFreqPerHour": dict(label="적정 판정 절대 하한", unit="회/h", type="float",
                                 min=0.5, max=6.0, default=2.0,
                                 scope="pipeline", group="model",
                                 note="야간 상대평가 오라벨 방지 가드. 재계산 필요 — P3 개방.",
                                 applies="사분면 적정/과잉 판정 — 재계산 필요"),
}

# 기본값 동기화 — SPECS 의 리터럴이 params.py 와 갈라지지 않게 정본에서 덮는다.
_DEFAULT_SRC = {
    "sim.headwayMult": PARAMS.HEADWAY_MULT,
    **{f"sim.fstar.{p}": PARAMS.FSTAR[p] for p in PARAMS.PERIODS},
    **{f"sim.phi.{p}": PARAMS.PHI[p] for p in PARAMS.PERIODS},
    **{f"cost.{t}.krw": PARAMS.COST_TOTAL[t] for t in ("stop", "drt", "freq")},
    "cost.defaultBudget": PARAMS.DEFAULT_BUDGET,
    "model.busTripRate": PARAMS.BASE_VALUES["model.busTripRate"],
    "model.minFreqPerHour": PARAMS.BASE_VALUES["model.minFreqPerHour"],
}
for _k, _v in _DEFAULT_SRC.items():
    SPECS[_k]["default"] = _v

# C계급 — 기준선에 구워진 상수. 표시 전용(살아있는 시뮬 모듈에서 읽는다).
#   (attr, label, note)
BASELINE_DISPLAY = [
    ("W_FREQ", "공급지수 운행빈도 가중", "0.78 — 팀 설계값. 바꾸려면 새 기준선 발행(재계산+재검증) 필요."),
    ("W_COV", "공급지수 접근성 가중", "0.22 — W_FREQ 와 합이 1."),
    ("DAMP_EXP", "빈 땅 MI 감쇠 지수", "0.65 — 인구<50 임야 183칸의 가짜 사각지대 제거 실측 근거."),
    ("MI_CLAMP", "MI 절대값 상한", "±2.6 — 극단값의 색 스케일 붕괴 방지."),
    ("ELD", "우선순위 고령 가중", "1.6 — 회귀 학습값이 아니라 정책 결정값. 근거는 README §4."),
    ("COVM", "커버리지 임계(m)", "600 — 최근접 정류장 거리 중앙값 392m 실측에서 결정."),
    ("WALK", "승하차 안분 반경(m)", "800 — 03_join 설계와 결합. 변경 금지."),
    ("MIN_FREQ_PER_H", "적정 판정 하한(회/h)", "2.0 — model.minFreqPerHour 와 동일 값(사본)."),
    ("HEADWAY_MULT", "(현재 적용) 증편 배수", "sim.headwayMult 오버라이드가 실제로 주입된 값."),
]


# ─── 오버라이드 저장소 ──────────────────────────────────────────────────────────
def _read_override() -> dict:
    try:
        raw = json.loads(OVERRIDE_PATH.read_text("utf-8"))
        params = raw.get("params", {})
        ok = {}
        for k, v in (params.items() if isinstance(params, dict) else []):
            # 사람 소유 파일이라 수기 편집을 전제한다 — 형식이 어긋난 항목은
            # 서버를 죽이는 대신 버리고 크게 알린다.
            if (isinstance(v, dict) and isinstance(v.get("value"), (int, float))
                    and not isinstance(v.get("value"), bool)):
                ok[k] = v
            else:
                print(f"[admin] 오버라이드 항목 형식 오류로 무시: {k}={v!r}",
                      file=sys.stderr, flush=True)
        return ok
    except FileNotFoundError:
        return {}
    except Exception as e:  # 손상 — 조용히 넘어가지 않는다 (db.py 의 문화)
        print(f"[admin] 오버라이드 파일 손상, 기본값으로 동작: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return {}


def _write_override(params: dict) -> None:
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "updatedAt": _now(), "params": params}
    tmp = OVERRIDE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, OVERRIDE_PATH)  # 같은 디렉토리 → 원자 교체


def _append_history(event: dict) -> None:
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": _now(), **event}
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def effective(key: str):
    """지금 계산에 쓰여야 할 값 — 오버라이드 우선, 없으면 코드 기본값."""
    ov = _read_override().get(key)
    if ov is not None:
        return ov["value"]
    return SPECS[key]["default"]


def _pipeline_effective(key: str):
    """pipeline 계급의 '실제 적용값' — 산출물(메모리 meta)에 구워진 값을 읽는다.
    오버라이드를 저장해도 재계산 전에는 여기 값이 안 바뀐다(= 재계산 대기)."""
    meta = (_ctx["DATA"] or {}).get("meta") or {}
    a = meta.get("assumptions") or {}
    if key == "model.busTripRate":
        return (a.get("busTripRate") or {}).get("value")
    if key == "model.minFreqPerHour":
        return (a.get("minFreqPerHour") or {}).get("value")
    return None


# ─── 값 주입 — 서버 상수 + 시뮬 엔진 + meta 를 한 번에 ─────────────────────────
def apply_runtime_params() -> dict:
    """오버라이드를 서빙 상태에 주입한다. lifespan 직후 · 저장 직후 · 재적재 직후
    세 곳 모두에서 불려야 한다 — 하나라도 빼먹으면 값이 소리 없이 초기화되거나
    한 화면에 두 단가가 표시된다."""
    DATA, COST_KRW, PERIODS = _ctx["DATA"], _ctx["COST_KRW"], _ctx["PERIODS"]
    raw = _read_override()
    ov = {k: v["value"] for k, v in raw.items()
          if k in SPECS and SPECS[k]["scope"] == "runtime"}
    eff = {k: ov.get(k, s["default"]) for k, s in SPECS.items()}

    # ① 서버 계산 정본
    for t in ("stop", "drt", "freq"):
        COST_KRW[t] = eff[f"cost.{t}.krw"]

    # ② 시뮬 엔진 — 요청 시점에 sim.* 속성으로 읽힌다 (기준선과 무관한 배치 물리량만)
    sim = DATA.get("sim")
    if sim is not None:
        sim.HEADWAY_MULT = eff["sim.headwayMult"]
        for p in PERIODS:
            sim.FSTAR[p] = eff[f"sim.fstar.{p}"]
            sim.PHI[p] = eff[f"sim.phi.{p}"]

    # ③ 화면·챗봇·보고서가 보는 meta. 제자리 수정이 아니라 사본을 완성한 뒤
    #    단일 키 재바인딩으로 교체한다 — 다른 스레드가 같은 dict 를 json.dumps 로
    #    순회하는 도중 키를 꽂으면 RuntimeError(500)가 난다.
    meta = copy.deepcopy(DATA.get("meta")) if DATA.get("meta") else None
    if meta and "cost" in meta:
        for t in ("stop", "drt", "freq"):
            c = meta["cost"].get(t)
            if not c:
                continue
            c["krw"] = eff[f"cost.{t}.krw"]
            life = c.get("lifeYears") or 1
            c["annualKrw"] = round(c["krw"] / life)
            c["overridden"] = f"cost.{t}.krw" in ov
        meta["cost"]["defaultBudget"] = eff["cost.defaultBudget"]
        for e in meta.get("effects", []):
            t = e.get("type")
            if t in ("stop", "drt", "freq") and t in meta["cost"]:
                e["unitKrw"] = meta["cost"][t]["krw"]
                e["annualKrw"] = meta["cost"][t]["annualKrw"]
                e["overridden"] = meta["cost"][t].get("overridden", False)
        meta["paramsOverride"] = {"count": len(ov),
                                  "updatedAt": max((v.get("at", "") for v in raw.values()), default=None)}
        DATA["meta"] = meta
    return eff


# ─── 인증 ──────────────────────────────────────────────────────────────────────
_AUTH = {"fail": 0, "locked_until": 0.0, "logged": 0}


def auth_required() -> bool:
    return bool(os.environ.get("ADMIN_TOKEN", ""))


def warn_if_open() -> None:
    """기동 시 1회 — 무인증 상태를 조용히 넘기지 않는다(db.py 의 문화)."""
    if not auth_required():
        print("[admin] ⚠ ADMIN_TOKEN 미설정 — 관리자 콘솔이 인증 없이 열려 있습니다. "
              "공개 서버라면 .env 에 ADMIN_TOKEN 을 설정하세요.", file=sys.stderr, flush=True)
    else:
        print("[admin] 관리자 콘솔 토큰 인증 활성", flush=True)


async def require_admin(authorization: str = Header(default=""),
                        x_forwarded_for: str = Header(default="")):
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        return True   # 무인증 모드 — 위 docstring 의 경고 참고
    given = authorization[7:].strip() if authorization.startswith("Bearer ") else authorization.strip()
    # 정답 토큰은 잠금 중에도 통과한다 — 잠금은 브루트포스 지연 장치이지 정당한
    # 관리자를 봉쇄하는 장치가 아니다. (구버전 토큰을 든 탭의 폴링이 잠금을
    # 재점화해 새 토큰까지 429 로 막는 역공을 함께 차단한다.)
    if given and hmac.compare_digest(given, token):
        _AUTH["fail"] = 0
        _AUTH["logged"] = 0  # 성공 시 리셋 — 누적 10회 뒤 감사 기록이 무음화되는 것 방지
        return True
    if time.monotonic() < _AUTH["locked_until"]:
        raise HTTPException(429, "인증 실패가 반복되어 잠겼습니다. 10분 뒤 다시 시도하세요.")
    _AUTH["fail"] += 1
    if _AUTH["logged"] < 10:  # 이력 파일을 무한히 키우지 못하게 창당 10줄 상한
        _AUTH["logged"] += 1
        # XFF 최우측 = 신뢰 프록시(Caddy)가 덧붙인 실제 접속 IP. 최좌측은 위조 가능.
        _append_history({"kind": "auth.fail",
                         "ip": (x_forwarded_for.split(",")[-1].strip() or "-")})
    if _AUTH["fail"] >= 10:
        _AUTH["fail"] = 0
        _AUTH["logged"] = 0
        _AUTH["locked_until"] = time.monotonic() + 600
    await asyncio.sleep(1.0)  # async 지연 — 스레드풀을 잡지 않는다
    raise HTTPException(401, "관리자 토큰이 올바르지 않습니다.")


router = APIRouter(prefix="/api/v1/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])


# ─── 최신화 잡 ──────────────────────────────────────────────────────────────────
JOB = {"id": None, "status": "idle",  # idle | running | done | failed
       "steps": [], "step": None, "log": collections.deque(maxlen=400),
       "startedAt": None, "finishedAt": None, "error": None, "result": None}
RELOAD_LOCK = asyncio.Lock()
# save_params 는 동기 핸들러(스레드풀 병렬)라 read-modify-write 에 락이 필요하다 —
# 없으면 동시 저장 중 나중 쓰기가 먼저 쓴 키를 조용히 되돌린다(lost update).
SAVE_LOCK = threading.Lock()

_STEP_SCRIPT = {"join": "03_join.py", "model": "04_model.py",
                "validate": "07_validate.py", "load": "05_load.py"}
# 각 단계가 갱신하는 라이브 파일 — 스테이징 성공 후 이것만 원자 교체한다.
_STEP_OUT = {
    "join": ["dataset_hwaseong/stops_hwaseong.csv", "dataset_hwaseong/grid_join.csv"],
    "model": ["dataset_hwaseong/grid_metrics.csv", "dataset_hwaseong/norm_stats.json"],
    "validate": ["dataset_hwaseong/validation.json"],
}


def _log(line: str) -> None:
    JOB["log"].append(line)
    print(f"[admin.refresh] {line}", flush=True)


async def _run_script(script: Path, cwd: Path, label: str) -> None:
    JOB["step"] = label
    _log(f"── {label} 시작")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
             # 스테이징에서 돌아도 params.py 가 실제 오버라이드를 읽게 한다
             "HW_VAR_DIR": str(VAR)})
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        _log(f"[{label}] {line.decode('utf-8', 'replace').rstrip()}")
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"{label} 실패 (종료코드 {rc}) — 스테이징만 오염, 라이브 무변경")


async def _reload_data() -> dict:
    """디스크/DB → 메모리 재적재. 실패하면 예외 — DATA 는 손대지 않았으므로
    기존 데이터로 서빙이 계속된다(자동 롤백)."""
    async with RELOAD_LOCK:
        new = await asyncio.to_thread(_ctx["build_snapshot"])   # 기준선 assert 포함 ~5초
        _ctx["DATA"].update(new)                                 # lifespan 과 같은 교체 방식
        apply_runtime_params()                                   # 새 sim 모듈에 오버라이드 재주입!
    kpi = new.get("grid_am", {}).get("kpi", {})
    return {"updatedAt": new.get("meta", {}).get("updatedAt"),
            "cells": len(new.get("cells", {}).get("am", {})),
            "needCellsAm": kpi.get("needCells")}


def _prune_dirs(pattern: str, keep: int) -> None:
    """stage-*/backup-* 이 무한히 쌓여 (도커에선 호스트의) 디스크를 채우지 않게
    최근 keep 개만 남긴다. 이름이 타임스탬프라 정렬 = 시간순이다."""
    dirs = sorted(VAR.glob(pattern))
    for d in dirs[:-keep] if keep else dirs:
        shutil.rmtree(d, ignore_errors=True)


async def _run_refresh(steps: list, reason: str, actor: str) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pipeline_steps = [s for s in ("join", "model", "validate", "load") if s in steps]
    stage = None
    try:
        before_kpi = (_ctx["DATA"].get("grid_am") or {}).get("kpi", {}).get("needCells")

        if pipeline_steps:
            # 1) 스테이징 — 제자리 덮어쓰기 금지. 04_model 은 저장 후 검증이라
            #    실패 시 반쪽 산출물이 남고, 그 상태로 재시작하면 서버가 못 뜬다.
            stage = VAR / f"stage-{ts}"
            _prune_dirs("stage-*", keep=0)      # 이전 실행 잔재 정리 (실패분 포함)
            _prune_dirs("backup-*", keep=5)
            JOB["step"] = "stage"
            _log(f"스테이징 준비: {stage}")
            (stage / "server").mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copytree, ROOT / "dataset_hwaseong", stage / "dataset_hwaseong")
            (stage / "analysis").mkdir(parents=True, exist_ok=True)
            for py in (ROOT / "analysis").glob("*.py"):
                shutil.copy2(py, stage / "analysis" / py.name)
            (stage / "server" / "static").mkdir(parents=True, exist_ok=True)
            spec_file = ROOT / "dataset_hwaseong" / "grid_spec.json"
            if spec_file.exists():
                shutil.copy2(spec_file, stage / "dataset_hwaseong" / "grid_spec.json")

            # 2) 파이프라인 실행 (각 스크립트 말미 assert 가 검증 게이트)
            for s in pipeline_steps:
                await _run_script(stage / "analysis" / _STEP_SCRIPT[s], stage, s)

            # 3) 시뮬 import 게이트 — "스왑하면 서버가 못 뜰 산출물"을 여기서 차단
            gate = ("import importlib.util,sys;"
                    "p=sys.argv[1];"
                    "spec=importlib.util.spec_from_file_location('sim_gate',p);"
                    "m=importlib.util.module_from_spec(spec);"
                    "spec.loader.exec_module(m);"
                    "print('기준선 게이트 통과')")
            JOB["step"] = "gate"
            _log("── 시뮬 기준선 게이트")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", gate, str(stage / "analysis" / "05_simulate.py"),
                cwd=str(stage), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
            out, _ = await proc.communicate()
            for ln in out.decode("utf-8", "replace").splitlines()[-5:]:
                _log(f"[gate] {ln}")
            if proc.returncode != 0:
                raise RuntimeError("시뮬 기준선 게이트 실패 — 산출물을 반영하지 않습니다")

            # 4) 백업 → 5) 파일별 원자 교체 (dataset 과 static 을 한 쌍으로)
            JOB["step"] = "swap"
            swap: list = []
            for s in pipeline_steps:
                swap += _STEP_OUT.get(s, [])
            if "load" in pipeline_steps:
                swap += [f"server/static/{p.name}"
                         for p in (stage / "server" / "static").glob("*.json")]
            backup = VAR / f"backup-{ts}"
            backup.mkdir(parents=True, exist_ok=True)
            for rel in swap:
                live = ROOT / rel
                if live.exists():
                    dst = backup / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live, dst)
            _log(f"백업 {len(swap)}개 → {backup}")
            for rel in swap:
                # stage(var/, 도커에선 바인드 마운트)와 ROOT 는 다른 파일시스템일 수
                # 있어 os.replace 직행은 EXDEV 로 죽는다. 목적지와 같은 디렉토리에
                # .new 로 복사한 뒤 replace — 원자성은 목적지 fs 안에서 성립한다.
                live = ROOT / rel
                tmp = live.with_name(live.name + ".new")
                shutil.copy2(stage / rel, tmp)
                os.replace(tmp, live)
            _log(f"원자 교체 {len(swap)}개 완료")

        # DB 재적재 — 명시 요청("db")했거나, DB 모드에서 계약 JSON 을 새로
        # 구웠을 때("load"). 빼먹으면 reload 가 옛 batch_* 를 읽어 파일만 새것인
        # "성공했는데 화면은 그대로" 상태가 된다.
        if os.environ.get("DATABASE_URL") and ("db" in steps or "load" in pipeline_steps):
            await _run_script(ROOT / "analysis" / "06_load_db.py", ROOT, "db")

        # 재적재 — 파이프라인이 돌았든 아니든 마지막은 항상 메모리 반영
        JOB["step"] = "reload"
        _log("── 메모리 재적재")
        result = await _reload_data()
        result["needCellsAmBefore"] = before_kpi
        result["paramsVersion"] = ((_ctx["DATA"].get("meta") or {}).get("paramsVersion"))
        if "validate" in pipeline_steps:
            try:
                vj = json.loads((ROOT / "dataset_hwaseong" / "validation.json").read_text("utf-8"))
                usable = vj.get("qualitativeUsable")
                result["validation"] = {"qualitativeUsable": usable,
                                        "cvLogR2": (vj.get("r2") or {}).get("cv_log")}
                if isinstance(usable, (int, float)) and usable < 3:
                    _log(f"⚠ 정성 대조 usable {usable}건 < 목표 3건 — 발표 인용 수치를 재확인하세요")
            except Exception as e:
                _log(f"validation.json 요약 실패(무시): {type(e).__name__}")
        JOB.update(status="done", result=result, finishedAt=_now(), step=None)
        _log(f"완료 — needCells(am) {before_kpi} → {result.get('needCellsAm')}")
        _append_history({"kind": "refresh.done", "jobId": JOB["id"], "ok": True,
                         "steps": steps, "result": result, "actor": actor})
    except Exception as e:
        JOB.update(status="failed", error=f"{type(e).__name__}: {e}", finishedAt=_now(), step=None)
        _log(f"실패: {JOB['error']}")
        _append_history({"kind": "refresh.done", "jobId": JOB["id"], "ok": False,
                         "steps": steps, "error": JOB["error"], "actor": actor})
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


# ─── API ───────────────────────────────────────────────────────────────────────
def _param_rows() -> list:
    raw = _read_override()
    rows = []
    for key, s in SPECS.items():
        ov = raw.get(key)
        editable = s["scope"] in ("runtime", "pipeline")
        eff = effective(key)
        pending = False
        if s["scope"] == "pipeline":
            live = _pipeline_effective(key)
            if live is not None:
                eff = live
            pending = ov is not None and eff != ov["value"]
        rows.append({
            "key": key, "label": s["label"], "unit": s["unit"], "type": s["type"],
            "min": s["min"], "max": s["max"], "scope": s["scope"], "group": s["group"],
            "editable": editable, "requiresRefresh": s["scope"] == "pipeline",
            "default": s["default"],
            "override": ov["value"] if ov else None,
            "effective": eff,
            "pending": pending,
            "overridden": ov is not None,
            "reason": ov.get("reason") if ov else None,
            "actor": ov.get("actor") if ov else None,
            "at": ov.get("at") if ov else None,
            "note": s["note"], "applies": s["applies"],
        })
    sim = (_ctx["DATA"] or {}).get("sim")
    for attr, label, note in BASELINE_DISPLAY:
        val = getattr(sim, attr, None) if sim is not None else None
        rows.append({"key": f"baseline.{attr}", "label": label, "unit": "", "type": "float",
                     "min": None, "max": None, "scope": "baseline", "group": "baseline",
                     "editable": False, "requiresRefresh": True,
                     "default": val, "override": None, "effective": val,
                     "overridden": False, "reason": None, "actor": None, "at": None,
                     "note": note, "applies": "기준선(D·S·MI) — 편집은 새 기준선 발행 절차로만"})
    return rows


@router.get("/params")
def get_params():
    raw = _read_override()
    return {"updatedAt": max((v["at"] for v in raw.values()), default=None),
            "overrideCount": len(raw), "params": _param_rows()}


class SaveRequest(BaseModel):
    changes: dict
    reason: str = ""
    actor: str = "admin"


@router.post("/params")
def save_params(req: SaveRequest):
    if JOB["status"] == "running":
        raise HTTPException(409, "데이터 갱신이 진행 중입니다. 끝난 뒤 다시 시도하세요.")
    reason = (req.reason or "").strip()
    if len(reason) < 5:
        raise HTTPException(400, "reason 은 5자 이상이어야 합니다 — 왜 값을 고쳤는지 기록이 남아야 합니다.")
    if not req.changes:
        raise HTTPException(400, "changes 가 비어 있습니다.")

    # 전건 검증 통과 시에만 적용 (all-or-nothing — _validate_placements 의 철학)
    unknown = [k for k in req.changes if k not in SPECS]
    if unknown:
        raise HTTPException(400, f"changes 에 알 수 없는 키가 있습니다: {unknown}")
    # pipeline 계급은 저장 가능하되 산출물 재계산([지표 재계산]) 전에는 화면에
    # 반영되지 않는다 — 응답 requiresRefresh 로 알린다. baseline 계급은 SPECS 에
    # 없어 아래 unknown 검사에서 자동 거부된다.
    normalized: dict = {}
    for k, v in req.changes.items():
        if v is None:                       # null = 기본값 복귀 (revoke)
            normalized[k] = None
            continue
        s = SPECS[k]
        if s["type"] == "int":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
                raise HTTPException(400, f"{k} 는 정수여야 합니다 (받은 값: {v!r})")
            v = int(v)
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise HTTPException(400, f"{k} 는 숫자여야 합니다 (받은 값: {v!r})")
            v = float(v)
        if not (s["min"] <= v <= s["max"]):
            raise HTTPException(
                400, f"{k} 는 {s['min']:,}~{s['max']:,} 사이여야 합니다 (받은 값: {v:,})")
        normalized[k] = v

    with SAVE_LOCK:
        return _save_locked(req, normalized, reason)


def _save_locked(req: "SaveRequest", normalized: dict, reason: str):
    current = _read_override()
    applied = []
    for k, v in normalized.items():
        old = current[k]["value"] if k in current else None
        old_eff = old if old is not None else SPECS[k]["default"]
        if v is None:
            if k in current:
                del current[k]
                applied.append({"key": k, "old": old_eff, "new": SPECS[k]["default"], "revoked": True})
        else:
            if old_eff != v:
                current[k] = {"value": v, "reason": reason, "actor": req.actor, "at": _now()}
                applied.append({"key": k, "old": old_eff, "new": v})
    if not applied:
        raise HTTPException(400, "바뀐 값이 없습니다.")

    # 이력 먼저, 상태 나중 — 상태 파일이 있으면 이력도 반드시 있다
    _append_history({"kind": "param.set", "actor": req.actor, "reason": reason,
                     "changes": applied})
    _write_override(current)
    apply_runtime_params()
    needs = sorted({a["key"] for a in applied if SPECS[a["key"]]["scope"] == "pipeline"})
    return {"ok": True, "applied": applied, "overrideCount": len(current),
            "requiresRefresh": needs, "params": _param_rows()}


class RefreshRequest(BaseModel):
    steps: list = ["reload"]
    reason: str = ""
    actor: str = "admin"


@router.post("/refresh")
async def start_refresh(req: RefreshRequest):
    if JOB["status"] == "running":
        raise HTTPException(409, "이미 갱신이 진행 중입니다.")
    valid = {"join", "model", "validate", "load", "db", "reload"}
    bad = [s for s in req.steps if s not in valid]
    if bad:
        raise HTTPException(400, f"steps 에 알 수 없는 단계가 있습니다: {bad} (허용: {sorted(valid)})")
    if not req.steps:
        raise HTTPException(400, "steps 가 비어 있습니다.")
    if "db" in req.steps and not os.environ.get("DATABASE_URL"):
        raise HTTPException(400, "DATABASE_URL 이 없어 db 단계를 실행할 수 없습니다.")

    JOB.update(id=f"RF-{int(time.time())}", status="running", steps=req.steps,
               step="queued", startedAt=_now(), finishedAt=None, error=None, result=None)
    JOB["log"].clear()
    _append_history({"kind": "refresh.start", "jobId": JOB["id"], "steps": req.steps,
                     "reason": req.reason, "actor": req.actor})
    asyncio.get_running_loop().create_task(_run_refresh(req.steps, req.reason, req.actor))
    return {"ok": True, "jobId": JOB["id"], "steps": req.steps}


@router.get("/status")
def get_status():
    DATA = _ctx["DATA"] or {}
    raw = _read_override()
    meta = DATA.get("meta") or {}
    try:
        ADMIN_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(ADMIN_DIR, os.W_OK)
    except OSError:
        writable = False
    return {
        "data": {"source": DATA.get("_source"), "loadedAt": DATA.get("_loadedAt"),
                 "metaUpdatedAt": meta.get("updatedAt"),
                 "gridCells": len((DATA.get("cells") or {}).get("am", {})),
                 "kpiAm": (DATA.get("grid_am") or {}).get("kpi")},
        "overrides": {"count": len(raw), "keys": sorted(raw),
                      "updatedAt": max((v["at"] for v in raw.values()), default=None)},
        "job": {"id": JOB["id"], "status": JOB["status"], "steps": JOB["steps"],
                "step": JOB["step"], "startedAt": JOB["startedAt"],
                "finishedAt": JOB["finishedAt"], "error": JOB["error"],
                "result": JOB["result"], "logTail": list(JOB["log"])[-40:]},
        "env": {"dbConfigured": bool(os.environ.get("DATABASE_URL")),
                "varDirWritable": writable,
                "authRequired": auth_required()},
    }


@router.get("/history")
def get_history(limit: int = Query(50, ge=1, le=500), kind: Optional[str] = None):
    items = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if kind and ev.get("kind", "").split(".")[0] != kind:
                    continue
                items.append(ev)
    except FileNotFoundError:
        pass
    return {"total": len(items), "items": items[-limit:][::-1]}
