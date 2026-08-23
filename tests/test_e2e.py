# -*- coding: utf-8 -*-
"""
③ 브라우저 E2E — Playwright(Chromium)

    pip install pytest-playwright && playwright install chromium
    python -m pytest tests/test_e2e.py -q -m e2e

전제
    · 프론트 저장소(hwaseong-dashboard)가 백엔드와 같은 부모 폴더에 있을 것
      → 백엔드가 /app/ 으로 직접 서빙하므로 같은 원점, CORS 불필요
    · 백엔드는 conftest.py 의 live_server 픽스처가 자동으로 띄운다

순수 JS 라 Jest+RTL 같은 프레임워크 테스트 도구가 맞지 않는다. 확인해야 할 것이
"브라우저에서 실제로 그려지는가" 이므로 E2E 가 유일하게 맞는 계층이다.

⚠️ 이 화면은 외부 CDN(카카오맵 SDK·Pretendard 폰트)을 부른다. 오프라인 CI 에서는
   그 요청만 실패하고 SVG 지도는 정상 렌더링된다 — 테스트는 그 실패를 무시하고
   자체 스크립트 오류(pageerror)만 잡는다.
"""
import re

import pytest

pytestmark = pytest.mark.e2e

playwright_sync = pytest.importorskip("playwright.sync_api", reason="playwright 미설치")
from playwright.sync_api import sync_playwright, expect  # noqa: E402

CELL = "#map g.cells rect[data-id]"
EXTERNAL = ("dapi.kakao.com", "cdn.jsdelivr.net", "fonts.", "t1.daumcdn.net")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        # --no-proxy-server: 사내/CI 프록시 뒤에서 127.0.0.1 이 프록시로 새는 것을 막는다
        b = p.chromium.launch(args=["--no-proxy-server"])
        yield b
        b.close()


@pytest.fixture
def page(browser, live_server, frontend_available):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.script_errors = []          # 자체 JS 예외
    pg.failed_own = []             # 같은 원점 리소스 로드 실패
    pg.base = live_server
    pg.on("pageerror", lambda e: pg.script_errors.append(str(e)))
    # 콘솔의 "Failed to load resource" 는 URL 을 담지 않아 CDN 인지 구분되지 않는다.
    # 리소스 실패는 requestfailed 에서 URL 로 판별하고, 외부 도메인은 무시한다.
    pg.on("requestfailed", lambda r: pg.failed_own.append(r.url)
          if r.url.startswith(live_server) else None)
    yield pg
    ctx.close()


def _open(pg, html: str, **params):
    q = "&".join(f"{k}={v}" for k, v in {"server": pg.base, **params}.items())
    pg.goto(f"{pg.base}/app/{html}?{q}", wait_until="networkidle", timeout=60_000)
    pg.wait_for_selector(CELL, timeout=30_000)
    pg.wait_for_timeout(600)


def _api(pg, path):
    return pg.request.get(pg.base + path).json()


def _stop_band_cell(pg, period="am"):
    """정류장 신설 구간(0.15 <= coverage < 0.50) 격자 하나. 수단과 격자를 맞춰
    고르지 않으면 '단가는 맞는데 배치가 안 되는' 테스트가 된다."""
    for c in _api(pg, f"/api/v1/grid?period={period}")["cells"]:
        if c["action"] == "NEW_STOP":
            return c["id"]
    pytest.skip("NEW_STOP 구간 격자가 없다")


# ══════════════════════════════════════════════════════════════
# 1. 대시보드 로드 — 격자가 실제로 그려지는가
# ══════════════════════════════════════════════════════════════
def test_grid_renders_all_cells(page):
    """격자 수를 리터럴로 박지 않는다 — meta 에서 파생해 500m 전환에도 살아남게."""
    _open(page, "index.html")
    expected = _api(page, "/api/v1/meta")["grid"]["cellCount"]
    assert page.locator(CELL).count() == expected, \
        f"격자 {page.locator(CELL).count()}개 렌더 (기대 {expected}개)"


def test_no_script_errors_on_load(page):
    _open(page, "index.html")
    assert page.script_errors == [], page.script_errors[:3]
    assert page.failed_own == [], page.failed_own[:3]


def test_kpi_matches_api(page):
    """상단 KPI 가 /grid 응답과 같은 수를 보여주는가."""
    _open(page, "index.html")
    need = _api(page, "/api/v1/grid?period=am")["kpi"]["needCells"]
    assert re.sub(r"[^\d]", "", page.locator("#k1").inner_text()) == str(need)


def test_map_colors_are_not_uniform(page):
    """bins 가 빠지면 지도가 오류 없이 한 색으로 칠해진다 — 그 회귀를 잡는다."""
    _open(page, "index.html")
    classes = page.eval_on_selector_all(
        CELL, "els => [...new Set(els.map(e => e.getAttribute('class')))]")
    assert len(classes) >= 4, f"색 구간이 {len(classes)}종뿐: {classes}"


def test_region_boundaries_drawn(page):
    _open(page, "index.html")
    assert page.locator("#map path").count() >= 29, "읍면동 경계(29개)가 안 그려졌다"


