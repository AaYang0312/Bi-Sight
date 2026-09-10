-- 可审计的经营查询运行状态；只由管理员执行。
-- 运行状态仅保存安全投影，不存原始问题、隐藏推理、数据库错误或真实 ERP 标识。

CREATE TABLE IF NOT EXISTS bi.query_runs (
  id                 uuid PRIMARY KEY,
  chat_id            uuid NOT NULL REFERENCES bi.app_chats(id) ON DELETE CASCADE,
  user_message_id    uuid NOT NULL REFERENCES bi.app_messages(id) ON DELETE CASCADE,
  subject_id         text NOT NULL,
  tool_call_id       text NOT NULL,
  domain             text NOT NULL DEFAULT 'business_query'
                     CHECK (domain = 'business_query'),
  attempt_no         integer NOT NULL CHECK (attempt_no >= 1),
  status             text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','succeeded','needs_input',
                                       'missing_data','partial','failed')),
  current_node       text NOT NULL DEFAULT 'received',
  revision           integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
  normalized_request jsonb NOT NULL DEFAULT '{}',
  state              jsonb NOT NULL DEFAULT '{}',
  error_code         text,
  started_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  completed_at       timestamptz,
  UNIQUE (user_message_id, domain, attempt_no)
);

CREATE TABLE IF NOT EXISTS bi.query_run_events (
  run_id      uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  revision    integer NOT NULL CHECK (revision >= 1),
  node        text NOT NULL,
  event_type  text NOT NULL CHECK (event_type IN
              ('entered','completed','failed','transitioned')),
  status      text NOT NULL CHECK (status IN
              ('running','succeeded','needs_input','missing_data','partial','failed')),
  payload     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, revision)
);

CREATE TABLE IF NOT EXISTS bi.query_artifacts (
  id             uuid PRIMARY KEY,
  run_id         uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  artifact_type  text NOT NULL CHECK (artifact_type = 'metric_result'),
  payload        jsonb NOT NULL,
  data_as_of     timestamptz,
  coverage       jsonb,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS query_runs_chat_started_idx
  ON bi.query_runs(chat_id, started_at DESC);
CREATE INDEX IF NOT EXISTS query_runs_message_attempt_idx
  ON bi.query_runs(user_message_id, attempt_no);
CREATE INDEX IF NOT EXISTS query_artifacts_run_idx
  ON bi.query_artifacts(run_id);

GRANT SELECT, INSERT, UPDATE ON bi.query_runs TO bi_app;
GRANT SELECT, INSERT ON bi.query_run_events, bi.query_artifacts TO bi_app;
