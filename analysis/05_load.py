# -*- coding: utf-8 -*-
"""
정적 JSON 생성 + PostgreSQL 적재 (graceful fallback)

    python analysis/05_load.py

입력 (dataset_hwaseong/)
    grid_hwaseong.csv    격자 마스터 (셀 수는 grid_spec.json 참조)
    grid_metrics.csv     격자 × 시간대 (셀 수 × 4)
    grid_join.csv        격자 × 시간대 원재료
    stops_hwaseong.csv   화성시 정류장
    routes.csv           노선 목록
    route_stops.csv      노선 경유 정류장
    boarding_hwaseong.csv 일별 승하차
    flow_hourly.csv      시간대별 유동인구
    hwaseong_dong.geojson 읍면동 경계

산출 (server/static/)
    meta.json            메타·설정 (API §3.1)
    grid_{am,day,pm,night}.json  격자 데이터 (API §3.2)
    stops.json           정류장 목록 (API §3.3)
    routes.json          노선 목록 (API §3.4)
    profiles.json        시간대별 승하차 프로파일 (API §3.5)
"""
import json
import math
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")   # Windows 콘솔 한글 깨짐 방지
sys.stderr.reconfigure(encoding="utf-8")
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import od_curve                                    # noqa: E402  (경로 삽입 후)

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"
STATIC_DIR = ROOT / "server" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

PERIODS = ["am", "day", "pm", "night"]

# 일 버스통행 환산 — 인구 × (전수단 원단위 2.5 × 버스분담률 0.10).
#
# 목업의 TRIP_COEF=3200 은 정규화값(0~1)에 곱하는 임의 계수라 실데이터 근거가 없다.
# 그대로 쓰면 사각지대 잠재수요가 104만 통행/일이 나오는데, 화성시 실제 버스 승차가
# 일 169,026 이라 6.2배다. "사각지대 하나가 시 전체 이용의 6배"는 발표에서 못 버틴다.
# 인구 기준으로 바꾸면 잠재/실현 = 2.18배로 억압수요가 설명 가능한 범위에 들어온다.
# ⚠️ 두 계수 모두 가정값이다. meta.assumptions 에 confirmed:false 로 실어 보낸다.
TRIP_RATE, BUS_SHARE = 2.5, 0.10
BUS_TRIP_RATE = TRIP_RATE * BUS_SHARE
MI_THRESHOLDS = [-1.2, -0.7, -0.25, 0.25, 0.7, 1.2]
TODAY = str(date.today())
HOURS_LIST = list(range(5, 24))  # [5, 6, ..., 23]

QUADRANT_LABEL = {
    "need": "고수요·저공급",
    "over": "저수요·고공급",
    "drt":  "수요응답형",
    "ok":   "적정",
    "mid":  "균형권",
}
ACTION_LABEL = {
    "NEW_STOP": "신설",
    "ADD_FREQ": "증차",
    "DRT":      "똑버스",
}


# ── 유틸 ────────────────────────────────────────────────────────────────────
def safe_float(v, decimals=None):
    if v is None:
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    if decimals is not None:
        return round(f, decimals)
    return f


def safe_int(v, default=0):
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f):
            return default
        return int(f)
    except (TypeError, ValueError):
        return default


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))


# ── 1. 데이터 적재 ──────────────────────────────────────────────────────────
print("데이터 읽는 중...")

gh    = pd.read_csv(D_DIR / "grid_hwaseong.csv")

# 격자 스펙 (02_grid 사이드카). 없으면 1km 초기 산출물로 간주합니다.
# 스펙이 있으면 cellCount 를 CSV 와 대조합니다 — 02_grid 가 중간에 실패해
# CSV 와 스펙이 갈라진 상태로 하류가 도는 것을 여기서 막습니다.
try:
    _spec = json.loads((D_DIR / "grid_spec.json").read_text(encoding="utf-8"))
    GRID_SIZE_M = int(_spec["sizeMeters"])
    if int(_spec.get("cellCount", len(gh))) != len(gh):
        sys.exit(f"grid_spec.json({_spec['cellCount']}칸)과 grid_hwaseong.csv({len(gh)}칸)가 "
                 "다릅니다 — 02_grid.py 를 다시 실행하세요.")
    print(f"격자 스펙: {GRID_SIZE_M}m · {len(gh)}칸 (grid_spec.json)")
except FileNotFoundError:
    GRID_SIZE_M = 1000
    print("격자 스펙 없음 — 1km 초기 산출물로 간주합니다.")
except (KeyError, ValueError, TypeError) as e:
    sys.exit(f"grid_spec.json 이 손상됐습니다({e}) — 02_grid.py 를 다시 실행하세요.")
gm    = pd.read_csv(D_DIR / "grid_metrics.csv")
gj    = pd.read_csv(D_DIR / "grid_join.csv")
stops = pd.read_csv(D_DIR / "stops_hwaseong.csv")
def _plus(name):
    """09_augment_routes 가 만든 보강본이 있으면 그걸 읽는다 (03_join 과 동일 규칙).
       화성 면허가 아니라 빠졌던 노선을 채운 파일이다. 없으면 원본으로 돌아간다."""
    stem, ext = name.rsplit(".", 1)
    p = D_DIR / f"{stem}_plus.{ext}"
    if p.exists():
        print(f"  [보강] {p.name} 사용")
        return pd.read_csv(p)
    return pd.read_csv(D_DIR / name)


