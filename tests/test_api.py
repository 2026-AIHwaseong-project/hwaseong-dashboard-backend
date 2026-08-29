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
            joined = (system or "") + "".join(m.get("content", "") for m in messages)
            seen["n"] = len(joined)
            seen["text"] = joined
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
    """산출물이 스스로 밝힌 정류장 수와 실제 배포 수가 같아야 한다.

    마스터(stops_hwaseong.csv 3,158)와 API 배포(2,866)는 원래 다르다 —
    승하차 원본과 매칭되지 않아 API 식별자(41590-{ARS})를 만들 수 없는 행
    292개가 빠지기 때문이다. 문제는 그 차이가 아니라 **어디에도 안 적혀
    있는 것**이었으므로, meta.dataQuality.stops 가 세 수(마스터·배포·제외)를
    밝히고 이 테스트가 그것과 API 를 대조한다.
    """
    import pandas as pd
    csv_rows = len(pd.read_csv(ROOT / "dataset_hwaseong" / "stops_hwaseong.csv"))
    api_rows = len(c.get("/api/v1/stops").json()["stops"])
    meta_stops = c.get("/api/v1/meta").json()["dataQuality"]["stops"]

    assert meta_stops["masterCount"] == csv_rows, \
        f"meta 가 말한 마스터 {meta_stops['masterCount']}개 · 실제 CSV {csv_rows}행"
    assert meta_stops["publishedCount"] == api_rows, \
        f"meta 가 말한 배포 {meta_stops['publishedCount']}개 · API {api_rows}개"
    assert meta_stops["masterCount"] - meta_stops["excludedCount"] == api_rows, \
        "마스터 − 제외 = 배포 가 맞지 않는다"
    assert meta_stops.get("excludedReason"), "제외 사유가 비어 있다"


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
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
              "OPENROUTER_API_KEY", "AI_PROVIDER"):
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
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
              "OPENROUTER_API_KEY", "AI_PROVIDER"):
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


# ══════════════════════════════════════════════════════════════
# H. 보고서 출력 계약 — 서버가 모델 출력을 검증하는가
#    (docs/REPORT_PIPELINE_PLAN.md 의 6단계에 대응)
# ══════════════════════════════════════════════════════════════
SIX = ["summary", "status", "problem", "plan", "effect", "next"]

BROKEN_DRAFT = {
    "title": "보고서",
    "sections": [
        {"key": "summary", "heading": "검토 개요", "body": "수요가 많다.", "bullets": []},
        {"key": "made_up", "heading": "7. 없는 장", "body": "지어낸 장.", "bullets": []},
        {"key": "plan", "heading": "개선", "body": "고친다.", "bullets": []},
    ],
    "tables": [{
        "key": "priority",
        "title": "노선 조정 우선순위 및 연차별 투자계획 상세 검토표(2026~2030)",   # 39자
        "columns": ["순위", "격자", "조치"],
        "rows": [[1, "다사6707"], [2, "다사4814", "똑버스", "군더더기"]],
    }],
    "disclaimer": "x",
}


@pytest.fixture
def ai_draft(monkeypatch):
    """AI 가 준 것처럼 임의의 초안을 돌려주게 만드는 헬퍼."""
    import server.main as sm

    def _use(payload):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        monkeypatch.setattr(sm, "_acall_ai", _fake_acall(text))
    return _use


def test_draft_sections_forced_to_requested_keys(c, ai_draft):
    """요청한 key 만 남고, 없는 key 는 버리고, **빠진 장은 채워야 한다.**

    예전에는 `keys == ["summary", "plan"]` 을 단언했다 — 6장을 요청했는데 2장만
    돌아온 상태를 정상으로 고정한 셈이다. 그러면 heading 이 1~2 로 매끈하게
    다시 매겨져 네 장이 없다는 흔적이 사라지고, 화면상 멀쩡해 보이는 채로
    결재 문서까지 내려간다. 계약은 "요청한 장 수만큼 나온다" 여야 한다.
    """
    ai_draft(BROKEN_DRAFT)
    d = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "sections": SIX}).json()
    keys = [s["key"] for s in d["sections"]]
    assert "made_up" not in keys, f"요청에 없는 key 가 살아남음: {keys}"
    assert keys == SIX, f"요청한 6장이 다 나오지 않음: {keys}"
    assert d.get("missingSections") == ["status", "problem", "effect", "next"], \
        f"빠진 장이 기록되지 않음: {d.get('missingSections')}"
    filled = {s["key"]: s.get("body", "") for s in d["sections"]}
    assert "작성하지 못했습니다" in filled["status"], "빈 장이 눈에 안 보인다"
    assert filled["summary"] == "수요가 많다.", "모델이 낸 내용이 덮였다"


def test_chat_draft_is_validated(c, monkeypatch):
    """채팅으로 고친 초안도 계약을 거쳐야 한다 — 예전에는 개수만 셌다.

    섹션 수가 같기만 하면 heading 이 없어도, key 를 지어내도 그대로 나갔다.
    """
    base = {"title": "보고서", "sections": [
        {"key": k, "heading": f"{i}. x", "body": "b"} for i, k in enumerate(SIX, 1)]}
    # 개수는 6 그대로지만 heading 을 지우고 key 하나를 지어냈다
    edited = {"title": "보고서", "sections": (
        [{"key": k, "body": "고침"} for k in SIX[:5]] + [{"key": "made_up", "body": "지어냄"}])}
    import server.main as m
    monkeypatch.setattr(m, "_extract_json",
                        lambda t: {"reply": "고쳤습니다", "draft": edited})
    res = m._chat_result("{}", "claude", "claude-opus-5", "report", base)
    assert "draftRejected" not in res, "개수가 같은데 거부됐다"
    secs = res["draft"]["sections"]
    assert [s["key"] for s in secs] == SIX, [s["key"] for s in secs]
    assert secs[0]["heading"] == "1. 검토 개요", secs[0]["heading"]
    assert res["draft"].get("missingSections") == ["next"], res["draft"].get("missingSections")


