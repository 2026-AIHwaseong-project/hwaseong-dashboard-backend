"""
정류장 · 승하차 · 노선 · 유동인구를 격자에 조인

    python analysis/03_join.py

산출 (dataset_hwaseong/)
    stops_hwaseong.csv   정류장 마스터 + 평일 일평균 승하차(초승 기준) + 노선수 + 시간대별 운행빈도
    grid_join.csv        격자 × 4시간대 — D·S 의 원재료

04_model.py 가 이걸 받아 D·S·MI·4분면·우선순위를 만듭니다.

---
설계 결정 3가지 (측정하고 정한 것)

[1] 조인 키는 정류장번호. ARS 가 아니다.
    정류장번호↔정류소ID 98.9% > ARS↔정류소번호 98.5% 이고, 무엇보다
    ID 는 전국 유일인데 ARS 는 시군구 안에서만 유일하다.
    외부 노출 id 는 프론트 계약대로 `41590-{ARS}` 를 유지한다.

[2] 시간축은 공급이 만든다. 수요 쪽 이동 대리지표는 만들지 않는다.
    유동인구 CSV 는 체류인구다. 24시간 곡선을 그려보면 정오에 최대(30만),
    새벽 4시에 최소(11만)이고 저녁 유출은 21~01 시에 몰린다.
    버스 퇴근 첨두(17-19)와 형태가 다르다. |Δ체류| 를 이동 대리지표로 써보니
    퇴근 시간대가 0.63%/h 로 주저앉고 0 시가 최상위로 올라왔다.
    없는 데이터를 추정 위에 추정으로 쌓는 셈이라 쓰지 않는다.

    대신 실측인 배차간격으로 공급의 시간 변화를 만든다. night_alloc 이
    146개 노선 중 5개뿐인 것 자체가 "심야엔 버스가 거의 안 다닌다"는 신호다.
    수요 쪽 시간 변화는 연령가중 체류비율에서 약하게만 나온다(z 최대차 0.18).

[3] 노선이 안 붙는 정류장은 운행빈도를 0 이 아니라 결측으로 둔다.
    TAGO 는 승하차 정류장의 74.7% 만 덮는다. 나머지 728개는 실제로 버스가
    오는데(승차량 합계 120만) 노선 정보가 없을 뿐이다. 0 으로 넣으면
    거짓 사각지대가 728개 생긴다. 마을버스 원본에는 경유정류소가 없어
    직접 메울 수 없으므로, 같은 읍면동 중앙값으로 대체하고 그 사실을 센다.

[4] 수요는 `초승` 만 센다. `승차합계` 가 아니다. (2026-08-12 수정)
    승차합계 20,258,919 = 초승 16,504,918(81.5%) + 환승 3,754,001(18.5%).
    환승은 새로 발생한 통행이 아니라 이미 버스에 탄 사람이 갈아탄 것이다.
    이걸 수요로 세면 철도 연계 격자의 D 가 통째로 부풀고, MI 가 따라 부풀어
    추천이 그쪽으로 쏠린다. 실제로 환승비가 병점역후문 71.2% · 어천역 77.1% ·
    야목역 81.7% 이고, 환승비 30% 이상인 66개 정류장이 전체 승차의 21.1% 다.
    "버스를 갈아타는 곳"과 "사람이 버스를 타기 시작하는 곳"은 다른 문제다.

[5] 일평균은 **평일** 기준이다. 119일 통짜 평균이 아니다. (2026-08-12 수정)
    주말 승차는 평일의 68.3% 이고, 그 비율이 정류장마다 크게 다르다 —
    반월중·고등학교앞 0.06 부터 석포산업단지 3.59 까지 약 60배다.
    119일을 통으로 평균내면 평일 기준 대비 중앙 -10.7%(범위 -25%~+47%)로
    어긋난다. am(07-09)·pm(17-19) 은 통근 시간대라 평일이 맞는 자다.
    주말분은 `board_day_we` 로 따로 남긴다 — 요일축을 도입할 때 쓴다.

[6] 승차량도 노선처럼 원본 결측이 있다. (2026-08-13 수정)
    정확도 재검증 중 다사5915(진안동, 인구 15,890명)의 예측오차가 가장 컸다.
    추적해 보니 배정된 정류장 6개(반정아이파크·망포고 등, 전부 노선·배차 실측)가
    경기도 승하차 원본에 **단 한 행도 없었다** — 수원 경계라 수원 쪽 집계로
    빠진 것으로 보인다. "버스는 오는데 카드 데이터만 없다"와 "진짜로 승객이
    없다"(TG·휴게소·차고지처럼 구조적으로 무정차)를 구분하지 않으면 후자를
    지어내는 게 아니라 전자를 놓치는 방향으로 사각지대를 과소평가한다.
    전수 조사: 노선·배차 실측인데 board_day=0 인 정류장 108개 중 60개는
    이름 패턴(미정차·TG·휴게소·차고지·기점)으로 정상 판별되고, 나머지 48개
    (영향 격자 29개)만 결측으로 보아 [3] 과 같은 방식(읍면동 중앙값)으로 채운다.
    freq_imputed 판정이 board_day<=0 을 게이트로 쓰므로([3]), 이 대체는
    반드시 그 판정보다 먼저 실행해야 한다 — 안 그러면 board_day 결측이
    freq_imputed 오판정까지 연쇄로 일으킨다.
"""
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"

