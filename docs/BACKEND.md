# 백엔드 아키텍처 & 업무 분담

> 화성시 버스 수요·공급 미스매칭 대시보드 — 백엔드 2인 작업 기준서
> v1.0 · 2026-08-07 · **프론트 저장소 `docs/API.md` 확인 후 확정**

---

## 0. 세 줄 요약

1. **FastAPI + PostgreSQL/PostGIS.** 프론트가 요구한 엔드포인트 9개를 구현한다.
2. **배치(Python 파이프라인)와 온라인(API)을 분리**한다. 무거운 계산은 절대 요청 경로에 넣지 않는다.
3. **A = 데이터 파이프라인 / B = API 서버.** 인터페이스는 DB 스키마 하나뿐이다.

---

## 1. 왜 서버가 필요한가 (결론이 바뀐 이유)

초기 검토에서는 "정적 JSON + 서버 없음"이 결론이었다. 프론트 스펙을 확인하고 뒤집혔다.

| 트리거 | 근거 |
|---|---|
| **AI 보고서** | 프론트에 「AI 보고서 생성」 버튼이 이미 구현됨. Claude API 키를 브라우저에 넣을 수 없음 → **서버 필수** |
| **시뮬레이션** | `POST /simulations` 가 4개 시간대 전체 + baseline + delta 를 재계산해 돌려줘야 함 |
| **관리자 수정·실시간** | 확장 요구사항 |

**이 셋이 없었으면 서버는 만들지 않는 게 맞았다.** 지금은 필요하다.

---

## 2. 전체 구조

```
┌─ 배치 (오프라인, 하루 1회 또는 수동) ─────────────────┐
│  dataset/  →  Python 파이프라인 01~05  →  DB 적재      │
└───────────────────────────┬──────────────────────────┘
                            │ write (batch_*)
                            ▼
                 ┌──────────────────────┐
                 │  PostgreSQL 17       │   단일 진실 원천
                 │  + PostGIS           │
                 └──┬────────────────┬──┘
              read  │                │  read / write (admin_*)
                    ▼                ▼
┌─ 온라인 (FastAPI) ───────────────────────────────────┐
│  GET  /meta /grid /priorities /stops /routes         │
│  GET  /stops/{id}/profile                            │
│  POST /simulations          ← 서버 재계산             │
│  POST /reports/draft        ← Claude API 프록시       │
│  (확장) 관리자 CRUD · 실시간 SSE                       │
└───────────────────────────┬──────────────────────────┘
                            │ JSON
                            ▼
              프론트 (config.js 두 줄만 수정)
```

**핵심 규칙: 배치와 온라인을 섞지 않는다.**
격자 계산이 API 요청 경로에 들어가면 응답이 수십 초가 되고, 배치가 운영 테이블을 잠그면 서비스가 멎는다.

---

## 3. 기술 스택 (확정)

| 영역 | 선택 | 이유 |
|---|---|---|
| 언어 | **Python 3.14** | 파이프라인과 서버가 같은 언어 → 모델 코드 재사용 |
| API | **FastAPI** | 프론트가 스켈레톤까지 써둠. 자동 OpenAPI 문서 |
| DB | **PostgreSQL 17 + PostGIS** | 격자 폴리곤 공간질의. DuckDB는 동시 쓰기 불가라 운영 부적합 |
| ORM | **SQLAlchemy 2.0 + Alembic** | 2인 협업에서 마이그레이션 없으면 DB가 갈라짐 |
| 배치 저장 | **DuckDB** (중간 단계만) | 206개 CSV 198MB를 글롭 쿼리로 처리. 실측 265만행 5초 |
| 공간 | GeoPandas · Shapely · pyproj | 격자 생성·공간조인 |
| 모델 | scikit-learn | 회귀·군집 |
| 실시간 | **SSE** | 단방향이면 충분. 재연결이 브라우저 기본 제공 |
| 배포 | Docker Compose | api + db + worker |

### 좌표계 규칙 (어기면 하루 날아감)

| 용도 | EPSG |
|---|---|
| 거리·면적 연산, 격자 생성 | **5179** (미터 단위) |
| 저장·API 응답 | **4326** (WGS84 경위도) |

---

## 4. 데이터 흐름 4계층

```
L0  dataset/                원본. 불변. .gitignore
L1  warehouse.duckdb        stg_*  적재 (배치 전용, 중간 산출)
L2  PostgreSQL              batch_* / admin_*  운영 DB
L3  FastAPI 응답            프론트 계약 형태로 변환
```

L1은 **배치 담당(A)만** 사용한다. L2부터가 두 사람의 공유 지점이다.

---

## 5. DB 스키마 — 소유권 분리

> 상세 DDL: [`server/schema_ops.sql`](server/schema_ops.sql)

| 접두사 | 소유자 | 규칙 |
|---|---|---|
| `batch_*` | 파이프라인(A) | 매 실행마다 REPLACE. **사람이 수정 금지** |
| `admin_*` | 서버(B) | append-only. **배치가 조회조차 안 함** |
| `v_*` | — | 둘을 합친 뷰. **API는 이것만 읽음** |

