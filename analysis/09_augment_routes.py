# -*- coding: utf-8 -*-
"""
경유정류소 보강 — 화성 정류장을 지나지만 화성 면허가 아니라 빠졌던 노선을 채운다

    입력  route_station.csv   08 단계가 받은 경기도 BMS 경유정류소 (482,779행)
          routes.csv          01 단계가 받은 화성 면허 146개 노선 + 배차
          route_stops.csv     01 단계가 받은 경유구간
    출력  routes_plus.csv       위 146개 + 살아있는 추가 노선 (배차 포함)
          route_stops_plus.csv  위 경유구간 + 추가 노선의 경유구간
          augment_meta.json     생존 판정 결과 요약

왜 필요한가
-----------
01_fetch 는 TAGO 를 cityCode=화성 으로 조회한다. 그래서 화성시 면허 노선
146개만 들어오고, 수원·오산·용인 면허로 화성을 지나는 노선은 통째로 빠진다.
마을버스 155개 노선(routes_village.csv)은 경유정류소가 아예 공개되지 않는다.
그 결과 정류장 746개(26%)에 노선이 하나도 안 붙었고, 그중 666개는 승차량이
있었다 — 병점역은 하루 1,357명이 타는데 노선이 없는 것으로 잡혔다.

03_join 은 이걸 읍면동 중앙값으로 대체해 거짓 사각지대는 막고 있었지만,
병점역 같은 대형 정류장까지 중앙값(배차 6.0회)으로 눌러 과소평가한다.

폐선 거르기 — 이 단계의 핵심
----------------------------
route_station.csv 의 데이터 기준일은 2023-09-26 이다. 그 사이 없어진 노선이
섞여 있다. 표본 30개를 경기도 배차 API 에 물어보니 응답한 것은 40% 뿐이었다.
없는 버스를 공급에 넣으면 사각지대가 사라진 것처럼 보인다 — 분석이 거꾸로
망가진다. 그래서 추가 후보 전부를 배차 API 에 물어보고, 배차가 확인된
노선만 채택한다. 배차는 어차피 운행빈도 계산에 필요하므로 같은 호출로 끝난다.
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"
KEY = os.getenv("DATA_GO_KR_KEY_DECODING", "")
GG = os.getenv("GG_BUS_ROUTE_BASE", "")
PAUSE = 0.08

read = lambda n: list(csv.DictReader(open(D / n, encoding="utf-8-sig")))
gid = lambda v: str(v or "").strip().removeprefix("GGB")

print("=" * 66)
print("[1] 대상 추리기")
# 03_join 이 쓰는 정류장 키 = 국토부 정류장번호에서 GGB 뗀 것 = 경기도 정류소id
ours_stop = {gid(s["정류장번호"]) for s in read("stops_national_hwaseong.csv")}
ours_route = {r["gg_route_id"] for r in read("routes.csv")}
print(f"  우리 정류장 {len(ours_stop):,}개 / 보유 노선 {len(ours_route)}개")

# 우리 정류장을 지나는 노선을 경유정류소에서 모은다
links = defaultdict(list)          # route_id -> [(seq, station_id)]
for r in read("route_station.csv"):
    sid = str(r["station_id"]).strip()
    if sid in ours_stop:
        links[r["route_id"]].append((int(r["seq"] or 0), sid))
extra = sorted(set(links) - ours_route)
print(f"  우리 정류장을 지나는 노선 {len(links)}개 중 미보유 {len(extra)}개")

print("=" * 66)
print(f"[2] 생존 확인 + 배차 수집 — 후보 {len(extra)}개")


def headway(route_id):
    """경기도 노선 정보. 응답이 없으면 지금은 없는 노선으로 본다."""
    p = {"serviceKey": KEY, "routeId": route_id, "format": "json"}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    f"{GG}/getBusRouteInfoItemv2?" + urllib.parse.urlencode(p),
                    timeout=25) as r:
                d = json.load(r)
            body = d.get("response", {}).get("msgBody")
            return body.get("busRouteInfoItem") if body else None
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.0 * (attempt + 1))


alive, dead = {}, 0
for i, rid in enumerate(extra, 1):
    it = headway(rid)
    if it:
        alive[rid] = it
    else:
        dead += 1
    if i % 40 == 0 or i == len(extra):
        print(f"    {i}/{len(extra)}  생존 {len(alive)}  폐선 {dead}")
    time.sleep(PAUSE)
print(f"  => 살아있는 추가 노선 {len(alive)}개 ({len(alive) / max(len(extra), 1):.0%})")

print("=" * 66)
print("[3] 병합 저장")
# routes_plus — 기존 스키마를 그대로 쓴다. 03_join 이 읽는 컬럼만 맞으면 된다.
base_routes = read("routes.csv")
cols = list(base_routes[0].keys())
added = []
for rid, it in alive.items():
    row = {c: "" for c in cols}
    row.update({
        "route_id": "GGB" + rid, "gg_route_id": rid,
        "route_no": str(it.get("routeName") or ""),
        "route_type": "", "route_type_cd": str(it.get("routeTypeCd") or ""),
        "start_stop": str(it.get("startStationName") or ""),
        "end_stop": str(it.get("endStationName") or ""),
        "first_time": str(it.get("upFirstTime") or "").replace(":", ""),
        "last_time": str(it.get("upLastTime") or "").replace(":", ""),
        "peek_alloc": str(it.get("peekAlloc") or ""),
        "npeek_alloc": str(it.get("nPeekAlloc") or ""),
        # nightAlloc 은 같은 응답에 들어 있다 — 01_fetch.py 도 이 필드를 읽는다.
        # 예전에 "0" 을 박아 두었더니 03_join 의 night→npeek 폴백이 걸려,
        # 심야 배차가 비첨두 배차로 대체 추정됐다. 표본에서 nightAlloc 은 항상
        # nPeekAlloc 이상(=배차간격이 더 김)이라 심야 공급이 과대 산정된다.
        "night_alloc": str(it.get("nightAlloc") or ""),
        "up_first": str(it.get("upFirstTime") or ""),
        "up_last": str(it.get("upLastTime") or ""),
        "region": str(it.get("regionName") or ""),
        "company": str(it.get("companyName") or ""),
        "gg_route_name": str(it.get("routeName") or ""),
    })
    added.append(row)

with open(D / "routes_plus.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(base_routes)
    w.writerows(added)
print(f"  -> routes_plus.csv  {len(base_routes)} + {len(added)} = {len(base_routes) + len(added)}행")

# route_stops_plus — 기존 경유구간 + 추가 노선의 경유구간
base_links = read("route_stops.csv")
lcols = list(base_links[0].keys())
# 05_load 는 ars_no 로 정류장을 찾는다("41590-"+ars). 좌표도 지도에 그대로 쓴다.
# 비워 두면 03_join 만 보강되고 화면(stops.json/routes.json)은 그대로다.
info = {}
for s in read("stops_national_hwaseong.csv"):
    k = gid(s["정류장번호"])
    a = str(s.get("모바일단축번호") or "").strip()
    if a.endswith(".0"):
        a = a[:-2]
    info[k] = {"name": s["정류장명"], "ars": a.lstrip("0"),
               "lon": s.get("경도", ""), "lat": s.get("위도", "")}

new_links, no_ars = [], 0
for rid in alive:
    for seq, sid in sorted(links[rid]):
        m = info.get(sid)
        if not m or not m["ars"]:
            no_ars += 1
            continue
        row = {c: "" for c in lcols}
        row.update({"route_id": "GGB" + rid,
                    "route_no": str(alive[rid].get("routeName") or ""),
                    "seq": seq, "node_id": "GGB" + sid, "ars_no": m["ars"],
                    "name": m["name"], "lon": m["lon"], "lat": m["lat"],
                    "in_hwaseong": 1})
        new_links.append(row)
if no_ars:
    print(f"  ARS 번호가 없어 건너뛴 경유구간 {no_ars:,}건")

with open(D / "route_stops_plus.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=lcols, extrasaction="ignore")
    w.writeheader()
    w.writerows(base_links)
    w.writerows(new_links)
print(f"  -> route_stops_plus.csv  {len(base_links):,} + {len(new_links):,} = "
      f"{len(base_links) + len(new_links):,}행")

covered_before = {gid(l["node_id"]) for l in base_links} & ours_stop
covered_after = covered_before | {gid(l["node_id"]) for l in new_links}
meta = {
    "extra_candidates": len(extra), "extra_alive": len(alive), "extra_dead": dead,
    "stops_covered_before": len(covered_before),
    "stops_covered_after": len(covered_after),
    "stops_gained": len(covered_after) - len(covered_before),
    "source_snapshot": "route_station.csv 기준일 2023-09-26",
    "built": time.strftime("%Y-%m-%d %H:%M"),
}
(D / "augment_meta.json").write_text(
    json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"  -> augment_meta.json")
print(f"  정류장 노선 커버리지 {len(covered_before):,} → {len(covered_after):,} "
      f"(+{meta['stops_gained']:,})")

assert len(alive) > 0, "살아있는 추가 노선이 하나도 없습니다 — 배차 API 를 확인하세요"
assert meta["stops_gained"] > 0, "커버리지가 늘지 않았습니다"