SIGUNGU = "41590"          # 화성시 행정표준코드 (프론트 계약의 id 접두어)
WALK_M = 800               # 도보권. 승하차 안분 반경 겸 커버리지 임계거리
PERIODS = [("am", 7, 9), ("day", 9, 17), ("pm", 17, 19), ("night", 22, 24)]
AGE = ["a0009", "a1014", "a1519", "a2024", "a2529", "a3034", "a3539",
       "a4044", "a4549", "a5054", "a5559", "a6064", "a6569", "a70p"]
FLOW_AGE = AGE[:-1]        # 유동인구는 65~69 가 마지막. a70p 는 6569 비율을 대용

to5179 = Transformer.from_crs(4326, 5179, always_xy=True).transform

read = lambda n: list(csv.DictReader(open(D / n, encoding="utf-8-sig")))
gid = lambda v: str(v or "").strip().removeprefix("GGB")


def read_plus(name):
    """09_augment_routes 가 만든 보강본이 있으면 그걸 쓴다.

    화성 면허가 아니라 01_fetch 에서 빠졌던 노선(수원·오산 면허, 마을버스)을
    경기도 BMS 경유정류소로 채운 파일이다. 없으면 원본으로 그대로 돌아간다 —
    09 를 안 돌린 팀원도 파이프라인이 깨지지 않는다."""
    stem, ext = name.rsplit(".", 1)
    p = D / f"{stem}_plus.{ext}"
    if p.exists():
        print(f"  [보강] {p.name} 사용")
        return list(csv.DictReader(open(p, encoding="utf-8-sig")))
    return read(name)


def od_period_share():
    """교통카드 OD(10_fetch_od.py)에서 시간대별 통행 비중을 뽑는다.

    승차량 B 를 시간대에 나누는 배율로 쓴다. 지금까지는 유동인구 시간배율이
    유일한 시간 신호였는데, 그건 '거기 사람이 있다'를 재는 값이라 버스
    이용의 출퇴근 첨두를 못 잡는다 — 실측과 상관계수가 0.42 뿐이고
    출근(07-09)은 7.7% 대 18.4% 로 2.4배 어긋난다.

    ⚠️ 잠재수요 P 에는 쓰지 않는다. 심야 버스가 없는 곳은 태그가 안 찍혀
    OD 가 낮게 나오는데, 그걸 '심야 수요 없음' 으로 읽으면 우리가 찾으려는
    심야 공백을 스스로 지워 버린다. P 는 유동인구 기준 그대로 둔다.

    파일이 없으면 None — 10 을 안 돌린 팀원도 파이프라인이 그대로 돈다."""
    p = D / "od_quarter.csv"
    if not p.exists():
        return None
    hr = defaultdict(float)
    for x in csv.DictReader(open(p, encoding="utf-8-sig")):
        try:
            hr[int(x["hour"])] += float(x["trips"] or 0)
        except (TypeError, ValueError):
            continue          # 시간·통행수가 비어 있는 행은 버린다
    tot = sum(hr.values())
    if tot <= 0:
        return None
    return {pid: sum(hr[h] for h in range(h0, h1)) / tot
            for pid, h0, h1 in PERIODS}


