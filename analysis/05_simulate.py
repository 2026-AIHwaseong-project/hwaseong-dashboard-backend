# -*- coding: utf-8 -*-
"""
배치 시뮬레이션 엔진 — 물리 재계산(기계식) + 그리디 추천

    python analysis/05_simulate.py          # 기준선 일치 검증 + 표본 실험 출력

의존
    dataset_hwaseong/grid_metrics.csv   04_model.py 산출
    dataset_hwaseong/norm_stats.json    04_model.py 산출 (시뮬이 고정 사용)
    dataset_hwaseong/grid_hwaseong.csv  격자 마스터
    dataset_hwaseong/grid_join.csv      격자 × 시간대 원재료
    dataset_hwaseong/stops_hwaseong.csv 정류장 좌표 + 시간대별 freq

설계
    - 좌표계: EPSG:5179. 격자/정류장 lon/lat → 5179 2차 다항 회귀 (잔차 < 0.6m).
    - 수요측(D, zD, damp, nf) 고정 / 공급측(freq, cov)만 기계 재계산.
    - NORM = norm_stats["periods"] 고정 사용 — 재계산 금지.
    - Poisson 회귀 ΔB̂: E[B] = exp(b0 + b1·log1p(pot) + b2·log1p(freq) + b3·cov + …)

수단 파라미터
    신설 f*: 마을버스 실측 배차 — 출퇴근 25분=4.8회/2h창, 평일 60분=8회/8h창, 심야 0
    DRT φ: 1.2회/h → 시간대 창 환산 (am·pm·night=2.4, day=9.6)
    증편: headway × 0.7 (1.43× 운행횟수)
    비용(연환산): 신설 4.2백만원, DRT 180백만원, 증편 95백만원
"""
import json
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from sklearn.linear_model import PoissonRegressor
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"

# 상수 정본은 params.py — 04_model 과 같은 값을 읽어야 기준선 assert 가 성립한다.
# (QUAD 를 CUT 으로 그대로 쓴다. fref_q 키가 하나 더 있지만 키 접근만 하므로 무해)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from params import (PERIODS, PERIOD_HOURS, MIN_FREQ_PER_H,              # noqa: E402
                    MI_THRESHOLDS as MI_TH, W_FREQ, W_COV, DAMP_EXP, MI_CLAMP,
                    ELD_COEF as ELD, QUAD as CUT, WALK_M as WALK,
                    COV_THRESHOLD_M as COVM, FSTAR, PHI, HEADWAY_MULT,
                    COST_ANNUAL as COST, R_FINAL)

# ---------- 데이터 적재 ----------
gh   = pd.read_csv(D_DIR / "grid_hwaseong.csv")
st   = pd.read_csv(D_DIR / "stops_hwaseong.csv")
gj   = pd.read_csv(D_DIR / "grid_join.csv")
base = pd.read_csv(D_DIR / "grid_metrics.csv")
NORM = json.loads((D_DIR / "norm_stats.json").read_text(encoding="utf-8"))["periods"]

GIDS   = gh.grid_id.values
N      = len(gh)
IDX    = {g: i for i, g in enumerate(GIDS)}
REGION = gh.set_index("grid_id")["region"]

# 좌표: lon/lat → EPSG:5179, 2차 다항 회귀 (잔차 < 0.6m 검증)
def _design(lon, lat):
    return np.column_stack([np.ones_like(lon), lon, lat, lon * lat, lon**2, lat**2])

_A = _design(gh.lon.values, gh.lat.values)
_cx = np.linalg.lstsq(_A, gh.x_5179.values, rcond=None)[0]
_cy = np.linalg.lstsq(_A, gh.y_5179.values, rcond=None)[0]
GX, GY = gh.x_5179.values, gh.y_5179.values
_B = _design(st.lon.values, st.lat.values)
SX, SY = _B @ _cx, _B @ _cy

