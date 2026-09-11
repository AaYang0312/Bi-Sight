-- 版本化多领域运行契约（计划 Task 3）。
-- 必须在 004_query_runtime.sql 之后执行；008 提供目录版本与来源批次。
--
-- 原则：`revision` 只表示状态推进，不承担数据版本语义。哪一版口径、哪一批数据
-- 必须由显式字段记录，且请求指纹依赖它们，否则回填之后的新查询会命中旧结果。

-- 1) 运行身份：跨重试稳定的请求身份，以及"为什么终止"。
ALTER TABLE bi.query_runs
  ADD COLUMN IF NOT EXISTS root_request_id    uuid,
  ADD COLUMN IF NOT EXISTS request_fingerprint text,
  ADD COLUMN IF NOT EXISTS recovery_count     integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS termination_reason text;

ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_fingerprint_format;
ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_fingerprint_format
  CHECK (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$');

ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_recovery_count_non_negative;
ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_recovery_count_non_negative
  CHECK (recovery_count >= 0);

-- 终止原因只能取自固定码表：把任意错误文本写进运行记录等于开了第二条日志通道。
-- 这份清单与 bi_agent.runtime.models.TERMINATION_REASONS 由测试比对，不允许两边漂移。
ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_termination_reason;
ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_termination_reason CHECK (
  termination_reason IS NULL OR termination_reason IN (
    'succeeded', 'missing_parameters', 'invalid_parameters', 'forbidden',
    'coverage_incomplete', 'data_as_of_unknown', 'source_quality_failed',
    'source_not_onboarded', 'revenue_not_attributed', 'result_too_large',
    'comparison_coverage_incomplete', 'deadline_exceeded', 'query_timeout',
    'persistence_failed', 'contract_violation', 'upstream_unavailable',
    'transient_source_failure', 'recovery_exhausted')
);

-- 2) 领域白名单扩展：仍然可枚举，未知领域继续被数据库拒。
ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_domain_check;
ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_domain_check CHECK (
  domain IN ('business_query', 'commerce_performance', 'listing_price_audit',
             'inventory_watch')
);

COMMENT ON COLUMN bi.query_runs.request_fingerprint IS
  '规范化请求 + 授权范围 + 口径与数据版本的 sha256；版本变化后不得命中旧结果。';
COMMENT ON COLUMN bi.query_runs.root_request_id IS
  '同一原始请求跨重试共用；恢复尝试不新造请求身份。';

-- 3) 数据血缘：一次查询用了哪一版口径与哪几批来源数据。
CREATE TABLE IF NOT EXISTS bi.query_provenance (
  run_id           uuid PRIMARY KEY REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  template_id      text NOT NULL,
  template_version text NOT NULL,
  metric_version   text NOT NULL,
  schema_version   text NOT NULL,
  catalog_version  bigint NOT NULL DEFAULT 0 CHECK (catalog_version >= 0),
  mapping_version  text NOT NULL,
  policy_version   text NOT NULL,
  graph_version    text NOT NULL,
  source_batches   text[] NOT NULL DEFAULT '{}',
  data_as_of       timestamptz,
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  -- 版本字段是标识符，不是自由文本：挡住把 SQL 片段或主键塞进血缘的写法。
  CONSTRAINT query_provenance_no_whitespace CHECK (
    template_id = btrim(template_id) AND template_version = btrim(template_version)
    AND metric_version = btrim(metric_version) AND schema_version = btrim(schema_version)
    AND mapping_version = btrim(mapping_version) AND policy_version = btrim(policy_version)
    AND graph_version = btrim(graph_version))
);

COMMENT ON COLUMN bi.query_provenance.source_batches IS
  '本次结果依赖的同步批次；指向 bi.sync_batches，不复制其内容。';

-- 4) Artifact 类型白名单扩展：六类可枚举，未知类型仍被拒。
ALTER TABLE bi.query_artifacts DROP CONSTRAINT IF EXISTS query_artifacts_artifact_type_check;
ALTER TABLE bi.query_artifacts ADD CONSTRAINT query_artifacts_artifact_type_check CHECK (
  artifact_type IN ('metric_result', 'comparison_table', 'trend_series', 'chart_spec',
                    'price_audit', 'inventory_alerts')
);

-- 图表必须与数据集同版本：chart_spec 不允许引用不存在的 Artifact 版本。
ALTER TABLE bi.query_artifacts
  ADD COLUMN IF NOT EXISTS dataset_ref   uuid,
  ADD COLUMN IF NOT EXISTS chart_version integer;

ALTER TABLE bi.query_artifacts DROP CONSTRAINT IF EXISTS query_artifacts_chart_version;
ALTER TABLE bi.query_artifacts ADD CONSTRAINT query_artifacts_chart_version CHECK (
  (artifact_type = 'chart_spec' AND dataset_ref IS NOT NULL AND chart_version >= 1)
  OR (artifact_type <> 'chart_spec' AND dataset_ref IS NULL AND chart_version IS NULL));

-- 5) 诊断记录：需要留证的 SQL 与参数进这张表，通过引用关联；
--    绝不写进模型消息或普通事件文本。不建 reporting 视图，也不给 bi_reader。
CREATE TABLE IF NOT EXISTS bi.query_diagnostics (
  id          uuid PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  template_id text NOT NULL,
  sql_text    text NOT NULL,
  parameters  jsonb NOT NULL DEFAULT '{}',
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT query_diagnostics_template_no_whitespace CHECK (template_id = btrim(template_id))
);

CREATE INDEX IF NOT EXISTS query_diagnostics_run_idx ON bi.query_diagnostics(run_id);
CREATE INDEX IF NOT EXISTS query_runs_fingerprint_idx
  ON bi.query_runs(subject_id, request_fingerprint, started_at DESC);
CREATE INDEX IF NOT EXISTS query_artifacts_dataset_ref_idx
  ON bi.query_artifacts(dataset_ref) WHERE dataset_ref IS NOT NULL;

COMMENT ON TABLE bi.query_diagnostics IS
  '受控诊断记录：SQL 原文与参数只在这里，通过 run_id 引用；不进模型载荷与事件文本。';

GRANT SELECT, INSERT, UPDATE ON bi.query_provenance, bi.query_diagnostics TO bi_app;
REVOKE ALL ON bi.query_provenance, bi.query_diagnostics FROM PUBLIC;
