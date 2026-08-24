-- ============================================================================
--  운영 DB 스키마 (PostgreSQL 16 + PostGIS 3.4)
--
--  적재:  python analysis/06_load_db.py       (server/static/*.json → DB)
--  기동:  docker compose up -d db
--  읽기:  server/db.py 가 v_* 만 읽어 server/main.py 의 DATA 를 채웁니다.
--
--  ── 설계 핵심: 소유권 분리 ────────────────────────────────────────────────
--    batch_*   배치가 매번 통째로 갈아엎음. 사람이 절대 수정 금지.
--    admin_*   사람이 수정. 배치가 조회조차 안 함. append-only.
--    v_*       둘을 합쳐 보여주는 뷰. API 는 이것만 읽음.
--
--  이 분리를 안 하면 관리자 수정 기능에서 가장 흔한 사고가 납니다 —
--  관리자가 화요일에 수정 → 수요일 배치 재실행 → 수정 소멸, 아무도 모름.
--
--  ⚠️ 이 파일은 계약 JSON(server/static/*.json)의 실제 형태에 맞춰져 있습니다.
--     컬럼을 고치면 06_load_db.py(적재)와 server/db.py(복원) 양쪽이 같이
--     바뀌어야 합니다. 셋이 어긋나면 API 응답이 조용히 달라집니다.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- ⚠️ json 과 jsonb 를 섞어 쓰는 것은 실수가 아닙니다.
--    jsonb 는 객체의 **키 순서를 보존하지 않습니다**(길이·바이트순으로 재정렬하고
--    공백도 버립니다). 프론트로 그대로 나가는 문서를 jsonb 에 넣으면 되돌려 읽을 때
--    {"mi":…,"demand":…} 가 {"mi":…,"flow":…} 순으로 바뀌어 응답 바이트가 달라집니다.
--    그래서 규칙은 하나입니다:
--      · 통과만 하는 문서(bins·kpi·meta·profile·path…)  → json   (원문 그대로)
--      · 안에서 질의·집계하는 것(override·row_counts…)  → jsonb  (연산자 필요)
--    통과 문서를 jsonb 로 "정리"하지 마세요. 조용히 응답이 바뀝니다.


-- ============================================================================
--  1. 배치 소유 — 파이프라인이 REPLACE 함
-- ============================================================================

-- 배치 실행 이력. 롤백과 재현에 필요하고, "이 화면의 숫자는 언제 만든 것인가"의 답.
CREATE TABLE IF NOT EXISTS batch_run (
  id          BIGSERIAL PRIMARY KEY,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status      TEXT NOT NULL DEFAULT 'running',   -- running|success|failed
  row_counts  JSONB,
  note        TEXT
);

-- 격자 — 시간대와 무관한 속성만. (아래 9개 필드가 4시간대 전부에서 동일함을
-- 실측 확인했습니다. 시간대별로 변하는 것은 batch_grid_metrics 로 갑니다.)
-- ⚠️ ord 는 장식이 아닙니다. 계약 JSON 의 배열 순서를 그대로 보존합니다.
--    /api/v1/priorities 는 priorityScore 로 정렬하는데 Python 의 sort 가 안정
--    정렬이라, 점수가 같은 격자들의 순위를 **입력 순서**가 가릅니다. 순서가
--    달라지면 동점 구간의 1위가 조용히 바뀝니다. batch_stop·batch_route·
--    batch_stop_profile 의 ord 도 같은 이유입니다(응답 바이트가 같아야 합니다).
CREATE TABLE IF NOT EXISTS batch_grid (
  grid_id         TEXT PRIMARY KEY,
  ord             INTEGER NOT NULL,
  name            TEXT NOT NULL,
  region          TEXT NOT NULL,
  region_code     TEXT NOT NULL,
  region_kind     TEXT NOT NULL,
  lon             DOUBLE PRECISION NOT NULL,
  lat             DOUBLE PRECISION NOT NULL,
  elderly_ratio   DOUBLE PRECISION,
  nearest_stop_id TEXT,
  -- 격자 폴리곤. 중심좌표와 meta.grid.sizeMeters 로 06_load_db.py 가 채웁니다.
  -- 계약 JSON 에는 중심점만 있어서, 면적·포함 질의를 하려면 여기서 만들어야 합니다.
  geom            GEOMETRY(Polygon, 4326),
  batch_run_id    BIGINT REFERENCES batch_run(id)
);
CREATE INDEX IF NOT EXISTS batch_grid_geom_ix ON batch_grid USING GIST (geom);

-- 격자 × 시간대 — 모델이 내놓는 지표. 관리자가 고치고 싶어하는 값이 전부 여기 있습니다.
CREATE TABLE IF NOT EXISTS batch_grid_metrics (
  grid_id            TEXT NOT NULL REFERENCES batch_grid(grid_id) ON DELETE CASCADE,
  period             TEXT NOT NULL CHECK (period IN ('am','day','pm','night')),
  -- 평일/주말. 화면의 요일 토글이 이 축입니다. 05_load.py 가 grid_{period}.json 과
  -- grid_{period}_we.json 두 벌을 굽고, 계약도 daytype 파라미터를 받습니다.
  daytype            TEXT NOT NULL DEFAULT 'wd' CHECK (daytype IN ('wd','we')),
  demand             INTEGER,            -- D×100 (정수). 화면 표기 단위 그대로
  supply             INTEGER,            -- S×100
  z_demand           DOUBLE PRECISION,
  z_supply           DOUBLE PRECISION,
  mi                 DOUBLE PRECISION,   -- 미스매칭 지수 = zD − zS
  flow               DOUBLE PRECISION,
  flow_trips_per_day INTEGER,
  coverage           DOUBLE PRECISION,
  quadrant           TEXT,               -- need|over|drt|ok|mid
  action             TEXT,               -- NEW_STOP|ADD_FREQ|DRT
  priority_score     DOUBLE PRECISION,
  -- 색 구간 4개(mi/demand/supply/flow). 화면 렌더 전용이라 개별 컬럼으로 풀 이유가
  -- 없습니다 — 질의 대상이 아니고 항상 통째로 나갑니다.
  bins               JSON NOT NULL,
  batch_run_id       BIGINT REFERENCES batch_run(id),
  PRIMARY KEY (grid_id, period, daytype)
);
-- (period, daytype) 인덱스는 아래 "구버전 DB 따라잡기" 뒤에서 만듭니다 —
--  구버전에는 daytype 컬럼이 아직 없어 여기서 만들면 실패합니다.

-- 시간대별 상수 — MI 색 경계. 모델이 정한 값이라 격자에서 파생되지 않습니다.
--
-- ⚠️ KPI(needCells·needShare·잠재수요…)는 **일부러 여기 두지 않습니다.** 격자에서
--    세면 나오는 값을 따로 저장하면, 관리자가 어떤 칸을 need 로 고쳤을 때 지도에는
--    붉은 칸이 하나 늘고 상단 KPI 는 그대로인 화면이 나옵니다(실제로 그렇게 나왔고
--    그래서 뺐습니다). server/db.py 의 _kpi 가 합쳐진 값에서 셉니다.
CREATE TABLE IF NOT EXISTS batch_grid_period (
  period         TEXT NOT NULL CHECK (period IN ('am','day','pm','night')),
  daytype        TEXT NOT NULL DEFAULT 'wd' CHECK (daytype IN ('wd','we')),
  mi_thresholds  JSON NOT NULL,
  batch_run_id   BIGINT REFERENCES batch_run(id),
  PRIMARY KEY (period, daytype)
);

-- meta.json — 지역·격자 제원·수식 계수·단가·데이터 품질 표기 등 설정 문서입니다.
-- 행 데이터가 아니라 문서라 통째로 둡니다. 10개 표로 쪼개도 질의할 일이 없고,
-- 쪼개는 순간 프론트 계약과 어긋날 자리만 10군데 생깁니다.
CREATE TABLE IF NOT EXISTS batch_meta (
  id           SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  doc          JSON NOT NULL,
  batch_run_id BIGINT REFERENCES batch_run(id)
);

CREATE TABLE IF NOT EXISTS batch_stop (
  stop_id           TEXT PRIMARY KEY,
  ord               INTEGER NOT NULL,
  ars_no            TEXT,
  name              TEXT NOT NULL,
  dong              TEXT,
  lon               DOUBLE PRECISION NOT NULL,
  lat               DOUBLE PRECISION NOT NULL,
  kind              TEXT,
  routes            JSON NOT NULL,      -- 경유 노선 id 배열
  boardings_per_day DOUBLE PRECISION,
  geom              GEOMETRY(Point, 4326),
  batch_run_id      BIGINT REFERENCES batch_run(id)
);
CREATE INDEX IF NOT EXISTS batch_stop_geom_ix ON batch_stop USING GIST (geom);

CREATE TABLE IF NOT EXISTS batch_route (
  route_id     TEXT PRIMARY KEY,
  ord          INTEGER NOT NULL,
  name         TEXT NOT NULL,
  type         TEXT,
  stop_ids     JSON NOT NULL,
  -- path 는 [[lon,lat],…] 원본을 그대로 둡니다. geom 은 그것으로 만든 조회용 사본이라,
  -- 프론트로 나가는 좌표는 항상 path 에서 나옵니다(왕복 변환으로 값이 흔들리지 않게).
  path         JSON NOT NULL,
  -- 운행정보(기점·종점·첫차·막차·배차간격·회사·회차점). 노선 상세 화면이 통째로
  -- 쓰고 질의 대상이 아니라 한 칸에 둡니다 — batch_meta·batch_stop_profile 과 같은
  -- 판단입니다. 개별 컬럼으로 풀면 09_augment_routes 가 필드를 늘릴 때마다
  -- 스키마·적재기·리더 세 곳을 함께 고쳐야 하고, 하나만 빠지면 조용히 사라집니다.
  ops          JSON,
  geom         GEOMETRY(LineString, 4326),
  batch_run_id BIGINT REFERENCES batch_run(id)
);
CREATE INDEX IF NOT EXISTS batch_route_geom_ix ON batch_route USING GIST (geom);

-- 정류장 시간대 프로필. 정류장당 문서 하나로 나가고 부분 질의가 없어 통째로 둡니다.
CREATE TABLE IF NOT EXISTS batch_stop_profile (
  stop_id      TEXT PRIMARY KEY,
  ord          INTEGER NOT NULL,
  doc          JSON NOT NULL,
  batch_run_id BIGINT REFERENCES batch_run(id)
);


-- ============================================================================
--  2. 관리자 소유 — 배치가 절대 안 건드림
-- ============================================================================

CREATE TABLE IF NOT EXISTS app_user (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,          -- bcrypt/argon2. 평문 금지
  role          TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer','admin')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ★ 관리자 값 덮어쓰기. 배치 결과를 직접 고치지 않고 여기에 쌓습니다.
--   되돌리기는 DELETE 가 아니라 revoked_at 을 채웁니다 — 이력이 남아야
--   "왜 모델 값을 사람이 고쳤는가"에 답할 수 있습니다(reason 이 NOT NULL 인 이유).
CREATE TABLE IF NOT EXISTS admin_grid_override (
  id         BIGSERIAL PRIMARY KEY,
  grid_id    TEXT NOT NULL,
  period     TEXT CHECK (period IN ('am','day','pm','night')),  -- NULL = 전 시간대
  daytype    TEXT CHECK (daytype IN ('wd','we')),                -- NULL = 평일·주말 모두
  field      TEXT NOT NULL CHECK (field IN
               ('mi','quadrant','priority_score','demand','supply','action')),
  value_num  DOUBLE PRECISION,
  value_text TEXT,
  reason     TEXT NOT NULL,
  created_by BIGINT REFERENCES app_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  -- 숫자 필드에 글자를, 글자 필드에 숫자를 넣는 사고를 스키마에서 막습니다.
  CONSTRAINT admin_grid_override_value_ck CHECK (
    (field IN ('mi','priority_score','demand','supply') AND value_num IS NOT NULL)
    OR (field IN ('quadrant','action') AND value_text IS NOT NULL)
  )
);
-- 살아 있는 override 만 뷰가 읽으므로 부분 인덱스로 충분합니다.
CREATE INDEX IF NOT EXISTS admin_grid_override_live_ix
  ON admin_grid_override (grid_id, period) WHERE revoked_at IS NULL;
-- 같은 칸·같은 시간대·같은 필드에 살아 있는 override 는 하나뿐이어야 합니다.
-- (없으면 아래 jsonb_object_agg 에서 어느 쪽이 이길지 정해지지 않습니다.)
-- (같은 이유로) 이 유니크 인덱스도 아래 따라잡기 뒤에서 만듭니다.

-- 시뮬레이션 시나리오 (공유 URL 의 실체)
CREATE TABLE IF NOT EXISTS scenario (
  id         BIGSERIAL PRIMARY KEY,
  slug       TEXT UNIQUE NOT NULL,
  title      TEXT,
  period     TEXT,
  budget_krw BIGINT,
  placements JSONB NOT NULL,            -- [{type:'drt', cellId:'다마1234'}, …]
  created_by BIGINT REFERENCES app_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id      BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES app_user(id),
  action  TEXT NOT NULL,                -- 'override.create' | 'override.revoke' …
  target  TEXT,
  payload JSONB,
  at      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ============================================================================
--  3. 실시간 — TTL 캐시. 영구 보관 안 함.
-- ============================================================================

CREATE TABLE IF NOT EXISTS rt_bus_position (
  route_id   TEXT NOT NULL,
  plate_no   TEXT NOT NULL,
  lon        DOUBLE PRECISION,
  lat        DOUBLE PRECISION,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (route_id, plate_no)
);
CREATE INDEX IF NOT EXISTS rt_bus_position_age_ix ON rt_bus_position (updated_at);
-- 워커가 주기적으로:
--   DELETE FROM rt_bus_position WHERE updated_at < now() - interval '5 min';


-- ============================================================================
--  4. 합성 뷰 — API 는 이것만 읽습니다
-- ============================================================================

-- 살아 있는 override 를 (격자, 시간대)당 JSON 한 덩이로 모읍니다.
--
-- 필드마다 LEFT JOIN 을 하나씩 두는 방법도 있지만(예전 설계가 그랬습니다),
-- 덮어쓸 수 있는 필드를 하나 늘릴 때마다 조인이 하나씩 붙습니다. 이렇게 모아 두면
-- 아래 뷰에서 COALESCE 한 줄만 늘어나고 조인 수는 그대로 둘입니다.

-- ── 구버전 DB 따라잡기 (멱등) ────────────────────────────────────────────────
-- 이 파일은 06_load_db.py 가 **매번** 통째로 실행합니다. 그런데
-- CREATE TABLE IF NOT EXISTS 는 이미 있는 테이블에 컬럼을 더해 주지 않습니다.
-- 요일축(daytype)을 뒤늦게 넣었으므로, 이미 올라와 있는 DB 를 여기서 따라잡게
-- 합니다. 새로 만든 DB 에서는 전부 no-op 입니다.
DO $$
BEGIN
  ALTER TABLE batch_grid_metrics  ADD COLUMN IF NOT EXISTS daytype TEXT NOT NULL DEFAULT 'wd';
  ALTER TABLE batch_grid_period   ADD COLUMN IF NOT EXISTS daytype TEXT NOT NULL DEFAULT 'wd';
  ALTER TABLE admin_grid_override ADD COLUMN IF NOT EXISTS daytype TEXT;
  -- 노선 운행정보도 스키마보다 나중에 들어왔습니다(09_augment_routes).
  ALTER TABLE batch_route ADD COLUMN IF NOT EXISTS ops JSON;

  -- 기본키에 daytype 이 빠져 있으면 다시 건다. 안 그러면 평일 행이 주말 행을
  -- 덮어써서 8벌이 4벌로 줄어든다(조용히 절반이 사라지는 종류).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.key_column_usage
    WHERE table_name = 'batch_grid_metrics'
      AND constraint_name = 'batch_grid_metrics_pkey' AND column_name = 'daytype'
  ) THEN
    ALTER TABLE batch_grid_metrics DROP CONSTRAINT batch_grid_metrics_pkey;
    ALTER TABLE batch_grid_metrics ADD PRIMARY KEY (grid_id, period, daytype);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.key_column_usage
    WHERE table_name = 'batch_grid_period'
      AND constraint_name = 'batch_grid_period_pkey' AND column_name = 'daytype'
  ) THEN
    ALTER TABLE batch_grid_period DROP CONSTRAINT batch_grid_period_pkey;
    ALTER TABLE batch_grid_period ADD PRIMARY KEY (period, daytype);
  END IF;
END $$;

-- 컬럼이 확보된 뒤에 만듭니다.
CREATE INDEX IF NOT EXISTS batch_grid_metrics_period_ix
  ON batch_grid_metrics (period, daytype);
-- 같은 칸·시간대·요일·필드에 살아 있는 override 는 하나뿐이어야 합니다.
CREATE UNIQUE INDEX IF NOT EXISTS admin_grid_override_one_live_ix
  ON admin_grid_override (grid_id, COALESCE(period, '*'), COALESCE(daytype, '*'), field)
  WHERE revoked_at IS NULL;

-- 뷰는 매번 새로 만듭니다. CREATE OR REPLACE 는 **컬럼을 끝에만** 더할 수 있어,
-- daytype 을 중간에 끼운 지금 형태로는 기존 DB 에서 실패합니다("cannot change
-- name of view column"). 뷰에는 데이터가 없으므로 지우고 다시 만드는 편이 안전합니다.
DROP VIEW IF EXISTS v_grid_cell;
DROP VIEW IF EXISTS v_grid_metrics;
DROP VIEW IF EXISTS v_grid_override;

CREATE OR REPLACE VIEW v_grid_override AS
SELECT grid_id,
       period,
       daytype,
       jsonb_object_agg(field,
         CASE WHEN value_num IS NOT NULL THEN to_jsonb(value_num)
              ELSE to_jsonb(value_text) END) AS ov
FROM admin_grid_override
WHERE revoked_at IS NULL
GROUP BY grid_id, period, daytype;

-- 시간대 지정 override 가 전 시간대(period IS NULL) override 를 이깁니다.
-- 둘 다 없으면 배치 값이 그대로 나옵니다 — override 가 0건이면 이 뷰의 출력은
-- batch_grid_metrics 와 완전히 같습니다(그래야 JSON 모드와 바이트가 일치합니다).
CREATE OR REPLACE VIEW v_grid_metrics AS
SELECT
  m.grid_id,
  m.period,
  m.daytype,
  COALESCE((sp.ov->>'demand')::int,          (gl.ov->>'demand')::int,          m.demand)         AS demand,
  COALESCE((sp.ov->>'supply')::int,          (gl.ov->>'supply')::int,          m.supply)         AS supply,
  m.z_demand,
  m.z_supply,
  COALESCE((sp.ov->>'mi')::float8,           (gl.ov->>'mi')::float8,           m.mi)             AS mi,
  m.flow,
  m.flow_trips_per_day,
  m.coverage,
  COALESCE( sp.ov->>'quadrant',               gl.ov->>'quadrant',              m.quadrant)       AS quadrant,
  COALESCE( sp.ov->>'action',                 gl.ov->>'action',                m.action)         AS action,
  COALESCE((sp.ov->>'priority_score')::float8,(gl.ov->>'priority_score')::float8,m.priority_score) AS priority_score,
  m.bins,
  (sp.grid_id IS NOT NULL OR gl.grid_id IS NOT NULL)                                             AS is_overridden
FROM batch_grid_metrics m
-- 시간대 지정 override 가 전 시간대 override 를 이깁니다. daytype 은 NULL 이면
-- 평일·주말 모두에 걸립니다(지정돼 있으면 그 요일에만).
LEFT JOIN v_grid_override sp ON sp.grid_id = m.grid_id AND sp.period = m.period
                            AND (sp.daytype IS NULL OR sp.daytype = m.daytype)
LEFT JOIN v_grid_override gl ON gl.grid_id = m.grid_id AND gl.period IS NULL
                            AND (gl.daytype IS NULL OR gl.daytype = m.daytype);

-- 격자 속성 + 지표를 합친 것. server/db.py 가 이 한 방을 시간대별로 읽습니다.
CREATE OR REPLACE VIEW v_grid_cell AS
SELECT g.grid_id, g.ord, g.name, g.region, g.region_code, g.region_kind, g.lon, g.lat,
       g.elderly_ratio, g.nearest_stop_id,
       v.period, v.daytype, v.demand, v.supply, v.z_demand, v.z_supply, v.mi, v.flow,
       v.flow_trips_per_day, v.coverage, v.quadrant, v.action, v.priority_score,
       v.bins, v.is_overridden
FROM batch_grid g
JOIN v_grid_metrics v ON v.grid_id = g.grid_id;
