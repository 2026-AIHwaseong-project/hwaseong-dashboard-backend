"""
.env에 키를 넣은 뒤 이 파일을 실행하세요.
    pip install requests python-dotenv
    python check_api.py

3개 API가 전부 200 + 정상 응답을 주는지, 화성시 도시코드가 맞는지 확인합니다.
키 Encoding/Decoding 혼동과 도시코드 오류가 가장 흔한 실패 원인이라 여기서 잡습니다.
"""
import os, sys, json
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

KEY = os.getenv("DATA_GO_KR_KEY_DECODING", "")
TAGO_ROUTE = os.getenv("TAGO_ROUTE_BASE")
TAGO_STOP = os.getenv("TAGO_STOP_BASE")
GG_ROUTE = os.getenv("GG_BUS_ROUTE_BASE")
CITY = os.getenv("TAGO_CITY_CODE", "31240")

ok_all = True

def report(name, ok, msg):
    global ok_all
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {msg}")
    if not ok:
        ok_all = False

if not KEY or "붙여넣기" in KEY:
    print("!! .env의 DATA_GO_KR_KEY_DECODING 을 먼저 채우세요.")
    sys.exit(1)

print("=" * 64)
print("[1] TAGO 버스정류소정보 — 화성시 정류소 조회")
try:
    r = requests.get(
        f"{TAGO_STOP}/getSttnNoList",
        params={"serviceKey": KEY, "cityCode": CITY, "numOfRows": 5,
                "pageNo": 1, "_type": "json"},
        timeout=20,
    )
    body = r.json()["response"]["body"]
    total = body.get("totalCount", 0)
    items = body.get("items", {}).get("item", [])
    if isinstance(items, dict):
        items = [items]
    report("HTTP", r.status_code == 200, f"status={r.status_code}")
    report("정류소 건수", total > 100, f"totalCount={total:,} (화성시면 2,000건 이상 기대)")
    if items:
        print(f"        예시: {items[0].get('nodenm')} ({items[0].get('gpslati')}, {items[0].get('gpslong')})")
except Exception as e:
    report("TAGO 정류소", False, f"{type(e).__name__}: {e}")
    print("        → 응답 원문 일부:", r.text[:200] if 'r' in dir() else "-")

print("=" * 64)
print("[2] TAGO 버스노선정보 — 화성시 노선 조회")
try:
    r = requests.get(
        f"{TAGO_ROUTE}/getRouteNoList",
        params={"serviceKey": KEY, "cityCode": CITY, "numOfRows": 5,
                "pageNo": 1, "_type": "json"},
        timeout=20,
    )
    body = r.json()["response"]["body"]
    total = body.get("totalCount", 0)
    report("HTTP", r.status_code == 200, f"status={r.status_code}")
    report("노선 건수", total > 10, f"totalCount={total:,}")
except Exception as e:
    report("TAGO 노선", False, f"{type(e).__name__}: {e}")
    print("        → 응답 원문 일부:", r.text[:200] if 'r' in dir() else "-")

print("=" * 64)
print("[3] 경기도 버스노선 조회 — 배차간격 확인용")
try:
    r = requests.get(
        f"{GG_ROUTE}/getBusRouteListv2",
        params={"serviceKey": KEY, "keyword": "화성", "format": "json"},
        timeout=20,
    )
    txt = r.text
    report("HTTP", r.status_code == 200, f"status={r.status_code}")
    if "SERVICE" in txt and "ERROR" in txt:
        report("응답", False, txt[:200])
    else:
        data = r.json()
        # 경기도 API는 응답 루트 키가 오퍼레이션마다 달라 통째로 확인
        report("응답 파싱", True, f"최상위 키={list(data.keys())}")
        print("        (배차간격은 getBusRouteInfoItemv2 에서 routeId별로 조회)")
except Exception as e:
    report("경기도 노선", False, f"{type(e).__name__}: {e}")
    print("        → 응답 원문 일부:", r.text[:300] if 'r' in dir() else "-")

print("=" * 64)
if ok_all:
    print("전체 통과 — 수집 스크립트 작성으로 넘어가도 됩니다.")
else:
    print("""실패 항목이 있습니다. 흔한 원인 순서대로 확인하세요.
  1. SERVICE_KEY_IS_NOT_REGISTERED_ERROR
     → DECODING 키가 아니라 ENCODING 키를 넣었을 가능성. .env에서 바꿔보세요.
  2. 승인 직후라면 최대 1시간 반영 지연이 있을 수 있습니다.
  3. totalCount가 0 또는 비정상
     → TAGO_CITY_CODE가 틀렸습니다. 아래로 화성시 코드를 찾으세요.
        https://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCtyCodeList
        ?serviceKey=<키>&_type=json""")
