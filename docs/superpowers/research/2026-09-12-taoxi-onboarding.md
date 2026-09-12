# 淘系订单/售后接入复核（非敏感出库通道）

日期：2026-09-12 ｜ 关联：`2026-09-06-kuaimai-data-verification.md` ｜ 分支：`taoxi/outstock-sync`

## 结论

1. **淘系（tb/tm）订单已经 `erp.trade.outstock.simple.query`（交易模块·销售出库查询）以非敏感字段口径接入 `bi.orders` / `bi.order_items` / `bi.order_payments`**，与抖音 `erp.trade.list.query` 通道并存，`sync_state` 按 `(source, entity, shop_id)` 主键隔离，互不干扰。
2. **淘系售后走既有 `erp.aftersale.list.query` 通道直接可用**，无需改动路由（实测见下表；`tid/sid/rawRefundMoney/refundMoney/platformCompleteTime` 非空率高）。
3. **口径限制（必须向使用者声明）**：淘系订单是 **ERP 销售出库口径，不是平台账单口径**。收件人姓名/手机/地址/省市区/街道/邮编、`buyerNick`、`buyerMessage`、发票、`taobaoId`、`platformPaymentAmount`、`ptConsignTime` 均不返回或为敏感字段；出库响应携带的 `shopName/sellerNick/openUid/mobileTail` **一律不入库**。实付、成本、毛利、佣金、邮费、状态、商品行齐全，可支撑经营分析；不宣称财务对账完成。
4. 平台→源路由为模块常量 `ORDER_SOURCE_BY_PLATFORM = {"tb": OUTSTOCK_SOURCE, "tm": OUTSTOCK_SOURCE}`（`_shop_order_source` 查 `bi.shops.platform` 解析，缺档案直接报错防假覆盖），其余平台（含未知）回退 `erp.trade.list.query`；同步前先跑 `sync shops`。

## 1. 实测结果（测试库 bi_agent_test@127.0.0.1:54329）

- 环境：worktree `taoxi-sync`，`.env`（凭证）+ `BI_WRITER_DSN=bi_sync@…`，`BI_SHOP_IDS` 为 12 家淘系店。
- 命令序列：`sync shops`（42 店幂等）→ `probe 2026-08-15..16`（166520，38 单）→ `backfill --days 30`（12 店）→ `incremental`（12 店）→ `reconcile --days 3`（12 店）。
- 并发说明：本任务期间同一 worktree 未被第二会话提交（已核实）；若后续多会话共用 worktree，写入均为幂等 upsert + `covered` 区间并，无双写脏数据，人工复核可用 batch_id 区分轮次。
- 全部门店 `last_error_code` 为空；三实体 `covered` 均为单一连续区间（未出现破碎多段，说明回填+增量+对账链路在 30 天内完整相接）。

### 1.1 每店订单/支付/售后规模

窗口：`backfill --days 30`（覆盖 2026-08-13 → 2026-09-12）+ `incremental` + `reconcile --days 3`；三实体（orders / aftersales_occurrence / aftersales_cohort）每店 `covered` 均为**单一连续区间** [08-13, 09-12]，`last_error_code` 全空，水位推进到增量运行终点。

