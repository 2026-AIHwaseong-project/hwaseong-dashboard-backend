# -*- coding: utf-8 -*-
"""
적재 — 정적 JSON(프론트 계약 형태) + PostgreSQL batch_* 테이블

    python analysis/06_load.py                 # JSON 만 (DB 없어도 됨)
    python analysis/06_load.py --db            # JSON + PostgreSQL 적재

산출 (data/)
    meta.json            GET /meta
    grid_{period}.json   GET /grid?period=      ★ 가장 중요
    priorities_{p}.json  GET /priorities?period=
    stops.json           GET /stops
    routes.json          GET /routes

---
[1] 왜 JSON 을 먼저 만드는가 — 데모 안전장치 (기획서 §7)

발표장에서 DB나 네트워크가 죽으면 서버 구조는 통째로 멎는다. 프론트에 한 줄
폴백을 두면 그때도 화면이 뜬다.

    fetch("/api/v1/grid?period=am").catch(() => fetch("/data/grid_am.json"))

그래서 이 파일은 **DB 없이도 항상 JSON 을 떨군다.** `--db` 를 줄 때만 적재를
시도하고, 실패해도 JSON 은 이미 나와 있다.

부수 효과가 하나 더 있다. 담당 B 의 서버가 준비되기 전에도 프론트가
`USE_MOCK: false` 로 실데이터를 볼 수 있다.

[2] 응답 형태는 프론트 docs/API.md 가 계약이다
snake_case 인 파이프라인 컬럼을 camelCase 로 바꾸는 지점이 여기다.
서버(담당 B)도 같은 변환을 하므로, 이 파일의 to_cell() 이 사실상 참조 구현이다.

[3] x/y/w/h 는 넣지 않는다
프론트가 grid.bbox 기준으로 직접 투영한다(API.md "지도 좌표 — 전부 경위도입니다").
lon/lat 만 주면 되고, 서버가 SVG 좌표를 계산할 이유가 없다.
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"
OUT = ROOT / "data"
OUT.mkdir(exist_ok=True)

PERIODS = [("am", "출근", "07–09", [7, 9]), ("day", "낮", "09–17", [9, 17]),
           ("pm", "퇴근", "17–19", [17, 19]), ("night", "심야", "22–24", [22, 24])]
QUAD_LABEL = {"need": "고수요·저공급", "over": "저수요·고공급", "drt": "수요응답 후보",
              "ok": "적정", "mid": "균형권"}
ACTION_LABEL = {"NEW_STOP": "신설", "ADD_FREQ": "증차", "DRT": "똑버스", "": ""}

# 사업비 — 전부 가정값이다. 프론트가 confirmed:false 로 화면에 표시한다.
COST = {
    "stop": {"krw": 42_000_000, "basis": "capital", "lifeYears": 10,
             "annualMaintenanceKrw": 1_000_000, "label": "정류장 신설 1개소"},
    "drt": {"krw": 180_000_000, "basis": "operating", "lifeYears": 1,
            "label": "똑버스 1대 연간 운영비"},
    "freq": {"krw": 95_000_000, "basis": "operating", "lifeYears": 1,
             "label": "배차 증편 (노선 1개 · 1일 4회) 연간"},
}
EFFECTS = [("stop", "정류장 신설", "●", 2.0, "capital", "1회성 자본비(내용연수 10년)"),
           ("drt", "똑버스 배치", "◆", 3.0, "operating", "연간 운영비"),
           ("freq", "배차 증편", "▲", 2.4, "operating", "연간 운영비")]

g = lambda o, k, d=None: (None if pd.isna(o.get(k, d)) else o.get(k, d))


def num(v, nd=None):
    if v is None or pd.isna(v):
        return None
    f = float(v)
    return round(f, nd) if nd is not None else f


def to_cell(r):
    """파이프라인 행 → 프론트 cells[] 한 칸. 서버도 같은 변환을 한다."""
    return {
        "id": r["grid_id"],
        "name": f"{r['region']} {r['grid_id'][-4:]}",
        "region": r["region"], "regionCode": str(r["region_code"]),
        "regionKind": r["region_kind"],
        "lon": num(r["lon"], 5), "lat": num(r["lat"], 5),
        "demand": num(r["d_score"], 1), "supply": num(r["s_score"], 1),
        "zDemand": num(r["zD"], 4), "zSupply": num(r["zS"], 4),
        "mi": num(r["mi"], 4),
        "flow": num(r["nf"], 4), "flowTripsPerDay": int(round(float(r["flow_trips_per_day"]))),
        "elderlyRatio": num(r["elderly_ratio"], 4),
        "coverage": num(r["coverage"], 4),
        "quadrant": r["quadrant"], "quadrantLabel": QUAD_LABEL[r["quadrant"]],
        "action": r["action"] or "", "actionLabel": ACTION_LABEL.get(r["action"] or "", ""),
        "priorityScore": num(r["priority"], 5),
        "nearestStopId": r["nearest_stop_id"] if isinstance(r["nearest_stop_id"], str) else "",
        "adjusted": False,
        "bins": {"mi": int(r["bin_mi"]), "demand": int(r["bin_demand"]),
                 "supply": int(r["bin_supply"]), "flow": int(r["bin_flow"])},
    }


def reason_of(r):
    """사람이 읽을 문장. 화면과 AI 보고서에 그대로 인용된다."""
    d, s = int(round(r["d_score"])), int(round(r["s_score"]))
    if r["action"] == "NEW_STOP":
        why = f"가장 가까운 정류장까지 {int(r['nearest_stop_m'])}m 로 도보권 밖"
    elif r["action"] == "DRT":
        why = "노선 자체가 닿지 않아 고정노선보다 수요응답형이 적합"
    else:
        why = f"정류장은 도보권({int(r['nearest_stop_m'])}m)이나 운행이 부족"
    eld = f", 고령 인구 비중 {r['elderly_ratio']:.0%}" if r["elderly_ratio"] >= 0.2 else ""
    return f"수요지수 {d} 대비 공급지수 {s}, {why}{eld}"


def write(name, obj):
    p = OUT / name
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  -> data/{name}  {p.stat().st_size / 1024:>7,.0f} KB")
    return p


def main(load_db=False):
    m = pd.read_csv(D_DIR / "grid_metrics.csv")
    gh = pd.read_csv(D_DIR / "grid_hwaseong.csv")
    st = pd.read_csv(D_DIR / "stops_hwaseong.csv")
    rt = pd.read_csv(D_DIR / "routes.csv")
    rs = pd.read_csv(D_DIR / "route_stops.csv")
    norm = json.loads((D_DIR / "norm_stats.json").read_text(encoding="utf-8"))
    n_cells = m["grid_id"].nunique()

    print("=" * 62)
    print("[1] meta.json")
    bbox = [num(gh.lon.min(), 5), num(gh.lat.min(), 5), num(gh.lon.max(), 5), num(gh.lat.max(), 5)]
    meta = {
        "region": "화성시", "updatedAt": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "isMockData": False,
        "periods": [{"id": i, "name": n, "label": l, "hours": h} for i, n, l, h in PERIODS],
        "grid": {"sizeMeters": 1000, "analysisCellCount": n_cells,
                 "cellCount": n_cells, "crs": "EPSG:4326", "bbox": bbox},
        # ⚠️ 실제 출처다. 프론트 목의 "STCIS"·"통신사 유동인구" 는 우리가 안 쓰는 것들이다.
        "dataQuality": {
            "boardingDaily": {"level": "observed", "label": "일별 승하차",
                              "source": "경기데이터드림 정류소별 승하차 인원 집계"},
            "boardingHourly": {"level": "estimated", "label": "시간대별 승하차",
                               "method": "일자별 승하차를 연령가중 유동인구 시간배율로 안분",
                               "note": "원자료에 시간대 정보가 없습니다."},
            "flowHourly": {"level": "observed", "label": "시간대별 유동인구",
                           "source": "경기도 분석갤러리 유동인구(화성시) · 2023-12~2024-01"},
            "headway": {"level": "observed", "label": "배차간격",
                        "source": "경기도 버스노선 조회 API (peek/nPeek/nightAlloc)"},
            "boundary": {"level": "observed", "label": "행정경계",
                         "source": "SGIS 통계지리정보서비스 읍면동 경계 (bnd_dong_00_2025_2Q)"},
        },
        "cost": {**{k: {**v, "source": "가정값 — 실제 사업비 미확정", "confirmed": False}
                    for k, v in COST.items()},
                 "defaultBudget": 3_000_000_000},
        "assumptions": {
            "busTripRate": {"value": 0.25, "confirmed": False,
                            "note": "1인 1일 버스통행 = 전수단 원단위 2.5 × 버스분담률 0.10"},
            "minFreqPerHour": {"value": 2.0,
                               "note": "적정·공급과잉 판정의 절대 하한. 야간 상대평가 오라벨 방지"},
        },
        "formula": {
            "demand": "D = 0.5·norm(교통카드 승하차) + 0.5·norm(연령가중 유동인구)",
            "supply": "S = 0.78·norm(운행빈도) + 0.22·정류장 커버리지",
            "mismatch": "MI = (z(D) − z(S)) × clamp(D/P55(D),0,1)^0.65, ±2.6 클램프",
            "priority": "MI × (0.35 + norm(인구)) × (1 + 1.6·min(고령비,1))",
        },
        "effects": [{"type": t, "label": l, "icon": i, "radiusKm": r,
                     "unitKrw": COST[t]["krw"], "costBasis": b,
                     "costBasisLabel": bl, "costAssumed": True}
                    for t, l, i, r, b, bl in EFFECTS],
    }
    write("meta.json", meta)

    print("[2] grid_{period}.json  ★")
    mi_th = norm["constants"]["miThresholds"]
    for pid, _, _, _ in [p[:4] for p in PERIODS]:
        sub = m[m.period == pid]
        need = sub[sub.quadrant == "need"]
        kpi = {
            "needCells": int((sub.quadrant == "need").sum()),
            "drtCells": int((sub.quadrant == "drt").sum()),
            "overCells": int((sub.quadrant == "over").sum()),
            "totalCells": int(len(sub)),
            "needShare": round((sub.quadrant == "need").mean() * 100, 1),
            "potentialTripsPerDay": int(need.flow_trips_per_day.sum()),
            "elderlyTripsPerDay": int((need.flow_trips_per_day * need.elderly_ratio).sum()),
        }
        write(f"grid_{pid}.json", {
            "period": pid,
            "scale": {"miThresholds": mi_th},
            "kpi": kpi,
            "cells": [to_cell(r) for r in sub.to_dict("records")],
        })

    print("[3] priorities_{period}.json")
    for pid, _, _, _ in [p[:4] for p in PERIODS]:
        top = m[(m.period == pid) & (m.priority > 0)].nlargest(10, "priority")
        write(f"priorities_{pid}.json", {"period": pid, "items": [
            {"rank": i, "cellId": r["grid_id"], "name": f"{r['region']} {r['grid_id'][-4:]}",
             "mi": num(r["mi"], 3), "priorityScore": num(r["priority"], 4),
             "demand": num(r["d_score"], 1), "supply": num(r["s_score"], 1),
             "flowTripsPerDay": int(round(r["flow_trips_per_day"])),
             "elderlyRatio": num(r["elderly_ratio"], 4),
             "coverage": num(r["coverage"], 4),
             "action": r["action"], "actionLabel": ACTION_LABEL.get(r["action"], ""),
             "nearestStopId": r["nearest_stop_id"] if isinstance(r["nearest_stop_id"], str) else "",
             "reason": reason_of(r)}
            for i, r in enumerate(top.to_dict("records"), 1)]})

    print("[4] stops.json · routes.json")
    by_stop = (rs.dropna(subset=["ars_no"])
               .assign(ars=lambda d: d.ars_no.astype("Int64").astype(str))
               .groupby("ars")["route_no"].apply(lambda s: sorted({str(x) for x in s})).to_dict())
    st2 = st[st.stop_id.notna() & st.stop_id.astype(str).str.strip().ne("")]
    write("stops.json", {"stops": [
        {"id": r["stop_id"], "arsNo": str(r["ars"]).split(".")[0],
         "name": r["name"], "dong": r["region"],
         "lon": num(r["lon"], 6), "lat": num(r["lat"], 6),
         "kind": "hub" if r["board_day"] >= 500 else "normal",
         "routes": by_stop.get(str(r["ars"]).split(".")[0], []),
         "boardingsPerDay": num(r["board_day"], 1),
         "freqImputed": bool(r["freq_imputed"])}
        for r in st2.to_dict("records")]})

    write("routes.json", {"routes": [
        {"id": r["route_id"], "name": str(r["route_no"]), "type": r["route_type"],
         "company": g(r, "company"), "start": r["start_stop"], "end": r["end_stop"],
         "headwayPeek": num(g(r, "peek_alloc")), "headwayOffpeak": num(g(r, "npeek_alloc")),
         "headwayNight": num(g(r, "night_alloc")),
         "isDrt": any(k in f"{r['route_no']}{g(r, 'gg_route_name') or ''}"
                      for k in ("똑", "콜버스"))}
        for r in rt.to_dict("records")]})

    print("=" * 62)
    print("[5] 검증")
    am = json.loads((OUT / "grid_am.json").read_text(encoding="utf-8"))
    assert len(am["cells"]) == n_cells, f"셀 {len(am['cells'])} != 격자 {n_cells}"
    assert am["kpi"]["totalCells"] == n_cells, "kpi.totalCells 불일치"
    assert all(c["bins"]["mi"] in range(7) for c in am["cells"]), "bins.mi 가 0~6 밖"
    assert all(0 <= c["bins"][k] <= 4 for c in am["cells"]
               for k in ("demand", "supply", "flow")), "5분위 bins 가 0~4 밖"
    miss = [c["id"] for c in am["cells"] if not c["nearestStopId"]]
    assert not miss, f"nearestStopId 가 빈 셀 {len(miss)}개"
    pr = json.loads((OUT / "priorities_am.json").read_text(encoding="utf-8"))
    assert pr["items"] and all(i["reason"] for i in pr["items"]), "reason 이 비었습니다"
    total = sum(p.stat().st_size for p in OUT.glob("*.json"))
    print(f"  셀 {n_cells} · 정류장 {len(st2):,} · 노선 {len(rt)} · 총 {total / 1e6:.2f} MB")
    print(f"  출근 KPI: {am['kpi']}")
    print(f"  우선순위 1위: {pr['items'][0]['name']} — {pr['items'][0]['reason']}")

    if load_db:
        print("=" * 62)
        print("[6] PostgreSQL 적재")
        try:
            import psycopg2  # noqa: F401
            sys.exit("psycopg2 는 있으나 DB 적재는 담당 B 의 Docker Compose 준비 후 연결합니다.")
        except ImportError:
            print("  psycopg2 미설치 — 건너뜁니다. JSON 은 위에 이미 나왔습니다.")
    else:
        print("\n  DB 적재는 --db 로. 없어도 위 JSON 만으로 프론트가 돕니다.")
    print("\n  ✅ 통과")


if __name__ == "__main__":
    main(load_db="--db" in sys.argv)
