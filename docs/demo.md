# 演示说明（合成数据）

数据来源：`tests/test_db.py` 的 `seed_business_case`，冻结时刻 **2026-09-08 09:00+08**，覆盖 **2026-08-25 至 2026-09-08**，金额均为人民币元。不含任何真实客户/订单信息。

## 五分钟演示路径

准备：本地测试库执行 `sql/001_init.sql` 后，将 `.env.app` 的 `BI_READER_DSN` 指向该库、`BI_SHOP_IDS=S1`，再运行一次种子（可用 `python -c "from tests.test_db import seed_business_case; ..."` 于 autocommit 连接执行），随后启动：

```powershell
uv run --env-file .env.app streamlit run app.py --server.address 127.0.0.1
```

1. **固定经营查询**：日期 2026-09-01 至 09-07（含）、店铺 S1、指标支付金额/订单数 → 表格显示 1000 / 6；`数据截止 2026-09-08 00:00+08`。
2. **连续追问**：对话输入「最近7天店铺A的支付金额」→ 卡片1000；再问「那上个月呢？」→ 保留店铺与指标，日期变为 [08-01,09-01)，返回缺数据（覆盖不足），不会只查已覆盖几天冒充整月。
3. **退款跨期**：「9月1日至7日实际退款发生多少？」→ 100（含 C0 于 09-03 的跨期退款 50）；「同批退款率」→ 5%（截至 09-08 00:00，不含 C0 与 09-09 退款）。
4. **预算假设**：「假设10月销售额10万元、推广费用率12%，最多花多少？」→ 12000，标注“用户输入假设”，不称真实账户额度。
5. **缺数据边界**：「今天的支付额」→ missing_data（未完成日提示），不生成0业绩；「9月4日支付金额」→ 真实 0（覆盖完整）；「最近7天实际推广费率和ROAS」→ 缺数据（无实耗源，不返回0）。
6. **provider启动配置**：`.env.app` 设置 `LLM_PROVIDER=deepseek`（或 `qwen`）+ `LLM_MODEL` + 对应密钥后重启页面，对话可用；未配置时固定查询照常工作。

## 20题验收

```powershell
uv run python -m unittest tests.test_core -v
uv run --env-file .env.test python -m unittest tests.test_db -v
uv run --env-file .env.test python -m tests.acceptance --offline
uv run --env-file .env.qwen-test python -m tests.acceptance --provider-smoke
uv run --env-file .env.deepseek-test python -m tests.acceptance --provider-smoke
uv run --env-file .env.qwen-test python -m tests.acceptance --live
uv run --env-file .env.deepseek-test python -m tests.acceptance --live
```

`.env.qwen-test` / `.env.deepseek-test` 为本地忽略文件：只含自身密钥、明确型号、测试DB身份（见 `.env.example` 名称）。

### provider 联调状态表

| provider | 离线适配（mock回合） | 真实smoke | 真实live 20题 |
| --- | --- | --- | --- |
| qwen | 通过（协议、ID回传、错误映射、总时限） | 未实测（需真实凭证与公司授权） | 未实测 |
| deepseek | 通过（同上） | 未实测（需真实凭证与公司授权） | 未实测 |

离线20题：20/20 通过（结构化断言，见 `tests/questions.jsonl`）。**离线结果不能证明模型理解准确率**；只有 smoke/live 且工具回合完整（提出调用→同ID回传→回答）才算 provider 验收。仅一个 provider 通过时部署该 provider，另一项保留未验收状态；两者都通过才称“双provider验证通过”。

## 面试说明要点

五个模块的输入输出：

| 模块 | 输入 | 输出 | 关键设计 |
| --- | --- | --- | --- |
| 快麦客户端 | 官方参数、签名 | 分页完整证明后的原始行 | 3次尝试、退避、脱敏错误类别；空返回必须有结束证据 |
| 事实层 | 原始交易/售后 | 版本化ERP事实+商业单支付事实 | 白名单字段、版本保护、拆合单按行级分摊重建、不确定即 unverified |
| 同步 | 窗口+水位 | 完整覆盖记录 | 业务覆盖与修改水位分离；失败回滚不动水位；两通道归档去重 |
| 指标 | QueryRequest | ToolResult（含覆盖、口径、限制） | 固定SQL模板、REPEATABLE READ、覆盖门禁优先、拒绝部分汇总 |
| Agent | 用户问题 | 确定性卡片+受控解释 | 两工具、4次调用/1次修正/30秒、匿名映射、澄清与PII分支 |

数据去重与覆盖：商业订单支付在 `order_payments` 一行一次（拆单合并、合单拆分都只确认一次）；退款按平台售后号 canonical 去重；覆盖用 multirange 表达缺口，「真实0」与「缺数据」严格分开。

Agent与确定性计算分工：模型只做意图理解、参数组装、结果解释；金额、分母、覆盖、权限全部由确定性代码和数据库约束保证，模型输出不参与金额计算。

借鉴 OpenChatBI 的工具选择与有限修复思路（工具受限集合、参数错误一次修复）；不宣称实现通用 Text2SQL——本系统没有自由 SQL 通道。

复用源码说明：本项目未复制任何参考仓库代码；如后续引入 MIT 许可代码，将在此保留对应声明。
