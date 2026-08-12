# 화성시 버스 수요·공급 미스매칭 대시보드 — 백엔드

> 26년 여름학기 AI화성챌린지 대학생 솔루션데이 · 과제 23번(교통분야)
> 프론트엔드: [hwaseong-dashboard](https://github.com/2026-AIHwaseong-project/hwaseong-dashboard)

화성시를 격자로 나눠 시간대별로 **버스 수요와 공급의 격차(MI)** 를 산출하고,
정류장·똑버스·증편을 어디에 넣을지 시뮬레이션하는 의사결정 도구의 백엔드입니다.

---

## 처음 오셨다면 이 순서로

| 순서 | 문서 | 내용 |
|---|---|---|
| 1 | **[docs/BACKEND.md](docs/BACKEND.md)** | **아키텍처 · 업무 분담 · 일정.** 먼저 읽으세요 |
| 2 | [docs/기획서.md](docs/기획서.md) | 문제 정의 · 분석 모델 수식 · 데이터 출처 |
| 3 | [dataset_hwaseong/README.md](dataset_hwaseong/README.md) | 데이터 목록 · 원본 출처 · 알려진 제약 |
| 4 | 프론트 `docs/API.md` | 구현할 엔드포인트 10개 명세 |

---

## 저장소 구조

```
├── docs/
│   ├── BACKEND.md              아키텍처 · 업무 분담  ★ 여기부터
│   └── 기획서.md                문제 정의 · 모델 수식
├── analysis/                   데이터 파이프라인 (담당 A)
│   ├── schema.sql              DuckDB 스키마 (배치 중간 저장)
│   ├── 00_extract_hwaseong.py  전국 원본 → 화성시 추출
│   ├── 01_fetch.py             노선·배차·경유정류소 수집     ✅ 완료
│   ├── 02_grid.py              SGIS 격자 ↔ 화성시 공간조인  ✅ 완료
│   ├── 03_join.py              정류장·승하차·노선 → 격자      ✅ 완료
│   ├── 04_model.py             D·S·MI·4분면·우선순위         ✅ 완료
│   ├── 05_simulate.py          배치 시뮬레이션·그리디 추천    ✅ 완료
│   ├── 05_load.py              프론트 계약 JSON 산출         ✅ 완료
│   ├── 07_validate.py          회귀·공간CV·정성 검증          ✅ 완료
│   └── check_api.py            공공데이터 API 키 검증
├── server/                     API 서버 (담당 B)
│   ├── main.py                 FastAPI 10개 엔드포인트       ✅ 완료
│   ├── static/                 ★ 계약 JSON (05_load.py 산출)
│   └── schema_ops.sql          PostgreSQL 운영 스키마
├── dataset_hwaseong/           화성시 데이터 + 파이프라인 산출물 (26.3MB)
└── .env.example                환경변수 템플릿
```

---

## 시작하기

```bash
git clone https://github.com/2026-AIHwaseong-project/hwaseong-dashboard-backend.git
cd hwaseong-dashboard-backend

pip install pandas duckdb requests python-dotenv openpyxl
# 담당 A 추가:  pip install pyshp shapely pyproj scikit-learn
# 담당 B 추가:  pip install fastapi uvicorn sqlalchemy alembic psycopg2-binary

cp .env.example .env      # 발급받은 공공데이터포털 키를 넣으세요
python analysis/check_api.py
```

`check_api.py` 가 전부 PASS 면 준비 완료입니다.

### 클론하면 데이터가 다 들어옵니다 (원본은 안 들어옵니다)

**분석에 쓰는 데이터는 전부 커밋돼 있습니다.** 팀원이 클론하면 전원이 바이트 단위로
같은 데이터를 씁니다 — 체크섬 대조로 확인했습니다.

| 클론 직후 | 상태 |
|---|---|
| `grid_hwaseong.csv` 786격자 | ✅ 바로 사용 |
| `boarding_hwaseong.csv` 301,455행 | ✅ 바로 사용 |
| `stops_national_hwaseong.csv` 3,158행 | ✅ 바로 사용 |
| `flow_hourly.csv` · 경계 · 나머지 8개 | ✅ 바로 사용 |
| **전국 원본** `dataset/` 372MB | ❌ `.gitignore` |
| **`.env`** API 키 | ❌ `.gitignore` — 각자 발급 |

**`03_join.py` 이후 작업은 클론만으로 전부 됩니다.** 원본이 필요한 건 두 경우뿐입니다.

| 원본이 필요한 때 | 받을 것 |
|---|---|
| `00_extract_hwaseong.py` 재실행 | 승하차 tar 4개 · 국토부 정류장 · 화성시 CSV 6개 |
| `02_grid.py` 재실행 (격자 로직 변경 시) | [SGIS 격자 15141768](https://www.data.go.kr/data/15141768/fileData.do) 279MB |

둘 다 **결과물이 이미 커밋돼 있어 평소에는 받을 필요가 없습니다.**
원본 없이 실행하면 스크립트가 무엇을 받아야 하는지 알려주고 멈춥니다.

---

## 업무 분담 요약

상세는 [docs/BACKEND.md §7](docs/BACKEND.md) 참조.

| | **담당 A — 데이터 파이프라인** | **담당 B — API 서버** |
|---|---|---|
| 결과물 | `batch_*` 테이블 | 엔드포인트 10개 |
| 최대 난관 | `02_grid.py` 격자 공간조인 | `POST /simulations` · `/recommendations` 재계산 |
| 도구 | pandas · DuckDB · pyshp/shapely | FastAPI · SQLAlchemy |

**인터페이스는 DB 스키마 하나뿐입니다.** A는 `batch_*` 에 쓰고, B는 `v_*` 뷰를 읽습니다.
서로의 테이블을 건드리지 않으면 충돌이 없습니다.

**A는 Day 2에 더미로 `batch_grid` 를 먼저 채워** B를 출발시켜 주세요.

---

## 현재 상태

| 항목 | 상태 |
|---|---|
| 공공데이터포털 API 3건 | ✅ 검증 완료 (정류소 3,366 · 노선 146) |
| 화성시 도시코드 31240 | ✅ 확인 |
| 정류소 매칭률 | ✅ 98.9% (정류장번호 직결) |
| 배차간격 취득 | ✅ `peek/nPeek/nightAlloc` |
| 똑버스·콜버스 | ✅ **9개** 확인 (01/03/04/05/06/06-1/06-2/06-3 + 광역콜버스) |
| 원본 데이터 수집 | ✅ 완료 |
| 통신사 유동인구 | ❌ SKT 제공 불가 회신 — 대체 경로로 진행 |
| **`02_grid.py` 격자 공간조인** | ✅ **완료 — 786격자.** 총인구 102.7만(실제 약 100만) |
| **`01_fetch.py` API 수집** | ✅ **완료.** 노선 146 · 배차 취득률 **100%** · 경유구간 11,969 |
| **`03_join.py` 격자 조인** | ✅ **완료.** 승차 배분 100% 보존 · 심야 운행 출근의 9% |
| **`04_model.py` 모델 산출** | ✅ **완료.** D·S·MI·4분면·우선순위 — need **28/28/30/42** (am/day/pm/night) · 야간 무공급 구제 포함 |
| **`05_simulate.py` 시뮬레이션** | ✅ 완료 (팀원) · 절대 가드 동기화 |
| **`05_load.py` JSON 산출** | ✅ **완료.** `server/static/` 13파일 · DB 없이 동작 · `/data` 로도 서빙 |
| **검증 (`07_validate.py`)** | ✅ **완료.** 홀드아웃 로그 R² **0.842** (목표 0.6) · 배차 탄력성 +0.608 |
| **API 서버 (`server/main.py`)** | ✅ **완료.** FastAPI 10개 엔드포인트 · 시뮬/추천/보고서 |
| PostgreSQL | ⬜ **미사용.** 정적 JSON 으로 동작 — 이유는 [BACKEND.md §0-1](docs/BACKEND.md) |

**최대 난관이던 격자 공간조인이 뚫렸습니다.** 검증 근거는
[dataset_hwaseong/README.md](dataset_hwaseong/README.md) 참조.

---

## 주의

- **`.env` 는 절대 커밋하지 마세요.** API 키가 들어갑니다 (`.gitignore` 등록됨).
- 전국 원본(`dataset/`, 372MB)은 커밋하지 않습니다. 화성시 추출본만 올립니다.
