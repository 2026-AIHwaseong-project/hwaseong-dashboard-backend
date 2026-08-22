# -*- coding: utf-8 -*-
"""
화성시 버스 대시보드 백엔드 — API 계약·불변식·입력검증 테스트

    pip install pytest httpx
    python -m pytest tests/ -q

구성
    A. 계약      응답 스키마와 참조 무결성 (프론트가 의존하는 것)
    B. 불변식    수식·판정 규칙이 산출물과 일치하는가
    C. 교차일관성 같은 사실이 여러 엔드포인트에서 같은 값인가
    D. 입력검증  경계·악의적 입력을 막는가       ← 현재 실패하는 것들
    E. 의미일관성 같은 이름의 필드가 같은 뜻인가  ← 현재 실패하는 것들

D·E 는 지금 실패합니다. 실패가 곧 이 저장소의 미해결 결함 목록입니다.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.main import app  # noqa: E402

PERIODS = ["am", "day", "pm", "night"]
BUDGET = 3_000_000_000


def _fake_acall(text, seen=None):
    """AI 호출을 가로채는 대역.

    보고서·챗봇이 예전에는 _call_ai / _call_ai_chat 두 함수였는데 지금은 스트리밍
    비동기 _acall_ai 하나로 합쳐졌다. (text, provider, model) 튜플을 돌려준다.
    seen 을 주면 실제로 넘어간 프롬프트 길이를 기록한다."""
    async def fake(provider, model, system, messages, **kw):
        if seen is not None:
            seen["n"] = len(system or "") + sum(len(m.get("content", "")) for m in messages)
        return text, provider, model
    return fake


@pytest.fixture(scope="session")
def c():
    with TestClient(app) as client:
        yield client


# ══════════════════════════════════════════════════════════════
# A. 계약 — 프론트 렌더링이 의존하는 필드
# ══════════════════════════════════════════════════════════════
CELL_FIELDS = {
    "id", "name", "region", "regionCode", "regionKind", "lon", "lat",
    "demand", "supply", "zDemand", "zSupply", "mi", "flow", "flowTripsPerDay",
    "elderlyRatio", "coverage", "quadrant", "quadrantLabel", "action",
    "actionLabel", "priorityScore", "nearestStopId", "bins",
}


@pytest.mark.parametrize("p", PERIODS)
def test_grid_cell_schema(c, p):
    """bins 가 빠지면 지도가 오류 없이 균일한 색으로 칠해진다."""
    for cell in c.get(f"/api/v1/grid?period={p}").json()["cells"]:
        missing = CELL_FIELDS - set(cell)
        assert not missing, f"{p}/{cell.get('id')} 필드 누락: {missing}"
        assert set(cell["bins"]) == {"mi", "demand", "supply", "flow"}


@pytest.mark.parametrize("p", PERIODS)
def test_nearest_stop_is_resolvable(c, p):
    """격자 클릭 → 정류장 프로파일 경로. 끊기면 조용히 실패한다."""
    stop_ids = {s["id"] for s in c.get("/api/v1/stops").json()["stops"]}
    for cell in c.get(f"/api/v1/grid?period={p}").json()["cells"]:
        assert cell["nearestStopId"] in stop_ids, \
            f"{cell['id']}.nearestStopId={cell['nearestStopId']} 가 /stops 에 없음"


def test_route_stop_refs_resolvable(c):
    stop_ids = {s["id"] for s in c.get("/api/v1/stops").json()["stops"]}
    for r in c.get("/api/v1/routes").json()["routes"]:
        for sid in (r.get("stopIds") or []):
            assert sid in stop_ids, f"노선 {r['id']} 가 없는 정류장 {sid} 참조"


def test_bad_period_rejected(c):
    assert c.get("/api/v1/grid?period=xxx").status_code == 400
    assert c.post("/api/v1/simulations", json={"period": "xxx"}).status_code == 400


def test_unknown_stop_profile_404(c):
    assert c.get("/api/v1/stops/없는정류장/profile").status_code == 404


# ══════════════════════════════════════════════════════════════
# B. 불변식 — 수식과 판정 규칙
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("p", PERIODS)
def test_kpi_recomputable_from_cells(c, p):
    """KPI 는 저장값이 아니라 셀에서 세야 한다 (관리자 수정 대비)."""
    d = c.get(f"/api/v1/grid?period={p}").json()
    cells, kpi = d["cells"], d["kpi"]
    need = [x for x in cells if x["quadrant"] == "need"]
    assert kpi["needCells"] == len(need)
    assert kpi["drtCells"] == sum(1 for x in cells if x["quadrant"] == "drt")
    assert kpi["totalCells"] == len(cells)
    assert kpi["potentialTripsPerDay"] == sum(x["flowTripsPerDay"] for x in need)
    assert kpi["needShare"] == round(100 * len(need) / len(cells), 1)


@pytest.mark.parametrize("p", PERIODS)
def test_action_is_coverage_exclusive(c, p):
    """수단은 커버리지 단독 배타 결정 (0.15 / 0.50). 지도 배지 == 추천 게이트."""
    for x in c.get(f"/api/v1/grid?period={p}").json()["cells"]:
        cov = x["coverage"]
        exp = "ADD_FREQ" if cov >= 0.5 else ("NEW_STOP" if cov >= 0.15 else "DRT")
        assert x["action"] == exp, f"{x['id']} cov={cov} action={x['action']} 기대={exp}"


@pytest.mark.parametrize("p", PERIODS)
def test_priority_only_on_need(c, p):
    for x in c.get(f"/api/v1/grid?period={p}").json()["cells"]:
        if x["quadrant"] != "need":
            assert x["priorityScore"] == 0


@pytest.mark.parametrize("p", PERIODS)
def test_mi_clamped(c, p):
    for x in c.get(f"/api/v1/grid?period={p}").json()["cells"]:
        assert -2.6 <= x["mi"] <= 2.6


@pytest.mark.parametrize("p", PERIODS)
def test_need_share_band(c, p):
    """2~25% 밴드를 벗어나면 모델이 무너진 것 (04_model.py 의 assert 와 동일)."""
    kpi = c.get(f"/api/v1/grid?period={p}").json()["kpi"]
    assert 2.0 <= kpi["needShare"] <= 25.0


def test_time_axis_alive(c):
    """모든 시간대 need 집합이 같으면 시간축이 죽은 것."""
    sets = {p: frozenset(x["id"] for x in c.get(f"/api/v1/grid?period={p}").json()["cells"]
                         if x["quadrant"] == "need") for p in PERIODS}
    assert len(set(sets.values())) > 1


# ══════════════════════════════════════════════════════════════
# C. 교차 일관성
# ══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("p", PERIODS)
def test_priorities_sorted(c, p):
    items = c.get(f"/api/v1/priorities?period={p}&limit=10").json()["items"]
    scores = [i["priorityScore"] for i in items]
    assert scores == sorted(scores, reverse=True)
    assert [i["rank"] for i in items] == list(range(1, len(items) + 1))


@pytest.mark.parametrize("p", PERIODS)
def test_empty_simulation_equals_grid(c, p):
    """배치 0건 시뮬레이션 KPI == /grid KPI."""
    grid = c.get(f"/api/v1/grid?period={p}").json()["kpi"]
    sim = c.post("/api/v1/simulations", json={"period": p, "placements": []}).json()
    blk = next(x for x in sim["periods"] if x["period"] == p)
    for k in ("needCells", "drtCells", "totalCells",
              "potentialTripsPerDay", "elderlyTripsPerDay"):
        assert blk["kpi"][k] == grid[k], f"{p}.{k}: sim={blk['kpi'][k]} grid={grid[k]}"


def test_recommendation_deterministic(c):
    body = {"strategy": "efficiency", "period": "night", "budgetKrw": BUDGET}
    a = c.post("/api/v1/recommendations", json=body).json()
    b = c.post("/api/v1/recommendations", json=body).json()
    assert [x["cellId"] for x in a["placements"]] == [x["cellId"] for x in b["placements"]]


def test_recommendation_within_budget(c):
    r = c.post("/api/v1/recommendations",
               json={"period": "am", "budgetKrw": 200_000_000, "maxPlacements": 50}).json()
    assert r["usedKrw"] <= 200_000_000 and r["remainingKrw"] >= 0


def test_recommendation_period_actually_matters(c):
    """'시간대를 바꾸면 우선순위가 뒤집힌다'가 추천에도 반영되는가."""
    def ids(p):
        return [x["cellId"] for x in c.post(
            "/api/v1/recommendations",
            json={"strategy": "balance", "period": p, "budgetKrw": BUDGET}).json()["placements"]]
    assert ids("am") != ids("night")


def test_recommendation_allowed_types(c):
    r = c.post("/api/v1/recommendations",
               json={"period": "night", "allowedTypes": ["drt"], "budgetKrw": BUDGET}).json()
    assert all(x["type"] == "drt" for x in r["placements"])


def test_concurrent_requests_consistent(c):
    """동시 호출에서 전역 상태가 오염되지 않는가."""
    import concurrent.futures as cf
    body = {"strategy": "efficiency", "period": "night", "budgetKrw": BUDGET}
    with cf.ThreadPoolExecutor(8) as ex:
        outs = list(ex.map(
            lambda _: tuple(x["cellId"] for x in
                            c.post("/api/v1/recommendations", json=body).json()["placements"]),
            range(8)))
    assert len(set(outs)) == 1


# ══════════════════════════════════════════════════════════════
# D. 입력 검증  ← 현재 실패 (미해결 결함)
# ══════════════════════════════════════════════════════════════
def test_priorities_negative_limit_rejected(c):
    """limit=-1 이 Python 슬라이스로 새어 cells[:-1] 이 되면 안 된다."""
    r = c.get("/api/v1/priorities?period=am&limit=-1")
    assert r.status_code == 400, \
        f"limit=-1 이 200 으로 통과하고 {len(r.json()['items'])}건을 반환함"


def test_simulation_placement_list_capped(c):
    """배열 길이 상한이 없으면 비용·연산이 무제한으로 커진다."""
    body = {"period": "am",
            "placements": [{"type": "stop", "cellId": "다사6707", "count": 1}] * 2000}
    assert c.post("/api/v1/simulations", json=body).status_code == 400


def test_simulation_rejects_negative_budget(c):
    r = c.post("/api/v1/simulations",
               json={"period": "am", "budgetKrw": -1,
                     "placements": [{"type": "drt", "cellId": "다사6707"}]})
    assert r.status_code == 400


def test_simulation_flags_over_budget(c):
    """예산 1억에 36억짜리 배치를 넣으면 초과를 알려야 한다."""
    j = c.post("/api/v1/simulations",
               json={"period": "am", "budgetKrw": 100_000_000,
                     "placements": [{"type": "drt", "cellId": "다사6707", "count": 20}]}).json()
    assert j["cost"]["totalKrw"] <= j["budgetKrw"] or j.get("overBudget") is True, \
        f"예산 {j['budgetKrw']:,} < 비용 {j['cost']['totalKrw']:,} 인데 초과 표시가 없음"


def test_report_model_allowlist(c, monkeypatch):
    """임의 모델 문자열이 그대로 SDK 로 넘어가면 안 된다 (비용·오류 경로)."""
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(sm, "_acall_ai", _fake_acall(json.dumps({"sections": [], "tables": []})))
    r = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "model": "존재하지-않는-모델"})
    assert r.status_code == 400 or r.json().get("model") != "존재하지-않는-모델", \
        "허용목록 밖 모델이 그대로 수락됨"


def test_report_context_size_capped(c, monkeypatch):
    """context 가 프롬프트에 그대로 실려 토큰 비용이 증폭되면 안 된다."""
    import server.main as sm
    seen = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(sm, "_acall_ai",
                        _fake_acall(json.dumps({"sections": []}), seen))
    c.post("/api/v1/reports/draft",
           json={"period": "am", "provider": "claude",
                 "context": {"simulation": {"pad": "가" * 500_000}}})
    assert seen.get("n", 0) < 100_000, f"프롬프트가 {seen.get('n', 0):,}자까지 커짐"


def test_chat_last_message_must_be_user(c):
    r = c.post("/api/v1/chat", json={"messages": [{"role": "assistant", "content": "hi"}]})
    assert r.status_code == 400


# ══════════════════════════════════════════════════════════════
# E. 의미 일관성  ← 현재 실패 (미해결 결함)
# ══════════════════════════════════════════════════════════════
def test_resolved_trips_one_meaning(c):
    """같은 응답 안에서 '해소 통행량'이 한 가지 뜻이어야 한다."""
    r = c.post("/api/v1/recommendations",
               json={"strategy": "efficiency", "period": "am", "budgetKrw": BUDGET}).json()
    items_sum = sum(x["expectedResolvedTrips"] for x in r["placements"])
    summary = r["summary"]["expectedResolvedTrips"]
    sim = r["simulation"]["effectiveness"]["resolvedTripsPerDay"]
    assert items_sum == pytest.approx(summary, rel=0.05) == pytest.approx(sim, rel=0.05), \
        f"items 합={items_sum} · summary={summary} · simulation={sim} 셋이 서로 다름"


def test_freq_placement_is_associative(c):
    """'증편 2회'를 어떻게 표현하든 같은 결과여야 한다."""
    def eff(pl):
        return c.post("/api/v1/simulations",
                      json={"period": "am", "placements": pl}
                      ).json()["effectiveness"]["resolvedTripsPerDay"]
    one = eff([{"type": "freq", "cellId": "다사5914", "count": 2}])
    two = eff([{"type": "freq", "cellId": "다사5914", "count": 1},
               {"type": "freq", "cellId": "다사5914", "count": 1}])
    assert one == pytest.approx(two, rel=0.01), \
        f"count=2 → {one} · 1건+1건 → {two} (표현 방식만 다른데 결과가 다름)"


def test_documented_stop_count_matches_api(c):
    """README·API_SPEC 이 말하는 정류장 수와 실제 배포 수가 같아야 한다."""
    import pandas as pd
    csv_rows = len(pd.read_csv(ROOT / "dataset_hwaseong" / "stops_hwaseong.csv"))
    api_rows = len(c.get("/api/v1/stops").json()["stops"])
    assert csv_rows == api_rows, \
        f"문서 기준 {csv_rows}개 · API 배포 {api_rows}개 (stop_id 결측 {csv_rows - api_rows}개 탈락)"


# ══════════════════════════════════════════════════════════════
# F. 나머지 엔드포인트 (10개 전부 커버)
# ══════════════════════════════════════════════════════════════
def test_meta_contract(c):
    m = c.get("/api/v1/meta").json()
    for k in ("periods", "grid", "cost", "assumptions", "formula", "dataQuality"):
        assert k in m, f"meta.{k} 누락"
    assert len(m["periods"]) == 4
    assert m["grid"]["cellCount"] == m["grid"]["analysisCellCount"]
    assert m["isMockData"] is False, "시연 직전 목데이터로 되돌아가 있으면 안 된다"
    for mode in ("stop", "drt", "freq"):
        assert m["cost"][mode]["krw"] > 0


def test_stops_contract(c):
    stops = c.get("/api/v1/stops").json()["stops"]
    assert len(stops) > 0
    for s in stops[:50]:
        assert isinstance(s["lon"], float) and isinstance(s["lat"], float)
        assert isinstance(s["boardingsPerDay"], (int, float)), \
            "빠지면 전 정류장 툴팁이 '일 승차 0명' 이 된다"


def test_stop_profile_contract(c):
    sid = c.get("/api/v1/stops").json()["stops"][0]["id"]
    p = c.get(f"/api/v1/stops/{sid}/profile").json()
    assert p, "프로파일이 비어 있다"


def test_providers_contract(c):
    d = c.get("/api/v1/providers").json()
    ids = {p["id"] for p in d["providers"]}
    assert ids == {"claude", "openai", "gemini"}
    for p in d["providers"]:
        assert isinstance(p["available"], bool) and p["defaultModel"]
        assert p["defaultModel"] in {m["id"] for m in p["models"]}, \
            f"{p['id']} 기본 모델이 목록에 없다"


def test_report_falls_back_without_key(c, monkeypatch):
    """키가 없어도 「AI 보고서 생성」 버튼이 깨지면 안 된다."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "AI_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    j = c.post("/api/v1/reports/draft", json={"period": "am"}).json()
    assert j["isAiGenerated"] is False
    assert len(j["sections"]) > 0 and j["tables"][0]["rows"]
    for t in j["tables"]:
        assert len(t["title"]) <= 31, "엑셀 시트명 31자 초과"
        for row in t["rows"]:
            assert len(row) == len(t["columns"]), "행 길이가 columns 와 다르다"


