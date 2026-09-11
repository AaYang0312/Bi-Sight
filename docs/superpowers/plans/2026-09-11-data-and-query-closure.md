# 数据与查询闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让经营者在授权范围内使用真实店铺 / 商品名称查询可信数据，并在覆盖不足、模型失败或工具预算耗尽时得到明确且可追踪的结果。

**Architecture:** 保留现有 FastAPI / React、固定指标引擎和确定性查询图。新增名称目录与授权展示解析，补齐数据覆盖和版本来源，再将恢复决策从自由工具循环移入确定性策略。后续 Schema 检索、SQL 探索、学习记忆和隔离分析按独立子项目交付。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL、FastAPI、React 19、TypeScript、unittest、Vitest；近期不引入新的 Agent 图框架。

**Spec:** [现有进度与方向对齐](../research/2026-09-11-progress-and-roadmap.md)，其中 P0 / P1 为近期目标，P2–P5 为后续路线。

## Global Constraints

- 当前基线为 `873e6fd`；执行前读取最新 HEAD、迁移序号和其他 Agent 的改动，避免覆盖并行工作。
- 另一 Agent 已在编写 `005_product_dimension.sql`、`sync_products()` 及商品测试。先接收该交付；本计划名称模块在其基础上补齐，禁止重建第二套商品表或复用 005 编号。
- 现有阶段 1/2 已合入，不重复实现或从旧工作树覆盖当前代码。
- 首轮全量范围按已授权抖音店铺、已有订单 / 售后实体及显式历史窗口验收；不将店铺档案数当同步完成数。
- 金额由确定性工具产生，沿用 `[start,end)`、北京时间、支付与退款口径；商品父项与子件不可重复计入销售额。
- 真实业务名称在授权应用展示层解析；模型使用稳定 opaque ref。名称字段不与 ERP 主键混为一类。
- 独立测试库与真实数据环境隔离；缺少集成环境必须标注未执行，不能记为通过。
- Schema / 口径 / 名称变化不覆写历史 Artifact；旧聊天继续可读。
- 本计划是待实施交付清单，本次只编写文档，不执行迁移或同步。

## Task 1：数据覆盖验收与版本来源（P0，先交付）

**Files:**
- Create: `backend/bi_agent/data_quality.py`、`backend/tests/test_data_quality.py`。
- Modify: `backend/bi_agent/sync.py`、`backend/bi_agent/metrics.py`、`docs/runbook.md`、`docs/metrics.md`。
- Create: `backend/sql/006_data_quality.sql`（005 已由商品维度占用；执行时再次确认并顺延）。

**Interfaces:** 新增 `assess_query_coverage(conn, request: QueryRequest) -> CoverageAssessment`；结果包含 `requested_window`、`covered_windows`、`missing_windows`、`data_as_of`、`quality_status`、`source_batches`。不以最后任务成功时间替代 `data_as_of`。

- [ ] 在独立测试库增加“有店铺无事实”“窗口有缺口”“成功同步但业务截止未推进”“对账失败”用例，运行后确认旧实现不能通过新增断言。
- [ ] 明确 `quality_ok` 的升级规则和历史数据处理：新增 `unknown/passed/failed` 质量状态及对账时间 / 版本；历史 false 不直接当已发现数据错误。失败范围禁止出数，未知质量明确披露，完成核验后才提升为 passed。
- [ ] 将每个店铺 / 实体 / 窗口的同步批次、覆盖和对账证据持久化；部分分页失败时窗口不推进，金额缺失与真实零分开。
- [ ] 在固定查询调用前返回结构化缺口；冻结原查询窗口，建议的可用窗口只作为建议返回。
- [ ] 更新 runbook，按执行时的授权清单运行历史回填、增量和核对，并记录各组合完成 / 不支持 / 失败及原因。
- [ ] 回归后形成独立提交：`feat: expose verified data coverage and provenance`。

验证命令：

```sh
cd backend
.venv/bin/python -m unittest tests.test_data_quality tests.test_core tests.test_business_query_graph -v
```

