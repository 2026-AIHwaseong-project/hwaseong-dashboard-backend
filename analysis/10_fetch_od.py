# -*- coding: utf-8 -*-
"""
교통카드 빅데이터(STCIS) 15분단위 OD 수집 — 잠재수요를 실측으로 바꾸기 위한 시험 수집

    출력  od_quarter_{wd|we}.csv   출발동 · 도착동 · 시간 · 15분 · 통행수 · 평균통행시간
          od_meta_{wd|we}.json     수집 범위와 한계
          od_seen_{wd|we}.json     이미 확정 응답(OK/NOT_FOUND) 받은 쌍 장부 — 이어받기용
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

이어받기 (2026-08-23 추가) — 지속 호출 자체가 불안정하다
--------------------------------------------------------------
burst 20쌍은 항상 0.8~0.9초/호출로 깨끗한데, 실제로 지속 100쌍을 돌리면
평균 18.8초/호출까지 늘어지고 오류가 26% 났다(같은 세션, 같은 키, 사전
헬스체크는 멀쩡했음). 물량이 아니라 **한 프로세스가 오래 붙어있는 것**
자체가 문제로 보인다.

그래서 이 스크립트는 이어받기를 한다 — od_seen_{sfx}.json 에 이미 확정
응답(OK 든 NOT_FOUND 든)을 받은 쌍을 적어 두고, 다음 실행에서는 그 쌍을
건너뛴다. 오류로 끝난 쌍은 확정이 아니므로 장부에 안 남고 다음 실행에서
자동으로 다시 시도된다. OD_TOP_N 을 조금씩 올려가며(10 → 15 → 20 → 25)
여러 번, 시간을 두고 짧게 돌리면 매번 그만큼만 늘어난 쌍을 받는다 — top
N 이 항상 같은 순위 기준이라 작은 N 의 결과가 큰 N 의 부분집합이 된다.

    사용법:  OD_TOP_N=10 python analysis/10_fetch_od.py   (오늘 · 100쌍)
             OD_TOP_N=15 python analysis/10_fetch_od.py   (나중에 · 10 은 건너뛰고 125쌍만)
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
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

CSV_PATH = D / f"od_quarter_{SFX}.csv"
META_PATH = D / f"od_meta_{SFX}.json"
SEEN_PATH = D / f"od_seen_{SFX}.json"
CSV_HEADER = ["date", "stg_emd", "stg_nm", "arr_emd", "arr_nm",
              "hour", "quarter", "trips", "avg_sec"]

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


def pair_key(a, b):
    return f"{a}|{b}"


# ─── 이어받기 장부 ───────────────────────────────────────────────────────────
# od_seen_{sfx}.json 이 없는데 od_quarter_{sfx}.csv 는 있는 경우(장부 도입 전에
# 받아 둔 결과물) — CSV 에 이미 있는 쌍은 OK 로 역산해 채운다. 안 그러면 이번
# 실행이 그 쌍들을 다시 받아 CSV 에 같은 쌍이 중복으로 쌓인다. NOT_FOUND 였던
# 쌍은 CSV 에 흔적이 없어 복원 못 하고 한 번 더 확인하게 되는데, 그건 헛수고가
# 아니라 안전한 재확인이다(빨라서 비용도 작다).
seen = {}
if SEEN_PATH.exists():
    seen = json.loads(SEEN_PATH.read_text("utf-8"))
elif CSV_PATH.exists():
    for r in csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")):
        seen[pair_key(r["stg_emd"], r["arr_emd"])] = "OK"
    if seen:
        print(f"[이어받기] {SEEN_PATH.name} 이 없어 {CSV_PATH.name} 에서 {len(seen):,}쌍을 역산했습니다.")


print("=" * 66)
print("[1] 화성시 법정동 목록 — 정류장 수 기준")
stops = []
for sgg in SGG:
    d = api("bussttn", sdCd="41", sggCd=sgg)
    got = d.get("result") or []
    stops += got
    print(f"  {sgg}: 정류장 {len(got):,}개")
cnt = Counter(x["emdCd"] for x in stops)
print(f"  법정동 {len(cnt)}개 / 정류장 {len(stops):,}개")

top = [e for e, _ in cnt.most_common(TOP_N)]
pairs = [(a, b) for a in top for b in top]
to_fetch = [(a, b) for a, b in pairs if pair_key(a, b) not in seen]
cover = sum(cnt[e] for e in top) / max(len(stops), 1)
print(f"  상위 {TOP_N}개 동 선택 → {len(pairs):,}쌍 (정류장 커버 {cover:.0%})"
      f" · 이미 확정 {len(pairs) - len(to_fetch):,}쌍 · 이번에 받을 쌍 {len(to_fetch):,}개")
assert len(to_fetch) <= DAILY_CAP, (
    f"이번에 받을 {len(to_fetch):,}쌍은 일일한도 {DAILY_CAP:,} 초과 — OD_TOP_N 을 줄이세요")

print("=" * 66)
if not to_fetch:
    print("[2] 받을 쌍이 없습니다 — 이미 전부 확정된 범위입니다. OD_TOP_N 을 올려서 범위를 넓히세요.")
else:
    print(f"[2] OD 수집 — {DATE} · {len(to_fetch):,}쌍")

# 호출마다 바로 CSV 에 append 하고 장부를 다시 씁니다 — 끝나고 한꺼번에 쓰면,
# 이 스크립트가 막으려는 바로 그 상황(지속 호출 중 멈춤)에서 process 를 죽이는
# 순간 지금까지 받은 것까지 전부 날아갑니다. 호출 1회가 초 단위라 매번 쓰는
# 비용은 무시할 만합니다.
write_header = not CSV_PATH.exists()
csv_f = open(CSV_PATH, "a", encoding="utf-8-sig", newline="")
w = csv.writer(csv_f)
if write_header:
    w.writerow(CSV_HEADER)

new_row_count, ok, empty, err = 0, 0, 0, 0
t0 = time.time()
for i, (a, b) in enumerate(to_fetch, 1):
    d = api("quarterod", opratDate=DATE, stgEmdCd=a, arrEmdCd=b)
    st = d.get("status")
    if st == "OK":
        ok += 1
        seen[pair_key(a, b)] = "OK"
        for r in d.get("result") or []:
            w.writerow([r["opratDate"], r["stgEmdCd"], r.get("stgEmdNm", ""),
                        r["arrEmdCd"], r.get("arrEmdNm", ""),
                        r.get("tzon", ""), r.get("quater", ""),
                        r.get("useStf", 0), r.get("useTm", "")])
            new_row_count += 1
        csv_f.flush()
        SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    elif st == "NOT_FOUND":
        empty += 1
        seen[pair_key(a, b)] = "NOT_FOUND"
        SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    else:
        err += 1
        # 확정 응답이 아니므로 장부에 안 남긴다 — 다음 실행이 다시 시도한다.
    if i % 50 == 0 or i == len(to_fetch):
        el = time.time() - t0
        eta = el / i * (len(to_fetch) - i)
        # flush 를 안 하면 파이프로 넘길 때 출력이 버퍼에 갇혀 진행률이 안 보인다.
        print(f"    {i:,}/{len(to_fetch):,}  성공 {ok:,} 무통행 {empty:,} 오류 {err:,}"
              f"  누적 {new_row_count:,}행  경과 {el / 60:.1f}분  남음 {eta / 60:.1f}분",
              flush=True)
    time.sleep(PAUSE)
csv_f.close()

print("=" * 66)
print("[3] 저장")
print(f"  -> {CSV_PATH.name}  이번에 {new_row_count:,}행 추가 (호출마다 이미 반영됨)")
resolved_ok = sum(1 for v in seen.values() if v == "OK")
resolved_empty = sum(1 for v in seen.values() if v == "NOT_FOUND")
print(f"  -> {SEEN_PATH.name}  누적 확정 {len(seen):,}쌍 (성공 {resolved_ok:,} · 무통행 {resolved_empty:,})")

# 누적 통계는 전체 CSV 를 다시 읽어 계산한다 — 몇 번을 나눠 받았든 항상 정확하다.
all_rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
emd_name, out = {}, defaultdict(float)
for r in all_rows:
    emd_name.setdefault(r["stg_emd"], r.get("stg_nm", ""))
    emd_name.setdefault(r["arr_emd"], r.get("arr_nm", ""))
    out[r["stg_emd"]] += float(r.get("trips") or 0)

meta = {
    "date": DATE, "top_n": TOP_N, "pairs": len(pairs),
    "pairs_resolved": len(seen), "pairs_remaining": len(pairs) - len(seen),
    "pairs_with_data": resolved_ok, "pairs_empty": resolved_empty,
    "pairs_error_this_run": err,
    "rows": len(all_rows), "total_trips": round(sum(out.values())),
    "stop_coverage": round(cover, 3),
    "sgg_used": SGG,
    "caveat": ("상위 %d개 동 전조합만 받았다. 화성시 전체 통행량이 아니다. "
               "전수는 188x188=35,344 쌍이라 하루 10,000회 제한을 넘는다." % TOP_N),
    "built": time.strftime("%Y-%m-%d %H:%M"),
}
META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"  -> {META_PATH.name}  누적 통행 {meta['total_trips']:,}건"
      f" ({len(seen):,}/{len(pairs):,}쌍 확정, TOP_N={TOP_N} 기준)")

print("  누적 출발 통행 상위 10개 동:")
for e, v in sorted(out.items(), key=lambda kv: -kv[1])[:10]:
    print(f"    {emd_name.get(e, e):14s} {v:8,.0f} 통행")

assert all_rows, "OD 를 한 건도 못 받았습니다 — 날짜나 인증키를 확인하세요"
