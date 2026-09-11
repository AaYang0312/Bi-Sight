# 真实数据核对发现的金额口径缺陷

发现时间：2026-09-11 15:50 前后（北京时间）。方式：把真实 API（`bi_agent.api`，
development 身份）接到持有真实快麦数据的库，用真实模型跑端到端，再用
`bi_reader` / 属主身份从 reporting 视图与底层表独立重算同一窗口。

结论：这不是实现走偏，而是**金额口径本身有一处自相矛盾**，合成测试库结构上测不出来。

## 1. 现象

单店 166754（元发钉枪(抖音)），窗口 `[2026-09-01, 2026-09-10)` 北京时间：

| 量 | 数值 | 来源 |
| --- | --- | --- |
| 店铺支付总额 `paid_amount` | 39,905.22 | `reporting.v_payments`，仅 `verified` |
| 商品分摊合计 `product_paid_amount` | 40,879.65 | `reporting.v_product_daily` 求和 |
| 差额 | **+974.43** | 商品侧反而大于店铺侧 |

商品级金额合计**超过**店铺支付总额，而两个数都是"支付金额"。

## 2. 根因

`allocation_verified` 的语义（`bi_agent/sync.py::rebuild_payments`）：只有该商业单的
支付事实被判定为已核验（`order_payments.verified=true`）时，其行才置 true；
支付金额或时间不确定的单，行留 `false`。

而 `product_paid_amount` 的口径文本写着「**已核验的**非赠品父项行级分摊支付金额」
（`METRIC_DEFINITIONS`），但实际路径**完全没有按 `allocation_verified` 过滤**：

- `reporting.v_product_daily`：`WHERE active AND line_kind <> 'gift' AND product_id IS NOT NULL
  AND allocated_paid_amount IS NOT NULL`，只用 `bool_and(allocation_verified)` 附带一个标志列；
- `metrics._product_rows`：把该标志合并成行上的 `allocation_verified`，不过滤；
- `ARTIFACT_RESULT_COLUMNS` 白名单**不含** `allocation_verified` → 投影时该列被丢掉，
  模型载荷与展示 Artifact 里都不存在。

本窗口实测：63 行 `allocation_verified = false`，金额合计 **3,643.04**，全部计进了
名为"已核验"的指标。

排除项（都已核对，不是这些原因）：

- 不是父项/子件重复计入：890 个商业单「行分摊合计 = 该单已核验支付额」全部相等，
  over=0 / under=0；重复 `line_id` 组数 0。
- 不是赠品行：本窗口 `line_kind='gift'` 金额为 0。
- 不是 `commercial_id` 归属缺失：为空的行为 0。
- 不是行日与支付日跨窗口错位：行在窗内而其商业单支付在窗外的情况为 0。
  37,236.61（已核验行）+ 3,643.04（未核验行）= 40,879.65 完全对得上。

## 3. 为什么测试没抓到

`tests/test_db.py::seed_business_case` 构造的支付事实都是 `verified=true`，
所以「未核验行混入商品金额」这条路径在合成库里永远不会出现。这不是断言漏写，
而是夹具缺少「支付未核验但仍带分摊金额」这一形态。

## 4. 可选修法（需产品/口径决定，未擅自改动金额）

| 方案 | 效果 | 代价 |
| --- | --- | --- |
| A 按定义过滤：商品指标只算 `allocation_verified=true` 的行 | 名称与数值一致，商品合计 ≤ 店铺合计 | 本窗口商品金额下降 3,643.04，历史报表口径变化 |
| B 保留全部行，改口径文本并披露未核验金额 | 数值不变 | 「已核验」措辞必须去掉，否则仍是虚假声明 |
| C 保留全部行 + 把 `allocation_verified` 加进展示白名单并在缺口里披露 | 不改变数值，操作者可见 | 需要扩展 Artifact 契约（属 Task 3 版本化范围） |

三者都会改变对外呈现，任一选择都需要显式确认；本文档只记录事实与证据。

## 5. 顺带记录

- 真实环境状态：`.env.app` 与 `.env.sync` 目前都指向本机
  `localhost:54329/bi_agent_test`（内含真实快麦同步数据），本机实例内没有
  独立的 `bi_agent` 库；tailnet 中也无 `100.88.1.15` 节点。因此**生产库迁移
  （005 / 007 / 008）状态未知且本轮未执行**。
- 已验证的真实链路行为：缺覆盖时先返回 `missing_data` + `suggested_window`，
  模型按建议重新发起查询后才拿到数字，且回答明确说明「9/10 尚未覆盖」，
  未偷改用户窗口；结果带「来源质量未核验（尚无对账记录）」。
