# -*- coding: utf-8 -*-
"""
추천 그리디는 최적해의 몇 %인가 — 작은 문제의 정확해 대조 + 전체 문제의 개선 탐색

    python analysis/12_greedy_optimality.py               # 전체
    python analysis/12_greedy_optimality.py --pairs       # [1] 배치 간 상호작용만
    python analysis/12_greedy_optimality.py --exact       # [2] 소규모 정확해 대조만
    python analysis/12_greedy_optimality.py --full        # [3] 전체 후보 국소탐색만
    python analysis/12_greedy_optimality.py --seeds 5     # [2] 무작위 인스턴스 수 (기본 3)
    python analysis/12_greedy_optimality.py --periods am  # 시간대 제한

산출은 표준출력뿐입니다. 어떤 파일도 쓰지 않습니다(11_sensitivity.py 와 같은 이유).

대조 대상은 **서비스 코드 그 자체**다
    server/main.py 의 `_greedy` 를 import 해서 그대로 부른다. 복제본을 만들어 비교하면
    "복제본이 원본과 같은가"라는 질문이 하나 더 생기기 때문이다. 입력은 계약 JSON
    (DATABASE_URL 을 지운다)과 관리자 오버라이드 없는 기본 상수다.

목적함수 f(S) — `_greedy` 가 매 스텝 더하는 한계값 tB 의 합과 같은 식
    f(S) = Σ_c w_c·μ_c·[ exp(b2·(log1p F_c(S) − lq0_c) + b3·(cov_c(S) − cov0_c)) − 기준선 항 ]
    요청 시간대 하나, 평일 + 주말 합. equity 는 w = 고령가중 × (need·drt 마스크), 나머지는 1.
    F_c(S) = 기준선 운행 + Σ 배치별 주입량 (가산), cov_c(S) = 정류장을 놓은 격자만 1.0.
    [0] 에서 f(그리디 배치) = Σ tB 를 assert 로 확인한다 — 같은 문제를 푸는지의 증명.

문제 (서비스와 같은 제약)
    최대화  f(S)
    제약    Σ 총사업비 ≤ 예산,  |S| ≤ 10(rec.maxPlacements 기본값)
            수단 게이트 — 기준선 커버리지로 배타: stop 0.15~0.5 · freq ≥ 0.5 · drt < 0.15
            정류장을 놓은 격자는 커버리지가 1.0 이 되어 증편이 열린다(서비스와 동일 — 심야 실측 발생)
            balance 는 읍면동당 1건, quick 은 정류장만
    1km 격자는 셀 간격(1,000m)이 정류장 영향 반경(800m)보다 커서, 정류장 하나는 자기 셀의
    커버리지만 바꾼다. 그래서 cov 변화가 배치끼리 겹치지 않고 위 가산 표현이 정확하다(assert).

왜 이렇게 재는가
    [1] 상호작용 — 그리디가 근사 보장(1−1/e 류)을 가지려면 목적함수가 서브모듈러(한계효과
        체감)여야 한다. 쌍 (x, y) 마다 f({x,y}) − f({x}) − f({y}) 를 재서, 양수(상보성)가
        있는지 본다. 하나라도 있으면 보장은 성립하지 않는다.
    [2] 정확해 — 후보 격자를 K(기본 20)개로 줄인 문제에서 실행 가능한 배치 조합을 **전부**
        평가해 최적해를 구한다. 같은 K 격자로 제한한 서비스 그리디와 비교한다.
          real   : 전체 문제에서 그리디가 고른 격자 + 단독 효과 상위 격자.
                   제한 그리디가 전체 그리디와 같은 배치를 내면, 여기서 찾은 최적해는
                   **전체 문제에서도 실행 가능한 해**다 → 전체 문제 격차의 하한.
          random : 후보 풀에서 무작위 K 격자.
    [3] 전체 후보(123~157격자)는 전수 탐색이 불가능하다(C(150,10) ≈ 10^15). 그리디 해에서
        출발해 1건 추가·1:1 교체로 더 나은 해를 찾는다(국소탐색). 찾은 해는 실행 가능한
        해이므로 "최적해는 최소한 이만큼은 된다"는 하한이고, 진짜 최적해는 더 클 수 있다.
        비교용으로 **절대 효과 그리디**(÷비용 없이 ΔB̂ 가 큰 순)도 같이 돌린다.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

os.environ.pop("DATABASE_URL", None)   # 계약 JSON 경로로 고정 — DB 유무와 무관하게 같은 입력
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import server.main as M  # noqa: E402

STRATS = ["efficiency", "equity", "balance", "quick"]
BUDGETS = [300_000_000, 1_000_000_000, 3_000_000_000]   # 3억 · 10억 · 30억(기본값)
MAX_PL = 10
TYPES_ALL = ["stop", "drt", "freq"]


def eok(x):
    return f"{x / 1e8:.2f}억"


class _Cap(Exception):
    pass


# ─── 문제 정의 ─────────────────────────────────────────────────────────────────
class Ctx:
    """(시간대, 전략) 하나의 문제. 후보 풀 전체의 배치 항목과 목적함수 평가기를 든다."""

    def __init__(self, sim, gid_region, period, strategy):
        self.sim, self.p, self.strategy = sim, period, strategy
        s, p = sim, period
        self.has_we = getattr(s, "S0_WE", None) is not None
        assert self.has_we, "주말 기준선(S0_WE)이 없으면 서비스 목적함수와 달라진다"

        # 후보 — server/main.py _greedy 와 같은 마스크
        cand = (np.isin(s.S0[p]["quad0"], ["need", "drt"])
                | np.isin(s.S0_WE[p]["quad0"], ["need", "drt"]))
        self.pool_cells = np.where(cand)[0]

        # 1km 가정: 정류장의 nearest 갱신이 자기 셀에만 닿는다
        Dg = s.Dg.copy()
        np.fill_diagonal(Dg, np.inf)
        assert Dg.min() > s.R_FINAL["stop"], "셀 간격 ≤ 정류장 반경 — 가산 cov 가정이 깨진다"
        assert Dg.min() > s.WALK, "같은 수단 800m 이격 제약이 발동하는 격자 — 이 스크립트 범위 밖"

        self.cov_gate = np.clip(1 - s.S0[p]["nearest"] / s.COVM, 0.05, 1.0)
        types = ["stop"] if strategy == "quick" else TYPES_ALL
        self.regions = sorted({gid_region.get(s.GIDS[c], "") for c in self.pool_cells} - {""})
        rix = {r: i for i, r in enumerate(self.regions)}

        # 배치 항목: (mode, cell, cost, region_idx, dep)
        items = []
        for c in self.pool_cells:
            cv = self.cov_gate[c]
            reg = rix.get(gid_region.get(s.GIDS[c], ""), -1)
            if 0.15 <= cv < 0.5 and "stop" in types:
                items.append(dict(mode="stop", c=c, reg=reg, dep=-1))
                if "freq" in types:   # 정류장을 놓으면 그 셀 cov=1.0 → 증편이 열린다
                    items.append(dict(mode="freq", c=c, reg=reg, dep=len(items) - 1, dyn=True))
            elif cv >= 0.5 and "freq" in types:
                items.append(dict(mode="freq", c=c, reg=reg, dep=-1))
            elif cv < 0.15 and "drt" in types:
                items.append(dict(mode="drt", c=c, reg=reg, dep=-1))
        for it in items:
            it["cost"] = int(M.COST_KRW[it["mode"]])
            it.setdefault("dyn", False)
        self.items = items
        self.key = {(it["mode"], int(it["c"])): j for j, it in enumerate(items)}

        # 항목별 주입량 (평일·주말 각각) — _greedy 의 상태 갱신식 그대로
        N = s.N
        n = len(items)
        self.INCw = np.zeros((n, N))
        self.INCe = np.zeros((n, N))
        self.DCw = np.zeros((n, N))
        self.DCe = np.zeros((n, N))
        cbw = np.clip(1 - s.S0[p]["nearest"] / s.COVM, 0.05, 1.0)
        cbe = np.clip(1 - s.S0_WE[p]["nearest"] / s.COVM, 0.05, 1.0)
        mult = s.HEADWAY_MULT - 1.0          # 같은 셀 증편은 1회뿐(used) → 항상 첫 배수
        for j, it in enumerate(items):
            d = s.Dg[it["c"]]
            if it["mode"] == "stop":
                m = d <= s.WALK
                self.INCw[j, m] = self.INCe[j, m] = s.FSTAR[p] * (1 - d[m] / s.WALK)
                mc = d <= s.R_FINAL["stop"]
                self.DCw[j, mc] = np.clip(1 - np.minimum(s.S0[p]["nearest"][mc], d[mc]) / s.COVM,
                                          0.05, 1.0) - cbw[mc]
                self.DCe[j, mc] = np.clip(1 - np.minimum(s.S0_WE[p]["nearest"][mc], d[mc]) / s.COVM,
                                          0.05, 1.0) - cbe[mc]
            elif it["mode"] == "drt":
                r = s.R_FINAL["drt"]
                m = d <= r
                self.INCw[j, m] = self.INCe[j, m] = s.PHI[p] * (1 - d[m] / r)
            else:
                ds = np.sqrt((s.SX - s.GX[it["c"]]) ** 2 + (s.SY - s.GY[it["c"]]) ** 2)
                ms = ds <= s.R_FINAL["freq"]
                self.INCw[j] = s.Wsg[:, ms] @ (s.STOP_FREQ[p][ms] * mult)
                self.INCe[j] = s.Wsg[:, ms] @ (s.STOP_FREQ_WE[p][ms] * mult)

        # 목적함수 상수
        Pw, Pe = s.POIS[p], s.POIS_WE[p]
        if strategy == "equity":
            em = cand.astype(float)
            ww, we_ = s.S0[p]["eldw"] * em, s.S0_WE[p]["eldw"] * em
        else:
            ww = we_ = np.ones(N)
        self.c = dict(
            F0w=s.S0[p]["freq"], F0e=s.S0_WE[p]["freq"], cbw=cbw, cbe=cbe,
            lq0w=Pw["lq0"], lq0e=Pe["lq0"], cov0w=Pw["cov0"], cov0e=Pe["cov0"],
            b2w=Pw["b2"], b3w=Pw["b3"], b2e=Pe["b2"], b3e=Pe["b3"],
            wmuw=ww * Pw["mu"], wmue=we_ * Pe["mu"],
        )
        c = self.c
        c["basew"] = np.exp(np.clip(c["b2w"] * (np.log1p(c["F0w"]) - c["lq0w"])
                                    + c["b3w"] * (cbw - c["cov0w"]), -20, 6))
        c["basee"] = np.exp(np.clip(c["b2e"] * (np.log1p(c["F0e"]) - c["lq0e"])
                                    + c["b3e"] * (cbe - c["cov0e"]), -20, 6))
        self.cost = np.array([it["cost"] for it in items], dtype=float)

    # ── 평가 ──
    def sub(self, ids):
        """항목 부분집합의 평가용 축약 — 영향이 닿는 셀(U)만 남긴다."""
        ids = np.asarray(ids, dtype=int)
        c = self.c
        touch = ((np.abs(self.INCw[ids]) + np.abs(self.INCe[ids])
                  + np.abs(self.DCw[ids]) + np.abs(self.DCe[ids])).sum(0) > 0)
        U = np.where(touch & ((c["wmuw"] > 0) | (c["wmue"] > 0)))[0]
        out = {k: (v[U] if isinstance(v, np.ndarray) else v) for k, v in c.items()}
        out.update(INCw=self.INCw[np.ix_(ids, U)], INCe=self.INCe[np.ix_(ids, U)],
                   DCw=self.DCw[np.ix_(ids, U)], DCe=self.DCe[np.ix_(ids, U)])
        return out

    @staticmethod
    def f_X(X, sb):
        Fw = sb["F0w"] + X @ sb["INCw"]
        Cw = sb["cbw"] + X @ sb["DCw"]
        ew = np.clip(sb["b2w"] * (np.log1p(Fw) - sb["lq0w"]) + sb["b3w"] * (Cw - sb["cov0w"]), -20, 6)
        v = (np.exp(ew) - sb["basew"]) @ sb["wmuw"]
        Fe = sb["F0e"] + X @ sb["INCe"]
        Ce = sb["cbe"] + X @ sb["DCe"]
        ee = np.clip(sb["b2e"] * (np.log1p(Fe) - sb["lq0e"]) + sb["b3e"] * (Ce - sb["cov0e"]), -20, 6)
        return v + (np.exp(ee) - sb["basee"]) @ sb["wmue"]

    def f_sets(self, sets, ids=None):
        """sets: 항목 인덱스 리스트들. ids 를 주면 그 축약 공간에서 평가."""
        ids = np.arange(len(self.items)) if ids is None else np.asarray(ids)
        pos = {int(j): i for i, j in enumerate(ids)}
        sb = self.sub(ids)
        X = np.zeros((len(sets), len(ids)))
        for r, S in enumerate(sets):
            for j in S:
                X[r, pos[int(j)]] = 1.0
        return self.f_X(X, sb) if len(sets) else np.zeros(0)

    def feasible(self, S, budget):
        S = set(S)
        if len(S) > MAX_PL or sum(self.items[j]["cost"] for j in S) > budget:
            return False
        for j in S:
            d = self.items[j]["dep"]
            if d >= 0 and d not in S:
                return False
        if self.strategy == "balance":
            regs = [self.items[j]["reg"] for j in S if self.items[j]["reg"] >= 0]
            if len(regs) != len(set(regs)):
                return False
        return True

    # ── 서비스 그리디 ──
    def service_greedy(self, budget, cells=None):
        rid = None if cells is None else {self.sim.GIDS[c] for c in cells}
        placed, _st, stopped, _stwe = M._greedy(self.sim, self.strategy, budget, MAX_PL,
                                                list(TYPES_ALL), rid, self.p)
        S = [self.key[(pl["mode"], int(pl["gi"]))] for pl in placed]
        return S, float(sum(pl["tB"] for pl in placed)), stopped


# ─── 열거 ──────────────────────────────────────────────────────────────────────
def enum_sets(ctx, ids, budget, cap):
    """ids 항목으로 만들 수 있는 실행 가능한 배치 조합 전부(빈 집합 포함)를 비트마스크로."""
    its = [ctx.items[j] for j in ids]
    pos = {int(j): i for i, j in enumerate(ids)}
    costs = [it["cost"] for it in its]
    deps = [pos.get(it["dep"], -2) if it["dep"] >= 0 else -1 for it in its]
    regs = [it["reg"] for it in its]
    bal = ctx.strategy == "balance"
    n = len(its)
    out = []

    def rec(start, mask, cnt, c, rmask):
        out.append(mask)
        if len(out) > cap:
            raise _Cap
        if cnt == MAX_PL:
            return
        for j in range(start, n):
            cj = c + costs[j]
            if cj > budget:
                continue
            d = deps[j]
            if d == -2 or (d >= 0 and not (mask >> d) & 1):
                continue
            r = regs[j]
            if bal and r >= 0:
                if (rmask >> r) & 1:
                    continue
                rec(j + 1, mask | (1 << j), cnt + 1, cj, rmask | (1 << r))
            else:
                rec(j + 1, mask | (1 << j), cnt + 1, cj, rmask)

    rec(0, 0, 0, 0, 0)
    return np.array(out, dtype=np.int64)


def exact_over(ctx, ids, budgets, cap, chunk=20000):
    """ids 항목 공간에서 예산별 최적해. 최대 예산으로 한 번 열거하고 예산별로 거른다."""
    masks = enum_sets(ctx, ids, max(budgets), cap)
    sb = ctx.sub(ids)
    k = len(ids)
    bits = np.arange(k, dtype=np.int64)
    cvec = np.array([ctx.items[j]["cost"] for j in ids], dtype=float)
    vals = np.empty(len(masks))
    costs = np.empty(len(masks))
    for s0 in range(0, len(masks), chunk):
        X = ((masks[s0:s0 + chunk, None] >> bits) & 1).astype(np.float64)
        vals[s0:s0 + chunk] = ctx.f_X(X, sb)
        costs[s0:s0 + chunk] = X @ cvec
    res = {}
    for b in budgets:
        ok = costs <= b
        i = int(np.argmax(np.where(ok, vals, -np.inf)))
        S = [int(ids[t]) for t in range(k) if (masks[i] >> t) & 1]
        res[b] = (float(vals[i]), S, float(costs[i]))
    return res, len(masks)


def cells_items(ctx, cells):
    cs = set(int(c) for c in cells)
    return [j for j, it in enumerate(ctx.items) if int(it["c"]) in cs]


def comp(ctx, S):
    out = {}
    for j in S:
        m = ctx.items[j]["mode"] + ("*" if ctx.items[j]["dyn"] else "")
        out[m] = out.get(m, 0) + 1
    return " ".join(f"{k}{v}" for k, v in sorted(out.items())) or "-"


# ─── 독립 검증 — 서비스 코드 경로로 다시 재기 ──────────────────────────────────────
def service_check(ctx, S, budget):
    """이 스크립트가 찾은 해 S 를 서비스 코드로 다시 잰다(가산 표현을 거치지 않는다).

    1) 배치를 서비스 순서 규칙대로 하나씩 놓으며 매번 서비스와 같은 게이트를 통과하는지
       (정류장 → 그 셀 증편* 순서, 커버리지는 현재 평일 상태에서) 확인한다.
    2) server/main.py `_apply_cumulative`(평일·주말)로 상태를 만들고 sim.Bhat 으로 ΔB̂ 를 잰다.
    둘 중 하나라도 어긋나면 AssertionError."""
    s, p = ctx.sim, ctx.p
    S = sorted(S, key=lambda j: (ctx.items[j]["dyn"], j))     # 증편*은 자기 정류장 뒤
    pls = [{"type": ctx.items[j]["mode"], "cellId": s.GIDS[ctx.items[j]["c"]], "count": 1} for j in S]
    assert len(S) <= MAX_PL and sum(ctx.items[j]["cost"] for j in S) <= budget
    if ctx.strategy == "quick":
        assert all(pl["type"] == "stop" for pl in pls)
    st = None
    regs = []
    for j, pl in zip(S, pls):
        nearest = (s.S0[p]["nearest"] if st is None else st[p]["nearest"])
        cv = float(np.clip(1 - nearest[ctx.items[j]["c"]] / s.COVM, 0.05, 1.0))
        ok = {"stop": 0.15 <= cv < 0.5, "freq": cv >= 0.5, "drt": cv < 0.15}[pl["type"]]
        assert ok, f"게이트 위반 {pl} cov={cv:.3f}"
        st = M._apply_cumulative(s, [pl], st, "wd")
        if ctx.strategy == "balance":
            r = M.DATA["cells"]["am"].get(pl["cellId"], {}).get("region", "")
            assert not (r and r in regs), "balance 읍면동 중복"
            regs.append(r)
    tot = 0.0
    for dt, S0, key in (("wd", s.S0, "w"), ("we", s.S0_WE, "e")):
        stx = M._apply_cumulative(s, pls, None, dt)
        cov1 = np.clip(1 - stx[p]["nearest"] / s.COVM, 0.05, 1.0)
        cov0 = np.clip(1 - S0[p]["nearest"] / s.COVM, 0.05, 1.0)
        w = ctx.c["wmu" + key] / (s.POIS[p] if dt == "wd" else s.POIS_WE[p])["mu"]
        tot += float(((s.Bhat(p, stx[p]["freq"], cov1, dt) - s.Bhat(p, S0[p]["freq"], cov0, dt)) * w).sum())
    f = float(ctx.f_sets([S])[0]) if S else 0.0
    assert abs(tot - f) <= 1e-7 * max(1.0, abs(f)), f"서비스 경로 재측정 불일치: {tot} vs {f}"
    return tot


# ─── [0] 검증 ──────────────────────────────────────────────────────────────────
def selftest_enum(ctx, ncell=7):
    """DFS 열거기가 놓치는 조합이 없는지 — 작은 문제에서 2^n 전수(feasible 필터)와 최적값 대조."""
    cells = list(ctx.pool_cells[:ncell])
    ids = cells_items(ctx, cells)
    res, _ = exact_over(ctx, ids, BUDGETS, cap=10**7)
    n = len(ids)
    allS = [[ids[t] for t in range(n) if (m >> t) & 1] for m in range(1 << n)]
    vals = ctx.f_sets(allS)
    for b in BUDGETS:
        ok = np.array([ctx.feasible(S, b) for S in allS])
        brute = float(vals[ok].max())
        assert abs(brute - res[b][0]) <= 1e-9 * max(1.0, abs(brute)), \
            f"열거기 불일치 {ctx.p}/{ctx.strategy}/{b}: DFS {res[b][0]} vs 전수 {brute}"
    return n


def verify(ctx):
    worst = 0.0
    for b in BUDGETS:
        S, tb, _ = ctx.service_greedy(b)
        f = float(ctx.f_sets([S])[0]) if S else 0.0
        rel = abs(f - tb) / max(abs(tb), 1e-9)
        worst = max(worst, rel)
        assert rel < 1e-6, f"목적함수 불일치 {ctx.p}/{ctx.strategy}/{b}: f={f} Σt={tb}"
        assert ctx.feasible(S, b), "서비스 그리디 해가 이 스크립트의 제약으로 실행 불가능"
    return worst


# ─── [1] 상호작용 ──────────────────────────────────────────────────────────────
def pairs(ctx):
    n = len(ctx.items)
    single = ctx.f_sets([[j] for j in range(n)])
    P = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ctx.items[i], ctx.items[j]
            # 함께 놓일 수 있는 쌍만 — 증편*은 자기 정류장과의 쌍만 실행 가능
            if a["dyn"] or b["dyn"]:
                if not (b["dyn"] and b["dep"] == i):
                    continue
            P.append((i, j))
    if not P:
        return None
    sb = ctx.sub(np.arange(n))
    out = np.empty(len(P))
    for s0 in range(0, len(P), 20000):
        blk = P[s0:s0 + 20000]
        X = np.zeros((len(blk), n))
        for r, (i, j) in enumerate(blk):
            X[r, i] = X[r, j] = 1.0
        out[s0:s0 + len(blk)] = ctx.f_X(X, sb)
    I = np.array([out[r] - single[i] - single[j] for r, (i, j) in enumerate(P)])
    scale = np.array([abs(single[i]) + abs(single[j]) for i, j in P])
    rel = I / np.maximum(scale, 1e-12)
    pos = rel > 1e-9
    neg = rel < -1e-9
    kinds = {}
    for r in np.where(pos)[0]:
        i, j = P[r]
        k = "+".join(sorted([ctx.items[i]["mode"] + ("*" if ctx.items[i]["dyn"] else ""),
                             ctx.items[j]["mode"] + ("*" if ctx.items[j]["dyn"] else "")]))
        kinds[k] = kinds.get(k, 0) + 1
    top = int(np.argmax(rel)) if pos.any() else None
    return dict(n=len(P), pos=int(pos.sum()), neg=int(neg.sum()),
                max_rel=float(rel.max()), kinds=kinds,
                top=(P[top], float(I[top]), float(rel[top])) if top is not None else None)


# ─── [3] 전체 후보 — 국소탐색 · 절대 효과 그리디 ──────────────────────────────────
def local_search(ctx, S0, budget, max_iter=60, bundles=True):
    """bundles=True 면 '정류장 + 같은 셀 증편*' 묶음을 한 번에 넣는 이동(0·1·2건 제거)을 더한다.
    둘은 상보적이라(정류장이 cov 를 1.0 으로 올려 증편 효과를 키움) 1건씩 바꾸는 이동으로는
    서로를 못 찾는다 — 시험 실행에서 1:1 교체가 정확해(+5.1%)를 놓친 이유."""
    n = len(ctx.items)
    S = list(S0)
    cur = float(ctx.f_sets([S])[0]) if S else 0.0
    sb = ctx.sub(np.arange(n))
    pairs_b = [(it["dep"], j) for j, it in enumerate(ctx.items) if it["dyn"]] if bundles else []
    for _ in range(max_iter):
        cand = []
        inS = set(S)
        outs = [j for j in range(n) if j not in inS]
        for j in outs:                                   # 1건 추가
            T = S + [j]
            if ctx.feasible(T, budget):
                cand.append(T)
        for i in S:                                      # 1:1 교체
            base = [x for x in S if x != i]
            for j in outs:
                T = base + [j]
                if ctx.feasible(T, budget):
                    cand.append(T)
        if pairs_b:                                      # 묶음 추가 (0·1·2건 제거)
            rems = [()] + [(i,) for i in S] + [(S[x], S[y]) for x in range(len(S))
                                               for y in range(x + 1, len(S))]
            for si, fj in pairs_b:
                if si in inS or fj in inS:
                    continue
                for rem in rems:
                    T = [x for x in S if x not in rem] + [si, fj]
                    if ctx.feasible(T, budget):
                        cand.append(T)
        if not cand:
            break
        X = np.zeros((len(cand), n))
        for r, T in enumerate(cand):
            X[r, T] = 1.0
        v = ctx.f_X(X, sb)
        r = int(np.argmax(v))
        if v[r] <= cur * (1 + 1e-9) + 1e-9:
            break
        S, cur = cand[r], float(v[r])
    return S, cur


def bundle_greedy(ctx, budget, ids=None):
    """서비스 그리디 + 한 가지 수정: '정류장 + 같은 셀 증편*'을 한 후보로 함께 평가한다.
    효율 = 묶음 한계 ΔB̂ / 묶음 사업비(1.37억), 건수 2건. 그 밖의 규칙은 서비스와 같다
    (÷총사업비 효율 순, 예산·건수·게이트·balance). 개선안이 격차를 메우는지 재기 위한 변형."""
    n = len(ctx.items)
    pool = set(range(n)) if ids is None else set(int(j) for j in ids)
    bund = [(it["dep"], j) for j, it in enumerate(ctx.items)
            if it["dyn"] and j in pool and it["dep"] in pool]
    S, cur = [], 0.0
    sb = ctx.sub(np.arange(n))
    while True:
        moves = [[j] for j in pool if j not in S]
        moves += [[si, fj] for si, fj in bund if si not in S and fj not in S]
        cand, mv = [], []
        for m in moves:
            if ctx.feasible(S + m, budget):
                cand.append(S + m)
                mv.append(m)
        if not cand:
            break
        X = np.zeros((len(cand), n))
        for r, T in enumerate(cand):
            X[r, T] = 1.0
        g = ctx.f_X(X, sb) - cur
        eff = g / np.array([ctx.cost[m].sum() for m in mv])
        r = int(np.argmax(eff))
        if g[r] <= 1e-9:
            break
        S, cur = cand[r], cur + float(g[r])
    return S, cur


def gain_greedy(ctx, budget):
    """÷비용 없이 한계 ΔB̂ 절대값이 큰 순 — 비교용 변형(서비스에는 없다)."""
    n = len(ctx.items)
    S, cur = [], 0.0
    sb = ctx.sub(np.arange(n))
    while True:
        cand = [S + [j] for j in range(n) if j not in S and ctx.feasible(S + [j], budget)]
        if not cand:
            break
        X = np.zeros((len(cand), n))
        for r, T in enumerate(cand):
            X[r, T] = 1.0
        v = ctx.f_X(X, sb)
        r = int(np.argmax(v))
        if v[r] <= cur + 1e-9:
            break
        S, cur = cand[r], float(v[r])
    return S, cur


# ─── 실행 ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--exact", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--cap", type=int, default=2_500_000)
    ap.add_argument("--periods", nargs="*", default=None)
    ap.add_argument("--strategies", nargs="*", default=None)
    a = ap.parse_args()
    run_all = not (a.pairs or a.exact or a.full)

    t0 = time.time()
    M.DATA.update(M._build_data_snapshot())
    sim = M.DATA["sim"]
    gid_region = {gid: M.DATA["cells"]["am"].get(gid, {}).get("region", "") for gid in sim.GIDS}
    periods = a.periods or list(sim.PERIODS)
    strats = a.strategies or STRATS
    print(f"적재 {time.time() - t0:.1f}s · 단가 {M.COST_KRW} · 예산 {[eok(b) for b in BUDGETS]} · 최대 {MAX_PL}건")

    ctxs = {}
    print("\n[0] 검증 — f(서비스 그리디 배치) 와 Σ tB 의 상대오차, 후보 풀")
    for p in periods:
        for st in strats:
            c = Ctx(sim, gid_region, p, st)
            ctxs[(p, st)] = c
            w = verify(c)
            ne = selftest_enum(c)
            print(f"  {p:5s} {st:10s} 후보 {len(c.pool_cells):3d}격자 · 항목 {len(c.items):3d} "
                  f"(증편* {sum(it['dyn'] for it in c.items)}) · 최대 상대오차 {w:.1e} · "
                  f"열거기 자체검사 {ne}항목 2^{ne} 전수와 일치")

    if run_all or a.pairs:
        print("\n[1] 배치 쌍 상호작용 — I = f({x,y}) − f({x}) − f({y}), 상대값 = I / (f(x)+f(y))")
        print("    서브모듈러(한계효과 체감)라면 모든 쌍에서 I ≤ 0 이어야 한다.")
        for p in periods:
            for st in [s for s in strats if s in ("efficiency", "equity")]:
                c = ctxs[(p, st)]
                r = pairs(c)
                if r is None:
                    continue
                kinds = ", ".join(f"{k} {v}" for k, v in sorted(r["kinds"].items(), key=lambda x: -x[1]))
                print(f"  {p:5s} {st:10s} 쌍 {r['n']:6d} · I<0 {r['neg']:6d} · I>0 {r['pos']:5d} "
                      f"· 최대 상대 +{max(r['max_rel'], 0):.3f}  [{kinds}]")
                if r["top"]:
                    (i, j), I, rel = r["top"]
                    a_, b_ = c.items[i], c.items[j]
                    print(f"        최대 상보: {a_['mode']}@{sim.GIDS[a_['c']]} + {b_['mode']}"
                          f"{'*' if b_['dyn'] else ''}@{sim.GIDS[b_['c']]}  I=+{I:.1f}/일 (+{rel:.1%})")

    rows = []
    if run_all or a.exact:
        print(f"\n[2] 정확해 대조 — 후보 {a.k}격자로 줄인 문제, 실행 가능한 조합 전수 평가 (상한 {a.cap:,}개)")
        print("    비율 = 그리디 ΔB̂ / 최적 ΔB̂.  예산별로 같은 격자 집합에서 비교한다.")
        rng = np.random.default_rng(20261123)
        for p in periods:
            for st in strats:
                c = ctxs[(p, st)]
                n = len(c.items)
                single = c.f_sets([[j] for j in range(n)])
                # real: 전체 그리디가 고른 격자 + 단독 효과(절대·비용당) 상위 격자
                gcells = []
                for b in BUDGETS:
                    S, _, _ = c.service_greedy(b)
                    for j in S:
                        if int(c.items[j]["c"]) not in gcells:
                            gcells.append(int(c.items[j]["c"]))
                ok = [j for j in range(n) if not c.items[j]["dyn"]]
                by_abs = [int(c.items[j]["c"]) for j in sorted(ok, key=lambda j: -single[j])]
                by_eff = [int(c.items[j]["c"]) for j in sorted(ok, key=lambda j: -single[j] / c.cost[j])]
                alt = []
                for x, y in zip(by_abs, by_eff):
                    for z in (x, y):
                        if z not in gcells and z not in alt:
                            alt.append(z)
                insts = [("real", gcells, alt)]
                for sd in range(a.seeds):
                    cells = list(rng.choice(c.pool_cells, size=min(a.k, len(c.pool_cells)), replace=False))
                    insts.append((f"rand{sd}", [], [int(x) for x in cells]))
                for name, fixed, extra in insts:
                    k = a.k
                    while True:
                        cells = (fixed + extra)[:max(k, len(fixed))]
                        ids = cells_items(c, cells)
                        try:
                            t1 = time.time()
                            res, nsets = exact_over(c, ids, BUDGETS, a.cap)
                            break
                        except _Cap:
                            k -= 2
                    for b in BUDGETS:
                        Sg, vg, _ = c.service_greedy(b, cells)
                        Sf, vf, _ = c.service_greedy(b)
                        vo, So, co = res[b]
                        same_full = sorted(Sg) == sorted(Sf)
                        # 최적해는 전체 문제에서도 실행 가능하고, 전체 공간에서 평가해도 같은 값이어야 한다
                        assert c.feasible(So, b)
                        assert abs(float(c.f_sets([So])[0] if So else 0.0) - vo) <= 1e-7 * max(1.0, vo)
                        if So:
                            service_check(c, So, b)
                        ratio = vg / vo if vo > 0 else 1.0
                        _, vbg = bundle_greedy(c, b, ids)
                        rows.append(dict(p=p, st=st, inst=name, b=b, ratio=ratio, vg=vg, vo=vo,
                                         rbg=vbg / vo if vo > 0 else 1.0,
                                         rmx=max(vg, vbg) / vo if vo > 0 else 1.0,
                                         cg=sum(c.items[j]["cost"] for j in Sg), co=co,
                                         compg=comp(c, Sg), compo=comp(c, So), same_full=same_full,
                                         k=len(cells), nsets=nsets))
                    print(f"  {p:5s} {st:10s} {name:6s} K={len(cells):2d} 조합 {nsets:>9,} "
                          f"{time.time() - t1:5.1f}s  "
                          + "  ".join(f"{eok(r['b'])}:{r['ratio']:.4f}" for r in rows[-len(BUDGETS):]))

        print("\n  요약 — 전략 × 예산 (서비스 그리디 비율 최소 / 평균 / 최적 일치 | 묶음 인지 그리디 최소 / 평균 / 최적 일치)")
        for st in strats:
            for b in BUDGETS:
                rs = [r for r in rows if r["st"] == st and r["b"] == b]
                if not rs:
                    continue
                v = np.array([r["ratio"] for r in rs])
                u = np.array([r["rbg"] for r in rs])
                print(f"    {st:10s} {eok(b):>7s}  n={len(v):2d}  최소 {v.min():.4f}  평균 {v.mean():.4f}  "
                      f"일치 {np.mean(v > 0.99995):4.0%} | 최소 {u.min():.4f}  평균 {u.mean():.4f}  "
                      f"일치 {np.mean(u > 0.99995):4.0%}")
        v = np.array([r["ratio"] for r in rows])
        u = np.array([r["rbg"] for r in rows])
        print(f"    {'전체':10s} {'':>7s}  n={len(v):3d} 최소 {v.min():.4f}  평균 {v.mean():.4f}  "
              f"일치 {np.mean(v > 0.99995):4.0%} | 최소 {u.min():.4f}  평균 {u.mean():.4f}  "
              f"일치 {np.mean(u > 0.99995):4.0%}")
        x = np.array([r["rmx"] for r in rows])
        print(f"    둘 중 나은 쪽(서비스·묶음 그리디를 모두 돌려 큰 값)  최소 {x.min():.4f}  평균 {x.mean():.4f}  "
              f"일치 {np.mean(x > 0.99995):4.0%}")
        real = [r for r in rows if r["inst"] == "real"]
        lb = [r for r in real if r["same_full"] and r["ratio"] < 0.99995]
        if real:
            print(f"\n  전체 문제 격차가 증명된 경우(real · 전체그리디 재현 · 최적 미달): {len(lb)}/{len(real)}건")
            for r in lb:
                print(f"    {r['p']:5s} {r['st']:10s} {eok(r['b']):>7s} 그리디는 실행 가능한 더 나은 해의 {r['ratio']:.4f} "
                      f"(+{r['vo'] / r['vg'] - 1:.1%} 개선 가능)")
            print("\n  real 인스턴스 — 제한 그리디가 전체 그리디와 같은 배치인 경우만 전체 문제의 격차 하한")
            for r in sorted(real, key=lambda r: r["ratio"])[:12]:
                print(f"    {r['p']:5s} {r['st']:10s} {eok(r['b']):>7s} 비율 {r['ratio']:.4f} "
                      f"(그리디 {r['vg']:8.1f} · {eok(r['cg'])} · {r['compg']:14s} | 최적 {r['vo']:8.1f} · "
                      f"{eok(r['co'])} · {r['compo']:14s}) 전체그리디재현={'예' if r['same_full'] else '아니오'}")

    if run_all or a.full:
        print("\n[3] 전체 후보 — 그리디 해에서 출발한 국소탐색(1건 추가 · 1:1 교체), 절대 효과 그리디")
        print("    국소탐색 해는 실행 가능한 해 → '최적해 ≥ 이 값'. 비율 = 서비스 그리디 / 찾은 최선.")
        frows = []
        for p in periods:
            for st in strats:
                c = ctxs[(p, st)]
                for b in BUDGETS:
                    Sg, vg, stopped = c.service_greedy(b)
                    _, vl1 = local_search(c, Sg, b, bundles=False)
                    Sl, vl = local_search(c, Sg, b)
                    Sa, va = gain_greedy(c, b)
                    Sal, val_ = local_search(c, Sa, b)
                    best = max(vg, vl1, vl, va, val_)
                    Sbg, vbg = bundle_greedy(c, b)
                    Sbl, vbl = local_search(c, Sbg, b)
                    best = max(best, vbg, vbl)
                    Sb = max([(vg, Sg), (vl, Sl), (va, Sa), (val_, Sal), (vbg, Sbg), (vbl, Sbl)],
                             key=lambda x: x[0])[1]
                    if Sb:
                        service_check(c, Sb, b)      # 찾은 최선이 서비스 규칙·서비스 계산으로도 성립하는가
                    if Sbg:
                        service_check(c, Sbg, b)
                    frows.append(dict(p=p, st=st, b=b, vg=vg, vl1=vl1, vl=vl, va=va, vbg=vbg, best=best,
                                      cg=sum(c.items[j]['cost'] for j in Sg),
                                      cbg=sum(c.items[j]['cost'] for j in Sbg)))
                    print(f"  {p:5s} {st:10s} {eok(b):>7s}  그리디 {vg:9.1f} ({eok(sum(c.items[j]['cost'] for j in Sg))}, "
                          f"{comp(c, Sg)})  국소1:1 {vl1:9.1f}  국소+묶음 {vl:9.1f}  절대그리디 {va:9.1f}  "
                          f"묶음그리디 {vbg:9.1f} ({eok(sum(c.items[j]['cost'] for j in Sbg))}, {comp(c, Sbg)})  "
                          f"최선 {best:9.1f} ({eok(sum(c.items[j]['cost'] for j in Sb))}, {comp(c, Sb)})  "
                          f"그리디/최선 {vg / best:.4f}  묶음그리디/최선 {vbg / best:.4f}")
        print("\n  요약 — 전략 × 예산 (찾은 최선 대비: 서비스 그리디 최소 / 평균 | 묶음 인지 그리디 최소 / 평균)")
        for st in strats:
            for b in BUDGETS:
                rs = [r for r in frows if r["st"] == st and r["b"] == b]
                if rs:
                    v = np.array([r["vg"] / r["best"] for r in rs])
                    u = np.array([r["vbg"] / r["best"] for r in rs])
                    x = np.array([max(r["vg"], r["vbg"]) / r["best"] for r in rs])
                    print(f"    {st:10s} {eok(b):>7s}  최소 {v.min():.4f}  평균 {v.mean():.4f} | "
                          f"최소 {u.min():.4f}  평균 {u.mean():.4f} | 둘 중 나은 쪽 최소 {x.min():.4f}  평균 {x.mean():.4f}")

    print(f"\n총 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
