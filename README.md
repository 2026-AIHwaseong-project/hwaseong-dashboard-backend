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
| 4 | 프론트 `docs/API.md` | 구현할 엔드포인트 9개 명세 |

---

## 저장소 구조

```
├── docs/
│   ├── BACKEND.md              아키텍처 · 업무 분담  ★ 여기부터
│   └── 기획서.md                문제 정의 · 모델 수식
├── analysis/                   데이터 파이프라인 (담당 A)
│   ├── schema.sql              DuckDB 스키마 (배치 중간 저장)
│   ├── 00_extract_hwaseong.py  전국 원본 → 화성시 추출
│   └── check_api.py            공공데이터 API 키 검증
├── server/                     API 서버 (담당 B)
│   └── schema_ops.sql          PostgreSQL 운영 스키마
├── dataset_hwaseong/           화성시 추출 데이터 (24.8MB, 커밋됨)
├── prototypes/                 목업 프로토타입 (모델 로직 참고용)
└── .env.example                환경변수 템플릿
```

---

## 시작하기

```bash
git clone https://github.com/2026-AIHwaseong-project/hwaseong-dashboard-backend.git
cd hwaseong-dashboard-backend

pip install pandas duckdb requests python-dotenv openpyxl
# 담당 A 추가:  pip install geopandas shapely pyproj scikit-learn
# 담당 B 추가:  pip install fastapi uvicorn sqlalchemy alembic psycopg2-binary

cp .env.example .env      # 발급받은 공공데이터포털 키를 넣으세요
python analysis/check_api.py
```

`check_api.py` 가 전부 PASS 면 준비 완료입니다.

---

## 업무 분담 요약

상세는 [docs/BACKEND.md §7](docs/BACKEND.md) 참조.

| | **담당 A — 데이터 파이프라인** | **담당 B — API 서버** |
|---|---|---|
| 결과물 | `batch_*` 테이블 | 엔드포인트 9개 |
| 최대 난관 | `02_grid.py` 격자 공간조인 | `POST /simulations` 재계산 |
| 도구 | pandas · DuckDB · GeoPandas | FastAPI · SQLAlchemy |

**인터페이스는 DB 스키마 하나뿐입니다.** A는 `batch_*` 에 쓰고, B는 `v_*` 뷰를 읽습니다.
서로의 테이블을 건드리지 않으면 충돌이 없습니다.

**A는 Day 2에 더미로 `batch_grid` 를 먼저 채워** B를 출발시켜 주세요.

---

## 현재 상태

| 항목 | 상태 |
|---|---|
| 공공데이터포털 API 3건 | ✅ 검증 완료 (정류소 3,366 · 노선 146) |
| 화성시 도시코드 31240 | ✅ 확인 |
| 정류소 ARS 매칭률 | ✅ 99.5% |
| 배차간격 취득 | ✅ `peek/nPeek/nightAlloc` |
| 똑버스 5개 노선 | ✅ API 조회 가능 |
| 원본 데이터 수집 | ✅ 완료 |
| 파이프라인 구현 | ⬜ 착수 전 |
| API 서버 | ⬜ 착수 전 |

---

## 주의

- **`.env` 는 절대 커밋하지 마세요.** API 키가 들어갑니다 (`.gitignore` 등록됨).
- 전국 원본(`dataset/`, 372MB)은 커밋하지 않습니다. 화성시 추출본만 올립니다.
