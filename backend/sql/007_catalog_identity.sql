-- 目录身份层：持久 opaque 引用、目录版本、成交名称快照。
-- 必须在 005_product_dimension.sql 之后执行。
--
-- 口径：模型侧只看 ref，真实名称只在授权展示层出现（见
-- docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 2）。
-- ref 由 (kind, natural_key) 稳定派生并落表，用于反查；ERP 数字主键不出后端。

CREATE TABLE IF NOT EXISTS bi.entity_refs (
  kind        text NOT NULL,
  natural_key text NOT NULL,
  ref         text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, natural_key),
  CONSTRAINT entity_refs_kind CHECK (kind IN ('shop', 'product', 'sku')),
  CONSTRAINT entity_refs_ref_format CHECK (ref ~ '^ent-[0-9a-z]{8}$')
);

-- ref 全局唯一：哈希截断若相撞必须报错，不能让两个实体共用一个引用。
CREATE UNIQUE INDEX IF NOT EXISTS entity_refs_ref_unique_idx ON bi.entity_refs(ref);

COMMENT ON COLUMN bi.entity_refs.ref IS
  '不透明引用 ent-xxxxxxxx，由 (kind,natural_key) 稳定派生；不是授权凭证，筛选仍要复核授权范围。';
COMMENT ON COLUMN bi.entity_refs.natural_key IS
  'ERP 侧主键（店铺 shop_id / 商品 sysItemId / SKU sysSkuId）；只在库内出现。';

-- 目录版本：名称或档案发生变化时递增，供 Artifact 记录它解析时用的目录版本。
CREATE TABLE IF NOT EXISTS bi.catalog_state (
  id         smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  version    bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO bi.catalog_state(id, version) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;

-- 成交名称快照：订单响应实测带 sysTitle / sysSkuPropertiesName，不额外调用接口。
-- 档案改名后用快照留住“当时成交叫什么”，但展示优先级仍是档案 > 快照。
ALTER TABLE bi.order_items
  ADD COLUMN IF NOT EXISTS product_name_snapshot text,
  ADD COLUMN IF NOT EXISTS sku_label_snapshot text;

COMMENT ON COLUMN bi.order_items.product_name_snapshot IS
  '成交时快麦订单行 sysTitle 原样值；缺失保持 NULL，不用档案或平台标题反推。';
COMMENT ON COLUMN bi.order_items.sku_label_snapshot IS
  '成交时快麦订单行 sysSkuPropertiesName 原样值；与商品名分列，不合并成一个展示串。';

-- 商品日聚合：档案名称与成交快照都原样给出，列序写定当契约；
-- 取用优先级只在 catalog 一处实现，避免 SQL 与 Python 两边漂移。
CREATE OR REPLACE VIEW reporting.v_product_daily AS
SELECT daily.shop_id, daily.day, daily.product_id, daily.quantity, daily.gift_quantity,
       daily.product_paid_amount, daily.allocation_verified, daily.line_kind,
       product.title AS product_name,
       daily.product_name_snapshot AS product_name_snapshot
FROM (
  SELECT shop_id,
         (paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
         product_id,
         sum(quantity) AS quantity,
         sum(gift_quantity) AS gift_quantity,
         sum(allocated_paid_amount) AS product_paid_amount,
         bool_and(allocation_verified) AS allocation_verified,
         line_kind,
         max(product_name_snapshot) AS product_name_snapshot
  FROM bi.order_items
  WHERE active AND line_kind <> 'gift' AND product_id IS NOT NULL
    AND allocated_paid_amount IS NOT NULL
  GROUP BY shop_id, day, product_id, line_kind
) daily
LEFT JOIN bi.products product ON product.product_id = daily.product_id;

-- 目录版本对聊天 API 可读：Artifact 要记录它解析名称时用的目录版本（后续缓存失效用）。
-- 只暴露单行版本，不暴露 bi.entity_refs 里的引用→ERP主键映射。
CREATE OR REPLACE VIEW reporting.v_catalog_version AS
SELECT version, updated_at FROM bi.catalog_state WHERE id = 1;

-- 权限：001 的“ON ALL TABLES IN SCHEMA”不覆盖后建表，必须逐项授权。
GRANT SELECT, INSERT, UPDATE ON bi.entity_refs TO bi_sync;
GRANT SELECT, UPDATE ON bi.catalog_state TO bi_sync;
GRANT SELECT ON bi.products TO bi_sync;
GRANT SELECT ON reporting.v_catalog_version TO bi_reader, bi_app;
REVOKE ALL ON bi.entity_refs, bi.catalog_state FROM PUBLIC;
REVOKE ALL ON bi.entity_refs, bi.catalog_state FROM bi_app;