def num(v, default=0.0):
    try:
        f = float(str(v).strip())
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def ars(v):
    """모바일단축번호는 '37539.0' 로, 승하차 정류소번호는 '04074' 로 온다."""
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.lstrip("0") or ""


def hhmm(v):
    """'0430' / '04:30' / 2300 → 분. 실패하면 None."""
    s = str(v or "").strip().replace(":", "")
    if not s.isdigit() or not 3 <= len(s) <= 4:
        return None
    h, m = int(s[:-2]), int(s[-2:])
    return h * 60 + m if 0 <= h <= 30 and m < 60 else None


def is_weekend(v):
    """'20251201' → 토·일이면 True. 읽을 수 없는 값은 평일로 본다 [5]."""
    s = str(v or "").strip()
    if len(s) != 8 or not s.isdigit():
        return False
    return date(int(s[:4]), int(s[4:6]), int(s[6:])).weekday() >= 5


print("=" * 66)
print("[1] 정류장 마스터 (국토부) + 승하차 결합")
stops = {}
for s in read("stops_national_hwaseong.csv"):
    k = gid(s["정류장번호"])
    lon, lat = num(s["경도"]), num(s["위도"])
    if not k or not (126 < lon < 128 and 36 < lat < 38):
        continue
    x, y = to5179(lon, lat)
    stops[k] = {"key": k, "ars": ars(s["모바일단축번호"]), "name": s["정류장명"],
                "lon": round(lon, 6), "lat": round(lat, 6), "x": x, "y": y,
                # 평일(wd)·주말(we) 을 따로 쌓는다 [5]. days 는 집계일수 세기용.
                "board_wd": 0.0, "alight_wd": 0.0, "days_wd": set(),
                "board_we": 0.0, "alight_we": 0.0, "days_we": set(),
                "board_seen": False}   # 원본에 이 정류장 행이 단 한 번이라도 있었는가 [6]
print(f"  좌표 있는 정류장 {len(stops):,}개")

hit = miss = 0
for b in read("boarding_hwaseong.csv"):
    s = stops.get(gid(b["정류소ID"]))
    if s is None:
        miss += 1
        continue
    hit += 1
    s["board_seen"] = True
    d = b["승하차일자"]
    sfx = "we" if is_weekend(d) else "wd"
    s["board_" + sfx] += num(b["초승"])       # [4] 환승 제외 — 초승만이 새 통행이다
    s["alight_" + sfx] += num(b["하차"])
    s["days_" + sfx].add(d)
print(f"  승하차 {hit:,}행 결합 / 미매칭 {miss:,}행 ({hit / (hit + miss):.1%})")

# [5] 평일과 주말은 자가 다르다. 일수도 따로 세야 한다.
n_wd = len({d for s in stops.values() for d in s["days_wd"]}) or 1
n_we = len({d for s in stops.values() for d in s["days_we"]}) or 1
for s in stops.values():
    s["board_day"] = round(s["board_wd"] / n_wd, 2)      # 기준값 = 평일 일평균
    s["alight_day"] = round(s["alight_wd"] / n_wd, 2)
    s["board_day_we"] = round(s["board_we"] / n_we, 2)   # 요일축 도입 대비 보관
print(f"  집계 기간 평일 {n_wd}일 · 주말 {n_we}일 → **평일** 일평균 기준 [5]")
_bwd = sum(s["board_wd"] for s in stops.values()) / n_wd
_bwe = sum(s["board_we"] for s in stops.values()) / n_we
print(f"  초승 일평균  평일 {_bwd:>9,.0f} · 주말 {_bwe:>9,.0f}  (주말/평일 {_bwe / max(_bwd, 1):.3f})")

print("=" * 66)
print("[2] 노선 → 정류장 운행빈도 (시간대별)")
routes = {}
for r in read_plus("routes.csv"):
    peek, npeek, night = num(r["peek_alloc"]), num(r["npeek_alloc"]), num(r["night_alloc"])
    start = hhmm(r["first_time"]) or hhmm(r["up_first"])
    end = hhmm(r["last_time"]) or hhmm(r["up_last"])
    routes[r["route_id"]] = {
        "no": r["route_no"], "peek": peek, "npeek": npeek, "night": night,
        "start": 0 if start is None else start,
        "end": 24 * 60 if end is None else max(end, start or 0),
    }


