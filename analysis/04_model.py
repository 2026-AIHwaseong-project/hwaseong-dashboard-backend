"""
수요 D · 공급 S · 미스매칭 MI · 4분면 · 우선순위

    python analysis/04_model.py

입력  grid_join.csv       (03_join.py 산출, 격자 786 × 4시간대)
산출  grid_metrics.csv    격자 × 시간대 — d/s/z/mi/quad/priority/bins
      norm_stats.json     ★ 정규화 기준통계 (고정값)

기획서 §5 수식과 프론트 assets/js/mock.js 의 임계값을 그대로 옮깁니다.

---
[1] 정규화 기준통계를 고정하는 이유 ★ 이 파일에서 제일 중요합니다

프론트는 배치를 놓을 때마다 POST /simulations 를 부르고, 서버는 공급 S 를
다시 계산합니다. 이때 z 의 평균·표준편차를 매번 다시 구하면 배치 전과 후를
**서로 다른 자로 재는 셈**이 되어 KPI 비교가 통째로 무의미해집니다.

  배치 전  z = (S - 평균A) / 편차A
  배치 후  z = (S - 평균B) / 편차B      ← 평균이 올라가서 z 는 그대로일 수 있음

그래서 "배치 없음" 상태에서 한 번 구한 값을 norm_stats.json 에 박아두고
이후 모든 재계산이 이 값을 씁니다. 담당 B 는 이 파일을 mart_norm_stats
테이블에 넣고 시뮬레이션에서 반드시 참조해야 합니다.

[2] 기준통계는 시간대별로 따로 잡습니다
프론트 mock.js 가 NORM[t] 로 시간대마다 따로 들고 있어 그 계약을 따릅니다.
따라서 MI 는 "그 시간대 안에서 상대적으로 못 받는 정도" 입니다.
심야에 전체 운행이 줄어드는 것 자체는 z 에서 상쇄되고, **줄어드는 정도가
격자마다 다른 것**이 사각지대 구성을 바꿉니다.

[3] 정규화는 log1p 후 min-max 입니다
승차량이 극단적으로 치우쳐 있습니다(상위 격자 10,671 대 중앙값 수십).
그냥 min-max 하면 동탄 몇 칸만 1 에 가깝고 나머지가 전부 0 근처로 뭉개져
농촌 격자끼리 구분이 사라집니다. 프로젝트 초기에 인구를 그대로 썼다가
농어촌이 구조적으로 배제됐던 것과 같은 함정입니다.
"""
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"
PERIODS = ["am", "day", "pm", "night"]

# 기획서 §5.2·5.3 가중치
W_BOARD, W_POTENTIAL = 0.5, 0.5
W_FREQ, W_COVERAGE = 0.78, 0.22
ELDERLY_W = 1.6                       # §5.6 고령 가중
MI_CLIP = 2.6

# 프론트 cells[].flowTripsPerDay 용. 모델 계산에는 안 쓰이고 화면 표시 전용입니다.
#
# ⚠️ 둘 다 가정값입니다. 사업비 단가와 같은 성격이라 화면에 표시해야 합니다.
#
#   TRIP_RATE  1인 1일 통행수. 도시교통 표준 원단위. **전 수단** 기준입니다.
#   BUS_SHARE  그중 버스 분담률. 화성시는 넓고 자가용 의존이 높아 낮게 잡습니다.
#
# 처음에 TRIP_RATE 만 곱했다가 사각지대 잠재수요가 132만 통행/일로 나왔습니다.
# 화성시 실제 버스 승차가 일 169,026 인데 사각지대 하나가 그 8배일 수 없습니다.
# 전 수단 통행을 버스 통행으로 착각한 것이었습니다.
TRIP_RATE = 2.5
BUS_SHARE = 0.10
BUS_TRIP_RATE = TRIP_RATE * BUS_SHARE       # 인구 1명당 일 0.25 버스통행
MI_BINS = [-1.5, -0.8, -0.25, 0.25, 0.8, 1.5]   # 7단계 발산형(0~6). 3이 균형

# §5.5 4분면 — 프론트 docs/API.md §3.2 와 같은 값이어야 합니다
Q = {"need_zd": 0.20, "need_mi": 0.55, "over_zd": -0.30, "over_zs": 0.30,
     "drt_zd": -0.35, "drt_zs": -0.35, "drt_flow_pct": 30, "ok_zd": 0.25, "ok_zs": 0.25}

num = lambda v: float(v) if str(v).strip() not in ("", "None", "nan") else 0.0


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def norm_stats(vals):
    """log1p 후 min-max. 되돌릴 수 있게 파라미터를 남긴다."""
    lg = [math.log1p(max(0.0, v)) for v in vals]
    return {"lo": min(lg), "hi": max(lg)}