# 정류장→격자 800m 거리가중 행렬 (03_join [3] 재현; lost stop은 최근접 배정)
Dm = np.sqrt((GX[:, None] - SX[None, :]) ** 2 + (GY[:, None] - SY[None, :]) ** 2)
Wsg = np.where(Dm <= WALK, 1 - Dm / WALK, 0.0)
for _j in np.where((Dm <= WALK).sum(0) == 0)[0]:
    _i = int(np.argmin(Dm[:, _j]))
    Wsg[_i, _j] = 1 - min(Dm[_i, _j], WALK - 1) / WALK
STOP_FREQ = {p: st[f"freq_{p}"].values.astype(float) for p in PERIODS}

# 격자 간 거리행렬
Dg = np.sqrt((GX[:, None] - GX[None, :]) ** 2 + (GY[:, None] - GY[None, :]) ** 2)

# ---------- 기준선 상태 (period별) ----------
S0 = {}
for _p in PERIODS:
    _sub = gj[gj.period == _p].set_index("grid_id").reindex(GIDS)
    _bm  = base[base.period == _p].set_index("grid_id").reindex(GIDS)
    _K   = NORM[_p]
    _nb  = np.clip((np.log1p(_sub.boardings.values)  - _K["loB"]) / (_K["hiB"] - _K["loB"]), 0, 1)
    _nf  = np.clip((np.log1p(_sub.potential.values)  - _K["loF"]) / (_K["hiF"] - _K["loF"]), 0, 1)
    _D   = 0.5 * _nb + 0.5 * _nf
    S0[_p] = dict(
        freq    = _sub.freq.values.astype(float),
        nearest = _sub.nearest_stop_m.values.astype(float),
        D=_D,
        zD      = (_D - _K["mD"]) / _K["sD"],
        nf      = _nf,
        damp    = np.clip(_D / _K["dRef"], 0, 1) ** DAMP_EXP,
        potential = _sub.potential.values.astype(float),
        eldw    = 1 + ELD * np.minimum(_sub.elderly_ratio.values, 1.0),
        mi0     = _bm.mi.values,
        bin0    = _bm.bin_mi.values,
        quad0   = _bm.quadrant.values,
        boardings = _sub.boardings.values.astype(float),
    )


def compute(p, freq, nearest):
    """공급측 재계산 → mi, quad, bin, KPI. 수요측(D,zD,damp,nf) 고정."""
    K, ss = NORM[p], S0[p]
    cov = np.clip(1 - nearest / COVM, 0.05, 1.0)
    nq  = np.clip((np.log1p(freq) - K["loQ"]) / (K["hiQ"] - K["loQ"]), 0, 1)
    S   = W_FREQ * nq + W_COV * cov
    zS  = (S - K["mS"]) / K["sS"]
    mi  = np.clip((ss["zD"] - zS) * ss["damp"], -MI_CLAMP, MI_CLAMP)
    # 절대 가드 — 04_model.py [8] 과 반드시 같아야 한다. 다르면 기준선 assert 가 잡는다.
    # z 는 시간대 안의 상대평가라 밤엔 자가 짧아진다. 가드가 없으면 야간 시간당
    # 0.37회 다니는 격자가 "적정" 이 된다(실측 61곳 오라벨).
    enough = (freq / PERIOD_HOURS[p]) >= MIN_FREQ_PER_H

    quad = np.full(N, "mid", dtype=object)
    m_need = (ss["zD"] >= CUT["need_zd"]) & (mi >= CUT["need_mi"])
    m_over = ~m_need & (ss["zD"] <= CUT["over_zd"]) & (zS >= CUT["over_zs"]) & enough
    m_drt  = ~m_need & ~m_over & (ss["zD"] <= CUT["drt_zd"]) & (zS <= CUT["drt_zs"]) & (ss["nf"] >= K["fRef"])
    m_ok   = (~m_need & ~m_over & ~m_drt
              & (ss["zD"] >= CUT["ok_zd"]) & (zS >= CUT["ok_zs"]) & enough)
    quad[m_need], quad[m_over], quad[m_drt], quad[m_ok] = "need", "over", "drt", "ok"
    b    = np.sum(mi[:, None] > np.array(MI_TH)[None, :], axis=1)
    unres = m_need | m_drt
    U    = float(np.sum(ss["potential"][unres] * ss["eldw"][unres]))
    return dict(mi=mi, quad=quad, bin=b, cov=cov, S=S, zS=zS,
                need=int(m_need.sum()), drt=int(m_drt.sum()), U=U)