routes_df = _plus("routes.csv")
rs    = _plus("route_stops.csv")
boarding = pd.read_csv(D_DIR / "boarding_hwaseong.csv")
flow  = pd.read_csv(D_DIR / "flow_hourly.csv")

with open(D_DIR / "hwaseong_dong.geojson", encoding="utf-8") as fh:
    geojson = json.load(fh)

# 정류장: stop_id 없는 행 제거
stops = stops.dropna(subset=["stop_id"]).copy()

# grid_id → region_kind 매핑 (stops에 region_kind 열 없음 → grid에서 채움)
grid_kind = gh.set_index("grid_id")["region_kind"].to_dict()
stops["region_kind"] = stops["grid_id"].map(grid_kind)

# ── 2. 시간대별 유동인구 배율 계산 ─────────────────────────────────────────
print("시간배율 계산 중...")

age_cols = [c for c in flow.columns
            if c not in ("시군구코드", "시군구명", "시간코드", "외국인구분", "연도_월_일")]
flow["_row_sum"] = flow[age_cols].sum(axis=1)
hour_agg = flow.groupby("시간코드")["_row_sum"].sum()

grand = float(hour_agg.sum())
raw_ratio = {}
for h in range(24):
    raw_ratio[h] = float(hour_agg.get(h, 0.0)) / grand if grand > 0 else 0.0

# 0–4시 → 0, 5–23시로 재정규화
for h in range(5):
    raw_ratio[h] = 0.0
ratio_sum_5_23 = sum(raw_ratio[h] for h in range(5, 24))
hourly_ratio = {}
for h in range(24):
    if h >= 5 and ratio_sum_5_23 > 0:
        hourly_ratio[h] = raw_ratio[h] / ratio_sum_5_23
    else:
        hourly_ratio[h] = 0.0

# 승하차를 시간에 나누는 배율은 교통카드 OD 실측으로 바꾼다 (파일이 있을 때만).
# 유동인구는 '거기 사람이 있다'를 재는 값이라 버스의 출퇴근 첨두를 못 잡는다 —
# 실측과 상관계수 0.42, 출근(07-09)은 7.7% 대 18.4% 로 2.4배 어긋난다.
# 모델의 승차량도 같은 실측으로 교정하므로(03_join [4-1]) 같은 B 를 두 곳이
# 서로 다른 자로 나누지 않는다. 잠재수요는 이 배율을 쓰지 않는다.
HOURLY_SRC = "연령가중 유동인구 시간배율"
FLOW_CURVE = [hourly_ratio[h] for h in HOURS_LIST]
OD_CITY_CURVE, OD_EMD_CURVE = od_curve.hourly_by_emd(D_DIR, HOURS_LIST)
ARS2EMD = od_curve.load_stop_emd(D_DIR)
if OD_CITY_CURVE:
    HOURLY_SRC = "교통카드 OD 15분단위 실측 시간분포(법정동별)"
    print(f"  시간배율: {HOURLY_SRC} — 법정동 {len(OD_EMD_CURVE)}개 곡선, "
          f"나머지는 시 전체 곡선")
else:
    print("  시간배율: od_quarter.csv 없음 — 유동인구 기준 유지")


def stop_curve(ars_val):
    """정류장이 속한 법정동의 시간곡선. 없으면 시 전체, OD 자체가 없으면 유동인구."""
    if not OD_CITY_CURVE:
        return FLOW_CURVE
    a = str(ars_val or "").strip()
    if a.endswith(".0"):
        a = a[:-2]
    a = a.lstrip("0")
    return OD_EMD_CURVE.get(ARS2EMD.get(a, ""), OD_CITY_CURVE)

# ── 3. 정류장 ARS → 일평균 승하차 집계 (boarding_hwaseong) ─────────────────
print("승하차 집계 중...")

boarding = boarding.copy()
boarding["_ars_str"] = (
    boarding["정류소번호"]
    .dropna()
    .astype(float)
    .astype(int)
    .astype(str)
)
# NaN 제거 — 아래 [11] 의 승하차 매칭률 산출에만 쓴다.
#
# 예전에는 여기서 원본을 ARS 로 다시 집계해 프로파일 카드의 승하차를 만들었다.
# 그런데 03_join 은 정류소ID·초승(환승 제외)·평일 기준이고 이쪽은 ARS·승차합계·
# 전체요일 기준이라, 같은 정류장이 화면마다 다른 값을 냈다 — 시 전체로 재면
# 171,031 대 151,978 로 12.5% 어긋났다. 수요 정의를 바꾼 커밋(9c6bc88)이
# 03_join 만 고치고 여기를 빠뜨린 것이다.
# 수요 정의는 03_join 한 곳에서만 정한다. 여기서는 그 결과를 읽어 쓴다.
boarding_valid = boarding.dropna(subset=["_ars_str"])

