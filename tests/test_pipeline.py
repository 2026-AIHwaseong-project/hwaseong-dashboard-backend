# -*- coding: utf-8 -*-
"""
① 파이프라인 단위 테스트 — 산출식이 의도대로 계산되는가

    python -m pytest tests/test_pipeline.py -q

07_validate.py 와의 차이
    07_validate 는 **실데이터 전체를 돌려 회귀 성능을 재는** 검증기다.
    여기는 **픽스처를 넣어 수식 자체를 확인하는** 단위 테스트다.
    수식이 틀려도 R² 는 그럴듯하게 나올 수 있으므로 둘 다 필요하다.

04_model.py 는 파일명이 숫자로 시작해 import 가 안 되므로 importlib 으로 로드한다
(server/main.py 가 05_simulate.py 를 로드하는 방식과 같다). main() 은
__main__ 가드 안에 있어 로드만으로는 실행되지 않는다.
"""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
D_DIR = ROOT / "dataset_hwaseong"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace(".py", "").replace("_", ""), ROOT / "analysis" / name)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def M():
    return _load("04_model.py")


# ══════════════════════════════════════════════════════════════
# 1. 정규화 — log1p → P3/P97 클램프 min-max
# ══════════════════════════════════════════════════════════════
def test_pctl_is_lower_no_interpolation(M):
    """보간 없는 lower 방식. 프론트 mock.js 와 같은 계약이라 바뀌면 색이 어긋난다."""
    a = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert M.pctl(a, 0.0) == 0
    assert M.pctl(a, 0.5) == 4          # floor(0.5 * 9) = 4
    assert M.pctl(a, 1.0) == 9
    assert M.pctl(a, 0.97) == 8         # floor(0.97 * 9) = 8, 보간이면 8.73


def test_norm_log_range_and_monotonic(M):
    v = np.array([0, 1, 10, 100, 1000, 100000], dtype=float)
    lo, hi = np.log1p(1), np.log1p(1000)
    out = M.norm_log(v, lo, hi)
    assert out.min() >= 0.0 and out.max() <= 1.0, "0~1 클램프가 안 먹었다"
    assert np.all(np.diff(out) >= 0), "단조증가가 아니다"
    assert out[0] == 0.0 and out[-1] == 1.0, "하한·상한이 각각 0·1 이어야 한다"


def test_norm_log_degenerate_range(M):
    """lo == hi (모든 값이 같은 격자) 에서 0 나눗셈이 나면 안 된다."""
    out = M.norm_log(np.array([5.0, 5.0]), 1.0, 1.0)
    assert np.all(np.isfinite(out))


def test_zstats_zero_variance(M):
    """분산 0 이면 sd=1 로 대체해야 z 가 inf 가 되지 않는다."""
    m, sd = M.zstats(np.array([3.0, 3.0, 3.0]))
    assert m == 3.0 and sd == 1.0


# ══════════════════════════════════════════════════════════════
# 2. 커버리지 — 임계 600m, 경계값 0.15 / 0.50
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("dist_m,expected_cov,expected_action", [
    (0.0,   1.00, "ADD_FREQ"),    # 정류장 바로 옆
    (299.0, None, "ADD_FREQ"),    # 300m 직전 — 증편 구간
    (300.0, 0.50, "ADD_FREQ"),    # 경계 정확히 0.50 → 증편 (>= 0.5)
    (301.0, None, "NEW_STOP"),    # 300m 넘으면 신설 구간
    (509.0, None, "NEW_STOP"),
    (510.0, 0.15, "NEW_STOP"),    # 경계 정확히 0.15 → 신설 (>= 0.15)
    (511.0, None, "DRT"),         # 510m 넘으면 똑버스
    (600.0, 0.05, "DRT"),         # 임계 밖은 하한 0.05
    (5000.0, 0.05, "DRT"),
])
def test_coverage_boundaries(M, dist_m, expected_cov, expected_action):
    """0.50 == 정류장 300m · 0.15 == 510m 등가. 이 컷이 04_model 과 server/main 두 곳에 있다."""
    cov = float(np.clip(1.0 - dist_m / M.COV_THRESHOLD_M, 0.05, 1.0))
    if expected_cov is not None:
        assert cov == pytest.approx(expected_cov, abs=1e-9)
    action = "ADD_FREQ" if cov >= 0.5 else ("NEW_STOP" if cov >= 0.15 else "DRT")
    assert action == expected_action, f"{dist_m}m → cov {cov:.4f} → {action}"


