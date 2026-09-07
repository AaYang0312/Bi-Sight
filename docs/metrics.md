# 指标口径与来源对账

数据依据：[快麦复核报告](../superpowers/research/2026-09-06-kuaimai-data-verification.md)、[脱敏实测统计](2026-09-06-kuaimai-data-recheck.json)。本文是「可执行对账清单」：来源表记录接口、字段路径、单位、粒度、状态规则、抽样范围、启用状态和证据日期。**启用状态为「未启用/待对账」的项，代码与页面不得当作可用能力。**

## 1. 数据来源表

| 数据 | 官方方法 | 状态 | 证据日期 | 抽样范围 |
| --- | --- | --- | --- | --- |
| 店铺 | `erp.shop.list.query` | 待对账（任务4） | 2026-09-06 | 42条，33条启用，多平台 |
| 普通订单（非淘系/拼多多） | `erp.trade.list.query` | 待对账（任务4） | 2026-09-06 | 抖音一店第一页20条 |
| 售后工单 | `erp.aftersale.list.query` | 待对账（任务4） | 2026-09-06 | 抖音14条 |
| 商品档案 | `item.list.query` | 后续扩展 | 2026-09-06 | 3个启用商品 |
| 商品SKU | `erp.item.sku.list.get` | 后续扩展 | 2026-09-06 | 8个SKU |
| 当前库存 | `stock.api.status.query` 等 | 不在本版范围 | 2026-09-06 | 3条SKU样本 |
| 历史成本 | `erp.item.history.cost.price.query` | 禁用（成功空结果≠无成本） | 2026-09-06 | 成交SKU无记录 |
| 采购金额及采购明细 | `purchase.order.query` | 禁用（文档单位为分，本版不接入） | 2026-09-06 | 空 |
| 推广消耗 | 无公开方法 | **未证实，禁用** | 2026-09-06 | 增值报表需联系实施取得授权 |

## 2. 字段规范化规则（按完整路径）

| 字段 | 规范化规则 |
| --- | --- |
| 订单 `payAmount/payment/platformPaymentAmount` | 分别为买家已付/应付/平台支付，均独立保留；不得互换 |
| 订单 `updTime` / `modified` | 前者为ERP数据更新时间，后者为平台修改时间；对 `upd_time` 增量先核对二者含义和样本，不直接把后者当ERP版本 |
| 订单 `cost` / `orders[].cost` / `orders[].suits[].cost` | 分别为总成本/普通行单位成本（需×num）/部分套件子结构总成本，分别标注；不统一乘数量 |
| 售后 `rawRefundMoney` / `items[].rawRefundMoney` | 单头是元，商品明细是分；首版退款聚合仅使用单头，商品退款暂不开放 |
| 售后 `onlineStatus=7` + `platformCompleteTime` | 平台退款成功候选条件；还需检查工单作废/合并、平台售后号去重 |
| 采购 `totalAmount/actualTotalAmount`、明细 `price/amount` | 文档为分；本版不接入，不得误复用订单转换函数 |
| `orders[].itemSysId/skuSysId` | 显式映射为商品查询的 `sysItemId/sysSkuId`；不按名字模糊匹配 |
| 订单 `grossProfit` | ERP毛利参考；实际运费/包材/平台扣费缺失，不得称净利润 |
| 拼多多 `payAmount/payment/modified` | 样本全部缺失；不得纳入支付指标，也不用其他字段反推 |

## 3. 指标口径（任务5实现后与人工答案对账）

业务时区 `Asia/Shanghai`；内部范围 `[start, end)` 排他。

