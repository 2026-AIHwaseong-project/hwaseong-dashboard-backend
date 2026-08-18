# -*- coding: utf-8 -*-
"""
화성시 버스 대시보드 FastAPI 서버
    uvicorn server.main:app --reload   (백엔드 루트에서)

엔드포인트 10개:
    GET  /api/v1/meta
    GET  /api/v1/grid?period=am
    GET  /api/v1/priorities?period=am&limit=10
    GET  /api/v1/stops
    GET  /api/v1/routes
    GET  /api/v1/stops/{stop_id}/profile
    POST /api/v1/simulations
    POST /api/v1/recommendations
    POST /api/v1/reports/draft
    POST /api/v1/chat

의존: server/static/ (analysis/05_load.py 로 생성)
      server/chat_kb.md (챗봇 지식 — 사람이 쓰는 파일)
      analysis/05_simulate.py (importlib 로 로드 — 파일명 숫자 시작)
      server/db.py — DATABASE_URL 이 있으면 여기서 대신 읽습니다(없으면 JSON)
"""
import importlib.util
import json
import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db

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
STRAT_META = {
    "efficiency": {"label": "효율 최우선", "note": "사업비 1원당 해소 통행량이 가장 큰 순서로 고릅니다."},
    "equity":     {"label": "교통약자 우선", "note": "고령 잠재통행량 기준으로 개선 효과를 측정합니다."},
    "balance":    {"label": "지역 균형", "note": "읍면동당 최대 1개를 원칙으로 고릅니다."},
    "quick":      {"label": "즉시 착수", "note": "시설비 없이 정류장 신설만 선택합니다."},
}

DATA: dict = {}


# ─── 시작/종료 ──────────────────────────────────────────────────────────────────
def _load_json() -> dict:
    """계약 JSON 에서 읽는 기본 경로. DB 없이도 서버는 이대로 완전히 동작합니다."""
    src = {f"grid_{p}": json.loads((STATIC / f"grid_{p}.json").read_text("utf-8"))
           for p in PERIODS}
    for k in ("meta", "stops", "routes", "profiles"):
        src[k] = json.loads((STATIC / f"{k}.json").read_text("utf-8"))
    return src


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DATABASE_URL 이 있으면 DB(v_* 뷰)에서, 없거나 실패하면 JSON 에서 읽습니다.
    # 둘은 같은 것을 돌려줘야 합니다 — 확인은 python analysis/06_verify_db.py.
    # 아래 어느 쪽으로 왔든 DATA 의 모양이 같으므로 엔드포인트·시뮬레이션 엔진은
    # 자기가 무엇을 읽고 있는지 알 필요가 없습니다.
    src = db.load_all(QUAD_LABEL, ACTION_LABEL)
    if src is None:
        src = _load_json()
        print("[server] 계약 JSON 에서 로드", flush=True)
    DATA.update(src)
    DATA["cells"] = {p: {c["id"]: c for c in DATA[f"grid_{p}"]["cells"]} for p in PERIODS}

    spec = importlib.util.spec_from_file_location("hw_sim", ROOT / "analysis" / "05_simulate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    DATA["sim"] = m
    print("[server] 시뮬레이션 엔진 로드 완료", flush=True)
    yield


def _json(payload) -> Response:
    """큰 응답의 직렬화 우회 — FastAPI 기본 jsonable_encoder 는 값 하나하나를
    재귀 검사해서, 500m 격자(3,144셀×4시간대)에서는 /simulations 인코딩에만
    39초가 걸렸다(실측). json.dumps 직행이면 같은 응답이 0.1초대다.
    numpy 스칼라가 섞여 들어오면 .item() 으로 강등한다.

    allow_nan=False 는 일부러 남긴다 — 기본값(True)이면 NaN/Infinity 가
    JSON 문법에 없는 리터럴로 본문에 실려 200 으로 나가고, 브라우저의
    JSON.parse 가 원인 모를 예외를 던진다. Starlette 기본 JSONResponse 도
    allow_nan=False 라, 이 직행 경로가 그 방어를 조용히 걷어내지 않게 한다."""
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                           allow_nan=False,
                           default=lambda o: o.item() if hasattr(o, "item") else str(o)),
        media_type="application/json; charset=utf-8",
    )


