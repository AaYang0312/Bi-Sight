-- 数据就绪与来源凭证：质量状态升级规则、对账时间/版本、同步批次证据。
-- 必须在 007_catalog_identity.sql 之后执行。
--
-- 口径见 docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 1：
-- 覆盖（covered）、业务截止（data_as_of）与质量（quality_status）是三件不同的事，
-- 任何一件都不能拿另一件替代。

-- 1) 质量状态：boolean 的“非真即错”换成显式三态。
ALTER TABLE bi.sync_state
  ADD COLUMN IF NOT EXISTS quality_status     text NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS quality_checked_at timestamptz,
  ADD COLUMN IF NOT EXISTS quality_rule       text,
  ADD COLUMN IF NOT EXISTS quality_reason     text;

ALTER TABLE bi.sync_state DROP CONSTRAINT IF EXISTS sync_state_quality_status_check;
ALTER TABLE bi.sync_state ADD CONSTRAINT sync_state_quality_status_check
  CHECK (quality_status IN ('unknown', 'passed', 'failed'));

COMMENT ON COLUMN bi.sync_state.quality_status IS
  'unknown=从未核验（默认，仍可出数但必须披露）；passed=按 quality_rule 核验通过；failed=对账发现差异，禁止出数。';
COMMENT ON COLUMN bi.sync_state.quality_rule IS
  '核验所用口径版本；口径升级后旧的 passed 不得自动沿用，必须重跑对账。';
COMMENT ON COLUMN bi.sync_state.quality_checked_at IS
  '最近一次对账时刻；不用 last_success_at 顶替。';

-- 2) 先拆掉依赖旧列的视图：CREATE OR REPLACE VIEW 不能减列，只能 DROP 后重建。
--    视图权限属于视图对象，重建后在末尾逐项重新授权。
DROP VIEW IF EXISTS reporting.v_coverage;

-- 3) 历史数据处理：旧的 quality_ok=false 只代表“没核验过”，一律记 unknown，
--    不能当成已经发现数据有错；true 才继承为 passed，并显式标注是无版本的旧核验。
--    整块 guarded 在列存在时才执行，保证本迁移可重复跑。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'bi' AND table_name = 'sync_state'
               AND column_name = 'quality_ok') THEN
    UPDATE bi.sync_state
       SET quality_status = 'passed', quality_checked_at = last_success_at,
           quality_rule = 'legacy:quality_ok'
     WHERE quality_ok;
    -- 单一真源：布尔列退役，避免与三态并存后两边漂移。
    ALTER TABLE bi.sync_state DROP COLUMN quality_ok;
  END IF;
END
$$;

COMMENT ON COLUMN bi.sync_state.last_error_code IS
  '最近一次同步失败的错误码；不表示数据质量，质量看 quality_status。';

-- 4) 同步批次证据：回答“这些数字是哪几批同步出来的”。
--    窗口按 [start,end) 业务时间记录，与 covered 同一口径。
CREATE TABLE IF NOT EXISTS bi.sync_batches (
  batch_id     text NOT NULL,
  source       text NOT NULL,
  entity       text NOT NULL,
  shop_id      text NOT NULL,
  business_window tstzrange NOT NULL,
  window_kind  text NOT NULL DEFAULT 'business',
  mode         text NOT NULL,
  row_count    integer,
  business_end timestamptz,
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, entity, shop_id, batch_id),
  CONSTRAINT sync_batches_window_not_empty CHECK (NOT isempty(business_window)),
  CONSTRAINT sync_batches_window_kind CHECK (
    window_kind IN ('business', 'modified')),
  CONSTRAINT sync_batches_mode CHECK (
    mode IN ('backfill', 'incremental', 'replay', 'reconcile', 'scan', 'probe')),
  CONSTRAINT sync_batches_row_count_non_negative CHECK (
    row_count IS NULL OR row_count >= 0)
);

-- 本迁移还未对外发布；已在开发库跑过旧版 008 的，这里补齐列与约束，不要求重建库。
ALTER TABLE bi.sync_batches ADD COLUMN IF NOT EXISTS window_kind text NOT NULL DEFAULT 'business';
ALTER TABLE bi.sync_batches DROP CONSTRAINT IF EXISTS sync_batches_window_kind;
ALTER TABLE bi.sync_batches ADD CONSTRAINT sync_batches_window_kind
  CHECK (window_kind IN ('business', 'modified'));

CREATE INDEX IF NOT EXISTS sync_batches_window_idx
  ON bi.sync_batches USING gist (business_window);

COMMENT ON COLUMN bi.sync_batches.business_window IS
  '该批次处理的 [start,end) 窗口；具体是什么时间口径看 window_kind，不能直接当业务覆盖。';
COMMENT ON COLUMN bi.sync_batches.window_kind IS
  'business=回填/重放/对账的业务时间窗口，可当覆盖凭证；modified=增量的修改时间窗口，不能拿来证明业务覆盖。';

COMMENT ON COLUMN bi.sync_batches.row_count IS
  '该批次落库行数；NULL 表示当时未统计，不能按 0 解释成“确实没有数据”。';

-- 5) 视图重建：v_coverage 换列后必须逐项重新授权（001 的 ON ALL TABLES 不覆盖后建对象）。
CREATE OR REPLACE VIEW reporting.v_coverage AS
SELECT source, entity, shop_id, watermark, covered, data_as_of,
       last_success_at, last_attempt_at, last_error_code,
       quality_status, quality_checked_at, quality_rule, quality_reason
FROM bi.sync_state;

-- 列数变了，CREATE OR REPLACE VIEW 不能改列，先 DROP 再重建。
DROP VIEW IF EXISTS reporting.v_source_batches;

CREATE OR REPLACE VIEW reporting.v_source_batches AS
SELECT source, entity, shop_id, batch_id, lower(business_window) AS window_start,
       upper(business_window) AS window_end, window_kind, mode, row_count,
       business_end, recorded_at
FROM bi.sync_batches;

GRANT SELECT, INSERT, UPDATE ON bi.sync_batches TO bi_sync;
REVOKE ALL ON bi.sync_batches FROM PUBLIC;
REVOKE ALL ON bi.sync_batches FROM bi_app;
GRANT SELECT ON reporting.v_source_batches TO bi_reader, bi_app;
GRANT SELECT ON reporting.v_coverage TO bi_reader, bi_app;
