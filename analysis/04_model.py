# -*- coding: utf-8 -*-
"""
D · S · MI · 4분면 · 우선순위 산출 — 786격자 × 4시간대

    python analysis/04_model.py

입력 (dataset_hwaseong/)
    grid_join.csv        03_join 산출 — 격자 × 시간대 원재료
    grid_hwaseong.csv    02_grid 산출 — 격자 마스터 (읍면동 배정)

산출 (dataset_hwaseong/)
    grid_metrics.csv     격자 × 시간대 — D/S/MI/quadrant/action/priority/bins
    norm_stats.json      정규화 기준통계 (mart_norm_stats). 시뮬레이션은 이 값을
                         고정 사용한다 — 배치 전후를 같은 자로 재기 위해(기획서 §8.7).

05_load.py 가 이걸 받아 PostgreSQL(batch_grid_metrics)과 정적 JSON 을 만듭니다.

---
설계 결정 7가지 (전부 786격자 실데이터로 측정하고 정한 것)

[1] 정규화는 log1p 후 P3~P97 클램프 min-max. minmax 원값은 못 쓴다.
    승차량 왜도 6.5, 최대/P99 = 2.7 이라 minmax 를 그대로 쓰면 격자의 82%가
    0~100 점 중 1점 미만에 뭉개진다(동탄·병점 몇 칸이 스케일 독점).
    log1p 적용 시 중간층(P40~P90)이 점수 범위의 61%를 쓰게 되고,
    상위 10 얼굴은 그대로다(방식 간 스피어만 0.98+). robust-z 는 승차 MAD 가
    0.48 로 붕괴해 max 603 으로 폭주 — 탈락.

[2] coverage 임계는 600m. 400m(도보 5분)가 아니다.
    격자 중심~최근접 정류장 거리의 중앙값이 392m 라서 임계 400m 면 인구 격자의
    45%가 바닥값 0.05 로 뭉개진다(1km 격자 중심점의 위치 오차보다 임계가 미세).
    600m 가 판별력 최고(인구격자 std 0.297). "도보 5분 400m + 접근 여유"로 설명.
    grid_join 의 coverage(800m) 컬럼은 쓰지 않고 nearest_stop_m 에서 재산정.

[3] MI 에는 수요규모 감쇠항이 필수다. 순수 z(D)-z(S) 는 빈 땅을 칠한다.
    인구 50 미만 "빈 땅" 183곳의 naive MI 중앙값이 +0.58 (수요 0 이지만 공급이
    더 0 이라서). 감쇠 (D/P55)^0.65 적용 시 need 색(MI>=0.55) 빈 땅이
    133곳 → 0곳. 지수는 0.5~1.0 어디든 순위 불변(스피어만 0.998+).
    need 판정에는 수학적으로 무영향(zD>=0.2 격자의 감쇠 인자 최솟값 = 1.0 실측).

[4] need 컷은 MI >= 0.75 (mock 의 0.55 에서 상향).
    0.55 면 need 가 63~93개로 과다하고, zD 상한(2.67) > zS 상한(2.11) 비대칭
    때문에 운행 411~574회/h 만원공급 동탄 격자 4곳이 need 로 침투한다.
    0.75 상향으로 침투 4곳은 전부 ok 로 이동. [5]~[7] 반영 후 최종
    need = 49/40/48/85 (am/day/pm/night).
    bins 경계도 실측 MI p1~p99 = ±1.3 에 맞춰 ±1.5 → ±1.2 로 축소
    (±1.5 는 극단 2색이 주간에 0~4칸으로 죽는다).

[5] 운행빈도 정규화 상한(hiQ)만 4시간대 공통. z 는 시간대별 유지.
    시간대별 hiQ 를 쓰면 밤엔 잣대 자체가 짧아져서(hiQ 5.40 vs 3.17) 같은
    절대 공급이 밤에는 덜 나쁘게 채점된다. 시간대 탭 간 공급색(bin_supply)
    비교가 가능해지는 것도 이 덕분. z 까지 통합하면 야간 need 가 162개로
    폭발해 그것대로 못 쓴다 — 상한만 통일이 최소침습.

[6] over/ok 판정에 절대 가드 freq >= 2회/h.
    시간대별 상대평가(z)만 쓰면 인구 3,003명에 야간 0.74회/h 인 격자가
    "적정", 0.78회/h 인 매송면이 "공급과잉"으로 찍힌다(야간 상대평가의 산물
    — 주간엔 freq<2 인 ok/over 가 0개). 절대 가드 한 줄로 방어.
    같은 이유로 시간대별 평균 MI 류 KPI 는 금지 — 시간대별 z 라서
    평균이 항상 0 인 항등식이다. 시간대 비교에는 needShare 를 쓸 것.

[7] 공급기아 구제: freq < 2회/h 인데 잠재수요 단독으로 need 인 격자는 need.
    승차 실측이 D 의 절반이라 "버스가 없어서 승차 0 → 저수요 판정" 순환이
    남는다(대표 사례: 향남읍 다사4904, pop 686 · freq 0.19회/h · 잠재수요
    단독 MI 1.45 인데 mid). 잠재수요(potential)만으로 D 를 재계산한 반사실
    MI 가 need 컷을 넘으면 구제한다. 실측 발동: 4개 시간대 합 47건 · 고유
    격자 26개(인구 중앙값 392, 전부 freq<2 — 야간 무운행 진안동 15,890명
    격자 포함). 구제는 분면·조치 라벨에만 적용하고, 우선순위는 모든 격자
    동일하게 실측 MI 로 산정한다 — 반사실 MI 를 순위에 쓰면 인구 300 격자가
    시 전체 2위로 뛰는 과잉이 실측됐다(순환에 의한 과소평가는 한계로 명기).

---
알려진 한계 (수식으로 못 덮는 것 — 발표·보고서에 명기할 것)

· 고용 단독 격자 사각: 다사3907 남양읍(현대차 남양연구소, 종사자 15,749 ·
  인구 198)은 mid. 잠재수요가 거주인구 기반이라 통근 도착수요를 못 본다
  (통근버스 의존이라 시내버스 실측 하차도 49건/일뿐 — 이중 맹점).
  workers>3000 & pop<300 은 시 전체 3곳.
· 시간대별 MI 변화의 91~94%는 공급(배차)이 만든다. 승하차 원본에 시간대가
  없어 수요 시간축은 유동인구 안분(추정)이기 때문. 시간대 토글은
  "공급이 언제 꺼지는가"의 축으로 설명해야 정직하다.
· 야간 서사의 근거는 회귀가 아니라 실측이다: 인구 500 이상 · 야간 운행 0회
  격자 12곳(정남면·화산동 집중). 야간 회귀 탄력성은 시간 안분 순환 때문에
  근거로 쓸 수 없다.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"

PERIODS = ["am", "day", "pm", "night"]

# ---- 상수 (조정 근거는 docstring) ----
COV_THRESHOLD_M = 600.0          # [2]
WALK_M = 800.0                   # 03_join 의 승하차 안분 반경 (국소성 방어에 사용)
ALPHA_D = 0.5                    # D = α·n(승차) + (1-α)·n(잠재) — α 0.3~0.7 top-20 교체 0~1개
W_FREQ, W_COV = 0.78, 0.22       # S 가중 — 회귀 표준화 기여로 재도출 시 0.75:0.25 (가정값 재확인)
DAMP_EXP = 0.65                  # [3]
MI_CLAMP = 2.6
ELD_COEF = 1.6                   # 우선순위 고령 가중 (형평성 정책가중 — 이중계상 <=12%, 각주로 방어)
FREQ_ABS_MIN = 2.0               # [6][7] 절대 가드 · 공급기아 기준 (회/h)
MI_THRESHOLDS = [-1.2, -0.7, -0.25, 0.25, 0.7, 1.2]   # [4]
QUAD = dict(need_zd=0.20, need_mi=0.75, over_zd=-0.30, over_zs=0.30,
            drt_zd=-0.35, drt_zs=-0.35, ok_zd=0.25, ok_zs=0.25, fref_q=0.30)


def pctl(arr, q):
    """분위수 — sorted[floor(q·(n-1))], 보간 없음 (mock.js pctl 과 동일 계약)"""
    a = np.sort(np.asarray(arr, dtype=float))
    return float(a[int(np.floor(q * (len(a) - 1)))])


def norm_log(v, lo, hi):
    return np.clip((np.log1p(v) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)


def zstats(v):
    m = float(np.mean(v))
    sd = float(np.std(v))            # 모집단 std (ddof=0)
    return m, (sd if sd > 0 else 1.0)


def main():
    gj = pd.read_csv(D_DIR / "grid_join.csv")
    gh = pd.read_csv(D_DIR / "grid_hwaseong.csv")
    gj = gj.merge(gh[["grid_id", "region", "region_code", "region_kind"]], on="grid_id", how="left")

    print(f"[1] 입력  grid_join {len(gj)}행 · 격자 {gj.grid_id.nunique()}개")

    # coverage 600m 재산정 [2]
    gj["cov"] = np.clip(1.0 - gj["nearest_stop_m"] / COV_THRESHOLD_M, 0.05, 1.0)

    # 인구가중 (시간대 불변, 786격자 1회)
    pop786 = gh.set_index("grid_id")["pop"].astype(float)
    lp = np.log1p(pop786.values)
    loP, hiP = pctl(lp, 0.03), pctl(lp, 0.97)
    popW = pd.Series(np.clip((lp - loP) / max(hiP - loP, 1e-9), 0, 1), index=pop786.index)

    # 운행빈도 정규화 경계만 4시간대 공통 [5]
    lq_all = np.log1p(gj["freq"].values)
    loQ, hiQ = pctl(lq_all, 0.03), pctl(lq_all, 0.97)

    norm_stats = {
        "constants": {
            "alphaD": ALPHA_D, "wFreq": W_FREQ, "wCov": W_COV, "dampExp": DAMP_EXP,
            "miClamp": MI_CLAMP, "covThresholdM": COV_THRESHOLD_M, "walkM": WALK_M,
            "freqAbsMin": FREQ_ABS_MIN, "elderlyCoef": ELD_COEF,
            "miThresholds": MI_THRESHOLDS, "quadCuts": QUAD,
            "normMethod": "log1p -> P3/P97 clamp min-max (pctl=lower)",
            "loQ": loQ, "hiQ": hiQ,
            "popWeightBounds": {"loP": loP, "hiP": hiP},
        },
        "periods": {},
    }

    rows = []
    rescued_log = []
    for t in PERIODS:
        sub = gj[gj["period"] == t].reset_index(drop=True)
        lb, lf = np.log1p(sub["boardings"]), np.log1p(sub["potential"])
        K = {}
        K["loB"], K["hiB"] = pctl(lb, 0.03), pctl(lb, 0.97)
        K["loF"], K["hiF"] = pctl(lf, 0.03), pctl(lf, 0.97)
        nb = norm_log(sub["boardings"].values, K["loB"], K["hiB"])
        nf = norm_log(sub["potential"].values, K["loF"], K["hiF"])
        nq = norm_log(sub["freq"].values, loQ, hiQ)
        cov = sub["cov"].values
        freq = sub["freq"].values

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

        # 공급기아 반사실 [7]: D 를 잠재수요 단독으로 재계산한 MI
        K["mDnf"], K["sDnf"] = zstats(nf)
        zD_nf = (nf - K["mDnf"]) / K["sDnf"]
        dRef_nf = pctl(nf, 0.55) + 1e-9
        mi_nf = (zD_nf - zS) * np.clip(nf / dRef_nf, 0, 1) ** DAMP_EXP

        # 4분면 (판정 순서: need > over > drt > ok > mid) — [6] 절대 가드 포함
        m_need = (zD >= QUAD["need_zd"]) & (mi >= QUAD["need_mi"])
        m_rescue = (~m_need & (freq < FREQ_ABS_MIN)
                    & (zD_nf >= QUAD["need_zd"]) & (mi_nf >= QUAD["need_mi"]))   # [7]
        m_need = m_need | m_rescue
        m_over = (~m_need & (zD <= QUAD["over_zd"]) & (zS >= QUAD["over_zs"])
                  & (freq >= FREQ_ABS_MIN))
        m_drt = (~m_need & ~m_over & (zD <= QUAD["drt_zd"]) & (zS <= QUAD["drt_zs"])
                 & (nf >= K["fRef"]))
        m_ok = (~m_need & ~m_over & ~m_drt & (zD >= QUAD["ok_zd"]) & (zS >= QUAD["ok_zs"])
                & (freq >= FREQ_ABS_MIN))
        quad = np.full(len(sub), "mid", dtype=object)
        quad[m_need], quad[m_over], quad[m_drt], quad[m_ok] = "need", "over", "drt", "ok"

        # 우선순위 — clamp 이전 MI 사용(상단 동률 방지). 구제 격자도 동일 산식 [7]
        pw = popW.reindex(sub["grid_id"]).values
        e_clip = np.minimum(sub["elderly_ratio"].values, 1.0)   # elderly_ratio>1 이상치 7곳 방어
        pri = np.where(m_need, mi_raw * (0.35 + pw) * (1 + ELD_COEF * e_clip), 0.0)

        # action: drt → DRT / need 인데 사실상 무운행 → DRT / 접근 열위 → 신설 / 그 외 증편
        action = np.where(cov < 0.42, "NEW_STOP", "ADD_FREQ").astype(object)
        action[m_drt] = "DRT"
        action[m_need & (freq < FREQ_ABS_MIN)] = "DRT"

        # bins — 기준선 분위 경계 고정 (지도 채색은 이 값만 쓴다. 프론트는 임계값을 모른다)
        K["dBinT"] = [pctl(D, q) for q in (0.2, 0.4, 0.6, 0.8)]
        K["sBinT"] = [pctl(S, q) for q in (0.2, 0.4, 0.6, 0.8)]
        K["fBinT"] = [pctl(nf, q) for q in (0.2, 0.4, 0.6, 0.8)]
        bin_mi = np.sum(mi[:, None] > np.array(MI_THRESHOLDS)[None, :], axis=1)
        bin_d = np.sum(D[:, None] > np.array(K["dBinT"])[None, :], axis=1)
        bin_s = np.sum(S[:, None] > np.array(K["sBinT"])[None, :], axis=1)
        bin_f = np.sum(nf[:, None] > np.array(K["fBinT"])[None, :], axis=1)

        norm_stats["periods"][t] = {k: (list(map(float, v)) if isinstance(v, list) else float(v))
                                    for k, v in K.items()}
        for g in sub.loc[m_rescue, "grid_id"]:
            rescued_log.append((t, g))

        rows.append(pd.DataFrame({
            "grid_id": sub["grid_id"], "period": t,
            "boardings": sub["boardings"], "potential": sub["potential"],
            "freq": sub["freq"], "coverage": np.round(cov, 4),
            "D": np.round(D, 6), "S": np.round(S, 6),
            "zD": np.round(zD, 4), "zS": np.round(zS, 4),
            "mi": np.round(mi, 4), "mi_raw": np.round(mi_raw, 4),
            "quadrant": quad, "action": action, "priority": np.round(pri, 5),
            "bin_mi": bin_mi, "bin_demand": bin_d, "bin_supply": bin_s, "bin_flow": bin_f,
            "nearest_stop_id": sub["nearest_stop_id"], "nearest_stop_m": sub["nearest_stop_m"],
            "pop": sub["pop"], "workers": sub["workers"],
            "elderly_ratio": sub["elderly_ratio"],
            "region": sub["region"], "region_code": sub["region_code"],
            "region_kind": sub["region_kind"],
        }))

    res = pd.concat(rows, ignore_index=True)

    # ---- [2] 검증 ----
    n_grid = gj.grid_id.nunique()
    assert len(res) == n_grid * 4, f"행수 {len(res)} != {n_grid}x4"
    key_cols = ["D", "S", "mi", "priority", "bin_mi"]
    assert not res[key_cols].isna().any().any(), "핵심 컬럼에 NaN"

    # 국소성 방어: coverage 클램프 발동점(임계×0.95)이 승하차 안분 반경(800m) 이내여야
    # 시뮬레이션의 국소 재계산(반경 밖 ΔMI=0)이 성립한다 — 임계를 800 이상으로 올리면 깨짐
    assert COV_THRESHOLD_M * (1 - 0.05) <= WALK_M, "coverage 임계가 도보권을 초과 — 국소 재계산 불성립"

    qtab = res.pivot_table(index="quadrant", columns="period", values="grid_id",
                           aggfunc="count", fill_value=0).reindex(["need", "over", "drt", "ok", "mid"])
    for t in PERIODS:
        n_need = qtab.loc["need", t]
        assert 15 <= n_need <= 120, f"{t} need {n_need}개 — 컷 재점검 필요"

    ga = res[res.quadrant.isin(["over", "ok"])]
    assert (ga.freq >= FREQ_ABS_MIN).all(), "절대 가드 위반 — freq<2 인 over/ok 존재"
    need = res[res.quadrant == "need"]
    assert (need["pop"] >= 50).all(), "빈 땅(pop<50)이 need 에 침투"
    assert (need.priority > 0).all(), "priority 없는 need 격자"
    am4904 = res[(res.period == "am") & (res.grid_id == "다사4904")]
    assert len(am4904) and am4904.iloc[0].quadrant == "need", "공급기아 구제(다사4904) 미작동"

    # ---- [3] 저장 ----
    res.to_csv(D_DIR / "grid_metrics.csv", index=False, encoding="utf-8-sig")
    with open(D_DIR / "norm_stats.json", "w", encoding="utf-8") as f:
        json.dump(norm_stats, f, ensure_ascii=False, indent=2)
    print(f"[3] 저장  grid_metrics.csv {len(res)}행 · norm_stats.json")

    # ---- [4] 요약 ----
    print("\n[4] quadrant × period")
    print(qtab.to_string())
    share = (qtab.loc["need"] / n_grid * 100).round(1)
    print("    needShare(%):", dict(share))
    print(f"    공급기아 구제 {len(rescued_log)}건:", rescued_log)
    dt = res[(res.period == "am") & res.region.str.startswith("동탄", na=False)]
    print("    동탄 계열 am:", dt.groupby("quadrant").size().to_dict())
    for t in ["am", "night"]:
        top = res[(res.period == t) & (res.priority > 0)].nlargest(5, "priority")
        print(f"    {t} 우선순위 top-5:",
              [f"{r.grid_id}({r.region})" for r in top.itertuples()])
    return res


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit(f"검증 실패: {e}")