app = FastAPI(title="화성시 버스 대시보드 API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# 시뮬레이션 응답(cellsByPeriod)이 1.5MB — JSON 이라 8~10배 압축됩니다.
# 격자를 500m 로 세분화하면 4배가 더 커지므로 압축이 사실상 필수입니다.
app.add_middleware(GZipMiddleware, minimum_size=1024)

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
    class _NoCacheStatic(StaticFiles):
        """개발 중에는 브라우저 캐시를 끕니다.

        StaticFiles 는 ETag/Last-Modified 를 주는데, 프론트 JS 를 고치고 새로고침해도
        브라우저가 캐시본을 계속 써서 "코드는 바꿨는데 화면은 그대로"가 됩니다.
        실제로 map.js 를 고쳤는데 옛 코드가 도는 걸 한참 쫓았습니다.
        """
        def is_not_modified(self, response_headers, request_headers) -> bool:
            return False

        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            return resp

    app.mount("/app", _NoCacheStatic(directory=str(_FRONT), html=True), name="frontend")


# ─── 공통 헬퍼 ──────────────────────────────────────────────────────────────────
def _chk_period(p: str):
    if p not in PERIODS:
        raise HTTPException(400, f"period는 {PERIODS} 중 하나여야 합니다.")


def _make_reason(cell: dict) -> str:
    d, s, cov, action = cell["demand"], cell["supply"], cell["coverage"], cell["action"]
    if action == "NEW_STOP":
        return (f"수요지수 {d} 대비 공급지수 {s}, 가장 가까운 정류장이 300~510m "
                f"(커버리지 {cov:.2f}) — 노선은 지나지만 정류장이 멀다")
    if action == "DRT":
        return (f"수요지수 {d} 대비 공급지수 {s}, 가장 가까운 정류장이 510m 밖 "
                f"(커버리지 {cov:.2f}) — 노선 미연결, 수요응답형 필요")
    return f"수요지수 {d} 대비 공급지수 {s}, 정류장 도보권(300m) 내이나 배차 부족"


def _derive_action(coverage: float, quadrant: str) -> str:
    """배치 후 갱신된 coverage 로 수단 배지를 다시 매긴다.

    04_model.py 의 action 산출과 **같은 경계**를 쓴다(0.15 / 0.50). 그래야
    지도 배지와 POST /recommendations 의 배타 게이트가 같은 말을 한다.
    quadrant 는 수요·공급 위치를 나타내는 별개 축이라 여기서 보지 않는다."""
    if coverage < 0.15:
        return "DRT"
    if coverage < 0.5:
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

    이전에는 sim.S0[p]["potential"] 을 전 격자(1km 배포판 기준 786칸)에 대해 합했는데, 그건
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
    return _json(DATA["meta"])


# ─── 2. GET /api/v1/grid ───────────────────────────────────────────────────────
@app.get("/api/v1/grid")
def get_grid(period: str = Query("am")):
    _chk_period(period)
    return _json(DATA[f"grid_{period}"])


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
    return _json(DATA["stops"])


# ─── 5. GET /api/v1/routes ─────────────────────────────────────────────────────
@app.get("/api/v1/routes")
def get_routes():
    return _json(DATA["routes"])


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
    return _json(_build_sim_response(sim, placements, state, req.name, req.budgetKrw))


# ─── 8. POST /api/v1/recommendations ──────────────────────────────────────────
class RecRequest(BaseModel):
    strategy: str = "efficiency"
    period: str = "am"
    budgetKrw: int = 3_000_000_000
    maxPlacements: int = 10
    allowedTypes: list = ["stop", "drt", "freq"]
    region: Optional[str] = None
    cellIds: Optional[list] = None   # 임의 영역(지도 드래그) — 후보를 이 격자로 제한
    includeAlternatives: bool = False


def _greedy(sim, strategy: str, budget: int, max_pl: int,
            allowed_types: list, region_ids=None, period: str = "am") -> tuple:
    """전략별 그리디. (placed, state, stopped) 반환.

    stopped 는 왜 멈췄는지다. 계약(docs/API_SPEC.md §8)에 있는 값이고,
    없으면 화면이 "0건인데 예산 소진" 같은 모순 문구를 낸다.
        max_reached | budget_exhausted | budget_too_small
        no_further_gain | no_candidate

    period 는 **최적화 대상 시간대**다. 후보 사분면·수단 게이트·목적함수가
    전부 이 시간대 기준이다. 예전에는 후보와 게이트를 am 으로 못박고 목적함수는
    4시간대를 합산해서, period 를 바꿔도 배치가 글자 하나 안 바뀌었다 —
    "시간대를 바꾸면 우선순위가 뒤집힌다"는 이 프로젝트의 핵심 주장을
    정작 추천 화면만 반영하지 못하고 있었다.

    배치의 물리적 효과는 여전히 4시간대 전부에 적용된다(아래 상태 갱신 루프).
    고르는 기준만 요청 시간대이고, 파급 효과는 응답의 periods[] 로 전부 보고된다.
    """
    state = {p: {"freq": sim.S0[p]["freq"].copy(),
                 "nearest": sim.S0[p]["nearest"].copy()} for p in PERIODS}
    placed, used = [], set()
    placed_idx = []          # (mode, gi) — 같은 수단 최소 이격(800m) 검사용
    region_cnt: dict = {}
    freq_cnt: dict = {}
    budget_left = budget

    # 후보는 **요청 시간대**의 사분면으로 고른다. 심야에만 사각지대인 격자가
    # am 기준 후보 목록에서 통째로 빠지던 문제를 여기서 막는다.
    cand_mask = np.isin(sim.S0[period]["quad0"], ["need", "drt"])
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

    stopped = "max_reached"
    min_cost = min(COST_KRW[m] for m in (["stop"] if strategy == "quick" else allowed_types))
    if budget < min_cost:
        return [], state, "budget_too_small"

    # equity 의 수혜 대상(need/drt) 마스크 — 기준선 사분면 기반이라 불변.
    # 목적함수가 요청 시간대 하나이므로 그 시간대만 만든다.
    eld_mask = ({period: np.isin(sim.S0[period]["quad0"], ["need", "drt"])}
                if strategy == "equity" else None)

    for _ in range(max_pl):
        if budget_left < min_cost:
            stopped = "budget_exhausted"
            break
        cov_now = np.clip(1 - state[period]["nearest"] / sim.COVM, 0.05, 1.0)

        # 현재 상태의 Bhat 벡터 — 스텝당 1회. 후보 평가는 아래에서 이 벡터와의
        # **영향권 부분 차이**만 계산한다. 배치 영향권 밖 셀은 (새 합)−(현 합)에서
        # 정확히 상쇄되므로 계산할 필요가 없다 — 전체 재계산은 500m(3,144셀)에서
        # 스텝당 100초를 넘던 핫스팟이었다(실측 10건 1,040초).
        # 목적함수가 요청 시간대 하나이므로 기준 벡터도 그 시간대만 만든다.
        c0 = np.clip(1 - state[period]["nearest"] / sim.COVM, 0.05, 1.0)
        curBvec = {period: sim.Bhat(period, state[period]["freq"], c0)}

        best = None

        for mode in types:
            cost = COST_KRW[mode]
            if cost > budget_left:
                continue
            # 수단은 coverage 로 **배타 결정**된다 (README §2 · docs/API_SPEC.md §8).
            #   cov ≥ 0.50        정류장은 도보권 안 → 버스가 안 온다 → freq 증편
            #   0.15 ≤ cov < 0.50 노선은 지나는데 정류장이 멀다 → stop 신설
            #   cov < 0.15        노선 자체가 없다 → 고정노선 비효율 → drt 똑버스
            #
            # drt 를 전 격자 허용(np.ones)으로 두면 세 수단이 모든 후보에서 경쟁하는데,
            # 통행/원 효율이 freq 의 1/45 라 예산을 아무리 키워도 순서가 오지 않는다.
            # 실제로 4전략 × 4시간대 전부에서 drt 채택이 0건이었다 — 지도는 사각지대
            # 격자 대부분에 "똑버스"를 권하는데 추천 화면만 한 대도 안 놓는 모순이었다.
            # 배타 게이트로 되돌리면 cov<0.15 구간에서는 drt 가 유일한 수단이 된다.
            g_ok = {"stop": (cov_now >= 0.15) & (cov_now < 0.5),
                    "freq": cov_now >= 0.5,
                    "drt":  cov_now < 0.15}[mode]

            for gi in cand_idx:
                if not g_ok[gi] or (mode, gi) in used:
                    continue
                # 같은 수단끼리 도보권(sim.WALK=800m) 안에 겹쳐 배치하지 않는다.
                # 1km 격자에서는 셀 간격 자체가 1km 라 이 제약이 발동하지 않지만,
                # 격자를 세분화하면(500m) 인접 형제 셀 두 곳이 같은 도보권을
                # 이중 서비스하는 추천이 나온다 — 셀 크기가 곧 이격 제약이던 전제 제거.
                if any(pm == mode and sim.Dg[gi][pg] < sim.WALK
                       for pm, pg in placed_idx):
                    continue
                gid = sim.GIDS[gi]
                if strategy == "balance":
                    reg = gid_region.get(gid, "")
                    if reg and region_cnt.get(reg, 0) >= 1:
                        continue

                # ── 영향권 인덱스 (시간대 무관) ──
                d = sim.Dg[gi]
                if mode == "stop":
                    # freq 는 WALK(800m) 안, nearest 는 R_FINAL.stop(800m) 안에서만
                    # 변한다 — 둘 다 800m 라 하나의 마스크로 충분하다.
                    idx = np.where(d <= sim.WALK)[0]
                    ms = mult = None
                elif mode == "drt":
                    idx = np.where(d <= sim.R_FINAL["drt"])[0]
                    ms = mult = None
                else:  # freq — 선택 정류장들의 도보권에 있는 셀만 영향
                    ds = np.sqrt((sim.SX - sim.GX[gi])**2 + (sim.SY - sim.GY[gi])**2)
                    ms = np.where(ds <= sim.R_FINAL["freq"])[0]
                    cnt = freq_cnt.get(gi, 0)
                    mult = sim.HEADWAY_MULT ** (cnt + 1) - sim.HEADWAY_MULT ** cnt
                    idx = np.where(sim.Wsg[:, ms].sum(axis=1) > 0)[0]
                if len(idx) == 0:
                    continue

                tB = 0.0
                # 목적함수는 **요청 시간대 하나**다. 4시간대를 합산하면 출근의
                # 큰 수요가 심야를 항상 덮어써서 period 가 결과에 영향을 주지 못한다.
                for p in (period,):
                    f_sub = state[p]["freq"][idx]
                    n_sub = state[p]["nearest"][idx]
                    if mode == "stop":
                        f_sub = f_sub + sim.FSTAR[p] * (1 - d[idx] / sim.WALK)
                        n_sub = np.minimum(n_sub, d[idx])
                    elif mode == "drt":
                        r = sim.R_FINAL["drt"]
                        f_sub = f_sub + sim.PHI[p] * (1 - d[idx] / r)
                    else:
                        f_sub = f_sub + sim.Wsg[np.ix_(idx, ms)] @ (sim.STOP_FREQ[p][ms] * mult)
                    c_sub = np.clip(1 - n_sub / sim.COVM, 0.05, 1.0)
                    # sim.Bhat 의 부분 평가 — 같은 식을 idx 셀에만 적용
                    P = sim.POIS[p]
                    b_sub = P["mu"][idx] * np.exp(np.clip(
                        P["b2"] * (np.log1p(f_sub) - P["lq0"][idx])
                        + P["b3"] * (c_sub - P["cov0"][idx]), -20, 6))
                    if strategy == "equity":
                        w = sim.S0[p]["eldw"][idx] * eld_mask[p][idx]
                        tB += float((b_sub * w).sum()) - float((curBvec[p][idx] * w).sum())
                    else:
                        tB += float(b_sub.sum()) - float(curBvec[p][idx].sum())

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
            # 비교 기준(curBvec)은 다음 스텝 시작에서 새 상태로 다시 계산된다

        if mode == "freq":
            freq_cnt[gi] = freq_cnt.get(gi, 0) + 1
        used.add((mode, gi))
        placed_idx.append((mode, gi))
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
    # _greedy 는 임의의 격자 ID 집합을 받는다. 읍면동 이름(region)은 그 집합을
    # 만드는 한 가지 방법일 뿐이라, 지도에서 끈 영역(cellIds)도 같은 경로로 쓴다.
    #
    # `is not None` 이어야 한다. `if req.cellIds:` 로 쓰면 빈 배열이 falsy 라
    # "이 영역으로 제한해 달라"는 요청이 조용히 화성시 전체 추천으로 바뀐다.
    # _greedy 가 496행 주석에서 못박은 함정을 호출부에서 한 단계 위로 되살리는
    # 꼴이다. 형제 파라미터인 region 도 없는 동네를 주면 빈 집합 → 0건이 계약이고
    # e2e-live.js 가 그걸 단언한다 — cellIds 만 정반대로 동작하면 안 된다.
    #
    # str() 로 감싸는 것은 스키마가 bare list(=list[Any])라서다. 딕셔너리 같은
    # 해시 불가 원소가 들어오면 set() 이 TypeError 를 던져 400 이 아니라 500 이
    # 나간다. 같은 함수의 strategy·allowedTypes 는 전부 400 으로 돌려준다.
    region_ids = None
    if req.cellIds is not None:
        region_ids = {str(x) for x in req.cellIds}
    elif req.region:
        region_ids = {c["id"] for c in DATA["cells"]["am"].values()
                      if c["region"] == req.region}

    # region 이 오면 balance(지역 균형)는 성립하지 않는다. 동별 1건 상한이
    # 곧 1건 추천이라서다. efficiency 로 대체하고 alternatives 에서도 뺀다.
    strategy = "efficiency" if (region_ids is not None and req.strategy == "balance") else req.strategy

    placed, final_state, stopped = _greedy(
        sim, strategy, req.budgetKrw, req.maxPlacements,
        list(req.allowedTypes), region_ids, req.period,
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

    # 실제 설정된 프로바이더를 그대로 씁니다 — "Claude" 로 못박아 두면 .env 를
    # gemini 로 바꿔도 화면엔 계속 Claude 라고 나갑니다(실측: 여기만 놓쳤었음).
    narrative_provider = _detect_provider()
    narrative_label = _PROVIDERS[narrative_provider]["label"] if narrative_provider else "AI 미설정 — 규칙 기반 초안"

    result = {
        "method": "budget-constrained greedy marginal benefit",
        "methodLabel": "예산 제약 하 한계효과 최대화",
        "methodNote": f"{PERIOD_NAME[req.period]} 시간대 기준으로, 미해결 통행량을 "
                      "사업비 1원당 가장 많이 줄이는 지점을 순차 선택합니다. "
                      "수단은 정류장 접근성(coverage)으로 배타 결정되며, "
                      "배치 효과는 4개 시간대 전부에 반영해 보고합니다.",
        # 요청에 없었으면 null = 화성시 전체 (docs/API_SPEC.md §8)
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
            "narrative": narrative_label,
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
            # 계약(docs/API_SPEC.md §8)에 있는데 빠져 있던 필드. 프론트 어댑터가
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
                # 대안 전략도 본안과 **같은 시간대**로 비교해야 한다.
                # period 를 안 넘기면 본안만 요청 시간대이고 대안 4종은 출근 기준이 되어,
                # 화면의 전략 비교표가 서로 다른 시간대를 나란히 놓게 된다.
                ap, _, _st = _greedy(sim, s, req.budgetKrw, req.maxPlacements,
                                     alt_types, region_ids, req.period)
                alts.append({
                    "strategy": s, "label": STRAT_META[s]["label"],
                    "count": len(ap), "totalKrw": sum(p["cost"] for p in ap),
                    "mix": {m: sum(1 for p in ap if p["mode"] == m)
                            for m in ["stop", "drt", "freq"]},
                })
            except Exception:
                pass
        result["alternatives"] = alts

    return _json(result)


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
            {"id": "gemini-3.6-flash",        "name": "Gemini 3.6 Flash",      "tier": "standard"},
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
    이 버튼인데(README §8), 발표장에서 키가 안 먹거나 호출이 실패하면 그대로 멎는다.
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
             "bullets": [f"분석 격자 {total}개 "
                         f"({DATA['meta']['grid']['sizeMeters'] / 1000:g}km 단위)",
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


#  보고서 한 벌(6개 섹션 + 표)이 한글로 5,000자를 넘습니다. 4096 으로 두었더니
#  JSON 이 중간에 잘려 "Expecting ',' delimiter: line 96 column 43 (char 4907)" 로
#  파싱이 실패했습니다(실측). 잘린 응답은 무엇을 고쳐도 복구가 안 되므로 상한을 올립니다.
AI_MAX_TOKENS = 8192


# ─── AI 호출 코어 — 여러 사람이 동시에 눌러도 버티게 ──────────────────────────
#
#  예전에는 이 파일의 AI 호출이 전부 동기 함수였습니다. FastAPI 는 `def` 핸들러를
#  anyio 스레드풀에서 돌리는데, 이 파일의 **다른 엔드포인트도 전부 `def`** 라
#  같은 풀을 나눠 씁니다. AI 호출 하나가 수십 초 붙들면 그 시간만큼 스레드가 묶이고,
#  발표장에서 여러 명이 동시에 챗봇을 두드리면 /grid·/meta 같은 조회까지 멎습니다.
#  그래서 AI 경로만 `async def` 로 바꿔 스레드를 아예 점유하지 않게 했습니다.
#
#  ⚠️ IP 당 레이트리밋으로 막지 않습니다. 53073f2 에서 넣었다가 094ebeb 에서
#     되돌린 이유 그대로입니다 — 행사장 WiFi 뒤 심사위원 여럿이 같은 IP 로 잡혀
#     정상 사용자가 429 를 봅니다. 대신 **거절하지 않고 줄을 세웁니다**:
#     동시 호출 수만 세마포어로 묶고 나머지는 잠깐 기다렸다 들어갑니다.
AI_MAX_CONCURRENCY   = int(os.environ.get("AI_MAX_CONCURRENCY", "6"))
AI_QUEUE_TIMEOUT_S   = float(os.environ.get("AI_QUEUE_TIMEOUT_S", "25"))
AI_REQUEST_TIMEOUT_S = float(os.environ.get("AI_REQUEST_TIMEOUT_S", "120"))
AI_MAX_RETRIES       = int(os.environ.get("AI_MAX_RETRIES", "2"))

# ─── 속도 — thinking budget · 챗봇 토큰 상한 ──────────────────────────────
#
#  실측(2026-08): 같은 질문 두 문장 답에 9~44초가 걸렸는데, 그중 39초 이상이
#  "생각"이었고 실제 글자가 나오는 건 마지막 0.1초 안쪽이었다(스트리밍이 안 도와줌).
#  Gemini 는 thinking_budget=0 도 thinking_level="off" 도 이 모델에서 거부한다
#  (400) — 완전히 끄는 건 안 되고 낮추는 것만 된다. 128 은 실측으로 통과했다.
#  같은 값도 실행마다 편차가 커서(9초~44초) 이 값이 "항상 빠르게"를 보장하진
#  않는다 — 최악을 낮추는 것이지 편차 자체를 없애는 게 아니다.
#
#  컨텍스트 캐싱(cached_content)도 실측했지만 뺐다 — Gemini API가 `cached_content`
#  와 `system_instruction`을 같이 못 받는 제약이 있어(400) 프롬프트 조립을 통째로
#  갈라야 했고, 그 갈라진 구조에서 report 모드가 max_tokens 캡과 얽혀 실제로
#  깨졌다(6섹션 초안 수정이 잘림). 속도 이득도 편차 폭 안에 묻혀 확실치 않았다 —
#  복잡도만 늘고 이득은 불확실해서 걷어냈다.
AI_THINKING_BUDGET = int(os.environ.get("AI_THINKING_BUDGET", "256"))

#  챗봇 답은 _CHAT_RULES 가 3~5문장으로 못박아 둔다. AI_MAX_TOKENS(8192)는
#  보고서용 — 6개 섹션짜리라 그대로 둔다. 챗봇 help 모드에만 낮은 상한을 따로 둔다
#  (report 모드는 안 고친 섹션까지 포함해 draft.sections 전체를 되돌려줘야 해서
#  낮은 캡을 쓰면 잘린다 — chat() 참고).
#  1024로 실측했을 때 목록형 답이 1011자까지 나온 사례가 있어(문장은 안 잘렸지만
#  여유가 크지 않았다) 2048로 올렸다 — 보통 답은 수백 자라 흔한 경우엔 비용·속도
#  차이가 없고, 꼬리 쪽(긴 목록 답)에서 잘릴 위험만 줄인다.
AI_CHAT_MAX_TOKENS = int(os.environ.get("AI_CHAT_MAX_TOKENS", "2048"))

_ai_sem: Optional[asyncio.Semaphore] = None
_ai_clients: dict = {}


class AIBusy(Exception):
    """줄이 너무 길어 대기 상한을 넘겼다. 500 이 아니라 안내 문구로 나갑니다."""


@asynccontextmanager
async def _ai_slot():
    """동시 호출 슬롯 하나를 잡는다. 못 잡으면 AIBusy."""
    global _ai_sem
    if _ai_sem is None:                      # 이벤트 루프 안에서 처음 만든다
        _ai_sem = asyncio.Semaphore(AI_MAX_CONCURRENCY)
    try:
        await asyncio.wait_for(_ai_sem.acquire(), AI_QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise AIBusy()
    try:
        yield
    finally:
        _ai_sem.release()


def _ai_client(provider: str):
    """프로바이더별 비동기 클라이언트 — **프로세스에 하나만** 만들어 재사용한다.

    예전에는 요청마다 새로 만들었습니다. 그러면 호출마다 TLS 핸드셰이크를 다시 하고
    커넥션 풀도 매번 버려집니다. 동시 요청이 몰릴 때 가장 먼저 티가 나는 낭비입니다.
    타임아웃·재시도도 여기서 한 번만 못박습니다(SDK 기본값에 맡기지 않습니다).
    """
    if provider in _ai_clients:
        return _ai_clients[provider]

    cfg = _PROVIDERS.get(provider)
    if cfg is None:
        raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")
    key = os.environ.get(cfg["env"])
    if not key:
        raise HTTPException(500, f"{cfg['env']} 환경변수가 없습니다.")

    if provider == "claude":
        try:
            import anthropic
        except ImportError:
            raise HTTPException(500, "pip install anthropic")
        client = anthropic.AsyncAnthropic(
            api_key=key, timeout=AI_REQUEST_TIMEOUT_S, max_retries=AI_MAX_RETRIES,
        )
    elif provider == "openai":
        try:
            import openai
        except ImportError:
            raise HTTPException(500, "pip install openai")
        client = openai.AsyncOpenAI(
            api_key=key, timeout=AI_REQUEST_TIMEOUT_S, max_retries=AI_MAX_RETRIES,
        )
    elif provider == "gemini":
        try:
            from google import genai as genai_sdk
            from google.genai import types as genai_types
        except ImportError:
            raise HTTPException(500, "pip install google-genai")
        # google-generativeai(구버전)에서 옮겨왔다 — thinking budget이 그 SDK의
        # GenerationConfig 프로토콜엔 필드 자체가 없었다(실측 확인).
        #
        # retry_options 를 안 주면 이 SDK는 기본이 "1번 시도하고 바로 포기"다
        # (소스 확인: retry_args(None) → stop_after_attempt(1)). 빈 HttpRetryOptions()
        # 를 주면 SDK 기본 정책이 켜진다 — 408·429·500·502·503·504 에러에 최대 5번,
        # 1→2→4→8초 지수 백오프. "지금 트래픽이 몰려 일시적으로 과부하" 라는
        # 503 UNAVAILABLE 이 바로 이 목록에 있다.
        client = genai_sdk.Client(
            api_key=key,
            http_options=genai_types.HttpOptions(retry_options=genai_types.HttpRetryOptions()),
        )
    else:
        raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")

    _ai_clients[provider] = client
    return client


async def _astream_ai(provider: str, model: str, system: Optional[str],
                      messages: list, *, max_tokens: int = AI_MAX_TOKENS) -> AsyncIterator[str]:
    """프로바이더 3종을 한 입구로 모은 스트리밍 호출. 텍스트 조각을 순서대로 낸다.

    보고서 생성(단발)과 챗봇(여러 차례)이 예전에는 `_call_ai` / `_call_ai_chat` 두
    벌로 갈라져 있었습니다. 프로바이더가 늘 때마다 두 곳을 같이 고쳐야 해서 한 곳으로
    합쳤습니다 — 보고서는 system 없이 user 한 통을 보내는 특수한 경우일 뿐입니다.

    **비스트리밍 응답에도 내부적으로는 스트리밍을 씁니다.** 두 가지 이유입니다.
    ① 긴 응답에서 HTTP 유휴 타임아웃에 걸리지 않습니다.
    ② 요즘 모델은 사고(thinking) 토큰이 max_tokens 를 함께 먹습니다. 한 번에 받으면
       사고가 길어진 날 본문이 잘려 "JSON 파싱 실패"로 떨어집니다(위 주석의 그 사고).
    """
    client = _ai_client(provider)

    if provider == "claude":
        kw = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            kw["system"] = system
        async with client.messages.stream(**kw) as stream:
            # text_stream 은 텍스트 블록만 흘려 줍니다. 최신 모델이 앞에 붙이는
            # ThinkingBlock 을 직접 걸러낼 필요가 없어졌습니다(예전 버그 자리).
            async for chunk in stream.text_stream:
                yield chunk
        return

    if provider == "openai":
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        stream = await client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=msgs, stream=True,
        )
        async for ev in stream:
            if not ev.choices:
                continue
            piece = ev.choices[0].delta.content
            if piece:
                yield piece
        return

    if provider == "gemini":
        from google.genai import types as gtypes

        contents = [gtypes.Content(role=("model" if x["role"] == "assistant" else "user"),
                                    parts=[gtypes.Part(text=x["content"])])
                    for x in messages]
        cfg = gtypes.GenerateContentConfig(
            system_instruction=system or None, max_output_tokens=max_tokens,
            thinking_config=gtypes.ThinkingConfig(thinking_budget=AI_THINKING_BUDGET),
        )
        stream = await client.aio.models.generate_content_stream(
            model=model, contents=contents, config=cfg,
        )
        async for ev in stream:
            if ev.text:
                yield ev.text
        return

    raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")


async def _acall_ai(provider: str, model: str, system: Optional[str],
                    messages: list, *, max_tokens: int = AI_MAX_TOKENS) -> str:
    """스트리밍을 다 모아 한 문자열로. 슬롯을 잡고 전체 시간도 제한한다."""
    async with _ai_slot():
        parts: list = []

        async def _drain():
            async for piece in _astream_ai(provider, model, system, messages,
                                           max_tokens=max_tokens):
                parts.append(piece)

        try:
            await asyncio.wait_for(_drain(), AI_REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise HTTPException(504, f"AI 응답이 {AI_REQUEST_TIMEOUT_S:g}초 안에 오지 않았습니다.")
        return "".join(parts)


def _call_ai(provider: str, model: str, prompt: str) -> str:
    """동기 진입점 — scripts/test_ai.py 가 이 이름으로 씁니다. 서버는 쓰지 않습니다."""
    return asyncio.run(_acall_ai(provider, model, None,
                                 [{"role": "user", "content": prompt}]))


class ReportRequest(BaseModel):
    period: str = "am"
    provider: str = "auto"      # auto | claude | openai | gemini
    model: Optional[str] = None  # None → 프로바이더 기본 모델
    format: str = "sections"
    tone: str = "공문"
    sections: list = ["summary", "status", "problem", "plan", "effect", "next"]
    context: dict = {}


@app.post("/api/v1/reports/draft")
async def draft_report(req: ReportRequest):
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
- 고수요·저공급(need) 격자: {kpi.get('needCells', '미제공')}개 / 전체 {kpi.get('totalCells', DATA['meta']['grid']['cellCount'])}개
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
        text = (await _acall_ai(provider, model, None,
                                [{"role": "user", "content": prompt}])).strip()
        # 코드펜스 벗기기 + 산문 뒤에 붙은 JSON 건지기는 _extract_json 한 곳에 모았습니다
        # (챗봇에서 실제로 그 두 경우를 다 만났습니다 — 그 함수 주석 참고).
        result = _extract_json(text)
        if not isinstance(result, dict):
            raise json.JSONDecodeError("JSON 객체를 찾지 못했습니다", text[:200], 0)
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
        if isinstance(e, AIBusy):
            detail = "지금 AI 요청이 몰려 있습니다. 잠시 후 다시 눌러 주세요"
        else:
            detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        fb["disclaimer"] = f"AI 호출에 실패해 규칙 기반 초안으로 대체했습니다 ({provider}: {detail}). " \
                           + fb["disclaimer"]
        return fb


# ═══════════════════════════════════════════════════════════════════════════════
#  10. POST /api/v1/chat — 도움 챗봇 · 보고서 채팅 수정
#
#  RAG 를 쓰지 않습니다. 챗봇이 알아야 할 개념 지식은 meta.json 안에 2,353자
#  (formula·assumptions·effects·cost·dataQuality)뿐이고, chat_kb.md 를 더해도
#  1만 자 안팎입니다 — 모델 컨텍스트에 통째로 들어갑니다. 코퍼스가 컨텍스트에
#  들어가는데 임베딩·벡터저장소·청킹을 붙일 이유가 없습니다.
#
#  데이터 질문에도 RAG 는 틀린 도구입니다. "향남읍에서 3번째로 심한 격자"는 의미가
#  비슷한 문장을 찾는 문제가 아니라 순위를 세는 문제입니다. 그래서 답에 필요한 것을
#  서버가 미리 추려 프롬프트에 싣습니다(_chat_pack).
# ═══════════════════════════════════════════════════════════════════════════════

CHAT_KB_PATH = ROOT / "server" / "chat_kb.md"
MAX_CHAT_CHARS = 2000      # 사용자 입력 한 번의 상한
MAX_CHAT_TURNS = 10        # 서버로 넘어오는 이력 상한 (프롬프트 폭주 방지)


def _extract_json(text: str):
    """모델 응답에서 JSON 객체 하나를 건져 냅니다. 못 건지면 None.

    ⚠️ json.loads(text) 만으로는 부족합니다. "JSON 만 응답하라"고 못박아도 모델이
       **산문을 먼저 쓰고 그 뒤에 JSON 을 덧붙이는** 경우가 실제로 나옵니다(실측 —
       답변 문장 + 빈 줄 + {"reply": …}). 그러면 파싱이 실패하고 사용자에게 프롬프트
       내부 형식이 그대로 노출됩니다.
       그래서 ① 코드펜스를 벗기고 ② 통째로 파싱해 보고 ③ 안 되면 중괄호 균형을 세어
       첫 번째 완결된 객체를 잘라 씁니다."""
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _region_digest(period: str) -> list:
    """읍면동별 한 줄 요약. "향남읍은 어때?" 류에 답하려면 Top10(시 전체)만으로는
    부족합니다. 29개 × 한 줄이라 2KB 안팎이고, 격자 786개를 통째로 넣는 것(356KB)과
    달리 프롬프트에 들어갑니다."""
    cells = DATA[f"grid_{period}"]["cells"]
    by: dict = {}
    for c in cells:
        r = by.setdefault(c["region"], {"n": 0, "need": 0, "drt": 0, "top": None})
        r["n"] += 1
        if c["quadrant"] == "need":
            r["need"] += 1
        if c["quadrant"] == "drt":
            r["drt"] += 1
        if r["top"] is None or c["priorityScore"] > r["top"]["priorityScore"]:
            r["top"] = c
    out = []
    for name in sorted(by, key=lambda k: -by[k]["need"]):
        r = by[name]
        t = r["top"]
        out.append({
            "region": name, "cells": r["n"], "needCells": r["need"], "drtCells": r["drt"],
            "topCell": t["id"], "topCellName": t["name"],
            "topMi": t["mi"], "topAction": ACTION_LABEL.get(t["action"], t["action"]),
        })
    return out


def _chat_pack(period: str) -> dict:
    """프롬프트에 실을 사실 묶음. **모델이 말하는 수치는 전부 여기서만 나와야 합니다.**

    ⚠️ meta 를 통째로 넣지 마세요. meta.map.regions 가 읍면동 경계 폴리곤 좌표라
       혼자 13.5만 자입니다(전체 meta 의 99%). 챗봇이 읽을 지식이 아닙니다."""
    meta = DATA["meta"]
    top = sorted([c for c in DATA[f"grid_{period}"]["cells"]
                  if c["quadrant"] in ("need", "drt")],
                 key=lambda c: c["priorityScore"], reverse=True)[:10]
    return {
        "개념": {k: meta[k] for k in
                 ("formula", "assumptions", "cost", "effects", "dataQuality", "grid")
                 if k in meta},
        "시간대": meta.get("periods"),
        "현재시간대": {"id": period, "name": PERIOD_NAME[period], "hours": PERIOD_HOURS[period]},
        "현재KPI": DATA[f"grid_{period}"]["kpi"],
        "우선순위Top10": [
            {"rank": i + 1, "cellId": c["id"], "name": c["name"], "region": c["region"],
             "mi": c["mi"], "demand": c["demand"], "supply": c["supply"],
             "coverage": c["coverage"], "action": ACTION_LABEL.get(c["action"], c["action"])}
            for i, c in enumerate(top)
        ],
        "읍면동요약": _region_digest(period),
        "규모": {"격자": len(DATA[f"grid_{period}"]["cells"]),
                 "정류장": len(DATA["stops"]["stops"]),
                 "노선": len(DATA["routes"]["routes"])},
    }


_CHAT_RULES = """당신은 화성시 버스 수요·공급 미스매칭 대시보드의 도우미입니다.

■ 반드시 지킬 것
- **아래 <사실> 에 없는 수치는 절대 말하지 마세요.** 모르면 "그 수치는 지금 화면
  데이터에 없습니다" 라고 답하고 어디를 보면 되는지 알려 주세요. 이 제품은 화면에
  나가는 모든 수치가 실측이어야 한다는 방침을 지킵니다 — 추정치를 지어내면 그 방침이
  깨집니다.
- 한국어로, 공무원이 읽을 문장으로 답합니다. 3~5문장으로 짧게. 목록이 필요하면 씁니다.
- 노선별 이용객수는 산출 불가입니다(승하차가 정류장 단위로만 있고 노선별로 안 나뉨).
  요청받으면 왜 못 하는지 설명하세요.

■ 응답 형식 — JSON 만, 마크다운 코드블록 없이
{"reply": "답변 문장", "action": {"type": "none"}}

■ action — 사용자가 무언가를 보거나 시키고 싶어 하면 **반드시** 채웁니다.
  {"type":"period","value":"am|day|pm|night"}       시간대 전환
  {"type":"layer","value":"mi|demand|supply|flow"}  지도 색 기준 전환
  {"type":"show","query":"향남읍"}                  읍면동·격자ID·버스번호·정류장으로 이동
  {"type":"recommend"}                              AI 추천 배치안 계산(시뮬레이션 화면 전용)
  {"type":"nav","page":"dashboard|simulation","query":"선택","after":{...선택, 도착 후 이어 할 action 1개}}
                                                     다른 화면으로 이동

  "보여줘" · "찾아줘" · "이동해줘" · "어디야?" · "~로 바꿔줘" · "눌러줘/계산해줘/추천해줘" 는
  전부 요청입니다. 말로 "했습니다" 라고만 하고 action 을 none 으로 두면 화면은 그대로라
  거짓말이 됩니다 — 하겠다고 말했으면 action 을 반드시 채우세요.
  반대로 단순한 설명 질문("MI가 뭐야?")에는 {"type":"none"} 입니다.

  **<지금 화면> 의 `이화면에서_가능` 목록에 없는 걸 요청받으면**, "화면 데이터에
  없습니다" 라고 답하지 마세요 — 그건 그 화면에 없을 뿐 서비스엔 있는 기능입니다.
  대신 nav 로 필요한 화면부터 옮기세요:
    - 지금 화면에서 바로 되면(목록에 있으면) 해당 action 하나만.
    - 다른 화면의 기능이면 nav 를 쓰고, 옮기자마자 이어서 할 일이 있으면 그 action 을
      nav.after 에 실으세요(예: 시뮬레이션 화면으로 옮기며 AI 추천도 같이 —
      {"type":"nav","page":"simulation","after":{"type":"recommend"}}).
    - nav.query 는 dashboard 로 갈 땐 읍면동·격자ID 등 검색어, simulation 으로 갈 땐
      <사실> 에 나온 실제 격자ID(예: "다사6707")만 쓰세요 — 지어내면 그 격자를 못 찾습니다.
    - 이미 그 화면이면 nav 를 쓰지 말고 그 화면의 action 을 바로 쓰세요."""

_CHAT_REPORT_RULES = """당신은 화성시 버스 대시보드의 보고서 편집기입니다.
사용자의 지시대로 **현재 초안**을 고쳐 씁니다.

■ 반드시 지킬 것
- **수치를 바꾸지 마세요.** 아래 <사실> 과 <현재 초안> 에 있는 숫자만 씁니다.
  강조·순서·어조·분량은 지시대로 바꾸되 숫자를 새로 만들거나 고치면 안 됩니다.
- 공문 어조를 유지합니다. 섹션 구조(key)는 그대로 두고 heading·body·bullets 만 고칩니다.
- 지시가 특정 섹션만 가리키면 그 섹션만 바꾸고 나머지는 그대로 돌려주세요.

■ 응답 형식 — JSON 만, 마크다운 코드블록 없이
{"reply": "무엇을 어떻게 고쳤는지 한두 문장",
 "action": {"type":"none"},
 "draft": {"sections":[{"key":"summary","heading":"…","body":"…","bullets":["…"]}]}}

draft.sections 는 **고친 전체 목록**입니다(안 고친 섹션도 그대로 포함)."""


#  프로바이더별 분기는 _astream_ai 한 곳으로 모았습니다. 예전에는 이 자리에 보고서용
#  _call_ai 와 거의 같은 코드가 한 벌 더 있었고, Gemini 모델을 추가할 때마다 두 곳을
#  같이 고쳐야 했습니다.


# ─── 스트리밍 중인 JSON 에서 reply 만 뽑아내기 ────────────────────────────────
#
#  모델은 {"reply": "...", "action": {...}} 형태로 답합니다. 그 원문을 그대로 흘리면
#  화면에 `{"reply": "안녕하` 가 찍힙니다. 그래서 서버가 조각을 모아 가며 reply 값이
#  **자란 만큼만** 잘라 내보냅니다. reply 가 첫 키라 사실상 첫 조각부터 흘러갑니다.
_JSON_ESC = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
             "n": "\n", "r": "\r", "t": "\t"}
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*", re.I)


class _ReplyExtractor:
    """조각을 넣으면 새로 드러난 reply 텍스트만 돌려준다."""

    def __init__(self):
        self.buf = ""
        self.pos = 0
        self.body = -1      # reply 문자열 본문이 시작하는 위치
        self.closed = False
        self.plain = False  # JSON 이 아니라 산문이 오는 경우

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        if self.closed:
            return ""
        if self.plain:
            out, self.pos = self.buf[self.pos:], len(self.buf)
            return out
        if self.body < 0:
            self._locate()
            if self.body < 0:
                return ""
            if self.plain:                       # _locate 가 산문으로 판정한 경우
                out, self.pos = self.buf[self.pos:], len(self.buf)
                return out

        out, b, n, i = [], self.buf, len(self.buf), self.pos
        while i < n:
            c = b[i]
            if c == '"':                         # 문자열 끝 — 여기서 멈춘다
                self.closed = True
                break
            if c != "\\":
                out.append(c)
                i += 1
                continue
            if i + 1 >= n:                       # 이스케이프가 아직 덜 왔다
                break
            e = b[i + 1]
            if e == "u":
                if i + 6 > n:                    # \uXXXX 네 자리가 아직 덜 왔다
                    break
                try:
                    out.append(chr(int(b[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
            else:
                out.append(_JSON_ESC.get(e, e))
                i += 2
        self.pos = i
        return "".join(out)

    def _locate(self):
        k = self.buf.find('"reply"')
        if k >= 0:
            j = self.buf.find(":", k + 7)
            if j < 0:
                return
            q = self.buf.find('"', j + 1)
            if q < 0:
                return
            self.body = self.pos = q + 1
            return
        # 200자를 봐도 못 찾았고 시작이 { 도 아니면 JSON 이 아니라고 본다.
        if len(self.buf) >= 200 and not _FENCE_RE.sub("", self.buf).lstrip().startswith("{"):
            self.plain = True
            self.body = self.pos = 0


def _sse(event: str, data: dict) -> str:
    """SSE 한 덩어리. json.dumps 가 줄바꿈을 이스케이프하므로 프레임이 깨지지 않는다."""
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


class ChatRequest(BaseModel):
    mode: str = "help"               # help | report
    period: str = "am"
    messages: list = []              # [{role:'user'|'assistant', content}] — 이력은 클라이언트 보관
    context: dict = {}               # 지금 화면 상태 {cellId, layer, routeId, …}
    draft: Optional[dict] = None     # mode=report 일 때 현재 초안
    provider: str = "auto"
    model: Optional[str] = None
    stream: bool = False             # true 면 text/event-stream 으로 흘려보낸다


def _chat_unavailable(reason: str) -> dict:
    """키가 없거나 호출이 실패했을 때. **500 을 던지지 않습니다** — 발표장에서 버튼이
    깨지지 않게 하는 것이 이 서버의 첫 번째 이유입니다
    (README §8, _fallback_report 와 같은 원리)."""
    return {"reply": reason, "action": {"type": "none"}, "ok": False}


_CHAT_BUSY = ("지금 AI 요청이 몰려 있습니다. 잠시 뒤 다시 보내 주세요.")


def _chat_result(text: str, provider: str, model: str, mode: str) -> dict:
    """모델이 낸 원문 → 화면이 쓰는 최종 응답. 스트리밍·비스트리밍이 같이 씁니다."""
    out = _extract_json(text)
    if not isinstance(out, dict) or not isinstance(out.get("reply"), str):
        # 형식은 틀렸어도 사람이 읽을 말은 왔을 수 있으니 통째로 버리지 않습니다.
        # 다만 JSON 조각이 화면에 노출되면 안 되므로 첫 중괄호 앞까지만 씁니다.
        plain = text.split("{", 1)[0].strip() or "AI 응답을 해석하지 못했습니다."
        return {"reply": plain[:1500], "action": {"type": "none"}, "ok": True,
                "provider": _PROVIDERS[provider]["label"], "model": model}

    act = out.get("action") or {"type": "none"}
    if not isinstance(act, dict) or act.get("type") not in (
        "period", "layer", "show", "recommend", "nav", "none"
    ):
        act = {"type": "none"}
    res = {"reply": out["reply"], "action": act, "ok": True,
           "provider": _PROVIDERS[provider]["label"], "model": model}
    if mode == "report" and isinstance(out.get("draft"), dict):
        res["draft"] = out["draft"]
    return res


def _sse_response(gen) -> StreamingResponse:
    """SSE 응답 껍데기.

    압축을 두 겹으로 막습니다. 이 앱에는 GZipMiddleware 가 걸려 있고, 조각이 압축
    버퍼에 고이면 '한 번에 뭉쳐서' 도착해 스트리밍을 붙인 의미가 사라집니다.
      · 지금 쓰는 Starlette(1.3.1)는 text/event-stream 을 이미 기본 제외합니다
        (DEFAULT_EXCLUDED_CONTENT_TYPES — 확인함).
      · 그래도 Content-Encoding 을 직접 박아 둡니다. 같은 응답자가 헤더에
        content-encoding 이 있으면 무조건 그대로 통과시키므로(IdentityResponder),
        Starlette 가 낮은 버전이거나 제외 목록이 바뀌어도 안전합니다.
    """
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Content-Encoding": "identity",
        "X-Accel-Buffering": "no",     # 리버스프록시(Caddy·nginx)가 버퍼링하지 않게
    })


async def _sse_once(payload: dict) -> AsyncIterator[str]:
    yield _sse("done", payload)


async def _chat_sse(provider: str, model: str, system: str, msgs: list, mode: str,
                    *, max_tokens: int = AI_MAX_TOKENS) -> AsyncIterator[str]:
    """조각이 오는 대로 reply 를 흘리고, 끝나면 최종 구조를 done 으로 한 번 더 보낸다.

    화면은 delta 로 글자를 채우다가 done 에서 action·draft 를 받습니다. 중간에 끊겨도
    done 을 못 받은 것으로 판정할 수 있어, 반쪽짜리 답을 완성본으로 오해하지 않습니다.
    """
    ex, raw = _ReplyExtractor(), []
    try:
        async with _ai_slot():
            deadline = time.monotonic() + AI_REQUEST_TIMEOUT_S
            async for piece in _astream_ai(provider, model, system, msgs,
                                           max_tokens=max_tokens):
                raw.append(piece)
                grown = ex.feed(piece)
                if grown:
                    yield _sse("delta", {"text": grown})
                if time.monotonic() > deadline:
                    raise asyncio.TimeoutError
    except AIBusy:
        yield _sse("done", _chat_unavailable(_CHAT_BUSY))
        return
    except asyncio.TimeoutError:
        yield _sse("done", _chat_unavailable(
            f"AI 응답이 {AI_REQUEST_TIMEOUT_S:g}초 안에 끝나지 않았습니다."))
        return
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        yield _sse("done", _chat_unavailable(f"AI 호출에 실패했습니다 ({provider}: {detail})."))
        return

    yield _sse("done", _chat_result("".join(raw).strip(), provider, model, mode))


@app.post("/api/v1/chat")
async def chat(req: ChatRequest):
    _chk_period(req.period)
    if req.mode not in ("help", "report"):
        raise HTTPException(400, "mode는 help 또는 report 여야 합니다.")

    msgs = [m for m in req.messages
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str) and m["content"].strip()]
    msgs = [{"role": m["role"], "content": m["content"][:MAX_CHAT_CHARS]}
            for m in msgs][-MAX_CHAT_TURNS * 2:]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "마지막 메시지는 user 여야 합니다.")

    provider = _detect_provider() if req.provider == "auto" else req.provider
    if provider is None:
        nokey = _chat_unavailable(
            "AI 키가 설정되어 있지 않아 채팅을 쓸 수 없습니다. .env 에 "
            "ANTHROPIC_API_KEY · OPENAI_API_KEY · GOOGLE_API_KEY 중 하나를 넣어 주세요."
        )
        # 스트리밍을 요청했으면 실패도 같은 형식으로 — 화면의 파서가 갈리지 않게.
        return _sse_response(_sse_once(nokey)) if req.stream else nokey
    if provider not in _PROVIDERS:
        raise HTTPException(400, f"provider는 {list(_PROVIDERS)} 중 하나여야 합니다.")
    model = req.model or _default_model(provider)

    kb = CHAT_KB_PATH.read_text("utf-8") if CHAT_KB_PATH.exists() else ""
    rules = _CHAT_REPORT_RULES if req.mode == "report" else _CHAT_RULES
    system = (
        rules
        + "\n\n<지식>\n" + kb
        + "\n\n<사실>\n" + json.dumps(_chat_pack(req.period), ensure_ascii=False)
        + "\n\n<지금 화면>\n" + json.dumps(req.context, ensure_ascii=False)
    )
    if req.mode == "report":
        system += "\n\n<현재 초안>\n" + json.dumps(req.draft or {}, ensure_ascii=False)

    # report 모드는 안 고친 섹션까지 포함해 draft.sections 전체를 되돌려줘야 한다
    # (_CHAT_REPORT_RULES) — 6개 섹션짜리 초안이면 1024로는 잘린다(실측: 57초 걸리고
    # JSON 파싱 실패로 떨어짐). 낮은 캡은 짧은 대화형 답만 오는 help 모드에만 쓴다.
    max_tokens = AI_CHAT_MAX_TOKENS if req.mode == "help" else AI_MAX_TOKENS

    if req.stream:
        return _sse_response(_chat_sse(provider, model, system, msgs, req.mode,
                                       max_tokens=max_tokens))

    try:
        text = (await _acall_ai(provider, model, system, msgs,
                                max_tokens=max_tokens) or "").strip()
    except AIBusy:
        return _chat_unavailable(_CHAT_BUSY)
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        return _chat_unavailable(f"AI 호출에 실패했습니다 ({provider}: {detail}).")

    return _json(_chat_result(text, provider, model, req.mode))
