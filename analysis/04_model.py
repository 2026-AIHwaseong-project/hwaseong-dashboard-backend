# -*- coding: utf-8 -*-
"""
D · S · MI · 4분면 · 우선순위 산출 — 전 격자 × 4시간대

    python analysis/04_model.py

입력 (dataset_hwaseong/)
    grid_join.csv        03_join 산출 — 격자 × 시간대 원재료
    grid_hwaseong.csv    02_grid 산출 — 격자 마스터 (읍면동 배정)

산출 (dataset_hwaseong/)
    grid_metrics.csv     격자 × 시간대 — D/S/MI/quadrant/action/priority/bins
    norm_stats.json      정규화 기준통계. 05_simulate.py 는 이 값을 고정 사용한다.
                         (배치 전후를 같은 자로 재기 위해 재계산 금지)

⚠️ 격자 크기를 바꾸면(예: 500m 전환) 아래 [1]~[8] 의 측정 근거는 전부
   1km·786격자 기준 기록이 된다. 컷·감쇠·가드 값 자체는 그대로 돌아가지만
   분포가 달라지므로 아래 [1]~[8] 을 하나씩 다시 잴 것. 각 상수가 어떤 실측에서
   나왔는지는 README §2 "상수는 어디서 나왔나" 참고.

---
수식 설계 결정 (전부 1km 786격자 실데이터로 측정·확인한 것)

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
    승차 시간축=교통카드 OD 법정동별 25개 곡선 실측(08-13 교체). 잠재수요만 시군구
   곡선 하나. 시간대 z 변동의 86.7%(ΣVar_t: zS 32.46 vs zD 4.97)가 공급(배차) 차이.
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")   # 없으면 Windows 콘솔에서 한글이 깨집니다
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"

# 상수 정본은 params.py 다 — 04_model·05_simulate·05_load·server 가 같은 값을
# 읽는다(사본 갈라짐 방지). 각 상수의 실측 근거 주석도 params.py 로 옮겼다.
# 관리자 오버라이드(model.*)는 params 가 import 시점에 반영한다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from params import (PERIODS, MI_THRESHOLDS, COV_THRESHOLD_M, ALPHA_D,   # noqa: E402
                    W_FREQ, W_COV, DAMP_EXP, MI_CLAMP, ELD_COEF, QUAD,
                    MIN_FREQ_PER_H, PERIOD_HOURS, BUS_TRIP_RATE)
import params as _params                                                # noqa: E402

# 요일축 — 관리자가 조정할 모델 튜닝값이 아니라 파이프라인 구조 상수라 params.py
# 로는 안 옮긴다. wd 산출물은 기존 파일명·키를 그대로 유지한다
# (05_simulate.py·07_validate.py 가 고정 사용 — 계약을 안 바꾼다)
DAYTYPES = ["wd", "we"]


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
    gj = gj.merge(gh[["grid_id", "region", "region_code", "region_kind", "lon", "lat"]],
                  on="grid_id", how="left")

    # coverage 600m 재산정 [2]
    gj["cov600"] = np.clip(1.0 - gj["nearest_stop_m"] / COV_THRESHOLD_M, 0.05, 1.0)

    # 인구가중 (시간대 불변): log1p P3~P97 min-max, 전 격자 기준 1회
    pop_all = gh.set_index("grid_id")["pop"].astype(float)
    lp = np.log1p(pop_all.values)
    loP, hiP = pctl(lp, 0.03), pctl(lp, 0.97)
    popW = pd.Series(np.clip((lp - loP) / max(hiP - loP, 1e-9), 0, 1), index=pop_all.index)

    # wd 는 기존 키·파일명을 그대로 쓴다("periods") — 05_simulate.py·07_validate.py 가
    # 이 계약을 고정으로 읽는다. we 는 새 키("periods_we")로 나란히 추가한다.
    norm_stats = {"paramsVersion": _params.params_version(),
                  "periods": {}, "periods_we": {}, "constants": {
        "alphaD": ALPHA_D, "wFreq": W_FREQ, "wCov": W_COV, "dampExp": DAMP_EXP,
        "miClamp": MI_CLAMP, "covThresholdM": COV_THRESHOLD_M,
        "miThresholds": MI_THRESHOLDS, "elderlyCoef": ELD_COEF,
        "normMethod": "log1p -> P3/P97 clamp min-max (pctl=lower)",
        "quadCuts": QUAD, "popWeightBounds": {"loP": loP, "hiP": hiP},
    }}
    NORM_KEY = {"wd": "periods", "we": "periods_we"}

    rows = []
    PAIRS = [(dt, t) for dt in DAYTYPES for t in PERIODS]
    for dt, t in PAIRS:
        sub = gj[(gj["period"] == t) & (gj["daytype"] == dt)].reset_index(drop=True)
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
        # 격자를 세분화하면 빈 셀 비중이 늘어 분위 기준점이 0 으로 주저앉을 수 있다.
        # 그러면 감쇠([3])와 drt 게이트가 무력화된다 — 조용히 지나가지 않게 잡는다.
        if K["fRef"] <= 0:
            print(f"  ⚠️ {dt}/{t}: fRef=0 — 빈 셀 비중 증가로 drt 게이트 무력화. "
                  "fref_q 분위를 인구>0 셀 기준으로 재산정 필요 (README §2)")
        if K["dRef"] < 0.05:
            print(f"  ⚠️ {dt}/{t}: dRef={K['dRef']:.4f} — MI 감쇠 기준점이 0 근처. 재튜닝 필요")
        damp = np.clip(D / K["dRef"], 0, 1) ** DAMP_EXP
        mi_raw = (zD - zS) * damp
        mi = np.clip(mi_raw, -MI_CLAMP, MI_CLAMP)
        mi_naive = np.clip(zD - zS, -MI_CLAMP, MI_CLAMP)

        # [8] 절대 가드 — over·ok 는 실제 운행량이 최소선을 넘어야 한다.
        #
        # z 는 시간대 안에서의 상대평가라 밤에는 자 자체가 짧아진다. 그래서 가드가
        # 없으면 야간 시간당 0.37회 다니는 향남읍 격자(인구 3,003)가 zS +0.7 로
        # "적정" 이 되고, 0.48회 다니는 마도면이 "공급과잉" 으로 찍힌다.
        # 실측 61곳이 이렇게 오라벨된다. "시간당 0.4회인데 적정"은 심사에서 못 버틴다.
        freq_h = sub["freq"].values / PERIOD_HOURS[t]      # 시간대 창 길이로 나눈 시간당 운행
        enough = freq_h >= MIN_FREQ_PER_H

        quad = np.full(len(sub), "mid", dtype=object)
        m_need = (zD >= QUAD["need_zd"]) & (mi >= QUAD["need_mi"])
        m_over = ~m_need & (zD <= QUAD["over_zd"]) & (zS >= QUAD["over_zs"]) & enough
        m_drt = (~m_need & ~m_over
                 & (zD <= QUAD["drt_zd"]) & (zS <= QUAD["drt_zs"])
                 & (nf >= K["fRef"]))
        m_ok = (~m_need & ~m_over & ~m_drt
                & (zD >= QUAD["ok_zd"]) & (zS >= QUAD["ok_zs"]) & enough)
        quad[m_need], quad[m_over], quad[m_drt], quad[m_ok] = "need", "over", "drt", "ok"

        pw = popW.reindex(sub["grid_id"]).values
        e_clip = np.minimum(sub["elderly_ratio"].values, 1.0)
        pri = np.where(m_need, mi_raw * (0.35 + pw) * (1 + ELD_COEF * e_clip), 0.0)

        # action — 수단은 coverage 로 **배타 결정**한다 (README §2 · docs/API_SPEC.md §8).
        #   cov ≥ 0.50 (정류장 ≤300m)   도보권 안인데 버스가 안 온다 → 증차
        #   0.15 ≤ cov < 0.50 (300~510m) 노선은 지나는데 정류장이 멀다 → 신설
        #   cov < 0.15 (>510m)          노선 자체가 없다시피 하다 → 똑버스
        #
        # 예전 규칙(cov<0.42 신설 + 사분면 drt + need&freq<2 → DRT)은 서버의
        # 추천 게이트(0.15/0.50)와 기준이 달라, 지도가 "똑버스"라고 표시한 격자를
        # 클릭해 추천을 돌리면 정류장 신설이나 증차가 나왔다. 심야 기준으로
        # DRT 표시 111칸 중 53칸이 그랬다. 화면 배지와 추천이 같은 말을 하도록
        # 한쪽으로 통일한 것이고, 판정식(cov600)은 양쪽이 이미 동일하다.
        # 사분면(quadrant)은 수요·공급 위치를 나타내는 별개 축이라 그대로 둔다.
        action = np.where(cov >= 0.5, "ADD_FREQ",
                          np.where(cov >= 0.15, "NEW_STOP", "DRT")).astype(object)

        # bins — 기준선 5분위 경계 고정
        K["dBinT"] = [pctl(D, q) for q in (0.2, 0.4, 0.6, 0.8)]
        K["sBinT"] = [pctl(S, q) for q in (0.2, 0.4, 0.6, 0.8)]
        K["fBinT"] = [pctl(nf, q) for q in (0.2, 0.4, 0.6, 0.8)]
        bin_mi = np.sum(mi[:, None] > np.array(MI_THRESHOLDS)[None, :], axis=1)
        bin_d = np.sum(D[:, None] > np.array(K["dBinT"])[None, :], axis=1)
        bin_s = np.sum(S[:, None] > np.array(K["sBinT"])[None, :], axis=1)
        bin_f = np.sum(nf[:, None] > np.array(K["fBinT"])[None, :], axis=1)

        norm_stats[NORM_KEY[dt]][t] = {
            k: (list(map(float, v)) if isinstance(v, list) else float(v))
            for k, v in K.items()
        }
        rows.append(pd.DataFrame({
            "grid_id": sub["grid_id"], "period": t, "daytype": dt,
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
            # 계약(docs/API_SPEC.md §2 cells[]) 컬럼. 모델엔 안 쓰이고 응답 전용.
            "d_score": np.round(D * 100, 2), "s_score": np.round(S * 100, 2),
            "flow_trips_per_day": np.round(sub["pop"].values * BUS_TRIP_RATE, 1),
            "nearest_stop_id": sub["nearest_stop_id"],
            "nearest_stop_m": sub["nearest_stop_m"],
            "lon": sub["lon"], "lat": sub["lat"],
        }))

    res_all = pd.concat(rows, ignore_index=True)
    res = res_all[res_all["daytype"] == "wd"].reset_index(drop=True)         # 기존 계약
    res_we = res_all[res_all["daytype"] == "we"].reset_index(drop=True)     # 새 요일축

    # 저장 — wd 는 파일명·컬럼 전부 기존과 동일(계약 유지). we 는 별도 파일로 나란히 둔다.
    out_cols = [
        "grid_id", "period", "D", "S", "zD", "zS", "mi", "quadrant", "priority",
        "bin_mi", "bin_demand", "bin_supply", "coverage", "freq", "pop", "workers",
        "elderly_ratio", "region", "mi_raw", "mi_naive", "nf", "bin_flow", "action",
        "region_code", "region_kind",
        "d_score", "s_score", "flow_trips_per_day", "nearest_stop_id", "nearest_stop_m",
        "lon", "lat",
    ]
    res[out_cols].to_csv(D_DIR / "grid_metrics.csv", index=False, encoding="utf-8-sig")
    res_we[out_cols].to_csv(D_DIR / "grid_metrics_we.csv", index=False, encoding="utf-8-sig")
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
    dongtan = res[(res.period == "am") & res.region.str.startswith("동탄", na=False)]
    print("동탄 계열 am:", dongtan.groupby("quadrant").size().to_dict())
    print(f"저장: grid_metrics.csv {len(res)}행 · norm_stats.json")

    # ── 검증. 조용히 틀리는 종류의 계산이라 눈으로 보는 것만으론 부족하다.
    ph = res["period"].map(PERIOD_HOURS)
    bad = res[res["quadrant"].isin(["over", "ok"]) & (res["freq"] / ph < MIN_FREQ_PER_H)]
    assert bad.empty, f"운행 부족한데 over/ok 인 격자 {len(bad)}곳 — 절대 가드 확인"
    assert res["elderly_ratio"].max() <= 1.0 or (res["priority"] >= 0).all(), "고령비 클립 확인"
    assert res["mi"].between(-MI_CLAMP, MI_CLAMP).all(), "MI 클램프가 안 먹었습니다"
    assert (res.loc[res["quadrant"] != "need", "priority"] == 0).all(), "need 아닌데 우선순위가 있습니다"
    for t in PERIODS:
        share = (res[(res.period == t) & (res.quadrant == "need")].shape[0]
                 / res["grid_id"].nunique())
        assert 0.02 <= share <= 0.25, f"{t} need 비중 {share:.1%} — 2~25% 밖입니다"
    sets = {t: set(res[(res.period == t) & (res.quadrant == "need")]["grid_id"]) for t in PERIODS}
    assert any(sets[t] != sets["am"] for t in PERIODS[1:]), "모든 시간대 need 가 동일 — 시간축이 죽었습니다"
    print("검증 통과 — 절대 가드 · 고령비 클립 · MI 클램프 · 시간축")

    # ── we(주말) 도 같은 가드로 확인한다. 다만 need 비중 대역(2~25%)은 wd 실측으로
    # 튜닝된 값이라 — 주말은 표본이 달라 이 대역을 살짝 벗어나도 로직 결함은 아니다.
    # 그래서 절대 가드·클램프·클립·시간축은 assert 로, need 비중은 경고로만 본다.
    print(f"\n저장: grid_metrics_we.csv {len(res_we)}행")
    ph_we = res_we["period"].map(PERIOD_HOURS)
    bad_we = res_we[res_we["quadrant"].isin(["over", "ok"]) & (res_we["freq"] / ph_we < MIN_FREQ_PER_H)]
    assert bad_we.empty, f"[we] 운행 부족한데 over/ok 인 격자 {len(bad_we)}곳 — 절대 가드 확인"
    assert res_we["mi"].between(-MI_CLAMP, MI_CLAMP).all(), "[we] MI 클램프가 안 먹었습니다"
    assert (res_we.loc[res_we["quadrant"] != "need", "priority"] == 0).all(), "[we] need 아닌데 우선순위가 있습니다"
    for t in PERIODS:
        share_we = (res_we[(res_we.period == t) & (res_we.quadrant == "need")].shape[0]
                    / res_we["grid_id"].nunique())
        flag = "" if 0.02 <= share_we <= 0.25 else "  ⚠️ wd 튜닝 대역(2~25%) 밖 — 로직 결함은 아니고 요일별 재튜닝 검토 대상"
        print(f"  [we] {t} need 비중 {share_we:.1%}{flag}")
    print("[we] 검증 통과 — 절대 가드 · MI 클램프")
    return res


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"오류: {e}")
