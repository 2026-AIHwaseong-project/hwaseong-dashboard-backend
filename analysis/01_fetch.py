"""
공공 API 수집 — 노선 · 경유정류소 · 배차간격

    python analysis/01_fetch.py

산출 (dataset_hwaseong/)
    routes.csv        노선 마스터 + 배차간격(peek/nPeek/night) + 인가대수
    route_stops.csv   노선별 경유정류소 순서 (노선 ↔ 정류소 관계)
    stops_tago.csv    TAGO 정류소 마스터 (nodeid ↔ ARS번호 다리)

이 셋이 공급 S 의 재료입니다. S = 0.78·norm(운행빈도) + 0.22·커버리지 인데
운행빈도가 여기서만 나옵니다.

---
[검증 완료 2026-08-08] TAGO routeId 는 GGB 접두어만 떼면 경기도 routeId 다.

    TAGO  GGB200000008  →  경기도  200000008   ✅ 배차 peek=30 nPeek=40

두 API 의 ID 체계가 다를까 봐 프로젝트 내내 걸어둔 위험이었는데 호환된다.
안 되면 노선번호(400, 50-2)로 매칭하는 폴백을 쓸 참이었다.

[주의] cityCode=31240 은 화성시 밖 정류장도 준다(실측 22.7%).
화성시 면허 노선이 성남·용인까지 가기 때문이다. 여기서는 전부 받아두고
`in_hwaseong` 으로 태깅만 한다. 거르는 건 03_join.py 의 몫이다.
노선의 운행빈도를 계산하려면 노선 전체 맥락이 필요하다.
"""
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from shapely.geometry import Point, shape
from shapely.ops import unary_union

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dataset_hwaseong"

KEY = os.getenv("DATA_GO_KR_KEY_DECODING", "")
TAGO_ROUTE = os.getenv("TAGO_ROUTE_BASE")
TAGO_STOP = os.getenv("TAGO_STOP_BASE")
GG_ROUTE = os.getenv("GG_BUS_ROUTE_BASE")
CITY = os.getenv("TAGO_CITY_CODE", "31240")

PAUSE = 0.12          # API 예의. 146개 노선 × 2회면 300회쯤 된다
RETRY = 3

if not KEY:
    sys.exit(".env 의 DATA_GO_KR_KEY_DECODING 이 비어 있습니다. check_api.py 를 먼저 돌리세요.")


