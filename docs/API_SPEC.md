# 화성시 버스 대시보드 API 명세서

> **Base URL** `http://localhost:8000`  
> **버전** v1 · **업데이트** 2026-08-09  
> **서버** FastAPI · `uvicorn server.main:app --host 0.0.0.0 --port 8000`  
> **Docker** `docker-compose up --build`

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

---

## 공통 규칙

### 헤더
```
Content-Type: application/json
Accept: application/json
```

### CORS
모든 오리진 허용 (`allow_origins=["*"]`). 별도 인증 없음.

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
| 400 | 잘못된 파라미터 (period, strategy, provider 등) |
| 404 | 리소스 없음 (stop profile) |
| 500 | 서버 오류 / AI API 오류 |

---

## 1. `GET /api/v1/meta`

화면 구성에 필요한 고정 메타 정보. 서버 시작 시 1회 로드, 이후 불변.

### 응답

```json
{
  "region": "화성시",
  "updatedAt": "2026-08-09",
  "isMockData": false,
  "periods": [
    { "id": "am",    "name": "출근", "label": "07–09", "hours": [7, 9]   },
    { "id": "day",   "name": "낮",   "label": "09–17", "hours": [9, 17]  },
    { "id": "pm",    "name": "퇴근", "label": "17–19", "hours": [17, 19] },
    { "id": "night", "name": "심야", "label": "22–24", "hours": [22, 24] }
  ],
  "grid": {
    "sizeMeters": 1000,
    "cellCount": 786,
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
    { "type": "drt",  "label": "똑버스 배치", "icon": "◆", "radiusKm": 3.0, "unitKrw": 180000000 },
    { "type": "freq", "label": "배차 증편",   "icon": "▲", "radiusKm": 2.4, "unitKrw": 95000000  }
  ]
}
```

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
    "miThresholds": [-1.2, -0.7, -0.25, 0.25, 0.7, 1.2],
    "tripCoef": 3200
  },
  "kpi": {
    "needCells": 38,
    "drtCells": 63,
    "overCells": 28,
    "totalCells": 786,
    "needShare": 12.8,
    "potentialTripsPerDay": 1044504,
    "elderlyTripsPerDay": 234919,
    "avgMi": 0.0
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
| `flowTripsPerDay` | integer | 추정 일 통행량 (`round(nf × 3200)`) |
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
| `limit` | integer | `10` | 반환 최대 건수 |

### 응답

```json
{
  "period": "am",
  "items": [
    {
      "rank": 1,
      "cellId": "다사3921",
      "name": "새솔동",
      "mi": 1.02,
      "priorityScore": 1.1398,
      "demand": 72,
      "supply": 44,
      "flowTripsPerDay": 2558,
      "elderlyRatio": 0.127,
      "coverage": 0.361,
      "action": "ADD_FREQ",
      "actionLabel": "증차",
      "nearestStopId": "41590-55226",
      "reason": "수요지수 72 대비 공급지수 44, 정류장 도보권 내이나 배차 부족"
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
      "routes": ["GGB233000067"]
    }
  ]
}
```

### 정류장 `kind` 분류

| 값 | 기준 |
|---|---|
| `hub` | 경유 노선 수 ≥ 5 |
| `rural` | 소재 읍면동이 `면` |
| `res` | 그 외 |

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
  "stopId": "41590-37539",
  "stopName": "양지말입구",
  "kind": "rural",
  "routes": ["GGB233000067"],
  "isEstimated": true,
  "estimationMethod": "일자별 승하차를 통신 유동인구 시간배율로 안분",
  "hours": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
  "boardings":  [0, 2, 18, 31, 12, 8, 6, 9, 7, 6, 8, 11, 29, 22, 10, 7, 5, 4, 2],
  "alightings": [0, 1, 12, 21, 9, 6, 5, 7, 5, 5, 6,  9, 20, 17,  8, 5, 4, 3, 2],
  "summary": {
    "boardingsPerDay": 226,
    "alightingsPerDay": 88,
    "peakSharePct": 41.2
  }
}
```

- `hours`: 5시–23시 (19개 구간)
- `peakSharePct`: (07·08·17·18시 합산 / 전체) × 100
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
| `budgetKrw` | integer | `3000000000` | 예산 (원) |
| `placements` | array | `[]` | 배치 목록 |

#### `placements` 항목

| 필드 | 타입 | 설명 |
|---|---|---|
| `type` | string | `stop` / `drt` / `freq` |
| `cellId` | string | 격자 ID |
| `count` | integer | 수량 (기본 1) |

#### 수단별 물리 파라미터

| 수단 | 반경 | 주입 공급량 | 비용 (연환산) |
|---|---|---|---|
| `stop` (신설) | 800m 도보, 2km 커버리지 | 출퇴근 4.8회/창, 낮 8회, 심야 0 | 42,000,000원 |
| `drt` (똑버스) | 3,000m | φ = 2.4회/창 (낮 9.6) | 180,000,000원 |
| `freq` (증편) | 2,200m | headway × 0.7 (운행 ×1.43) | 95,000,000원 |

### 응답

```json
{
  "id": "SIM-1234567",
  "name": "시나리오 1",
  "createdAt": "2026-08-09 14:30",
  "placements": [
    {
      "type": "stop", "typeLabel": "정류장 신설",
      "cellId": "다사3921", "cellName": "새솔동",
      "count": 1, "radiusKm": 2.0, "unitKrw": 42000000
    }
  ],
  "cost": {
    "totalKrw": 137000000,
    "breakdown": [
      { "type": "stop", "label": "정류장 신설", "cellId": "다사3921", "unitKrw": 42000000, "count": 1, "amountKrw": 42000000 },
      { "type": "freq", "label": "배차 증편",   "cellId": "다사6311", "unitKrw": 95000000, "count": 1, "amountKrw": 95000000 }
    ]
  },
  "budgetKrw": 3000000000,
  "periods": [
    {
      "period": "am", "periodName": "출근",
      "kpi": {
        "needCells": 31, "drtCells": 63, "overCells": 28, "totalCells": 786,
        "needShare": 10.4, "potentialTripsPerDay": 1044504, "elderlyTripsPerDay": 234919, "avgMi": -0.003
      },
      "baseline": {
        "needCells": 38, "drtCells": 63, "overCells": 28, "totalCells": 786,
        "needShare": 12.8, "potentialTripsPerDay": 1044504, "elderlyTripsPerDay": 234919, "avgMi": 0.0
      },
      "delta": {
        "needCells": -7, "drtCells": 0, "overCells": 0,
        "needShare": -2.4, "avgMi": -0.003
      }
    }
    /* + day, pm, night */
  ],
  "effectiveness": {
    "resolvedNeedCells": 28,
    "resolvedTripsPerDay": 6120,
    "krwPerTripPerDay": 22386
  },
  "cellsByPeriod": {
    "am":    [ /* /grid 의 cells 와 동일 형식, adjusted=true 포함 */ ],
    "day":   [ /* ... */ ],
    "pm":    [ /* ... */ ],
    "night": [ /* ... */ ]
  }
}
```

- `resolvedTripsPerDay`: Poisson 회귀 기반 ΔB̂ (예측 승차 증가량)
- `krwPerTripPerDay`: `totalKrw ÷ resolvedTripsPerDay`
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
  "includeAlternatives": false
}
```