### 왜 이렇게 나누는가

관리자 수정 기능을 넣을 때 가장 흔한 사고:

```
관리자가 화요일에 수정  →  수요일 배치 재실행  →  수정 소멸. 아무도 모름.
```

`v_grid_metrics` 뷰가 `COALESCE(override, batch)` 로 합치므로, 배치를 몇 번 돌려도 관리자 수정이 살아남는다. 되돌리기도 DELETE 가 아니라 `revoked_at` 을 채워 **이력을 남긴다.**

`admin_grid_override.reason` 은 NOT NULL 이다. "왜 모델 값을 사람이 고쳤는가"는 심사에서 반드시 나오는 질문이고, 그 답이 DB에 있으면 강력하다.

---

## 6. 구현할 엔드포인트

프론트 `docs/API.md` 기준. 응답 형태는 그 문서와 `assets/js/mock.js` 가 규격이다.

| # | 경로 | 우선순위 | 담당 | 비고 |
|---|---|---|---|---|
| 1 | `GET /meta` | **필수** | B | 시간대·지도경계·단가·산식 |
| 2 | `GET /grid?period=` | **최우선** | B | 지도·산점도·표의 원천 |
| 3 | `GET /priorities?period=&limit=` | **필수** | B | `reason` 은 사람이 읽을 문장 |
| 4 | `GET /stops` | 필수 | B | |
| 5 | `GET /routes` | 필수 | B | |
| 6 | `GET /stops/{id}/profile` | 필수 | B | ⚠️ 원본에 시간대 없음 (§8) |
| 7 | `POST /simulations` | **필수** | B | 4시간대 전부 + baseline + delta |
| 8 | `POST /reports/draft` | **필수** | B | Claude API 프록시 |
| 9 | `POST /reports/export` | 선택 | — | 미구현 시 프론트가 브라우저에서 생성 |

### 연동 방법 (프론트가 정해둠)

```js
// assets/js/config.js — 두 줄만 바꾸면 끝
BASE_URL : ''    →  'http://localhost:8000'
USE_MOCK : true  →  false

// 점진 연동도 가능
ENDPOINT_OVERRIDES: { 'grid.list': false }   // 이 경로만 실서버
```

### CORS

```python
app.add_middleware(CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500"],
    allow_methods=["GET", "POST"], allow_headers=["*"])
```

---

## 7. 업무 분담

### 축을 "데이터 vs 서비스" 로 자른다

파이프라인 단계로 자르면(01·02 / 03·04·05) 뒷사람이 계속 기다린다.
데이터와 서비스로 자르면 **둘 다 Day 1부터 병렬**로 간다.

| | **담당 A — 데이터 파이프라인** | **담당 B — API 서버** |
|---|---|---|
| 결과물 | `batch_*` 테이블 | 엔드포인트 9개 |
| 핵심 난관 | 격자 공간조인 | 시뮬레이션 재계산 |
| 언어/도구 | pandas · DuckDB · GeoPandas | FastAPI · SQLAlchemy |

### 담당 A — 데이터 파이프라인

| # | 산출물 | 내용 |
|---|---|---|
| A1 | `01_fetch.py` | TAGO 정류소·노선, 경기도 배차, 똑버스 경유정류소 수집 → CSV |
| A2 | `02_grid.py` ★ | **격자 shp ↔ 화성시 경계 공간조인** → 850격자 확정 |
| A3 | `03_join.py` | ARS번호 매칭(실측 99.5%), 승하차 격자 배분, 철도역 거리 |
| A4 | `04_model.py` | 수요 D · 공급 S · MI · 4분면 · 우선순위 · 회귀계수 |
| A5 | `05_load.py` | `batch_grid` / `batch_grid_metrics` 적재 + `batch_run` 기록 |
| A6 | 검증 | 예측 vs 실측 승하차 상관 (목표 R² ≥ 0.6) |

**A2가 프로젝트 전체 최대 난관.** 격자코드로 화성시를 못 고르므로(코드집에 시도 매핑만 존재) shp 공간조인이 유일한 방법이고, 좌표계 실수가 여기서 난다. **막히면 일정 전체를 다시 짜야 하는 유일한 지점.**

### 담당 B — API 서버

| # | 산출물 | 내용 |
|---|---|---|
| B1 | 프로젝트 셋업 | FastAPI · Docker Compose · Alembic 초기 마이그레이션 |
| B2 | `GET /meta` `/grid` `/priorities` | `v_grid_metrics` → 프론트 `cells[]` 변환 계층 |
| B3 | `GET /stops` `/routes` `/stops/{id}/profile` | |
| B4 | `POST /simulations` ★ | 배치 효과 적용 후 **4시간대 재계산**. 정규화 기준통계 고정 |
| B5 | `POST /reports/draft` | Claude API 프록시 (`docs/AI-REPORT.md` 규격) |
| B6 | (확장) 관리자·실시간 | `admin_*` CRUD · 인증 · SSE 워커 |

