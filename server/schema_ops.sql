-- ============================================================
-- 운영 DB 스키마 (PostgreSQL 17 + PostGIS)
--
-- 설계 핵심: 소유권 분리
--   batch_*      배치가 매번 통째로 갈아엎음. 사람이 절대 수정 금지.
--   admin_*      사람이 수정. 배치가 절대 건드리지 않음.
--   v_*          두 개를 합쳐 보여주는 뷰. 프론트는 이것만 읽음.
--
-- 이 분리를 안 하면 배치 재실행 때 관리자 수정이 전부 날아갑니다.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================
-- 1. 배치 소유 — 파이프라인이 REPLACE 함
-- ============================================================

CREATE TABLE batch_grid (
  grid_id        TEXT PRIMARY KEY,
  geom           GEOMETRY(Polygon, 4326) NOT NULL,
  region_name    TEXT,
  pop            DOUBLE PRECISION,
  elderly_ratio  DOUBLE PRECISION,
  workers        DOUBLE PRECISION,
  nearest_rail_m DOUBLE PRECISION,
  batch_run_id   BIGINT NOT NULL
);
CREATE INDEX ON batch_grid USING GIST (geom);

CREATE TABLE batch_grid_metrics (
  grid_id   TEXT NOT NULL,
  period    SMALLINT NOT NULL CHECK (period BETWEEN 0 AND 3),
  d_score   DOUBLE PRECISION,
  s_score   DOUBLE PRECISION,
  mi        DOUBLE PRECISION,
  quad      TEXT,
  priority  DOUBLE PRECISION,
  batch_run_id BIGINT NOT NULL,
  PRIMARY KEY (grid_id, period)
);

-- 배치 실행 이력 — 롤백과 재현에 필요
CREATE TABLE batch_run (
  id          BIGSERIAL PRIMARY KEY,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status      TEXT NOT NULL DEFAULT 'running',  -- running|success|failed
  row_counts  JSONB,
  note        TEXT
);


-- ============================================================
-- 2. 관리자 소유 — 배치가 절대 안 건드림
-- ============================================================

CREATE TABLE app_user (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,       -- bcrypt/argon2. 평문 금지
  role          TEXT NOT NULL DEFAULT 'viewer',  -- viewer|admin
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ★ 관리자 값 덮어쓰기. 배치 결과를 직접 고치지 않고 여기에 쌓습니다.
CREATE TABLE admin_grid_override (
  id          BIGSERIAL PRIMARY KEY,
  grid_id     TEXT NOT NULL,
  period      SMALLINT,              -- NULL이면 전 시간대 적용
  field       TEXT NOT NULL,         -- 'quad' | 'priority' | 'mi' ...
  value_num   DOUBLE PRECISION,
  value_text  TEXT,
  reason      TEXT NOT NULL,         -- 왜 고쳤는지 필수. 심사 때 설명 근거
  created_by  BIGINT REFERENCES app_user(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at  TIMESTAMPTZ            -- 되돌리기는 삭제가 아니라 revoke
);
CREATE INDEX ON admin_grid_override (grid_id, period) WHERE revoked_at IS NULL;

-- 시뮬레이션 시나리오 (공유 URL의 실체)
CREATE TABLE scenario (
  id          BIGSERIAL PRIMARY KEY,
  slug        TEXT UNIQUE NOT NULL,  -- URL에 노출되는 짧은 키
  title       TEXT,
  placements  JSONB NOT NULL,        -- [{type:'drt', grid_id:'다마1234'}, ...]
  created_by  BIGINT REFERENCES app_user(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 감사 로그 — 누가 무엇을 언제 바꿨나
CREATE TABLE audit_log (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT REFERENCES app_user(id),
  action     TEXT NOT NULL,          -- 'override.create' 등
  target     TEXT,
  payload    JSONB,
  at         TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ============================================================
-- 3. 실시간 — TTL 캐시. 영구 보관 안 함.
-- ============================================================

CREATE TABLE rt_bus_position (
  route_id   TEXT NOT NULL,
  plate_no   TEXT NOT NULL,
  lon        DOUBLE PRECISION,
  lat        DOUBLE PRECISION,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (route_id, plate_no)
);
CREATE INDEX ON rt_bus_position (updated_at);
-- 워커가 주기적으로: DELETE FROM rt_bus_position WHERE updated_at < now() - interval '5 min';


-- ============================================================
-- 4. 합성 뷰 — 프론트/API는 이것만 읽습니다
--    override가 있으면 그 값이, 없으면 배치 값이 나옵니다.
-- ============================================================

CREATE OR REPLACE VIEW v_grid_metrics AS
SELECT
  m.grid_id,
  m.period,
  m.d_score,
  m.s_score,
  COALESCE(ov_mi.value_num,   m.mi)       AS mi,
  COALESCE(ov_qd.value_text,  m.quad)     AS quad,
  COALESCE(ov_pr.value_num,   m.priority) AS priority,
  (ov_mi.id IS NOT NULL
   OR ov_qd.id IS NOT NULL
   OR ov_pr.id IS NOT NULL)               AS is_overridden
FROM batch_grid_metrics m
LEFT JOIN admin_grid_override ov_mi
  ON ov_mi.grid_id = m.grid_id AND ov_mi.field = 'mi'
 AND (ov_mi.period IS NULL OR ov_mi.period = m.period)
 AND ov_mi.revoked_at IS NULL
LEFT JOIN admin_grid_override ov_qd
  ON ov_qd.grid_id = m.grid_id AND ov_qd.field = 'quad'
 AND (ov_qd.period IS NULL OR ov_qd.period = m.period)
 AND ov_qd.revoked_at IS NULL
LEFT JOIN admin_grid_override ov_pr
  ON ov_pr.grid_id = m.grid_id AND ov_pr.field = 'priority'
 AND (ov_pr.period IS NULL OR ov_pr.period = m.period)
 AND ov_pr.revoked_at IS NULL;
