# 指标口径与来源对账

> 2026-09-12 修订：淘系 tb/tm 已通过非敏感出库通道接入统一 main，但查询侧单源覆盖、退款硬门禁与跨平台 basis 尚待原路线图 Task 5 实现。当前运行限制仍有效；目标合同以 [多来源指标设计](superpowers/specs/2026-09-12-multi-source-metrics-design.md) 为准。关闭但已付款单保留 active=false，独立认证支付事实并匹配退款；paid_amount 是退款前支付，不能叫净支付。未来退款未匹配降级为可答+比例声明，不能直接把本页历史拒答测试当新验收预期。

| 当前整合状态 | 查询发布条件 |
| --- | --- |
| fxg 等已核验交易源 | 保留现有认证与覆盖，逐店可用性取实际状态 |
| tb/tm 出库同步已合并 | 先完成来源路由门禁、时间语义核验、退款诊断与ERP口径标签；不能宣布平台账单GMV完整 |
| pdd 有单据但无支付金额 | 方舟获批及金额/时间/退款认证前，任务5须以capabilities阻止支付指标 |

数据依据：[快麦复核报告](superpowers/research/2026-09-06-kuaimai-data-verification.md)、[脱敏实测统计](superpowers/research/2026-09-06-kuaimai-data-recheck.json)。本文是「可执行对账清单」：来源表记录接口、字段路径、单位、粒度、状态规则、抽样范围、启用状态和证据日期。**启用状态为「未启用/待对账」的项，代码与页面不得当作可用能力。**

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
| 订单 `unifiedStatus` / `sysStatus` | 两者原样保留；`unifiedStatus=CLOSED` 时订单不参与 ERP 有效单与商品父项统计，仅在它缺失时以 `sysStatus=CLOSED` 回退。平台 `status` 是平台码，不参与此判定；若已有有效支付时间和正支付额，支付事实仍保留，以便退款从已收款中抵扣 |
| 订单 `splitType` / `splitSid` | 仅 `splitType=1` 时，将 `splitSid` 写入 `split_parent_id` |
| 订单 `orders[].oid` / `orders[].type` / `orders[].giftNum` | `oid` 为平台行号；`type` 原样保留为 `source_type`。行性质：仅 `num=0` 且 `giftNum>0` 才整行判为 `gift`；`num>0` 且 `giftNum>0` 的混合行保留 `sale`（官方以 `giftNum` 判是否赠品，但销售数量与该行分摊金额不得因此从商品排行消失），赠品数量由 `gift_quantity` 单列承载。商品排行保留套件/组合/加工**父项**并输出 `line_kind`，不把它们表述为SKU子件排行 |
| 订单 `updTime` / `modified` | 前者为ERP数据更新时间，后者为平台修改时间；对 `upd_time` 增量先核对二者含义和样本，不直接把后者当ERP版本 |
| 订单 `cost` / `orders[].cost` / `orders[].suits[].cost` | 分别为总成本/普通行单位成本（需×num）/部分套件子结构总成本，分别标注；不统一乘数量 |
| 售后 `rawRefundMoney` / `items[].rawRefundMoney` | 单头是元，商品明细是分；首版退款聚合仅使用单头，商品退款暂不开放 |
| 售后 `onlineStatus=7` + `platformCompleteTime` | 平台退款成功候选条件；还需检查工单作废/合并、平台售后号去重。`status` 可为逗号分隔多值，含10/11时一律不计成功 |
| 售后 `finished` | 系统完成时间，映射为 `system_completed_at`；`systemCompleteTime`/`completeTime` 不作为来源 |
| 店铺 `active` | 映射为 `shops.enabled`；字段缺失才按已核验的 `state` 枚举（3/4启用）安全回退。查询混合范围时停用店铺被排除；若全部停用则拒绝返回指标 |
| 采购 `totalAmount/actualTotalAmount`、明细 `price/amount` | 文档为分；本版不接入，不得误复用订单转换函数 |
| `orders[].itemSysId/skuSysId` | 显式映射为商品查询的 `sysItemId/sysSkuId`；不按名字模糊匹配 |
| 订单 `grossProfit` | ERP毛利参考；实际运费/包材/平台扣费缺失，不得称净利润 |
| 拼多多 `payAmount/payment/modified` | 样本全部缺失；不得纳入支付指标，也不用其他字段反推 |
| 合单（一张 ERP 单多个 `tid`/`tids`）的单头 `payAmount` | **只等于其中一个子单的金额**，不是整张合计（实测 287 张合单：单头 14.25 / 行级合计 386.05）。因此合单不得用单头与行级交叉咬合判核验：改按行级 `payAmount` 的 `tid` 归属取证（`basis='items_merged'`），且要求行全部带金额、行 tid 均在单头声明列表内、兄弟子单均有行，否则仍为 `undetermined` |
| `verified` / `allocation_verified` | 均为**本系统派生的交叉咬合结论，不是 ERP 字段**；含义、三态与升级规则见第 6 节 |

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
| `quantity` / `product_paid_amount` | 有效非赠品父项数量与已核验行级分摊金额；结果以 `line_kind` 区分 sale/suite/combination/processing，套件不是子SKU排行 | order_items |
| 推广费率/ROAS | **未启用**：无实耗来源；折扣/成本不得替代广告费 | 无 |