**B4가 B쪽 최대 난관.** 프론트가 배치를 바꿀 때마다 호출하므로 응답이 빨라야 하고, `baseline` 은 항상 동일해야 한다.

### 인터페이스 — 이것만 지키면 충돌 없음

| 방향 | 계약 | 마감 |
|---|---|---|
| A → B | `batch_grid` (grid_id, geom, lon, lat, 속성) | **Day 5** |
| A → B | `batch_grid_metrics` (grid_id, period, d/s/mi/quad/priority) | Day 12 |
| A → B | `mart_norm_stats` (정규화 기준통계) | Day 12 |
| B → A | 스키마 변경 요청은 **Alembic 마이그레이션으로만** | 상시 |

**A는 실데이터 전에 더미로 `batch_grid` 를 먼저 채운다(Day 2).** 그래야 B가 Day 3부터 실제 쿼리를 짤 수 있다.

---

## 8. ⚠️ 실측으로 확인된 제약 4가지

프론트 스펙과 어긋나므로 **프론트 담당과 협의 필요.**

### ① 격자 250m → 1km

프론트 `meta.grid.sizeMeters: 250`, `cellCount: 389`.
실측: SGIS 공공데이터포털 배포판은 **1km 격자만** 제공(파일명 전부 `_1K`). 화성시 실제 격자 **약 850개**.

→ `sizeMeters: 1000`, `cellCount: 850` 으로 수정 요청.

### ② `stops.profile` 시간대별 승하차 불가 🔴

승하차 원본 컬럼 실측:
```
승하차일자 | 관할관청 | 정류소ID | 정류소번호 | 정류소명 | 승차합계 | 초승 | 환승 | 하차
```
**일자별 집계라 시간대가 없다.** 24시간 프로파일을 만들 수 없다.

→ **유동인구 CSV의 시간배율로 일 총량을 안분**하고, 응답에 `"isEstimated": true` 를 넣어 추정임을 표시한다.

### ③ 시간 축이 유동인구 하나에 의존

승하차에 시간대가 없으므로 D 와 P 가 **같은 시간배율**을 쓰게 된다.
→ 그러면 MI 의 시간대 변화가 배율에만 좌우된다.
→ **공급 쪽에 시간대별 배차(`peekAlloc`/`nPeekAlloc`/`nightAlloc`)를 넣어** 시간대별 MI 변화가 공급 변화에서 나오게 설계한다. (경기도 API 에서 취득 확인됨)

### ④ 사업비 단가가 근거 없음

`cost: { stop: 42000000, drt: 180000000, freq: 95000000 }` 는 가정값.
→ 화성시 예산서·유사 사업 실적으로 보정하거나, **화면에 가정값임을 명시.**

---

## 9. 일정 (4주)

| 주차 | 담당 A | 담당 B |
|---|---|---|
| **1주** | A1 수집 · **A2 격자 ★** · 더미 `batch_grid` | B1 셋업 · Alembic · `/meta` `/grid` 스켈레톤 |
| **2주** | A3 정류소·승하차 배분 · 철도역 | B2 B3 조회 API 완성 · 프론트 1차 연동 |
| **3주** | A4 모델 · A5 적재 | **B4 시뮬레이션 ★** · B5 AI 보고서 |
| **4주** | A6 검증 (R², 민원 대조) | B6 확장 · 배포 · 데모 리허설 |

**크리티컬 패스: A2 → A5 → B4.** A2가 밀리면 전부 밀린다.

---

## 10. 데모 안전장치 (필수)

발표장에서 DB·네트워크가 죽으면 서버 구조는 통째로 멎는다.

- `05_load.py` 는 DB 적재와 **동시에 정적 JSON 도 떨군다.**
- 프론트에 폴백을 넣는다.

```js
const grid = await fetch("/api/v1/grid?period=am").then(r => r.json())
  .catch(() => fetch("/data/grid.json").then(r => r.json()));
```

한 줄이면 발표가 안전해진다.

---

## 11. 착수 체크리스트

```bash
# 공통
pip install duckdb geopandas shapely pyproj scikit-learn fastapi uvicorn \
            sqlalchemy alembic psycopg2-binary python-dotenv requests

# A: 배치 스키마
duckdb warehouse.duckdb < analysis/schema.sql

# B: 운영 DB
docker compose up -d db
psql -f server/schema_ops.sql
```

| 확인 | 상태 |
|---|---|
| 공공데이터포털 API 키 3건 | ✅ 검증 완료 (정류소 3,366 · 노선 146) |
| 화성시 도시코드 31240 | ✅ 확인 |
| 정류소 ARS 매칭률 | ✅ 99.5% |
| 배차간격 취득 | ✅ `peek/nPeek/nightAlloc` |
| 똑버스 5개 노선 | ✅ API 조회 가능 |
| 원본 데이터 | ✅ `dataset/` 수집 완료 |