def get(url, params, tries=RETRY):
    """공공 API 는 간헐적으로 빈 응답·타임아웃을 낸다. 조용히 재시도한다."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == tries - 1:
                print(f"    !! 실패 {url.rsplit('/', 1)[-1]} {params.get('routeId', '')}: {e}")
                return None
            time.sleep(0.6 * (i + 1))


def tago_items(url, params):
    """TAGO 응답에서 item 목록만 꺼낸다. 1건이면 dict, 0건이면 빈 문자열로 온다."""
    d = get(url, {**params, "serviceKey": KEY, "_type": "json"})
    if not d:
        return []
    body = d.get("response", {}).get("body") or {}
    items = (body.get("items") or {})
    items = items.get("item") if isinstance(items, dict) else None
    if items is None:
        return []
    return items if isinstance(items, list) else [items]


def tago_all(url, params, page_size=1000):
    """페이지를 끝까지 넘긴다."""
    out, page = [], 1
    while True:
        got = tago_items(url, {**params, "numOfRows": page_size, "pageNo": page})
        out += got
        if len(got) < page_size:
            return out
        page += 1
        time.sleep(PAUSE)


# buffer(0) 은 25m 단순화 과정에서 생긴 자기교차 정리용. 없으면 union 이 터집니다.
hwaseong = unary_union([shape(f["geometry"]).buffer(0) for f in
                        json.loads((OUT / "hwaseong_dong.geojson").read_text(encoding="utf-8"))["features"]])
inside = lambda lon, lat: hwaseong.contains(Point(lon, lat))


def save(rows, name, cols):
    p = OUT / name
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  -> {name}  {len(rows):,}행  {p.stat().st_size / 1e6:.2f} MB")


print("=" * 64)
print("[1] TAGO 정류소 마스터 — nodeid ↔ ARS번호 다리")
stops = []
for s in tago_all(f"{TAGO_STOP}/getSttnNoList", {"cityCode": CITY}):
    lon, lat = float(s["gpslong"]), float(s["gpslati"])
    stops.append({
        "node_id": s["nodeid"], "ars_no": s.get("nodeno"), "name": s["nodenm"],
        "lon": round(lon, 6), "lat": round(lat, 6),
        "in_hwaseong": int(inside(lon, lat)),
    })
n_in = sum(s["in_hwaseong"] for s in stops)
print(f"  {len(stops):,}개 중 화성시 내부 {n_in:,}개 ({n_in / len(stops):.1%})")
save(stops, "stops_tago.csv", ["node_id", "ars_no", "name", "lon", "lat", "in_hwaseong"])

print("=" * 64)
print("[2] TAGO 노선 목록")
routes = []
for r in tago_all(f"{TAGO_ROUTE}/getRouteNoList", {"cityCode": CITY}):
    routes.append({
        "route_id": r["routeid"],
        "gg_route_id": str(r["routeid"]).removeprefix("GGB"),   # ★ 경기도 API 키
        "route_no": str(r.get("routeno", "")).strip(),
        "route_type": r.get("routetp", ""),
        "start_stop": r.get("startnodenm", ""), "end_stop": r.get("endnodenm", ""),
        "first_time": r.get("startvehicletime", ""), "last_time": r.get("endvehicletime", ""),
    })
print(f"  {len(routes):,}개 노선")

print("=" * 64)
print("[3] 경기도 배차간격 — 공급 S 의 78%")
ok = 0
for i, r in enumerate(routes, 1):
    d = get(f"{GG_ROUTE}/getBusRouteInfoItemv2",
            {"serviceKey": KEY, "routeId": r["gg_route_id"], "format": "json"})
    it = ((d or {}).get("response", {}).get("msgBody") or {}).get("busRouteInfoItem")
    if isinstance(it, list):
        it = it[0] if it else None
    if it:
        r.update({
            "peek_alloc": it.get("peekAlloc"), "npeek_alloc": it.get("nPeekAlloc"),
            "night_alloc": it.get("nightAlloc"), "dispatch_num": it.get("dispatchNum"),
            "up_first": it.get("upFirstTime"), "up_last": it.get("upLastTime"),
            "region": it.get("region"), "company": it.get("companyName"),
            "gg_route_name": str(it.get("routeName") or ""), "route_type_cd": it.get("routeTypeCd"),
        })
        ok += 1
    if i % 40 == 0 or i == len(routes):
        print(f"    {i}/{len(routes)}  배차 취득 {ok}")
    time.sleep(PAUSE)

rate = ok / len(routes)
print(f"  배차간격 취득률 {rate:.1%}  ({ok}/{len(routes)})")
save(routes, "routes.csv",
     ["route_id", "gg_route_id", "route_no", "route_type", "route_type_cd",
      "start_stop", "end_stop", "first_time", "last_time",
      "peek_alloc", "npeek_alloc", "night_alloc", "dispatch_num",
      "up_first", "up_last", "region", "company", "gg_route_name"])

print("=" * 64)
print("[4] 노선별 경유정류소")
by_node = {s["node_id"]: s for s in stops}
links, miss = [], 0
for i, r in enumerate(routes, 1):
    got = tago_all(f"{TAGO_ROUTE}/getRouteAcctoThrghSttnList",
                   {"cityCode": CITY, "routeId": r["route_id"]}, page_size=500)
    if not got:
        miss += 1
    for s in got:
        lon, lat = float(s["gpslong"]), float(s["gpslati"])
        links.append({
            "route_id": r["route_id"], "route_no": r["route_no"],
            "seq": s["nodeord"], "node_id": s["nodeid"], "ars_no": s.get("nodeno"),
            "name": s["nodenm"], "lon": round(lon, 6), "lat": round(lat, 6),
            "in_hwaseong": by_node.get(s["nodeid"], {}).get("in_hwaseong", int(inside(lon, lat))),
        })
    if i % 40 == 0 or i == len(routes):
        print(f"    {i}/{len(routes)}  누적 {len(links):,}개 구간")
    time.sleep(PAUSE)

save(links, "route_stops.csv",
     ["route_id", "route_no", "seq", "node_id", "ars_no", "name", "lon", "lat", "in_hwaseong"])

print("=" * 64)
print("[5] 검증")
# 경기도 API 는 노선명을 숫자로 줄 때가 있습니다(400 → int). str() 로 눌러줍니다.
name_of = lambda r: (r["route_no"] or str(r.get("gg_route_name") or "")).strip()
drt = [r for r in routes if any(k in f"{r['route_no']}{r.get('gg_route_name') or ''}"
                                for k in ("똑", "콜버스", "수요응답"))]
print(f"  똑버스·콜버스 {len(drt)}개: {', '.join(sorted(name_of(r) for r in drt))}")

hw_links = [l for l in links if l["in_hwaseong"]]
hw_routes = {l["route_id"] for l in hw_links}
print(f"  화성시를 지나는 노선 {len(hw_routes)}개 / 전체 {len(routes)}개")
print(f"  화성시 내부 경유구간 {len(hw_links):,}개 / 전체 {len(links):,}개")
print(f"  화성시 내부 정류장 중 노선이 붙은 것 {len({l['ars_no'] for l in hw_links}):,}개")

# 노선 없는 정류장이 많으면 경유정류소 수집이 샌 것입니다.
assert rate >= 0.8, f"배차 취득률 {rate:.1%} — 80% 미만이면 routeId 매칭을 다시 보세요"
assert len(links) > 5000, f"경유구간 {len(links)}개 — 너무 적습니다 (146노선이면 2만 내외 기대)"
assert miss <= len(routes) * 0.1, f"경유정류소를 못 받은 노선 {miss}개"
print(f"\n  ✅ 통과")