def test_draft_headings_are_numbered(c, ai_draft):
    """heading 번호는 서버가 매긴다 — 모델이 빼먹어도 한글 문서에 번호가 붙는다.

    번호는 **요청한 장 순서** 그대로 1부터 끊기지 않는다. 모델이 낸 장만 세면
    빠진 자리가 번호에서도 지워져 문서가 멀쩡해 보인다(위 테스트 참고).
    """
    ai_draft(BROKEN_DRAFT)
    d = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "sections": SIX}).json()
    headings = [s["heading"] for s in d["sections"]]
    assert headings == [f"{i}. {n}" for i, n in enumerate(
        ["검토 개요", "현황 분석", "도출된 문제점", "개선 방안", "기대 효과", "향후 조치 계획"], 1)], headings


def test_draft_partial_sections_still_complete(c, ai_draft):
    """일부만 요청하면 그만큼만, 번호는 다시 1부터."""
    ai_draft(BROKEN_DRAFT)
    d = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "sections": ["plan", "summary"]}).json()
    assert [s["key"] for s in d["sections"]] == ["plan", "summary"]
    assert [s["heading"] for s in d["sections"]] == ["1. 개선 방안", "2. 검토 개요"]
    assert "missingSections" not in d, "다 있는데 빠졌다고 표시됐다"


def test_draft_table_title_truncated(c, ai_draft):
    """엑셀 시트명 31자. 넘으면 프론트가 조용히 자르고 (2) 를 붙인다."""
    ai_draft(BROKEN_DRAFT)
    d = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "sections": SIX}).json()
    for t in d["tables"]:
        assert len(t["title"]) <= 31, f"{len(t['title'])}자: {t['title']}"


def test_draft_table_rows_match_columns(c, ai_draft):
    """RTF 는 초과 칸을 버리고 XLSX 는 다음 열에 쓴다 — 서버가 길이를 맞춰야 둘이 같아진다."""
    ai_draft(BROKEN_DRAFT)
    d = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "sections": SIX}).json()
    for t in d["tables"]:
        width = len(t["columns"])
        assert all(len(r) == width for r in t["rows"]), \
            f"행 길이 {[len(r) for r in t['rows']]} vs columns {width}"


def test_truncated_json_falls_back_not_empty(c, ai_draft):
    """잘린 응답에 빈 문서를 내보내면 제목만 있는 보고서가 결재로 올라간다."""
    ai_draft('{"title":"보고서","sections":[{"key":"summary","heading":"1. 검토 개요","body":"본문이 여기서 잘')
    d = c.post("/api/v1/reports/draft",
               json={"period": "am", "provider": "claude", "sections": SIX}).json()
    assert d["sections"], "빈 문서가 나갔다"
    assert d.get("isAiGenerated") is False
    assert "잘렸" in d["disclaimer"], d["disclaimer"]


