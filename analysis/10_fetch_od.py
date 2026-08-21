# -*- coding: utf-8 -*-
"""
교통카드 빅데이터(STCIS) 15분단위 OD 수집 — 잠재수요를 실측으로 바꾸기 위한 시험 수집

    출력  od_quarter_{wd|we}.csv   출발동 · 도착동 · 시간 · 15분 · 통행수 · 평균통행시간
          od_meta_{wd|we}.json     수집 범위와 한계
          (접미사는 OD_DATE 가 평일이면 wd, 토/일이면 we — 같은 스크립트로 두 번 돌려
           평일·주말 대표값을 각각 받는다)

왜 필요한가
-----------
지금 수요 D 의 절반은 '잠재수요'인데, 이건 거주인구에 연령가중을 걸어 만든
추정치다. "인구가 많으니 수요가 있을 것"이라는 가정이 들어간다.
OD 는 그 가정을 걷어낸다 — 실제로 몇 명이 어디서 어디로 갔는지 카드로 찍힌
값이다. 어느 동에서 나가는 통행이 많은데 버스가 없는지를 직접 말해 준다.

화성시 코드에 대한 함정 (2026-08-11 확인)
-----------------------------------------
sggCd=41590(화성시)으로 조회하면 NOT_FOUND 다. 화성시가 4개 구로 나뉘어
있어서다 — 41591(만세구) 41593 41595 41597. 41590 만 넣고 "데이터가 없다"고
결론내면 안 된다.

수집 범위의 한계 — 반드시 읽을 것
---------------------------------
OD API 는 stgEmdCd 와 arrEmdCd 를 둘 다 필수로 받는다. 생략·와일드카드 모두
ERROR 다. 화성시 법정동은 188개이므로 전조합은 35,344 쌍인데, 인증키 1개당
하루 10,000회 제한이라 하루에 다 못 받는다.
여기서는 시험 삼아 정류장이 많은 상위 TOP_N 개 동의 전조합만 받는다.
따라서 이 파일의 합계를 화성시 전체 통행량으로 읽으면 안 된다.

소요 시간 — 쿼터보다 응답속도가 먼저 걸린다 (2026-08-11 실측)
--------------------------------------------------------------
호출 1회에 중앙 1.5초 걸린다. 쿼터(10,000회/일)보다 이쪽이 훨씬 빡빡하다.

    TOP_N=25   625쌍   약 16분   정류장 커버 약 42%
    TOP_N=50 2,500쌍   약 66분   정류장 커버    61%
    전수     35,344쌍  약 15시간  ← 하루 한도도 넘고 시간도 안 된다

전수가 필요하면 며칠에 걸쳐 밤에 돌려야 한다. 낮에 손으로 확인하며 쓸
용도라면 TOP_N 을 25 안팎으로 두는 게 맞다.

    사용법:  OD_TOP_N=25 python analysis/10_fetch_od.py
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"
KEY = os.getenv("STCIS_KEY", "")
SGG = ["41591", "41593", "41595", "41597"]     # 화성시 4개 구
DATE = os.getenv("OD_DATE", "20260601")        # 월요일. 평일 대표값.
# 요일축 — DATE 하나로 평일/주말 실행을 겸한다. 파일명이 갈라져야 두 번 돌려도
# 서로 덮어쓰지 않는다. 토·일 둘 다 "we" 하나로 묶는다(시간대 배분용 대표값이라
# 토/일을 따로 볼 정밀도까지는 필요 없다 — 배차처럼 공급 자체가 요일별로 갈리는
# 값이 아니다).
_d = date(int(DATE[:4]), int(DATE[4:6]), int(DATE[6:8]))
SFX = "we" if _d.weekday() >= 5 else "wd"
# 호출 1회가 1.5초라 TOP_N 을 올리면 제곱으로 늘어난다. 25 면 약 16분.
TOP_N = int(os.getenv("OD_TOP_N", "25"))
DAILY_CAP = 10000
PAUSE = 0.05

if not KEY:
    sys.exit("STCIS_KEY 가 .env 에 없습니다.")


def api(svc, **kw):
    p = {"apikey": KEY}
    p.update(kw)
    url = f"https://stcis.go.kr/openapi/{svc}.json?" + urllib.parse.urlencode(p)
    rq = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(rq, timeout=45) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                return {"status": "ERROR"}
            time.sleep(1.0 * (attempt + 1))


print("=" * 66)
print("[1] 화성시 법정동 목록 — 정류장 수 기준")
stops, emd_name = [], {}
for sgg in SGG:
    d = api("bussttn", sdCd="41", sggCd=sgg)
    got = d.get("result") or []
    stops += got
    print(f"  {sgg}: 정류장 {len(got):,}개")
from collections import Counter, defaultdict
cnt = Counter(x["emdCd"] for x in stops)
print(f"  법정동 {len(cnt)}개 / 정류장 {len(stops):,}개")

top = [e for e, _ in cnt.most_common(TOP_N)]
pairs = [(a, b) for a in top for b in top]
cover = sum(cnt[e] for e in top) / max(len(stops), 1)
print(f"  상위 {TOP_N}개 동 선택 → {len(pairs):,}쌍 (정류장 커버 {cover:.0%})")
assert len(pairs) <= DAILY_CAP, (
    f"{len(pairs):,}쌍은 일일한도 {DAILY_CAP:,} 초과 — OD_TOP_N 을 줄이세요")

print("=" * 66)
print(f"[2] OD 수집 — {DATE} · {len(pairs):,}쌍")
rows, ok, empty, err = [], 0, 0, 0
t0 = time.time()
for i, (a, b) in enumerate(pairs, 1):
    d = api("quarterod", opratDate=DATE, stgEmdCd=a, arrEmdCd=b)
    st = d.get("status")
    if st == "OK":
        ok += 1
        for r in d.get("result") or []:
            rows.append(r)
            emd_name.setdefault(r["stgEmdCd"], r.get("stgEmdNm", ""))
            emd_name.setdefault(r["arrEmdCd"], r.get("arrEmdNm", ""))
    elif st == "NOT_FOUND":
        empty += 1
    else:
        err += 1
    if i % 50 == 0 or i == len(pairs):
        el = time.time() - t0
        eta = el / i * (len(pairs) - i)
        # flush 를 안 하면 파이프로 넘길 때 출력이 버퍼에 갇혀 진행률이 안 보인다.
        # 한 시간짜리 작업에서 "돌고는 있나"를 확인할 수 없으면 곤란하다.
        print(f"    {i:,}/{len(pairs):,}  성공 {ok:,} 무통행 {empty:,} 오류 {err:,}"
              f"  누적 {len(rows):,}행  경과 {el / 60:.1f}분  남음 {eta / 60:.1f}분",
              flush=True)
    time.sleep(PAUSE)

print("=" * 66)
print("[3] 저장")
with open(D / f"od_quarter_{SFX}.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "stg_emd", "stg_nm", "arr_emd", "arr_nm",
                "hour", "quarter", "trips", "avg_sec"])
    for r in rows:
        w.writerow([r["opratDate"], r["stgEmdCd"], r.get("stgEmdNm", ""),
                    r["arrEmdCd"], r.get("arrEmdNm", ""),
                    r.get("tzon", ""), r.get("quater", ""),
                    r.get("useStf", 0), r.get("useTm", "")])
print(f"  -> od_quarter_{SFX}.csv  {len(rows):,}행")

out = defaultdict(float)
for r in rows:
    out[r["stgEmdCd"]] += float(r.get("useStf") or 0)
meta = {
    "date": DATE, "top_n": TOP_N, "pairs": len(pairs),
    "pairs_with_data": ok, "pairs_empty": empty, "pairs_error": err,
    "rows": len(rows), "total_trips": round(sum(out.values())),
    "stop_coverage": round(cover, 3),
    "sgg_used": SGG,
    "caveat": ("상위 %d개 동 전조합만 받았다. 화성시 전체 통행량이 아니다. "
               "전수는 188x188=35,344 쌍이라 하루 10,000회 제한을 넘는다." % TOP_N),
    "built": time.strftime("%Y-%m-%d %H:%M"),
}
(D / f"od_meta_{SFX}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
print(f"  -> od_meta_{SFX}.json  통행 {meta['total_trips']:,}건")

print("  출발 통행 상위 10개 동:")
for e, v in sorted(out.items(), key=lambda kv: -kv[1])[:10]:
    print(f"    {emd_name.get(e, e):14s} {v:8,.0f} 통행")

assert rows, "OD 를 한 건도 못 받았습니다 — 날짜나 인증키를 확인하세요"
