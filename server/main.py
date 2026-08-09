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


def _period_kpi(sim, p: str, r: dict) -> tuple:
    bk = sim.BASE_KPI[p]
    n  = sim.N
    kpi = {
        "needCells": r["need"],
        "drtCells": r["drt"],
        "overCells": int((r["quad"] == "over").sum()),
        "totalCells": n,
        "needShare": round(r["need"] / n * 100, 1),
        "potentialTripsPerDay": int(sim.S0[p]["potential"].sum()),
        "elderlyTripsPerDay": int((sim.S0[p]["potential"] * sim.S0[p]["eldw"]).sum()),
        "avgMi": round(float(r["mi"].mean()), 3),
    }
    baseline = {
        "needCells": bk["need"],
        "drtCells": bk["drt"],
        "overCells": int((sim.S0[p]["quad0"] == "over").sum()),
        "totalCells": n,
        "needShare": round(bk["need"] / n * 100, 1),
        "potentialTripsPerDay": int(sim.S0[p]["potential"].sum()),
        "elderlyTripsPerDay": int((sim.S0[p]["potential"] * sim.S0[p]["eldw"]).sum()),
        "avgMi": round(float(sim.S0[p]["mi0"].mean()), 3),
    }
    delta = {k: round(kpi[k] - baseline[k], 4) for k in
             ["needCells", "drtCells", "overCells", "needShare", "avgMi"]}
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


@app.post("/api/v1/simulations")
def run_simulation(req: SimRequest):
    _chk_period(req.period)
    sim   = DATA["sim"]
    state = _apply_cumulative(sim, req.placements)
    return _build_sim_response(sim, req.placements, state, req.name, req.budgetKrw)


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
    """전략별 그리디. placed = [(mode, gi, gid, tB, cost)] 반환."""
    state = {p: {"freq": sim.S0[p]["freq"].copy(),
                 "nearest": sim.S0[p]["nearest"].copy()} for p in PERIODS}
    placed, used = [], set()
    region_cnt: dict = {}
    freq_cnt: dict = {}
    budget_left = budget

    am_quad   = sim.S0["am"]["quad0"]
    cand_mask = np.isin(am_quad, ["need", "drt"])
    if region_ids:
        in_reg    = np.array([sim.GIDS[i] in region_ids for i in range(sim.N)])
        cand_mask = cand_mask & in_reg
    cand_idx = np.where(cand_mask)[0]

    types = ["stop"] if strategy == "quick" else list(allowed_types)

    base_cells_am = DATA["cells"]["am"]
    gid_region = {gid: base_cells_am.get(gid, {}).get("region", "") for gid in sim.GIDS}

    # 초기 Bhat 합 (증분 비교 기준)
    cur_B = {p: float(np.sum(sim.Bhat(p, state[p]["freq"],
                                       np.clip(1 - state[p]["nearest"] / sim.COVM, .05, 1))))
             for p in PERIODS}

    for _ in range(max_pl):
        if budget_left <= 0:
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

    return placed, state


@app.post("/api/v1/recommendations")
def run_recommendations(req: RecRequest):
    _chk_period(req.period)
    if req.strategy not in STRAT_META:
        raise HTTPException(400, f"strategy는 {list(STRAT_META)} 중 하나여야 합니다.")

    sim = DATA["sim"]
    region_ids = None
    if req.region:
        region_ids = {c["id"] for c in DATA["cells"]["am"].values()
                      if c["region"] == req.region}

    placed, final_state = _greedy(
        sim, req.strategy, req.budgetKrw, req.maxPlacements,
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

    result = {
        "strategy": req.strategy,
        "strategyLabel": STRAT_META[req.strategy]["label"],
        "note": STRAT_META[req.strategy]["note"],
        "budgetKrw": req.budgetKrw,
        "usedKrw": total_krw,
        "remainingKrw": req.budgetKrw - total_krw,
        "placements": items,
        "simulation": sim_resp,
    }

    if req.includeAlternatives:
        alts = []
        for s in ["efficiency", "equity", "balance", "quick"]:
            if s == req.strategy:
                continue
            alt_types = ["stop"] if s == "quick" else list(req.allowedTypes)
            try:
                ap, _ = _greedy(sim, s, req.budgetKrw, req.maxPlacements, alt_types, region_ids)
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
    import os
    result = []
    for name, cfg in _PROVIDERS.items():
        available = bool(os.environ.get(cfg["env"]))
        result.append({
            "id": name,
            "label": cfg["label"],
            "available": available,
            "envKey": cfg["env"],
            "defaultModel": cfg["default_model"],
            "models": cfg["models"],
        })
    return {"providers": result}


def _detect_provider() -> str:
    """환경변수를 확인해 사용 가능한 첫 번째 프로바이더 반환."""
    import os
    for name, cfg in _PROVIDERS.items():
        if os.environ.get(cfg["env"]):
            return name
    return "claude"  # 키 없으면 claude로 시도 (오류 메시지가 명확함)


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
        return msg.content[0].text

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
    if provider not in _PROVIDERS:
        raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")
    model = req.model or _PROVIDERS[provider]["default_model"]

    period_name  = PERIOD_NAME[req.period]
    period_hours = PERIOD_HOURS[req.period]
    kpi          = req.context.get("kpi", {})
    priorities   = req.context.get("priorities", [])[:5]
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"AI API 오류 ({provider}): {e}")