## 4. 已知功能门槛

- 平台实退与系统实退分开；系统退款成功指标未对账前不发布。
- `platformPaymentAmount` 在当前已核验样本未返回，`raw_platform_payment` 不可作为指标来源；`order_payments.currency='CNY'` 是平台默认币种假设，尚无 `tradeExt.currency` 来源核验。
- 同批退款率需原单匹配完成；未匹配退款返回缺数据并显示数量。
- 「最近7天」= 最近7个完整自然日；「今天」未完成，返回缺数据而非0。
- 日期跨度最多366天。逐日分组最多500个（店铺,日）组，超出直接拒参数；`total`/`shop` 分组在SQL端按店铺聚合后再累加，看到的行数只取决于店铺数，不会被日行数上限压低。任何取数路径真的命中500行上限时返回 `unavailable` 并说明原因，绝不返回被截断的偏低汇总。
- 查询时间预算耗尽一律返回 `unavailable`，不与「确实没有数据」混同（后者才是 `ok` + 空行）。
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

## 6. 覆盖、业务截止与质量状态

三者是三个不同的事实，任何一个都不能拿另一个替代（`bi_agent/data_quality.py`）：

| 概念 | 存在哪 | 不能用什么替代 |
| --- | --- | --- |
| 覆盖 `covered` | `bi.sync_state.covered`（tstzmultirange） | 不能拿“同步任务成功”宣布已覆盖 |
| 业务截止 `data_as_of` | `bi.sync_state.data_as_of` | 禁止用 `last_success_at` 顶替 |
| 来源质量 `quality_status` | `bi.sync_state.quality_status` + `quality_rule` + `quality_checked_at` | 不能因请求成功自动置 passed |

质量三态的口径：

- `unknown`：从未对账。仍可以出数，但结果必须带「来源质量未核验（尚无对账记录）」。
- `passed`：本窗口内确有 `mode='reconcile'` 且 `window_kind='business'` 的批次凭证，
  且无归属未确认的平台成功退款。凭证只来自 `bi.sync_batches`，不看“任务成功过”。
- `failed`：对账发现差异（当前规则是 `unmatched_success_refunds`）。**禁止出数**。
- 历史数据的 `quality_ok=false` 在 008 迁移中统一记为 `unknown`，不是 `failed`：
  “没查过”不等于“已查出问题”。
- 口径版本 `quality_rule` 变更后，旧 `passed` 自动降级为 `unknown`，必须重跑对账才能恢复。

查询前的结构化缺口：`assess_query_coverage(conn, request)` 返回
`requested_window` / `covered_windows` / `missing_windows` / `data_as_of` /
`quality_status` / `source_batches` / `gaps` / `suggested_window`。
原请求窗口在此冻结；`suggested_window` 只是给用户的下一步建议，
系统不得代用户缩短日期。缺口的实体与店铺归因只留在服务端，
对外 `coverage.gaps` 仍只是日期段，避免缺口反成主键泄露面。

批次口径：`bi.sync_batches.window_kind` 区分 `business`（回填/重放/对账）与
`modified`（增量的修改时间窗口）。只有 business 凭证参与覆盖与质量判定；
`row_count` 为 NULL 表示当时未统计，与“确实 0 行”不同，不得合并解释。

