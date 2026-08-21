# -*- coding: utf-8 -*-
"""교통카드 OD 에서 법정동별 시간분포 곡선을 만든다.

왜 한 곳에 모았나
-----------------
03_join(모델의 시간대 배분)과 05_load(정류장 프로파일)가 같은 승차량 B 를
시간에 나눈다. 두 곳이 각자 계산하면 갈라진다 — 실제로 수요 정의가 그렇게
갈라져 시 전체로 12.5% 어긋난 적이 있다(b79dbe8). 곡선은 여기서만 만든다.

왜 동별인가
-----------
시 전체 곡선 하나를 쓰면 출근 비중이 모든 정류장에서 18.5% 로 같아진다.
실측은 동마다 다르다 — 새솔동 43.9% · 청계동 27.3% · 석우동 10.1% 로
4.3배 차이가 난다. 베드타운은 아침에 몰리고 도심은 하루에 퍼진다.

작은 표본은 시 전체 곡선 쪽으로 당긴다 (shrinkage)
--------------------------------------------------
통행 649건짜리 동의 43.9% 를 곧이곧대로 쓰면 표본 노이즈가 그대로 들어온다.
w = n/(n+SHRINK) 로 섞는다 — 통행이 많은 동은 자기 곡선을, 적은 동은 시 전체
곡선을 쓰게 되고 그 사이가 연속적으로 이어진다. 임계값으로 자르면 1건 차이로
곡선이 튀는데 그런 절벽을 만들지 않는다.
"""
from collections import defaultdict
from pathlib import Path
import csv

SHRINK = 1000          # 이만큼의 통행이 모여야 자기 곡선을 절반 반영한다


def load_stop_emd(d_dir: Path):
    """ARS번호 → 법정동코드. 없으면 빈 dict."""
    p = Path(d_dir) / "stop_emd.csv"
    if not p.exists():
        return {}
    out = {}
    for x in csv.DictReader(open(p, encoding="utf-8-sig")):
        a, e = (x.get("ars") or "").strip(), (x.get("emd_cd") or "").strip()
        if a and e:
            out[a] = e
    return out


def hourly_by_emd(d_dir: Path, hours, sfx="wd"):
    """(시 전체 곡선, {법정동: 곡선}) 반환. OD 가 없으면 (None, {}).

    곡선은 hours 위의 비중 리스트이고 합이 1 이다. sfx 로 평일(wd)/주말(we)
    OD 파일을 고른다 — 주말분이 없는 팀원은 "we" 요청도 (None, {}) 로 조용히
    빠진다(파이프라인이 깨지지 않는다)."""
    p = Path(d_dir) / f"od_quarter_{sfx}.csv"
    if not p.exists():
        return None, {}

    city = defaultdict(float)
    per = defaultdict(lambda: defaultdict(float))
    for x in csv.DictReader(open(p, encoding="utf-8-sig")):
        try:
            h = int(x["hour"])
            t = float(x["trips"] or 0)
        except (TypeError, ValueError, KeyError):
            continue          # 시간·통행수가 비어 있는 행은 버린다
        if h not in hours:
            continue
        city[h] += t
        per[(x.get("stg_emd") or "").strip()][h] += t

    tot = sum(city.values())
    if tot <= 0:
        return None, {}
    city_curve = [city[h] / tot for h in hours]

    out = {}
    for emd, hr in per.items():
        n = sum(hr.values())
        if not emd or n <= 0:
            continue
        w = n / (n + SHRINK)
        raw = [hr[h] / n for h in hours]
        out[emd] = [w * r + (1 - w) * c for r, c in zip(raw, city_curve)]
    return city_curve, out


def period_share(curve, hours, periods):
    """시간곡선 → 시간대별 비중 {pid: share}. curve 가 None 이면 None."""
    if curve is None:
        return None
    idx = {h: i for i, h in enumerate(hours)}
    return {pid: sum(curve[idx[h]] for h in range(h0, h1) if h in idx)
            for pid, h0, h1 in periods}
