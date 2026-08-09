# -*- coding: utf-8 -*-
"""
모델 검증 — 승차 예측 회귀 · 공간 교차검증 · 정성 대조 (A6)

    python analysis/07_validate.py

산출
    dataset_hwaseong/validation.json   검증 결과 (발표·보고서 인용용)

기획서 §10 의 검증 3종을 실제로 돌립니다.
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
qual = {}
for p in ["am", "night"]:
    top = gm[(gm.period == p) & (gm.priority > 0)].nlargest(5, "priority")
    print(f"\n  [{p}]")
    rows = []
    for i, r in enumerate(top.itertuples(), 1):
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
QUALITATIVE = [
    {"region": "새솔동", "rank": "출근 1·4위", "verified": True,
     "finding": "송산그린시티. 안산 생활권인데 연결 시내버스가 안산 업체 10번 하나뿐. "
                "화성시가 동탄·병점권 위주로 투자해 노선 신설이 지연된다고 보도됨",
     "source": "경기일보 2018-05-02 / 나무위키 송산그린시티·새솔동"},
    {"region": "비봉면", "rank": "심야 신규 need", "verified": True,
     "finding": "비봉지구 마을버스 막차가 22시경 종료. 인구 증가 대비 대중교통 개선 지연",
     "source": "나무위키 가축수송(교통)/사례/버스/경기도"},
    {"region": "화산동·정남면", "rank": "심야 1·5위", "verified": False,
     "finding": "공개 자료에서 개별 민원 사례를 확인하지 못함. "
                "다만 야간 무공급(운행 0회) 격자로 실측되며 화산동 다사5612 는 인구 6,872",
     "source": None},
]

print("=" * 64)
print("[3-1] 정성 대조 — 공개 자료에서 확인된 것")
for q in QUALITATIVE:
    print(f"  {'✅' if q['verified'] else '⬜'} {q['region']:12} ({q['rank']})")
    print(f"       {q['finding']}")
hit = sum(q["verified"] for q in QUALITATIVE)
print(f"\n  확인 {hit}/{len(QUALITATIVE)}건 — 목표는 Top5 중 3곳 이상 일치")

print("=" * 64)
print("[4] 저장 · 판정")
out = {
    "method": "Poisson 회귀 (log-link), 일 단위. 좌변 board_day 는 실측값",
    "note": "시간대별 승차는 유동인구 배율로 안분한 추정치라 시간대 회귀는 "
            "잠재수요와 같은 배율을 공유해 순환이 된다. 그래서 일 단위로 검증한다.",
    "n_grids": int(len(y)), "n_regions": int(len(set(groups))),
    "features": FEATURES,
    "coefficients": {f: round(float(b), 4) for f, b in zip(FEATURES, beta)},
    "freqElasticity": round(float(elast), 4),
    "r2": {"train_raw": round(r2_raw, 4), "train_log": round(r2_log, 4),
           "cv_raw": round(cv_raw, 4), "cv_log": round(cv_log, 4)},
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
# 보장되지 않는다. 실측 학습 0.800 대 홀드아웃 0.801 로 사실상 동률이고,
# 원단위는 0.790 → 0.732 로 정상 순서다. 과적합이 거의 없다는 뜻이라
# 허용오차를 두고, 눈에 띄게 뒤집힐 때만 잡는다.
assert cv_log <= r2_log + 0.05, \
    f"홀드아웃({cv_log:.3f})이 학습({r2_log:.3f})을 크게 웃돕니다 — 누수 의심"
assert cv_raw < r2_raw + 1e-9, f"원단위 홀드아웃({cv_raw:.3f})이 학습({r2_raw:.3f}) 이상 — 누수 의심"
assert 0.0 < elast < 1.5, f"배차 탄력성 {elast:.2f} — 부호나 크기가 비정상"
print(f"\n  홀드아웃 로그 R² {cv_log:.3f} vs 목표 {R2_TARGET}"
      f"  →  {'✅ 통과' if cv_log >= R2_TARGET else '❌ 미달'}")
print(f"  배차 탄력성 {elast:+.3f} — 증편 계수의 실측 근거")