def test_fallback_has_six_sections(c, monkeypatch):
    """폴백도 계약과 같은 6장이어야 한다 — 키가 죽어도 목차가 바뀌면 안 된다."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
              "OPENROUTER_API_KEY", "AI_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    d = c.post("/api/v1/reports/draft", json={"period": "am", "sections": SIX}).json()
    assert [s["key"] for s in d["sections"]] == SIX
    assert [s["heading"][:2] for s in d["sections"]] == [f"{i}." for i in range(1, 7)]
    assert all(s["body"].strip() for s in d["sections"]), "본문이 빈 섹션이 있다"


def test_chat_rejects_shrunken_draft(c, monkeypatch):
    """모델이 고친 한 장만 돌려주면 나머지가 사라진다 — 줄어든 초안은 반영하지 않는다."""
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(sm, "_acall_ai", _fake_acall(json.dumps({
        "reply": "1장만 고쳤습니다.", "action": {"type": "none"},
        "draft": {"sections": [{"key": "summary", "heading": "1. 검토 개요", "body": "고친 본문"}]},
    }, ensure_ascii=False)))
    base = {"sections": [{"key": k, "heading": f"{i}. x", "body": "y"}
                         for i, k in enumerate(SIX, 1)]}
    j = c.post("/api/v1/chat", json={
        "mode": "report", "provider": "claude", "draft": base,
        "messages": [{"role": "user", "content": "1장만 고쳐줘"}]}).json()
    assert "draft" not in j, "줄어든 초안이 그대로 반영됐다"
    assert j.get("draftRejected") == "sections_shrank"
    assert j["reply"], "reply 는 그대로 나가야 한다"


def test_chat_accepts_full_draft(c, monkeypatch):
    """전체를 돌려주면 정상 반영되어야 한다 (가드가 과하게 걸리지 않는지)."""
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    full = {"sections": [{"key": k, "heading": f"{i}. x", "body": "고친 본문"}
                         for i, k in enumerate(SIX, 1)]}
    monkeypatch.setattr(sm, "_acall_ai", _fake_acall(json.dumps({
        "reply": "전체를 고쳤습니다.", "action": {"type": "none"}, "draft": full},
        ensure_ascii=False)))
    base = {"sections": [{"key": k, "heading": f"{i}. x", "body": "y"}
                         for i, k in enumerate(SIX, 1)]}
    j = c.post("/api/v1/chat", json={
        "mode": "report", "provider": "claude", "draft": base,
        "messages": [{"role": "user", "content": "전체 고쳐줘"}]}).json()
    assert len(j["draft"]["sections"]) == 6


def test_prompt_has_no_dead_avgmi_and_forbids_invented_numbers(c, monkeypatch):
    """avgMi 는 KPI 에서 지운 필드다. 프롬프트에 남아 있으면 '미제공'이 매번 실린다."""
    import server.main as sm
    seen = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(sm, "_acall_ai",
                        _fake_acall(json.dumps({"sections": [], "tables": []}), seen))
    c.post("/api/v1/reports/draft",
           json={"period": "am", "provider": "claude", "sections": SIX})
    prompt = seen["text"]        # _fake_acall 이 system + user 를 합쳐 담는다
    assert "avgMi" not in prompt and "평균 MI" not in prompt, "죽은 avgMi 줄이 남아 있다"
    # 수치 금지 지시는 2026-08-29 부터 system 프롬프트가 맡는다(user 는 값만 싣는다).
    # 어느 쪽에 있든 **합쳐진 프롬프트에는 반드시 있어야 한다** — 이 지시가 빠지면
    # 모델이 없는 숫자를 채워 넣는다.
    assert ("주어지지 않은 값을 지어내지 않습니다" in prompt
            or "없는 수치는 어떤 경우에도 쓰지 마십시오" in prompt), "수치 금지 문장이 없다"
    assert "위에 주어진 수치만 사용하십시오" in prompt, "user 쪽 수치 한정 문장이 없다"


# ══════════════════════════════════════════════════════════════
# I. 관리자 데이터 업로드
#
# 이 구간의 **가장 중요한 두 건은 신규 기능이 아니라 기존 경로 회귀**다.
# 업로드용으로 RefreshRequest 에 필드를 더하면서 uploadId 없는 요청의 동작이
# 바뀌면, 배포된 프론트의 [모델 재계산]·[화면 반영] 버튼이 조용히 죽는다
# (완료 토스트는 그대로 떠서 실패로 보이지도 않는다).
# ══════════════════════════════════════════════════════════════
import base64 as _b64


@pytest.fixture
def adm(monkeypatch, tmp_path):
    """관리자 상태를 테스트용으로 격리한다 — var/ 도 이력도 tmp 로 돌린다.

    JOB 은 모듈 전역이고 c 픽스처가 session 스코프라, 상태를 되돌리지 않으면
    이후 모든 관리자 테스트가 409 로 무너진다.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from server import admin

    monkeypatch.setattr(admin, "VAR", tmp_path)
    monkeypatch.setattr(admin, "ADMIN_DIR", tmp_path / "admin")
    monkeypatch.setattr(admin, "HISTORY_PATH", tmp_path / "admin" / "history.jsonl")
    monkeypatch.setenv("HW_UPLOAD_ENABLED", "1")
    monkeypatch.delenv("HW_UPLOAD_APPLY", raising=False)

    calls = []

    async def fake_run(steps, reason, actor, upload_id=None, apply=True):
        calls.append({"steps": list(steps), "uploadId": upload_id, "apply": apply})
        admin.JOB.update(status="done")

    monkeypatch.setattr(admin, "_run_refresh", fake_run)
    admin.JOB.update(status="idle", id=None, step=None, error=None, result=None)
    admin._LAST_UPLOAD_AT["t"] = 0.0
    try:
        yield admin, calls
    finally:
        admin.JOB.update(status="idle", id=None, step=None, error=None, result=None)
        admin._LAST_UPLOAD_AT["t"] = 0.0


def _stops_csv() -> str:
    return (ROOT / "dataset_hwaseong" / "stops_national_hwaseong.csv").read_text("utf-8-sig")


def _payload(text: str, encoding: str = "utf-8-sig", **over) -> dict:
    body = {"datasetId": "stopsNational", "filename": "stops.csv",
            "contentB64": _b64.b64encode(text.encode(encoding)).decode(),
            "reason": "테스트 업로드", "actor": "test"}
    body.update(over)
    return body


def test_refresh_without_upload_id_is_unchanged(c, adm):
    """uploadId 없는 갱신은 종전과 완전히 같아야 한다 — 배포된 프론트가 쓰는 경로."""
    admin, calls = adm
    r = c.post("/api/v1/admin/refresh", json={"steps": ["reload"], "reason": "x", "actor": "t"})
    assert r.status_code == 200, r.text
    assert calls[-1]["steps"] == ["reload"], "steps 가 서버에서 바뀌었다"
    assert calls[-1]["apply"] is True, "uploadId 없는 갱신이 예행으로 떨어졌다 — 화면 반영이 죽는다"
    assert calls[-1]["uploadId"] is None


def test_refresh_full_chain_still_applies(c, adm):
    """전체 체인도 여전히 실제 반영까지 간다."""
    admin, calls = adm
    r = c.post("/api/v1/admin/refresh",
               json={"steps": ["join", "model", "validate", "load", "reload"], "reason": "x"})
    assert r.status_code == 200, r.text
    assert calls[-1]["apply"] is True
    assert "load" in calls[-1]["steps"]


def test_upload_is_404_when_disabled(c, adm, monkeypatch):
    """킬 스위치가 꺼져 있으면 업로드는 존재하지 않는다."""
    monkeypatch.setenv("HW_UPLOAD_ENABLED", "0")
    r = c.post("/api/v1/admin/upload", json=_payload(_stops_csv()))
    assert r.status_code == 404, r.text


def test_upload_rejects_unknown_dataset(c, adm):
    """화이트리스트 밖은 400 — 여기 없는 파일은 도달 자체가 불가능하다."""
    r = c.post("/api/v1/admin/upload", json=_payload(_stops_csv(), datasetId="../../etc/passwd"))
    assert r.status_code == 400, r.text


def test_upload_rejects_wrong_header(c, adm):
    """컬럼이 다르면 400 이고, 무엇이 다른지 화면에 띄울 수 있어야 한다."""
    bad = _stops_csv().replace("정류장명", "정류소명", 1)
    r = c.post("/api/v1/admin/upload", json=_payload(bad))
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "정류장명" in detail and "정류소명" in detail, f"진단 정보가 없다: {detail}"


