# -*- coding: utf-8 -*-
"""
화성시 버스 대시보드 FastAPI 서버
    uvicorn server.main:app --reload   (백엔드 루트에서)

엔드포인트 9개:
    GET  /api/v1/meta
    GET  /api/v1/grid?period=am
    GET  /api/v1/priorities?period=am&limit=10
    GET  /api/v1/stops
    GET  /api/v1/routes
    GET  /api/v1/stops/{stop_id}/profile
    POST /api/v1/simulations
    POST /api/v1/recommendations
    POST /api/v1/reports/draft

의존: server/static/ (analysis/05_load.py 로 생성)
      analysis/05_simulate.py (importlib 로 로드 — 파일명 숫자 시작)
"""
import importlib.util
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent

# .env 로드 — AI 프로바이더 키와 기본값을 파일 하나로 관리한다.
#   ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY : 프로바이더별 키
#   AI_PROVIDER : provider=auto 요청의 기본 프로바이더 (claude|openai|gemini)
#   AI_MODEL    : 기본 모델 재지정 (AI_PROVIDER 와 함께 설정할 때만 적용)
# 이미 셸에 있는 환경변수가 우선한다(load_dotenv 는 덮어쓰지 않음).
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # python-dotenv 미설치면 셸 환경변수만 사용
STATIC = ROOT / "server" / "static"
PERIODS = ["am", "day", "pm", "night"]
PERIOD_NAME = {"am": "출근", "day": "낮", "pm": "퇴근", "night": "심야"}
PERIOD_HOURS = {"am": "07–09", "day": "09–17", "pm": "17–19", "night": "22–24"}
QUAD_LABEL = {
    "need": "고수요·저공급", "over": "저수요·고공급",
    "drt": "수요응답형", "ok": "적정", "mid": "균형권",
}
ACTION_LABEL = {"NEW_STOP": "신설", "ADD_FREQ": "증차", "DRT": "똑버스"}
TYPE_LABEL = {"stop": "정류장 신설", "drt": "똑버스 배치", "freq": "배차 증편"}
COST_KRW = {"stop": 42_000_000, "drt": 180_000_000, "freq": 95_000_000}
RADIUS_KM = {"stop": 2.0, "drt": 3.0, "freq": 2.4}
TRIP_COEF = 3200
STRAT_META = {
    "efficiency": {"label": "효율 최우선", "note": "사업비 1원당 해소 통행량이 가장 큰 순서로 고릅니다."},
    "equity":     {"label": "교통약자 우선", "note": "고령 잠재통행량 기준으로 개선 효과를 측정합니다."},
    "balance":    {"label": "지역 균형", "note": "읍면동당 최대 1개를 원칙으로 고릅니다."},
    "quick":      {"label": "즉시 착수", "note": "시설비 없이 정류장 신설만 선택합니다."},
}

DATA: dict = {}


