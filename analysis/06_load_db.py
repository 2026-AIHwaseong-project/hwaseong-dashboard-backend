# -*- coding: utf-8 -*-
"""
06_load_db.py — 계약 JSON(server/static/*.json) → PostgreSQL 적재

    docker compose up -d db
    pip install psycopg2-binary
    python analysis/06_load_db.py

파이프라인 CSV 가 아니라 **05_load.py 가 이미 확정한 계약 JSON** 을 읽습니다.
그래야 DB 내용이 JSON 과 같음이 자명해지고(서버가 어느 쪽을 읽어도 응답이 같아야
합니다), 45MB 파이프라인을 다시 돌리지 않아도 DB 를 채울 수 있습니다.

적재 순서: batch_run 시작 → 테이블 TRUNCATE → INSERT → geom 생성 → batch_run 완료.
전부 한 트랜잭션이라 도중에 죽으면 이전 내용이 그대로 남습니다.

⚠️ admin_* 는 건드리지 않습니다. TRUNCATE 대상에서 admin_grid_override 가 빠져
   있는 것은 실수가 아니라 이 스키마의 요점입니다 — 배치를 몇 번 돌려도 사람이
   고친 값이 살아남아야 합니다. admin_grid_override 에 batch_grid 로 가는 외래키를
   두지 않은 것도 같은 이유입니다(격자가 TRUNCATE 돼도 override 가 안 지워지게).
"""
import json
import math
import os
import sys
from pathlib import Path

# Windows 콘솔 기본값이 cp949 라 '⚠️' 같은 글자에서 print 가 UnicodeEncodeError 로
# 죽습니다(한글은 cp949 에 있어 안 죽는 탓에 더 늦게 드러납니다). 진입점 스크립트라
# 여기서 stdout 을 UTF-8 로 바꿔 둡니다 — 한글도 깨지지 않고 나옵니다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass    # 리다이렉트된 스트림 등 reconfigure 를 못 쓰는 경우

try:
    import psycopg2
    from psycopg2.extensions import AsIs, Float, register_adapter
    from psycopg2.extras import Json, execute_values
except ImportError:
    sys.exit("psycopg2 가 없습니다:  pip install psycopg2-binary")


def _adapt_float(f: float):
    """음수 0 을 살려서 보냅니다.

    PostgreSQL 은 `-0.0` 같은 **숫자 리터럴을 numeric 으로 먼저 파싱**하는데
    numeric 에는 음수 0 이 없습니다. 그래서 float8 컬럼에 넣어도 +0 이 됩니다
    (psycopg2 는 ' -0.0' 을 제대로 보냅니다 — 잃는 쪽은 서버의 리터럴 파서입니다).
    문자열로 감싸 float8 파서를 타게 하면 그대로 남습니다.

    사소해 보이지만 계약 JSON 에는 mi:-0.0 이 실제로 들어 있습니다
    (아주 작은 음수를 round(x, 4) 한 결과). 이걸 놓치면 DB 모드에서만 값이
    0.0 으로 바뀌고, JS 의 toFixed 는 "-0.00" 과 "0.00" 을 다르게 찍습니다.
    """
    if f == 0.0 and math.copysign(1.0, f) < 0:
        return AsIs("'-0'::float8")
    return Float(f)     # 나머지는 psycopg2 기본 어댑터 그대로(NaN·Infinity 포함)


register_adapter(float, _adapt_float)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "server" / "static"
SCHEMA = ROOT / "server" / "schema_ops.sql"
PERIODS = ["am", "day", "pm", "night"]

# 서버(server/main.py)와 같은 곳에서 설정을 읽습니다. 이걸 안 하면 .env 에 적어 둔
# DATABASE_URL 을 서버만 보고 이 스크립트는 아래 기본값으로 가서, 둘이 서로 다른
# DB 를 보면서도 아무 경고가 안 뜹니다.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass    # python-dotenv 미설치면 셸 환경변수만 사용

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://hw:hw_pass@localhost:5432/hwaseong"
)


def read(name: str):
    return json.loads((STATIC / f"{name}.json").read_text("utf-8"))