关键断言示例（测试数据由新测试模块建立）：

```python
self.assertEqual(assessment.requested_window, ("2026-09-04", "2026-09-11"))
self.assertIn(("2026-09-09", "2026-09-11"), assessment.missing_windows)
self.assertNotEqual(assessment.quality_status, "passed")
```

## Task 2：真实名称、稳定引用和授权展示（P0）

**Files:**
- Create: `backend/bi_agent/catalog/models.py`、`backend/bi_agent/catalog/repository.py`、`backend/tests/test_catalog.py`。
- Reuse: 并行任务的 `backend/sql/005_product_dimension.sql` 与 `sync_products()`；Create: `backend/sql/007_catalog_identity.sql`（只扩展缺少的引用 / SKU / 历史名称，执行时重新确认序号）。
- Modify: `backend/bi_agent/sync.py`、`backend/bi_agent/metrics.py`、`backend/bi_agent/business_query/tool.py`、`backend/bi_agent/runtime/models.py`、`backend/bi_agent/chats.py`、`backend/bi_agent/agent.py`。
- Modify: `frontend/src/components/ArtifactView.tsx`、`frontend/src/types.ts`；Create: `frontend/src/components/ArtifactView.test.tsx`。

**Interfaces:** `EntityRef` 包含 `kind: shop|product|sku`、`ref`、`catalog_version`。`resolve_display_entities(conn, subject_id, refs) -> list[DisplayEntity]` 返回授权范围内的 `ref/display_name/sku_label/name_source`。模型投影只返回引用，展示投影在应用内附名称；旧匿名 Artifact 保持兼容。

- [ ] 建立改名、同名商品、不同店铺、多个 SKU、名称缺失、超 26 个商品、排名变化、越权名称读取的失败用例。已覆盖：改名、同名（店铺/商品）、名称缺失、30 商品、重排稳定性、越权读取。**未覆盖：多个 SKU**（见下方遗留）。
- [ ] 订单行新增成交名称 / SKU 文本快照；建立商品和 SKU 主档、店铺平台关系、名称来源与有效时间，主键必须区分平台与账号范围。已完成：`product_name_snapshot` / `sku_label_snapshot` 落 `bi.order_items`、商品主档 `bi.products`、店铺平台关系与 `source_modified_at` 版本保护。**未完成：SKU 主档**。
- [x] 从订单行先补可用名称，再同步获准主档并标注来源。历史订单未带名称且主档不存在时展示“名称未取得”与稳定引用，禁止编造。取用优先级单点实现于 `catalog.pick_display_name`（档案 > 成交快照 > 未取得）。
- [x] 使用持久化 opaque ref 替换每次查询重新编号的商品别名；将引用作为下一轮实体筛选的可信输入，重新检查用户授权。`bi.entity_refs` 落表 + `ref_for_key` 同源派生；`_resolve_shops` 只接受已登记引用，未识别值标记 `invalid_shop` 且不进入授权节点。
- [x] 在聊天保存 / 读取投影与前端卡片接入真实展示名；扩展明确字段白名单，保持模型与展示投影分别校验。`ARTIFACT_RESULT_COLUMNS` 与模型投影分别校验，前端 `ArtifactView` 渲染真实名并保留“名称未取得”占位。
- [x] 历史重放补名称前后比较相同范围支付额、销量和父项聚合，数值应不变；测试完成后独立提交：`feat: resolve real catalog names with stable references`。见 `tests/test_db.py::test_archive_name_join_leaves_totals_untouched`。

**Task 2 遗留（SKU 维度）：** `EntityKind.SKU` 与 `DisplayEntity.sku_label` 已就位，前端也会渲染规格，但 `build_catalog` 从不填充 `sku_label`，`reporting.v_product_daily` 也不带 SKU 粒度，因此“同商品多 SKU”既无主档也无失败用例。补名称时不得改变 `(shop, day, product, line_kind)` 聚合粒度，否则父项金额会被重复计入；一个分组内出现多个不同规格时只允许展示商品名，禁止任选其一。