def test_upload_normalizes_cp949(c, adm):
    """엑셀에서 저장한 cp949 가 utf-8-sig 로 정규화돼 저장돼야 한다 —
    03_join 이 utf-8-sig 로 열기 때문에, 안 하면 파이프라인이 죽는다."""
    admin, _ = adm
    r = c.post("/api/v1/admin/upload", json=_payload(_stops_csv(), encoding="cp949"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encodingConverted"] is True
    assert body["rows"] == 3158
    saved = (admin.VAR / body["uploadId"] / "stops_national_hwaseong.csv").read_bytes()
    assert saved[:3] == b"\xef\xbb\xbf", "BOM 이 없다 — 파이프라인이 읽지 못한다"
    assert "정류장명" in saved.decode("utf-8-sig").splitlines()[0]


def test_upload_conflicts_while_job_running(c, adm):
    """갱신 중에는 접수하지 않는다 — workers=1 이라 재계산이 CPU 를 잠식한다."""
    admin, _ = adm
    admin.JOB.update(status="running")
    r = c.post("/api/v1/admin/upload", json=_payload(_stops_csv()))
    assert r.status_code == 409, r.text


def test_apply_is_locked_without_env(c, adm):
    """라이브 반영은 따로 켜야 한다 — 기본은 검증(예행)까지만."""
    admin, calls = adm
    up = c.post("/api/v1/admin/upload", json=_payload(_stops_csv()))
    assert up.status_code == 200, up.text
    uid = up.json()["uploadId"]

    denied = c.post("/api/v1/admin/refresh", json={"uploadId": uid, "apply": True, "reason": "x"})
    assert denied.status_code == 409, denied.text

    admin._LAST_UPLOAD_AT["t"] = 0.0
    ok = c.post("/api/v1/admin/refresh", json={"uploadId": uid, "reason": "x"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["dryRun"] is True
    assert calls[-1]["apply"] is False
    assert calls[-1]["steps"] == ["join", "model", "validate"], "예행이 라이브 단계를 포함한다"


def test_refresh_rejects_bad_upload_id(c, adm):
    """uploadId 로 경로를 벗어날 수 없어야 한다."""
    r = c.post("/api/v1/admin/refresh", json={"uploadId": "../../etc", "reason": "x"})
    assert r.status_code == 400, r.text


def test_upload_client_steps_are_ignored(c, adm):
    """클라이언트가 일부 단계만 보내도 서버가 전체로 치환한다 —
    부분 실행은 옛 세대끼리 정합해 게이트를 통과시킨 뒤 라이브를 자기모순으로 만든다."""
    admin, calls = adm
    up = c.post("/api/v1/admin/upload", json=_payload(_stops_csv()))
    uid = up.json()["uploadId"]
    r = c.post("/api/v1/admin/refresh",
               json={"uploadId": uid, "steps": ["load"], "reason": "x"})
    assert r.status_code == 200, r.text
    assert calls[-1]["steps"] == ["join", "model", "validate"]


# ══════════════════════════════════════════════════════════════
# J. 계약 정합 회귀 — 2026-08-26 프론트-백엔드 계약 불일치 수정분
# ══════════════════════════════════════════════════════════════

def test_recommendation_balance_scope_rules(c):
    """balance 치환은 읍면동(region) 범위에서만 — 지도 영역(cellIds)은 여러 동에
    걸칠 수 있어 지역 균형(동별 1건 상한)이 유효하다.

    두 번 고쳐진 자리다: 처음에는 region 만 보고 치환해 cellIds 에서 이름·목록이
    어긋났고, 다음에는 cellIds 까지 묶어 치환해 **영역을 지정하면 지역 균형을
    고를 수 없는 버그**가 됐다. 치환·목록 제외·이름은 항상 한목소리여야 하고,
    치환 자체는 단일 읍면동 범위에만 성립한다(동별 1건 상한 = 곧 1건 추천)."""
    # ① 지도 영역(cellIds) — balance 가 그대로 살아 동별 1건 상한으로 돈다
    ids = [x["cellId"] for x in
           c.get("/api/v1/priorities?period=am&limit=10").json()["items"]]
    j = c.post("/api/v1/recommendations",
               json={"period": "am", "strategy": "balance", "budgetKrw": BUDGET,
                     "cellIds": ids, "includeAlternatives": True}).json()
    assert j["strategy"] == "balance"
    assert j["simulation"]["name"] == j["strategyLabel"] + " 추천안"
    assert "balance" in [s["id"] for s in j["strategies"]]
    assert j["placements"], "영역 안 need/drt 후보인데 배치가 0건이다"
    regions = [p["region"] for p in j["placements"]]
    assert len(regions) == len(set(regions)), "지역 균형인데 같은 읍면동에 두 건이 놓였다"
    # ② 읍면동(region) — 성립하지 않아 efficiency 로 치환되고 목록·대안에서도 빠진다
    region = next(x["region"] for x in
                  c.get("/api/v1/grid?period=am").json()["cells"] if x["quadrant"] == "need")
    j2 = c.post("/api/v1/recommendations",
                json={"period": "am", "strategy": "balance", "budgetKrw": BUDGET,
                      "region": region, "includeAlternatives": True}).json()
    assert j2["strategy"] == "efficiency"
    assert j2["simulation"]["name"] == j2["strategyLabel"] + " 추천안"
    assert "balance" not in [s["id"] for s in j2["strategies"]]
    assert "balance" not in [a["strategy"] for a in j2["alternatives"]]


def test_chat_daytype_is_validated(c):
    r = c.post("/api/v1/chat", json={"daytype": "xx",
                                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400


def test_chat_pack_follows_daytype(c):
    """주말 화면을 보며 물으면 <사실> 팩도 주말 수치여야 한다 — 예전에는 평일 키
    고정이라 주말 needCells 를 물어도 평일 값으로 답하는 조용한 불일치였다."""
    import server.main as m
    assert m._chat_pack("am", "wd")["현재KPI"] == m.DATA["grid_am"]["kpi"]
    assert m._chat_pack("am", "we")["현재KPI"] == m.DATA["grid_am_we"]["kpi"]
    assert m._chat_pack("am", "we")["현재시간대"]["요일유형"] == "주말"


def test_trips_kpi_reads_weekend_cells(c, monkeypatch):
    """_trips_kpi(daytype="we") 가 주말 셀 사전을 읽는가 — 예전에는 평일 셀을 읽었고,
    현재 배포 데이터는 wd/we 의 flowTripsPerDay 가 같아 티가 안 났을 뿐이다.
    주말 셀만 1씩 흘려 바꿔 본다: 평일 셀을 읽는 구현이면 변화가 안 보인다."""
    import server.main as m
    quad = m.DATA["sim"].S0["am"]["quad0"]
    before, _ = m._trips_kpi("am", quad, "we")
    bumped = {gid: dict(cell, flowTripsPerDay=cell["flowTripsPerDay"] + 1)
              for gid, cell in m.DATA["cells"]["am_we"].items()}
    monkeypatch.setitem(m.DATA["cells"], "am_we", bumped)
    after, _ = m._trips_kpi("am", quad, "we")
    n_need = int((quad == "need").sum())
    assert n_need > 0 and after == before + n_need


def test_meta_cost_compare_single_source(c):
    """/meta 의 costCompare 와 /recommendations summary 가 같은 정본(COST_COMPARE)을
    읽는가. 예전에는 summary 에만 하드코딩돼 있어 프론트 report.js 가
    meta.costCompare 를 우선 참조하고도 항상 자기 폴백 문구로 떨어졌다."""
    meta = c.get("/api/v1/meta").json()
    assert meta["costCompare"]["basis"] == "total"
    j = c.post("/api/v1/recommendations",
               json={"period": "am", "budgetKrw": BUDGET}).json()
    assert j["summary"]["costCompareBasis"] == meta["costCompare"]["basis"]
    assert j["summary"]["costCompareLabel"] == meta["costCompare"]["label"]
    assert j["summary"]["costCompareNote"] == meta["costCompare"]["note"]


def test_admin_params_no_range_limits(c, monkeypatch, tmp_path):
    """파라미터 범위(min/max) 제거(2026-08-26) — 예전 범위 밖 값이 저장되고,
    서버를 실제로 죽이는 값 둘만 400 으로 남는다: 비유한수(Infinity/NaN 은
    응답 직렬화 allow_nan=False 를 죽인다) · 비용 ≤ 0 (추천이 비용으로 나눈다)."""
    from server import admin
    d = tmp_path / "admin"
    d.mkdir()
    monkeypatch.setattr(admin, "ADMIN_DIR", d)
    monkeypatch.setattr(admin, "OVERRIDE_PATH", d / "params_override.json")
    monkeypatch.setattr(admin, "HISTORY_PATH", d / "history.jsonl")
    try:
        r = c.post("/api/v1/admin/params", json={
            "reason": "범위 제거 검증", "actor": "test",
            "changes": {"sim.headwayMult": 5.0,                      # 옛 max 2.0 밖
                        "cost.defaultBudget": 5_000_000_000_000}})   # 옛 max 1조 밖
        assert r.status_code == 200, r.text
        rows = {p["key"]: p for p in c.get("/api/v1/admin/params").json()["params"]}
        assert rows["sim.headwayMult"]["min"] is None and rows["sim.headwayMult"]["max"] is None
        assert rows["sim.headwayMult"]["effective"] == 5.0
        # 사유는 선택 — 없이도 저장된다 (2026-08-26 완화)
        assert c.post("/api/v1/admin/params", json={
            "changes": {"sim.phi.night": 3.0}}).status_code == 200
        assert c.post("/api/v1/admin/params", json={
            "reason": "비용 0 가드", "actor": "test",
            "changes": {"cost.stop.krw": 0}}).status_code == 400
        # httpx 는 inf 를 직렬화하지 않으므로 원문으로 보낸다 — Python json 파서는
        # `Infinity` 리터럴을 받아들여서 이 경로가 실제로 존재하는 공격면이다.
        r_inf = c.post("/api/v1/admin/params",
                       content=b'{"reason": "\ube44\uc720\ud55c\uc218 \uac00\ub4dc", "actor": "test",'
                               b' "changes": {"sim.phi.am": Infinity}}',
                       headers={"Content-Type": "application/json"})
        assert r_inf.status_code == 400, r_inf.text
    finally:
        # 메모리에 적용된 오버라이드를 기본값으로 되돌린다 — 오버라이드 파일(tmp)을
        # 지운 뒤 재적용하면 COST_KRW·sim 속성·meta 사본이 전부 기본값으로 돌아와
        # 세션 스코프 TestClient 를 공유하는 다른 테스트가 오염되지 않는다.
        if admin.OVERRIDE_PATH.exists():
            admin.OVERRIDE_PATH.unlink()
        admin.apply_runtime_params()


def test_scenario_share_roundtrip(c, monkeypatch, tmp_path):
    """시나리오 공유 — 저장한 것을 링크(id)로 그대로 되찾는가, 쓰레기 입력은 막히는가."""
    import server.main as m
    monkeypatch.setattr(m, "SCEN_DIR", tmp_path / "scenarios")
    cell = c.get("/api/v1/priorities?period=am&limit=1").json()["items"][0]["cellId"]
    r = c.post("/api/v1/scenarios", json={
        "name": "공유 검증\x07시나리오", "period": "am", "daytype": "we",
        "budgetKrw": 1_000_000_000,
        "placements": [{"type": "drt", "cellId": cell, "count": 2}]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    got = c.get(f"/api/v1/scenarios/{sid}").json()
    assert got["name"] == "공유 검증시나리오"          # 제어문자는 걷어낸다
    assert got["period"] == "am" and got["budgetKrw"] == 1_000_000_000
    assert got["daytype"] == "we"                     # 요일 토글 상태가 링크로 복원된다
    assert c.post("/api/v1/scenarios", json={
        "daytype": "xx", "placements": [{"type": "drt", "cellId": cell}]}).status_code == 400
    assert got["placements"] == [{"type": "drt", "cellId": cell, "count": 2}]
    # 빈 시나리오·형식 위반 id·없는 id 는 각각 400/400/404
    assert c.post("/api/v1/scenarios", json={"placements": []}).status_code == 400
    assert c.get("/api/v1/scenarios/x").status_code == 400
    assert c.get("/api/v1/scenarios/aaaaaaaaaaaa").status_code == 404
    # 배치 검증은 /simulations 와 같은 자 — 없는 격자는 400
    assert c.post("/api/v1/scenarios", json={
        "placements": [{"type": "stop", "cellId": "없는격자", "count": 1}]}).status_code == 400


def test_grid_override_wiring(c, monkeypatch, tmp_path):
    """관리자 격자 오버라이드 — 저장 즉시 /grid 셀·라벨·KPI 에 반영되고(셀에서
    재집계 — "지도만 붉어지고 KPI 는 그대로" 사고 방지), 되돌리면 원값 복원."""
    from server import admin
    d = tmp_path / "admin"
    d.mkdir()
    monkeypatch.setattr(admin, "ADMIN_DIR", d)
    monkeypatch.setattr(admin, "GRID_OVERRIDE_PATH", d / "grid_override.json")
    monkeypatch.setattr(admin, "HISTORY_PATH", d / "history.jsonl")
    grid = c.get("/api/v1/grid?period=am").json()
    kpi0 = grid["kpi"]["needCells"]
    gid = next(x["id"] for x in grid["cells"] if x["quadrant"] == "mid")
    rec_id = None
    try:
        r = c.post("/api/v1/admin/grid-overrides", json={
            "gridId": gid, "period": "am", "daytype": "wd",
            "field": "quadrant", "value": "need", "reason": "현장 실사 검증용 테스트"})
        assert r.status_code == 200, r.text
        rec_id = r.json()["override"]["id"]
        assert r.json()["override"]["prev"] == "mid"
        g2 = c.get("/api/v1/grid?period=am").json()
        c2 = next(x for x in g2["cells"] if x["id"] == gid)
        assert c2["quadrant"] == "need" and c2["overridden"] is True
        assert c2["quadrantLabel"] == "고수요·저공급"      # 라벨을 빼먹으면 값과 라벨이 다른 말을 한다
        assert g2["kpi"]["needCells"] == kpi0 + 1
        # 입력 검증 — 1단계에서 안 연 필드(mi)·짧은 사유·없는 격자·틀린 값
        assert c.post("/api/v1/admin/grid-overrides", json={
            "gridId": gid, "period": "am", "field": "mi", "value": 1.0,
            "reason": "다섯자넘는사유"}).status_code == 400
        # 사유는 선택(2026-08-26 완화) — 비워도 저장되고, 같은 자리 재저장은
        # 옛것을 자동 취소하며 진짜 원값(prev)을 물려받는다
        r2 = c.post("/api/v1/admin/grid-overrides", json={
            "gridId": gid, "period": "am", "daytype": "wd",
            "field": "quadrant", "value": "need"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["override"]["reason"] == ""
        assert r2.json()["override"]["prev"] == "mid"
        rec_id = r2.json()["override"]["id"]
        assert c.post("/api/v1/admin/grid-overrides", json={
            "gridId": "없는격자", "period": "am", "field": "quadrant", "value": "need",
            "reason": "다섯자넘는사유"}).status_code == 400
        assert c.post("/api/v1/admin/grid-overrides", json={
            "gridId": gid, "period": "am", "field": "quadrant", "value": "빨강",
            "reason": "다섯자넘는사유"}).status_code == 400
    finally:
        if rec_id:
            rr = c.post("/api/v1/admin/grid-overrides/revoke",
                        json={"id": rec_id, "reason": "테스트 정리"})
            assert rr.status_code == 200, rr.text
    g3 = c.get("/api/v1/grid?period=am").json()
    c3 = next(x for x in g3["cells"] if x["id"] == gid)
    assert c3["quadrant"] == "mid" and c3["overridden"] is False
    assert g3["kpi"]["needCells"] == kpi0


def test_pipeline_params_unlocked_and_boot_safe(c, monkeypatch, tmp_path):
    """모델·기준선 상수 개방(2026-08-26) — 저장은 requiresRefresh 로 답하고,
    재계산 전에 서버가 재기동돼도 시뮬 엔진이 죽지 않는다(크래시 루프 회귀).

    잠갔던 이유가 이것이었다: params.py 가 import 시점에 오버라이드를 적용하는데
    엔진이 params 에서 상수를 직접 읽어, 옛 산출물과 새 상수가 어긋나면 기준선
    assert 로 서버가 못 떴다. 지금은 엔진이 norm_stats(산출물)에서 읽는다 —
    아래에서 params 는 오버라이드 값(9.0)을, 엔진은 산출물 값(2.0)을 들고 있어야
    한다. 같은 프로세스에서 두 값이 공존하는 것이 이 설계의 증명이다."""
    from server import admin
    d = tmp_path / "admin"
    d.mkdir()
    monkeypatch.setattr(admin, "ADMIN_DIR", d)
    monkeypatch.setattr(admin, "OVERRIDE_PATH", d / "params_override.json")
    monkeypatch.setattr(admin, "HISTORY_PATH", d / "history.jsonl")
    try:
        r = c.post("/api/v1/admin/params", json={
            "reason": "개방 검증",
            "changes": {"model.minFreqPerHour": 9.0, "baseline.wFreq": 0.5}})
        assert r.status_code == 200, r.text
        assert set(r.json()["requiresRefresh"]) == {"baseline.wFreq", "model.minFreqPerHour"}
        rows = {p["key"]: p for p in c.get("/api/v1/admin/params").json()["params"]}
        assert rows["model.minFreqPerHour"]["editable"] is True
        assert rows["baseline.wFreq"]["editable"] is True
        # 재계산 전 — 실제 적용값(산출물의 자)은 그대로고 '재계산 대기'가 선다
        assert rows["baseline.wFreq"]["effective"] == 0.78
        assert rows["baseline.wFreq"]["pending"] is True
        # 크래시 루프 회귀 — 오버라이드가 있는 채로 엔진을 새로 exec(=재기동 경로)
        import os
        import subprocess
        out = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util;"
             "spec=importlib.util.spec_from_file_location('sim_boot','analysis/05_simulate.py');"
             "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
             "import params;"
             "print(m.MIN_FREQ_PER_H, params.MIN_FREQ_PER_H, m.W_FREQ)"],
            cwd=str(ROOT), env={**os.environ, "HW_VAR_DIR": str(tmp_path)},
            capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, (out.stdout or "") + (out.stderr or "")
        sim_min, params_min, sim_wfreq = out.stdout.split()[-3:]
        assert float(sim_min) == 2.0      # 엔진 = 산출물의 자 (재계산 전 옛 값 유지)
        assert float(params_min) == 9.0   # 파이프라인 = 오버라이드 (재계산이 쓸 값)
        assert float(sim_wfreq) == 0.78
    finally:
        if admin.OVERRIDE_PATH.exists():
            admin.OVERRIDE_PATH.unlink()
        admin.apply_runtime_params()


def test_premium_model_gate(c, monkeypatch):
    """익명 사용자의 프리미엄 모델 지정 차단(심층 분석 [높음] — 비용 증폭 벡터).
    ADMIN_TOKEN 이 설정된 서버에서만 게이트가 선다 — 토큰이 아예 없으면 콘솔
    전체가 열려 있는 상태라 이 게이트만 잠그는 것은 뜻이 없다."""
    monkeypatch.setenv("ADMIN_TOKEN", "gate-test-token")
    body = {"provider": "claude", "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "hi"}]}
    # 무토큰 + 프리미엄 명시 → 403
    assert c.post("/api/v1/chat", json=body).status_code == 403
    assert c.post("/api/v1/reports/draft",
                  json={"provider": "claude", "model": "claude-opus-5",
                        "period": "am"}).status_code == 403
    # 올바른 토큰이면 통과 (키가 없어도 403 이 아니라 폴백 200)
    r = c.post("/api/v1/chat", json=body,
               headers={"Authorization": "Bearer gate-test-token"})
    assert r.status_code == 200
    # 모델 미지정·기본 모델은 무토큰 그대로 — auto 와 같아 비용 증폭이 아니다
    assert c.post("/api/v1/chat", json={
        "provider": "claude", "messages": [{"role": "user", "content": "hi"}]
    }).status_code == 200
    assert c.post("/api/v1/chat", json={
        "provider": "claude", "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    # 저가(fast) 모델 지정은 막지 않는다
    assert c.post("/api/v1/chat", json={
        "provider": "claude", "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    # ADMIN_TOKEN 미설정이면 게이트도 열린다
    monkeypatch.delenv("ADMIN_TOKEN")
    assert c.post("/api/v1/chat", json=body).status_code == 200


def test_chat_multi_actions(c, monkeypatch):
    """복수 액션 계약(2026-08-27) — actions 배열을 그대로 싣되 화이트리스트로 거른다.
    action(단수)은 구버전 화면 호환으로 첫 액션을 함께 보낸다."""
    import json as _json
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    def ask(payload):
        monkeypatch.setattr(sm, "_acall_ai", _fake_acall(_json.dumps(payload)))
        return c.post("/api/v1/chat", json={
            "provider": "claude", "messages": [{"role": "user", "content": "hi"}]}).json()

    # ① 배열 그대로 + 단수 호환
    j = ask({"reply": "ok", "actions": [{"type": "period", "value": "night"},
                                        {"type": "layer", "value": "supply"}]})
    assert [a["type"] for a in j["actions"]] == ["period", "layer"]
    assert j["action"]["type"] == "period"

    # ② 새 액션들이 통과하는가 (1·2단계에서 연 것)
    j = ask({"reply": "ok", "actions": [{"type": "daytype", "value": "we"},
                                        {"type": "budget", "value": 50},
                                        {"type": "place", "tool": "drt",
                                         "cellId": "다사6707", "count": 2}]})
    assert [a["type"] for a in j["actions"]] == ["daytype", "budget", "place"]

    # ③ 화이트리스트 밖은 버리고, none 도 실행 목록에서 뺀다
    j = ask({"reply": "ok", "actions": [{"type": "eval", "code": "alert(1)"},
                                        {"type": "none"},
                                        {"type": "select", "cellId": "다사6707"}]})
    assert [a["type"] for a in j["actions"]] == ["select"]

    # ④ nav 뒤는 자른다 — 페이지를 떠나므로 실행되지 않는다
    j = ask({"reply": "ok", "actions": [{"type": "nav", "page": "simulation"},
                                        {"type": "recommend"}]})
    assert [a["type"] for a in j["actions"]] == ["nav"]

    # ⑤ 상한 4개
    j = ask({"reply": "ok", "actions": [{"type": "period", "value": "am"}] * 6})
    assert len(j["actions"]) == 4

    # ⑥ 구버전 형식(action 단수)도 받는다
    j = ask({"reply": "ok", "action": {"type": "recommend"}})
    assert [a["type"] for a in j["actions"]] == ["recommend"]

    # ⑦ 할 일이 없으면 빈 배열 + action none
    j = ask({"reply": "설명만", "actions": [{"type": "none"}]})
    assert j["actions"] == [] and j["action"]["type"] == "none"


def test_refresh_step_dependencies(c, adm, monkeypatch):
    """재계산 단계 의존성 보정(2026-08-27) — 독립 검증 P0-3·주의 2·주의 3.

    model 만 돌리고 load 를 빼면 grid_metrics 는 새 상수로 다시 구워지는데
    server/static/*.json 은 옛 세대라, reload 가 옛 JSON 을 읽어 **잡은 성공인데
    화면 수치만 안 바뀐다.** db 도 같은 이유로 옛 세대를 적재해 기준일이 후퇴한다.
    사람이 눈치채기 어려운 조합이라 서버가 보정한다."""
    admin, calls = adm

    def ask(body):
        r = c.post("/api/v1/admin/refresh", json=body)
        return r, (r.json() if r.status_code == 200 else None)

    # ① model → load 자동 추가 + 순서 정규화
    r, j = ask({"steps": ["reload", "model"], "reason": "의존성"})
    assert r.status_code == 200, r.text
    assert j["steps"] == ["model", "load", "reload"]
    assert j["autoAddedSteps"] == ["load"]
    assert calls[-1]["steps"] == ["model", "load", "reload"]

    # ② db 도 load 를 요구한다 (계약 JSON 을 적재하므로).
    #    db 단계는 DATABASE_URL 이 있어야 접수되므로 값만 넣는다 — 실제 적재는
    #    adm 픽스처가 _run_refresh 를 대역으로 바꿔 두어 일어나지 않는다.
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    _, j = ask({"steps": ["db"], "reason": "의존성"})
    assert "load" in j["steps"] and j["steps"].index("load") < j["steps"].index("db")

    # ③ 이미 온전한 조합은 건드리지 않는다
    _, j = ask({"steps": ["join", "model", "validate", "load", "reload"], "reason": "온전"})
    assert j["autoAddedSteps"] == []

    # ④ reload 단독은 산출물을 다시 굽지 않으므로 보정 대상이 아니다
    _, j = ask({"steps": ["reload"], "reason": "화면 반영"})
    assert j["steps"] == ["reload"] and j["autoAddedSteps"] == []

    # ⑤ 일반 재계산에 dryRun 을 얹으면 400 — 예행인 줄 알고 눌렀는데 라이브가
    #    도는 형태를 막는다(조용한 무시가 가장 나쁘다)
    r, _ = ask({"steps": ["model"], "dryRun": True, "reason": "예행 오해"})
    assert r.status_code == 400 and "dryRun" in r.json()["detail"]


def test_report_number_guard_and_fallback_notice(c, monkeypatch):
    """산문 수치 대조 가드(2026-08-29) — 구조만 보던 검증에 문장 속 숫자를 더한다.

    독립 검증에서 격자를 '500m×500m'(실제 1km), 단가 1.8억을 '18억 원'으로 적은
    초안이 그대로 나갔다. 문장을 고치지는 않는다 — 어디가 의심스러운지 표시하고
    담당자가 판단한다(보고서 자체는 정상 부분이 많아 버리지 않는다)."""
    import json as _json
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    def ask(body_text):
        draft = {"title": "검토(안)", "sections": [
            {"key": "summary", "heading": "1. 검토 개요", "body": body_text, "bullets": []}]}
        monkeypatch.setattr(sm, "_acall_ai", _fake_acall(_json.dumps(draft)))
        return c.post("/api/v1/reports/draft",
                      json={"period": "am", "sections": ["summary"]}).json()

    # ① 입력에 없는 금액·거리 → 경고 + disclaimer 앞머리
    j = ask("사업비는 월 18억 원이며 500m×500m 격자 기준으로 분석하였다.")
    assert j.get("numberWarnings"), j.get("numberWarnings")
    assert "확인되지 않는 수치" in j["disclaimer"]

    # ② 입력에 있는 수치만 쓰면 경고 없음 (786격자·1000m·단가 4,200만 원)
    j2 = ask("전체 786개 격자를 1000m 기준으로 분석하였다. 정류장 단가는 4,200만 원이다.")
    assert not j2.get("numberWarnings"), j2.get("numberWarnings")
    assert "확인되지 않는 수치" not in (j2.get("disclaimer") or "")


def test_report_uses_system_prompt(c, monkeypatch):
    """지시문은 system 으로, 이번 요청 수치는 user 로 나뉘어 나가는가.
    (문체 고정 + 프롬프트 캐시의 전제 — 고정부에 매번 바뀌는 값이 섞이면 안 된다)"""
    import json as _json
    import server.main as sm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    seen = {}

    async def fake(provider, model, system, messages, **kw):
        seen["system"] = system
        seen["user"] = messages[0]["content"]
        return _json.dumps({"title": "t", "sections": [
            {"key": "summary", "heading": "1. 검토 개요", "body": "본문", "bullets": []}]}), provider, model

    monkeypatch.setattr(sm, "_acall_ai", fake)
    c.post("/api/v1/reports/draft", json={"period": "am", "sections": ["summary"]})
    assert seen["system"] and "공문서 문체" in seen["system"]
    # 고정부에 시간대·날짜 같은 가변값이 섞이면 캐시가 매번 미스난다
    assert "출근" not in seen["system"] and "2026-" not in seen["system"]
    # 가변값은 user 쪽에 있어야 한다
    assert "출근" in seen["user"]