| shop_id | 平台 | 店铺 | orders | payments | verified | verified% | 售后行 | 售后matched | 售后canonical | canonical金额(¥) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 166517 | tb | 宝威德滤纸直销 | 3437 | 3470 | 3211 | 92.5% | 259 | 40 | 226 | 10436.24 |
| 166520 | tb | 元发钉枪五金工具 | 1303 | 1379 | 1101 | 79.8% | 199 | 46 | 172 | 30109.93 |
| 166647 | tb | 沃金五金机电商城 | 617 | 657 | 539 | 82.0% | 84 | 24 | 73 | 27674.08 |
| 166650 | tb | 滤纸滤布工厂 | 424 | 429 | 396 | 92.3% | 28 | 2 | 27 | 1282.69 |
| 166684 | tb | 宝威德滤纸 | 474 | 478 | 439 | 91.8% | 38 | 3 | 37 | 4060.77 |
| 166685 | tb | 木工钉枪批发 | 246 | 254 | 218 | 85.8% | 33 | 5 | 29 | 3446.71 |
| 166693 | tb | 元发工具工厂 | 230 | 236 | 193 | 81.8% | 38 | 5 | 36 | 4555.18 |
| 167166 | tb | 装修吊顶工具 | 136 | 139 | 108 | 77.7% | 32 | 4 | 27 | 3487.39 |
| 186607 | tb | 沃金劳保用品企业店 | 898 | 899 | 897 | 99.8% | 0 | 0 | 0 | 0 |
| 900007148 | tb | 沃金数码商城 | 15 | 15 | 9 | 60.0% | 7 | 1 | 7 | 1386.80 |
| 166687 | tm | 沃金五金专营店 | 400 | 423 | 331 | 78.3% | 64 | 7 | 61 | 12211.77 |
| 900453539 | tm | 元发旗舰店 | 187 | 187 | 155 | 82.9% | 39 | 6 | 34 | 7576.94 |
| **合计** | | | **8367** | **8566** | **7597** | **88.7%**（¥810,511.40） | **821** | **143** | **729** | **¥106,228.50** |

注：`paid_at` 最小值早于窗口起点（如 166647 至 2026-07-02）——出库接口按修改时间命中近 30 天窗口、回传更早付款的历史在途/完结单，属正常（记录本身完整，`data_as_of` 与 `covered` 仍按窗口口径）。

### 1.2 状态与规范化分布

| 指标 | 值 |
|---|---|
| normalization_status | 全部 `normal`（8367/8367）；`needs_review`=0，`invalid`=0 |
| active | 7759（92.7%）；非活跃（TRADE_CLOSED 等）608（7.3%） |
| split_parent_id | 本窗口 0 行（实测该批无拆单命中；逻辑已由单测覆盖） |
| 逐日断层（08-15→09-12） | 11/12 店零订单天数 = 0；900007148 沃金数码商城 21 天无单（全窗口仅 15 单的小店，非断档） |
| 抽查 5 单（166520，08-15） | 金额链路 payAmount=payment、cost、grossProfit=payment−cost−postFee 自洽；closed→active=false ✓ |
| quality_ok | 全部 false —— 与存量抖音店一致，`sync_state.quality_ok` 为人工质检占位列，同步代码从不写入，非新通道缺陷 |

## 2. 实施要点与踩坑记录

1. **分页复用**：出库通道直接复用 `_fetch_orders_cursor` / `_fetch_orders_paged`，仅把 method 参数化；响应形状 `{pageNo, pageSize, total, list}` 与交易查询一致。
2. **增量语义**：`timeType=upd_time + queryType=0`；回填 `timeType=pay_time` + queryType 0/1 双通道（3 个月内外），窗口 ≤1 天，与抖音通道相同逻辑。
3. **`split_parent_id` ← `splitSid`**：出库接口无 `splitParentId` 字段；`splitSid` 为"拆单主单 sid"，`-1`/空视为无拆单（已实测本窗口全部无值，逻辑按文档处理）。
4. **回填水位（本次修正）**：旧实现在回填开始前取 `t1 = now`，且 12 店共用同一时间戳，回填结果不落水位（data_as_of 有值、watermark 保持 epoch）。改为每店回填写完后取 `t1 = now`：回填窗口以 `pay_time` 分片不产生 upd_time 口径的连续覆盖，水位不动；随后的 ≤1 天修改时间扫描（queryType 0/1）落入 `covered` 并把水位推进到扫描窗口终点（实测水位 = 扫描窗口终点，见 §1.1 注）。
5. **出库 `orders[]` 无 `skuOrderId`**：商品行回退使用 `skuId` 作为 `sku_order_id`，保证抖音"同 skuSysId 多行"的邮费分摊判别对淘系同样成立（淘系该键常空 → 单组 → 全额邮费记主组）。
6. **出库响应 `created` 可能为 null**：缺 `payTime` 时按缺时间判 invalid，不依赖 created。
7. **PII 红线（守护用例固化）**：`PII_FORBIDDEN_FIELDS` 共 24 项：收件人 10（receiverName/Phone/Mobile/Address/State/City/District/Street/Zip/Country）+ 买家 4（buyerNick/buyerMessage/buyerName/buyerPhone）+ 发票 4（invoiceName/Remark/Kind/tradeInvoice）+ taobaoId/ptConsignTime + 出库响应实际携带的 shopName/sellerNick/openUid/mobileTail；规范化入口命中即整单 invalid 且不写日志；表结构本身无这些列，测试防止未来扩列违约。**入库字段集合 = 现有列，一个未加**。
8. **金额单位为元**（探针 `payAmount="16.90"` 这类字符串），`to_decimal` 直接兼容；分/元换算仅适用于推广费用接口，与订单无关。
9. **抖音 `erp.trade.list.query` 排除淘系订单**（文档明示），因此淘系唯一非敏感订单通道是出库接口；奇门/方舟敏感通道不在范围内。