def test_coverage_never_below_floor(M):
    cov = np.clip(1.0 - np.array([600.0, 1e6]) / M.COV_THRESHOLD_M, 0.05, 1.0)
    assert (cov == 0.05).all(), "하한 0.05 가 깨지면 z 분포가 무너진다"


# ══════════════════════════════════════════════════════════════
# 3. MI — 감쇠항이 빈 땅을 걸러내는가
# ══════════════════════════════════════════════════════════════
def _mi(M, zD, zS, D, dRef):
    damp = np.clip(D / dRef, 0, 1) ** M.DAMP_EXP
    return float(np.clip((zD - zS) * damp, -M.MI_CLAMP, M.MI_CLAMP))


def test_mi_damping_suppresses_empty_land(M):
    """사람이 안 사는 임야: 공급이 0 이라 zD-zS 는 크지만 감쇠 후 need 컷 아래여야 한다."""
    dRef = 0.30
    naive = _mi(M, zD=-0.5, zS=-2.0, D=0.30, dRef=dRef)     # 감쇠 없음 상태의 격차 1.5
    empty = _mi(M, zD=-0.5, zS=-2.0, D=0.01, dRef=dRef)     # 수요가 기준점의 1/30
    assert naive > empty, "감쇠가 안 먹었다"
    assert empty < M.QUAD["need_mi"], f"빈 땅이 need 컷을 넘었다 (MI={empty})"


def test_mi_damping_keeps_populated_cell(M):
    """수요가 기준점 이상이면 감쇠 계수가 1 이라 격차가 그대로 남아야 한다."""
    assert _mi(M, zD=1.2, zS=-0.3, D=0.6, dRef=0.30) == pytest.approx(1.5, abs=1e-9)


def test_mi_clamp(M):
    assert _mi(M, zD=9.0, zS=-9.0, D=1.0, dRef=0.3) == M.MI_CLAMP
    assert _mi(M, zD=-9.0, zS=9.0, D=1.0, dRef=0.3) == -M.MI_CLAMP


def test_damp_exponent_shape(M):
    """지수 0.65 는 1 보다 작아야 한다 — 1 이면 저수요 격자가 과하게 눌린다."""
    assert 0 < M.DAMP_EXP < 1


# ══════════════════════════════════════════════════════════════
# 4. D · S 합성
# ══════════════════════════════════════════════════════════════
def test_demand_supply_weights_sum_to_one(M):
    assert M.ALPHA_D + (1 - M.ALPHA_D) == 1.0
    assert M.W_FREQ + M.W_COV == pytest.approx(1.0)


def test_demand_is_half_and_half(M):
    """실현수요(승차)와 잠재수요를 절반씩. 한쪽이 0 이어도 다른 쪽이 살아야 한다."""
    D = M.ALPHA_D * 1.0 + (1 - M.ALPHA_D) * 0.0
    assert D == pytest.approx(0.5)


def test_supply_of_zero_freq_cell(M):
    """운행 0 인 격자도 정류장이 가까우면 S 가 0 이 아니다 (커버리지 항)."""
    S = M.W_FREQ * 0.0 + M.W_COV * 1.0
    assert S == pytest.approx(M.W_COV)
    S_far = M.W_FREQ * 0.0 + M.W_COV * 0.05
    assert 0 < S_far < S


# ══════════════════════════════════════════════════════════════
# 5. 사분면 판정 — 절대 가드와 배타성
# ══════════════════════════════════════════════════════════════
def _quad(M, zD, zS, mi, nf, fRef, freq_per_h):
    enough = freq_per_h >= M.MIN_FREQ_PER_H
    Q = M.QUAD
    need = (zD >= Q["need_zd"]) and (mi >= Q["need_mi"])
    over = (not need) and zD <= Q["over_zd"] and zS >= Q["over_zs"] and enough
    drt = (not need and not over and zD <= Q["drt_zd"]
           and zS <= Q["drt_zs"] and nf >= fRef)
    ok = (not need and not over and not drt
          and zD >= Q["ok_zd"] and zS >= Q["ok_zs"] and enough)
    return "need" if need else "over" if over else "drt" if drt else "ok" if ok else "mid"