支付归属披露：003 口径保留“关闭但已支付”的支付事实，而 `v_product_daily` 只取有效行，
所以店铺支付额可以大于商品合计。这部分差额不得静默：`attribution_gap()` 按请求窗口
算出未进商品维度的金额，并拆成关闭订单行 / 赠品行 / 无商品归属 / 其他四个**必现**分项，
随 `limitations` 一起给出（事件码 `revenue_not_attributed`）。
分项必须全列：实测模型拿不到归因时会自己编原因（把关闭行造成的差额说成
“赠品/非父项行未计入”），给出显式 0 才能排除错猜。
真实数据核对（166754，`[2026-09-01,2026-09-10)`）：披露为
「支付额中3130.51元未计入商品维度（关闭订单行3130.51元；赠品行0元；无商品归属0元；其他0元）」，
与用 `bi_reader` 独立重算的结果逐元一致。

### 多平台来源就绪清单（2026-09-11 历史快照）

原则：没有实测的接口不填“可用”。下表于 2026-09-11 由真实账号逐店取证，
取证方法：逐店调 `erp.trade.list.query` 比对单头与行级 `payAmount` 存在率，
同步后比对 `bi.sync_state.quality_status` 与覆盖区间。

| 平台 | 店数 | 订单 | 成本 | 上架实际价 | 实物库存 | 渠道库存 |
| --- | --- | --- | --- | --- | --- | --- |
| 抖音 `fxg` | 3/4 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 京东 `jd` | 1/1 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 快手 `kuaishou` | 1/1 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 视频号 `wxsph` | 1/1 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 微购相册 `wsxc` | 1/1 | **已对账通过**（量极小） | 未执行 | 未执行 | 未执行 | 未执行 |
| 拼多多 `pdd` | 0/13 | **不可用**：交易接口可查但单头与行级均无 `payAmount`/`payment`（实测 46/46 单无金额、行 `oid` 也缺失），不得纳入支付指标 | 未执行 | 未执行 | 未执行 | 未执行 |
| 淘宝 `tb` | 0/10 | **不可用**：`erp.trade.list.query` 回 `code=33 非法店铺编码`（userId 与 shopId 均被拒），本 appKey 未开通淘系交易查询授权 | 未执行 | 未执行 | 未执行 | 未执行 |
| 天猫 `tm` | 0/2 | **不可用**：同上（`非法店铺编码`） | 未执行 | 未执行 | 未执行 | 未执行 |
| 淘工厂 `alibabac2m` | 0/1 | **不可用**：同上 | 未执行 | 未执行 | 未执行 | 未执行 |
| 1688 | 0/1（未启用） | **不可用**：`deadline=2025-01-21` 已过期且未启用 | 未执行 | 未执行 | 未执行 | 未执行 |

已接入的 7 家店均已回款逐元核对（见
`docs/superpowers/research/2026-09-11-production-deploy-and-sync.md`）。
所有店的 `aftersales_cohort` 仍为 `unknown`（同批 cohort 窗口不写批次凭证）；
库存与上架价、跨渠道 SKU 映射属计划 Task 6 / 9 / 10，本表不因代码存在而改为“可用”。

## 7. 版本化运行契约与复现等级

`revision` 只表示状态推进，**不承担数据版本语义**（计划 Task 3）。哪一版口径、
哪一版名称目录、哪几批来源数据支撑了这次结果，由 `bi.query_provenance` 显式记录：
`template_id/template_version`、`metric_version`、`schema_version`、`catalog_version`、
`mapping_version`、`policy_version`、`graph_version`、`source_batches`、`data_as_of`。

请求身份落在 `bi.query_runs`：`root_request_id`、`request_fingerprint`、`recovery_count`、
`termination_reason`。指纹 = 授权主体 + 授权店铺范围 + 规范化参数 + 上述版本的 sha256，
因此**目录版本或数据截止一变，同一问题不会再命中旧结果**；换一个主体或授权范围也不会命中
（否则等于越权读取他人结果）。

需要留证的 SQL 与参数只进 `bi.query_diagnostics`，通过 `run_id` 引用；
不写进模型消息，也不写进普通事件文本，且**不建 reporting 视图**。

复现等级（如实声明）：本契约保证**已存 Artifact 可复现读取**——给定 run 与 artifact 引用，
能取回当时发布的载荷及其版本与来源批次。在没有版本化事实库之前，
**不承诺任意历史 SQL 重跑一致**：`source_batches` 只能指回当时的同步批次，
不能重建当时的上游状态。
