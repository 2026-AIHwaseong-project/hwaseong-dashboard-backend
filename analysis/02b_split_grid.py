# -*- coding: utf-8 -*-
"""
검증용 격자 분할 — 1km 실측 격자를 균등 4분할해 500m 파이프라인을 시운전한다

    python analysis/02b_split_grid.py            # 1km → 500m (2×2 분할)

⚠️ 이 산출물은 **실데이터가 아니다**. 셀 위치·크기는 실제 500m 격자와 같지만,
   인구·종사자 등 통계는 부모 1km 실측값을 4로 나눈 **가정 분할**이다
   (총량은 보존되지만 셀 간 분포 차이는 지어낸 것이 없다 — 형제 4칸이 전부 같은 값).
   용도는 단 하나: SGIS 500m 신청 데이터가 오기 전에 파이프라인·서버·화면이
   500m 체계에서 도는지 검증하는 것. **제출·발표에 쓰면 안 된다.**
   grid_spec.json 에 sourceSuffix="_SPLIT_DRYRUN" 으로 표시된다.

되돌리기 (1km 실데이터 복원):
    git checkout -- dataset_hwaseong/grid_hwaseong.csv dataset_hwaseong/grid_spec.json
    이후 03→04→05_load 재실행 (또는 git checkout 으로 산출물 일괄 복원)
"""
import csv
import json
import sys
from datetime import date
from pathlib import Path

from pyproj import Transformer

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"
SRC = D / "grid_hwaseong.csv"
SPEC = D / "grid_spec.json"

FACTOR = 2                      # 한 변 분할 수 (2 → 4등분, 500m)
CHILD_M = 1000 // FACTOR
SUFF = "abcd"                   # 자식 셀 ID 접미사 (좌상→우상→좌하→우하)

to4326 = Transformer.from_crs(5179, 4326, always_xy=True).transform

# 분할 대상 확인 — 이미 분할본이면 중복 분할을 막는다
try:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    if spec.get("sizeMeters") != 1000:
        sys.exit(f"현재 격자가 {spec.get('sizeMeters')}m 입니다 — 1km 원본에서만 분할할 수 있습니다.\n"
                 "git checkout -- dataset_hwaseong/ 으로 1km 를 복원한 뒤 다시 실행하세요.")
except FileNotFoundError:
    pass

rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
cols = rows[0].keys()
COUNT_COLS = [c for c in cols if c in
              ("pop", "elderly", "households", "houses", "workers")
              or c.startswith("a")]          # 연령대 a0009…a70p 포함 — 전부 나눔

half = 1000 / FACTOR / 2                     # 자식 중심까지의 오프셋 (±250m)
OFF = [(-half, half), (half, half), (-half, -half), (half, -half)]

out = []
for r in rows:
    x0, y0 = float(r["x_5179"]), float(r["y_5179"])
    for k, (dx, dy) in enumerate(OFF):
        c = dict(r)
        c["grid_id"] = r["grid_id"] + SUFF[k]
        cx, cy = x0 + dx, y0 + dy
        lon, lat = to4326(cx, cy)
        c["x_5179"], c["y_5179"] = round(cx, 1), round(cy, 1)
        c["lon"], c["lat"] = round(lon, 5), round(lat, 5)
        for col in COUNT_COLS:
            c[col] = round(float(r[col] or 0) / (FACTOR * FACTOR), 1)
        p = float(c["pop"]) or 0
        c["elderly_ratio"] = round(float(c["elderly"]) / p, 4) if p else 0.0
        out.append(c)

with open(SRC, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(cols))
    w.writeheader()
    w.writerows(out)

SPEC.write_text(json.dumps({
    "sizeMeters": CHILD_M,
    "cellCount": len(out),
    "sourceSuffix": "_SPLIT_DRYRUN",
    "note": "⚠️ 검증용 균등 분할 — 부모 1km 실측을 4등분한 가정값. 제출 금지. "
            "SGIS 500m 신청 데이터 도착 시 02_grid.py --size 500 으로 교체",
    "generatedAt": str(date.today()),
}, ensure_ascii=False, indent=2), encoding="utf-8")

pop = sum(float(c["pop"]) for c in out)
print(f"분할 완료: {len(rows):,}칸(1km) → {len(out):,}칸({CHILD_M}m) · 총인구 {pop:,.0f}명 (보존)")
print("⚠️ 검증용 가정 분할입니다 — grid_spec.json 의 note 참조. 제출 금지.")
print("다음: python analysis/03_join.py → 04_model → 05_simulate → 05_load")
