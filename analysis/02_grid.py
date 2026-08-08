"""
SGIS 1km 격자 → 화성시만 추출 + 읍면동 배정 + 격자 통계 결합

    python analysis/02_grid.py

이 프로젝트 최대 난관입니다. 격자코드로 화성시를 못 고르기 때문입니다.
코드집 `adm_grid_mapping.xlsx` 는 시도 단위 매핑만 있어(경기도 → 다바·다사·
다아·라바·라사·라아) 격자코드 문자열로는 시군구를 거를 수 없습니다.
따라서 격자 경계 shp 를 화성시 행정경계로 **공간조인**하는 게 유일한 방법입니다.

산출: dataset_hwaseong/grid_hwaseong.csv   (담당 A → 05_load.py → batch_grid)

좌표계
    입력 격자 shp : EPSG:5179 (미터)   ← 모든 공간 연산은 여기서
    입력 경계     : EPSG:4326 (경위도) ← 5179 로 변환해서 씀
    출력          : 둘 다. lon/lat 은 4326, 면적·거리는 5179 기준
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import shapefile
from pyproj import Transformer
from shapely.geometry import Polygon, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")   # 없으면 sys.exit 메시지가 깨져 나옵니다

ROOT = Path(__file__).resolve().parent.parent
SGIS = ROOT / "dataset" / "국가데이터처_SGIS 격자 통계 및 경계"
BOUNDARY = ROOT / "dataset_hwaseong" / "hwaseong_dong.geojson"
OUT = ROOT / "dataset_hwaseong" / "grid_hwaseong.csv"

# 경기도(시도코드 31)에 걸치는 100km 격자 블록. 코드집 adm_grid_mapping.xlsx 기준
BLOCKS = ["다바", "다사", "다아", "라바", "라사", "라아"]

# 연령 구간 — flow_hourly.csv 의 컬럼(남자0009…여자6569)과 같은 구간으로 맞춥니다.
#
# 왜 맞추는가: 유동인구 CSV 는 시군구 단위 하나뿐이라 모든 격자에 같은 시간배율이
# 곱해집니다. 그런데 z-score 는 공통 배수에 불변이라(z(c·D) = z(D)) 시간대를 바꿔도
# 수요 z 가 전혀 안 바뀝니다. 시간대 탭이 죽는 겁니다.
#
# 연령별로 곱하면 살아납니다. 격자마다 연령 구성이 다르므로
#     D_t(i) = Σ_연령 [ 격자 i 의 해당 연령 인구 × 그 연령의 t 시간대 유동 비율 ]
# 고령이 많은 격자는 낮에 완만하고 청년이 많은 격자는 출퇴근에 뾰족해집니다.
# 그래서 여기서 연령대를 합치지 않고 그대로 남깁니다. (03_join.py 에서 사용)
AGE_BANDS = {
    "a0009": ["in_age_001", "in_age_002"],   # 유동인구는 0~9 를 한 칸으로 줍니다
    "a1014": ["in_age_003"],
    "a1519": ["in_age_004"],
    "a2024": ["in_age_005"],
    "a2529": ["in_age_006"],
    "a3034": ["in_age_007"],
    "a3539": ["in_age_008"],
    "a4044": ["in_age_009"],
    "a4549": ["in_age_010"],
    "a5054": ["in_age_011"],
    "a5559": ["in_age_012"],
    "a6064": ["in_age_013"],
    "a6569": ["in_age_014"],
    # ⚠️ 유동인구 CSV 는 65~69 가 마지막입니다. 70 세 이상 대응 컬럼이 없습니다.
    #    03_join.py 에서 6569 비율을 대용으로 씁니다(그 사실을 응답에 표시).
    "a70p": [f"in_age_{i:03d}" for i in range(15, 22)],
}

# 결합할 격자 통계. (폴더, 파일접두, {출력컬럼: [통계항목코드…]})
STATS = [
    ("1. 2024년 격자 통계(인구)", "인구", {"pop": ["to_in_001"], **AGE_BANDS}),
    ("2. 2024년 격자 통계(가구)", "가구", {"households": ["to_ga_001"]}),
    ("3. 2024년 격자 통계(주택)", "주택", {"houses": ["to_ho_001"]}),
    ("4. 2024년 격자통계(사업체, 종사자)/종사자(대분류)", "종사자",
     {"workers": ["to_em_020"]}),                                # 총종사자 = 출근 도착수요
]

to4326 = Transformer.from_crs(5179, 4326, always_xy=True).transform
to5179 = Transformer.from_crs(4326, 5179, always_xy=True).transform


def load_dongs():
    """읍면동 경계를 5179 로 변환해 반환. 프론트 build-boundary.py 산출물을 재사용합니다.

    Polygon 과 MultiPolygon(제부도 등 섬을 낀 면)이 섞여 있어 타입을 가리지 않는
    shapely.ops.transform 을 씁니다. buffer(0) 은 단순화 과정에서 생긴 자기교차 정리용.
    """
    gj = json.loads(BOUNDARY.read_text(encoding="utf-8"))
    return [{**f["properties"], "geom": transform(to5179, shape(f["geometry"])).buffer(0)}
            for f in gj["features"]]


def read_stat(path, wanted):
    """SGIS 통계는 long format(격자코드·통계항목·통계값). 필요한 코드만 격자별로 합산."""
    code2col = {c: col for col, codes in wanted.items() for c in codes}
    acc = defaultdict(lambda: defaultdict(float))
    with open(path, encoding="cp949") as f:
        next(f)  # header
        for row in csv.reader(f):
            col = code2col.get(row[2])
            if col:
                # 비공개 처리된 셀은 빈 값이나 '*' 로 옵니다
                try:
                    acc[row[1]][col] += float(row[3])
                except ValueError:
                    pass
    return acc


if not (SGIS / "2. 경계").is_dir():
    sys.exit(
        "\n원본 SGIS 격자 데이터가 없습니다.\n"
        f"  찾은 경로: {SGIS}\n\n"
        "이 스크립트는 전국 원본(279MB)이 있어야 돌아갑니다. 저장소에는 커밋돼 있지\n"
        "않습니다(.gitignore). **이미 만들어진 결과물이 필요할 뿐이라면 받지 마세요** —\n"
        "  dataset_hwaseong/grid_hwaseong.csv  (786격자, 커밋됨)\n"
        "가 이 스크립트의 산출물이고 그대로 쓰면 됩니다.\n\n"
        "격자 로직을 바꾸려고 재실행하는 경우에만 아래에서 받아 dataset/ 에 푸세요.\n"
        "  https://www.data.go.kr/data/15141768/fileData.do\n"
    )

print("=" * 64)
print("[1] 화성시 읍면동 경계 로드")
dongs = load_dongs()
hwaseong = unary_union([d["geom"] for d in dongs])
minx, miny, maxx, maxy = hwaseong.bounds
print(f"  {len(dongs)}개 읍면동 · 면적 {hwaseong.area / 1e6:,.0f} km²")
print(f"  bbox(5179) x {minx:,.0f}~{maxx:,.0f}  y {miny:,.0f}~{maxy:,.0f}")

tree = STRtree([d["geom"] for d in dongs])

print("=" * 64)
print(f"[2] 격자 shp {len(BLOCKS)}개 블록 → 화성시 필터 + 읍면동 배정")
cells, scanned = {}, 0
for blk in BLOCKS:
    shp = SGIS / "2. 경계" / f"grid_{blk}" / f"grid_{blk}_1K"
    if not shp.with_suffix(".shp").exists():
        print(f"  !! 없음: {shp.name}")
        continue

    hit = 0
    r = shapefile.Reader(str(shp), encoding="utf-8")
    for sr in r.iterShapeRecords():
        scanned += 1
        x0, y0, x1, y1 = sr.shape.bbox
        if x1 < minx or x0 > maxx or y1 < miny or y0 > maxy:
            continue                                    # bbox 선필터 — 전국 격자라 이게 없으면 느립니다
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        poly = Polygon(sr.shape.points)

        # 중심점이 어느 읍면동 안에 있는가. 프론트도 같은 점-다각형 판정을 씁니다.
        # ponytail: 중심점 기준이라 경계에 걸친 격자는 통째로 포함되거나 빠집니다.
        #           1km 격자·844km² 규모에서 오차가 작고 프론트와 결과가 일치합니다.
        dong = next((dongs[i] for i in tree.query(poly.centroid)
                     if dongs[i]["geom"].contains(poly.centroid)), None)
        if dong is None:
            continue

        lon, lat = to4326(cx, cy)
        cells[sr.record["GRID_CD"]] = {
            "grid_id": sr.record["GRID_CD"],
            "lon": round(lon, 5), "lat": round(lat, 5),   # 5자리 ≈ 1m
            "x_5179": round(cx, 1), "y_5179": round(cy, 1),
            "region_code": dong["code"], "region": dong["name"], "region_kind": dong["kind"],
        }
        hit += 1
    print(f"  {blk}  {len(r):>6,}칸 중 {hit:>4,}칸")

print(f"  스캔 {scanned:,}칸 → 화성시 {len(cells):,}칸")
if not cells:
    sys.exit("!! 격자가 하나도 안 잡혔습니다. 좌표계나 경계 파일을 확인하세요.")

print("=" * 64)
print("[3] 격자 통계 결합")
for folder, prefix, wanted in STATS:
    got = 0
    for blk in BLOCKS:
        p = SGIS / "1. 통계" / folder / f"2024년_{prefix}_{blk}_1K.csv"
        if not p.exists():
            continue
        for gid, vals in read_stat(p, wanted).items():
            if gid in cells:
                cells[gid].update(vals)
                got += 1
    shown = ", ".join(list(wanted)[:3]) + (f" 외 {len(wanted) - 3}개" if len(wanted) > 3 else "")
    print(f"  {prefix:6} {got:>5,}칸 결합  ({shown})")

BASE = ["pop", "households", "houses", "workers"]
cols = (["grid_id", "lon", "lat", "x_5179", "y_5179",
         "region_code", "region", "region_kind",
         "pop", "elderly", "elderly_ratio", "households", "houses", "workers"]
        + list(AGE_BANDS))
for c in cells.values():
    for k in BASE + list(AGE_BANDS):
        c.setdefault(k, 0)                              # 통계가 없는 격자 = 실제로 0인 무인 격자
        c[k] = round(c[k], 1)
    # 고령(65+)은 연령대에서 파생합니다. 따로 세면 두 값이 어긋날 여지가 생깁니다.
    c["elderly"] = round(c["a6569"] + c["a70p"], 1)
    c["elderly_ratio"] = round(c["elderly"] / c["pop"], 4) if c["pop"] else 0.0

rows = sorted(cells.values(), key=lambda c: c["grid_id"])
with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)

print("=" * 64)
print("[4] 검증")
pop = sum(c["pop"] for c in rows)
eld = sum(c["elderly"] for c in rows)
print(f"  격자 {len(rows):,}칸 · 총인구 {pop:,.0f}명 · 고령 {eld:,.0f}명 ({eld / pop:.1%})")

# 연령대 합 == 총인구 인지. 어긋나면 SGIS 코드 매핑이 틀린 것입니다.
band_sum = sum(sum(c[b] for b in AGE_BANDS) for c in rows)
gap = abs(band_sum - pop) / pop
print(f"  연령대 합 {band_sum:,.0f}명 vs 총인구 {pop:,.0f}명 — 차이 {gap:.2%}")
print(f"  총종사자 {sum(c['workers'] for c in rows):,.0f}명 · 총가구 {sum(c['households'] for c in rows):,.0f}가구")

by_region = defaultdict(lambda: [0, 0.0])
for c in rows:
    by_region[c["region"]][0] += 1
    by_region[c["region"]][1] += c["pop"]
print(f"\n  읍면동 {len(by_region)}개 (인구 상위 5 / 격자 상위 5)")
for r_ in sorted(by_region.items(), key=lambda kv: -kv[1][1])[:5]:
    print(f"    {r_[0]:8} {r_[1][0]:>3}칸 {r_[1][1]:>9,.0f}명")
for r_ in sorted(by_region.items(), key=lambda kv: -kv[1][0])[:5]:
    print(f"    {r_[0]:8} {r_[1][0]:>3}칸 {r_[1][1]:>9,.0f}명")

# 화성시 실제 인구는 약 100만명(2026). 격자 합이 여기서 크게 벗어나면 공간조인이 틀린 것입니다.
assert 700_000 < pop < 1_300_000, f"총인구 {pop:,.0f} — 화성시 규모(약 100만)를 벗어납니다"
assert 600 < len(rows) < 1100, f"격자 {len(rows)}칸 — 예상(약 850칸)을 벗어납니다"
assert len(by_region) == len(dongs), f"읍면동 {len(by_region)}/{len(dongs)}개만 격자를 받았습니다"
assert gap < 0.02, f"연령대 합이 총인구와 {gap:.1%} 어긋납니다 — SGIS 코드 매핑 확인"
print(f"\n  ✅ 통과 → {OUT.relative_to(ROOT)}  ({OUT.stat().st_size / 1e6:.2f} MB)")
