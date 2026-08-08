# -*- coding: utf-8 -*-
"""
D · S · MI · 4분면 · 우선순위 산출 — 786격자 × 4시간대

    python analysis/04_model.py

입력 (dataset_hwaseong/)
    grid_join.csv        03_join 산출 — 격자 × 시간대 원재료
    grid_hwaseong.csv    02_grid 산출 — 격자 마스터 (읍면동 배정)

산출 (dataset_hwaseong/)
    grid_metrics.csv     격자 × 시간대 — D/S/MI/quadrant/action/priority/bins
    norm_stats.json      정규화 기준통계. 05_simulate.py 는 이 값을 고정 사용한다.
                         (배치 전후를 같은 자로 재기 위해 재계산 금지)

---
수식 설계 결정 (전부 786격자 실데이터로 측정·확인한 것)

[1] 정규화: log1p 후 P3~P97 클램프 min-max (보간 없는 lower pctl).
    승차량 왜도 6.5, 최대/P99=2.7 → minmax 쓰면 82%가 1점 미만에 뭉개짐.
    robust-z 는 승차 MAD=0.48로 붕괴(max 603배) → 탈락.

[2] coverage 임계 600m (400m 아님).
    격자 중심~최근접 정류장 중앙값=392m → 임계 400m 면 인구 격자 45%가 바닥(0.05).
    grid_join 의 coverage(800m) 컬럼은 무시하고 nearest_stop_m 에서 재산정.

[3] MI 감쇠항 (D/P55)^0.65 필수.
    naive zD-zS 는 인구<50 빈 땅 183곳 MI 중앙 +0.58 (공급이 더 0이라서).
    감쇠 후 need 색(MI>=0.75) 빈 땅: 133 → 0곳.

[4] need 컷: MI >= 0.75 (mock 0.55에서 상향).
    0.55 면 동탄 411~574회/h 만원공급 격자 4곳이 need 침투(zD상한 2.67 > zS 2.11 비대칭).
    0.75 상향 → 침투 4곳 전부 ok. MI bins 실측 p1~p99=±1.3 → 경계 ±1.5→±1.2로 축소.

[5] freq 정규화: 시간대별 P3/P97 (loQ/hiQ 시간대별 산출, norm_stats["periods"][t]에 저장).
    05_simulate.py 는 이 값을 읽어 freq 재계산에 쓴다 — 구조 변경 시 동반 수정 필요.

[6] 4분면 컷 (tuneA): need_mi=0.75, need_zd=0.20, over_zd=-0.30, over_zs=0.30,
    drt_zd=-0.35, drt_zs=-0.35, ok_zd=0.25, ok_zs=0.25, fref_q=0.30(P30 잠재수요).

[7] 알려진 한계: 잠재수요가 거주인구 기반 → 고용 단독 격자(남양연구소 종사 15,749)는 mid.
    승차 시간축=유동인구 안분(추정)이므로 시간대 MI 변화의 91~94%는 공급(배차) 차이.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"

PERIODS = ["am", "day", "pm", "night"]
MI_THRESHOLDS = [-1.2, -0.7, -0.25, 0.25, 0.7, 1.2]
COV_THRESHOLD_M = 600.0
ALPHA_D = 0.5
W_FREQ, W_COV = 0.78, 0.22
DAMP_EXP = 0.65
MI_CLAMP = 2.6
ELD_COEF = 1.6
QUAD = dict(need_zd=0.20, need_mi=0.75, over_zd=-0.30, over_zs=0.30,
            drt_zd=-0.35, drt_zs=-0.35, ok_zd=0.25, ok_zs=0.25, fref_q=0.30)


def pctl(arr, q):
    """sorted[floor(q*(n-1))] — 보간 없는 lower 방식 (mock.js pctl 동일 계약)"""
    a = np.sort(np.asarray(arr, dtype=float))
    return float(a[int(np.floor(q * (len(a) - 1)))])


def norm_log(v, lo, hi):
    return np.clip((np.log1p(v) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)


def zstats(v):
    m = float(np.mean(v))
    sd = float(np.std(v))  # 모집단 std (ddof=0)
    return m, (sd if sd > 0 else 1.0)


def main():
    gj = pd.read_csv(D_DIR / "grid_join.csv")
    gh = pd.read_csv(D_DIR / "grid_hwaseong.csv")
    gj = gj.merge(gh[["grid_id", "region", "region_code", "region_kind"]], on="grid_id", how="left")

    # coverage 600m 재산정 [2]
    gj["cov600"] = np.clip(1.0 - gj["nearest_stop_m"] / COV_THRESHOLD_M, 0.05, 1.0)

    # 인구가중 (시간대 불변): log1p P3~P97 min-max, 786격자 기준 1회
    pop786 = gh.set_index("grid_id")["pop"].astype(float)
    lp = np.log1p(pop786.values)
    loP, hiP = pctl(lp, 0.03), pctl(lp, 0.97)
    popW = pd.Series(np.clip((lp - loP) / max(hiP - loP, 1e-9), 0, 1), index=pop786.index)

    norm_stats = {"periods": {}, "constants": {
        "alphaD": ALPHA_D, "wFreq": W_FREQ, "wCov": W_COV, "dampExp": DAMP_EXP,
        "miClamp": MI_CLAMP, "covThresholdM": COV_THRESHOLD_M,
        "miThresholds": MI_THRESHOLDS, "elderlyCoef": ELD_COEF,
        "normMethod": "log1p -> P3/P97 clamp min-max (pctl=lower)",
        "quadCuts": QUAD, "popWeightBounds": {"loP": loP, "hiP": hiP},
    }}

    rows = []
    for t in PERIODS:
        sub = gj[gj["period"] == t].reset_index(drop=True)
        lb = np.log1p(sub["boardings"])
        lf = np.log1p(sub["potential"])
        lq = np.log1p(sub["freq"])
        K = {}
        K["loB"], K["hiB"] = pctl(lb, 0.03), pctl(lb, 0.97)
        K["loF"], K["hiF"] = pctl(lf, 0.03), pctl(lf, 0.97)
        K["loQ"], K["hiQ"] = pctl(lq, 0.03), pctl(lq, 0.97)  # [5] 시간대별
        nb = norm_log(sub["boardings"].values, K["loB"], K["hiB"])
        nf = norm_log(sub["potential"].values, K["loF"], K["hiF"])
        nq = norm_log(sub["freq"].values, K["loQ"], K["hiQ"])
        cov = sub["cov600"].values

        D = ALPHA_D * nb + (1 - ALPHA_D) * nf
        S = W_FREQ * nq + W_COV * cov
        K["mD"], K["sD"] = zstats(D)
        K["mS"], K["sS"] = zstats(S)
        zD = (D - K["mD"]) / K["sD"]
        zS = (S - K["mS"]) / K["sS"]
        K["dRef"] = pctl(D, 0.55) + 1e-9
        K["fRef"] = pctl(nf, QUAD["fref_q"])
        damp = np.clip(D / K["dRef"], 0, 1) ** DAMP_EXP
        mi_raw = (zD - zS) * damp
        mi = np.clip(mi_raw, -MI_CLAMP, MI_CLAMP)
        mi_naive = np.clip(zD - zS, -MI_CLAMP, MI_CLAMP)

        # 4분면 (판정 순서: need > over > drt > ok > mid)
        quad = np.full(len(sub), "mid", dtype=object)
        m_need = (zD >= QUAD["need_zd"]) & (mi >= QUAD["need_mi"])
        m_over = ~m_need & (zD <= QUAD["over_zd"]) & (zS >= QUAD["over_zs"])
        m_drt = (~m_need & ~m_over
                 & (zD <= QUAD["drt_zd"]) & (zS <= QUAD["drt_zs"])
                 & (nf >= K["fRef"]))
        m_ok = ~m_need & ~m_over & ~m_drt & (zD >= QUAD["ok_zd"]) & (zS >= QUAD["ok_zs"])
        quad[m_need], quad[m_over], quad[m_drt], quad[m_ok] = "need", "over", "drt", "ok"

        pw = popW.reindex(sub["grid_id"]).values
        e_clip = np.minimum(sub["elderly_ratio"].values, 1.0)
        pri = np.where(m_need, mi_raw * (0.35 + pw) * (1 + ELD_COEF * e_clip), 0.0)

        # action: drt/무운행 need → DRT / 접근 열위 → 신설 / 그 외 증편
        freq_v = sub["freq"].values
        action = np.where(cov < 0.42, "NEW_STOP", "ADD_FREQ").astype(object)
        action[m_drt] = "DRT"
        action[m_need & (freq_v < 2.0)] = "DRT"

        # bins — 기준선 5분위 경계 고정
        K["dBinT"] = [pctl(D, q) for q in (0.2, 0.4, 0.6, 0.8)]
        K["sBinT"] = [pctl(S, q) for q in (0.2, 0.4, 0.6, 0.8)]
        K["fBinT"] = [pctl(nf, q) for q in (0.2, 0.4, 0.6, 0.8)]
        bin_mi = np.sum(mi[:, None] > np.array(MI_THRESHOLDS)[None, :], axis=1)
        bin_d = np.sum(D[:, None] > np.array(K["dBinT"])[None, :], axis=1)
        bin_s = np.sum(S[:, None] > np.array(K["sBinT"])[None, :], axis=1)
        bin_f = np.sum(nf[:, None] > np.array(K["fBinT"])[None, :], axis=1)

        norm_stats["periods"][t] = {
            k: (list(map(float, v)) if isinstance(v, list) else float(v))
            for k, v in K.items()
        }
        rows.append(pd.DataFrame({
            "grid_id": sub["grid_id"], "period": t,
            "D": np.round(D, 6), "S": np.round(S, 6),
            "zD": np.round(zD, 4), "zS": np.round(zS, 4),
            "mi": np.round(mi, 4), "mi_raw": np.round(mi_raw, 4),
            "mi_naive": np.round(mi_naive, 4),
            "quadrant": quad, "action": action, "priority": np.round(pri, 5),
            "bin_mi": bin_mi, "bin_demand": bin_d, "bin_supply": bin_s, "bin_flow": bin_f,
            "nf": np.round(nf, 5), "coverage": np.round(cov, 4),
            "freq": sub["freq"], "pop": sub["pop"], "workers": sub["workers"],
            "elderly_ratio": sub["elderly_ratio"], "region": sub["region"],
            "region_code": sub["region_code"], "region_kind": sub["region_kind"],
        }))

    res = pd.concat(rows, ignore_index=True)

    # 저장
    out_cols = [
        "grid_id", "period", "D", "S", "zD", "zS", "mi", "quadrant", "priority",
        "bin_mi", "bin_demand", "bin_supply", "coverage", "freq", "pop", "workers",
        "elderly_ratio", "region", "mi_raw", "mi_naive", "nf", "bin_flow", "action",
        "region_code", "region_kind",
    ]
    res[out_cols].to_csv(D_DIR / "grid_metrics.csv", index=False, encoding="utf-8-sig")
    with open(D_DIR / "norm_stats.json", "w", encoding="utf-8") as f:
        json.dump(norm_stats, f, ensure_ascii=False, indent=2)

    # 요약
    qtab = res.pivot_table(index="quadrant", columns="period", values="grid_id",
                           aggfunc="count", fill_value=0).reindex(["need", "over", "drt", "ok", "mid"])
    print(qtab.to_string())
    n_grid = res["grid_id"].nunique()
    share = (qtab.loc["need"] / n_grid * 100).round(1)
    print("needShare(%):", dict(share))
    for t in ["am", "night"]:
        top = res[(res.period == t) & (res.priority > 0)].nlargest(5, "priority")
        print(f"{t} 우선순위 top-5:", [f"{r.grid_id}({r.region})" for r in top.itertuples()])
    dt = res[(res.period == "am") & res.region.str.startswith("동탄", na=False)]
    print("동탄 계열 am:", dt.groupby("quadrant").size().to_dict())
    print(f"저장: grid_metrics.csv {len(res)}행 · norm_stats.json")
    return res


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"오류: {e}")