| 필드 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `strategy` | string | `"efficiency"` | 추천 전략 (아래 참고) |
| `period` | string | `"am"` | 유효성 검증용 시간대 |
| `budgetKrw` | integer | `3000000000` | 예산 상한 (원) |
| `maxPlacements` | integer | `10` | 최대 배치 건수 |
| `allowedTypes` | array | `["stop","drt","freq"]` | 허용 수단 |
| `region` | string\|null | `null` | 특정 읍면동으로 범위 제한 (예: `"새솔동"`) |
| `includeAlternatives` | boolean | `false` | 다른 전략 요약 병렬 반환 여부 |

#### 추천 전략

| `strategy` | 설명 | 특이사항 |
|---|---|---|
| `efficiency` | 사업비 1원당 ΔB̂(예측 승차 증가) 최대화 | 기본값 |
| `equity` | 고령 통행량 기준 효과 최대화 | need·drt 격자의 `eldw` 가중 ΔBhat |
| `balance` | 읍면동당 최대 1개 원칙 | 지역 균형 분배 |
| `quick` | `stop` 수단만 허용 | 시설 투자 없이 빠른 효과 |

#### 그리디 알고리즘 요약

1. 후보: am 기준 `need` + `drt` 격자
2. 매 회차: 현재 상태에서 (수단, 격자) 조합의 ΔBhat / 비용 계산
3. 최고 효율 선택 → 상태 갱신 → 반복
4. 종료 조건: 예산 소진 / maxPlacements 달성 / 추가 이득 없음