| 指标 | 口径 | 来源依赖 |
| --- | --- | --- |
| `paid_amount` | 已验证商业订单支付金额之和（`order_payments.verified`，人民币） | orders/order_payments |
| `paid_orders` | 已验证商业订单数（一行一商业单） | order_payments |
| `erp_documents` | ERP单据数（拆合单粒度，仅作对账参考，不作客单价分母） | orders |
| `aov` | paid_amount / paid_orders（总口径，不平均每日） | 同上 |
| `refund_amount` | 平台退款成功（`platform_success` 且 `refund_canonical`）按完成时间归属的发生额 | aftersales_occurrence |
| `cash_difference` | paid_amount − refund_amount；**期间收支差额，不是净利润** | 上两者 |
| `cohort_refund_rate` | 同批：`[start,end)` 支付商业单在明确截止时刻前的累计退款 / 同批支付额 | aftersales_cohort + 原单匹配 |
| `quantity` / `product_paid_amount` | 有效销售父行数量与已核验行级分摊金额 | order_items |
| 推广费率/ROAS | **未启用**：无实耗来源；折扣/成本不得替代广告费 | 无 |

## 4. 已知功能门槛

- 平台实退与系统实退分开；系统退款成功指标未对账前不发布。
- 同批退款率需原单匹配完成；未匹配退款返回缺数据并显示数量。
- 「最近7天」= 最近7个完整自然日；「今天」未完成，返回缺数据而非0。
- 日期跨度最多366天；结果最多500组。
- 真实推广实耗、成本贡献、淘系/拼多多完整支付指标均有数据门槛，未满足时明确不可用。

## 4.1 推广预算情景测算口径（promotion.py）

- 输入只接受当前用户明确输入（页面表单或问题中明确假设）；模型不能把ERP成本/优惠解释为实耗，也不能沿用上一轮参数。
- `sales_cap`：上限 = 假设销售额 × 假设费用率（费用率≤100%）；预测销售不达预期时阈值需调整。
- `budget_scenario`：剩余 = max(0, 预算-已花)；超支单列 = max(0, 已花-预算)；日均 = 剩余/剩余天数（排他截止日起算），周期结束不除零。
- `actual_budget`/`contribution_cap`：一律 missing_data——真实费用率=同期实耗/同期有效支付（分母0不可计算），预算进度要求费用完整覆盖到昨日，贡献上限 max(0, C-P)；均未具备输入，不实现分支。
- 假设结果 `basis=用户输入假设`，`coverage.status=missing`（无费用实绩源）、`data_as_of=None`；不得写成“账号实际剩余额度”，也不得称店铺收入/费用为广告归因ROAS。

## 5. 真实对账结果摘要

### 合成基准（已通过，冻结时刻 2026-09-08 09:00+08）

`tests/test_db.py` 的 `seed_business_case` 覆盖 2026-08-25至2026-09-08，含拆合单、跨期退款、部分退款、待处理/关闭工单；人工答案与系统输出一致：

| 断言 | 人工答案 | 结果 |
| --- | --- | --- |
| `[09-01,09-08)` 支付/商业单/ERP单 | 1000元 / 6 / 6 | ✅ 一致 |
| 客单价（总口径） | 166.67（1000/6） | ✅ 一致 |
| 退款发生 / 期间收支差 | 100元 / 900元 | ✅ 一致 |
| 同批退款率（截至09-08 00:00） | 50/1000 = 5% | ✅ 一致 |
| 商品A/B 金额/数量 | 600元/7件，400元/4件 | ✅ 一致 |
| 每日趋势（含真实0） | 500/100/200/0/200/0/0 | ✅ 一致 |
| 上期 `[08-25,09-01)` 与增长 | 500元，+500（+100%） | ✅ 一致 |
| 09-02粒度 | 商业单1、ERP单2 | ✅ 一致 |
| 未匹配成功退款 | missing_data + 数量提示 | ✅ 符合 |
| 未授权店铺/注入串 | forbidden，无副作用 | ✅ 符合 |
| 09-04真实0 vs 超覆盖日期 | 0 与 missing_data 区分 | ✅ 符合 |
| 0分母 | NULL + 不可计算说明 | ✅ 符合 |

### 真实店铺对账（待任务4.7执行）

（待试点一店一天真实 probe 与后台报表核对后填写：差异表与口径确认。）