def trips(rt, h0, h1):
    """이 노선이 [h0,h1) 시간대에 내는 운행횟수. 운행시간과 겹치는 만큼만 센다."""
    lo, hi = max(rt["start"], h0 * 60), min(rt["end"], h1 * 60)
    if hi <= lo:
        return 0.0                                   # 이 시간대엔 안 다님
    if h0 >= 22 or h0 < 6:
        headway = rt["night"] or rt["npeek"] or rt["peek"]
    elif h0 in (7, 17):
        headway = rt["peek"] or rt["npeek"]
    else:
        headway = rt["npeek"] or rt["peek"]
    return (hi - lo) / headway if headway else 0.0


stop_freq = defaultdict(lambda: defaultdict(float))
stop_routes = defaultdict(set)
linked = 0
for l in read_plus("route_stops.csv"):
    rt = routes.get(l["route_id"])
    k = gid(l["node_id"])
    if rt is None or k not in stops:
        continue
    linked += 1
    stop_routes[k].add(rt["no"])
    for pid, h0, h1 in PERIODS:
        stop_freq[k][pid] += trips(rt, h0, h1)

have = len(stop_freq)
print(f"  경유구간 {linked:,}건 결합 → 노선이 붙은 정류장 {have:,}/{len(stops):,} ({have / len(stops):.1%})")
for pid, h0, h1 in PERIODS:
    v = [stop_freq[k][pid] for k in stop_freq]
    print(f"    {pid:5} ({h0:02d}-{h1:02d})  정류장당 평균 운행 {sum(v) / len(v):5.1f}회"
          f" · 운행 0인 정류장 {sum(1 for x in v if x == 0):>4}개")

print("=" * 66)
print("[3] 격자 배정 · 커버리지 · 철도역")
grid = read("grid_hwaseong.csv")
for g in grid:
    g["x"], g["y"] = to5179(num(g["lon"]), num(g["lat"]))
    for k in AGE + ["pop", "workers", "elderly"]:
        g[k] = num(g[k])

rails = [to5179(num(r["역경도"]), num(r["역위도"])) for r in read("rail_stations.csv")
         if num(r["역경도"])]

# 노선 결측 정류장을 읍면동 중앙값으로 메우기 위해 정류장 → 격자 먼저 배정
gxy = [(g["x"], g["y"]) for g in grid]


def nearest_grid(x, y):
    best, bd = None, 1e18
    for i, (gx, gy) in enumerate(gxy):
        d = (gx - x) ** 2 + (gy - y) ** 2
        if d < bd:
            best, bd = i, d
    return best, math.sqrt(bd)


for s in stops.values():
    i, d = nearest_grid(s["x"], s["y"])
    s["grid_id"] = grid[i]["grid_id"]
    s["region"] = grid[i]["region"]
    s["grid_m"] = round(d, 1)
    s["n_routes"] = len(stop_routes.get(s["key"], ()))

# [6] 승차량 결측 대체 — freq 대체([3], 바로 아래)보다 먼저 해야 한다.
# TG·휴게소·차고지·기점·미정차 는 버스가 서지 않거나 승객을 안 태우는 지점이라
# 원본에 없는 게 정상이다 — 대체 대상에서 뺀다.
NONBOARDING_NAME = ("미정차", "TG", "휴게소", "차고지", "기점")
bmed, amed = defaultdict(list), defaultdict(list)
for s in stops.values():
    if s["board_seen"]:
        bmed[s["region"]].append(s["board_day"])
        amed[s["region"]].append(s["alight_day"])