BASE_KPI = {p: compute(p, S0[p]["freq"], S0[p]["nearest"]) for p in PERIODS}
for _p in PERIODS:
    assert np.abs(BASE_KPI[_p]["mi"] - S0[_p]["mi0"]).max() < 5e-4, f"기준선 불일치: {_p}"
    assert (BASE_KPI[_p]["quad"] == S0[_p]["quad0"]).all(), f"quad 불일치: {_p}"

# ---------- Poisson 회귀 (ΔB̂용) ----------
POIS = {}
_gj2 = gj.copy()
_gj2["cov600"] = np.clip(1 - _gj2.nearest_stop_m / COVM, 0.05, 1.0)
for _p in PERIODS:
    _sub = _gj2[_gj2.period == _p].set_index("grid_id").reindex(GIDS)
    _X = np.column_stack([
        np.log1p(_sub.potential.values), np.log1p(_sub.freq.values),
        _sub.cov600.values, np.log1p(_sub.workers.values),
        np.minimum(_sub.elderly_ratio.values, 1.0),
        np.log1p(_sub.rail_m.fillna(_sub.rail_m.median()).values),
    ])
    _y  = _sub.boardings.values
    _sc = StandardScaler().fit(_X)
    _m  = PoissonRegressor(alpha=1e-3, max_iter=3000).fit(_sc.transform(_X), _y)
    _beta = _m.coef_ / _sc.scale_
    _mu   = _m.predict(_sc.transform(_X))
    POIS[_p] = dict(b2=_beta[1], b3=_beta[2], mu=_mu,
                    lq0=np.log1p(S0[_p]["freq"]), cov0=BASE_KPI[_p]["cov"])


def Bhat(p, freq, cov):
    """기준선 대비 예측 승차 변화 (Σ μ·exp(b2·Δlq + b3·Δcov))"""
    P = POIS[p]
    return P["mu"] * np.exp(
        np.clip(P["b2"] * (np.log1p(freq) - P["lq0"]) + P["b3"] * (cov - P["cov0"]), -20, 6)
    )


def dB_hat(p, dfreq, dcov):
    P = POIS[p]
    dl = np.log1p(S0[p]["freq"] + dfreq) - P["lq0"]
    return float(np.sum(P["mu"] * (np.exp(np.clip(P["b2"] * dl + P["b3"] * dcov, -20, 6)) - 1)))


# ---------- 배치 연산 ----------
def apply_stop(p, gi, r_m, count=1):
    """신설: 격자중심 가상정류장, freq += count·f*·(1-d/800), nearest=min."""
    d = Dg[gi]
    freq = S0[p]["freq"].copy()
    nearest = S0[p]["nearest"].copy()
    m = d <= min(WALK, r_m)
    freq[m] += count * FSTAR[p] * (1 - d[m] / WALK)
    mc = d <= r_m
    nearest[mc] = np.minimum(nearest[mc], d[mc])
    return freq, nearest


def apply_drt(p, gi, r_m, count=1):
    """DRT: freq += count·φ·(1-d/r), nearest 불변."""
    d = Dg[gi]
    freq = S0[p]["freq"].copy()
    m = d <= r_m
    freq[m] += count * PHI[p] * (1 - d[m] / r_m)
    return freq, S0[p]["nearest"].copy()