# ── 4. route_stops → 화성시 정류장-노선 매핑 ────────────────────────────────
print("노선-정류장 매핑 중...")

stop_ids_set = set(stops["stop_id"].astype(str).values)

rs_hw = rs[rs["in_hwaseong"] == 1].copy()
rs_hw = rs_hw.dropna(subset=["ars_no"]).copy()
rs_hw["_sid"] = "41590-" + rs_hw["ars_no"].astype(int).astype(str)
rs_hw = rs_hw[rs_hw["_sid"].isin(stop_ids_set)].copy()

# stop_id → route_id 목록 (순서 무관, 중복 제거)
stop_to_routes = (
    rs_hw.groupby("_sid")["route_id"]
    .apply(lambda x: list(x.unique()))
    .to_dict()
)

# 노선 → 화성시 정류장 순서 목록 (seq 기준, 같은 stop 두 번 출현 시 첫번째만)
def build_route_stops(route_id):
    sub = rs_hw[rs_hw["route_id"] == route_id].sort_values("seq")
    seen = set()
    sids, coords = [], []
    for _, r in sub.iterrows():
        sid = r["_sid"]
        if sid not in seen:
            seen.add(sid)
            sids.append(sid)
            coords.append([safe_float(r["lon"], 6), safe_float(r["lat"], 6)])
    return sids, coords

# ── 5. 보조 함수 ─────────────────────────────────────────────────────────────

def stop_kind(n_routes, region_kind):
    if n_routes >= 5:
        return "hub"
    if isinstance(region_kind, str) and region_kind == "면":
        return "rural"
    return "res"


def route_type_str(row):
    rt = str(row.get("route_type") or "")
    rn = str(row.get("route_no") or "")
    if "마을버스" in rt:
        return "local"
    if "똑버스" in rt or "DRT" in rn.upper():
        return "drt"
    return "trunk"


# ── 6. stops.json ────────────────────────────────────────────────────────────
print("stops.json 생성 중...")

stops_out = []
for _, row in stops.iterrows():
    sid = str(row["stop_id"])
    ars = row["ars"]
    ars_str = str(int(float(ars))) if pd.notna(ars) else ""
    kind = stop_kind(safe_int(row["n_routes"]), row.get("region_kind"))
    routes_list = stop_to_routes.get(sid, [])

    stops_out.append({
        "id":     sid,
        "arsNo":  ars_str,
        "name":   str(row["name"]),
        "dong":   str(row["region"]),
        "lon":    safe_float(row["lon"], 6),
        "lat":    safe_float(row["lat"], 6),
        "kind":   kind,
        "routes": routes_list,
        # 지도에서 점 크기를 이 값에 비례시킨다. 전부 같은 크기로 그리면
        # 병점역과 시골 정류장이 똑같이 보여서 2,866개가 격자 색을 덮는다.
        "boardingsPerDay": safe_float(row["board_day"], 1),
    })

write_json(STATIC_DIR / "stops.json", {"stops": stops_out})
print(f"  stops.json: {len(stops_out)}개 정류장")

# ── 7. routes.json ───────────────────────────────────────────────────────────
print("routes.json 생성 중...")

valid_route_ids = set(rs_hw["route_id"].unique())

routes_out = []
for _, row in routes_df.iterrows():
    rid = str(row["route_id"])
    if rid not in valid_route_ids:
        continue
    sids, path = build_route_stops(rid)
    routes_out.append({
        "id":      rid,
        "name":    str(row["route_no"]),
        "type":    route_type_str(row.to_dict()),
        "stopIds": sids,
        "path":    path,
    })

write_json(STATIC_DIR / "routes.json", {"routes": routes_out})
print(f"  routes.json: {len(routes_out)}개 노선")

# ── 7-1. 격자 표시 이름 — 읍면동 안에서 방위로 세분 ──────────────────────────
#
# 예전에는 봉담읍 45칸이 전부 이름이 "봉담읍" 이라 지도·표·보고서에서 어느 칸인지
# 구분이 안 됐다. "봉담읍 북부" 처럼 방위를 붙이면 사람이 바로 짚을 수 있다.
#
# 왜 리(里) 가 아니라 방위인가
#   봉담읍은 읍이라 아래가 동이 아니라 리다("봉담 몇동"은 존재하지 않는다).
#   법정리 경계(BML_BADM_AS 194건)는 우리가 갖고 있지만 좌표가 없어 못 쓴다.
#   무엇보다 사람은 "동화리"가 어딘지 몰라도 "봉담읍 북부"는 지도에서 바로 찾는다.
#
# 칸이 적은 읍면동까지 쪼개면 오히려 장황해지므로 크기에 따라 단계를 둔다.
print("격자 표시 이름 생성 중...")

DIR8 = ["동", "북동", "북", "북서", "서", "남서", "남", "남동"]


