# -*- coding: utf-8 -*-
"""
민감도 분석 — 상수를 흔들면 결론이 얼마나 달라지는가

    python analysis/11_sensitivity.py              # 전체
    python analysis/11_sensitivity.py --model      # 가중치·거리기준만 (평일)
    python analysis/11_sensitivity.py --weekend    # 주말 민감도
    python analysis/11_sensitivity.py --weights    # 가중치 근거를 데이터에서 찾을 수 있는가
    python analysis/11_sensitivity.py --cost       # 사업 단가 · 비교 기간

산출은 표준출력뿐입니다. **어떤 파일도 쓰지 않습니다** — 산출물 디렉토리를
건드리면 E2E 가 작업 트리를 스왑한 사고(2026-08-26, norm_stats 오염)가 재발합니다.

왜 필요한가
    04_model.py 의 상수 중 `COV_THRESHOLD_M`·`need_mi`·`DAMP_EXP` 등은 README §4
    「상수는 어디서 나왔나」에 실측 근거가 있지만, **수요 가중 `ALPHA_D`(0.5/0.5)와
    공급 가중 `W_FREQ`(0.78/0.22)는 근거가 기록돼 있지 않습니다.** 근거를 만들 수
    없다면 최소한 "그 값이 결론을 얼마나 좌우하는가"는 재고 넘어가야 합니다.

    사업 단가도 같습니다. 정류장 4,200만 원은 공개자료로 확인하지 못한 가정값이라
    (README §4 「단가 근거」), 그 불확실성이 추천 결과를 바꾸는지 재야 합니다.

읽는 법
    [0] 재현 검증이 실패하면 나머지 수치는 의미가 없습니다. 반드시 통과를 확인하세요.
    Top10 유지율이 낮을수록 그 상수에 민감하다는 뜻입니다.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "dataset_hwaseong"

sys.path.insert(0, str(ROOT / "analysis"))
import params as P  # noqa: E402  — 상수 정본

PERIODS = P.PERIODS
PERIOD_HOURS = P.PERIOD_HOURS
QUAD = P.QUAD


def pctl(arr, q):
    """04_model.py 와 같은 lower 방식(보간 없음). 바꾸면 재현이 깨집니다."""
    a = np.sort(np.asarray(arr, dtype=float))
    return float(a[int(np.floor(q * (len(a) - 1)))])


def norm_log(v, lo, hi):
    return np.clip((np.log1p(v) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)


def zstats(v):
    return float(np.mean(v)), float(np.std(v) + 1e-9)


_gj_all = pd.read_csv(D / "grid_join.csv")
_gh = pd.read_csv(D / "grid_hwaseong.csv")

_pop = _gh.set_index("grid_id")["pop"].astype(float)
_lp = np.log1p(_pop.values)
_loP, _hiP = pctl(_lp, 0.03), pctl(_lp, 0.97)
POPW = pd.Series(np.clip((_lp - _loP) / max(_hiP - _loP, 1e-9), 0, 1), index=_pop.index)
REGION = _gh.set_index("grid_id")["region"]


def run(alpha_d=None, w_freq=None, cov_m=None, damp=None, eld=None, daytype="wd"):
    """04_model.py 의 D·S·MI·사분면·우선순위 산출을 재현합니다.

    인자를 주지 않으면 params.py 기본값 — 그 결과가 grid_metrics.csv 와
    일치해야 합니다(아래 [0]).
    """
    a = P.ALPHA_D if alpha_d is None else alpha_d
    wf = P.W_FREQ if w_freq is None else w_freq
    wc = 1.0 - wf
    cm = P.COV_THRESHOLD_M if cov_m is None else cov_m
    de = P.DAMP_EXP if damp is None else damp
    ec = P.ELD_COEF if eld is None else eld

    gj = _gj_all[_gj_all["daytype"] == daytype].copy()
    gj["cov600"] = np.clip(1.0 - gj["nearest_stop_m"] / cm, 0.05, 1.0)

    out = []
    for t in PERIODS:
        sub = gj[gj["period"] == t].reset_index(drop=True)
        lb, lf, lq = (np.log1p(sub["boardings"]), np.log1p(sub["potential"]),
                      np.log1p(sub["freq"]))
        nb = norm_log(sub["boardings"].values, pctl(lb, 0.03), pctl(lb, 0.97))
        nf = norm_log(sub["potential"].values, pctl(lf, 0.03), pctl(lf, 0.97))
        nq = norm_log(sub["freq"].values, pctl(lq, 0.03), pctl(lq, 0.97))
        cov = sub["cov600"].values

        Dv = a * nb + (1 - a) * nf
        Sv = wf * nq + wc * cov
        mD, sD = zstats(Dv)
        mS, sS = zstats(Sv)
        zD, zS = (Dv - mD) / sD, (Sv - mS) / sS
        dRef = pctl(Dv, 0.55) + 1e-9
        fRef = pctl(nf, QUAD["fref_q"])

        mi_raw = (zD - zS) * (np.clip(Dv / dRef, 0, 1) ** de)
        mi = np.clip(mi_raw, -P.MI_CLAMP, P.MI_CLAMP)

        enough = (sub["freq"].values / PERIOD_HOURS[t]) >= P.MIN_FREQ_PER_H
        quad = np.full(len(sub), "mid", dtype=object)
        m_need = (zD >= QUAD["need_zd"]) & (mi >= QUAD["need_mi"])
        m_over = ~m_need & (zD <= QUAD["over_zd"]) & (zS >= QUAD["over_zs"]) & enough
        m_drt = (~m_need & ~m_over & (zD <= QUAD["drt_zd"])
                 & (zS <= QUAD["drt_zs"]) & (nf >= fRef))
        m_ok = (~m_need & ~m_over & ~m_drt & (zD >= QUAD["ok_zd"])
                & (zS >= QUAD["ok_zs"]) & enough)
        quad[m_need], quad[m_over], quad[m_drt], quad[m_ok] = "need", "over", "drt", "ok"

        pw = POPW.reindex(sub["grid_id"]).values
        e_clip = np.minimum(sub["elderly_ratio"].values, 1.0)
        pri = np.where(m_need, mi_raw * (0.35 + pw) * (1 + ec * e_clip), 0.0)
        action = np.where(cov >= 0.5, "ADD_FREQ",
                          np.where(cov >= 0.15, "NEW_STOP", "DRT")).astype(object)

        out.append(pd.DataFrame({
            "grid_id": sub["grid_id"], "period": t, "mi": np.round(mi, 4),
            "quadrant": quad, "action": action, "priority": np.round(pri, 5),
            "dOverRef": Dv / dRef,
            "region": REGION.reindex(sub["grid_id"]).values,
        }))
    return pd.concat(out, ignore_index=True)


def verify(base, daytype="wd"):
    """[0] 기본값 재현이 커밋된 산출물과 같은가. 실패하면 나머지는 의미가 없습니다."""
    fname = "grid_metrics.csv" if daytype == "wd" else "grid_metrics_we.csv"
    print("=" * 72)
    print(f"[0] 재현 검증 — 기본 상수 산출 vs 커밋된 {fname}")
    print("=" * 72)
    gm = pd.read_csv(D / fname)
    if "daytype" in gm.columns:
        gm = gm[gm["daytype"] == daytype]
    gm = gm[["grid_id", "period", "mi", "quadrant", "action", "priority"]]
    m = base.merge(gm, on=["grid_id", "period"], suffixes=("_new", "_ref"))
    if len(m) != len(base):
        sys.exit(f"조인 실패: {len(m)} != {len(base)}")
    mi_e = float(np.abs(m["mi_new"] - m["mi_ref"]).max())
    pr_e = float(np.abs(m["priority_new"] - m["priority_ref"]).max())
    q_ne = int((m["quadrant_new"] != m["quadrant_ref"]).sum())
    a_ne = int((m["action_new"] != m["action_ref"]).sum())
    print(f"  행 {len(m):,} · mi 최대오차 {mi_e:.6f} · priority 최대오차 {pr_e:.6f}")
    print(f"  quadrant 불일치 {q_ne} · action 불일치 {a_ne}")
    if not (mi_e < 1e-3 and pr_e < 1e-3 and q_ne == 0 and a_ne == 0):
        sys.exit("  → 재현 실패. 민감도 수치를 신뢰할 수 없으므로 중단합니다.")
    print("  → 재현 성공 (이하 수치 유효)\n")


def top_n(df, period, n=10):
    d = df[(df.period == period) & (df.quadrant.isin(["need", "drt"]))]
    return d.sort_values("priority", ascending=False).head(n)


def model_sweep(base, daytype="wd"):
    dn = "평일" if daytype == "wd" else "주말"
    print("=" * 72)
    print(f"[1] 가중치·거리기준 민감도 ({dn} · Top10 = need+drt 우선순위 상위 10칸)")
    print("=" * 72)
    SWEEP = [
        ("수요 가중 α 0.5 → 0.3  (잠재수요 쪽으로)", dict(alpha_d=0.3)),
        ("수요 가중 α 0.5 → 0.7  (실현승차 쪽으로)", dict(alpha_d=0.7)),
        ("공급 가중 wFreq 0.78 → 0.60", dict(w_freq=0.60)),
        ("공급 가중 wFreq 0.78 → 0.90", dict(w_freq=0.90)),
        ("커버리지 임계 600m → 400m", dict(cov_m=400.0)),
        ("커버리지 임계 600m → 800m", dict(cov_m=800.0)),
        ("MI 감쇠 지수 0.65 → 0.40", dict(damp=0.40)),
        ("MI 감쇠 지수 0.65 → 1.00", dict(damp=1.00)),
        ("고령 가중 1.6 → 0.0", dict(eld=0.0)),
        ("고령 가중 1.6 → 3.0", dict(eld=3.0)),
    ]
    summary = {}
    for label, kw in SWEEP:
        var = run(daytype=daytype, **kw)
        keeps = []
        print(f"\n── {label}")
        for p in PERIODS:
            b, v = top_n(base, p), top_n(var, p)
            keep = len(set(b.grid_id) & set(v.grid_id))
            keeps.append(keep)
            breg, vreg = set(b.region.dropna()), set(v.region.dropna())
            nb = int((base[base.period == p].quadrant == "need").sum())
            nv = int((var[var.period == p].quadrant == "need").sum())
            same1 = b.iloc[0].grid_id == v.iloc[0].grid_id if len(b) and len(v) else True
            print(f"   {p:5s} Top10 {keep:2d}/10 · 읍면동 {len(breg & vreg)}/{len(breg)} · "
                  f"need {nb}→{nv}{'' if same1 else '  ← 1위 변동'}")
        summary[label] = float(np.mean(keeps))
    print("\n" + "=" * 72)
    print("[2] 요약 — Top10 유지율 (낮을수록 민감)")
    print("=" * 72)
    for label, avg in sorted(summary.items(), key=lambda t: t[1]):
        print(f"  {avg*10:5.0f}%  {'█' * int(avg):<10s}  {label}")


def damp_saturation(base):
    print("\n" + "=" * 72)
    print("[3] MI 감쇠 지수가 순위를 안 바꾸는 이유 — need 격자에서 감쇠항이 포화")
    print("=" * 72)
    print("  감쇠 = clip(D/dRef, 0, 1)^DAMP_EXP 이므로 D ≥ dRef 면 지수와 무관하게 1.")
    for p in PERIODS:
        n = base[(base.period == p) & (base.quadrant == "need")]
        r = n["dOverRef"].values
        print(f"   {p:5s} need {len(n):2d}칸 · D/dRef 최소 {r.min():.2f} · "
              f"1.0 이상 {(r >= 1).sum()}/{len(n)}칸")
    print("  → 감쇠항은 **빈 땅을 걸러내는 장치**이지 순위를 만드는 장치가 아닙니다.")


def weight_evidence():
    """[5] 가중치 근거를 데이터에서 찾을 수 있는가 — 찾을 수 없다는 것까지가 결론."""
    from sklearn.linear_model import PoissonRegressor
    from sklearn.model_selection import GroupKFold
    from sklearn.preprocessing import StandardScaler

    print("\n" + "=" * 72)
    print("[5] 공급 가중 wFreq — 승차 데이터로 정할 수 있는가")
    print("=" * 72)

    gj = _gj_all[_gj_all["daytype"] == "wd"]
    day = gj[gj.period == "am"].set_index("grid_id").copy()
    day["freq_day"] = gj.groupby("grid_id")["freq"].sum()
    day = day.join(_gh.set_index("grid_id")[["region"]], rsuffix="_g")
    # S 가 실제로 쓰는 자(600m). grid_join.coverage 는 800m 라 다른 값이다.
    day["cov600"] = np.clip(1.0 - day["nearest_stop_m"] / P.COV_THRESHOLD_M, 0.05, 1.0)
    y = day["board_day"].values
    groups = day["region"].values

    X = pd.DataFrame({
        "log_pop": np.log1p(day["pop"]),
        "log_freq": np.log1p(day["freq_day"]),
        "cov600": day["cov600"],
        "log_workers": np.log1p(day["workers"]),
        "elderly_ratio": np.minimum(day["elderly_ratio"], 1.0),
        "log_rail": np.log1p(day["rail_m"].fillna(day["rail_m"].median())),
    })
    sc = StandardScaler().fit(X.values)
    m = PoissonRegressor(alpha=1e-3, max_iter=5000).fit(sc.transform(X.values), y)
    print("  (a) 표준화 계수 — 1 표준편차 변화당 효과. 상대 비교가 가능한 형태.")
    for f, b in sorted(zip(X.columns, m.coef_), key=lambda t: -abs(t[1])):
        print(f"      {f:15} {b:+.4f}")
    d = dict(zip(X.columns, m.coef_))
    bf, bc = abs(d["log_freq"]), abs(d["cov600"])
    print(f"      → 승차 설명력 기준 운행빈도:커버리지 = "
          f"{bf/(bf+bc)*100:.1f} : {bc/(bf+bc)*100:.1f}")

    lq = np.log1p(day["freq_day"])
    nq = norm_log(day["freq_day"].values, pctl(lq, 0.03), pctl(lq, 0.97))
    cov = day["cov600"].values
    basec = np.column_stack([np.log1p(day["pop"].values), np.log1p(day["workers"].values)])

    def cv_r2(Xm):
        pred = np.zeros(len(y))
        for tr, te in GroupKFold(n_splits=5).split(Xm, y, groups):
            s = StandardScaler().fit(Xm[tr])
            mm = PoissonRegressor(alpha=1e-3, max_iter=5000).fit(s.transform(Xm[tr]), y[tr])
            pred[te] = mm.predict(s.transform(Xm[te]))
        a, p = np.log1p(y), np.log1p(pred)
        return 1 - ((a - p) ** 2).sum() / ((a - a.mean()) ** 2).sum()

    print("\n  (b) wFreq 를 쓸어 S 의 승차 설명력(공간 CV R²)을 잰다")
    rows = [(w, cv_r2(np.column_stack([basec, w * nq + (1 - w) * cov])))
            for w in (0.0, 0.2, 0.4, 0.6, 0.78, 0.9, 1.0)]
    for w, r in rows:
        tag = "  ← 현행" if abs(w - P.W_FREQ) < 1e-9 else ""
        print(f"      wFreq {w:4.2f}   CV R² {r:.4f}{tag}")
    best = max(rows, key=lambda t: t[1])
    cur = [r for w, r in rows if abs(w - P.W_FREQ) < 1e-9][0]
    print(f"      → 최대는 wFreq={best[0]:.2f} ({best[1]:.4f}), "
          f"현행 대비 {best[1]-cur:+.4f} ({(best[1]-cur)/cur*100:+.2f}%)")

    print("\n  (c) 그런데 이 기준은 무효다 — 커버리지를 빼면 무엇이 사라지는가")
    print("      운행 0 격자는 승차도 0 이라, 승차 적합은 그 칸들을 구분할 수 없다.")
    print("      바로 그 칸들이 이 도구가 찾으려는 대상이다.\n")
    print("      기간   운행0칸  인구>0  최대동률군(wF=0.78)  (wF=1.00)   차이")
    for t in PERIODS:
        s = gj[gj.period == t].reset_index(drop=True)
        nqt = norm_log(s["freq"].values, pctl(np.log1p(s["freq"]), .03),
                       pctl(np.log1p(s["freq"]), .97))
        covt = np.clip(1 - s["nearest_stop_m"].values / P.COV_THRESHOLD_M, 0.05, 1.0)
        z = s["freq"].values == 0
        a = np.unique(np.round(P.W_FREQ * nqt + (1 - P.W_FREQ) * covt, 6),
                      return_counts=True)[1].max()
        b = np.unique(np.round(nqt, 6), return_counts=True)[1].max()
        print(f"      {t:6s} {int(z.sum()):>6}  {int((z & (s['pop'].values > 0)).sum()):>6}"
              f"  {a:>17}  {b:>9}  {b-a:>6}")
    print("      → 심야에 156칸의 구분이 사라진다. need 가 가장 많은 시간대다.")

    print("\n" + "=" * 72)
    print("[6] 수요 가중 ALPHA_D — 적합으로 정하면 순환")
    print("=" * 72)
    print("  D = α·norm(승차) + (1−α)·norm(잠재수요) 이고 검증 좌변이 승차이므로,")
    print("  '승차를 가장 잘 맞히는 α' 는 자명하게 1.0 이다(승차로 승차를 설명).")
    print("  ALPHA_D 는 적합 대상이 아니라 **정책적 선언**이다 —")
    print("  '버스가 없어서 못 탄 사람을 실제로 탄 사람과 같은 비중으로 본다'.\n")
    qualitative_check()


# verified 읍면동 — 07_validate.py 의 QUALITATIVE 중 verified=True 인 6건.
VERIFIED = ["새솔동", "비봉면", "동탄9동", "봉담읍", "진안동", "동탄7동"]


def qual_hits(alpha_d=None, w_freq=None):
    """07_validate.py [3]·[3-1] 과 **같은 지표**로 정성 대조 적중 수를 센다.

    정의: QUAL_PERIODS=['am','night'] · TOP_N=5 — 각 시간대에서 priority>0 격자를
    점수 내림차순으로 상위 5**격자**. verified 읍면동이 두 시간대 Top5 중
    **어느 한쪽에라도** 등장하면 적중.

    ⚠️ 이 지표를 임의로 바꾸면(예: 출근만, Top10, 읍면동 단위) README §6 의
       '6건 중 4건' 과 비교할 수 없는 다른 수가 나온다. 실제로 그렇게 어긋난 적이
       있다 — 출근 Top10 읍면동으로 세면 α=1.0 이 더 좋아 보인다.
    """
    hit, detail = set(), {}
    for p in ("am", "night"):
        v = run(alpha_d=alpha_d, w_freq=w_freq)
        d = v[(v.period == p) & (v.priority > 0)].sort_values("priority", ascending=False)
        top = d.head(5)
        detail[p] = list(top.region)
        hit |= set(top.region.dropna())
    return len([r for r in VERIFIED if r in hit]), detail


def qualitative_check():
    print(f"  정성 대조 — verified {len(VERIFIED)}건 중 (출근 Top5 ∪ 심야 Top5) 적중 수")
    print(f"  지표는 07_validate.py 와 동일: {', '.join(VERIFIED)}\n")
    print("   [α 스윕]  wFreq 고정")
    for a in (0.0, 0.3, 0.5, 0.7, 1.0):
        n, det = qual_hits(alpha_d=a)
        tag = "  ← 현행" if a == P.ALPHA_D else ("  ← 승차 적합 최적" if a == 1.0 else "")
        print(f"     α={a:.1f}  적중 {n}/{len(VERIFIED)}{tag}")
    print("\n   [wFreq 스윕]  α 고정")
    for w in (0.60, 0.78, 0.90, 1.00):
        n, _ = qual_hits(w_freq=w)
        tag = "  ← 현행" if abs(w - P.W_FREQ) < 1e-9 else ""
        print(f"     wFreq={w:.2f}  적중 {n}/{len(VERIFIED)}{tag}")
    print("\n  → 4~5 사이에서만 움직인다. n=6 에서 1건 차이는 판별력이 없다.")
    print("     정성 대조는 α·wFreq 어느 쪽도 가르지 못한다 — 현행이 더 낫지도,")
    print("     더 나쁘지도 않다는 것이 이 표본이 말할 수 있는 전부다.")


def cost_sweep():
    print("\n" + "=" * 72)
    print("[4] 사업 단가 민감도 — 서버의 실제 그리디(_greedy)를 그대로 호출")
    print("=" * 72)
    sys.path.insert(0, str(ROOT))
    import server.main as M

    M.DATA.update(M._build_data_snapshot())
    M.admin.init(DATA=M.DATA, COST_KRW=M.COST_KRW, PERIODS=M.PERIODS,
                 build_snapshot=M._build_data_snapshot,
                 QUAD_LABEL=M.QUAD_LABEL, ACTION_LABEL=M.ACTION_LABEL)
    M.admin.apply_runtime_params()

    sim, BUDGET, MAXPL = M.DATA["sim"], P.DEFAULT_BUDGET, 20
    TYPES, PERIOD = ["stop", "drt", "freq"], "am"
    BASE = dict(M.COST_KRW)

    def greedy(costs, strategy="efficiency", budget=None):
        M.COST_KRW.update(costs)
        try:
            pl, _s, stop, _w = M._greedy(sim, strategy, budget or BUDGET, MAXPL,
                                         TYPES, None, PERIOD)
        finally:
            M.COST_KRW.update(BASE)   # 단가는 반드시 원복 — 다음 시나리오가 오염된다
        cells = [x["gid"] for x in pl]
        mix = {m: sum(1 for x in pl if x["mode"] == m) for m in TYPES}
        return cells, mix, sum(x["cost"] for x in pl), stop

    b_cells, b_mix, b_total, b_stop = greedy(BASE)
    print(f"  조건: 예산 {BUDGET/1e8:.0f}억 · 최대 {MAXPL}건 · {PERIOD} · efficiency")
    print(f"  [기준] {len(b_cells)}건 · {b_total/1e8:.1f}억 · {b_mix} · {b_stop}\n")

    SCEN = [
        ("정류장 4,200만 → 6,500만 (성동 스마트쉼터 소형 '24)", {"stop": 65_000_000}),
        ("정류장 4,200만 → 1억 (성동 스마트쉼터 중형 '24)", {"stop": 100_000_000}),
        ("정류장 4,200만 → 2,000만 (BIT 독립형 수준 '16)", {"stop": 20_000_000}),
        ("똑버스 1.8억 → 1.92억 (경기 표준운송원가 '23)", {"drt": 192_000_000}),
        ("똑버스 1.8억 → 2.05억 (수원 표준운송원가 '24)", {"drt": 205_000_000}),
        ("증편 9,500만 → 9,333만 (화성 마을버스 실측)", {"freq": 93_330_000}),
    ]
    for label, delta in SCEN:
        c = dict(BASE)
        c.update(delta)
        cells, mix, total, stop = greedy(c)
        keep = len(set(b_cells) & set(cells))
        print(f"  {label}")
        print(f"    {len(cells)}건 · {total/1e8:.1f}억 · {mix} · "
              f"기준안 격자 유지 {keep}/{len(b_cells)} ({keep/max(len(b_cells),1)*100:.0f}%)")

    print("\n" + "-" * 72)
    print("  [비교 기준] 총사업비 vs 연환산 — 이게 수단 구성을 가장 크게 바꿉니다")
    print("-" * 72)
    a_cells, a_mix, a_total, _ = greedy({"stop": int(P.COST_ANNUAL["stop"]),
                                         "drt": int(P.COST_ANNUAL["drt"]),
                                         "freq": int(P.COST_ANNUAL["freq"])})
    keep = len(set(b_cells) & set(a_cells))
    print(f"    총사업비(현행) : {len(b_cells):2d}건 · {b_total/1e8:5.1f}억 · {b_mix}")
    print(f"    연환산         : {len(a_cells):2d}건 · {a_total/1e8:5.1f}억 · {a_mix}")
    print(f"    격자 유지 {keep}/{len(b_cells)} ({keep/max(len(b_cells),1)*100:.0f}%)")

    print("\n" + "-" * 72)
    print("  [조건 의존성] 위 결과는 efficiency·30억·am 하나의 조건이다")
    print("-" * 72)
    print("    같은 단가 변형을 전략·예산·시간대를 바꿔 가며 다시 잰다.")
    print("    분모는 **각 조건의 자기 기준안 건수**다(조건마다 다르다).\n")
    CONDS = ([(f"{s:11s} 30억 am", s, P.DEFAULT_BUDGET, "am")
              for s in ("efficiency", "equity", "balance", "quick")]
             + [(f"efficiency  {lab} am", "efficiency", b, "am")
                for b, lab in ((500_000_000, " 5억"), (1_000_000_000, "10억"),
                               (10_000_000_000, "100억"))]
             + [(f"efficiency  30억 {p:5s}", "efficiency", P.DEFAULT_BUDGET, p)
                for p in ("day", "pm", "night")])
    VARI = {"정류장 6,500만": {"stop": 65_000_000},
            "정류장 1억": {"stop": 100_000_000},
            "정류장 2,000만": {"stop": 20_000_000}}
    def cells_for(costs, strat, bud, per):
        """greedy() 는 모듈 상수 PERIOD 를 쓰므로, 시간대를 바꿔야 하는 여기서는
        _greedy 를 직접 부른다. 단가 원복은 동일하게 finally 로 보장한다."""
        M.COST_KRW.update(costs)
        try:
            pl, _s, _st, _w = M._greedy(sim, strat, bud, MAXPL, TYPES, None, per)
        finally:
            M.COST_KRW.update(BASE)
        return [x["gid"] for x in pl]

    print(f"    {'조건':26s}{'기준':>5}" + "".join(f"{k:>18s}" for k in VARI))
    worst = (None, 1.0)
    for label, strat, bud, per in CONDS:
        bc = cells_for(BASE, strat, bud, per)
        row = f"    {label:26s}{len(bc):>5}"
        for name, delta in VARI.items():
            c = dict(BASE)
            c.update(delta)
            cv = cells_for(c, strat, bud, per)
            keep, den = len(set(bc) & set(cv)), max(len(bc), 1)
            row += f"{keep:>8}/{den:<3}({keep/den*100:>3.0f}%)"
            if keep / den < worst[1]:
                worst = (f"{label.strip()} · {name}", keep / den)
        print(row)
    print(f"\n    → 최저 유지율 {worst[1]*100:.0f}% ({worst[0]}).")
    print("      efficiency·am 에서 정류장 단가가 무해한 것은 그 조건에서 정류장이")
    print("      애초에 선택되지 않기 때문이며, **다른 조건에는 일반화되지 않는다.**")

    print("\n" + "-" * 72)
    print("  [비교 기간] 다년도 TCO — 예산도 기간만큼 늘려 같은 자로 비교")
    print("-" * 72)
    print("    정류장은 1회성(내용연수 10년), 똑버스·증편은 매년 발생.")
    print(f"    H년 총소유비용: 정류장 {P.COST_TOTAL['stop']/1e8:.2f}억 고정, "
          f"운영수단 ×H. 예산도 {P.DEFAULT_BUDGET/1e8:.0f}억×H.\n")
    print("      H(년)   예산    건수   신설  똑버스  증편   기준안 격자 유지")
    for H in (1, 3, 5, 10):
        tco = {"stop": P.COST_TOTAL["stop"],
               "drt": P.COST_TOTAL["drt"] * H,
               "freq": P.COST_TOTAL["freq"] * H}
        cells, mix, _total, _stop = greedy(tco, budget=P.DEFAULT_BUDGET * H)
        keep = len(set(b_cells) & set(cells))
        tag = "  ← 현행" if H == 1 else ""
        print(f"      {H:>2}    {P.DEFAULT_BUDGET*H/1e8:>4.0f}억  {len(cells):>4}   "
              f"{mix['stop']:>3}   {mix['drt']:>4}  {mix['freq']:>4}   "
              f"{keep:>2}/{len(b_cells)} ({keep/max(len(b_cells),1)*100:>3.0f}%){tag}")

    print("\n" + "-" * 72)
    print("  [전략] 같은 예산·같은 단가에서 목적함수만 바꿨을 때")
    print("-" * 72)
    for s in ["efficiency", "equity", "balance", "quick"]:
        cells, mix, total, _ = greedy(BASE, s)
        keep = len(set(b_cells) & set(cells))
        print(f"    {s:11s} {len(cells):2d}건 · {total/1e8:5.1f}억 · {mix} · "
              f"efficiency 안과 격자 공유 {keep}/{len(b_cells)}")


def main():
    args = set(sys.argv[1:])
    full = not args
    do_model = full or "--model" in args
    do_weekend = full or "--weekend" in args
    do_weights = full or "--weights" in args
    do_cost = full or "--cost" in args

    if do_model or do_weights:
        base = run()
        verify(base)
        if do_model:
            model_sweep(base)
            damp_saturation(base)
        if do_weights:
            weight_evidence()
    if do_weekend:
        print("\n" + "#" * 72)
        print("#  주말(we) — 평일과 별도 산출된 기준선에서 같은 스윕")
        print("#" * 72)
        base_we = run(daytype="we")
        verify(base_we, daytype="we")
        model_sweep(base_we, daytype="we")
    if do_cost:
        cost_sweep()


if __name__ == "__main__":
    main()
