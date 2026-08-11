# -*- coding: utf-8 -*-
"""
경기데이터드림 BMS — 노선 실제 경로형상 · 노선↔정류소 매핑 수집

    출력
      route_path.csv       노선ID, 순서, lon, lat        (노선이 실제로 달리는 도로 형상)
      route_station.csv    노선ID, 순서, 정류소ID, 상하행  (경유정류소 — 마을버스 포함)

왜 필요한가
-----------
[1] 지금 지도의 노선 선은 정류장을 순서대로 이은 직선입니다. TAGO·경기도
    노선 API 가 경유정류소 목록만 주고 폴리라인을 안 주기 때문입니다.
    전체 뷰에서는 티가 안 나지만 확대해 실제 도로가 보이면 선이 건물과
    논밭을 가로질러, "버스가 이 길로 간다"는 틀린 주장이 됩니다.

[2] 03_join 이 노선 결측 정류장 718개를 읍면동 중앙값으로 대체하고 있습니다.
    한산한 농촌 정류장은 그래도 되지만, 병점역(일 승차 1,357명)까지 중앙값
    6.0 회로 눌리는 건 명백한 과소평가입니다. 원인은 01_fetch 가
    cityCode=화성으로 조회해 화성시 면허 140개 노선만 받기 때문이고,
    마을버스 155개 노선은 경유정류소가 아예 없습니다.
    이 API 는 경기도 전체 BMS 를 그대로 주므로 둘 다 메웁니다.

API 특성 (2026-08-11 실측)
--------------------------
  - ROUTE_ID 필터가 먹지 않습니다. 전량 페이징만 가능합니다.
  - pSize 상한 1,000. 요청당 약 0.3초. 요청 횟수 제한은 없습니다.
  - 경로형상 600,000행 / 경유정류소 482,779행 → 합쳐 약 1,083 요청.

좌표계
------
X_CRDNT/Y_CRDNT 는 "직교좌표"로만 표기돼 있습니다. 값의 범위로 중부원점
TM(false northing 500000)까지는 좁혀지지만, 타원체가 GRS80(EPSG:5181)인지
Bessel(EPSG:2097)인지는 문서에 없습니다. 둘은 한반도에서 100~200m 어긋나
지도에서 도로를 벗어나므로 찍어보고 정합니다 — 정류소 좌표(WGS84 기준)와
경로점의 거리를 재서 더 가까운 쪽을 채택합니다.

    [판정 결과 2026-08-11] EPSG:5174 (중부원점 Bessel, FN 500000).
    정류소↔경로 중앙거리 6.2m 로 압도적. 차선 EPSG:2097 은 154.8m,
    EPSG:5181 은 252.5m 로 도로를 벗어난다.

수집 결과와 한계 (2026-08-11 실측)
----------------------------------
[경로형상] 쓸 수 없다. TBBMSDRAWHISTM 은 정확히 600,000행에서 끊기는
    1회성 부분 추출본이다(포털 표기도 "갱신주기 없음"). 우리 146개 노선 중
    25개만 걸리고, 그 25개도 seq 가 정확히 2벌씩 중복된다(HIST_ID 2개가
    섞임). 400번은 정류장 151개인데 경로점이 200개뿐이고, 73번은 4개다.
    노선 길이를 재 보면 M4130 이 2,355km 로 나온다 — 이력이 섞여 점프한다.
    => 지도의 노선은 당분간 점선(근사) 표기를 유지한다.

[경유정류소] 쓸 수 있다. 482,779행 완전본. 우리 정류장 중 노선이 안 붙던
    746개 가운데 333개(45%)가 채워지고, 문제였던 대형 정류장은 전부 해결된다
    (병점역 승차 1,357명 → 경유노선 4개, 삼성반도체 → 13개).
    단 데이터 기준일이 2023-09-26 이라 폐선이 섞인다. 화성 정류장을 지나는
    "우리가 못 가진 노선" 240개를 표본 조사하니 현재 배차 조회가 되는 것은
    40% 였다. 반드시 경기도 배차 API 로 생존 확인을 거쳐 살아있는 노선만
    운행빈도에 반영해야 한다. 안 그러면 없는 버스로 공급을 부풀린다.
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from pyproj import Transformer

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

KEY = os.getenv("GG_DATADREAM_KEY", "")
BASE = "https://openapi.gg.go.kr"
# 다른 단계들과 같은 자리를 쓴다 (.env 의 DATA_RAW_DIR 은 03_join 도 안 본다)
ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"
PAGE = 1000
PAUSE = 0.05          # API 예의. 제한은 없다지만 1,000회를 몰아치지는 않는다.

if not KEY:
    sys.exit("GG_DATADREAM_KEY 가 .env 에 없습니다.")


def fetch_all(table, want=None):
    """테이블 전량을 페이징으로 받는다. want 가 있으면 그 ROUTE_ID 만 남긴다."""
    rows, page, total = [], 1, None
    while True:
        p = {"KEY": KEY, "Type": "json", "pIndex": page, "pSize": PAGE}
        url = f"{BASE}/{table}?" + urllib.parse.urlencode(p)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    d = json.load(r)
                break
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if "RESULT" in d:                      # 마지막 페이지를 넘기면 여기로 온다
            break
        k = next(x for x in d if x != "RESULT")
        if total is None:
            total = d[k][0]["head"][0]["list_total_count"]
        got = d[k][1]["row"]
        rows.extend(r for r in got if want is None or r["ROUTE_ID"] in want)
        if page % 50 == 0 or len(got) < PAGE:
            print(f"    {page * PAGE:>7,}/{total:,}  보관 {len(rows):,}")
        if len(got) < PAGE:
            break
        page += 1
        time.sleep(PAUSE)
    return rows, total


# 우리 노선만 남긴다. routes.csv 의 gg_route_id 가 BMS ROUTE_ID 와 같은 체계다.
ours = {r["gg_route_id"] for r in csv.DictReader(
    open(D / "routes.csv", encoding="utf-8-sig"))}
print(f"우리 노선 {len(ours)}개 기준으로 걸러 담습니다")

print("=" * 66)
print("[1] 노선 경로형상 TBBMSDRAWHISTM")
path_rows, path_total = fetch_all("TBBMSDRAWHISTM", ours)
print(f"  전체 {path_total:,}행 중 우리 노선 {len(path_rows):,}행")

print("=" * 66)
print("[2] 노선 경유정류소 TBBMSROUTESTATIONM")
# 경유정류소는 우리 노선 밖도 필요하다 — 화성 정류장을 지나는 타 시군 면허
# 노선을 찾는 게 목적이므로 전량 보관하고 뒤에서 정류장 기준으로 거른다.
stn_rows, stn_total = fetch_all("TBBMSROUTESTATIONM", None)
print(f"  전체 {stn_total:,}행 수신")

print("=" * 66)
print("[3] 좌표계 판정 — 정류소 실제 위치와 대조")
# 경유정류소로 "노선 n번의 m번째 정류소" 를 알고, 정류소의 WGS84 좌표는
# stops_gg.csv 에 있다. 경로점은 정류소를 지나가므로, 올바른 좌표계로 변환하면
# 경로점과 정류소가 수십 m 안에서 만나야 한다.
gg_stops = {}
for r in csv.DictReader(open(D / "stops_gg.csv", encoding="utf-8-sig")):
    sid = str(r["정류소id"]).strip()
    try:
        gg_stops[sid] = (float(r["WGS84경도"]), float(r["WGS84위도"]))
    except (ValueError, KeyError):
        pass

by_route = {}
for r in path_rows:
    by_route.setdefault(r["ROUTE_ID"], []).append(r)
for v in by_route.values():
    v.sort(key=lambda r: r["COURS_DOT_ORDR"])

probe_rid = max(by_route, key=lambda k: len(by_route[k]))
probe_pts = by_route[probe_rid]
probe_stn = [r for r in stn_rows if r["ROUTE_ID"] == probe_rid]
probe_ll = [gg_stops[str(r["STTN_ID"]).strip()]
            for r in probe_stn if str(r["STTN_ID"]).strip() in gg_stops]
print(f"  기준 노선 {probe_rid}: 경로점 {len(probe_pts):,}개, 위치 아는 정류소 {len(probe_ll)}개")


def median_gap(epsg):
    """경로점을 epsg 로 보고 WGS84 로 되돌린 뒤, 각 정류소에서 가장 가까운
       경로점까지의 거리 중앙값(m). 맞는 좌표계면 아주 작아야 한다."""
    tr = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True)
    pts = [tr.transform(float(r["X_CRDNT"]), float(r["Y_CRDNT"])) for r in probe_pts]
    import math
    gaps = []
    for slon, slat in probe_ll:
        kx = 111320 * math.cos(math.radians(slat))
        best = min((((lon - slon) * kx) ** 2 + ((lat - slat) * 110574) ** 2)
                   for lon, lat in pts)
        gaps.append(best ** 0.5)
    gaps.sort()
    return gaps[len(gaps) // 2]


CANDIDATES = ["EPSG:5181", "EPSG:2097", "EPSG:5186", "EPSG:5174", "EPSG:5179"]
scores = {}
for e in CANDIDATES:
    try:
        scores[e] = median_gap(e)
        print(f"    {e}: 정류소↔경로 중앙거리 {scores[e]:10,.1f} m")
    except Exception as ex:
        print(f"    {e}: 변환 실패 {ex}")
CRS = min(scores, key=scores.get)
print(f"  => 채택 {CRS}  (중앙거리 {scores[CRS]:.1f} m)")
assert scores[CRS] < 60, (
    f"최적 좌표계도 중앙거리 {scores[CRS]:.0f}m — 경로점이 도로를 벗어납니다. "
    "후보 좌표계를 더 넣어 보세요.")

print("=" * 66)
print("[4] 저장")
tr = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
with open(D / "route_path.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["route_id", "seq", "lon", "lat", "turn", "acc_m"])
    for rid, pts in sorted(by_route.items()):
        for r in pts:
            lon, lat = tr.transform(float(r["X_CRDNT"]), float(r["Y_CRDNT"]))
            w.writerow([rid, r["COURS_DOT_ORDR"], round(lon, 6), round(lat, 6),
                        r.get("TRNORD_EXTNO") or "", r.get("STTN_ACCMLT_DSTN") or ""])
print(f"  -> route_path.csv  {len(path_rows):,}행  노선 {len(by_route)}개")

with open(D / "route_station.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["route_id", "seq", "station_id", "updown", "acc_m"])
    for r in stn_rows:
        w.writerow([r["ROUTE_ID"], r["STTN_ORDR"], r["STTN_ID"],
                    r.get("PROGRS_DIV_CD") or "", r.get("ACCMLT_DSTN") or ""])
print(f"  -> route_station.csv  {len(stn_rows):,}행")

meta = {"crs": CRS, "crs_gap_m": round(scores[CRS], 1),
        "path_rows": len(path_rows), "station_rows": len(stn_rows),
        "fetched": time.strftime("%Y-%m-%d %H:%M")}
(D / "geometry_meta.json").write_text(
    json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"  -> geometry_meta.json  {meta}")
