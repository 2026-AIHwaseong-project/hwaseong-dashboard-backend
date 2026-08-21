# -*- coding: utf-8 -*-
"""
모델 검증 — 승차 예측 회귀 · 공간 교차검증 · 정성 대조 (A6)

    python analysis/07_validate.py

산출
    dataset_hwaseong/validation.json   검증 결과 (발표·보고서 인용용)

README §4 의 검증 3종을 실제로 돌립니다.
    1. 예측 vs 실측 승차 R²          목표 ≥ 0.6
    2. 홀드아웃 (읍면동 단위 공간 CV)
    3. 우선순위 Top 5 정성 대조용 목록 출력

---
[1] ★ 시간대별이 아니라 '일 단위'로 회귀합니다 — 순환논리 회피

시간대별 승차(`boardings`)는 실측이 아닙니다. 일 총량을 유동인구 시간배율로
안분한 추정치입니다(03_join). 그런데 잠재수요(`potential`)도 같은 배율로 만듭니다.

    boardings_t = board_day × share_t
    potential_t = 연령가중인구 × share_t      ← 같은 share_t

이 둘을 회귀하면 **share_t 가 share_t 를 설명**합니다. R² 가 높게 나오지만
아무것도 검증하지 못합니다. 팀원 검증에서도 "야간 탄력성은 승차 시간안분이
유동 비율을 복사한 순환" 으로 같은 지점이 지적됐습니다.

그래서 좌변을 **`board_day`(실측 일평균 승차)** 로 두고 우변도 전부 일 단위로
맞춥니다. 좌변이 관측값이므로 R² 가 진짜 설명력을 뜻합니다.

[2] 공간 교차검증을 쓰는 이유
같은 읍면동 격자끼리는 서로 닮아서, 무작위로 나누면 학습 데이터의 이웃이
검증 데이터에 섞여 R² 가 부풀려집니다. 읍면동째로 빼면 "이 동을 안 보고도
맞히는가" 를 재게 되어 훨씬 보수적입니다.
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from sklearn.linear_model import PoissonRegressor
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"

FEATURES = ["log_pop", "log_freq", "coverage", "log_workers", "elderly_ratio", "log_rail"]
R2_TARGET = 0.6

gj = pd.read_csv(D / "grid_join.csv")
if "daytype" in gj.columns:      # 요일축 도입 이후 grid_join.csv — 검증은 평일 기준을 그대로 쓴다.
    gj = gj[gj["daytype"] == "wd"].drop(columns=["daytype"]).reset_index(drop=True)
gm = pd.read_csv(D / "grid_metrics.csv")
gh = pd.read_csv(D / "grid_hwaseong.csv")

# ── 일 단위 테이블 만들기 [1] ───────────────────────────────────────────────
day = gj[gj.period == "am"].set_index("grid_id").copy()          # 시간대 불변 컬럼
day["freq_day"] = gj.groupby("grid_id")["freq"].sum()            # 4시간대 운행 합
day = day.join(gh.set_index("grid_id")[["region"]], rsuffix="_g")

X = pd.DataFrame({
    "log_pop":      np.log1p(day["pop"]),
    "log_freq":     np.log1p(day["freq_day"]),
    "coverage":     day["coverage"],
    "log_workers":  np.log1p(day["workers"]),
    "elderly_ratio": np.minimum(day["elderly_ratio"], 1.0),
    "log_rail":     np.log1p(day["rail_m"].fillna(day["rail_m"].median())),
})
y = day["board_day"].values
groups = day["region"].values

print("=" * 64)
print("[1] 승차 예측 회귀 — 일 단위 (좌변이 실측값)")
print(f"  표본 {len(y):,}격자 · 읍면동 {len(set(groups))}개")
print(f"  실측 승차 일평균 합 {y.sum():,.0f} · 중앙값 {np.median(y):,.1f}")


def fit(Xtr, ytr):
    sc = StandardScaler().fit(Xtr)
    m = PoissonRegressor(alpha=1e-3, max_iter=5000).fit(sc.transform(Xtr), ytr)
    return sc, m


def r2(actual, pred):
    ss_res = float(((actual - pred) ** 2).sum())
    ss_tot = float(((actual - actual.mean()) ** 2).sum())
    return 1 - ss_res / ss_tot if ss_tot > 0 else 0.0


sc, model = fit(X.values, y)
pred_in = model.predict(sc.transform(X.values))
r2_raw = r2(y, pred_in)
r2_log = r2(np.log1p(y), np.log1p(pred_in))
print(f"  학습 R²  원단위 {r2_raw:.3f} · 로그 {r2_log:.3f}")

# 계수 — 표준화 되돌려 원 스케일 탄력성으로
beta = model.coef_ / sc.scale_
print("\n  계수 (log 항은 탄력성)")
for f, b in sorted(zip(FEATURES, beta), key=lambda t: -abs(t[1])):
    print(f"    {f:15} {b:+.3f}")
elast = dict(zip(FEATURES, beta))["log_freq"]

print("=" * 64)
print("[2] 공간 교차검증 — 읍면동째로 빼고 예측")
gkf = GroupKFold(n_splits=min(5, len(set(groups))))
oof = np.zeros(len(y))
for tr, te in gkf.split(X.values, y, groups):
    s, m = fit(X.values[tr], y[tr])
    oof[te] = m.predict(s.transform(X.values[te]))
cv_raw, cv_log = r2(y, oof), r2(np.log1p(y), np.log1p(oof))
print(f"  홀드아웃 R²  원단위 {cv_raw:.3f} · 로그 {cv_log:.3f}   (목표 ≥ {R2_TARGET})")

# ── 빈 땅을 빼고도 맞히는가 ──────────────────────────────────────────────
# 786격자 중 인구<50 이 183곳, 승차 0 이 180곳이다. "빈 땅에는 사람이 안 탄다"
# 를 맞히는 몫이 전체 R² 를 떠받친다. 실제 승차가 일어나는 격자만 놓고도
# 목표를 넘는지 봐야 한다 — 넘는다면 그게 더 정직하면서 더 강한 주장이다.
# (예측은 위 oof 를 그대로 쓴다. 부분집합마다 다시 학습하면 표본이 줄어
#  홀드아웃 조건이 달라져 비교가 안 된다.)
pop_v = day["pop"].values
subsets = {}
print("\n  실제 승차가 일어나는 격자만 (같은 홀드아웃 예측을 부분집합으로 평가)")
for th in (0, 50, 200):
    m = pop_v >= th
    s_log, s_raw = r2(np.log1p(y[m]), np.log1p(oof[m])), r2(y[m], oof[m])
    frac = y[m].sum() / y.sum()
    subsets[f"pop_ge_{th}"] = {"n": int(m.sum()), "boardingShare": round(float(frac), 4),
                               "cv_log": round(s_log, 4), "cv_raw": round(s_raw, 4)}
    print(f"    인구≥{th:>3}  {m.sum():>3}칸 (승차의 {frac:5.1%})  "
          f"로그 R² {s_log:+.3f} · 원단위 {s_raw:+.3f}")
HEADLINE = subsets["pop_ge_200"]
print(f"\n  ▶ 인용 권장: 승차의 {HEADLINE['boardingShare']:.1%} 가 일어나는 "
      f"{HEADLINE['n']}격자에서 로그 R² {HEADLINE['cv_log']:.3f}")

# 읍면동 하나씩 완전히 빼는 버전 — 가장 보수적
worst = []
for reg in sorted(set(groups)):
    m_te = groups == reg
    if m_te.sum() < 3 or (~m_te).sum() < 50:
        continue
    s, m = fit(X.values[~m_te], y[~m_te])
    p = m.predict(s.transform(X.values[m_te]))
    worst.append((reg, int(m_te.sum()), r2(np.log1p(y[m_te]), np.log1p(p))))
worst.sort(key=lambda t: t[2])
print("\n  읍면동 단독 제외 시 로그 R² — 하위 5 / 상위 3")
for r_, n, v in worst[:5]:
    print(f"    {r_:8} {n:>3}칸  {v:+.3f}")
for r_, n, v in worst[-3:]:
    print(f"    {r_:8} {n:>3}칸  {v:+.3f}")

print("=" * 64)
print("[3] 우선순위 Top 5 — 정성 대조용 (실제 민원·언론과 비교할 목록)")
QUAL_PERIODS = ["am", "night"]
TOP_N = 5
qual = {}
rank_of = {}          # (period, region) -> (최고순위, need격자수) — [3-1] 이 쓴다
for p in QUAL_PERIODS:
    need = (gm[(gm.period == p) & (gm.priority > 0)]
            .sort_values("priority", ascending=False).reset_index(drop=True))
    for i, r in enumerate(need.itertuples(), 1):
        key = (p, r.region)
        if key not in rank_of:
            rank_of[key] = (i, len(need))
    print(f"\n  [{p}]  need 격자 {len(need)}곳")
    rows = []
    for i, r in enumerate(need.head(TOP_N).itertuples(), 1):
        print(f"    {i}. {r.region:8} ({r.grid_id})  점수 {r.priority:5.2f}"
              f"  MI {r.mi:+.2f}  고령 {r.elderly_ratio:5.1%}  {r.action}"
              f"  ({r.lat:.4f}, {r.lon:.4f})")
        rows.append({"rank": i, "region": r.region, "gridId": r.grid_id,
                     "priority": round(float(r.priority), 4),
                     "action": r.action, "lat": float(r.lat), "lon": float(r.lon)})
    qual[p] = rows

# 언론·위키에서 실제로 확인한 것만 적는다. 못 찾은 건 못 찾았다고 둔다.
# 발표에서 "모델이 찍은 곳이 실제로 문제인 곳" 을 말할 때 근거가 되는 부분이라
# 추정을 섞으면 안 된다.
#
# ⚠️ 순위는 **적지 않는다**. 예전에는 "출근 1·4위" 처럼 손으로 적어 뒀는데,
#    노선 커버리지를 보강(140→200)하자 새솔동이 need 에서 빠졌는데도 문구는
#    "출근 1·4위 ✅" 로 남아 있었다. 검증이 모델을 따라오지 못하면 발표장에서
#    심사위원이 화면을 열어 대조하는 순간 근거가 무너진다.
#    그래서 순위는 아래에서 **매 실행마다 현재 산출물에서 계산**한다.
QUALITATIVE = [
    {"region": "새솔동", "verified": True,
     "finding": "송산그린시티. 안산 생활권인데 연결 시내버스가 안산 업체 10번 하나뿐. "
                "화성시가 동탄·병점권 위주로 투자해 노선 신설이 지연된다고 보도됨",
     "source": "경기일보 2018-05-02 / 나무위키 송산그린시티·새솔동"},
    {"region": "비봉면", "verified": True,
     "finding": "비봉지구 마을버스 막차가 22시경 종료. 인구 증가 대비 대중교통 개선 지연",
     "source": "나무위키 가축수송(교통)/사례/버스/경기도"},
    # ── 2026-08-13 추가 조사 (현재 Top5 usable < 3 미달을 메우기 위한 6개 읍면동) ──
    {"region": "동탄9동", "verified": True,
     "finding": "신동(동탄9동) 17,000세대·5만명 입주 지역. \"동탄역-신동 연계 출퇴근 차량 "
                "막차가 오후 7시면 종료\"되어 직장인 불편 호소. GTX·SRT 동탄역 접근 취약, "
                "트램 설계에서도 배제됐다고 시의회·지역위원장이 지적",
     "source": "경인매일 2025-04-07 「5만명 입주 앞둔 동탄 신동, 교통·문화시설 절대 부족」"},
    {"region": "봉담읍", "verified": True,
     "finding": "화성시 공식 민원(시민소통광장). 봉담2지구 등 약 17,000세대 입주했으나 "
                "서울행 광역버스는 7790번 하나뿐. 기존 8155·8156번은 \"만차가 되어 이용하지 "
                "못하는 경우가 대부분\", 1006번은 \"배차시간이 1시간 이상\"이라고 명시",
     "source": "화성시 시민소통광장 2026-04-20 「봉담기점 서울행 광역버스 신설 촉구」"},
    {"region": "진안동", "verified": True,
     "finding": "화성시 공식 민원(시민소통광장). 병점(병점·진안·안화동 일대)이 동탄 신도시 "
                "위주 투자에서 소외됐다며 \"야간·주말 교통 공백\", \"구청 순환버스조차 없고 "
                "똑버스 운행 계획에서도 사실상 제외\"라고 지적, 똑버스 조속 도입 요청",
     "source": "화성시 시민소통광장 2026-05-20 「병점지역 순환버스·똑버스 운행요청」"},
    {"region": "동탄7동", "verified": True,
     "finding": "동탄7·9동(마선거구) 시의원 후보가 \"대중교통 공백이 발생하는 야간 시간대\" "
                "해소를 위한 심야 자율주행 동탄순환버스 도입을 공약. 지역구 교통 현안으로 "
                "야간 공백이 공식 제기됨",
     "source": "경인신문(asn24) 2026-05-24 「화성시의회 김상균 후보, 동탄7·9동 교통 공약 발표」"},
    {"region": "남양읍", "verified": False,
     "finding": "⚠️ 신호가 엇갈린다 — 남양여객은 \"배차간격도 길고 수요도 극히 낮다\"고 "
                "서술되지만(나무위키), 남양뉴타운 자체는 2025-02 서울역 직행 5101번이 새로 "
                "개통되는 등 최근 노선이 늘고 있다(머니투데이). '지금도 문제'라고 단정할 "
                "개별 민원을 못 찾아 verified 는 보류한다",
     "source": None},
    {"region": "향남읍", "verified": False,
     "finding": "개별 지명 민원은 못 찾음. 다만 화성시가 2026-05 대중교통 소외지역 대응으로 "
                "구청 연계 순환버스 9개 노선을 발표하는 등, 시 차원에서 소외지역 문제를 "
                "공식 인지하고 있다는 정황은 있다",
     "source": "머니투데이 등 2026-05-18 「화성시, 3개 구청 연계 순환버스 9개 노선 추가 확충」"},
    {"region": "화산동", "verified": False,
     "finding": "공개 자료에서 개별 민원 사례를 확인하지 못함. "
                "다만 야간 무공급(운행 0회) 격자로 실측된다",
     "source": None},
    {"region": "정남면", "verified": False,
     "finding": "공개 자료에서 개별 민원 사례를 확인하지 못함",
     "source": None},
]


def rank_str(region):
    """이 읍면동이 지금 어디쯤인지 — 매 실행마다 산출물에서 직접 읽는다."""
    parts = []
    for p in QUAL_PERIODS:
        if (p, region) in rank_of:
            rk, n = rank_of[(p, region)]
            parts.append(f"{p} {rk}/{n}위")
        else:
            parts.append(f"{p} need 아님")
    return " · ".join(parts)


print("=" * 64)
print("[3-1] 정성 대조 — 공개 자료 근거 vs **현재** 우선순위")
for q in QUALITATIVE:
    q["currentRank"] = rank_str(q["region"])
    q["inTop5"] = any(q["region"] in [r["region"] for r in qual[p]] for p in QUAL_PERIODS)
    mark = "✅" if q["verified"] else "⬜"
    top = "★Top5" if q["inTop5"] else "     "
    print(f"  {mark} {top} {q['region']:8} — {q['currentRank']}")
    print(f"       {q['finding']}")

# 발표에서 쓸 수 있는 건 '근거가 있고 + 지금도 상위' 인 것뿐이다. 둘 다여야 한다.
usable = [q for q in QUALITATIVE if q["verified"] and q["inTop5"]]
verified_n = sum(q["verified"] for q in QUALITATIVE)
print(f"\n  공개근거 확보 {verified_n}/{len(QUALITATIVE)}건 · "
      f"그중 현재 Top{TOP_N} 에도 드는 것 **{len(usable)}건** (목표 3건)")
if len(usable) < 3:
    print(f"  ⚠️ 발표에서 '모델이 찍은 곳이 실제 문제인 곳' 이라고 말할 근거가 부족하다.")
    print(f"     현재 Top5 읍면동에 대한 공개자료 조사를 추가해야 한다:")
    for p in QUAL_PERIODS:
        regs = sorted({r["region"] for r in qual[p]})
        print(f"       [{p}] {', '.join(regs)}")

print("=" * 64)
print("[4] 저장 · 판정")
try:
    _spec = json.loads((D / "grid_spec.json").read_text(encoding="utf-8"))
    _grid_size = int(_spec["sizeMeters"])
    if int(_spec.get("cellCount", len(y))) != len(y):
        print(f"  ⚠️ grid_spec({_spec['cellCount']}칸) ≠ 분석 격자({len(y)}칸) — 02_grid 재실행 필요")
except FileNotFoundError:
    _grid_size = 1000
except (KeyError, ValueError, TypeError):
    _grid_size = 1000
    print("  ⚠️ grid_spec.json 손상 — 1km 로 간주하고 기록합니다")

out = {
    "gridSizeMeters": _grid_size,   # 어느 해상도에서 검증한 수치인지 — 500m 전환 시 필수 재실행
    "method": "Poisson 회귀 (log-link), 일 단위. 좌변 board_day 는 실측값",
    "note": "시간대별 승차는 유동인구 배율로 안분한 추정치라 시간대 회귀는 "
            "잠재수요와 같은 배율을 공유해 순환이 된다. 그래서 일 단위로 검증한다.",
    "n_grids": int(len(y)), "n_regions": int(len(set(groups))),
    "features": FEATURES,
    "coefficients": {f: round(float(b), 4) for f, b in zip(FEATURES, beta)},
    "freqElasticity": round(float(elast), 4),
    "r2": {"train_raw": round(r2_raw, 4), "train_log": round(r2_log, 4),
           "cv_raw": round(cv_raw, 4), "cv_log": round(cv_log, 4)},
    "r2BySubset": subsets,
    "headline": {"scope": f"인구≥200 격자 {HEADLINE['n']}칸 "
                          f"(실측 승차의 {HEADLINE['boardingShare']:.1%})",
                 "cv_log": HEADLINE["cv_log"], "cv_raw": HEADLINE["cv_raw"]},
    "demandBasis": "초승(환승 제외) · 평일 일평균 — 03_join.py [4][5]",
    "qualitativeUsable": len(usable),
    "cvMethod": "GroupKFold(5) by 읍면동 — 무작위 분할은 이웃 격자가 섞여 부풀려진다",
    "worstRegions": [{"region": r_, "cells": n, "r2_log": round(v, 4)} for r_, n, v in worst[:5]],
    "target": R2_TARGET, "passed": bool(cv_log >= R2_TARGET),
    "topPriority": qual,
    "qualitative": QUALITATIVE,
}
(D / "validation.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"  -> dataset_hwaseong/validation.json")

# 홀드아웃이 학습을 크게 웃돌면 누수를 의심해야 한다. 다만 Poisson 은 원단위
# deviance 를 최적화하는데 여기서는 로그 스케일로 R² 를 재므로 둘의 순서가
# 보장되지 않는다. 그래서 로그 쪽은 허용오차를 두고 눈에 띄게 뒤집힐 때만 잡는다.
#
# 2026-08-12 실측(초승·평일 기준): 로그 학습 0.846 → 홀드아웃 0.838,
# 원단위 학습 0.928 → 홀드아웃 0.874 로 둘 다 정상 순서다. 과적합이 거의 없다.
# (환승 포함·119일 혼합이던 이전 값은 로그 0.847→0.842, 원단위 0.854→0.801)
assert cv_log <= r2_log + 0.05, \
    f"홀드아웃({cv_log:.3f})이 학습({r2_log:.3f})을 크게 웃돕니다 — 누수 의심"
assert cv_raw < r2_raw + 1e-9, f"원단위 홀드아웃({cv_raw:.3f})이 학습({r2_raw:.3f}) 이상 — 누수 의심"
assert 0.0 < elast < 1.5, f"배차 탄력성 {elast:.2f} — 부호나 크기가 비정상"
print(f"\n  홀드아웃 로그 R² {cv_log:.3f} vs 목표 {R2_TARGET}"
      f"  →  {'✅ 통과' if cv_log >= R2_TARGET else '❌ 미달'}")
print(f"  배차 탄력성 {elast:+.3f} — 증편 계수의 실측 근거")
