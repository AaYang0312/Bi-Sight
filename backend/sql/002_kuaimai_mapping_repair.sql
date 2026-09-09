-- 修复快麦字段映射。先在已有库执行本迁移，再部署同步代码并 replay 历史范围。
-- 只新增可复核的状态/类型字段；不创建售后行或套件子件等第二期明细表。

ALTER TABLE bi.orders
  ADD COLUMN IF NOT EXISTS unified_status text,
  ADD COLUMN IF NOT EXISTS system_status text;

ALTER TABLE bi.order_items
  ADD COLUMN IF NOT EXISTS source_type integer;

COMMENT ON COLUMN bi.orders.unified_status IS
  '快麦 trade.unifiedStatus 原样值；CLOSED 使订单 inactive。';
COMMENT ON COLUMN bi.orders.system_status IS
  '快麦 trade.sysStatus 原样值；仅 unified_status 缺失时作为 active 回退。';
COMMENT ON COLUMN bi.order_items.source_type IS
  '快麦 trade.orders[].type 原始枚举；赠品判定仍以 gift_quantity > 0 为准。';
