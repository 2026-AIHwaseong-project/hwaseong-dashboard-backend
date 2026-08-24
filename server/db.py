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


def warn(msg: str) -> None:
    """경고는 stderr 로. Python 이 stderr 를 errors='backslashreplace' 로 열어 주므로
    콘솔 인코딩(Windows 기본 cp949)이 못 쓰는 글자가 섞여도 터지지 않습니다.
    stdout 은 errors='strict' 라 사정이 다릅니다 — load_all 끝 주석 참고."""
    print(f"[db] {msg}", file=sys.stderr, flush=True)


def _connect():
    """DATABASE_URL 이 없으면 None. 있으면 연결을 시도하고, 실패하면 크게 알린 뒤 None."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
    except ImportError:
        warn("DATABASE_URL 이 있지만 psycopg2 가 없습니다. JSON 으로 갑니다."
             "  (pip install psycopg2-binary)")
        return None
    try:
        conn = psycopg2.connect(url, connect_timeout=5)
    except Exception as exc:
        # 조용히 넘어가지 않습니다. DB 를 켰다고 믿으면서 실제로는 JSON 을 읽고 있는
        # 상태가 가장 나쁩니다 — 관리자 수정이 화면에 안 나오는데 원인을 못 찾습니다.
        warn(f"DATABASE_URL 연결 실패. JSON 으로 갑니다: {exc}")
        return None
    with conn.cursor() as cur:
        # float 왕복이 정확해야 합니다. PG12+ 의 기본값이 이미 최단 왕복 표기라
        # 사실상 같지만, "바이트가 같아야 한다"가 이 모듈의 계약이라 못박아 둡니다.
        cur.execute("SET extra_float_digits = 3")
    return conn


def _cells(cur, period, quad_label, action_label, daytype="wd"):
    """격자 셀 목록. 키 순서는 05_load.py 가 쓰는 순서 그대로입니다 — 바꾸지 마세요."""
    cur.execute(
        "SELECT grid_id, name, region, region_code, region_kind, lon, lat,"
        " demand, supply, z_demand, z_supply, mi, flow, flow_trips_per_day,"
        " elderly_ratio, coverage, quadrant, action, priority_score,"
        " nearest_stop_id, bins"
        " FROM v_grid_cell WHERE period = %s AND daytype = %s ORDER BY ord",
        (period, daytype),
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
                warn("batch_grid 가 비었습니다. JSON 으로 갑니다."
                     "  (python analysis/06_load_db.py)")
                return None

            data = {}
            cur.execute("SELECT doc FROM batch_meta WHERE id = 1")
            data["meta"] = cur.fetchone()[0]

            # 평일·주말 두 축. JSON 모드는 grid_am / grid_am_we 두 벌을 만들므로
            # DB 모드도 같은 키를 내야 화면의 요일 토글이 양쪽에서 같게 동작합니다.
            # 주말이 적재돼 있지 않으면 그 키를 만들지 않습니다 — 평일 값으로
            # 채워 넣으면 토글이 "둘 다 같은 화면"이 되어 조용히 틀립니다.
            for dt in ("wd", "we"):
                for p in PERIODS:
                    # _cells 가 같은 커서를 다시 쓰므로 여기서 먼저 꺼내 둡니다.
                    cur.execute(
                        "SELECT mi_thresholds FROM batch_grid_period"
                        " WHERE period = %s AND daytype = %s", (p, dt)
                    )
                    row = cur.fetchone()
                    if row is None:
                        continue
                    cells = _cells(cur, p, quad_label, action_label, dt)
                    key = f"grid_{p}" if dt == "wd" else f"grid_{p}_we"
                    data[key] = {
                        "period": p,
                        "scale": {"miThresholds": row[0]},
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
                "SELECT route_id, name, type, stop_ids, path, ops"
                " FROM batch_route ORDER BY ord"
            )
            # 키 순서가 05_load.py 와 같아야 합니다 — 계약 비교(06_verify_db.py)가
            # 정렬 없이 바이트로 맞춰 보기 때문입니다. ops 는 그 뒤에 이어 붙습니다.
            data["routes"] = {"routes": [
                {"id": r[0], "name": r[1], "type": r[2], "stopIds": r[3], "path": r[4],
                 **(r[5] or {})}
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
    except Exception as exc:
        warn(f"DB 읽기 실패. JSON 으로 갑니다: {exc}")
        return None
    finally:
        conn.close()

    # ⚠️ 이 print 는 try 밖에 있어야 합니다. 안에 뒀다가 실제로 당했습니다 —
    #    메시지에 em dash(U+2014)가 있었는데 Windows 콘솔 기본 인코딩이 cp949 라
    #    stdout 이 UnicodeEncodeError 를 던졌고, 위 except 가 그걸 잡아 "DB 읽기
    #    실패" 로 처리하며 조용히 JSON 으로 내려갔습니다. 데이터는 멀쩡히 다 읽은
    #    뒤였습니다. **출력이 실패했다고 데이터 출처가 바뀌면 안 됩니다.**
    #    (stderr 는 Python 이 errors='backslashreplace' 로 열어 줘서 안 터집니다.
    #     그래서 경고만 내던 개발 중에는 이 함정이 안 보였습니다.)
    print(f"[server] DB 에서 로드. {run}, 관리자 수정 {n_ov}건", flush=True)
    return data