```sh
cd backend
.venv/bin/python -m unittest tests.test_catalog tests.test_core tests.test_runtime tests.test_business_query_graph -v
cd ../frontend
npm test -- src/components/ArtifactView.test.tsx
npm run build
```

核心断言：

```python
self.assertEqual(first_ref, reranked_ref)
self.assertEqual(len(set(refs_for_30_products)), 30)
self.assertNotIn("真实商品名称", model_payload_json)
self.assertIn("真实商品名称", authorized_artifact_json)
self.assertEqual(unauthorized_display_entities, [])
```

## Task 3：版本化运行契约（P1，依赖 Task 1/2）

**Files:**
- Modify: `backend/bi_agent/runtime/models.py`、`runtime/repository.py`、`runtime/memory.py`、`business_query/state.py`、`business_query/nodes.py`。
- Create: `backend/sql/008_query_provenance.sql`（执行时重新确认序号）。
- Modify: `backend/tests/test_runtime.py`、`backend/tests/test_runtime_db.py`、`backend/tests/test_business_query_graph.py`。

**Interfaces:** 新增 `QueryProvenance`：`template_id/template_version/metric_version/schema_version/catalog_version/source_batches/data_as_of`；新增 `RequestIdentity`：`root_request_id/request_fingerprint/attempt_no/recovery_count/termination_reason`。现有 `revision` 只继续表示状态推进，不承担任何数据版本语义。

- [ ] 测试同一 state revision 对应不同数据批次、数据更新后缓存不复用、旧 Artifact 读取和非法字段写入。
- [ ] 固定查询记录模板 ID / 版本；需要执行诊断的 SQL 与参数进入受控记录，通过引用关联，不写入模型消息或普通事件文本。
- [ ] 扩展状态、事件、Artifact 的独立白名单和两类 Store，保证规范化请求与状态一致且 CAS 仍生效。
- [ ] 请求指纹包括授权范围、规范化参数、查询 / 数据版本；数据版本变化后不得命中旧结果。
- [ ] 说明复现等级：当前至少可重现已存 Artifact；没有版本化事实库时不承诺任意历史 SQL 重跑一致。测试后提交：`feat: persist query versions and recovery identity`。

```sh
cd backend
.venv/bin/python -m unittest tests.test_runtime tests.test_business_query_graph -v
uv run --env-file ../.env.test python -m unittest tests.test_runtime_db -v
```

## Task 4：确定性恢复与回答兜底（P1，依赖 Task 3）

**Files:**
- Create: `backend/bi_agent/business_query/recovery.py`、`backend/bi_agent/response_summary.py`、`backend/tests/test_recovery.py`。
- Modify: `backend/bi_agent/agent.py`、`backend/bi_agent/business_query/graph.py`、`business_query/nodes.py`、`runtime/models.py`、`backend/tests/test_core.py`、`backend/tests/test_api.py`。

**Interfaces:** `decide_recovery(error, coverage, request_identity, remaining_seconds) -> RecoveryDecision`；`render_result_summary(domain_result: DomainResult) -> str`。决策包含 `action`、`reason_code`、`max_additional_attempts`、`suggested_window`，不能包含未获确认的替代原请求。

| 条件 | 确定性处理 | 自动追加次数 |
| --- | --- | --- |
| 缺少参数 / 实体有歧义 | 询问缺少字段，不执行 SQL | 0 |
| 参数非法 | 返回安全字段错误，允许模型修正 | 整轮最多 1 |
| 缺数据覆盖 | 返回原窗口、缺口、共同截止、建议窗口 | 原请求重复查询 0 |
| 无权限 / 未支持指标 | 返回明确原因，终止 | 0 |
| 已识别临时连接故障 | 有足够预算且事务已回滚时重试 | 最多 1 |
| SQL 超时 / 总预算耗尽 | 终止，给范围建议 / 已有结果 | 0 |
| Artifact 持久化失败 | 不发布成功结果 | 0 |
| 相同请求、同一数据版本已有结果 | 复用 Artifact 引用 | 新 SQL 0 |