# ─── 시작/종료 ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    for p in PERIODS:
        DATA[f"grid_{p}"] = json.loads((STATIC / f"grid_{p}.json").read_text("utf-8"))
    DATA["meta"]     = json.loads((STATIC / "meta.json").read_text("utf-8"))
    DATA["stops"]    = json.loads((STATIC / "stops.json").read_text("utf-8"))
    DATA["routes"]   = json.loads((STATIC / "routes.json").read_text("utf-8"))
    DATA["profiles"] = json.loads((STATIC / "profiles.json").read_text("utf-8"))
    DATA["cells"]    = {p: {c["id"]: c for c in DATA[f"grid_{p}"]["cells"]} for p in PERIODS}

    spec = importlib.util.spec_from_file_location("hw_sim", ROOT / "analysis" / "05_simulate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    DATA["sim"] = m
    print("[server] 시뮬레이션 엔진 로드 완료", flush=True)
    yield


app = FastAPI(title="화성시 버스 대시보드 API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# 프론트 폴백: fetch("/api/v1/…").catch(() => fetch("/data/grid_am.json"))
#
# server/static 을 그대로 /data 로 내보낸다. 별도 data/ 폴더에 같은 JSON 을 한 벌 더
# 두면 어느 쪽이 최신인지 헷갈리고 실제로 갈라진다(05_load 와 06_load 가 그랬다).
# 생성기는 05_load.py 하나뿐이고 산출도 server/static 한 곳이다.
if STATIC.exists():
    app.mount("/data", StaticFiles(directory=str(STATIC)), name="data_fallback")

# 프론트엔드 정적 사이트가 옆에 체크아웃돼 있으면 같이 서빙한다 (있을 때만).
#   http://localhost:8000/app/ → hwaseong-dashboard. 같은 원점이라 CORS 가 필요 없다.
#   도커 이미지처럼 백엔드만 복사된 환경에서는 조용히 건너뛴다.
_FRONT = ROOT.parent / "hwaseong-dashboard"
if _FRONT.exists():
    app.mount("/app", StaticFiles(directory=str(_FRONT), html=True), name="frontend")


# ─── 공통 헬퍼 ──────────────────────────────────────────────────────────────────
def _chk_period(p: str):
    if p not in PERIODS:
        raise HTTPException(400, f"period는 {PERIODS} 중 하나여야 합니다.")


def _make_reason(cell: dict) -> str:
    d, s, cov, action = cell["demand"], cell["supply"], cell["coverage"], cell["action"]
    if action == "NEW_STOP":
        return f"수요지수 {d} 대비 공급지수 {s}, 가장 가까운 정류장 도보권 밖 (커버리지 {cov:.2f})"
    if action == "DRT":
        return f"수요지수 {d} 대비 공급지수 {s}, 노선 미연결 지역 (커버리지 {cov:.2f}) — 수요응답형 필요"
    return f"수요지수 {d} 대비 공급지수 {s}, 정류장 도보권 내이나 배차 부족"


def _derive_action(coverage: float, quadrant: str) -> str:
    if quadrant == "drt":
        return "DRT"
    if coverage < 0.42:
        return "NEW_STOP"
    return "ADD_FREQ"


# ─── 시뮬레이션 내부 함수 ──────────────────────────────────────────────────────
def _apply_cumulative(sim, placements: list) -> dict:
    """배치 목록을 순차 적용 → 4시간대 state {freq, nearest} 반환."""
    state = {p: {"freq": sim.S0[p]["freq"].copy(),
                 "nearest": sim.S0[p]["nearest"].copy()} for p in PERIODS}
    for pl in placements:
        gi = sim.IDX.get(str(pl.get("cellId", "")))
        if gi is None:
            continue
        mode  = pl.get("type", "stop")
        count = int(pl.get("count", 1))
        for p in PERIODS:
            f = state[p]["freq"]
            n = state[p]["nearest"]
            d = sim.Dg[gi]
            if mode == "stop":
                mw = d <= sim.WALK
                f[mw] += count * sim.FSTAR[p] * (1 - d[mw] / sim.WALK)
                mc = d <= sim.R_FINAL["stop"]
                n[mc] = np.minimum(n[mc], d[mc])
            elif mode == "drt":
                r = sim.R_FINAL["drt"]
                m = d <= r
                f[m] += count * sim.PHI[p] * (1 - d[m] / r)
            elif mode == "freq":
                ds = np.sqrt((sim.SX - sim.GX[gi])**2 + (sim.SY - sim.GY[gi])**2)
                ms = ds <= sim.R_FINAL["freq"]
                mult = sim.HEADWAY_MULT ** count - 1
                f += sim.Wsg[:, ms] @ (sim.STOP_FREQ[p][ms] * mult)
    return state


def _trips_kpi(p: str, quad_arr) -> tuple:
    """사각지대 잠재수요 — /grid 와 **같은 산식**이어야 한다.

    이전에는 sim.S0[p]["potential"] 을 786격자 전체에 대해 합했는데, 그건
    grid_join 의 시간대별 연령가중 인구지 일 버스통행이 아니다. 그래서 같은
    이름의 KPI 가 화면 두 곳에서 다른 값으로 나왔다.

        /grid          59,501     (need 격자의 flowTripsPerDay 합)
        /simulations   80,182     (전체 격자의 potential 합)

    elderlyTripsPerDay 는 더 어긋나 있었다. potential × (1+1.6·고령비) 였는데
    그건 고령 통행이 아니라 고령가중이 곱해진 전체 통행이라, 전체(94,218)가
    사각지대(5,517)보다 큰 값이 나왔다.

    여기서는 05_load.py 와 같이 **need 격자의 flowTripsPerDay** 를 센다.
    셀별 반올림 정수를 합해야 화면의 셀 합과 어긋나지 않는다(1916b8b 와 같은 이유).
    """
    cells = DATA["cells"][p]
    trips, eld = 0, 0.0
    for gid, q in zip(DATA["sim"].GIDS, quad_arr):
        if q != "need":
            continue
        c = cells.get(gid)
        if c is None:
            continue
        t = int(c["flowTripsPerDay"])
        trips += t
        eld += t * float(c["elderlyRatio"])
    # 고령분은 곱을 다 더한 뒤 한 번만 반올림한다. 셀마다 반올림하면
    # 05_load.py 와 ±1 어긋난다(실측 5,517 대 5,516).
    return trips, int(round(eld))


def _period_kpi(sim, p: str, r: dict) -> tuple:
    bk = sim.BASE_KPI[p]
    n  = sim.N
    now_trips, now_eld = _trips_kpi(p, r["quad"])
    base_trips, base_eld = _trips_kpi(p, sim.S0[p]["quad0"])
    kpi = {
        "needCells": r["need"],
        "drtCells": r["drt"],
        "overCells": int((r["quad"] == "over").sum()),
        "totalCells": n,
        "needShare": round(r["need"] / n * 100, 1),
        "potentialTripsPerDay": now_trips,
        "elderlyTripsPerDay": now_eld,
    }
    baseline = {
        "needCells": bk["need"],
        "drtCells": bk["drt"],
        "overCells": int((sim.S0[p]["quad0"] == "over").sum()),
        "totalCells": n,
        "needShare": round(bk["need"] / n * 100, 1),
        "potentialTripsPerDay": base_trips,
        "elderlyTripsPerDay": base_eld,
    }
    # avgMi 는 뺐다. 기준통계가 시간대별 z 라 평균이 항상 ≈0 인 항등식이고
    # 실제로 -0.0 이 나온다(프론트 자체 분석에서도 폐기 권고).
    delta = {k: round(kpi[k] - baseline[k], 4) for k in
             ["needCells", "drtCells", "overCells", "needShare",
              "potentialTripsPerDay", "elderlyTripsPerDay"]}
    return kpi, baseline, delta


def _cells_for_period(sim, p: str, r: dict, base_cells: dict) -> list:
    cells = []
    for gi, gid in enumerate(sim.GIDS):
        bc = base_cells.get(gid)
        if bc is None:
            continue
        mi_new   = float(r["mi"][gi])
        cov_new  = float(r["cov"][gi])
        quad_new = r["quad"][gi]
        act_new  = _derive_action(cov_new, quad_new)
        cells.append({
            "id": gid,
            "name": bc["name"],
            "region": bc["region"],
            "regionCode": bc["regionCode"],
            "regionKind": bc["regionKind"],
            "lon": bc["lon"],
            "lat": bc["lat"],
            "demand": bc["demand"],
            "supply": round(float(r["S"][gi]) * 100),
            "zDemand": bc["zDemand"],
            "zSupply": round(float(r["zS"][gi]), 4),
            "mi": round(mi_new, 4),
            "flow": bc["flow"],
            "flowTripsPerDay": bc["flowTripsPerDay"],
            "elderlyRatio": bc["elderlyRatio"],
            "coverage": round(cov_new, 4),
            "quadrant": quad_new,
            "quadrantLabel": QUAD_LABEL[quad_new],
            "action": act_new,
            "actionLabel": ACTION_LABEL[act_new],
            "priorityScore": bc["priorityScore"],
            "nearestStopId": bc["nearestStopId"],
            "adjusted": abs(mi_new - float(sim.S0[p]["mi0"][gi])) > 1e-6,
            "bins": {
                "mi": int(r["bin"][gi]),
                "demand": bc["bins"]["demand"],
                "supply": bc["bins"]["supply"],
                "flow": bc["bins"]["flow"],
            },
        })
    return cells


def _build_sim_response(sim, placements_raw: list, state: dict, name: str, budget_krw: int) -> dict:
    results = {p: sim.compute(p, state[p]["freq"], state[p]["nearest"]) for p in PERIODS}

    periods_list = []
    for p in PERIODS:
        kpi, baseline, delta = _period_kpi(sim, p, results[p])
        periods_list.append({"period": p, "periodName": PERIOD_NAME[p],
                              "kpi": kpi, "baseline": baseline, "delta": delta})

    breakdown = []
    for pl in placements_raw:
        mode  = pl.get("type", "stop")
        count = int(pl.get("count", 1))
        unit  = COST_KRW[mode]
        breakdown.append({"type": mode, "label": TYPE_LABEL[mode],
                           "cellId": pl.get("cellId"), "unitKrw": unit,
                           "count": count, "amountKrw": unit * count})
    total_krw = sum(x["amountKrw"] for x in breakdown)

    resolved = sum(sim.BASE_KPI[p]["need"] - results[p]["need"] for p in PERIODS)
    total_dB = sum(
        sim.dB_hat(p, state[p]["freq"] - sim.S0[p]["freq"],
                   results[p]["cov"] - sim.BASE_KPI[p]["cov"])
        for p in PERIODS
    )

    base_cells = DATA["cells"]
    cells_by_period = {
        p: _cells_for_period(sim, p, results[p], base_cells[p]) for p in PERIODS
    }

    pl_list = []
    for pl in placements_raw:
        mode = pl.get("type", "stop")
        gid  = str(pl.get("cellId", ""))
        cell = base_cells["am"].get(gid, {})
        pl_list.append({
            "type": mode, "typeLabel": TYPE_LABEL[mode],
            "cellId": gid, "cellName": cell.get("name", gid),
            "count": int(pl.get("count", 1)),
            "radiusKm": RADIUS_KM[mode], "unitKrw": COST_KRW[mode],
        })

    return {
        "id": f"SIM-{int(time.time() * 1000) % 10_000_000}",
        "name": name,
        "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "placements": pl_list,
        "cost": {"totalKrw": total_krw, "breakdown": breakdown},
        "budgetKrw": budget_krw,
        "periods": periods_list,
        "effectiveness": {
            "resolvedNeedCells": max(resolved, 0),
            "resolvedTripsPerDay": round(max(total_dB, 0)),
            "krwPerTripPerDay": (round(total_krw / total_dB) if total_dB > 0 else None),
        },
        "cellsByPeriod": cells_by_period,
    }


# ─── 1. GET /api/v1/meta ───────────────────────────────────────────────────────
@app.get("/api/v1/meta")
def get_meta():
    return DATA["meta"]


# ─── 2. GET /api/v1/grid ───────────────────────────────────────────────────────
@app.get("/api/v1/grid")
def get_grid(period: str = Query("am")):
    _chk_period(period)
    return DATA[f"grid_{period}"]


# ─── 3. GET /api/v1/priorities ─────────────────────────────────────────────────
@app.get("/api/v1/priorities")
def get_priorities(period: str = Query("am"), limit: int = Query(10)):
    _chk_period(period)
    cells = [c for c in DATA[f"grid_{period}"]["cells"]
             if c["quadrant"] in ("need", "drt")]
    cells.sort(key=lambda c: c["priorityScore"], reverse=True)
    items = [
        {
            "rank": i + 1,
            "cellId": c["id"],
            "name": c["name"],
            "mi": c["mi"],
            "priorityScore": c["priorityScore"],
            "demand": c["demand"],
            "supply": c["supply"],
            "flowTripsPerDay": c["flowTripsPerDay"],
            "elderlyRatio": c["elderlyRatio"],
            "coverage": c["coverage"],
            "action": c["action"],
            "actionLabel": c["actionLabel"],
            "nearestStopId": c["nearestStopId"],
            "reason": _make_reason(c),
        }
        for i, c in enumerate(cells[:limit])
    ]
    return {"period": period, "items": items}


# ─── 4. GET /api/v1/stops ──────────────────────────────────────────────────────
@app.get("/api/v1/stops")
def get_stops():
    return DATA["stops"]


# ─── 5. GET /api/v1/routes ─────────────────────────────────────────────────────
@app.get("/api/v1/routes")
def get_routes():
    return DATA["routes"]


# ─── 6. GET /api/v1/stops/{stop_id}/profile ───────────────────────────────────
@app.get("/api/v1/stops/{stop_id}/profile")
def get_stop_profile(stop_id: str):
    profile = DATA["profiles"].get(stop_id)
    if profile is None:
        raise HTTPException(404, f"프로필 없음: {stop_id}")
    return profile


# ─── 7. POST /api/v1/simulations ──────────────────────────────────────────────
class SimRequest(BaseModel):
    name: str = "시나리오"
    period: str = "am"
    budgetKrw: int = 3_000_000_000
    placements: list = []


MAX_COUNT = 20   # 한 격자에 같은 수단을 몇 개까지. 그 이상은 실무적으로 의미가 없다.


def _validate_placements(sim, placements: list) -> list:
    """배치 입력 검증. 통과한 것만 돌려주고 나머지는 400 으로 막는다.

    검증이 없을 때 실제로 이런 일이 났다.
      · 없는 cellId → 효과는 0인데 비용 4,200만원이 그대로 청구됐다.
        (_apply_cumulative 는 건너뛰는데 비용 계산은 별도라 안 걸러졌다)
      · type: "nosuch" → COST_KRW[mode] 에서 KeyError → 500
      · count: 999 → 정류장 999개에 419억원. 효과는 1칸.
    조용히 틀린 예산을 보여주느니 왜 틀렸는지 알려주고 막는 게 낫다.
    """
    out = []
    for i, pl in enumerate(placements):
        if not isinstance(pl, dict):
            raise HTTPException(400, f"placements[{i}] 는 객체여야 합니다.")
        mode = pl.get("type", "stop")
        if mode not in COST_KRW:
            raise HTTPException(400, f"placements[{i}].type 은 {list(COST_KRW)} 중 하나여야 합니다 (받은 값: {mode!r})")
        gid = str(pl.get("cellId", ""))
        if gid not in sim.IDX:
            raise HTTPException(400, f"placements[{i}].cellId 를 찾을 수 없습니다: {gid!r}")
        try:
            count = int(pl.get("count", 1))
        except (TypeError, ValueError):
            raise HTTPException(400, f"placements[{i}].count 는 정수여야 합니다.")
        if not 1 <= count <= MAX_COUNT:
            raise HTTPException(400, f"placements[{i}].count 는 1~{MAX_COUNT} 사이여야 합니다 (받은 값: {count})")
        out.append({"type": mode, "cellId": gid, "count": count})
    return out


@app.post("/api/v1/simulations")
def run_simulation(req: SimRequest):
    _chk_period(req.period)
    sim   = DATA["sim"]
    placements = _validate_placements(sim, req.placements)
    state = _apply_cumulative(sim, placements)
    return _build_sim_response(sim, placements, state, req.name, req.budgetKrw)


# ─── 8. POST /api/v1/recommendations ──────────────────────────────────────────
class RecRequest(BaseModel):
    strategy: str = "efficiency"
    period: str = "am"
    budgetKrw: int = 3_000_000_000
    maxPlacements: int = 10
    allowedTypes: list = ["stop", "drt", "freq"]
    region: Optional[str] = None
    includeAlternatives: bool = False


def _greedy(sim, strategy: str, budget: int, max_pl: int,
            allowed_types: list, region_ids=None) -> tuple:
    """전략별 그리디. (placed, state, stopped) 반환.

    stopped 는 왜 멈췄는지다. 프론트 계약(docs/API.md §3.7)에 있는 값이고,
    없으면 화면이 "0건인데 예산 소진" 같은 모순 문구를 낸다.
        max_reached | budget_exhausted | budget_too_small
        no_further_gain | no_candidate
    """
    state = {p: {"freq": sim.S0[p]["freq"].copy(),
                 "nearest": sim.S0[p]["nearest"].copy()} for p in PERIODS}
    placed, used = [], set()
    region_cnt: dict = {}
    freq_cnt: dict = {}
    budget_left = budget

    am_quad   = sim.S0["am"]["quad0"]
    cand_mask = np.isin(am_quad, ["need", "drt"])
    # `if region_ids:` 로 쓰면 빈 집합이 falsy 라 필터가 통째로 건너뛰어진다.
    # 오타난 읍면동을 보냈을 때 조용히 화성시 전체 결과가 나오는 게 더 위험하다.
    # None(=범위 지정 없음)과 빈 집합(=그 동에 후보 없음)을 구분한다.
    if region_ids is not None:
        in_reg    = np.array([sim.GIDS[i] in region_ids for i in range(sim.N)])
        cand_mask = cand_mask & in_reg
    cand_idx = np.where(cand_mask)[0]
    if len(cand_idx) == 0:
        return [], state, "no_candidate"

    types = ["stop"] if strategy == "quick" else list(allowed_types)

    base_cells_am = DATA["cells"]["am"]
    gid_region = {gid: base_cells_am.get(gid, {}).get("region", "") for gid in sim.GIDS}

    # 초기 Bhat 합 (증분 비교 기준)
    cur_B = {p: float(np.sum(sim.Bhat(p, state[p]["freq"],
                                       np.clip(1 - state[p]["nearest"] / sim.COVM, .05, 1))))
             for p in PERIODS}

    stopped = "max_reached"
    min_cost = min(COST_KRW[m] for m in (["stop"] if strategy == "quick" else allowed_types))
    if budget < min_cost:
        return [], state, "budget_too_small"

    for _ in range(max_pl):
        if budget_left < min_cost:
            stopped = "budget_exhausted"
            break
        cov_now = np.clip(1 - state["am"]["nearest"] / sim.COVM, 0.05, 1.0)
        best = None

        for mode in types:
            cost = COST_KRW[mode]
            if cost > budget_left:
                continue
            g_ok = {"stop": (cov_now >= 0.15) & (cov_now < 0.5),
                    "freq": cov_now >= 0.5,
                    "drt":  np.ones(sim.N, bool)}[mode]

            for gi in cand_idx:
                if not g_ok[gi] or (mode, gi) in used:
                    continue
                gid = sim.GIDS[gi]
                if strategy == "balance":
                    reg = gid_region.get(gid, "")
                    if reg and region_cnt.get(reg, 0) >= 1:
                        continue

                tB = 0.0
                for p in PERIODS:
                    d  = sim.Dg[gi]
                    f1 = state[p]["freq"].copy()
                    n1 = state[p]["nearest"].copy()
                    if mode == "stop":
                        mw = d <= sim.WALK
                        f1[mw] += sim.FSTAR[p] * (1 - d[mw] / sim.WALK)
                        mc = d <= sim.R_FINAL["stop"]
                        n1[mc] = np.minimum(n1[mc], d[mc])
                    elif mode == "drt":
                        r = sim.R_FINAL["drt"]
                        m = d <= r
                        f1[m] += sim.PHI[p] * (1 - d[m] / r)
                    elif mode == "freq":
                        ds = np.sqrt((sim.SX - sim.GX[gi])**2 + (sim.SY - sim.GY[gi])**2)
                        ms = ds <= sim.R_FINAL["freq"]
                        cnt = freq_cnt.get(gi, 0)
                        mult = sim.HEADWAY_MULT ** (cnt + 1) - sim.HEADWAY_MULT ** cnt
                        f1 += sim.Wsg[:, ms] @ (sim.STOP_FREQ[p][ms] * mult)
                    c1 = np.clip(1 - n1 / sim.COVM, 0.05, 1.0)

                    if strategy == "equity":
                        eld_mask = np.isin(sim.S0[p]["quad0"], ["need", "drt"])
                        b_new = sim.Bhat(p, f1, c1)[eld_mask] * sim.S0[p]["eldw"][eld_mask]
                        b_old = sim.Bhat(p, state[p]["freq"],
                                         np.clip(1 - state[p]["nearest"] / sim.COVM, .05, 1)
                                         )[eld_mask] * sim.S0[p]["eldw"][eld_mask]
                        tB += float(b_new.sum()) - float(b_old.sum())
                    else:
                        tB += float(np.sum(sim.Bhat(p, f1, c1))) - cur_B[p]

                eff = tB / cost
                if best is None or eff > best["eff"]:
                    best = {"mode": mode, "gi": gi, "tB": tB, "eff": eff, "cost": cost}

        if best is None:
            # 예산은 남았는데 넣을 곳이 없다. 한 건도 못 넣었으면 예산이 모자란 것.
            stopped = "no_further_gain" if placed else (
                "budget_too_small" if budget_left < min_cost else "no_candidate")
            break

        gi, mode, cost = best["gi"], best["mode"], best["cost"]
        gid = sim.GIDS[gi]
        budget_left -= cost

        for p in PERIODS:
            d = sim.Dg[gi]
            f = state[p]["freq"]
            n = state[p]["nearest"]
            if mode == "stop":
                mw = d <= sim.WALK
                f[mw] += sim.FSTAR[p] * (1 - d[mw] / sim.WALK)
                mc = d <= sim.R_FINAL["stop"]
                n[mc] = np.minimum(n[mc], d[mc])
            elif mode == "drt":
                r = sim.R_FINAL["drt"]
                m = d <= r
                f[m] += sim.PHI[p] * (1 - d[m] / r)
            elif mode == "freq":
                ds = np.sqrt((sim.SX - sim.GX[gi])**2 + (sim.SY - sim.GY[gi])**2)
                ms = ds <= sim.R_FINAL["freq"]
                cnt = freq_cnt.get(gi, 0)
                mult = sim.HEADWAY_MULT ** (cnt + 1) - sim.HEADWAY_MULT ** cnt
                f += sim.Wsg[:, ms] @ (sim.STOP_FREQ[p][ms] * mult)
            cur_B[p] = float(np.sum(sim.Bhat(p, f, np.clip(1 - n / sim.COVM, .05, 1))))

        if mode == "freq":
            freq_cnt[gi] = freq_cnt.get(gi, 0) + 1
        used.add((mode, gi))
        reg = gid_region.get(gid, "")
        region_cnt[reg] = region_cnt.get(reg, 0) + 1
        placed.append({"mode": mode, "gi": gi, "gid": gid, "tB": best["tB"], "cost": cost})

    return placed, state, stopped


@app.post("/api/v1/recommendations")
def run_recommendations(req: RecRequest):
    _chk_period(req.period)
    if req.strategy not in STRAT_META:
        raise HTTPException(400, f"strategy는 {list(STRAT_META)} 중 하나여야 합니다.")
    bad = [t for t in req.allowedTypes if t not in COST_KRW]
    if bad:
        raise HTTPException(400, f"allowedTypes 에 알 수 없는 수단이 있습니다: {bad}")
    if not req.allowedTypes:
        raise HTTPException(400, "allowedTypes 가 비어 있습니다.")
    if req.budgetKrw < 0:
        raise HTTPException(400, "budgetKrw 는 0 이상이어야 합니다.")
    if not 1 <= req.maxPlacements <= 50:
        raise HTTPException(400, "maxPlacements 는 1~50 사이여야 합니다.")

    sim = DATA["sim"]
    region_ids = None
    if req.region:
        region_ids = {c["id"] for c in DATA["cells"]["am"].values()
                      if c["region"] == req.region}

    # region 이 오면 balance(지역 균형)는 성립하지 않는다. 동별 1건 상한이
    # 곧 1건 추천이라서다. efficiency 로 대체하고 alternatives 에서도 뺀다.
    strategy = "efficiency" if (req.region and req.strategy == "balance") else req.strategy

    placed, final_state, stopped = _greedy(
        sim, strategy, req.budgetKrw, req.maxPlacements,
        list(req.allowedTypes), region_ids,
    )

    placements_raw = [{"type": pl["mode"], "cellId": pl["gid"], "count": 1} for pl in placed]
    sim_resp = _build_sim_response(
        sim, placements_raw, final_state,
        f"{STRAT_META[req.strategy]['label']} 추천안", req.budgetKrw,
    )

    base_cells_am = DATA["cells"]["am"]
    items = [
        {
            "rank": rank + 1,
            "type": pl["mode"],
            "typeLabel": TYPE_LABEL[pl["mode"]],
            "cellId": pl["gid"],
            "cellName": base_cells_am.get(pl["gid"], {}).get("name", pl["gid"]),
            "region": base_cells_am.get(pl["gid"], {}).get("region", ""),
            "count": 1,
            "radiusKm": RADIUS_KM[pl["mode"]],
            "costKrw": pl["cost"],
            "expectedResolvedTrips": round(pl["tB"]),
        }
        for rank, pl in enumerate(placed)
    ]
    total_krw = sum(pl["cost"] for pl in placed)

    # 해소 효과는 시뮬레이션 응답에서 그대로 가져온다. 따로 세면 두 값이 어긋난다.
    am_blk = next((x for x in sim_resp["periods"] if x["period"] == req.period),
                  sim_resp["periods"][0])
    resolved_cells = -int(am_blk["delta"]["needCells"])
    resolved_trips = max(0, -int(am_blk["delta"]["potentialTripsPerDay"]))
    resolved_eld   = max(0, -int(am_blk["delta"]["elderlyTripsPerDay"]))

    result = {
        "method": "budget-constrained greedy marginal benefit",
        "methodLabel": "예산 제약 하 한계효과 최대화",
        "methodNote": "미해결 통행량을 사업비 1원당 가장 많이 줄이는 지점을 순차 선택합니다.",
        # 요청에 없었으면 null = 화성시 전체 (docs/API.md §3.7)
        "region": req.region or None,
        "strategy": strategy,
        "strategyLabel": STRAT_META[strategy]["label"],
        "strategyNote": STRAT_META[strategy]["note"],
        "note": STRAT_META[strategy]["note"],
        "strategies": [{"id": k, "label": v["label"], "note": v["note"]}
                       for k, v in STRAT_META.items()
                       if not (req.region and k == "balance")],
        "budgetKrw": req.budgetKrw,
        "usedKrw": total_krw,
        "remainingKrw": req.budgetKrw - total_krw,
        "placements": items,
        "producedBy": {
            "placements": "최적화 알고리즘 (예산 제약 하 그리디)",
            "narrative": "Claude",
            "deterministic": True,
            "deterministicNote": "같은 조건이면 항상 같은 결과가 나옵니다. "
                                 "다른 안이 필요하면 난수가 아니라 전략(목적)을 바꿉니다.",
        },
        "summary": {
            "count": len(items),
            "totalKrw": total_krw,
            "budgetKrw": req.budgetKrw,
            "budgetUsedPct": round(total_krw / req.budgetKrw * 100, 1) if req.budgetKrw else 0.0,
            "expectedResolvedCells": resolved_cells,
            "expectedResolvedTrips": resolved_trips,
            # 계약(docs/API.md §3.7)에 있는데 빠져 있던 필드. 프론트 어댑터가
            # delta 로 보충하고 있었지만, 서버가 주는 게 맞다.
            "expectedResolvedElderlyTrips": resolved_eld,
            "krwPerTrip": int(total_krw / resolved_trips) if resolved_trips > 0 else None,
            "stoppedBecause": stopped,
            "costCompareBasis": "total",
            "costCompareLabel": "총사업비 기준",
            "costCompareNote": "예산 한도와 같은 기준(총사업비)으로 비교했습니다. "
                               "똑버스·증편은 이듬해에도 같은 예산이 필요합니다.",
        },
        "simulation": sim_resp,
    }

    if req.includeAlternatives:
        alts = []
        for s in ["efficiency", "equity", "balance", "quick"]:
            if s == strategy or (req.region and s == "balance"):
                continue
            alt_types = ["stop"] if s == "quick" else list(req.allowedTypes)
            try:
                ap, _, _st = _greedy(sim, s, req.budgetKrw, req.maxPlacements, alt_types, region_ids)
                alts.append({
                    "strategy": s, "label": STRAT_META[s]["label"],
                    "count": len(ap), "totalKrw": sum(p["cost"] for p in ap),
                    "mix": {m: sum(1 for p in ap if p["mode"] == m)
                            for m in ["stop", "drt", "freq"]},
                })
            except Exception:
                pass
        result["alternatives"] = alts

    return result


# ─── 9. POST /api/v1/reports/draft ────────────────────────────────────────────

# 지원 프로바이더 설정
_PROVIDERS = {
    "claude": {
        "env": "ANTHROPIC_API_KEY",
        "label": "Claude (Anthropic)",
        "default_model": "claude-sonnet-5",
        "models": [
            {"id": "claude-sonnet-5",          "name": "Claude Sonnet 5",     "tier": "standard"},
            {"id": "claude-opus-5",             "name": "Claude Opus 5",       "tier": "premium"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5",   "tier": "fast"},
        ],
    },
    "openai": {
        "env": "OPENAI_API_KEY",
        "label": "GPT (OpenAI)",
        "default_model": "gpt-5.6-sol",
        "models": [
            {"id": "gpt-5.6-sol",   "name": "GPT-5.6 Sol",   "tier": "premium",  "note": "$5/$30 per MTok"},
            {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "tier": "standard", "note": "$2/$12 per MTok"},
            {"id": "gpt-5.6-luna",  "name": "GPT-5.6 Luna",  "tier": "fast",     "note": "$0.20/$1.20 per MTok"},
        ],
    },
    "gemini": {
        "env": "GOOGLE_API_KEY",
        "label": "Gemini (Google)",
        "default_model": "gemini-3.1-pro-preview",
        "models": [
            {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro",        "tier": "premium"},
            {"id": "gemini-3.5-flash",        "name": "Gemini 3.5 Flash",      "tier": "standard"},
            {"id": "gemini-3-flash-preview",  "name": "Gemini 3 Flash",        "tier": "fast"},
            {"id": "gemini-3.1-flash-lite",   "name": "Gemini 3.1 Flash-Lite", "tier": "fast"},
        ],
    },
}


@app.get("/api/v1/providers")
def get_providers():
    """사용 가능한 AI 프로바이더·모델 목록 (프론트 드롭다운용)."""
    result = []
    for name, cfg in _PROVIDERS.items():
        available = bool(os.environ.get(cfg["env"]))
        result.append({
            "id": name,
            "label": cfg["label"],
            "available": available,
            "envKey": cfg["env"],
            "defaultModel": _default_model(name),
            "models": cfg["models"],
        })
    # provider=auto 일 때 실제로 어디로 가는지 (.env AI_PROVIDER 반영)
    return {"providers": result, "configuredDefault": _detect_provider()}


def _detect_provider():
    """provider=auto 의 결정 순서.

    ① .env 의 AI_PROVIDER (키도 있어야 함 — 키 없는 지정은 무시하고 다음으로)
    ② 키를 보유한 첫 번째 프로바이더
    ③ 하나도 없으면 None → 규칙 기반 초안(_fallback_report)
    """
    pref = os.environ.get("AI_PROVIDER", "").strip().lower()
    if pref in _PROVIDERS and os.environ.get(_PROVIDERS[pref]["env"]):
        return pref
    for name, cfg in _PROVIDERS.items():
        if os.environ.get(cfg["env"]):
            return name
    return None


def _default_model(provider: str) -> str:
    """기본 모델. .env 의 AI_MODEL 은 AI_PROVIDER 로 지정한 프로바이더에만
    적용한다 — 다른 프로바이더 요청에 남의 모델 ID 가 넘어가는 사고 방지."""
    env_provider = os.environ.get("AI_PROVIDER", "").strip().lower()
    env_model = os.environ.get("AI_MODEL", "").strip()
    if env_model and env_provider == provider:
        return env_model
    return _PROVIDERS[provider]["default_model"]


def _fallback_report(period: str, kpi: dict, priorities: list) -> dict:
    """AI 키가 없을 때 쓰는 규칙 기반 초안.

    「AI 보고서 생성」 버튼이 깨지지 않게 하려는 것이다. 서버를 만든 첫 번째 이유가
    이 버튼인데(기획서 §7), 발표장에서 키가 안 먹거나 호출이 실패하면 그대로 멎는다.
    정적 JSON 폴백(05_load.py)과 같은 원리로 마지막 방어선을 하나 둔다.

    ⚠️ 문장을 지어내지 않는다. 산출된 수치를 그대로 문장 틀에 끼울 뿐이고,
       응답에 isAiGenerated: false 를 실어 화면이 구분할 수 있게 한다.
    """
    pn, ph = PERIOD_NAME[period], PERIOD_HOURS[period]
    need, total = kpi.get("needCells", 0), kpi.get("totalCells", 0)
    share, trips = kpi.get("needShare", 0), kpi.get("potentialTripsPerDay", 0)
    eld = kpi.get("elderlyTripsPerDay", 0)
    top = priorities[:5]
    return {
        "title": "화성시 대중교통 수급 불일치 분석 및 노선 조정 검토(안)",
        "subtitle": f"{pn} 시간대({ph}) 기준",
        "org": "화성시", "dept": "교통정책과", "period": period,
        "provider": "규칙 기반 초안 (AI 미사용)", "model": None,
        "isAiGenerated": False,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sections": [
            {"key": "summary", "heading": "1. 검토 개요",
             "body": f"{pn} 시간대({ph}) 기준으로 화성시 {total}개 격자를 분석한 결과, "
                     f"수요 대비 공급이 부족한 격자가 {need}개({share}%)로 나타났다. "
                     f"해당 격자의 잠재 통행량은 일 {trips:,}통행이며 이 중 고령층 추정은 "
                     f"{eld:,}통행이다.",
             "bullets": [f"분석 격자 {total}개 (1km 단위)",
                         f"고수요·저공급 {need}개 ({share}%)",
                         f"사각지대 잠재수요 일 {trips:,}통행"]},
            {"key": "priority", "heading": "2. 우선 조치 대상",
             "body": "우선순위는 미스매칭 지수에 수요 규모와 고령 인구 비중을 가중해 산출했다.",
             "bullets": [f"{i}순위 {p.get('name', '')} — {p.get('actionLabel', '')}"
                         f" (수요 {p.get('demand')} / 공급 {p.get('supply')})"
                         for i, p in enumerate(top, 1)]},
        ],
        "tables": [{
            "key": "priority", "title": "노선 조정 우선순위",
            "columns": ["순위", "격자", "권역", "수요", "공급", "MI", "조치"],
            "rows": [[i, p.get("cellId", ""), p.get("name", ""), p.get("demand"),
                      p.get("supply"), p.get("mi"), p.get("actionLabel", "")]
                     for i, p in enumerate(top, 1)],
        }],
        "disclaimer": "AI 연동이 설정되지 않아 산출 수치를 문장 틀에 채운 규칙 기반 초안입니다. "
                      "서술 품질이 필요하면 .env 에 API 키를 설정하세요.",
    }


def _call_ai(provider: str, model: str, prompt: str) -> str:
    """프로바이더별 API 호출 → 텍스트 반환."""
    import os

    if provider == "claude":
        try:
            import anthropic
        except ImportError:
            raise HTTPException(500, "pip install anthropic")
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise HTTPException(500, "ANTHROPIC_API_KEY 환경변수가 없습니다.")
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        # content[0] 이 항상 텍스트인 게 아니다. 최신 모델은 ThinkingBlock 을
        # 먼저 넣어 보내는데, 첫 블록만 보면
        #   AttributeError: 'ThinkingBlock' object has no attribute 'text'
        # 로 죽는다(실측). 텍스트 블록만 골라 이어붙인다.
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    if provider == "openai":
        try:
            import openai
        except ImportError:
            raise HTTPException(500, "pip install openai")
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise HTTPException(500, "OPENAI_API_KEY 환경변수가 없습니다.")
        client = openai.OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content

    if provider == "gemini":
        try:
            import google.generativeai as genai
        except ImportError:
            raise HTTPException(500, "pip install google-generativeai")
        key = os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise HTTPException(500, "GOOGLE_API_KEY 환경변수가 없습니다.")
        genai.configure(api_key=key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(prompt)
        return resp.text

    raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")


class ReportRequest(BaseModel):
    period: str = "am"
    provider: str = "auto"      # auto | claude | openai | gemini
    model: Optional[str] = None  # None → 프로바이더 기본 모델
    format: str = "sections"
    tone: str = "공문"
    sections: list = ["summary", "status", "problem", "plan", "effect", "next"]
    context: dict = {}


@app.post("/api/v1/reports/draft")
def draft_report(req: ReportRequest):
    _chk_period(req.period)

    provider = _detect_provider() if req.provider == "auto" else req.provider
    if provider is not None and provider not in _PROVIDERS:
        raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")

    period_name  = PERIOD_NAME[req.period]
    period_hours = PERIOD_HOURS[req.period]
    # 프론트는 값이 없으면 키를 빼는 게 아니라 null 을 보낸다 (kpi: null).
    # dict.get(key, 기본값) 은 키가 있으면 None 을 그대로 돌려주므로
    # None[:5] 로 터진다 — `or` 로 None 까지 걸러야 한다.
    kpi          = req.context.get("kpi") or {}
    priorities   = (req.context.get("priorities") or [])[:5]

    # 프론트가 context 를 안 보내도 서버가 자기 데이터로 채운다.
    # 그래야 폴백이 빈 보고서가 되지 않는다.
    if not kpi:
        kpi = DATA[f"grid_{req.period}"]["kpi"]
    if not priorities:
        pf = STATIC / f"priorities_{req.period}.json"
        if pf.exists():
            priorities = json.loads(pf.read_text("utf-8"))["items"][:5]

    # 키가 하나도 없으면 AI 를 시도하지 않고 바로 규칙 기반 초안을 준다.
    # 500 을 던지면 화면의 「AI 보고서 생성」 버튼이 그냥 깨진다.
    if provider is None:
        return _fallback_report(req.period, kpi, priorities)
    model = req.model or _default_model(provider)
    sim_ctx      = req.context.get("simulation")
    rec_ctx      = req.context.get("recommendation")

    sim_block = ("시뮬레이션 결과:\n" + json.dumps(sim_ctx, ensure_ascii=False, indent=2)) if sim_ctx else ""
    rec_block = ("추천 배치안:\n"      + json.dumps(rec_ctx, ensure_ascii=False, indent=2)) if rec_ctx else ""

    prompt = f"""화성시 버스 수요·공급 미스매칭 분석 보고서 초안을 {req.tone} 형식으로 작성해주세요.

분석 시간대: {period_name} ({period_hours})

현황 데이터:
- 고수요·저공급(need) 격자: {kpi.get('needCells', '미제공')}개 / 전체 {kpi.get('totalCells', 786)}개
- needShare: {kpi.get('needShare', '미제공')}%
- 잠재통행량(일): {kpi.get('potentialTripsPerDay', '미제공')}통행
- 평균 MI: {kpi.get('avgMi', '미제공')}

우선순위 상위 격자 (최대 5개):
{json.dumps(priorities, ensure_ascii=False, indent=2)}

{sim_block}
{rec_block}

다음 섹션을 포함하여 JSON으로 보고서를 작성하세요 (sections: {json.dumps(req.sections, ensure_ascii=False)}):

{{
  "title": "화성시 대중교통 수급 불일치 분석 및 노선 조정 검토(안)",
  "subtitle": "{period_name} 시간대({period_hours}) 기준",
  "org": "화성시", "dept": "교통정책과",
  "period": "{req.period}",
  "generatedAt": "(생성 일시)",
  "provider": "{_PROVIDERS[provider]['label']}",
  "model": "{model}",
  "sections": [
    {{"key": "summary", "heading": "1. 검토 개요", "body": "본문...", "bullets": ["항목..."]}}
  ],
  "tables": [
    {{"key": "priority", "title": "노선 조정 우선순위",
      "columns": ["순위","격자","권역","수요","공급","MI","조치"],
      "rows": [[1, "격자ID", "새솔동", 72, 44, 1.02, "신설"]]}}
  ],
  "disclaimer": "본 문서는 AI가 자동 생성한 초안입니다. 담당자 검토 후 활용하시기 바랍니다."
}}

JSON만 응답하세요 (마크다운 코드블록 불필요)."""

    try:
        text = _call_ai(provider, model, prompt).strip()
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if m:
            text = m.group(1)
        result = json.loads(text)
        result["generatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        result["provider"]    = _PROVIDERS[provider]["label"]
        result["model"]       = model
        return result
    except json.JSONDecodeError as e:
        return {
            "title": "보고서 생성 오류 — JSON 파싱 실패",
            "subtitle": str(e),
            "period": req.period,
            "provider": _PROVIDERS[provider]["label"],
            "model": model,
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "sections": [],
            "tables": [],
            "disclaimer": "AI 응답을 JSON으로 파싱하지 못했습니다.",
        }
    except Exception as e:
        # 패키지 미설치·키 오류·네트워크 장애 어느 쪽이든 보고서는 나가야 한다.
        # 발표 중에 500 을 띄우느니 규칙 기반 초안을 주고 사유를 함께 적는다.
        fb = _fallback_report(req.period, kpi, priorities)
        detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        fb["disclaimer"] = f"AI 호출에 실패해 규칙 기반 초안으로 대체했습니다 ({provider}: {detail}). " \
                           + fb["disclaimer"]
        return fb
