# 真实环境部署与同步验收记录（2026-09-11）

范围：把代码部署到真实宿主、建真实库、跑真实快麦同步、按 runbook 回填/重放/对账并核对回款总额。
执行机器：Mac（代码与运行入口）→ SSH 隧道 → IBN5100 上的 PostgreSQL 17.6。

## 1. 环境事实（先纠正错误假设）

- 原以为存在 `100.88.1.15:5432` 的生产库：**不存在**。该地址 ping 不通，也不在当前 tailnet；
  `ibn5100-2` 的现址是 `100.115.77.125`。
- IBN5100 上的 PostgreSQL 监听 `127.0.0.1:54329` 与 `100.115.77.125:54329`（**不是 5432**）；
  tailscale 口直连被丢包，因此走 `ssh -L 5433:127.0.0.1:54329 home-win` 隧道。
- 该实例上只有 `bi_agent_test`，且是**旧 schema 的真实数据**（42 店 / 9414 单，
  但无 `bi.products` / `bi.entity_refs` / `bi.sync_batches`）。
- 因此本轮**新建** `bi_agent` 作为生产库，未改动 `bi_agent_test`。
- `D:\Projects\bi-agent` 是 `main @ 06a5cc2` 的旧副本（还在用 `BI_READER_DSN` 旧命名），
  没有 005/007/008 与本轮修正，**没有用它跑同步**；同步用的是 Mac 上当前分支代码。
- 授权店铺范围取自 `.env.sync`：只有 `166754`（元发钉枪(抖音)）。

## 2. 建库与迁移

`CREATE DATABASE bi_agent` 后按 runbook 顺序执行，**一次全部通过**：

`001 → 002 → 003 → 004 → 005 → 007 → 008`（006 已作废不补）

落地校验：`bi.products`、`bi.entity_refs`、`bi.catalog_state`、`bi.sync_batches`、
`bi.query_runs` 均存在；`order_items` 带 `product_name_snapshot` / `sku_label_snapshot`；
`sync_state` 带 `quality_status` / `quality_rule` / `quality_checked_at` / `quality_reason`。

## 3. 快麦凭证与真实同步

- `probe --start 2026-09-09 --end 2026-09-10`：64 单 / ¥2,681.34 / 售后 10 / 平台成功退款 9（¥239.50）→ 凭证有效。
- `shops` → 42 家；`products` → 435 档案，0 跳过 0 无效。
- `backfill --days 7` → 725 单、70 售后、7 个 cohort 窗口。
- `backfill --days 90` → 10,162 单、575 售后、90 个 cohort 窗口，耗时约 11 分钟。
- `replay --entity orders --start 2026-06-13 --end 2026-09-12` → 10,261 单，约 8 分钟。
- `replay --entity aftersales_occurrence` 同上区间 → 1,053 条。
- `reconcile --days 7`（两次）。
- `incremental` → 13 单（含回填截止后才到达的 2 单）。

**全程 `payment_downgrade_blocked = 0`**：没有任何已核验收入被清零。

## 4. 回款总额核对（runbook 要求的精确比较）

对整段区间 `[2026-06-13, 2026-09-12)` 北京时间、按支付时间：

| 来源 | 单数 | 金额 |
| --- | --- | --- |
| ERP 在线通道翻页（`queryType=0`，取完 total） | 9,492 | 733,415.520 |
| 库内 `bi.orders.raw_pay_amount` | 9,492 | 733,415.520000 |

**逐元一致 ✓**

过程中出现过两处"看似不一致"，都已定位、都不是丢数据：

1. replay 相对回填 +44 单 / +¥3,731.62：回填截止 17:28， replay 覆盖到整日；
   其中 44 单支付时间都在截止前但**当时 ERP 尚未产出该记录**（支付时间字段晚于入库时间），
   属后到数据，正是增量按 `upd_time` 扫描要解决的场景。
2. 总量核对时出现"ERP 有、库内无"2 单：`payTime` 分别是 **17:55:04 与 18:04:19**，
   而 replay 于 17:53:42 结束——是核对期间新产生的订单，
   金额 12.80 + 7.65 = 20.45，与当时差额**分毫不差**；随后 `incremental` 已收进。