def test_chat_unavailable_without_key(c, monkeypatch):
    """키가 없을 때 500 이 아니라 사람이 읽을 메시지여야 한다."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "AI_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    r = c.post("/api/v1/chat", json={"messages": [{"role": "user", "content": "MI가 뭐야?"}]})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and isinstance(j["reply"], str) and j["action"]["type"] == "none"


def test_chat_action_allowlist(c, monkeypatch):
    """모델이 이상한 action 을 보내도 화이트리스트 밖이면 none 으로 눌러야 한다."""
    import json as _json
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(sm, "_acall_ai", _fake_acall(_json.dumps(
        {"reply": "ok", "action": {"type": "eval", "code": "alert(1)"}})))
    j = c.post("/api/v1/chat", json={"provider": "claude",
                                     "messages": [{"role": "user", "content": "hi"}]}).json()
    assert j["action"]["type"] == "none"


# ══════════════════════════════════════════════════════════════
# G. 응답 시간 — 클릭 응답 목표 500ms (README §8)
# ══════════════════════════════════════════════════════════════
import time  # noqa: E402


@pytest.mark.parametrize("path", [
    "/api/v1/meta", "/api/v1/grid?period=am", "/api/v1/priorities?period=am&limit=10",
    "/api/v1/stops", "/api/v1/routes",
])
def test_get_under_500ms(c, path):
    t = time.perf_counter()
    assert c.get(path).status_code == 200
    dt = (time.perf_counter() - t) * 1000
    assert dt < 500, f"{path} {dt:.0f}ms"


@pytest.mark.parametrize("strategy", ["efficiency", "equity", "balance", "quick"])
@pytest.mark.parametrize("period", PERIODS)
def test_recommendation_under_500ms(c, strategy, period):
    """입력 조합별 응답 시간 — README 가 '최대 난관'으로 꼽은 재계산 경로."""
    t = time.perf_counter()
    r = c.post("/api/v1/recommendations",
               json={"strategy": strategy, "period": period, "budgetKrw": BUDGET})
    dt = (time.perf_counter() - t) * 1000
    assert r.status_code == 200
    assert dt < 500, f"{strategy}/{period} {dt:.0f}ms"


@pytest.mark.parametrize("n", [1, 5, 10, 20])
def test_simulation_scales(c, n):
    ids = [x["cellId"] for x in c.get(f"/api/v1/priorities?period=am&limit={n}").json()["items"]]
    t = time.perf_counter()
    r = c.post("/api/v1/simulations", json={
        "period": "am", "placements": [{"type": "stop", "cellId": i, "count": 1} for i in ids]})
    dt = (time.perf_counter() - t) * 1000
    assert r.status_code == 200
    assert dt < 500, f"{n}건 배치 {dt:.0f}ms"


def test_simulation_monotonic_in_placements(c):
    """배치를 늘리면 해소 통행량이 줄어들면 안 된다 (단조성)."""
    ids = [x["cellId"] for x in c.get("/api/v1/priorities?period=am&limit=5").json()["items"]]
    prev = -1
    for k in range(1, len(ids) + 1):
        j = c.post("/api/v1/simulations", json={
            "period": "am",
            "placements": [{"type": "freq", "cellId": i, "count": 1} for i in ids[:k]]}).json()
        cur = j["effectiveness"]["resolvedTripsPerDay"]
        assert cur >= prev, f"{k}건에서 감소: {prev} → {cur}"
        prev = cur