def apply_freq(p, gi, r_m, count=1):
    """증편: 반경 내 정류장 headway × 0.7^count → 격자 freq 재계산."""
    ds = np.sqrt((SX - GX[gi]) ** 2 + (SY - GY[gi]) ** 2)
    ms = ds <= r_m
    mult = HEADWAY_MULT ** count - 1
    dgrid = Wsg[:, ms] @ (STOP_FREQ[p][ms] * mult)
    return S0[p]["freq"] + dgrid, S0[p]["nearest"].copy()


APPLY = {"stop": apply_stop, "drt": apply_drt, "freq": apply_freq}


def evaluate(p, freq, nearest, center_gi=None):
    """배치 후 KPI 계산 (기준선 대비 델타)."""
    ss, bk = S0[p], BASE_KPI[p]
    r     = compute(p, freq, nearest)
    dmi   = r["mi"] - bk["mi"]
    dcov  = r["cov"] - bk["cov"]
    dfreq = freq - ss["freq"]
    ch    = np.abs(dmi) > 1e-9
    reach = float(Dg[center_gi][ch].max()) if (center_gi is not None and ch.any()) else float("nan")
    return dict(
        res=r, n_dmi=int(ch.sum()),
        n_bin=int((r["bin"] != bk["bin"]).sum()),
        d_need=r["need"] - bk["need"],
        d_drt=r["drt"] - bk["drt"],
        dU=bk["U"] - r["U"],
        dB=dB_hat(p, dfreq, dcov),
        max_dmi=float(np.abs(dmi).max()),
        reach_m=reach,
    )


def total_daily(mode, gi, count=1):
    """4시간대 합산 ΔB̂, ΔU, 색칠 칸수."""
    tB = tU = 0.0
    nb = nmi = 0
    for p in PERIODS:
        freq, nearest = APPLY[mode](p, gi, R_FINAL[mode], count)
        ev = evaluate(p, freq, nearest)
        tB += ev["dB"]
        tU += ev["dU"]
        nb += ev["n_bin"]
        nmi += ev["n_dmi"]
    return tB, tU, nb, nmi


