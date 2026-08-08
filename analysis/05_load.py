# -*- coding: utf-8 -*-
"""
정적 JSON 생성 + PostgreSQL 적재 (graceful fallback)

    python analysis/05_load.py

입력 (dataset_hwaseong/)
    grid_hwaseong.csv    격자 마스터 (786개)
    grid_metrics.csv     격자 × 시간대 (786×4)
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
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"
STATIC_DIR = ROOT / "server" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

PERIODS = ["am", "day", "pm", "night"]
TRIP_COEF = 3200
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
gm    = pd.read_csv(D_DIR / "grid_metrics.csv")
gj    = pd.read_csv(D_DIR / "grid_join.csv")
stops = pd.read_csv(D_DIR / "stops_hwaseong.csv")
routes_df = pd.read_csv(D_DIR / "routes.csv")
rs    = pd.read_csv(D_DIR / "route_stops.csv")
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
# NaN 제거 후 그룹 집계
boarding_valid = boarding.dropna(subset=["_ars_str"])
board_agg = (
    boarding_valid
    .groupby("_ars_str")
    .agg(
        board_sum=("승차합계", "sum"),
        alight_sum=("하차", "sum"),
        n_dates=("승하차일자", "nunique"),
    )
)
board_agg["board_day"]  = board_agg["board_sum"]  / board_agg["n_dates"]
board_agg["alight_day"] = board_agg["alight_sum"] / board_agg["n_dates"]

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
    need_share  = round(100.0 * (need_cells + drt_cells) / total_cells, 1) if total_cells else 0.0

    nf_arr     = gm_p["nf"].fillna(0.0)
    elder_arr  = gm_p["elderly_ratio"].fillna(0.0)
    mi_arr     = gm_p["mi"].fillna(0.0)

    potential_trips = int(round(float((nf_arr * TRIP_COEF).sum())))
    elderly_trips   = int(round(float((nf_arr * TRIP_COEF * elder_arr).sum())))
    avg_mi          = round(float(mi_arr.mean()), 3)

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
            "name":          str(r_gm["region"]),
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
            "flowTripsPerDay": int(round(safe_float(r_gm["nf"]) * TRIP_COEF)),
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
        "scale": {
            "miThresholds": MI_THRESHOLDS,
            "tripCoef":     TRIP_COEF,
        },
        "kpi": {
            "needCells":          need_cells,
            "drtCells":           drt_cells,
            "overCells":          over_cells,
            "totalCells":         total_cells,
            "needShare":          need_share,
            "potentialTripsPerDay": potential_trips,
            "elderlyTripsPerDay": elderly_trips,
            "avgMi":              avg_mi,
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
    sid = str(row["stop_id"])
    ars = row["ars"]
    if pd.isna(ars):
        continue
    ars_str = str(int(float(ars)))

    kind = stop_kind(safe_int(row["n_routes"]), row.get("region_kind"))
    routes_list = stop_to_routes.get(sid, [])

    # 일평균 승하차: boarding_hwaseong 우선, 없으면 stops_hwaseong fallback
    if ars_str in board_agg.index:
        bd_row = board_agg.loc[ars_str]
        board_total  = safe_float(bd_row["board_day"])
        alight_total = safe_float(bd_row["alight_day"])
    else:
        board_total  = safe_float(row.get("board_day", 0))
        alight_total = safe_float(row.get("alight_day", 0))

    # 시간대별 승하차
    boardings_hr  = [round(board_total  * hourly_ratio[h]) for h in HOURS_LIST]
    alightings_hr = [round(alight_total * hourly_ratio[h]) for h in HOURS_LIST]

    # 피크 비율
    peak_board = sum(boardings_hr[i] for i in peak_indices)
    peak_share = round(100.0 * peak_board / board_total, 1) if board_total > 0 else 0.0

    profiles[sid] = {
        "stopId":             sid,
        "stopName":           str(row["name"]),
        "kind":               kind,
        "routes":             routes_list,
        "isEstimated":        True,
        "estimationMethod":   "일자별 승하차를 통신 유동인구 시간배율로 안분",
        "hours":              HOURS_LIST,
        "boardings":          boardings_hr,
        "alightings":         alightings_hr,
        "summary": {
            "boardingsPerDay":  int(round(board_total)),
            "alightingsPerDay": int(round(alight_total)),
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
        "sizeMeters":        1000,
        "analysisCellCount": 786,
        "displaySizeMeters": 1500,
        "cellCount":         786,
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
