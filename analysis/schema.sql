-- ============================================================
-- 화성시 버스 수요·공급 미스매칭 — DuckDB 스키마
-- 백엔드 2인의 계약서. 이 파일이 유일한 인터페이스 정의입니다.
--
-- 실행:  duckdb warehouse.duckdb < analysis/schema.sql
-- ============================================================

INSTALL encodings; LOAD encodings;   -- 공공데이터 CSV가 cp949라 필수
INSTALL spatial;   LOAD spatial;     -- 격자 공간연산

-- ============================================================
-- L1  staging — 원본 적재. 가공하지 않음.
-- ============================================================

-- [A담당] SGIS 격자 통계 (long format: 격자코드/통계항목/통계값)
CREATE TABLE IF NOT EXISTS stg_grid_stat (
  base_year   INTEGER,
  grid_id     VARCHAR,      -- 예: '다마1234'  (1km 격자)
  item_code   VARCHAR,      -- 예: 'in_age_001', 'cp_bem_009'
  value       DOUBLE,
  domain      VARCHAR       -- 'pop' | 'household' | 'house' | 'worker' | 'biz'
);

-- [A담당] 유동인구 (시간배율의 유일한 출처)
CREATE TABLE IF NOT EXISTS stg_flow_hourly (
  sigungu_cd  VARCHAR,
  hour_cd     INTEGER,      -- 0~23
  is_foreign  BOOLEAN,
  age_sex     VARCHAR,      -- '남자2024' 등
  value       DOUBLE,
  ymd         DATE
);

-- [B담당] 승하차 — ⚠️ 시간대 없음. 일자별 집계임.
CREATE TABLE IF NOT EXISTS stg_boarding (
  ymd          DATE,
  admin_name   VARCHAR,     -- '화성시'
  stop_id      VARCHAR,     -- 승하차쪽 정류소ID (마스터와 79%만 일치)
  ars_no       INTEGER,     -- ★ 이게 매칭 키 (국토부와 99.5%)
  stop_name    VARCHAR,
  board_total  INTEGER,
  board_first  INTEGER,
  transfer     INTEGER,
  alight       INTEGER
);

-- [B담당] TAGO 정류소
CREATE TABLE IF NOT EXISTS stg_stop_tago (
  node_id   VARCHAR,        -- 'GGB233003084'
  node_no   INTEGER,        -- ARS번호
  node_name VARCHAR,
  lon       DOUBLE,
  lat       DOUBLE
);

-- [B담당] 노선 + 배차 (TAGO 노선 + 경기도 배차 병합)
CREATE TABLE IF NOT EXISTS stg_route (
  route_id      VARCHAR,
  route_name    VARCHAR,
  route_type    VARCHAR,    -- '일반버스' | '직행좌석버스' | 마을 등
  peek_alloc    INTEGER,    -- 첨두 배차(분)   → period 0,2
  npeek_alloc   INTEGER,    -- 비첨두 배차(분) → period 1
  night_alloc   INTEGER,    -- 심야 배차(분)   → period 3
  up_first      VARCHAR,
  up_last       VARCHAR,
  is_drt        BOOLEAN     -- 똑버스/콜버스 여부
);

-- [B담당] 노선별 경유 정류소
CREATE TABLE IF NOT EXISTS stg_route_stop (
  route_id VARCHAR,
  ars_no   INTEGER,
  seq      INTEGER
);

-- [A담당] 철도역 (동탄역은 TAGO 실측 좌표로 수동 추가)
CREATE TABLE IF NOT EXISTS stg_rail (
  name VARCHAR, line VARCHAR, lat DOUBLE, lon DOUBLE
);


-- ============================================================
-- L2  dimension — 정제된 기준 테이블
--     ★ 두 사람의 인터페이스는 여기서만 만납니다.
-- ============================================================

-- [A가 생성 → B가 읽음]  모든 공간연산은 A가 소유
CREATE TABLE IF NOT EXISTS dim_grid (
  grid_id        VARCHAR PRIMARY KEY,
  geom           GEOMETRY,      -- EPSG:4326 폴리곤
  lon            DOUBLE,        -- 중심점
  lat            DOUBLE,
  region_name    VARCHAR,       -- 읍면동
  pop            DOUBLE,
  elderly        DOUBLE,
  elderly_ratio  DOUBLE,
  households     DOUBLE,
  workers        DOUBLE,        -- 종사자 = 산단 출근수요의 핵심
  nearest_rail_m DOUBLE
);

-- [B가 좌표까지 생성 → A가 grid_id 채움]
CREATE TABLE IF NOT EXISTS dim_stop (
  ars_no      INTEGER PRIMARY KEY,
  stop_name   VARCHAR,
  lon         DOUBLE,
  lat         DOUBLE,
  grid_id     VARCHAR,    -- ← A가 공간조인으로 채움 (B는 NULL로 둠)
  routes_cnt  INTEGER,    -- 경유 노선 수
  is_drt_stop BOOLEAN     -- 똑버스 경유 정류소
);


-- ============================================================
-- L3  mart — 프론트가 쓰는 최종 형태
--     period: 0=출근(07-09) 1=낮(09-17) 2=퇴근(17-19) 3=심야(22-24)
-- ============================================================

CREATE TABLE IF NOT EXISTS mart_grid_metrics (
  grid_id          VARCHAR,
  period           INTEGER,

  -- [A] 수요
  demand_realized  DOUBLE,   -- B: 승하차 배분 × 시간배율
  demand_potential DOUBLE,   -- P: 인구·종사자 합성 × 시간배율
  d_score          DOUBLE,   -- 0.5·norm(B) + 0.5·norm(P)

  -- [B] 공급
  supply_freq      DOUBLE,   -- 배차간격 역수 (시간대별)
  supply_coverage  DOUBLE,   -- 정류장 커버리지
  s_score          DOUBLE,   -- 0.78·norm(freq) + 0.22·coverage

  -- [공동] 지표
  mi               DOUBLE,   -- clamp((z(D)-z(S)) × 감쇠, -2.6, 2.6)
  quad             VARCHAR,  -- need | over | drt | ok | mid
  priority         DOUBLE,   -- MI⁺ × 수요규모 × (1+1.6·고령비)

  PRIMARY KEY (grid_id, period)
);

-- 정규화 기준통계 — 시뮬레이터가 브라우저에서 재계산할 때 필요.
-- ★ 배치 없는 상태에서 한 번만 계산해 고정할 것.
CREATE TABLE IF NOT EXISTS mart_norm_stats (
  period INTEGER PRIMARY KEY,
  mD DOUBLE, sD DOUBLE, mS DOUBLE, sS DOUBLE, dRef DOUBLE, fRef DOUBLE
);