def main() -> int:
    if not STATIC.exists():
        sys.exit(f"계약 JSON 이 없습니다: {STATIC}  (먼저 python analysis/05_load.py)")

    print(f"연결: {DB_URL.rsplit('@', 1)[-1]}")
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    # 스키마는 매번 겁니다. 전부 IF NOT EXISTS / OR REPLACE 라 두 번 걸어도 무해하고,
    # 컨테이너를 새로 띄웠을 때 initdb 가 이미 걸어 뒀는지 신경 쓸 필요가 없습니다.
    cur.execute(SCHEMA.read_text("utf-8"))

    cur.execute(
        "INSERT INTO batch_run (note) VALUES (%s) RETURNING id",
        ("06_load_db.py — server/static/*.json 적재",),
    )
    run_id = cur.fetchone()[0]
    print(f"batch_run id={run_id}")

    # batch_* 만 비웁니다. admin_* 는 위 주석 참고.
    cur.execute(
        "TRUNCATE batch_grid, batch_grid_metrics, batch_grid_period, "
        "batch_meta, batch_stop, batch_route, batch_stop_profile CASCADE"
    )

    counts = {}

    # ── meta ────────────────────────────────────────────────────────────────
    meta = read("meta")
    cur.execute(
        "INSERT INTO batch_meta (id, doc, batch_run_id) VALUES (1, %s, %s)",
        (Json(meta), run_id),
    )
    cell_m = float(meta["grid"]["sizeMeters"])
    counts["meta"] = 1

    # ── 격자: 시간대 무관 속성 + 시간대별 지표 ───────────────────────────────
    # 평일(grid_am.json)과 주말(grid_am_we.json) 두 벌. 05_load.py 가 둘 다 굽고
    # 화면의 요일 토글이 이 축을 씁니다. 주말 파일이 없으면 평일만 싣습니다 —
    # 없는 것을 조용히 평일로 채우면 토글이 "둘 다 같은 화면"이 됩니다.
    grids = {("wd", p): read(f"grid_{p}") for p in PERIODS}
    for p in PERIODS:
        try:
            grids[("we", p)] = read(f"grid_{p}_we")
        except FileNotFoundError:
            pass
    daytypes = sorted({d for d, _ in grids})
    if "we" not in daytypes:
        print("⚠ 주말 계약 JSON(grid_*_we.json)이 없어 평일만 적재합니다 — "
              "화면의 주말 토글은 DB 모드에서 404 가 됩니다.")

    # 격자 뼈대는 am 기준 한 벌. (4시간대에서 아래 9개 필드가 같음은 확인했지만,
    # 데이터가 바뀌어 실제로 갈라지면 조용히 am 값이 이기는 대신 여기서 멈춥니다.)
    base = {c["id"]: c for c in grids[("wd", "am")]["cells"]}
    FIXED = ("name", "region", "regionCode", "regionKind", "lon", "lat",
             "elderlyRatio", "nearestStopId")
    for (dt, p), g in grids.items():
        for c in g["cells"]:
            b = base.get(c["id"])
            if b is None:
                sys.exit(f"격자 {c['id']} 가 am 에 없습니다 — 계약 JSON 이 어긋났습니다")
            bad = [f for f in FIXED if c[f] != b[f]]
            if bad:
                sys.exit(
                    f"격자 {c['id']} 의 {bad} 가 시간대({p})마다 다릅니다. "
                    "batch_grid 는 격자당 한 행이라 담을 수 없습니다 — "
                    "해당 필드를 batch_grid_metrics 로 옮겨야 합니다."
                )

    # ord = 계약 JSON 의 배열 순서. 왜 보존해야 하는지는 schema_ops.sql 의 주석 참고.
    execute_values(
        cur,
        "INSERT INTO batch_grid (grid_id, ord, name, region, region_code, region_kind,"
        " lon, lat, elderly_ratio, nearest_stop_id, batch_run_id) VALUES %s",
        [(c["id"], i, c["name"], c["region"], c["regionCode"], c["regionKind"],
          c["lon"], c["lat"], c["elderlyRatio"], c["nearestStopId"], run_id)
         for i, c in enumerate(base.values())],
    )
    counts["batch_grid"] = len(base)

    rows = []
    for (dt, p), g in sorted(grids.items()):
        for c in g["cells"]:
            rows.append((
                c["id"], p, dt, c["demand"], c["supply"], c["zDemand"], c["zSupply"],
                c["mi"], c["flow"], c["flowTripsPerDay"], c["coverage"],
                c["quadrant"], c["action"], c["priorityScore"], Json(c["bins"]), run_id,
            ))
    execute_values(
        cur,
        "INSERT INTO batch_grid_metrics (grid_id, period, daytype, demand, supply, z_demand,"
        " z_supply, mi, flow, flow_trips_per_day, coverage, quadrant, action,"
        " priority_score, bins, batch_run_id) VALUES %s",
        rows,
    )
    counts["batch_grid_metrics"] = len(rows)

    execute_values(
        cur,
        # KPI 는 안 넣습니다 — 격자에서 세는 값이라 저장하면 override 와 갈라집니다.
        # (근거는 schema_ops.sql 의 batch_grid_period 주석)
        "INSERT INTO batch_grid_period (period, daytype, mi_thresholds, batch_run_id)"
        " VALUES %s",
        [(p, dt, Json(g["scale"]["miThresholds"]), run_id)
         for (dt, p), g in sorted(grids.items())],
    )
    counts["batch_grid_period"] = len(grids)

    # ── 정류장 · 노선 · 프로필 ───────────────────────────────────────────────
    stops = read("stops")["stops"]
    execute_values(
        cur,
        "INSERT INTO batch_stop (stop_id, ord, ars_no, name, dong, lon, lat, kind,"
        " routes, boardings_per_day, batch_run_id) VALUES %s",
        [(s["id"], i, s["arsNo"], s["name"], s["dong"], s["lon"], s["lat"], s["kind"],
          Json(s["routes"]), s["boardingsPerDay"], run_id)
         for i, s in enumerate(stops)],
    )
    counts["batch_stop"] = len(stops)

    routes = read("routes")["routes"]
    execute_values(
        cur,
        "INSERT INTO batch_route (route_id, ord, name, type, stop_ids, path,"
        " ops, batch_run_id) VALUES %s",
        # 조회용 컬럼 5개를 뺀 나머지(운행정보)는 ops 한 칸에 순서 그대로 담습니다.
        # 계약 비교가 키 순서까지 바이트로 맞춰 보므로 dict 순서를 보존해야 합니다.
        [(r["id"], i, r["name"], r["type"], Json(r["stopIds"]), Json(r["path"]),
          Json({k: v for k, v in r.items()
                if k not in ("id", "name", "type", "stopIds", "path")}), run_id)
         for i, r in enumerate(routes)],
    )
    counts["batch_route"] = len(routes)

    profiles = read("profiles")
    execute_values(
        cur,
        "INSERT INTO batch_stop_profile (stop_id, ord, doc, batch_run_id) VALUES %s",
        [(k, i, Json(v), run_id) for i, (k, v) in enumerate(profiles.items())],
    )
    counts["batch_stop_profile"] = len(profiles)

    # ── PostGIS 도형 ────────────────────────────────────────────────────────
    # 계약 JSON 에는 격자 중심점만 있습니다. 폴리곤은 중심에서 meta.grid.sizeMeters
    # 의 절반만큼 사방으로 넓혀 만듭니다. 넓히는 계산은 미터 단위라야 하므로
    # EPSG:5179(중부원점 TM, 파이프라인이 쓰는 좌표계)로 옮겼다가 되돌립니다 —
    # 4326 에서 그냥 ±0.005도 하면 위도에 따라 칸 폭이 달라집니다.
    print("PostGIS 도형 생성 중...")
    cur.execute(
        """
        UPDATE batch_grid SET geom = ST_Transform(
          ST_Envelope(ST_Expand(
            ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 5179), %s)), 4326)
        """,
        (cell_m / 2.0,),
    )
    cur.execute(
        "UPDATE batch_stop SET geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)"
    )
    cur.execute(
        """
        UPDATE batch_route r SET geom = sub.g FROM (
          -- 별칭이 pt_ord 인 이유: batch_route 에 ord 컬럼이 있어서 그냥 ord 로 두면
          -- "column reference ord is ambiguous" 로 막힙니다(막히지 않았다면 노선
          -- 순번으로 좌표를 정렬해 선이 엉켰을 자리입니다).
          SELECT route_id,
                 ST_SetSRID(ST_MakeLine(
                   ST_MakePoint((e->>0)::float8, (e->>1)::float8) ORDER BY pt_ord), 4326) AS g
          FROM batch_route, LATERAL json_array_elements(path) WITH ORDINALITY t(e, pt_ord)
          GROUP BY route_id
        ) sub
        WHERE sub.route_id = r.route_id AND json_array_length(r.path) >= 2
        """
    )

    cur.execute(
        "UPDATE batch_run SET finished_at = now(), status = 'success', row_counts = %s"
        " WHERE id = %s",
        (Json(counts), run_id),
    )
    conn.commit()

    # ── 확인 ────────────────────────────────────────────────────────────────
    cur.execute("SELECT count(*) FROM v_grid_metrics")
    n_view = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM batch_grid WHERE geom IS NULL")
    n_nogeom = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM batch_route WHERE geom IS NULL")
    n_noline = cur.fetchone()[0]

    for k, v in counts.items():
        print(f"  {k:22s} {v:>6,}")
    print(f"  {'v_grid_metrics(뷰)':22s} {n_view:>6,}")
    if n_nogeom:
        print(f"  ⚠️ 격자 geom 없음 {n_nogeom}건")
    if n_noline:
        print(f"  ⚠️ 노선 geom 없음 {n_noline}건 (좌표 2개 미만)")

    cur.close()
    conn.close()
    print("적재 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