# ══════════════════════════════════════════════════════════════
# 2. 우선순위 Top 10 — 개수와 정렬
# ══════════════════════════════════════════════════════════════
def test_top10_count_and_order(page):
    _open(page, "index.html")
    items = page.locator("#t10 > *")
    api = _api(page, "/api/v1/priorities?period=am&limit=10")["items"]
    assert items.count() == len(api) == 10

    texts = [items.nth(i).inner_text() for i in range(items.count())]
    # 순번이 1..10 으로 붙는가
    assert [t.strip().split()[0] for t in texts] == [str(i) for i in range(1, 11)]
    # 격자 ID 와 순서가 API 와 일치하는가
    for i, row in enumerate(api):
        assert row["cellId"] in texts[i], f"{i+1}위 격자 불일치: {row['cellId']}"
    # 점수가 내림차순인가
    scores = [int(m.group(1)) for t in texts if (m := re.search(r"(\d+)점", t))]
    assert scores == sorted(scores, reverse=True), scores


def test_period_switch_changes_top10(page):
    """'시간대를 바꾸면 우선순위가 뒤집힌다' — 이 제품의 핵심 주장이 화면에서 보이는가."""
    _open(page, "index.html")
    before = page.locator("#t10").inner_text()
    page.locator(".periods button").nth(3).click()      # 심야
    page.wait_for_timeout(1200)
    after = page.locator("#t10").inner_text()
    assert before != after, "심야로 바꿔도 Top10 이 그대로다"

    night = _api(page, "/api/v1/grid?period=night")["kpi"]
    assert re.sub(r"[^\d]", "", page.locator("#k1").inner_text()) == str(night["needCells"])


def test_cell_click_opens_route_strip(page):
    """격자 클릭 → 경유 노선. nearestStopId 가 어긋나면 여기서 조용히 끊긴다."""
    _open(page, "index.html")
    top = _api(page, "/api/v1/priorities?period=am&limit=1")["items"][0]["cellId"]
    page.locator(f'{CELL}[data-id="{top}"]').dispatch_event("click")
    page.wait_for_timeout(1200)
    assert page.locator("#routeStrip").inner_text().strip() != "", "노선 스트립이 비었다"
    assert page.script_errors == [], page.script_errors[:3]
    assert page.failed_own == [], page.failed_own[:3]


# ══════════════════════════════════════════════════════════════
# 3. 시뮬레이션 — 배치하면 KPI 가 기준선 대비 갱신되는가
# ══════════════════════════════════════════════════════════════
def test_placement_updates_kpi_against_baseline(page):
    _open(page, "simulation.html")
    assert all("기준선" in page.locator(f"#k{i}d").inner_text() for i in (1, 2, 3)), \
        "배치 전에는 전부 기준선이어야 한다"

    # 배차 증편(▲)을 골라 심야 1순위 격자에 배치 — 효과가 확실히 나는 조합
    page.locator(".periods button").nth(3).click()
    page.wait_for_timeout(1000)
    target = _api(page, "/api/v1/priorities?period=night&limit=1")["items"][0]["cellId"]
    page.locator("#tools button").nth(2).click()
    page.locator(f'{CELL}[data-id="{target}"]').dispatch_event("click")
    page.wait_for_timeout(2500)

    assert page.locator("#placeList").inner_text().strip() != "", "배치 목록이 비었다"
    deltas = [page.locator(f"#k{i}d").inner_text() for i in (1, 2, 3)]
    assert any("기준선" not in d for d in deltas), f"KPI 델타가 갱신되지 않음: {deltas}"
    assert page.script_errors == [], page.script_errors[:3]
    assert page.failed_own == [], page.failed_own[:3]