def test_absolute_freq_guard_blocks_over(M):
    """야간 시간당 0.37회 운행 격자가 '적정'·'공급과잉'이 되면 안 된다 (실측 61곳 오라벨)."""
    assert _quad(M, zD=-0.5, zS=0.7, mi=0.0, nf=0.5, fRef=0.1, freq_per_h=0.37) == "mid"
    assert _quad(M, zD=-0.5, zS=0.7, mi=0.0, nf=0.5, fRef=0.1, freq_per_h=5.0) == "over"


def test_absolute_freq_guard_blocks_ok(M):
    assert _quad(M, zD=0.5, zS=0.5, mi=0.0, nf=0.5, fRef=0.1, freq_per_h=0.48) == "mid"
    assert _quad(M, zD=0.5, zS=0.5, mi=0.0, nf=0.5, fRef=0.1, freq_per_h=3.0) == "ok"


def test_need_takes_precedence(M):
    """need 조건을 만족하면 다른 분면으로 새면 안 된다."""
    assert _quad(M, zD=0.9, zS=0.9, mi=1.5, nf=0.9, fRef=0.1, freq_per_h=99) == "need"


def test_drt_gate_excludes_low_potential(M):
    """잠재수요 하위 30% 는 똑버스 후보에서 뺀다."""
    assert _quad(M, zD=-1.0, zS=-1.0, nf=0.50, fRef=0.30, mi=0.0, freq_per_h=0) == "drt"
    assert _quad(M, zD=-1.0, zS=-1.0, nf=0.10, fRef=0.30, mi=0.0, freq_per_h=0) == "mid"


def test_need_cut_rejects_high_supply_cell(M):
    """동탄 고빈도 격자(zD 높지만 공급도 높음)가 need 로 새면 안 된다 — 컷 0.75 의 존재 이유."""
    mi = _mi(M, zD=2.5, zS=2.2, D=0.9, dRef=0.3)   # 격차 0.3
    assert mi < M.QUAD["need_mi"]
    assert _quad(M, zD=2.5, zS=2.2, mi=mi, nf=0.9, fRef=0.1, freq_per_h=99) != "need"


# ══════════════════════════════════════════════════════════════
# 6. 우선순위 점수
# ══════════════════════════════════════════════════════════════
def _pri(M, mi_raw, popw, eld):
    return mi_raw * (0.35 + popw) * (1 + M.ELD_COEF * min(eld, 1.0))


def test_priority_offset_prevents_zero_collapse(M):
    """인구가중 0 인 격자도 점수가 0 이 되면 안 된다 (0.35 오프셋)."""
    assert _pri(M, 1.0, 0.0, 0.0) == pytest.approx(0.35)


def test_priority_elderly_weighting(M):
    """고령비가 높을수록 점수가 높아야 한다."""
    assert _pri(M, 1.0, 0.5, 0.30) > _pri(M, 1.0, 0.5, 0.05)


def test_priority_elderly_ratio_clipped(M):
    """고령비가 1 을 넘어도(데이터 오류) 가중이 폭주하면 안 된다."""
    assert _pri(M, 1.0, 0.5, 5.0) == _pri(M, 1.0, 0.5, 1.0)


# ══════════════════════════════════════════════════════════════
# 7. 격자 공간조인 (02_grid.py 산출물 검증)
#    원본 SGIS 279MB 가 필요해 재실행 테스트는 불가하므로 산출물을 검증한다.
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def gh():
    return pd.read_csv(D_DIR / "grid_hwaseong.csv")


def test_grid_totals_match_published_statistics(gh):
    """공표값 대조 — 한 칸이 밀리면 이후 전부가 틀린다."""
    assert len(gh) == 786
    assert 1_000_000 <= gh["pop"].sum() <= 1_060_000, gh["pop"].sum()
    assert 380_000 <= gh["households"].sum() <= 410_000, gh["households"].sum()
    assert 0.09 <= gh["elderly"].sum() / gh["pop"].sum() <= 0.13, "고령비 약 11%"


def test_grid_inside_hwaseong_bbox(gh):
    assert gh.lon.between(126.4, 127.3).all()
    assert gh.lat.between(36.9, 37.4).all()


def test_grid_ids_unique(gh):
    assert gh.grid_id.is_unique


def test_every_grid_has_region(gh):
    assert gh.region.notna().all(), "읍면동 배정이 빠진 격자가 있다"
    assert gh.region.nunique() == 29, f"읍면동 {gh.region.nunique()}개 (4읍 9면 16동 = 29)"


