# 화성시 버스 대시보드 API 명세서

> **Base URL** `http://localhost:8000`  
> **버전** v1 · **업데이트** 2026-08-13  
> **기동** `pip install -r requirements.txt` → `python main.py` ([README §1](../README.md#1-빠른-실행))  
> **Docker** `docker compose up --build`
>
> 이 문서가 엔드포인트 계약의 정본입니다. 응답 예시는 실서버(`/openapi.json`) 기준입니다.

---

## 목차

| # | 엔드포인트 | 메서드 | 설명 |
|---|---|---|---|
| 1 | `/api/v1/meta` | GET | 화면 구성 메타 (지역·기간·비용·경계) |
| 2 | `/api/v1/grid` | GET | 격자 데이터 + KPI |
| 3 | `/api/v1/priorities` | GET | 우선순위 목록 (need·drt 격자) |
| 4 | `/api/v1/stops` | GET | 정류장 목록 |
| 5 | `/api/v1/routes` | GET | 노선 목록 + 경로 |
| 6 | `/api/v1/stops/{stopId}/profile` | GET | 정류장 시간대별 승하차 프로파일 |
| 7 | `/api/v1/simulations` | POST | 배치 시뮬레이션 ★ |
| 8 | `/api/v1/recommendations` | POST | AI 추천 배치안 ★ |
| 9 | `/api/v1/reports/draft` | POST | 보고서 초안 생성 (AI 프록시) |
| 10 | `/api/v1/providers` | GET | AI 프로바이더·모델 목록 |
| 11 | `/api/v1/scenarios` | POST | 시나리오 공유 저장 (공유 링크의 실체) |
| 12 | `/api/v1/scenarios/{id}` | GET | 공유 시나리오 조회 |

---

## 공통 규칙

### 헤더
```
Content-Type: application/json
Accept: application/json
```

### CORS · 미들웨어
모든 오리진 허용 (`allow_origins=["*"]`). 별도 인증 없음.
**메서드는 GET·POST 만 허용**합니다 (`allow_methods=["GET","POST"]`).

응답 1KB 이상은 gzip 으로 나갑니다 (`GZipMiddleware(minimum_size=1024)`).

REST 외에 정적 마운트가 둘 있습니다.

| 경로 | 내용 |
|---|---|
| `/data` | `server/static/` 계약 JSON 직접 열람 (예: `/data/grid_am.json`) |
| `/app` | 형제 폴더에 프론트 저장소가 있으면 대시보드 화면을 같은 원점에서 서빙 |

루트 `/` 는 404 입니다.

### 시간대 코드 (`period`)
| 값 | 이름 | 시간 |
|---|---|---|
| `am` | 출근 | 07–09시 |
| `day` | 낮 | 09–17시 |
| `pm` | 퇴근 | 17–19시 |
| `night` | 심야 | 22–24시 |

유효하지 않은 값 → `400 Bad Request`

### 오류 응답
```json
{ "detail": "오류 메시지" }
```
| 코드 | 상황 |
|---|---|
| 400 | 잘못된 파라미터 (period, strategy, provider, count·maxPlacements 범위 등) |
| 404 | 리소스 없음 (stop profile) |
| 500 | 서버 오류 |

**AI 오류는 500 이 아닙니다.** `POST /reports/draft` 는 키가 하나도 없거나 LLM 호출·파싱이
실패해도 **200 + 규칙 기반 폴백 보고서**를 돌려줍니다(`isAiGenerated: false`). §9 참고.

---

## 1. `GET /api/v1/meta`

화면 구성에 필요한 고정 메타 정보. 서버 시작 시 1회 로드, 이후 불변.

### 응답

```json
{
  "region": "화성시",
  "updatedAt": "2026-08-13",
  "isMockData": false,
  "periods": [
    { "id": "am",    "name": "출근", "label": "07–09", "hours": [7, 9]   },
    { "id": "day",   "name": "낮",   "label": "09–17", "hours": [9, 17]  },
    { "id": "pm",    "name": "퇴근", "label": "17–19", "hours": [17, 19] },
    { "id": "night", "name": "심야", "label": "22–24", "hours": [22, 24] }
  ],
  "grid": {
    "sizeMeters": 1000,
    "displaySizeMeters": 1000,
    "cellCount": 786,
    "analysisCellCount": 786,
    "crs": "EPSG:4326",
    "bbox": [126.53771, 37.01994, 127.15638, 37.29048]
  },
  "map": {
    "regions": [
      {
        "code": "31240130",
        "name": "우정읍",
        "kind": "읍",
        "centroid": [126.838, 37.062],
        "bbox": [126.77, 37.02, 126.89, 37.11],
        "rings": [[[126.77, 37.02], [126.89, 37.02], ...]]
      }
    ]
  },
  "cost": {
    "stop": { "krw": 42000000,  "annualKrw": 4200000,   "basis": "capital",   "lifeYears": 10 },
    "drt":  { "krw": 180000000, "annualKrw": 180000000, "basis": "operating", "lifeYears": 1  },
    "freq": { "krw": 95000000,  "annualKrw": 95000000,  "basis": "operating", "lifeYears": 1  },
    "defaultBudget": 3000000000
  },
  "effects": [
    {
      "type": "stop",
      "label": "정류장 신설",
      "icon": "●",
      "radiusKm": 2.0,
      "unitKrw": 42000000,
      "coverageRange": [0.15, 0.5]
    },
    { "type": "drt",  "label": "똑버스 배치", "icon": "◆", "radiusKm": 3.0, "unitKrw": 180000000, "annualKrw": 180000000, "basis": "operating", "lifeYears": 1, "coverageRange": [0, 0.15] },
    { "type": "freq", "label": "배차 증편",   "icon": "▲", "radiusKm": 2.4, "unitKrw": 95000000,  "annualKrw": 95000000,  "basis": "operating", "lifeYears": 1, "coverageRange": [0.5, 1.0] }
  ],
  "dataQuality": {
    "boardingDaily":  { "level": "observed",  "label": "일별 승하차",       "source": "경기데이터드림 정류소별 승하차 인원 집계 (2025-12~2026-03)" },
    "boardingHourly": { "level": "estimated", "label": "시간대별 승하차",
                        "method": "일자별 승하차를 교통카드 OD 15분단위 실측 시간분포(법정동별)로 안분",
                        "note":   "원자료에 시간대 정보가 없습니다. 시간분포는 교통카드 OD(15분단위) 실측을 씁니다." },
    "flowHourly":     { "level": "observed",  "label": "시간대별 유동인구", "source": "경기도 분석갤러리 유동인구(화성시) · 2023-12~2024-01",
                        "note":   "승하차와 약 2년 시차가 있어 시간배율로만 사용합니다." },
    "headway":        { "level": "observed",  "label": "배차간격",         "source": "경기도 버스노선 조회 API (peekAlloc/nPeekAlloc/nightAlloc)" },
    "boundary":       { "level": "observed",  "label": "행정경계",         "source": "SGIS 통계지리정보서비스 읍면동 경계 (bnd_dong_00_2025_2Q)" }
  },
  "assumptions": {
    "busTripRate":    { "value": 0.25, "confirmed": false, "note": "1인 1일 버스통행 = 전수단 원단위 2.5 × 버스분담률 0.1" },
    "minFreqPerHour": { "value": 2.0,  "confirmed": false, "note": "적정·공급과잉 판정의 절대 하한. 야간 상대평가 오라벨 방지" }
  },
  "formula": {
    "demand": "0.5·norm_board + 0.5·norm_potential",
    "supply": "0.78·norm_freq + 0.22·coverage",
    "mi":     "(zD − zS) · (D/dRef)^0.65",
    "dampExp": 0.65, "wFreq": 0.78, "wCov": 0.22, "eldCoef": 1.6,
    "coverageThresholdM": 600, "needMiThreshold": 0.75
  }
}
```

> `cost.*` 에는 가정값 플래그가 없습니다. 단가가 가정값이라는 표시는 프론트
> `assets/js/config.js` 의 `COST[].confirmed` 가 담당합니다. `assumptions` 에 실리는 것은
> `busTripRate` 와 `minFreqPerHour` 둘뿐입니다.

---

## 2. `GET /api/v1/grid?period=am`

### 쿼리 파라미터
| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `period` | string | `am` | 시간대 코드 |

### 응답

```json
{
  "period": "am",
  "scale": {
    "miThresholds": [-1.2, -0.7, -0.25, 0.25, 0.7, 1.2]
  },
  "kpi": {
    "needCells": 30,
    "drtCells": 72,
    "overCells": 21,
    "totalCells": 786,
    "needShare": 3.8,
    "potentialTripsPerDay": 39110,
    "elderlyTripsPerDay": 3290
  },
  "cells": [ /* 786개, 아래 셀 필드 참고 */ ]
}
```

### 셀(cell) 필드

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | string | 격자 ID (예: `"다사3921"`) |
| `name` | string | 권역명 |
| `region` | string | 읍면동명 |
| `regionCode` | string | 읍면동 코드 |
| `regionKind` | string | `"읍"` / `"면"` / `"동"` |
| `lon` | number | 경도 (EPSG:4326) |
| `lat` | number | 위도 (EPSG:4326) |
| `demand` | integer | 수요지수 D × 100 (0–100) |
| `supply` | integer | 공급지수 S × 100 (0–100) |
| `zDemand` | number | 수요 z-점수 |
| `zSupply` | number | 공급 z-점수 |
| `mi` | number | 미스매칭지수 (−2.6 ~ +2.6) |
| `flow` | number | 정규화 잠재통행량 nf (0–1) |
| `flowTripsPerDay` | integer | 추정 일 버스통행량 (`round(격자 인구 × 0.25)` — `meta.assumptions.busTripRate`) |
| `elderlyRatio` | number | 고령비 (0–1) |
| `coverage` | number | 정류장 접근 커버리지 (0.05–1.0) |
| `quadrant` | string | `need` / `drt` / `over` / `ok` / `mid` |
| `quadrantLabel` | string | 한국어 레이블 |
| `action` | string | `NEW_STOP` / `DRT` / `ADD_FREQ` |
| `actionLabel` | string | `"신설"` / `"똑버스"` / `"증차"` |
| `priorityScore` | number | 우선순위 점수 (need 격자만 양수) |
| `nearestStopId` | string | 최근접 정류장 ID (예: `"41590-37539"`) |
| `adjusted` | boolean | 시뮬레이션 결과 변경 여부 |
| `bins.mi` | integer | MI 등급 0–6 (MI 임계값 [-1.2,−0.7,−0.25,0.25,0.7,1.2] 기준) |
| `bins.demand` | integer | 수요 5분위 0–4 |
| `bins.supply` | integer | 공급 5분위 0–4 |
| `bins.flow` | integer | 잠재통행 5분위 0–4 |

### 사분면(`quadrant`) 판정 기준

| 값 | 조건 | 조치 |
|---|---|---|
| `need` | zD ≥ 0.2 **and** MI ≥ 0.75 | 신설 / 증차 |
| `over` | zD ≤ −0.3 **and** zS ≥ 0.3 **and** freq/h ≥ 2 | 효율화 |
| `drt` | zD ≤ −0.35 **and** zS ≤ −0.35 **and** 잠재통행 ≥ P30 | 똑버스 |
| `ok` | zD ≥ 0.25 **and** zS ≥ 0.25 **and** freq/h ≥ 2 | 적정 |
| `mid` | 그 외 | — |

> 판정 순서: need → over → drt → ok → mid (첫 매치)

---

## 3. `GET /api/v1/priorities?period=am&limit=10`

### 쿼리 파라미터
| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `period` | string | `am` | 시간대 코드 |
| `limit` | integer | `10` | 반환 최대 건수. **0~100** 밖이면 400 |

### 응답

```json
{
  "period": "am",
  "items": [
    {
      "rank": 1,
      "cellId": "다사6707",
      "name": "동탄7동 동부",
      "mi": 1.8088,
      "priorityScore": 2.5823,
      "demand": 71,
      "supply": 25,
      "flowTripsPerDay": 1505,
      "elderlyRatio": 0.0732,
      "coverage": 0.05,
      "action": "DRT",
      "actionLabel": "똑버스",
      "nearestStopId": "41590-55750",
      "reason": "수요지수 71 대비 공급지수 25, 가장 가까운 정류장이 510m 밖 (커버리지 0.05) — 노선 미연결, 수요응답형 필요"
    }
  ]
}
```

- `need` + `drt` 격자를 `priorityScore` 내림차순 정렬
- `reason`: 서버가 데이터 기반으로 자동 생성하는 한국어 설명문

---

## 4. `GET /api/v1/stops`

### 응답

```json
{
  "stops": [
    {
      "id": "41590-37539",
      "arsNo": "37539",
      "name": "양지말입구",
      "dong": "송산면",
      "lon": 126.715767,
      "lat": 37.205283,
      "kind": "rural",
      "routes": ["GGB233000067"],
      "boardingsPerDay": 0.0
    }
  ]
}
```

정류장 2,866개. `boardingsPerDay` 는 평일 일평균 초승(환승 제외)이고 **소수**입니다.
값이 없어서 0 인 것과 실측 0 이 구분되지 않으므로, 점 크기 등에 쓸 때는 결측 처리를
따로 두세요.

### 정류장 `kind` 분류

| 값 | 기준 | 실제 분포 |
|---|---|---|
| `hub` | 경유 노선 수 ≥ 5 | 678 |
| `rural` | 소재 읍면동이 `면` | 944 |
| `res` | 그 외 | 1,244 |

> **정류장 ID 형식** `"41590-{ARS번호}"` — 화성시 시군구코드(41590) + ARS 번호

---

## 5. `GET /api/v1/routes`

### 응답

```json
{
  "routes": [
    {
      "id": "GGB200000008",
      "name": "400",
      "type": "trunk",
      "stopIds": ["41590-37325", "41590-37321", "..."],
      "path": [[127.044, 37.185], [127.046, 37.188], "..."]
    }
  ]
}
```

### 노선 `type` 분류

| 값 | 기준 |
|---|---|
| `trunk` | 일반버스 |
| `local` | 마을버스 |
| `drt` | 똑버스 / 수요응답형 |

> **현재 응답 200개 노선이 전부 `trunk` 입니다.** 마을버스 155개 노선은 경유정류소가
> 비공개라 파이프라인에서 제외됐습니다(`analysis/09_augment_routes.py`). 판정 코드는
> 3종을 지원하지만(`analysis/05_load.py`) `local`·`drt` 는 실제로 나오지 않습니다.

- `stopIds`: 화성시 내 정류장만 포함 (순서 보장)
- `path`: `[lon, lat]` 배열 — `stopIds`와 동일 순서

---

## 6. `GET /api/v1/stops/{stopId}/profile`

### 경로 파라미터
| 파라미터 | 타입 | 예시 |
|---|---|---|
| `stopId` | string | `41590-37539` |

### 응답

```json
{
  "stopId": "41590-55524",
  "stopName": "동탄호수공원.부영4차",
  "kind": "hub",
  "routes": ["GGB200000152", "GGB223000056", "..."],
  "isEstimated": true,
  "estimationMethod": "일자별 승하차를 교통카드 OD 15분단위 실측 시간분포(법정동별)로 안분",
  "hours": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
  "boardings":  [18.25, 54.02, 132.4, 181.09, 85.8, 64.54, "..."],
  "alightings": [6.56, 19.41, 47.57, 65.06, 30.83, 23.19, "..."],
  "summary": {
    "boardingsPerDay": 1788.1,
    "alightingsPerDay": 642.4,
    "peakSharePct": 31.9
  }
}
```

- `hours`: 5시–23시 (19개 구간)
- **`boardings`·`alightings`·`summary.*` 는 소수입니다.** 일 총량을 시간분포로 안분한
  결과라 정수가 아닙니다 — 파싱·표시에서 정수로 가정하지 마세요.
- `peakSharePct`: (07·08·17·18시 합산 / 전체) × 100
- 쿼리 파라미터가 없습니다. `?period=` 를 붙여도 무시되고 응답은 동일합니다.
- 존재하지 않는 `stopId` → `404 Not Found`

---

## 7. `POST /api/v1/simulations` ★

배치를 순차 적용하고 4개 시간대 전체의 KPI 변화와 786개 격자 재계산 결과를 반환합니다.

### 요청

```json
{
  "name": "시나리오 1",
  "period": "am",
  "budgetKrw": 3000000000,
  "placements": [
    { "type": "stop", "cellId": "다사3921", "count": 1 },
    { "type": "freq", "cellId": "다사6311", "count": 1 }
  ]
}
```

| 필드 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `name` | string | `"시나리오"` | 시나리오 이름 |
| `period` | string | `"am"` | 현재 표시 시간대 (유효성 검증용) |
| `budgetKrw` | integer | `3000000000` | 예산 (원). **음수면 400** |
| `placements` | array | `[]` | 배치 목록. **100건을 넘으면 400** (`MAX_PLACEMENTS=100`) |

#### `placements` 항목

| 필드 | 타입 | 설명 |
|---|---|---|
| `type` | string | `stop` / `drt` / `freq` — 그 밖의 값은 **400** |
| `cellId` | string | 격자 ID — 존재하지 않으면 **400** |
| `count` | integer | 수량 (기본 1). **1~20** 밖이면 400 (`MAX_COUNT=20`) |

#### 수단별 물리 파라미터

비용은 **총사업비**입니다(연환산 아님). 연환산은 `meta.cost.*.annualKrw` 를 보세요 —
정류장만 내용연수 10년으로 나뉘어 420만 원이고 나머지 둘은 총액과 같습니다.

| 수단 | 반경 | 주입 공급량 | 총사업비 |
|---|---|---|---|
| `stop` (신설) | 800m 도보, 2km 커버리지 | 출퇴근 4.8회/창, 낮 8회, 심야 0 | 42,000,000원 |
| `drt` (똑버스) | 3,000m | φ = 2.4회/창 (낮 9.6) | 180,000,000원 |
| `freq` (증편) | 2,200m | headway × 0.7 (운행 ×1.43) | 95,000,000원 |

### 응답

```json
{
  "id": "SIM-7590457",
  "name": "시나리오 1",
  "createdAt": "2026-08-13 07:53",
  "placements": [
    {
      "type": "stop", "typeLabel": "정류장 신설",
      "cellId": "다사6707", "cellName": "동탄7동 동부",
      "count": 1, "radiusKm": 2.0, "unitKrw": 42000000
    },
    {
      "type": "freq", "typeLabel": "배차 증편",
      "cellId": "다사6809", "cellName": "동탄9동 중심",
      "count": 1, "radiusKm": 2.4, "unitKrw": 95000000
    }
  ],
  "cost": {
    "totalKrw": 137000000,
    "breakdown": [
      { "type": "stop", "label": "정류장 신설", "cellId": "다사6707", "unitKrw": 42000000, "count": 1, "amountKrw": 42000000 },
      { "type": "freq", "label": "배차 증편",   "cellId": "다사6809", "unitKrw": 95000000, "count": 1, "amountKrw": 95000000 }
    ]
  },
  "budgetKrw": 3000000000,
  "overBudget": false,
  "periods": [
    {
      "period": "am", "periodName": "출근",
      "kpi": {
        "needCells": 28, "drtCells": 72, "overCells": 21, "totalCells": 786,
        "needShare": 3.6, "potentialTripsPerDay": 38833, "elderlyTripsPerDay": 3268
      },
      "baseline": {
        "needCells": 30, "drtCells": 72, "overCells": 21, "totalCells": 786,
        "needShare": 3.8, "potentialTripsPerDay": 39110, "elderlyTripsPerDay": 3290
      },
      "delta": {
        "needCells": -2, "drtCells": 0, "overCells": 0,
        "needShare": -0.2, "potentialTripsPerDay": -277, "elderlyTripsPerDay": -22
      }
    }
    /* + day, pm, night */
  ],
  "effectiveness": {
    "resolvedNeedCells": 5,
    "resolvedTripsPerDay": 735,
    "krwPerTripPerDay": 186272
  },
  "cellsByPeriod": {
    "am":    [ /* /grid 의 cells 와 동일 형식, adjusted=true 포함 */ ],
    "day":   [ /* ... */ ],
    "pm":    [ /* ... */ ],
    "night": [ /* ... */ ]
  }
}
```

- `resolvedTripsPerDay`: Poisson 회귀 기반 ΔB̂ (예측 승차 증가량) — **4개 시간대 합산**입니다.
  `periods[].delta` 는 시간대별이고 이 값은 전 시간대 합이라 서로 직접 비교되지 않습니다.
- `overBudget`(최상위): `cost.totalKrw` 가 요청한 `budgetKrw` 를 넘었는가.
  **`budgetKrw` 를 0 이나 생략으로 보내면 예산 제약이 없다는 뜻이라 항상 `false`** 입니다.
- `krwPerTripPerDay`: `totalKrw ÷ resolvedTripsPerDay`. **ΔB̂ ≤ 0 이면 숫자가 아니라 `null`**
  입니다 — 그대로 포맷하면 화면이 깨지므로 널 처리를 두세요.
- `resolvedNeedCells` 도 4개 시간대 합산입니다(같은 격자가 여러 시간대에서 풀리면 중복 계수).
- `cellsByPeriod`: 786개 전부 반환, 변경된 격자는 `adjusted: true`

---

## 8. `POST /api/v1/recommendations` ★

예산 내에서 전략에 따라 배치 위치를 그리디 알고리즘으로 자동 선정합니다.

### 요청

```json
{
  "strategy": "efficiency",
  "period": "am",
  "budgetKrw": 3000000000,
  "maxPlacements": 10,
  "allowedTypes": ["stop", "drt", "freq"],
  "region": null,
  "cellIds": null,
  "includeAlternatives": false
}
```

| 필드 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `strategy` | string | `"efficiency"` | 추천 전략 (아래 참고) |
| `period` | string | `"am"` | **후보·목적함수·수단 게이트의 기준 시간대.** 시간대를 바꾸면 순위가 바뀝니다 |
| `budgetKrw` | integer | `3000000000` | 예산 상한 (원) |
| `maxPlacements` | integer | `10` | 최대 배치 건수 |
| `allowedTypes` | array | `["stop","drt","freq"]` | 허용 수단 |
| `region` | string\|null | `null` | 특정 읍면동으로 범위 제한 (예: `"새솔동"`) |
| `cellIds` | array\|null | `null` | 임의 격자 집합으로 범위 제한 (지도 드래그 영역). **`region` 보다 우선** |
| `includeAlternatives` | boolean | `false` | 다른 전략 요약 병렬 반환 여부 |

**범위 제한(`cellIds` · `region`)의 동작**

- 둘 다 오면 `cellIds` 가 이깁니다. `region` 은 무시됩니다.
- `cellIds: []` (빈 배열)는 **"범위를 지정했는데 대상이 없다"** 로 해석해 `placements: []` ·
  `stoppedBecause: "no_candidate"` 를 돌려줍니다. 화성시 전체 추천으로 넓어지지 **않습니다**.
  `null` 이어야 "범위 제한 없음"입니다.
- 존재하지 않는 격자 ID나 없는 읍면동 이름도 400 이 아니라 같은 방식으로 0건입니다
  (해석 불가한 범위 = 빈 결과). 사용자가 "짓겠다"고 지정한 `placements[]` 는 반대로
  알 수 없는 `cellId` 에 400 을 던집니다 — 의도가 다르기 때문입니다.
- **`region`(읍면동 하나) 범위에서만** `balance`(지역 균형)가 `efficiency` 로
  대체됩니다 — 동별 1건 상한이 곧 1건 추천이라 성립하지 않기 때문입니다. 이때
  `strategies` 목록·`alternatives` 에서도 함께 빠집니다. 응답의 `strategy` 필드로
  확인하세요. **`cellIds`(지도 영역)에서는 대체되지 않습니다** — 영역은 여러
  읍면동에 걸칠 수 있어 "영역 안에서 동별 1개씩"이 뜻 있는 전략입니다.

#### 추천 전략

| `strategy` | 설명 | 특이사항 |
|---|---|---|
| `efficiency` | 사업비 1원당 ΔB̂(예측 승차 증가) 최대화 | 기본값 |
| `equity` | 고령 통행량 기준 효과 최대화 | need·drt 격자의 `eldw` 가중 ΔBhat |
| `balance` | 읍면동당 최대 1개 원칙 | 지역 균형 분배 |
| `quick` | `stop` 수단만 허용 | 시설 투자 없이 빠른 효과 |

#### 그리디 알고리즘 요약

1. 후보: **요청 `period` 기준** `need` + `drt` 격자.
   예전에는 am 으로 못박혀 있어 시간대를 바꿔도 결과가 같았습니다 — 지금은 바뀝니다.
2. 수단은 조합으로 경쟁하지 않고 **커버리지로 배타 결정**됩니다 —
   `cov < 0.15` → `drt`, `0.15 ≤ cov < 0.5` → `stop`, `cov ≥ 0.5` → `freq`.
   따라서 한 격자의 후보 수단은 언제나 하나입니다.
3. 매 회차: 후보별 ΔB̂ ÷ **총사업비**로 효율을 재고 최고값을 고릅니다.
   같은 수단끼리는 **800m 이내에 겹쳐 놓지 않습니다**(도보권 중복 방지).
4. 상태 갱신 → 반복. 종료 조건은 `summary.stoppedBecause` 로 나갑니다 —
   `max_reached` / `budget_exhausted` / `budget_too_small` / `no_further_gain` / `no_candidate`.

> 비용 비교는 **총사업비 기준**입니다(`summary.costCompareBasis: "total"`).
> 예산 한도를 총액으로 자르므로 순위도 같은 자로 매깁니다 — 연환산으로 매기면
> 정류장 쪽으로 쏠립니다. 세 기준을 나란히 돌려 본 사후분석은 프론트 저장소
> `docs/API.md` §2.1 에 있습니다.

### 응답

```json
{
  "method": "budget-constrained greedy marginal benefit",
  "methodLabel": "예산 제약 하 한계효과 최대화",
  "methodNote": "출근 시간대 기준으로, …사업비 1원당 가장 많이 줄이는 지점을 순차 선택합니다. …",
  "region": null,
  "strategy": "efficiency",
  "strategyLabel": "효율 최우선",
  "strategyNote": "사업비 1원당 해소 통행량이 가장 큰 순서로 고릅니다.",
  "note": "사업비 1원당 해소 통행량이 가장 큰 순서로 고릅니다.",
  "strategies": [
    { "id": "efficiency", "label": "효율 최우선", "note": "사업비 1원당 해소 통행량이 가장 큰 순서로 고릅니다." }
    /* + equity, balance, quick */
  ],
  "budgetKrw": 3000000000,
  "usedKrw": 897000000,
  "remainingKrw": 2103000000,
  "placements": [
    {
      "rank": 1,
      "type": "freq", "typeLabel": "배차 증편",
      "cellId": "다사6509", "cellName": "동탄6동 동부",
      "region": "동탄6동",
      "count": 1,
      "radiusKm": 2.4,
      "costKrw": 95000000,
      "expectedResolvedTrips": 850
    }
  ],
  "producedBy": {
    "placements": "최적화 알고리즘 (예산 제약 하 그리디)",
    "narrative": "Claude",
    "deterministic": true,
    "deterministicNote": "같은 조건이면 항상 같은 결과가 나옵니다. 다른 안이 필요하면 난수가 아니라 전략(목적)을 바꿉니다."
  },
  "summary": {
    "count": 10,
    "totalKrw": 897000000,
    "budgetKrw": 3000000000,
    "budgetUsedPct": 29.9,
    "expectedResolvedCells": 11,
    "expectedResolvedTrips": 17680,
    "expectedResolvedPotentialTrips": 4120,
    "expectedResolvedElderlyTrips": 1068,
    "krwPerTrip": 50735,
    "stoppedBecause": "max_reached",
    "costCompareBasis": "total",
    "costCompareLabel": "총사업비 기준",
    "costCompareNote": "예산 한도와 같은 기준(총사업비)으로 비교했습니다. 똑버스·증편은 이듬해에도 같은 예산이 필요합니다."
  },
  /* "해소 통행"의 자(尺)를 못박아 둡니다 — 같은 이름이 다른 뜻으로 쓰이지 않도록.
     · items[].expectedResolvedTrips  = 그 배치 한 건의 4시간대 합산 ΔB̂ (예측 승차 증가/일)
     · summary.expectedResolvedTrips  = items 합 = simulation.effectiveness.resolvedTripsPerDay
     · summary.expectedResolvedPotentialTrips = 요청 시간대 사각지대 잠재수요 감소량/일 (다른 지표)
     앞의 둘은 반드시 같은 값이어야 하며 tests/test_api.py 가 이를 검사합니다. */
  "simulation": { /* POST /simulations 응답과 동일한 구조 */ },
  "alternatives": [
    {
      "strategy": "equity",
      "label": "교통약자 우선",
      "count": 10,
      "totalKrw": 844000000,
      "mix": { "stop": 2, "drt": 0, "freq": 8 }
    }
  ]
}
```

- **`stoppedBecause` 는 `summary` 안에 있습니다** (최상위가 아닙니다).
- `alternatives`: `includeAlternatives: true` 일 때만 포함. **선택된 전략은 목록에서 빠지므로**
  4개 전략 중 3개만 옵니다.
- `producedBy`: 배치는 알고리즘이 정하고 문장만 AI 가 다듬는다는 것을 응답에 명시합니다.
  `deterministic: true` — 같은 조건이면 항상 같은 결과입니다.
- `simulation`: 선택된 `placements`를 `/simulations`에 넣은 것과 동일한 전체 결과

---

## 9. `POST /api/v1/reports/draft`

선택한 AI 프로바이더를 통해 공문 형식의 보고서 초안을 생성합니다.

### 요청

```json
{
  "period": "am",
  "provider": "auto",
  "model": null,
  "tone": "공문",
  "sections": ["summary", "status", "problem", "plan", "effect", "next"],
  "context": {
    "kpi": { "needCells": 30, "totalCells": 786, "needShare": 3.8, "potentialTripsPerDay": 39110 },
    "priorities": [ /* /priorities 응답의 items */ ],
    "simulation": { /* /simulations 응답 (선택) */ },
    "recommendation": { /* /recommendations 응답 (선택) */ }
  }
}
```

| 필드 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `period` | string | `"am"` | 분석 시간대 |
| `provider` | string | `"auto"` | `auto` / `claude` / `openai` / `gemini` |
| `model` | string\|null | `null` | 모델 ID 지정 (null = 프로바이더 기본값) |
| `tone` | string | `"공문"` | 문서 형식 |
| `sections` | array | (아래 참고) | 포함할 섹션 키 목록 |
| `context` | object | `{}` | KPI·우선순위·시뮬 결과 전달 |

#### 프로바이더 우선순위 (`auto`)

1. `.env` 의 `AI_PROVIDER` — **단, 해당 키도 있어야** 채택합니다. 키 없는 지정은 무시하고 다음으로.
2. 키를 보유한 첫 번째 프로바이더 (`ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `GOOGLE_API_KEY`)
3. 하나도 없으면 **규칙 기반 초안**으로 폴백합니다 (200 응답, 아래 참고).

`.env` 의 `AI_MODEL` 은 `AI_PROVIDER` 로 지정한 프로바이더에만 적용됩니다 —
다른 프로바이더 요청에 남의 모델 ID 가 넘어가는 것을 막기 위해서입니다.

#### 기본 섹션 키
`summary` (검토 개요) · `status` (현황) · `problem` (문제점) · `plan` (개선안) · `effect` (기대효과) · `next` (향후계획)

### 응답

```json
{
  "title": "화성시 대중교통 수급 불일치 분석 및 노선 조정 검토(안)",
  "subtitle": "출근 시간대(07–09) 기준",
  "org": "화성시",
  "dept": "교통정책과",
  "period": "am",
  "generatedAt": "2026-08-13 07:55",
  "provider": "Claude (Anthropic)",
  "model": "claude-sonnet-5",
  "isAiGenerated": true,
  "sections": [
    {
      "key": "summary",
      "heading": "1. 검토 개요",
      "body": "화성시 786개 격자 분석 결과 출근 시간대 기준 고수요·저공급 격자 30개(3.8%)가 확인됨.",
      "bullets": ["need 격자 30개 중 대부분이 동탄·봉담 신개발지 집중", "..."]
    }
  ],
  "tables": [
    {
      "key": "priority",
      "title": "노선 조정 우선순위 (상위 5개 격자)",
      "columns": ["순위", "격자", "권역", "수요", "공급", "MI", "조치"],
      "rows": [[1, "다사6707", "동탄7동 동부", 71, 25, 1.8088, "똑버스"]]
    }
  ],
  "disclaimer": "본 문서는 AI가 자동 생성한 초안입니다. 담당자 검토 후 활용하시기 바랍니다."
}
```

#### AI 키가 없거나 호출이 실패하면

**500 이 아니라 200** 이 나갑니다. 서버가 규칙 기반 초안(`_fallback_report`)을 만들어
같은 스키마로 돌려줍니다. LLM 응답의 JSON 파싱이 실패해도 마찬가지입니다.

| 필드 | 폴백일 때 값 |
|---|---|
| `isAiGenerated` | `false` |
| `provider` | `"규칙 기반 초안 (AI 미사용)"` |
| `model` | `null` |
| `sections` | 요청한 key 그대로 **전부**. 모델이 낸 장이 모자라면 서버가 채우고 `missingSections` 에 어느 장을 채웠는지 싣습니다 |

프론트는 `isAiGenerated` 로 분기하세요. 오류 코드로는 구분되지 않습니다.

---

## 10. `GET /api/v1/providers`

환경변수 기반으로 사용 가능한 AI 프로바이더와 모델 목록을 반환합니다. 프론트엔드 드롭다운 구성에 활용하세요.

### 응답

```json
{
  "configuredDefault": null,
  "providers": [
    {
      "id": "claude",
      "label": "Claude (Anthropic)",
      "available": true,
      "envKey": "ANTHROPIC_API_KEY",
      "defaultModel": "claude-sonnet-5",
      "models": [
        { "id": "claude-sonnet-5",          "name": "Claude Sonnet 5",   "tier": "standard" },
        { "id": "claude-opus-5",             "name": "Claude Opus 5",     "tier": "premium"  },
        { "id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "tier": "fast"     }
      ]
    },
    {
      "id": "openai",
      "label": "GPT (OpenAI)",
      "available": false,
      "envKey": "OPENAI_API_KEY",
      "defaultModel": "gpt-5.6-sol",
      "models": [
        { "id": "gpt-5.6-sol",   "name": "GPT-5.6 Sol",   "tier": "premium",  "note": "$5/$30 per MTok"      },
        { "id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "tier": "standard", "note": "$2/$12 per MTok"      },
        { "id": "gpt-5.6-luna",  "name": "GPT-5.6 Luna",  "tier": "fast",     "note": "$0.20/$1.20 per MTok" }
      ]
    },
    {
      "id": "gemini",
      "label": "Gemini (Google)",
      "available": false,
      "envKey": "GOOGLE_API_KEY",
      "defaultModel": "gemini-3.1-pro-preview",
      "models": [
        { "id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro",        "tier": "premium"  },
        { "id": "gemini-3.5-flash",        "name": "Gemini 3.5 Flash",      "tier": "standard" },
        { "id": "gemini-3-flash-preview",  "name": "Gemini 3 Flash",        "tier": "fast"     },
        { "id": "gemini-3.1-flash-lite",   "name": "Gemini 3.1 Flash-Lite", "tier": "fast"     }
      ]
    }
  ]
}
```

- `available`: 해당 프로바이더의 API 키 환경변수가 설정된 경우 `true`
- `configuredDefault`: `provider: "auto"` 가 실제로 어디로 가는지 (`_detect_provider` 결과).
  키가 하나도 없으면 `null` 이고 이때 `/reports/draft` 는 규칙 기반 초안으로 폴백합니다.
  드롭다운에서 "자동" 옆에 실제 대상을 표시할 때 쓰세요.

---

## 11. `POST /api/v1/scenarios` · 12. `GET /api/v1/scenarios/{id}`

시뮬레이션 배치안의 **공유 링크의 실체**입니다. 프론트의 [공유 링크] 버튼이 현재
배치·예산·시간대를 저장하고, 받은 `id` 를 `simulation.html?scenario={id}` 로 조립해
클립보드에 복사합니다. 링크를 연 브라우저는 부트 때 `GET /api/v1/scenarios/{id}` 로
같은 배치안을 복원합니다. 로컬 저장(`hw.scenarios`, localStorage)과 별개 경로입니다 —
로컬 저장은 그 브라우저에만 남습니다.

저장소는 서버의 `var/scenarios/` 파일입니다(DB 불필요 — `schema_ops.sql` 의
`scenario` 테이블은 DB 확장용으로 남아 있습니다). 쓰기는 `/simulations` 와 같은
신뢰 모델(공개)이고, 입력도 같은 검증(`_validate_placements`)을 지납니다.

### 요청 (POST)

```json
{
  "name": "향남·우정 우선 배치안",
  "period": "am",
  "budgetKrw": 3000000000,
  "placements": [{ "type": "drt", "cellId": "다사6707", "count": 1 }]
}
```

| 필드 | 타입 | 기본 | 규칙 |
|---|---|---|---|
| `name` | string | `""` | 80자 절단 · 제어문자 제거 (표시는 프론트가 이스케이프) |
| `period` | string | `am` | 없는 시간대는 **400** |
| `budgetKrw` | int\|null | 관리자 기본 예산 | 음수는 **400** |
| `placements` | array | — | `/simulations` §7 과 같은 검증 — 빈 배열은 **400** (빈 시나리오는 공유 불가) |

### 응답

```json
{ "ok": true, "id": "Ab3xK9q2LmZw", "path": "/api/v1/scenarios/Ab3xK9q2LmZw", "scenario": { "...": "저장된 원문" } }
```

- `GET /api/v1/scenarios/{id}` — 저장된 원문 그대로. 형식 위반 id 는 **400**,
  없는 id 는 **404** (링크가 잘못됐거나 서버에서 정리된 경우).
- 오래된 시나리오를 조용히 지우지 않습니다 — 공유해 둔 링크가 죽으면 안 되므로,
  상한(2,000건)에 닿으면 새 저장이 **409** 로 거절되고 운영자가 정리합니다.

---

## 부록 A — 서버 실행

```bash
# 권장 — 원커멘드 진입점
pip install -r requirements.txt
python main.py                 # 0.0.0.0:8000
python main.py --port 8080     # 포트 변경
python main.py --reload        # 개발 모드
python main.py --regen         # 정적 JSON 강제 재생성 후 기동
python main.py --setup         # 정적 JSON 생성만 (서버 미기동)

# Docker
docker compose up --build

# uvicorn 직접 실행 (server/static 자동 생성을 건너뜁니다)
uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
```

`server/static/` 이 비어 있으면 `main.py` 가 `analysis/05_load.py` 를 자동으로 돌려 채웁니다.
`uvicorn` 을 직접 쓰면 그 단계가 없으니 먼저 `python main.py --setup` 을 한 번 돌리세요.

## 부록 B — 환경변수 (`.env`)

전체 목록은 [`.env.example`](../.env.example) 을 보세요. AI 관련만 옮기면 다음과 같습니다.

```bash
# AI 보고서 — 없으면 규칙 기반 초안으로 폴백합니다 (필수 아님)
ANTHROPIC_API_KEY=sk-ant-...      # Claude  (기본 claude-sonnet-5)
OPENAI_API_KEY=sk-...             # GPT     (기본 gpt-5.6-sol)
GOOGLE_API_KEY=AIza...            # Gemini  (기본 gemini-3.1-pro-preview)

# provider=auto 가 고를 프로바이더를 못박고 싶을 때 (해당 키도 있어야 적용)
AI_PROVIDER=claude
# 기본 모델 재지정 — AI_PROVIDER 와 짝으로 설정할 때만 적용됩니다
AI_MODEL=claude-sonnet-5

# PostgreSQL (선택 — 현재 미사용)
DATABASE_URL=postgresql://hw:hw_pass@db:5432/hwaseong
```

공공데이터 수집용 키(`DATA_GO_KR_KEY_*`·`TAGO_*`·`GG_BUS_ROUTE_BASE` 등)는 원본 API 를
다시 수집할 때만 필요합니다. 산출물이 커밋돼 있어 서버 기동에는 쓰이지 않습니다.

## 부록 C — 프론트엔드 연동

프론트 설정은 `assets/js/config.js` 한 곳입니다. 실제 키는 `BASE_URL` · `API_PREFIX`
(`/api/v1`) · `EXTRA_HEADERS` · `AUTH` · `TIMEOUT_MS` / `TIMEOUT_MS_REPORT` ·
`EXPORT_MODE` · `PAGES` · `APP` · `GRID` · `KAKAO` · `COST` 입니다.
서버 주소는 코드를 고치지 말고 주소 뒤에 `?server=` 를 붙여 바꾸세요(브라우저에 기억됩니다).

**프론트에는 정적 폴백이 없습니다.** 모든 요청이 실서버로 갑니다 — 아래는 `/data` 를
직접 열어볼 때의 예시일 뿐 프론트 동작이 아닙니다.

```js
// 계약 JSON 직접 열람 (디버깅용)
const grid = await fetch(`${BASE_URL}/data/grid_am.json`).then(r => r.json());

// 프론트가 실제로 쓰는 경로
const grid2 = await fetch(`${BASE_URL}/api/v1/grid?period=am`).then(r => r.json());

// 프로바이더 목록
const { providers } = await fetch(`${BASE_URL}/api/v1/providers`).then(r => r.json());

// 보고서 (모델 직접 지정)
const report = await fetch(`${BASE_URL}/api/v1/reports/draft`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    provider: "openai",
    model: "gpt-5.6-terra",
    period: "am",
    context: { kpi, priorities }
  })
}).then(r => r.json());
```