def _median(vals):
    v = sorted(vals)
    return v[len(v) // 2] if v else 0.0


board_imputed = 0
for s in stops.values():
    if s["board_seen"] or any(k in s["name"] for k in NONBOARDING_NAME):
        s["board_imputed"] = 0
        continue
    if s["key"] not in stop_freq:
        s["board_imputed"] = 0   # 노선도 없는 곳은 board_day=0 그대로 — 진짜 무공급 후보
        continue
    board_imputed += 1
    s["board_imputed"] = 1
    fill = _median(bmed[s["region"]])
    s["board_day"] = fill
    s["board_day_we"] = fill      # 주말분도 원본이 없으므로 같은 값으로 채운다
    # 하차도 같은 이유로 결측이다. 승차만 채우면 '승차는 있는데 하차가 0' 인
    # 정류장이 32개 새로 생기고, 프로파일 카드에서 하차 막대만 통째로 빈다.
    s["alight_day"] = _median(amed[s["region"]])
print(f"  승차량 결측 정류장 {board_imputed:,}개(노선·배차 실측인데 원본에 없음)를 읍면동 중앙값으로 대체")

# [3] 결측 대체 — 같은 읍면동에서 노선이 붙은 정류장들의 중앙값
med = defaultdict(lambda: defaultdict(list))
for s in stops.values():
    if s["key"] in stop_freq:
        for pid, _, _ in PERIODS:
            med[s["region"]][pid].append(stop_freq[s["key"]][pid])
imputed = 0
for s in stops.values():
    if s["key"] in stop_freq:
        s["freq_imputed"] = 0
        continue
    if s["board_day"] <= 0:
        s["freq_imputed"] = 0            # 승하차도 없으면 진짜로 버스가 안 오는 곳
        continue
    imputed += 1
    s["freq_imputed"] = 1
    for pid, _, _ in PERIODS:
        v = sorted(med[s["region"]][pid])
        stop_freq[s["key"]][pid] = v[len(v) // 2] if v else 0.0
print(f"  노선 결측 정류장 {imputed:,}개를 읍면동 중앙값으로 대체")

# 정류장 영향을 도보권 800m 안 격자들에 뿌린다.
#
# 승차량과 운행빈도는 성격이 달라 배분 방식이 다르다.
#   승차량 — 보존해야 하는 총량. 정류장 하나의 승차를 주변 격자에 나눠 담고
#            가중치 합이 정류장마다 1 이 되게 한다. (격자 기준으로 정규화하면
#            총량이 안 맞는다 — 실측 9.1% 만 남았다)
#   운행빈도 — 보존 대상이 아니다. 도보권에 정류장이 둘이면 둘 다 탈 수 있으므로
#            거리가중 합으로 쌓는다.
import shapely.geometry as sgeom

slist = list(stops.values())
stree = STRtree([sgeom.Point(s["x"], s["y"]) for s in slist])
gtree = STRtree([sgeom.Point(g["x"], g["y"]) for g in grid])

board_g = defaultdict(float)
freq_g = defaultdict(lambda: defaultdict(float))
lost = 0
for s in slist:
    near = [(i, math.dist((s["x"], s["y"]), (grid[i]["x"], grid[i]["y"])))
            for i in gtree.query(sgeom.Point(s["x"], s["y"]).buffer(WALK_M))]
    near = [(i, d) for i, d in near if d <= WALK_M]
    if not near:                                   # 도보권에 격자 중심이 없으면 최근접 하나로
        i, d = nearest_grid(s["x"], s["y"])
        near = [(i, min(d, WALK_M - 1))]
        lost += 1
    wsum = sum(1 - d / WALK_M for _, d in near) or 1.0
    for i, d in near:
        w = (1 - d / WALK_M) / wsum
        board_g[grid[i]["grid_id"]] += s["board_day"] * w
        for pid, _, _ in PERIODS:
            freq_g[grid[i]["grid_id"]][pid] += stop_freq[s["key"]][pid] * (1 - d / WALK_M)
if lost:
    print(f"  도보권에 격자 중심이 없어 최근접으로 보낸 정류장 {lost:,}개")

rows = []
for g in grid:
    near = [(slist[i], math.dist((g["x"], g["y"]), (slist[i]["x"], slist[i]["y"])))
            for i in stree.query(sgeom.Point(g["x"], g["y"]).buffer(WALK_M))]
    near = [(s, d) for s, d in near if d <= WALK_M]
    # 거리(커버리지용)와 ID(화면 드릴다운용)는 기준이 다르다.
    #
    #   nearest_stop_m  — 물리적 접근성. 정류장이 거기 있으면 ID 유무와 무관하다.
    #   nearest_stop_id — 프론트가 /stops/{id}/profile 로 조회하는 값. ARS 가 없는
    #                     정류장(292개, 9.2% · 대부분 승하차 0)을 가리키면 링크가 끊긴다.
    #
    # 그래서 거리는 전체에서, ID 는 ARS 가 있는 정류장 중에서 고른다.
    if near:
        nearest_m = min(d for _, d in near)
    else:                                          # 도보권에 정류장이 없는 격자
        nearest_m = min(math.dist((g["x"], g["y"]), (s["x"], s["y"])) for s in slist)
    coverage = max(0.05, min(1.0, 1 - nearest_m / WALK_M))

    with_ars = [(s, d) for s, d in near if s["ars"]]
    s_min = (min(with_ars, key=lambda t: t[1])[0] if with_ars else
             min(((s, math.dist((g["x"], g["y"]), (s["x"], s["y"]))) for s in slist if s["ars"]),
                 key=lambda t: t[1])[0])
    rail_m = min((math.dist((g["x"], g["y"]), r) for r in rails), default=None)

    for pid, h0, h1 in PERIODS:
        rows.append({
            "grid_id": g["grid_id"], "period": pid,
            # 일 승차를 시간대로 쪼갠다. 배율은 [4] 에서 연령가중으로 채운다.
            "board_day": round(board_g[g["grid_id"]], 3),
            "freq": round(freq_g[g["grid_id"]][pid], 3),
            "coverage": round(coverage, 4),
            "nearest_stop_id": f"{SIGUNGU}-{s_min['ars']}" if s_min["ars"] else "",
            "nearest_stop_m": round(nearest_m, 1),
            "rail_m": round(rail_m, 1) if rail_m else "",
            "stops_n": len(near),
        })
print(f"  {len(grid):,}격자 × {len(PERIODS)}시간대 = {len(rows):,}행")

print("=" * 66)
print("[4] 잠재수요 — 연령가중 시간배율")
flow = defaultdict(float)
for x in read("flow_hourly.csv"):
    h = int(x["시간코드"])
    for i, b in enumerate(FLOW_AGE):
        tag = b[1:]                                  # a2024 → 2024
        flow[(b, h)] += num(x["남자" + tag + "수"]) + num(x["여자" + tag + "수"])
day_tot = {b: sum(flow[(b, h)] for h in range(24)) or 1 for b in FLOW_AGE}
share = {(b, pid): sum(flow[(b, h)] for h in range(h0, h1)) / day_tot[b]
         for b in FLOW_AGE for pid, h0, h1 in PERIODS}
for pid, _, _ in PERIODS:                            # a70p 는 6569 비율을 대용
    share[("a70p", pid)] = share[("a6569", pid)]

by_grid = {g["grid_id"]: g for g in grid}

# SGIS 는 소수 인원 격자의 연령 내역을 비공개 처리한다. 인구는 있는데 연령대가
# 전부 0 인 격자가 4개(23명) 있어, 시 전체 연령분포로 채운다.
city = {b: sum(g[b] for g in grid) for b in AGE}
city_tot = sum(city.values()) or 1
filled = 0
for g in grid:
    if g["pop"] > 0 and sum(g[b] for b in AGE) == 0:
        filled += 1
        for b in AGE:
            g[b] = g["pop"] * city[b] / city_tot
if filled:
    print(f"  연령 내역이 비공개된 격자 {filled}개를 시 전체 분포로 보정")

for r in rows:
    g = by_grid[r["grid_id"]]
    pid = r["period"]
    r["potential"] = round(sum(g[b] * share[(b, pid)] for b in AGE), 2)
    # 연령가중 시간배율. 격자마다 연령 구성이 달라 값이 다르다.
    # ⚠️ 추정치다. API 응답에 isEstimated: true 로 표시해야 한다.
    r["_pshare"] = sum(g[b] * share[(b, pid)] for b in AGE) / (g["pop"] or 1)
    r["elderly_ratio"] = g["elderly_ratio"]
    r["workers"] = g["workers"]
    r["pop"] = g["pop"]

# [4-1] 승차량의 시간 신호만 교통카드 OD 실측으로 교정한다.
# 격자별 연령 구성에서 오는 상대차는 그대로 두고(=_pshare 유지), 시 전체 합이
# OD 실측 비중과 맞도록 시간대마다 한 배율만 곱한다. 잠재수요 P 는 안 건드린다.
odsh = od_period_share()
kfac = {pid: 1.0 for pid, _, _ in PERIODS}
if odsh:
    for pid, _, _ in PERIODS:
        sub = [r for r in rows if r["period"] == pid]
        base = sum(r["board_day"] for r in sub)
        cur = (sum(r["board_day"] * r["_pshare"] for r in sub) / base) if base else 0.0
        if cur > 0:
            kfac[pid] = odsh[pid] / cur
    print("  [OD] 승차량 시간배율 교정 — 유동인구 → 교통카드 실측")
    for pid, _, _ in PERIODS:
        print(f"    {pid:6s} ×{kfac[pid]:5.2f}  (OD 실측 {odsh[pid] * 100:4.1f}%)")
else:
    print("  [OD] od_quarter.csv 없음 — 승차량 시간배율은 유동인구 기준 그대로")

for r in rows:
    r["boardings"] = round(r["board_day"] * r["_pshare"] * kfac[r["period"]], 3)

print("  시간대별 합 (연령가중 배율 적용)")
for pid, h0, h1 in PERIODS:
    p = sum(r["potential"] for r in rows if r["period"] == pid)
    b = sum(r["boardings"] for r in rows if r["period"] == pid)
    print(f"    {pid:5} ({h0:02d}-{h1:02d})  잠재 {p:>11,.0f}   승차 {b:>9,.0f}")

print("=" * 66)
print("[5] 저장")
with open(D / "stops_hwaseong.csv", "w", encoding="utf-8-sig", newline="") as f:
    cols = ["key", "stop_id", "ars", "name", "lon", "lat", "grid_id", "region",
            "board_day", "board_day_we", "alight_day", "board_imputed",
            "n_routes", "freq_imputed"] + [f"freq_{p}" for p, _, _ in PERIODS]
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for s in slist:
        w.writerow({**s, "stop_id": f"{SIGUNGU}-{s['ars']}" if s["ars"] else "",
                    **{f"freq_{p}": round(stop_freq[s["key"]][p], 3) for p, _, _ in PERIODS}})
print(f"  -> stops_hwaseong.csv  {len(slist):,}행")

with open(D / "grid_join.csv", "w", encoding="utf-8-sig", newline="") as f:
    cols = ["grid_id", "period", "boardings", "potential", "board_day", "freq", "coverage",
            "nearest_stop_id", "nearest_stop_m", "rail_m", "stops_n",
            "pop", "workers", "elderly_ratio"]
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
print(f"  -> grid_join.csv  {len(rows):,}행")

print("=" * 66)
print("[6] 검증")
am = [r for r in rows if r["period"] == "am"]
tot_b = sum(s["board_day"] for s in slist)
alloc_b = sum(r["board_day"] for r in am)
print(f"  일평균 승차 정류장 합 {tot_b:,.0f} → 격자 배분 합 {alloc_b:,.0f} ({alloc_b / tot_b:.1%})")
print(f"  도보권에 정류장 없는 격자 {sum(1 for r in am if r['stops_n'] == 0):>4}개 / {len(am)}")
print(f"  커버리지 중앙값 {sorted(r['coverage'] for r in am)[len(am) // 2]:.3f}")
print("\n  시간대별 운행빈도 합 — 여기서 시간축이 나옵니다")
base = None
for pid, h0, h1 in PERIODS:
    v = sum(r["freq"] for r in rows if r["period"] == pid)
    base = base or v
    print(f"    {pid:5} ({h0:02d}-{h1:02d})  {v:>10,.0f}  기준대비 {v / base:5.2f}배")

night = sum(r["freq"] for r in rows if r["period"] == "night")
assert alloc_b / tot_b > 0.9, f"승차 배분 손실 {1 - alloc_b / tot_b:.1%} — 도보권 밖 정류장이 너무 많습니다"
assert night < base * 0.6, "심야 운행빈도가 출근과 비슷합니다 — 배차 시간대 분기를 확인하세요"
assert all(r["potential"] > 0 for r in am if r["pop"] > 0), "인구가 있는데 잠재수요가 0인 격자가 있습니다"
print("\n  ✅ 통과")