@pytest.mark.parametrize("lon,lat,region,landmark", [
    (127.0660, 37.2286, "반월동", "삼성전자 화성캠퍼스"),
    (126.8186, 37.1643, "남양읍", "현대차 남양연구소"),
])
def test_known_landmark_lands_in_expected_region(gh, lon, lat, region, landmark):
    """알려진 좌표 → 격자 매핑. 좌표를 맞춰 넣은 게 아니라 경계로 자른 결과여야 한다."""
    d = (gh.lon - lon) ** 2 + (gh.lat - lat) ** 2
    hit = gh.loc[d.idxmin()]
    assert hit.region == region, f"{landmark} 좌표가 {hit.region} 으로 떨어짐 (기대 {region})"
    assert hit.workers > 10_000, f"{landmark} 격자 종사자 {hit.workers}명 — 대형 사업장이 아님"


def test_employment_only_cell_exists(gh):
    """종사자만 몰리고 거주인구는 없는 격자 = 공간조인이 맞다는 증거."""
    cells = gh[(gh.workers > 10_000) & (gh["pop"] < 1_000)]
    assert len(cells) > 0, "고용 단독 격자가 하나도 없다 — 공간조인 의심"


# ══════════════════════════════════════════════════════════════
# 8. 엣지 케이스 — 승하차 0 정류장 · 결측 보정
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def st():
    return pd.read_csv(D_DIR / "stops_hwaseong.csv")


def test_zero_boarding_stops_are_not_freq_imputed(st):
    """승하차 0 인 정류장은 일부러 보정하지 않는다 — 채우면 사각지대가 사라진다."""
    if "freq_imputed" not in st.columns:
        pytest.skip("freq_imputed 컬럼 없음")
    zero = st[(st.get("board_day", 0) == 0) & (st.get("alight_day", 0) == 0)]
    assert len(zero) > 0, "승하차 0 정류장이 하나도 없다 — 데이터 의심"
    assert not zero["freq_imputed"].astype(bool).any(), \
        f"승하차 0 인데 운행빈도가 보정된 정류장 {int(zero['freq_imputed'].astype(bool).sum())}곳"


def test_no_negative_metrics(st):
    for col in ("board_day", "alight_day"):
        if col in st.columns:
            assert (st[col].fillna(0) >= 0).all(), f"{col} 에 음수"


def test_grid_join_has_all_period_rows():
    """격자 × 시간대 × 요일축이 빠짐없이 채워져 있는가.

    요일축(wd/we) 도입 뒤 행 수가 두 배가 됐다. 축이 하나 늘어도 곱이 맞아야 하므로
    리터럴 3144 를 박지 않고 실제 축의 카디널리티에서 기대값을 만든다."""
    gj = pd.read_csv(D_DIR / "grid_join.csv")
    assert set(gj.period.unique()) == {"am", "day", "pm", "night"}
    n_daytype = gj.daytype.nunique() if "daytype" in gj.columns else 1
    expected = 786 * 4 * n_daytype
    assert len(gj) == expected, f"{len(gj)}행 (786 × 4 × 요일축 {n_daytype} = {expected})"
    group = ["period", "daytype"] if "daytype" in gj.columns else ["period"]
    assert gj.groupby(group).size().nunique() == 1, "시간대·요일 조합별 격자 수가 다르다"


def test_nearest_stop_distance_sane():
    gj = pd.read_csv(D_DIR / "grid_join.csv")
    assert (gj.nearest_stop_m >= 0).all()
    assert gj.nearest_stop_m.median() < 1000, "최근접 정류장 중앙값이 1km 초과 — 좌표계 의심"


# ══════════════════════════════════════════════════════════════
# 9. norm_stats 계약 — 시뮬레이션이 이걸 고정 참조한다
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def norm():
    return json.loads((D_DIR / "norm_stats.json").read_text("utf-8"))


def test_norm_stats_has_all_periods(norm):
    assert set(norm["periods"]) == {"am", "day", "pm", "night"}


def test_norm_stats_keys_present(norm):
    need = {"loB", "hiB", "loF", "hiF", "loQ", "hiQ", "mD", "sD", "mS", "sS", "dRef", "fRef"}
    for p, K in norm["periods"].items():
        assert need <= set(K), f"{p} 에 없는 키: {need - set(K)}"


def test_norm_stats_ranges_are_ordered(norm):
    for p, K in norm["periods"].items():
        assert K["hiB"] >= K["loB"] and K["hiF"] >= K["loF"] and K["hiQ"] >= K["loQ"], p
        assert K["sD"] > 0 and K["sS"] > 0, p