## 3. 与 09-06 复核的差异/冲突

- 09-06 快照中出库响应 `list[]` 仅示例性列了 8 个头字段；本次实测单头 82 键，`status/sysStatus/userId/shopName/orders[]` 均非空，以实测为准。
- 其余（淘系敏感字段缺失、售后可用、店铺/商品/库存不分平台）与 09-06 结论一致。
- **新发现（文档 vs 实测冲突，以实测为准）**：出库接口回填窗口 `timeType=pay_time` 声称按付款时间过滤，但实测命中了付款时间早于窗口起点近 6 周的记录（如 166647 店 2026-07-02 付款）——疑似对未完结/近期修改的订单服务端同时按修改时间匹配。影响：入库记录自身字段完整、无脏数据（幂等 upsert 兼容），仅回填窗口语义偏宽；已在 §1.1 注明，建议 reconcile/probe 抽样时按 `paid_at` 而非窗口判断预期行数。

## 4. 证据与追溯

- 代码：`bi_agent/sync.py`（`ORDER_SOURCE_BY_PLATFORM`、`_shop_order_source`、`normalise_trade(..., source=)`、`_fetch_orders_cursor/_fetch_orders_paged(method=)`、PII_FORBIDDEN_FIELDS 红线、回填水位修正），提交 `1e2b119` / `03d7713` / `161b3aa`。
- 测试：`tests/test_core.py`（源路由、出库规范化、PII 守护）、`tests/test_db.py`（出库落库 + 双通道状态隔离），`uv run --env-file .env.test python -m pytest tests/ -x -q` 全绿（104 passed, 12 subtests passed）。
- 官方文档快照：`D:\Projects\bi-agent\logs\kuaimai-llms-full-fresh.txt` §销售出库查询（L11182 起）、§售后工单查询（L15575 起）。
- 只读探针（2026-09-12）：`D:\Projects\bi-agent\logs\probe_tb.py` / `probe_tb2.py` / `probe_tb3.py`。
- 验证 SQL：`logs/verify_taoxi.sql`（本 worktree，git 忽略目录内）。

## 5. 未尽事项 / 人工复核点

- 拼多多订单需方舟 appkey，未接。1688（`1688`/`alibabac2b`→`alibabac2m`）本次范围外。
- 页面/Agent 侧对 12 家淘系店的开放与否是后续人工决定（metrics 层 source 过滤尚未纳入 outstock 源，见分支待办）。
- 长尾回填：出库 queryType=0 仅覆盖近 3 个月，更早订单需要 queryType=1 归档窗口回填（本次 30 天范围内未受影响）。
- 建议排期：每日 `incremental`（orders×2 通道 + aftersales occurrence/cohort），每周 `reconcile --days 3`，每月 `replay --start <月末-40d> --end <月末>` 清理退款迟到/补发/换货。