- [ ] 添加表中每个分支的失败测试，重点证明重复缺覆盖不会耗尽工具次数，也不会缩短用户日期。
- [ ] 在指标与图边界将可识别异常映射到固定错误码；未知错误保持 unavailable，禁止因猜测错误类型自动重试。
- [ ] 子图执行恢复决策；主 Agent 消费结构化 action，不从错误文案决定如何修复。共享整轮 deadline，任何重试不重置预算。
- [ ] 保留现有纯文本补答；补答失败或预算不足时使用基于 Artifact 的确定性摘要，包含指标、窗口、截止和限制，不重新计算金额。
- [ ] API 的错误文案消费具体终止原因，避免把查询预算或数据缺口一律显示成模型不可用。测试后提交：`feat: recover queries with typed bounded decisions`。

```sh
cd backend
.venv/bin/python -m unittest tests.test_recovery tests.test_core tests.test_runtime tests.test_business_query_graph -v
uv run --env-file ../.env.test python -m unittest tests.test_api -v
```

核心断言：

```python
self.assertEqual(query.call_count, 1)  # 原请求缺覆盖后不循环执行
self.assertEqual(final_result.filters["end"], "2026-09-11")
self.assertIn("数据覆盖不足", final_text)
self.assertEqual(retry_deadlines, [original_deadline, original_deadline])
```

## Task 5：近期端到端验收与交付

**Files:** Modify `backend/tests/acceptance.py`、`docs/runbook.md`、`docs/metrics.md`、`docs/demo.md`；新增验收记录到 `docs/superpowers/research/`。

- [ ] 先在合成库验收：真实名称、歧义澄清、改名追问、近期缺数据、零业务、停用 / 越权、超时、补答失败、重复请求、持久化失败。
- [ ] 执行现有全量离线套件及新增用例，确认失败和跳过数；测试账号不能使用真实业务库。
- [ ] 再针对固定店铺 / 窗口核对源报表与结果；支付额按 Decimal 精确比较，计数一致，差异必须有记录，不能用统一容差掩盖缺失字段。
- [ ] 用已配置真实模型单独运行验收，记录模型版本、数据版本、问题、正确率、澄清率、平均调用次数、耗时和失败分类；不把离线替身结果称为真实模型通过。
- [ ] 更新运行手册中实际 provider 联调状态、当前工作目录、迁移和数据覆盖清单；将 P0/P1 验收结果提交归档。

```sh
cd backend
.venv/bin/python -m unittest tests.test_core tests.test_runtime tests.test_business_query_graph tests.test_catalog tests.test_data_quality tests.test_recovery -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api tests.test_runtime_db -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
cd ../frontend
npm test
npm run build
```

## 后续独立子项目入口

近期闭环通过后再分别制定可执行详细计划，避免依赖尚未确定的目录和数据契约：

1. **P2 Schema 检索：** 新建 `semantic_catalog/` 和 `tests/test_schema_retrieval.py`；复用目录版本与权限；交付检索候选、最终选择、JOIN 粒度检查与题库报告。
2. **P3 SQL 探索：** 新建 `sql_query/` 和 `tests/test_sql_query_graph.py`；复用 `DomainResult` 与恢复预算；交付只读执行、安全校验、有限修复及固定指标交叉验收。
3. **P4 学习记忆：** 新建 `learning_memory/` 和 `tests/test_memory_promotion.py`；复用版本血缘；交付候选隔离、人工提升、版本失效和 approved-only 检索。
4. **P5 隔离分析：** 新建 `analysis/` 和 `tests/test_analysis_isolation.py`；复用数据集 / Artifact 引用；交付独立运行环境、预算、取消清理和受限摘要返回。

可并行边界：覆盖核查和名称来源分析可并行；共享 `sync.py`、`metrics.py`、`runtime/models.py` 与迁移时需要指定单一集成人。恢复策略可先使用合成 DomainResult 开发，落地依赖版本化契约。学习记忆和复杂分析不抢在数据目录、评测和版本契约之前上线。