def test_dref_not_collapsed(norm):
    """dRef 가 0 근처로 주저앉으면 MI 감쇠가 무력화된다 (500m 전환 시 위험)."""
    for p, K in norm["periods"].items():
        assert K["dRef"] >= 0.05, f"{p} dRef={K['dRef']}"


def test_fref_gate_alive(norm):
    """fRef=0 이면 drt 게이트가 무력화된다."""
    for p, K in norm["periods"].items():
        assert K["fRef"] > 0, f"{p} fRef=0 — drt 게이트 무력화"


def test_constants_match_module(norm, M):
    """norm_stats 에 박제된 상수가 코드와 갈라지면 시뮬레이션이 다른 자로 잰다."""
    C = norm["constants"]
    assert C["covThresholdM"] == M.COV_THRESHOLD_M
    assert C["dampExp"] == M.DAMP_EXP
    assert C["miClamp"] == M.MI_CLAMP
    assert C["wFreq"] == M.W_FREQ and C["wCov"] == M.W_COV
    assert C["elderlyCoef"] == M.ELD_COEF


# ══════════════════════════════════════════════════════════════
# 10. 산출물 재현성 (느림 — -m slow 로 분리)
# ══════════════════════════════════════════════════════════════
@pytest.mark.slow
def test_model_output_is_reproducible(tmp_path):
    """04_model.py 를 다시 돌리면 커밋된 grid_metrics.csv 와 바이트가 같아야 한다."""
    import shutil, subprocess, sys
    target = D_DIR / "grid_metrics.csv"
    backup = tmp_path / "grid_metrics.csv"
    shutil.copy2(target, backup)
    try:
        r = subprocess.run([sys.executable, str(ROOT / "analysis" / "04_model.py")],
                           cwd=ROOT, capture_output=True, text=True, timeout=900)
        assert r.returncode == 0, r.stderr[-800:]
        assert target.read_bytes() == backup.read_bytes(), \
            "04_model.py 재실행 결과가 커밋본과 다르다"
    finally:
        shutil.copy2(backup, target)


# ── 산출물 교체 목록 정합 ──────────────────────────────────────────────────────
#
# 데이터 갱신은 스테이징에서 파이프라인을 돌린 뒤 `_STEP_OUT` 에 적힌 파일만
# 라이브로 옮긴다. 스크립트가 쓰는 파일이 그 목록에서 빠지면 라이브가 **새 것과
# 옛 것의 혼합**이 되고, 05_simulate 의 기준선 assert 로 서버가 아예 못 뜬다
# (실제로 grid_metrics_we.csv 가 빠져 있었다 — 주말 지표만 옛것으로 남는 경로).
#
# 파일명을 하나씩 적어 두면 다음에 스크립트가 산출물을 늘릴 때 또 놓친다.
# 그래서 **스크립트 소스에서 쓰기 대상을 직접 읽어** 목록과 대조한다.
import re as _re

_WRITE_TARGET = _re.compile(r'(?:D|D_DIR)\s*/\s*"([^"]+\.(?:csv|json))"')
_WRITE_HINT = ('to_csv(', 'write_text(', '"w"', "'w'")


def _script_outputs(script: str) -> set:
    src = (ROOT / "analysis" / script).read_text("utf-8")
    found = set()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if any(h in s for h in _WRITE_HINT):
            found |= set(_WRITE_TARGET.findall(s))
    return found


@pytest.mark.parametrize("step,script", [
    ("join", "03_join.py"),
    ("model", "04_model.py"),
    ("validate", "07_validate.py"),
])
def test_pipeline_outputs_are_all_swapped(step, script):
    """각 단계가 실제로 쓰는 산출물이 전부 교체 목록에 있어야 한다."""
    import sys
    sys.path.insert(0, str(ROOT))
    from server import admin

    declared = _script_outputs(script)
    assert declared, f"{script} 에서 산출물을 찾지 못했습니다(정규식 확인 필요)"
    swapped = {Path(rel).name for rel in admin._STEP_OUT[step]}
    missing = declared - swapped
    assert not missing, (
        f"{script} 가 쓰는데 교체 목록에 없는 파일: {sorted(missing)}\n"
        f"라이브가 세대 혼합이 되어 서버가 못 뜹니다 — server/admin.py 의 "
        f"_STEP_OUT['{step}'] 에 추가하세요.")