按日数量核对：8/20–9/10 共 32 天，**ERP total 与库内单数不一致天数 = 0**。
归档通道独占日三边核对：6/14 与 6/17 的 `在线 = 归档 = 库内` 数量与金额**逐元相同**；
8/25、9/3 归档通道为空（数据仍在线，符合保留期）。

## 5. verified 分布与不变式

| basis | verified | 单数 | 金额 |
| --- | --- | --- | --- |
| `head` | true | 9,189 | 699,329.49 |
| `items_merged` | true | 678 | 67,823.52 |

- **`undetermined` 为 0**：`verified=false` 的支付记录一条都没有。
- `items_merged` 是本轮合单取证修正的成果：这 **678 单 ¥67,823.52** 在旧规则下会整笔变 NULL，
  从店铺收入里消失。replay 前后该桶数量与金额**完全不变**，说明分类对重放稳定。
- 全库不变式成立：**行分摊合计 == 已核验支付合计 == 767,153.01**。
- 活跃行中 `allocation_verified=false` 的为 **0**；29 行为 false 全部是 `active=false`
  的关闭行（`rebuild_payments` 的置真语句带 `AND active`，关闭行不参与指标，故不盖核验章）。

## 6. 质量状态与批次凭证（Task 1 的真实闭环）

```
aftersales_cohort      unknown   （无规则版本）   ← 没有批次凭证，拒绝自称已核验
aftersales_occurrence  passed    kuaimai-reconcile/1
orders                 passed    kuaimai-reconcile/1
```

批次凭证按窗口口径正确分离：

| window_kind | mode | 批次数 | 行数 |
| --- | --- | --- | --- |
| business | backfill | 194 | 11,532 |
| business | reconcile | 28 | 1,448 |
| business | replay | 182 | 11,314 |
| modified | incremental | 2 | 13 |
| modified | scan | 4 | 1 |

真实查询侧表现：只要 orders / aftersales_occurrence 的查询**不再出现**质量警告；
需要 `cohort_refund_rate`（依赖 cohort）的查询仍提示「来源质量未核验」——
披露按依赖项精确生效，不是一刀切。

## 7. 名称、引用与泄露

`reporting.v_product_daily`：1,490 行商品日数据，**1,490 行有档案名（100%）**，
**749 行有 SKU 规格（50%）**——此前旧数据是 0 行有快照，规格链路在真实数据上首次生效。

7 天商品 Top5 投影检查：模型载荷**不含任何真名、不含 ERP 主键**；
展示层给出真名与规格（如 `气动工具油-元发` + `气动工具油-元发120ML*1瓶`，
来源 `archive`）；`ent-6ce82c30` 草稿改写为「第一名 钢钉枪-元发，支付 6873.85，销量 26」。

## 8. 本轮真实数据暴露并已修的缺陷

**行数上限提示未登记公开词表**（`c182116` 引入，先前从未触发）：
90 天商品分组查询命中 `MAX_ROWS=500` 后，
「结果行数达到500上限，已拒绝出数以避免静默截断；请缩小日期范围或店铺范围」
不在 `_PUBLIC_LIMITATION_PATTERNS` 里，`validate_model_payload` 抛
`unsafe_persistence_payload`，查询图把它误报成 `result_contract_violation`
（"查询结果异常。"），操作者看不到"该缩小范围"的可执行建议。
已登记词表并归因到 `result_too_large`（与日分组超限同一码）。

## 9. 仍未解决 / 明确未执行

- `aftersales_cohort` 仍是 `unknown`：`check_cohort_window` 不写批次凭证
  （它没有 `mode` 参数，属刻意留下的接缝），所以 cohort 相关指标只能按未核验披露。
- **23 条平台成功退款未匹配到原单**，退款类指标被既有守卫拒绝出数（正确行为，需补拉原单）。
- 关闭订单行造成 **¥101,536.34** 未计入商品维度（占该区间支付额约 13%）。
  本轮把它变成显式披露，但"关闭单收入要不要进商品维度"仍是待决业务口径。
- 五平台就绪取证**未执行**：只有抖音一家在授权清单内，
  淘宝/拼多多/京东/快手 仍是「未执行」，不写"可用"。
- 真实模型 20 题验收**未执行**（`acceptance` 本轮只跑 `--offline` 20/20）。
- 生产库尚未接流量：`.env.app` 仍指向测试库，切换与 API 部署未做。