def test_cost_reflects_unit_price(page):
    """배치 1건의 집행액이 meta 의 단가와 같아야 한다."""
    _open(page, "simulation.html")
    unit = _api(page, "/api/v1/meta")["cost"]["stop"]["krw"]        # 정류장 신설
    target = _stop_band_cell(page)
    page.locator("#tools button").nth(0).click()
    page.locator(f'{CELL}[data-id="{target}"]').dispatch_event("click")
    page.wait_for_timeout(1500)
    used = page.locator("#budUsed").inner_text()
    assert re.sub(r"[^\d]", "", used) == str(unit // 10_000), \
        f"집행액 표기 {used} vs 단가 {unit:,}원"


def test_undo_and_reset(page):
    _open(page, "simulation.html")
    target = _stop_band_cell(page)
    page.locator("#tools button").nth(0).click()
    page.locator(f'{CELL}[data-id="{target}"]').dispatch_event("click")
    page.wait_for_timeout(1500)
    assert page.locator("#map g.placed > *").count() == 1

    page.locator("#btnUndo").click()
    page.wait_for_timeout(1500)
    assert page.locator("#map g.placed > *").count() == 0, "되돌리기가 안 먹었다"
    assert all("기준선" in page.locator(f"#k{i}d").inner_text() for i in (1, 2, 3)), \
        "되돌린 뒤 KPI 가 기준선으로 안 돌아왔다"


def test_recommendation_button_fills_placements(page):
    """AI 추천 배치안 — 서버 그리디 결과가 화면에 실리는가."""
    _open(page, "simulation.html")
    page.locator("#btnRecommend").click()
    page.wait_for_timeout(6000)
    assert page.locator("#recBox").inner_text().strip() != "", "추천 결과가 비었다"
    assert page.locator("#map g.placed > *").count() > 0, "추천 배치가 지도에 안 찍혔다"
    assert page.script_errors == [], page.script_errors[:3]
    assert page.failed_own == [], page.failed_own[:3]


# ══════════════════════════════════════════════════════════════
# 4. 보고서 — 파일이 실제로 내려받아지는가
# ══════════════════════════════════════════════════════════════
# 보고서 모달은 **기록 목록**을 먼저 연다. 예전에는 [AI 보고서]를 누르면 곧바로
# 생성이 나갔지만, 지금은 목록 안의 [AI 보고서 생성]을 눌러야 요청이 나간다.
# 두 단계를 한곳에 모아 둔다 — 흐름이 또 바뀌면 여기만 고치면 된다.
def _open_report_draft(page):
    page.locator("[data-report-open]:visible").first.click()
    gen = page.locator("[data-gen-new]")
    expect(gen).to_be_visible(timeout=10_000)
    gen.click()
    page.wait_for_timeout(8000)


@pytest.mark.parametrize("label,ext", [("한글 문서", ".rtf"), ("엑셀 파일", ".xlsx")])
def test_report_download(page, label, ext):
    """AI 키가 없어도 규칙 기반 초안으로 내려와야 한다 (마지막 방어선)."""
    _open(page, "simulation.html")
    _open_report_draft(page)

    btn = page.get_by_role("button", name=re.compile(label))
    expect(btn.first).to_be_visible(timeout=15_000)
    with page.expect_download(timeout=30_000) as dl:
        btn.first.click()
    path = dl.value.path()
    assert dl.value.suggested_filename.endswith(ext), dl.value.suggested_filename
    assert path and path.stat().st_size > 500, f"파일이 비었다: {path}"
    if ext == ".xlsx":
        import zipfile
        assert zipfile.is_zipfile(path), "xlsx 가 유효한 ZIP 이 아니다"


def test_report_has_six_sections(page):
    """섹션 6개·번호 1~6 은 한글 문서로 그대로 나가는 계약이다."""
    _open(page, "simulation.html")
    _open_report_draft(page)
    body = page.locator("body").inner_text()
    assert "검토 개요" in body, "보고서 초안이 렌더링되지 않았다"


def test_report_opens_to_list_not_generation(page):
    """[AI 보고서]는 **목록**을 연다 — 누르는 순간 AI 호출이 나가지 않는다."""
    _open(page, "simulation.html")
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/reports/draft" in r.url else None)
    page.locator("[data-report-open]:visible").first.click()
    expect(page.locator("[data-gen-new]")).to_be_visible(timeout=10_000)
    page.wait_for_timeout(2000)
    assert not calls, f"열기만 했는데 생성 요청이 나갔다: {calls}"
    body = page.locator("body").inner_text()
    assert "아직 만든 보고서가 없습니다" in body or "AI 보고서 생성" in body, body[:300]


# ══════════════════════════════════════════════════════════════
# 5. ?server= 파라미터 — 시연 때 코드를 안 고치고 주소만 바꾸는 경로
# ══════════════════════════════════════════════════════════════
def test_server_param_is_honored(page):
    """?server= 로 지정한 주소로 실제 API 호출이 나가는가."""
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/" in r.url else None)
    _open(page, "index.html")
    assert calls, "API 호출이 한 건도 없다"
    assert all(u.startswith(page.base) for u in calls), \
        f"지정하지 않은 주소로 나간 호출: {[u for u in calls if not u.startswith(page.base)][:3]}"


def test_server_param_persists_across_pages(page):
    """한 번 열면 기억되고, 다른 화면으로 넘어가도 유지되어야 한다."""
    _open(page, "index.html")
    calls = []
    page.on("request", lambda r: calls.append(r.url) if "/api/v1/" in r.url else None)
    page.goto(f"{page.base}/app/simulation.html", wait_until="networkidle", timeout=60_000)
    page.wait_for_selector(CELL, timeout=30_000)
    assert calls and all(u.startswith(page.base) for u in calls), \
        "?server= 없이 이동하니 다른 주소로 나간다"


def test_bad_server_param_shows_error_not_blank(page):
    """죽은 주소를 주면 흰 화면이 아니라 사유가 보여야 한다."""
    page.goto(f"{page.base}/app/index.html?server=http://127.0.0.1:1",
              wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(9000)
    body = page.locator("body").inner_text()
    boot = page.locator("#bootError")
    assert (boot.count() and boot.is_visible()) or len(body.strip()) > 200, \
        "백엔드 연결 실패인데 화면에 아무 안내가 없다"