### 응답

```json
{
  "strategy": "efficiency",
  "strategyLabel": "효율 최우선",
  "note": "사업비 1원당 해소 통행량이 가장 큰 순서로 고릅니다.",
  "budgetKrw": 3000000000,
  "usedKrw": 515000000,
  "remainingKrw": 2485000000,
  "placements": [
    {
      "rank": 1,
      "type": "stop", "typeLabel": "정류장 신설",
      "cellId": "다사5913", "cellName": "진안동",
      "region": "진안동",
      "count": 1,
      "radiusKm": 2.0,
      "costKrw": 42000000,
      "expectedResolvedTrips": 284
    }
  ],
  "simulation": { /* POST /simulations 응답과 동일한 구조 */ },
  "alternatives": [
    {
      "strategy": "equity",
      "label": "교통약자 우선",
      "count": 8,
      "totalKrw": 476000000,
      "mix": { "stop": 5, "drt": 2, "freq": 1 }
    }
  ]
}
```

- `alternatives`: `includeAlternatives: true` 일 때만 포함
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
    "kpi": { "needCells": 38, "totalCells": 786, "needShare": 12.8, "potentialTripsPerDay": 1044504, "avgMi": 0.0 },
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
`ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `GOOGLE_API_KEY` 순서로 환경변수 확인

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
  "generatedAt": "2026-08-09 14:30",
  "provider": "Claude (Anthropic)",
  "model": "claude-sonnet-5",
  "sections": [
    {
      "key": "summary",
      "heading": "1. 검토 개요",
      "body": "화성시 786개 격자 분석 결과 출근 시간대 기준 고수요·저공급 격자 38개(12.8%)가 확인됨.",
      "bullets": ["need 격자 38개 중 대부분이 동탄·봉담 신개발지 집중", "..."]
    }
  ],
  "tables": [
    {
      "key": "priority",
      "title": "노선 조정 우선순위 (상위 5개 격자)",
      "columns": ["순위", "격자", "권역", "수요", "공급", "MI", "조치"],
      "rows": [[1, "다사3921", "새솔동", 72, 44, 1.02, "증차"]]
    }
  ],
  "disclaimer": "본 문서는 AI가 자동 생성한 초안입니다. 담당자 검토 후 활용하시기 바랍니다."
}
```

---

## 10. `GET /api/v1/providers`

환경변수 기반으로 사용 가능한 AI 프로바이더와 모델 목록을 반환합니다. 프론트엔드 드롭다운 구성에 활용하세요.

### 응답

```json
{
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

---

## 부록 A — 서버 실행

```bash
# 직접 실행
pip install -r requirements.txt
uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

# Docker
docker-compose up --build

# 정적 JSON 사전 생성 (서버 없이 /data/ 폴백 활용 시)
python analysis/05_load.py   # server/static/ 생성
python analysis/06_load.py   # data/ 생성
```

## 부록 B — 환경변수 (`.env`)

```bash
# AI 보고서 — 하나 이상 필요
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...

# PostgreSQL (선택)
DATABASE_URL=postgresql://hw:hw_pass@localhost:5432/hwaseong
```

## 부록 C — 프론트엔드 연동

```js
// config.js
const CONFIG = {
  BASE_URL: "http://localhost:8000",
  USE_MOCK: false,
};

// 폴백 패턴 (data/ 정적 파일)
const grid = await fetch(`${BASE_URL}/api/v1/grid?period=am`)
  .catch(() => fetch(`${BASE_URL}/data/grid_am.json`))
  .then(r => r.json());

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
