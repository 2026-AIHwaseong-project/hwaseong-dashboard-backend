# -*- coding: utf-8 -*-
"""
db.py — PostgreSQL 에서 server/main.py 의 DATA 를 채웁니다.

    DATABASE_URL 이 있고 연결되면  → DB 에서 읽음
    없거나 연결 실패              → None 을 돌려주고 main.py 가 JSON 으로 감

읽는 대상은 v_* 뷰뿐입니다(batch_* 직접 조회 금지). 관리자 override 가 들어와도
API 가 자동으로 합쳐진 값을 보게 하는 것이 이 스키마의 요점이기 때문입니다.

⚠️ 이 모듈의 유일한 계약: **05_load.py 가 만든 계약 JSON 과 같은 것을 돌려준다.**
   키 순서까지 같아야 합니다 — /api/v1/priorities 가 priorityScore 로 정렬할 때
   Python 의 안정 정렬이라 동점 격자의 순위를 배열 순서가 가르고, 응답 바이트가
   달라지면 무엇이 달라진 것인지 아무도 추적하지 못합니다.
   확인: python analysis/06_verify_db.py
"""
import json
import os
import sys

PERIODS = ["am", "day", "pm", "night"]


def _connect():
    """DATABASE_URL 이 없으면 None. 있으면 연결을 시도하고, 실패하면 크게 알린 뒤 None."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
    except ImportError:
        print("[db] DATABASE_URL 이 있지만 psycopg2 가 없습니다 — JSON 으로 갑니다."
              "  (pip install psycopg2-binary)", file=sys.stderr, flush=True)
        return None
    try:
        conn = psycopg2.connect(url, connect_timeout=5)
    except Exception as exc:
        # 조용히 넘어가지 않습니다. DB 를 켰다고 믿으면서 실제로는 JSON 을 읽고 있는
        # 상태가 가장 나쁩니다 — 관리자 수정이 화면에 안 나오는데 원인을 못 찾습니다.
        print(f"[db] ⚠️ DATABASE_URL 연결 실패 — JSON 으로 갑니다: {exc}",
              file=sys.stderr, flush=True)
        return None
    with conn.cursor() as cur:
        # float 왕복이 정확해야 합니다. PG12+ 의 기본값이 이미 최단 왕복 표기라
        # 사실상 같지만, "바이트가 같아야 한다"가 이 모듈의 계약이라 못박아 둡니다.
        cur.execute("SET extra_float_digits = 3")
    return conn


def _cells(cur, period, quad_label, action_label):
    """격자 셀 목록. 키 순서는 05_load.py 가 쓰는 순서 그대로입니다 — 바꾸지 마세요."""
    cur.execute(
        "SELECT grid_id, name, region, region_code, region_kind, lon, lat,"
        " demand, supply, z_demand, z_supply, mi, flow, flow_trips_per_day,"
        " elderly_ratio, coverage, quadrant, action, priority_score,"
        " nearest_stop_id, bins"
        " FROM v_grid_cell WHERE period = %s ORDER BY ord",
        (period,),
    )
    out = []
    for (gid, name, region, rcode, rkind, lon, lat, demand, supply, zd, zs, mi,
         flow, ftpd, eld, cov, quad, action, prio, nsid, bins) in cur.fetchall():
        out.append({
            "id": gid, "name": name, "region": region, "regionCode": rcode,
            "regionKind": rkind, "lon": lon, "lat": lat,
            "demand": demand, "supply": supply, "zDemand": zd, "zSupply": zs,
            "mi": mi, "flow": flow, "flowTripsPerDay": ftpd,
            "elderlyRatio": eld, "coverage": cov,
            "quadrant": quad, "quadrantLabel": quad_label.get(quad, quad),
            "action": action, "actionLabel": action_label.get(action, action),
            "priorityScore": prio, "nearestStopId": nsid,
            # 배치 결과에는 조정된 칸이 없습니다. 이 깃발은 시뮬레이션 엔진이
            # 요청을 처리하며 켜는 런타임 표시라 DB 에 둘 것이 아닙니다.
            "adjusted": False,
            "bins": {"mi": bins["mi"], "demand": bins["demand"],
                     "supply": bins["supply"], "flow": bins["flow"]},
        })
    return out


def _kpi(cells: list) -> dict:
    """시간대 KPI. 저장된 값을 읽지 않고 **셀에서 다시 셉니다.**

    파이프라인이 낸 값을 그대로 보관할 수도 있지만, 그러면 관리자가 어떤 칸을
    need 로 고쳤을 때 지도에는 붉은 칸이 하나 늘고 상단 KPI 는 그대로인 화면이
    나옵니다(실제로 그렇게 나왔습니다). 합쳐진 값에서 세야 둘이 안 갈립니다.

    05_load.py 와 같은 결과가 나오는 이유 — 거기서도 셀에 실은 것과 **같은
    반올림값**으로 셉니다(flowTripsPerDay = round(pop×BUS_TRIP_RATE) 정수,
    elderlyRatio = 4자리 반올림). 원시 실수를 따로 합산하지 않으므로 셀 합과
    어긋날 자리가 없습니다. 같은지는 06_verify_db.py 가 바이트로 확인합니다.
    """
    need = [c for c in cells if c["quadrant"] == "need"]
    total = len(cells)
    return {
        "needCells": len(need),
        "drtCells": sum(1 for c in cells if c["quadrant"] == "drt"),
        "overCells": sum(1 for c in cells if c["quadrant"] == "over"),
        "totalCells": total,
        # needShare 는 need 만 셉니다 — 근거는 05_load.py 의 같은 자리 주석.
        "needShare": round(100.0 * len(need) / total, 1) if total else 0.0,
        "potentialTripsPerDay": sum(c["flowTripsPerDay"] for c in need),
        "elderlyTripsPerDay": int(round(
            sum(c["flowTripsPerDay"] * c["elderlyRatio"] for c in need))),
    }


def load_all(quad_label: dict, action_label: dict):
    """DATA 에 넣을 dict 을 돌려줍니다. DB 를 못 쓰면 None.

    라벨 표를 인자로 받는 이유 — main.py 가 이미 갖고 있는 정본을 그대로 씁니다.
    여기서 다시 정의하면 05_load.py·main.py·db.py 세 곳이 되고, 하나만 고치는
    순간 화면의 분면 이름이 조용히 갈라집니다.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM batch_grid")
            if not cur.fetchone()[0]:
                print("[db] ⚠️ batch_grid 가 비었습니다 — JSON 으로 갑니다."
                      "  (python analysis/06_load_db.py)", file=sys.stderr, flush=True)
                return None

            data = {}
            cur.execute("SELECT doc FROM batch_meta WHERE id = 1")
            data["meta"] = cur.fetchone()[0]

            for p in PERIODS:
                # _cells 가 같은 커서를 다시 쓰므로 여기서 먼저 꺼내 둡니다.
                cur.execute(
                    "SELECT mi_thresholds FROM batch_grid_period WHERE period = %s", (p,)
                )
                thresholds = cur.fetchone()[0]
                cells = _cells(cur, p, quad_label, action_label)
                data[f"grid_{p}"] = {
                    "period": p,
                    "scale": {"miThresholds": thresholds},
                    "kpi": _kpi(cells),
                    "cells": cells,
                }

            cur.execute(
                "SELECT stop_id, ars_no, name, dong, lon, lat, kind, routes,"
                " boardings_per_day FROM batch_stop ORDER BY ord"
            )
            data["stops"] = {"stops": [
                {"id": r[0], "arsNo": r[1], "name": r[2], "dong": r[3],
                 "lon": r[4], "lat": r[5], "kind": r[6], "routes": r[7],
                 "boardingsPerDay": r[8]}
                for r in cur.fetchall()
            ]}

            cur.execute(
                "SELECT route_id, name, type, stop_ids, path FROM batch_route ORDER BY ord"
            )
            data["routes"] = {"routes": [
                {"id": r[0], "name": r[1], "type": r[2], "stopIds": r[3], "path": r[4]}
                for r in cur.fetchall()
            ]}

            cur.execute("SELECT stop_id, doc FROM batch_stop_profile ORDER BY ord")
            data["profiles"] = {sid: doc for sid, doc in cur.fetchall()}

            cur.execute("SELECT count(*) FROM v_grid_metrics WHERE is_overridden")
            n_ov = cur.fetchone()[0]
            cur.execute(
                "SELECT id, finished_at FROM batch_run WHERE status = 'success'"
                " ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        run = f"batch_run #{row[0]} ({row[1]:%Y-%m-%d %H:%M})" if row else "batch_run 없음"
        print(f"[server] DB 에서 로드 — {run}, 관리자 수정 {n_ov}건", flush=True)
        return data
    except Exception as exc:
        print(f"[db] ⚠️ DB 읽기 실패 — JSON 으로 갑니다: {exc}", file=sys.stderr, flush=True)
        return None
    finally:
        conn.close()