def norm(v, st):
    lg = math.log1p(max(0.0, v))
    span = st["hi"] - st["lo"]
    return 0.0 if span <= 0 else min(1.0, max(0.0, (lg - st["lo"]) / span))


def zstats(vals):
    m = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
    return {"mean": m, "sd": sd or 1.0}


z = lambda v, st: (v - st["mean"]) / st["sd"]


def bin_of(v, edges):
    b = 0
    while b < len(edges) and v >= edges[b]:
        b += 1
    return b


rows = list(csv.DictReader(open(D / "grid_join.csv", encoding="utf-8-sig")))
by_period = defaultdict(list)
for r in rows:
    by_period[r["period"]].append(r)

print("=" * 66)
print("[1] 기준통계 산출 — 배치 없음 상태에서 한 번만")
NORM = {}
for p in PERIODS:
    rs = by_period[p]
    board = [num(r["boardings"]) for r in rs]
    poten = [num(r["potential"]) for r in rs]
    freq = [num(r["freq"]) for r in rs]

    st = {"board": norm_stats(board), "potential": norm_stats(poten), "freq": norm_stats(freq)}
    dv = [W_BOARD * norm(b, st["board"]) + W_POTENTIAL * norm(q, st["potential"])
          for b, q in zip(board, poten)]
    sv = [W_FREQ * norm(f, st["freq"]) + W_COVERAGE * num(r["coverage"])
          for f, r in zip(freq, rs)]

    st["zD"], st["zS"] = zstats(dv), zstats(sv)
    st["dRef"] = pct(sorted(dv), 55)                      # §5.4 감쇠항 기준
    st["flowP30"] = pct(sorted(poten), Q["drt_flow_pct"])  # §5.5 DRT 조건
    st["dq"] = [pct(sorted(dv), q) for q in (20, 40, 60, 80)]     # 5분위 경계
    st["sq"] = [pct(sorted(sv), q) for q in (20, 40, 60, 80)]
    st["fq"] = [pct(sorted(poten), q) for q in (20, 40, 60, 80)]
    NORM[p] = st
    print(f"  {p:5} zD(μ={st['zD']['mean']:.3f} σ={st['zD']['sd']:.3f})"
          f"  zS(μ={st['zS']['mean']:.3f} σ={st['zS']['sd']:.3f})  dRef={st['dRef']:.3f}")

print("=" * 66)
print("[2] D · S · MI · 4분면 · 우선순위")
out = []
for p in PERIODS:
    st = NORM[p]
    for r in by_period[p]:
        d = W_BOARD * norm(num(r["boardings"]), st["board"]) \
            + W_POTENTIAL * norm(num(r["potential"]), st["potential"])
        s = W_FREQ * norm(num(r["freq"]), st["freq"]) + W_COVERAGE * num(r["coverage"])
        zd, zs = z(d, st["zD"]), z(s, st["zS"])

        # §5.4 — 수요가 미미한 격자가 상대적 공급부족만으로 새빨개지는 걸 막는 감쇠항
        atten = min(1.0, max(0.0, d / st["dRef"])) ** 0.65 if st["dRef"] > 0 else 0.0
        mi = max(-MI_CLIP, min(MI_CLIP, (zd - zs) * atten))

        if zd >= Q["need_zd"] and mi >= Q["need_mi"]:
            quad = "need"
        elif zd <= Q["over_zd"] and zs >= Q["over_zs"]:
            quad = "over"
        elif zd <= Q["drt_zd"] and zs <= Q["drt_zs"] and num(r["potential"]) >= st["flowP30"]:
            quad = "drt"
        elif zd >= Q["ok_zd"] and zs >= Q["ok_zs"]:
            quad = "ok"
        else:
            quad = "mid"

        er = num(r["elderly_ratio"])
        priority = round(mi * (0.35 + d) * (1 + ELDERLY_W * er), 4) if quad == "need" else 0.0
        # 커버리지가 낮으면 정류장이 없는 것이고, 높으면 버스가 안 오는 것이다
        action = ("NEW_STOP" if num(r["coverage"]) < 0.5 else "ADD_FREQ") if quad == "need" \
            else ("DRT" if quad == "drt" else "")

        out.append({
            "grid_id": r["grid_id"], "period": p,
            "d_score": round(d * 100, 2), "s_score": round(s * 100, 2),
            "z_demand": round(zd, 4), "z_supply": round(zs, 4),
            "mi": round(mi, 4), "quad": quad, "action": action, "priority": priority,
            "coverage": round(num(r["coverage"]), 4),
            "flow_trips": round(num(r["potential"]), 2),
            "flow_trips_per_day": round(num(r["pop"]) * BUS_TRIP_RATE, 1),
            "elderly_ratio": round(er, 4),
            "boardings": round(num(r["boardings"]), 2),
            "freq": round(num(r["freq"]), 2),
            "nearest_stop_id": r["nearest_stop_id"],
            "nearest_stop_m": r["nearest_stop_m"],
            "bin_mi": bin_of(mi, MI_BINS),
            "bin_demand": bin_of(d, st["dq"]),
            "bin_supply": bin_of(s, st["sq"]),
            "bin_flow": bin_of(num(r["potential"]), st["fq"]),
        })

