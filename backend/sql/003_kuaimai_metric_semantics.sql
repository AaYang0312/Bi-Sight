-- 关闭但已付款订单保留支付事实；商品排行保留套件/组合/加工父项并标注行性质。
-- 必须在 002_kuaimai_mapping_repair.sql 后执行。

CREATE OR REPLACE VIEW reporting.v_product_daily AS
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
GROUP BY shop_id, day, product_id, line_kind;
