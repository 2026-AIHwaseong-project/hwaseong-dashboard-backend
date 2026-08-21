# -*- coding: utf-8 -*-
"""튜닝 상수 정본 — 04_model · 05_simulate · 05_load · server 가 전부 여기서 import 한다.

왜 한 곳인가
    같은 상수가 2~4벌로 흩어져 있었고 실제로 값이 갈라진 곳이 있었다
    (프론트 MI 폴백, 총액/연환산 단가, 파급 반경 계산 800m vs 표시 2.0km).
    od_curve.py 가 "두 스크립트가 같은 승차량을 다른 자로 나눠 12.5% 어긋난
    사고" 이후 곡선 생성을 한 곳으로 모은 것과 같은 이유다.

관리자 오버라이드 (pipeline 계급)
    server/admin.py 가 var/admin/params_override.json 에 기록한 값 중
    파이프라인 산출물에 구워지는 키(model.*)만 이 모듈이 import 시점에 반영한다.
    → 재계산(03→04→05)을 다시 돌리면 관리자 값이 산출물에 들어간다.
    경로는 HW_VAR_DIR 환경변수 우선 — 관리자 재계산이 스테이징 디렉토리에서
    돌 때도 실제 오버라이드를 읽게 하기 위해서다.
    런타임 계급(단가·주입량·배수)은 여기서 굽지 않는다 — 산출물에 안 들어가고,
    서버가 요청 시점에 apply_runtime_params 로 주입한다.

paramsVersion
    산출물에 구워지는 유효 상수 전체의 해시. meta.json 과 norm_stats.json 에
    스탬프되어 "이 화면 숫자는 어떤 상수로 만든 것인가"를 추적 가능하게 한다.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PERIODS = ["am", "day", "pm", "night"]
PERIOD_HOURS = {"am": 2, "day": 8, "pm": 2, "night": 2}   # 시간대 창 길이(h)

# ── 모델 상수 (기준선에 구워짐 — 바꾸면 새 기준선 발행 = 재계산+재검증) ────────
MI_THRESHOLDS = [-1.2, -0.7, -0.25, 0.25, 0.7, 1.2]
COV_THRESHOLD_M = 600.0
ALPHA_D = 0.5
W_FREQ, W_COV = 0.78, 0.22
DAMP_EXP = 0.65
MI_CLAMP = 2.6
ELD_COEF = 1.6
QUAD = dict(need_zd=0.20, need_mi=0.75, over_zd=-0.30, over_zs=0.30,
            drt_zd=-0.35, drt_zs=-0.35, ok_zd=0.25, ok_zs=0.25, fref_q=0.30)

# over·ok 절대 가드. freq 는 시간대 창 전체의 운행횟수라 시간당으로 환산해서 잰다.
MIN_FREQ_PER_H = 2.0

# 도보권. 승하차 안분 반경 겸 (03_join 의) 커버리지 임계거리.
WALK_M = 800.0

# 프론트 cells[].flowTripsPerDay 용. 전수단 원단위 2.5 × 버스 분담률 0.10.
# ⚠️ 둘 다 가정값이다. 처음 2.5 만 곱했다가 사각지대 잠재수요가 132만 통행/일로
#    나왔는데, 화성시 실제 버스 승차가 일 169,026 이라 8배였다. 전수단을 버스로
#    착각한 것이었다. 사업비 단가와 같은 성격이라 화면에 가정임을 표시해야 한다.
TRIP_RATE, BUS_SHARE = 2.5, 0.10
BUS_TRIP_RATE = TRIP_RATE * BUS_SHARE

# ── 배치 물리 (런타임 계급 — 산출물에 안 들어가고 요청 시점에 쓰임) ────────────
# FSTAR·PHI 는 '셀당'도 '면적당'도 아닌 **시설당** 주입량이다(가상정류장 1개,
# DRT 1대). 미터 반경 거리감쇠로 뿌리므로 격자 크기를 바꿔도 값은 그대로 둔다.
FSTAR = {"am": 4.8, "day": 8.0, "pm": 4.8, "night": 0.0}   # 신설 f* (회/창)
PHI = {"am": 2.4, "day": 9.6, "pm": 2.4, "night": 2.4}     # DRT φ (회/창)
HEADWAY_MULT = 1.43   # 증편 배수 (headway × 0.7)

# 파급 반경(m) — **계산 정본**. 화면 표시는 이 값을 km 로 파생한다.
#   (예전에는 표시가 stop 2.0km 로 계산 800m 와 2.5배 어긋나 있었다 — 지도에
#    반경 원을 그리지 않으므로 이 문구가 사용자가 얻는 유일한 파급 정보였다.)
R_FINAL = {"stop": 800.0, "drt": 3000.0, "freq": 2200.0}


def radius_km(t: str) -> float:
    return round(R_FINAL[t] / 1000, 1)


# ── 비용 (런타임 계급) ─────────────────────────────────────────────────────────
# 총사업비 — 서버(/simulations·/recommendations)와 화면 표기의 정본.
COST_TOTAL = {"stop": 42_000_000, "drt": 180_000_000, "freq": 95_000_000}
# 연환산 — 05_simulate 를 단독 실행(오프라인 데모)할 때만 쓰는 보조값.
# 서빙 경로는 쓰지 않는다. 문서 3곳이 "연환산 폐기, 총사업비 채택"을 못박았다.
COST_ANNUAL = {"stop": 4.2e6, "drt": 1.8e8, "freq": 9.5e7}
COST_META = {"stop": {"basis": "capital", "lifeYears": 10},
             "drt": {"basis": "operating", "lifeYears": 1},
             "freq": {"basis": "operating", "lifeYears": 1}}
DEFAULT_BUDGET = 3_000_000_000
COVERAGE_RANGE = {"stop": [0.15, 0.50], "drt": [0, 0.15], "freq": [0.50, 1.0]}


# ── 관리자 오버라이드 (pipeline 계급만 여기서 굽는다) ─────────────────────────
def _override_path() -> Path:
    var_dir = Path(os.environ.get("HW_VAR_DIR", str(ROOT / "var")))
    return var_dir / "admin" / "params_override.json"


def load_overrides() -> dict:
    """{key: value} — 형식이 어긋난 항목은 버리고 경고 (server/admin.py 와 동일 계약)."""
    try:
        raw = json.loads(_override_path().read_text("utf-8")).get("params", {})
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[params] 오버라이드 파일 손상, 기본값 사용: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return {}
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if (isinstance(v, dict) and isinstance(v.get("value"), (int, float))
                and not isinstance(v.get("value"), bool)):
            out[k] = v["value"]
    return out


# 오버라이드가 덮기 전의 순정 기본값 — admin 콘솔이 "기본값"으로 표시할 근거.
BASE_VALUES = {
    "model.busTripRate": BUS_TRIP_RATE,
    "model.minFreqPerHour": MIN_FREQ_PER_H,
}

_OV = load_overrides()
OVERRIDDEN = {}   # 산출물 주석·assumptions 플래그용: {상수명: True}

if "model.busTripRate" in _OV:
    BUS_TRIP_RATE = float(_OV["model.busTripRate"])
    OVERRIDDEN["busTripRate"] = True
if "model.minFreqPerHour" in _OV:
    MIN_FREQ_PER_H = float(_OV["model.minFreqPerHour"])
    OVERRIDDEN["minFreqPerHour"] = True


def baked() -> dict:
    """산출물에 실제로 구워지는 유효 상수 전체 — paramsVersion 의 재료."""
    return {
        "MI_THRESHOLDS": MI_THRESHOLDS, "COV_THRESHOLD_M": COV_THRESHOLD_M,
        "ALPHA_D": ALPHA_D, "W_FREQ": W_FREQ, "W_COV": W_COV,
        "DAMP_EXP": DAMP_EXP, "MI_CLAMP": MI_CLAMP, "ELD_COEF": ELD_COEF,
        "QUAD": QUAD, "MIN_FREQ_PER_H": MIN_FREQ_PER_H, "WALK_M": WALK_M,
        "BUS_TRIP_RATE": BUS_TRIP_RATE, "PERIOD_HOURS": PERIOD_HOURS,
    }


def params_version() -> str:
    """유효 상수의 지문. 상수 하나라도 다르면 다른 버전 — '이 지도는 어떤 자로
    만든 것인가'를 화면·산출물에서 추적할 수 있게 한다."""
    blob = json.dumps(baked(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:10]