print("=" * 66)
print("[3] 4분면 분포")
print(f"  {'시간대':6} {'need':>6} {'over':>6} {'drt':>6} {'ok':>6} {'mid':>6}   need 비중")
for p in PERIODS:
    rs = [o for o in out if o["period"] == p]
    c = defaultdict(int)
    for o in rs:
        c[o["quad"]] += 1
    print(f"  {p:6} {c['need']:>6} {c['over']:>6} {c['drt']:>6} {c['ok']:>6} {c['mid']:>6}"
          f"   {c['need'] / len(rs):>7.1%}")

print("=" * 66)
print("[4] 우선순위 Top 10 (출근)")
top = sorted((o for o in out if o["period"] == "am" and o["priority"] > 0),
             key=lambda o: -o["priority"])[:10]
reg = {r["grid_id"]: r for r in csv.DictReader(open(D / "grid_hwaseong.csv", encoding="utf-8-sig"))}
for i, o in enumerate(top, 1):
    g = reg[o["grid_id"]]
    print(f"  {i:2}. {o['grid_id']} {g['region']:7} 점수 {o['priority']:6.3f}"
          f"  MI {o['mi']:+.2f}  고령 {o['elderly_ratio']:5.1%}  {o['action']}")

print("=" * 66)
print("[5] 저장")
cols = list(out[0].keys())
with open(D / "grid_metrics.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(out)
print(f"  -> grid_metrics.csv  {len(out):,}행")

(D / "norm_stats.json").write_text(json.dumps({
    "note": "배치 없음 상태에서 한 번 산출한 고정값. 시뮬레이션 재계산은 반드시 이 값을 쓸 것.",
    "generated_by": "analysis/04_model.py",
    "weights": {"board": W_BOARD, "potential": W_POTENTIAL,
                "freq": W_FREQ, "coverage": W_COVERAGE, "elderly": ELDERLY_W},
    "quadrant": Q, "mi_clip": MI_CLIP, "mi_bins": MI_BINS,
    "trip_rate": {"trip_rate": TRIP_RATE, "bus_share": BUS_SHARE,
                  "bus_trip_rate": BUS_TRIP_RATE, "assumed": True,
                  "note": "1인 1일 버스통행 = 전수단 원단위 2.5 × 버스분담률 0.10. "
                          "둘 다 가정값이며 실측 아님. 화면에 표시할 것."},
    "periods": NORM,
}, ensure_ascii=False, indent=1), encoding="utf-8")
print("  -> norm_stats.json")

print("=" * 66)
print("[6] 검증")
for p in PERIODS:
    rs = [o for o in out if o["period"] == p]
    need = [o for o in rs if o["quad"] == "need"]
    share = len(need) / len(rs)
    assert 0.02 <= share <= 0.25, f"{p} need 비중 {share:.1%} — 2~25% 밖입니다. 임계값 확인"
assert all(o["priority"] == 0 for o in out if o["quad"] != "need"), "need 아닌데 우선순위가 있습니다"
assert all(-MI_CLIP <= o["mi"] <= MI_CLIP for o in out), "MI 클리핑이 안 먹었습니다"

# 시간대마다 need 구성이 실제로 달라지는지. 같으면 시간축이 죽은 것입니다.
sets = {p: {o["grid_id"] for o in out if o["period"] == p and o["quad"] == "need"} for p in PERIODS}
base = sets["am"]
print("  출근 대비 need 격자 구성 변화")
for p in PERIODS[1:]:
    only_p, only_am = sets[p] - base, base - sets[p]
    print(f"    {p:6} 신규 {len(only_p):>3}칸 · 이탈 {len(only_am):>3}칸 · 공통 {len(sets[p] & base):>3}칸")
assert any(sets[p] != base for p in PERIODS[1:]), "모든 시간대의 need 격자가 동일합니다 — 시간축이 죽었습니다"

# 잠재 대 실현 — 억압수요가 설명 가능한 범위인지. 원단위 가정의 유일한 검산입니다.
pop_all = sum(num(r["pop"]) for r in by_period["am"])
poten_city = pop_all * BUS_TRIP_RATE
actual = sum(num(r["boardings"]) for r in rows)          # 4시간대 합 = 일 총량
print(f"\n  억압수요 점검 — 시 전체 잠재 {poten_city:,.0f} 대 실현 {actual:,.0f}"
      f" = {poten_city / actual:.2f}배")
assert 1.0 <= poten_city / actual <= 3.0, \
    f"잠재/실현 {poten_city / actual:.2f}배 — 통행 원단위 가정을 다시 보세요"

print("\n  ✅ 통과")
