-- 事实表、约束、视图与最小数据库权限。只由管理员执行；文件不含密码。
-- 业务时间 timestamptz；金额 numeric(20,6)；source 固定为具体接口方法名。

CREATE SCHEMA IF NOT EXISTS bi;
CREATE SCHEMA IF NOT EXISTS reporting;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- 角色：密码由管理员通过 \password 或现有密钥设施设置
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bi_sync') THEN
    CREATE ROLE bi_sync LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bi_reader') THEN
    CREATE ROLE bi_reader LOGIN;
  END IF;
END
$$;

ALTER ROLE bi_reader SET default_transaction_read_only = on;
ALTER ROLE bi_reader SET statement_timeout = '5s';

-- ---------------------------------------------------------------------------
-- bi.shops：ERP userId 转字符串；capabilities 只由对账维护
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.shops (
  shop_id      text PRIMARY KEY,
  platform     text NOT NULL DEFAULT 'fxg',
  display_name text NOT NULL DEFAULT '',
  currency     text NOT NULL DEFAULT 'CNY',
  enabled      boolean NOT NULL DEFAULT true,
  capabilities text[] NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- bi.orders：ERP单据（保留拆合单关联；买家字段一律不入库）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.orders (
  shop_id              text NOT NULL REFERENCES bi.shops(shop_id),
  erp_id               text NOT NULL,
  commercial_ids       text[] NOT NULL DEFAULT '{}',
  split_parent_id      text,
  source               text NOT NULL,
  source_updated_at    timestamptz NOT NULL,
  platform_modified_at timestamptz,
  paid_at              timestamptz,
  raw_pay_amount         numeric(20,6),
  raw_payment            numeric(20,6),
  raw_platform_payment   numeric(20,6),
  raw_cost               numeric(20,6),
  raw_gross_profit       numeric(20,6),
  active               boolean NOT NULL DEFAULT true,
  normalization_status text NOT NULL DEFAULT 'normal',
  batch_id             text NOT NULL,
  PRIMARY KEY (shop_id, erp_id)
);

COMMENT ON COLUMN bi.orders.commercial_ids IS '原始商业订单号集合；只作关联，不直接展开后SUM订单金额';
COMMENT ON COLUMN bi.orders.normalization_status IS 'normal/needs_review/version_conflict';

-- ---------------------------------------------------------------------------
-- bi.order_items：保存销售父行；套件不同时累加父行和子件
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.order_items (
  shop_id               text NOT NULL,
  erp_id                text NOT NULL,
  line_id               text NOT NULL,
  commercial_id         text,
  platform_line_id      text,
  product_id            text,
  sku_id                text,
  paid_at               timestamptz,
  quantity              numeric(20,6) NOT NULL DEFAULT 0 CHECK (quantity NOT IN ('NaN', 'Infinity', '-Infinity')),
  gift_quantity         numeric(20,6) NOT NULL DEFAULT 0 CHECK (gift_quantity NOT IN ('NaN', 'Infinity', '-Infinity')),
  raw_paid_amount       numeric(20,6),
  raw_payment           numeric(20,6),
  raw_unit_cost         numeric(20,6),
  allocated_paid_amount numeric(20,6),
  allocation_verified   boolean NOT NULL DEFAULT false,
  line_kind             text NOT NULL DEFAULT 'sale',
  active                boolean NOT NULL DEFAULT true,
  PRIMARY KEY (shop_id, erp_id, line_id),
  FOREIGN KEY (shop_id, erp_id) REFERENCES bi.orders(shop_id, erp_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- bi.order_payments：一行对应原始商业订单，保存一次支付事实；
-- 由规范化交易重建；不确定金额或时间留NULL并令verified=false
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.order_payments (
  shop_id           text NOT NULL,
  commercial_id     text NOT NULL,
  paid_at           timestamptz,
  amount            numeric(20,6),
  currency          text NOT NULL DEFAULT 'CNY',
  basis             text NOT NULL DEFAULT '',
  verified          boolean NOT NULL DEFAULT false,
  source_updated_at timestamptz,
  PRIMARY KEY (shop_id, commercial_id)
);

-- ---------------------------------------------------------------------------
-- bi.aftersales：无原订单时仍入库，不设阻止未匹配退款的FK
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.aftersales (
  shop_id               text NOT NULL,
  aftersale_id          text NOT NULL,
  platform_refund_id    text,
  commercial_id         text,
  erp_id                text,
  raw_platform_amount   numeric(20,6),
  raw_system_amount     numeric(20,6),
  online_status         integer,
  work_status           integer,
  platform_completed_at timestamptz,
  system_completed_at   timestamptz,
  source_updated_at     timestamptz NOT NULL,
  platform_success      boolean NOT NULL DEFAULT false,
  refund_canonical      boolean NOT NULL DEFAULT false,
  matched               boolean NOT NULL DEFAULT false,
  batch_id              text NOT NULL,
  PRIMARY KEY (shop_id, aftersale_id)
);

-- ---------------------------------------------------------------------------
-- bi.sync_state：修改水位与业务覆盖分开；token字段不存token值
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.sync_state (
  source           text NOT NULL,
  entity           text NOT NULL,
  shop_id          text NOT NULL,
  watermark        timestamptz NOT NULL DEFAULT '1970-01-01 00:00:00+00',
  covered          tstzmultirange NOT NULL DEFAULT tstzmultirange(),
  data_as_of       timestamptz,
  last_success_at  timestamptz,
  last_attempt_at  timestamptz,
  last_error_code  text,
  quality_ok       boolean NOT NULL DEFAULT false,
  token_expires_at timestamptz,
  last_refresh_at  timestamptz,
  PRIMARY KEY (source, entity, shop_id),
  CONSTRAINT sync_state_entity CHECK (
    entity IN ('orders', 'aftersales_occurrence', 'aftersales_cohort', 'session'))
);

-- ---------------------------------------------------------------------------
-- 索引
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS orders_commercial_ids_idx ON bi.orders USING gin(commercial_ids);
CREATE INDEX IF NOT EXISTS orders_shop_paid_idx ON bi.orders(shop_id, paid_at);
CREATE INDEX IF NOT EXISTS orders_source_updated_idx ON bi.orders(shop_id, source_updated_at);
CREATE INDEX IF NOT EXISTS payments_time_idx ON bi.order_payments(shop_id, paid_at);
CREATE INDEX IF NOT EXISTS order_items_commercial_idx ON bi.order_items(shop_id, commercial_id);
CREATE INDEX IF NOT EXISTS order_items_product_idx ON bi.order_items(shop_id, product_id);
CREATE INDEX IF NOT EXISTS refunds_time_idx ON bi.aftersales(shop_id, platform_completed_at);
CREATE INDEX IF NOT EXISTS aftersales_commercial_idx ON bi.aftersales(shop_id, commercial_id);
CREATE INDEX IF NOT EXISTS aftersales_platform_refund_idx ON bi.aftersales(shop_id, platform_refund_id);
CREATE INDEX IF NOT EXISTS sync_state_covered_idx ON bi.sync_state USING gist(covered);

-- ---------------------------------------------------------------------------
-- reporting 只读视图：无PII；模型仍不能直接访问它们
-- 新视图逐项授权，避免给未来所有表默认读权限
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_payments AS
SELECT shop_id, commercial_id, paid_at, amount, currency, verified
FROM bi.order_payments;

CREATE OR REPLACE VIEW reporting.v_refunds AS
SELECT shop_id, aftersale_id, platform_refund_id, commercial_id, erp_id,
       raw_platform_amount, platform_success, refund_canonical, matched,
       platform_completed_at
FROM bi.aftersales;

CREATE OR REPLACE VIEW reporting.v_coverage AS
SELECT source, entity, shop_id, watermark, covered, data_as_of,
       last_success_at, last_attempt_at, last_error_code, quality_ok
FROM bi.sync_state;

CREATE OR REPLACE VIEW reporting.v_shops AS
SELECT shop_id, platform, display_name, currency, enabled, capabilities
FROM bi.shops;

-- ---------------------------------------------------------------------------
-- 权限：bi_sync 事实表读写，不能建表/角色；bi_reader 仅 reporting 指定视图
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA bi TO bi_sync;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA bi TO bi_sync;

GRANT USAGE ON SCHEMA reporting TO bi_reader;
GRANT SELECT ON reporting.v_payments, reporting.v_refunds,
                reporting.v_coverage, reporting.v_shops TO bi_reader;
