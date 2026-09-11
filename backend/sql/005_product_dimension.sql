-- 商品档案维表：把订单行里的快麦 itemSysId 对应到可读名称。
-- 只由管理员执行；必须在 003_kuaimai_metric_semantics.sql 之后执行。
--
-- 字段来源为 2026-09-11 对 item.list.query 的只读核验（total=435，结果键 items，
-- 字段 sysItemId/title/outerId/type/activeStatus/itemCategoryNames/purchasePrice/modified）。
-- 实测同一公司 90 天订单里的 83 个 product_id 全部命中档案，未命中 0 行。

CREATE TABLE IF NOT EXISTS bi.products (
  product_id           text PRIMARY KEY,
  title                text NOT NULL DEFAULT '',
  outer_id             text,
  item_type            text,
  category             text,
  active               boolean NOT NULL DEFAULT true,
  purchase_price       numeric(20,6),
  normalization_status text NOT NULL DEFAULT 'normal',
  source_modified_at   timestamptz,
  synced_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT products_normalization_status CHECK (
    normalization_status IN ('normal', 'needs_review', 'invalid'))
);

COMMENT ON COLUMN bi.products.product_id IS
  '快麦商品档案 sysItemId；与 bi.order_items.product_id（订单行 itemSysId）显式同名映射。';
COMMENT ON COLUMN bi.products.title IS
  'ERP 商品名称（档案 title）。档案未维护货号：实测 outerId 与 title 相同，故不参与展示名判定。';
COMMENT ON COLUMN bi.products.purchase_price IS
  '档案当前成本，仅供内部参考；禁止出现在任何 reporting 视图或工具结果列白名单中。';
COMMENT ON COLUMN bi.products.normalization_status IS
  'normal/needs_review（档案无名称，展示为未建档）/invalid（无 sysItemId，不入库）。';
COMMENT ON COLUMN bi.products.source_modified_at IS
  '档案 modified（毫秒）；较旧版本不得覆盖较新版本，与订单 replay 同规则。';

-- 商品日聚合：追加档案名称。名称只在 reporting 层出现，ERP 数字ID 仍留在 bi.*。
CREATE OR REPLACE VIEW reporting.v_product_daily AS
SELECT daily.*, product.title AS product_name
FROM (
  SELECT shop_id,
         (paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
         product_id,
         sum(quantity) AS quantity,
         sum(gift_quantity) AS gift_quantity,
         sum(allocated_paid_amount) AS product_paid_amount,
         bool_and(allocation_verified) AS allocation_verified,
         line_kind
  FROM bi.order_items
  WHERE active AND line_kind <> 'gift' AND product_id IS NOT NULL
    AND allocated_paid_amount IS NOT NULL
  GROUP BY shop_id, day, product_id, line_kind
) daily
LEFT JOIN bi.products product ON product.product_id = daily.product_id;

-- 权限：bi.products 是新表，001 的“ON ALL TABLES IN SCHEMA”不覆盖后建表，必须逐项授权。
GRANT USAGE ON SCHEMA bi TO bi_sync;
GRANT SELECT, INSERT, UPDATE ON bi.products TO bi_sync;
REVOKE ALL ON bi.products FROM PUBLIC;
REVOKE ALL ON bi.products FROM bi_app;