def greedy(nsel=10, portfolio=False, dedup=True, verbose=True):
    """그리디 추천 (비용효율 = 4창합 ΔB̂ / 연환산비용)."""
    amx     = base[base.period == "am"].set_index("grid_id").reindex(GIDS)
    cov_am  = BASE_KPI["am"]["cov"]
    cand_pool = (
        np.isin(amx.quadrant.values, ["need", "drt"])
        | ((amx.quadrant.values == "mid") & (amx.mi.values > 0.25))
    )
    cand_idx = np.where(cand_pool)[0]

    state = {p: dict(freq=S0[p]["freq"].copy(), nearest=S0[p]["nearest"].copy())
             for p in PERIODS}
    placed      = []
    used        = set()
    freq_count  = {}
    cur_B = {p: float(np.sum(Bhat(p, state[p]["freq"],
                                   np.clip(1 - state[p]["nearest"] / COVM, .05, 1))))
             for p in PERIODS}

    def _delta_state(mode, gi, p, freq_cur, cnt):
        d = Dg[gi]
        if mode == "stop":
            f1 = freq_cur.copy()
            m  = d <= WALK
            f1[m] += FSTAR[p] * (1 - d[m] / WALK)
            return f1, d
        if mode == "drt":
            f1 = freq_cur.copy()
            m  = d <= R_FINAL["drt"]
            f1[m] += PHI[p] * (1 - d[m] / R_FINAL["drt"])
            return f1, None
        ds  = np.sqrt((SX - GX[gi]) ** 2 + (SY - GY[gi]) ** 2)
        ms  = ds <= R_FINAL["freq"]
        boost = Wsg[:, ms] @ (STOP_FREQ[p][ms] * (HEADWAY_MULT - 1)) * (HEADWAY_MULT ** cnt)
        return freq_cur + boost, None

    for step in range(nsel):
        best = None
        cnt  = {m: sum(1 for pl in placed if pl[0] == m) for m in ["stop", "drt", "freq"]}
        cov_now = np.clip(1 - state["am"]["nearest"] / COVM, 0.05, 1.0)
        for mode in ["stop", "drt", "freq"]:
            if portfolio and cnt[mode] >= int(0.6 * nsel):
                continue
            if portfolio and step == nsel - 1 and cnt["drt"] == 0 and mode != "drt":
                continue
            g_ok = {
                "stop": (cov_now >= 0.15) & (cov_now < 0.5),
                "freq": cov_now >= 0.5,
                "drt":  np.ones(N, bool),
            }[mode]
            for gi in cand_idx:
                if not g_ok[gi] or (dedup and (mode, gi) in used):
                    continue
                tB = 0.0
                for p in PERIODS:
                    f1, ncand = _delta_state(mode, gi, p, state[p]["freq"],
                                             freq_count.get(gi, 0))
                    n1 = (np.minimum(state[p]["nearest"], ncand)
                          if ncand is not None else state[p]["nearest"])
                    c1 = np.clip(1 - n1 / COVM, 0.05, 1)
                    tB += float(np.sum(Bhat(p, f1, c1))) - cur_B[p]
                eff = tB / COST[mode]
                if best is None or eff > best["eff"]:
                    best = dict(mode=mode, gi=gi, eff=eff, tB=tB)
        if best is None:
            break
        gi, mode = best["gi"], best["mode"]
        for p in PERIODS:
            f1, ncand = _delta_state(mode, gi, p, state[p]["freq"], freq_count.get(gi, 0))
            state[p]["freq"] = f1
            if ncand is not None:
                state[p]["nearest"] = np.minimum(state[p]["nearest"], ncand)
            cur_B[p] = float(np.sum(Bhat(p, state[p]["freq"],
                                          np.clip(1 - state[p]["nearest"] / COVM, .05, 1))))
        if mode == "freq":
            freq_count[gi] = freq_count.get(gi, 0) + 1
        used.add((mode, gi))
        placed.append((mode, GIDS[gi], best["tB"]))
        if verbose:
            print(f"  {step+1:2d}. {mode:5s} @{GIDS[gi]} {REGION[GIDS[gi]]:6s} "
                  f"한계ΔB̂/일 {best['tB']:7.1f}  "
                  f"비용 {COST[mode]/1e6:6.1f}백만/년  "
                  f"원/일통행 {COST[mode]/max(best['tB'], 1e-9):>12,.0f}")

    tots = {}
    for p in PERIODS:
        r = compute(p, state[p]["freq"], state[p]["nearest"])
        tots[p] = dict(
            need=r["need"] - BASE_KPI[p]["need"],
            drt=r["drt"]  - BASE_KPI[p]["drt"],
            U=BASE_KPI[p]["U"] - r["U"],
            nbin=int((r["bin"] != BASE_KPI[p]["bin"]).sum()),
        )
    return placed, tots


if __name__ == "__main__":
    print("[회귀] period별 탄력성 b2(log1p freq)·b3(cov):")
    print({p: (round(POIS[p]["b2"], 3), round(POIS[p]["b3"], 3)) for p in PERIODS})

    print("\n[그리디] 10건 포트폴리오 제약")
    pl, tots = greedy(10, portfolio=True)
    comp = {}
    for p2 in pl:
        comp[p2[0]] = comp.get(p2[0], 0) + 1
    print("  수단구성:", comp)
    print("  period별 (ΔneedCells, ΔdrtCells, 해소U, 색칠칸):")
    for p in PERIODS:
        t = tots[p]
        print(f"    {p}: need{t['need']:+d} drt{t['drt']:+d} U={t['U']:.0f} bin={t['nbin']}")
    total_dB = sum(p2[2] for p2 in pl)
    total_cost = sum(COST[p2[0]] for p2 in pl)
    print(f"  총 ΔB̂/일 {total_dB:.0f}  총비용 {total_cost/1e8:.2f}억원/년")