def _build_cell_names(gh_df):
    """grid_id → 표시 이름. 읍면동 중심 기준 방위로 세분한다."""
    names = {}
    for region, sub in gh_df.groupby("region"):
        n = len(sub)
        if n <= 4:                       # 작은 동은 그대로 (동탄1동 5칸 등)
            for gid in sub["grid_id"]:
                names[gid] = region
            continue
        bins = 4 if n <= 20 else 8       # 클수록 잘게. 8방위면 106칸도 칸당 13개꼴
        cx, cy = sub["x_5179"].mean(), sub["y_5179"].mean()
        # 중심부 판정 반경 — 그 동 격자들의 중심거리 중앙값의 절반
        d = np.hypot(sub["x_5179"] - cx, sub["y_5179"] - cy)
        r_core = float(d.median()) * 0.5
        for gid, x, y in zip(sub["grid_id"], sub["x_5179"], sub["y_5179"]):
            dx, dy = x - cx, y - cy
            if math.hypot(dx, dy) <= r_core:
                names[gid] = region + " 중심"
                continue
            ang = math.degrees(math.atan2(dy, dx)) % 360
            if bins == 4:
                lab = ["동부", "북부", "서부", "남부"][int((ang + 45) % 360 // 90)]
            else:
                lab = DIR8[int((ang + 22.5) % 360 // 45)] + "부"
            names[gid] = region + " " + lab
    return names

# 이름을 유일하게 만들려고 격자번호를 붙여봤다가 뺐다. 화면이 이미 이름 옆에
# 격자 ID 를 따로 보여주고 있어서 "다사4813 | 봉담읍 북서부 4813" 처럼 번호가
# 두 번 나왔다. 유일성은 ID 가 담당하고 이름은 위치를 읽는 용도로 둔다.


CELL_NAME = _build_cell_names(gh)
_uniq = len(set(CELL_NAME.values()))
print(f"  {len(CELL_NAME):,}격자 → 표시 이름 {_uniq}종 (읍면동 {gh['region'].nunique()}개)")

# ── 8. grid_*.json (4개) ─────────────────────────────────────────────────────
print("grid_*.json 생성 중...")

gh_idx = gh.set_index("grid_id")

for period in PERIODS:
    gm_p = gm[gm["period"] == period].set_index("grid_id")
    gj_p = gj[gj["period"] == period].set_index("grid_id")

    q_vals = gm_p["quadrant"]
    need_cells  = int((q_vals == "need").sum())
    drt_cells   = int((q_vals == "drt").sum())
    over_cells  = int((q_vals == "over").sum())
    total_cells = len(gm_p)
    # needShare 는 need 만 센다. drt 를 같이 세면 화면에 "고수요·저공급 38개 ·
    # 전체 786개 중 12.8%" 처럼 뜨는데, 보는 사람이 38÷786 하면 4.8% 라 어긋난다.
    need_share = round(100.0 * need_cells / total_cells, 1) if total_cells else 0.0

    elder_arr = gm_p["elderly_ratio"].fillna(0.0).round(4)
    # 셀 직렬화(flowTripsPerDay)와 같은 값(셀별 반올림 정수)으로 합산한다.
    # 원시 실수를 합산 후 반올림하면 셀 합과 ±1 어긋나 화면 간 숫자가 갈린다.
    trips_arr = (gm_p["pop"].fillna(0.0) * BUS_TRIP_RATE).round().astype(int)

    # "사각지대 잠재수요" 는 need 격자만의 합이다. 전체를 더하면 라벨과 안 맞는다.
    is_need = (q_vals == "need")
    potential_trips = int(trips_arr[is_need].sum())
    elderly_trips   = int(round(float((trips_arr * elder_arr)[is_need].sum())))

    cells = []
    for gid in gm_p.index:
        r_gm = gm_p.loc[gid]
        r_gh = gh_idx.loc[gid] if gid in gh_idx.index else None
        r_gj = gj_p.loc[gid]  if gid in gj_p.index  else None

        lon = safe_float(r_gh["lon"] if r_gh is not None else 0, 5)
        lat = safe_float(r_gh["lat"] if r_gh is not None else 0, 5)

        nearest_stop_id = ""
        if r_gj is not None:
            nsid = r_gj.get("nearest_stop_id", "")
            if pd.notna(nsid):
                nearest_stop_id = str(nsid)

        quad   = str(r_gm["quadrant"])
        action = str(r_gm["action"])

        cells.append({
            "id":            gid,
            "name":          CELL_NAME.get(gid, str(r_gm["region"])),
            "region":        str(r_gm["region"]),
            "regionCode":    str(int(r_gm["region_code"])),
            "regionKind":    str(r_gm["region_kind"]),
            "lon":           lon,
            "lat":           lat,
            "demand":        int(round(safe_float(r_gm["D"]) * 100)),
            "supply":        int(round(safe_float(r_gm["S"]) * 100)),
            "zDemand":       round(safe_float(r_gm["zD"]), 4),
            "zSupply":       round(safe_float(r_gm["zS"]), 4),
            "mi":            round(safe_float(r_gm["mi"]), 4),
            "flow":          round(safe_float(r_gm["nf"]), 4),
            "flowTripsPerDay": int(round(safe_float(r_gm["pop"]) * BUS_TRIP_RATE)),
            "elderlyRatio":  round(safe_float(r_gm["elderly_ratio"]), 4),
            "coverage":      round(safe_float(r_gm["coverage"]), 4),
            "quadrant":      quad,
            "quadrantLabel": QUADRANT_LABEL.get(quad, quad),
            "action":        action,
            "actionLabel":   ACTION_LABEL.get(action, action),
            "priorityScore": round(safe_float(r_gm["priority"]), 4),
            "nearestStopId": nearest_stop_id,
            "adjusted":      False,
            "bins": {
                "mi":     safe_int(r_gm["bin_mi"], 3),
                "demand": safe_int(r_gm["bin_demand"], 0),
                "supply": safe_int(r_gm["bin_supply"], 0),
                "flow":   safe_int(r_gm["bin_flow"], 0),
            },
        })

    out = {
        "period": period,
        "scale": {"miThresholds": MI_THRESHOLDS},
        "kpi": {
            "needCells":          need_cells,
            "drtCells":           drt_cells,
            "overCells":          over_cells,
            "totalCells":         total_cells,
            "needShare":          need_share,
            "potentialTripsPerDay": potential_trips,
            "elderlyTripsPerDay": elderly_trips,
            # avgMi 는 뺐다. 기준통계가 시간대별 z 라 평균이 항상 ≈0 인 항등식이라
            # 화면에 띄울 정보가 없다(프론트 자체 분석에서도 폐기 권고).
        },
        "cells": cells,
    }

    write_json(STATIC_DIR / f"grid_{period}.json", out)
    print(f"  grid_{period}.json: {len(cells)}개 격자")

# ── 9. profiles.json ─────────────────────────────────────────────────────────
print("profiles.json 생성 중...")

# 피크 시간대 인덱스 (hours 7, 8, 17, 18)
peak_hours = {7, 8, 17, 18}
peak_indices = [i for i, h in enumerate(HOURS_LIST) if h in peak_hours]

profiles = {}
for _, row in stops.iterrows():
    # stop_id 가 프로파일의 키다. 없으면 API 로 꺼낼 방법이 없어 건너뛴다
    # (현재 292개 — 좌표만 있고 ID·ARS 가 둘 다 비어 있는 정류장).
    if pd.isna(row["stop_id"]):
        continue
    sid = str(row["stop_id"])

    kind = stop_kind(safe_int(row["n_routes"]), row.get("region_kind"))
    routes_list = stop_to_routes.get(sid, [])

    # 일평균 승하차 — 03_join 이 계산한 값(초승·평일·정류소ID 기준)을 그대로 쓴다.
    # 결측 대체분(board_imputed=1)도 여기로 함께 따라온다.
    board_total  = safe_float(row.get("board_day", 0))
    alight_total = safe_float(row.get("alight_day", 0))

    # 시간대별 승하차 — 정수로 반올림하면 안 된다.
    # 시간배율은 19개 시간(5~23시)에 나뉘므로 한 시간 몫이 평균 5% 안팎이다.
    # 일평균 5명인 정류장은 시간당 0.26명이라 round() 를 걸면 19칸이 전부 0 이 되고
    # 카드가 통째로 백지가 된다 — 기록이 없어서가 아니라 반올림으로 사라진 것이다.
    # 실측: 2,866개 중 1,263개가 백지였고 그중 1,015개는 실제 승차 기록이 있었다.
    # 일평균 자체가 소수인 값이므로 시간 몫도 소수로 두는 게 맞다.
    _cv = stop_curve(row.get("ars"))
    boardings_hr  = [round(board_total  * c, 2) for c in _cv]
    alightings_hr = [round(alight_total * c, 2) for c in _cv]

    # 피크 비율
    peak_board = sum(boardings_hr[i] for i in peak_indices)
    peak_share = round(100.0 * peak_board / board_total, 1) if board_total > 0 else 0.0

    profiles[sid] = {
        "stopId":             sid,
        "stopName":           str(row["name"]),
        "kind":               kind,
        "routes":             routes_list,
        "isEstimated":        True,
        "estimationMethod":   f"일자별 승하차를 {HOURLY_SRC}로 안분",
        "hours":              HOURS_LIST,
        "boardings":          boardings_hr,
        "alightings":         alightings_hr,
        "summary": {
            # 소수 한 자리까지 남긴다. int 로 자르면 일평균 0.4명인 정류장이
            # '일 승차 0명' 이 되어 기록이 없는 정류장과 구분되지 않는다.
            "boardingsPerDay":  round(board_total, 1),
            "alightingsPerDay": round(alight_total, 1),
            "peakSharePct":     peak_share,
        },
    }

write_json(STATIC_DIR / "profiles.json", profiles)
print(f"  profiles.json: {len(profiles)}개 정류장")

# ── 10. meta.json ────────────────────────────────────────────────────────────
print("meta.json 생성 중...")

# bbox from grid_hwaseong
lon_min = float(gh["lon"].min())
lon_max = float(gh["lon"].max())
lat_min = float(gh["lat"].min())
lat_max = float(gh["lat"].max())

# 읍면동 경계 (GeoJSON → regions 목록)
def extract_rings(geometry):
    """Polygon / MultiPolygon 모두 처리. 각 외곽 링([[lon,lat], ...]) 목록 반환."""
    gt = geometry["type"]
    coords = geometry["coordinates"]
    if gt == "Polygon":
        # coords = [outer_ring, hole1, ...]
        return [coords[0]]
    if gt == "MultiPolygon":
        # coords = [polygon1, polygon2, ...], polygon = [outer_ring, ...]
        return [poly[0] for poly in coords]
    return []


regions = []
for feat in geojson["features"]:
    props = feat["properties"]
    outer_rings = extract_rings(feat["geometry"])

    all_lons = [pt[0] for ring in outer_rings for pt in ring]
    all_lats = [pt[1] for ring in outer_rings for pt in ring]

    centroid = [
        round(sum(all_lons) / len(all_lons), 6),
        round(sum(all_lats) / len(all_lats), 6),
    ]
    bbox_r = [
        round(min(all_lons), 6), round(min(all_lats), 6),
        round(max(all_lons), 6), round(max(all_lats), 6),
    ]
    rings = [[[round(pt[0], 6), round(pt[1], 6)] for pt in ring] for ring in outer_rings]
    regions.append({
        "code":     props["code"],
        "name":     props["name"],
        "kind":     props["kind"],
        "centroid": centroid,
        "bbox":     bbox_r,
        "rings":    rings,
    })

# 데이터 품질 통계
boarding_dates = boarding["승하차일자"].dropna()
if len(boarding_dates):
    d0 = str(int(boarding_dates.min()))
    d1 = str(int(boarding_dates.max()))
    date_range_start = f"{d0[:4]}-{d0[4:6]}-{d0[6:]}"
    date_range_end   = f"{d1[:4]}-{d1[4:6]}-{d1[6:]}"
    n_dates = int(boarding_dates.nunique())
else:
    date_range_start = date_range_end = ""
    n_dates = 0

board_ars_set = set(boarding_valid["_ars_str"].unique())
stop_ars_set  = set(stops["ars"].dropna().astype(float).astype(int).astype(str).unique())
match_count   = len(board_ars_set & stop_ars_set)
boarding_match_rate = round(match_count / len(stop_ars_set), 3) if stop_ars_set else 0.0

meta = {
    "region":    "화성시",
    "updatedAt": TODAY,
    "isMockData": False,
    "periods": [
        {"id": "am",    "name": "출근", "label": "07–09", "hours": [7, 9]},
        {"id": "day",   "name": "낮",   "label": "09–17", "hours": [9, 17]},
        {"id": "pm",    "name": "퇴근", "label": "17–19", "hours": [17, 19]},
        {"id": "night", "name": "심야", "label": "22–24", "hours": [22, 24]},
    ],
    "grid": {
        # 격자 크기는 02_grid 가 남긴 grid_spec.json 에서 읽습니다 (하드코딩 금지 —
        # 500m 전환 시 여기와 프론트 표기가 같이 틀어지던 것을 막습니다).
        "sizeMeters":        GRID_SIZE_M,
        "analysisCellCount": int(len(gh)),
        "displaySizeMeters": GRID_SIZE_M,
        "cellCount":         int(len(gh)),
        "crs":               "EPSG:4326",
        "bbox": [
            round(lon_min, 6), round(lat_min, 6),
            round(lon_max, 6), round(lat_max, 6),
        ],
    },
    "dataQuality": {
        "boardingMatchRate":  boarding_match_rate,
        "gridCount":          int(len(gh)),
        "stopCount":          int(len(stops)),
        "routeCount":         int(len(routes_df)),
        "boardingDateRange":  {"start": date_range_start, "end": date_range_end},
        "boardingDateCount":  n_dates,
    },
    "map": {
        "viewBox":        [0, 0, 960, 640],
        "boundarySource": "SGIS 통계지리정보서비스 읍면동 경계 (bnd_dong_00_2025_2Q)",
        "regions":        regions,
        "scaleBar":       {"km": 5},
    },
    "cost": {
        "stop": {
            "krw":      42000000,
            "basis":    "capital",
            "lifeYears": 10,
            "annualKrw": 4200000,
        },
        "drt": {
            "krw":      180000000,
            "basis":    "operating",
            "lifeYears": 1,
            "annualKrw": 180000000,
        },
        "freq": {
            "krw":      95000000,
            "basis":    "operating",
            "lifeYears": 1,
            "annualKrw": 95000000,
        },
        "defaultBudget": 3000000000,
    },
    # 어느 수치가 실측이고 어느 수치가 추정인지.
    #
    # ⚠️ 프론트 목이 "교통카드빅데이터(STCIS)" 와 "통신사 유동인구" 로 적어뒀는데
    #    둘 다 우리가 안 쓰는 데이터다. STCIS 는 신청 리드타임 대비 이득이 없어
    #    통신사 유동인구는 SKT 가 제공 불가 회신을 보냈고(README §9), STCIS 는
    #    일별 승하차가 아니라 시간분포(교통카드 OD)로만 쓴다(README §3).
    #    심사에서 출처를 물었을 때 답할 수 없는 게 제일 위험하므로 실제 출처를 쓴다.
    "dataQuality": {
        "boardingDaily": {
            "level": "observed", "label": "일별 승하차",
            "source": "경기데이터드림 정류소별 승하차 인원 집계 (2025-12~2026-03)",
        },
        "boardingHourly": {
            "level": "estimated", "label": "시간대별 승하차",
            "method": f"일자별 승하차를 {HOURLY_SRC}로 안분",
            "note": ("원자료에 시간대 정보가 없습니다. "
                     + ("시간분포는 교통카드 OD(15분단위) 실측을 씁니다."
                        if "OD" in HOURLY_SRC else
                        "시간분포는 유동인구 추정입니다.")),
        },
        "flowHourly": {
            "level": "observed", "label": "시간대별 유동인구",
            "source": "경기도 분석갤러리 유동인구(화성시) · 2023-12~2024-01",
            "note": "승하차와 약 2년 시차가 있어 시간배율로만 사용합니다.",
        },
        "headway": {
            "level": "observed", "label": "배차간격",
            "source": "경기도 버스노선 조회 API (peekAlloc/nPeekAlloc/nightAlloc)",
        },
        "boundary": {
            "level": "observed", "label": "행정경계",
            "source": "SGIS 통계지리정보서비스 읍면동 경계 (bnd_dong_00_2025_2Q)",
        },
    },
    # 근거가 없는 값들. 사업비와 같은 성격이라 화면에 가정임을 표시해야 한다.
    "assumptions": {
        "busTripRate": {
            "value": BUS_TRIP_RATE, "confirmed": False,
            "note": f"1인 1일 버스통행 = 전수단 원단위 {TRIP_RATE} × 버스분담률 {BUS_SHARE}",
        },
        "minFreqPerHour": {
            "value": 2.0, "confirmed": False,
            "note": "적정·공급과잉 판정의 절대 하한. 야간 상대평가 오라벨 방지",
        },
    },
    "formula": {
        "demand":               "0.5·norm_board + 0.5·norm_potential",
        "supply":               "0.78·norm_freq + 0.22·coverage",
        "mi":                   "(zD − zS) · (D/dRef)^0.65",
        "dampExp":              0.65,
        "wFreq":                0.78,
        "wCov":                 0.22,
        "eldCoef":              1.6,
        "coverageThresholdM":   600,
        "needMiThreshold":      0.75,
    },
    "effects": [
        {
            "type":          "stop",
            "label":         "정류장 신설",
            "icon":          "●",
            "radiusKm":      2.0,
            "unitKrw":       42000000,
            "annualKrw":     4200000,
            "basis":         "capital",
            "lifeYears":     10,
            "coverageRange": [0.15, 0.50],
        },
        {
            "type":          "drt",
            "label":         "똑버스 배치",
            "icon":          "◆",
            "radiusKm":      3.0,
            "unitKrw":       180000000,
            "annualKrw":     180000000,
            "basis":         "operating",
            "lifeYears":     1,
            "coverageRange": [0, 0.15],
        },
        {
            "type":          "freq",
            "label":         "배차 증편",
            "icon":          "▲",
            "radiusKm":      2.4,
            "unitKrw":       95000000,
            "annualKrw":     95000000,
            "basis":         "operating",
            "lifeYears":     1,
            "coverageRange": [0.50, 1.0],
        },
    ],
}

write_json(STATIC_DIR / "meta.json", meta)
print("  meta.json 저장 완료")

# ── 10-1. priorities_{period}.json (API §3.3) ────────────────────────────────
print("priorities_{period}.json 생성 중...")


def priority_reason(r):
    """사람이 읽을 문장. 화면과 AI 보고서에 그대로 인용된다."""
    d, s = int(round(safe_float(r["D"]) * 100)), int(round(safe_float(r["S"]) * 100))
    m = int(safe_float(r["nearest_stop_m"]))
    act = str(r["action"])
    if act == "NEW_STOP":
        why = f"가장 가까운 정류장까지 {m}m 로 도보권 밖"
    elif act == "DRT":
        why = "노선 자체가 닿지 않아 고정노선보다 수요응답형이 적합"
    else:
        why = f"정류장은 도보권({m}m)이나 운행이 부족"
    e = safe_float(r["elderly_ratio"])
    return f"수요지수 {d} 대비 공급지수 {s}, {why}" + (f", 고령 인구 비중 {e:.0%}" if e >= 0.2 else "")


# grid_metrics.csv 에 nearest_stop_id/m 가 이미 들어 있다(04_model.py 에서 실음).
# gj 와 다시 조인하면 접미사가 붙어 KeyError 가 난다.
for period in PERIODS:
    top = (gm[(gm["period"] == period) & (gm["priority"] > 0)]
           .nlargest(10, "priority"))
    items = []
    for rank, (_, r) in enumerate(top.iterrows(), 1):
        act = str(r["action"])
        items.append({
            "rank": rank, "cellId": str(r["grid_id"]),
            "name": CELL_NAME.get(str(r["grid_id"]), str(r["region"])),
            "mi": round(safe_float(r["mi"]), 3),
            "priorityScore": round(safe_float(r["priority"]), 4),
            "demand": int(round(safe_float(r["D"]) * 100)),
            "supply": int(round(safe_float(r["S"]) * 100)),
            "flowTripsPerDay": int(round(safe_float(r["pop"]) * BUS_TRIP_RATE)),
            "elderlyRatio": round(safe_float(r["elderly_ratio"]), 4),
            "coverage": round(safe_float(r["coverage"]), 4),
            "action": act, "actionLabel": ACTION_LABEL.get(act, act),
            "nearestStopId": str(r["nearest_stop_id"]) if pd.notna(r["nearest_stop_id"]) else "",
            "reason": priority_reason(r),
        })
    write_json(STATIC_DIR / f"priorities_{period}.json", {"period": period, "items": items})
    print(f"  priorities_{period}.json: {len(items)}건")

# ── 11. PostgreSQL 적재 (graceful fallback) ──────────────────────────────────
print("PostgreSQL 적재 시도 중...")
try:
    import psycopg2  # noqa: F401 — import 실패 시 아래 except로 이동

    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/hwaseong",
    )
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # batch_grid
    cur.execute("""
        CREATE TABLE IF NOT EXISTS batch_grid (
            grid_id       TEXT PRIMARY KEY,
            lon           DOUBLE PRECISION,
            lat           DOUBLE PRECISION,
            x_5179        DOUBLE PRECISION,
            y_5179        DOUBLE PRECISION,
            region_code   TEXT,
            region        TEXT,
            region_kind   TEXT,
            pop           INTEGER,
            elderly       INTEGER,
            elderly_ratio DOUBLE PRECISION,
            workers       INTEGER
        )
    """)
    cur.execute("DELETE FROM batch_grid")
    for _, row in gh.iterrows():
        cur.execute(
            """INSERT INTO batch_grid VALUES
               (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                str(row["grid_id"]),
                safe_float(row["lon"]),
                safe_float(row["lat"]),
                safe_float(row["x_5179"]),
                safe_float(row["y_5179"]),
                str(int(row["region_code"])) if pd.notna(row.get("region_code")) else None,
                str(row["region"]),
                str(row["region_kind"]),
                safe_int(row.get("pop")),
                safe_int(row.get("elderly")),
                safe_float(row.get("elderly_ratio")),
                safe_int(row.get("workers")),
            ),
        )

    # batch_grid_metrics
    cur.execute("""
        CREATE TABLE IF NOT EXISTS batch_grid_metrics (
            grid_id    TEXT,
            period     TEXT,
            d          DOUBLE PRECISION,
            s          DOUBLE PRECISION,
            zd         DOUBLE PRECISION,
            zs         DOUBLE PRECISION,
            mi         DOUBLE PRECISION,
            quadrant   TEXT,
            priority   DOUBLE PRECISION,
            bin_mi     INTEGER,
            bin_demand INTEGER,
            bin_supply INTEGER,
            bin_flow   INTEGER,
            coverage   DOUBLE PRECISION,
            freq       DOUBLE PRECISION,
            nf         DOUBLE PRECISION,
            action     TEXT,
            PRIMARY KEY (grid_id, period)
        )
    """)
    cur.execute("DELETE FROM batch_grid_metrics")
    for _, row in gm.iterrows():
        cur.execute(
            """INSERT INTO batch_grid_metrics VALUES
               (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                str(row["grid_id"]),
                str(row["period"]),
                safe_float(row["D"]),
                safe_float(row["S"]),
                safe_float(row["zD"]),
                safe_float(row["zS"]),
                safe_float(row["mi"]),
                str(row["quadrant"]),
                safe_float(row["priority"]),
                safe_int(row["bin_mi"]),
                safe_int(row["bin_demand"]),
                safe_int(row["bin_supply"]),
                safe_int(row["bin_flow"]),
                safe_float(row["coverage"]),
                safe_float(row["freq"]),
                safe_float(row["nf"]),
                str(row["action"]),
            ),
        )

    # batch_run 기록
    cur.execute("""
        CREATE TABLE IF NOT EXISTS batch_run (
            id         SERIAL PRIMARY KEY,
            run_at     TIMESTAMPTZ DEFAULT NOW(),
            grid_count INTEGER,
            stop_count INTEGER,
            route_count INTEGER,
            note       TEXT
        )
    """)
    cur.execute(
        "INSERT INTO batch_run (grid_count, stop_count, route_count, note) VALUES (%s,%s,%s,%s)",
        (int(len(gh)), int(len(stops)), int(len(routes_df)), "05_load.py 자동 적재"),
    )

    conn.commit()
    cur.close()
    conn.close()
    print("  PostgreSQL 적재 완료")

except Exception as exc:
    print(f"  PostgreSQL 건너뜀 (DB 미연결): {exc}")

# ── 12. 완료 메시지 ──────────────────────────────────────────────────────────
print(
    f"\n저장 완료: meta.json, grid_*.json (4개), "
    f"stops.json ({len(stops_out)}개), "
    f"routes.json ({len(routes_out)}개), "
    f"profiles.json ({len(profiles)}개)"
)
