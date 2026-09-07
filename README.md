# BI Agent

面向电商经营分析的内部 BI Agent。当前版本连接快麦与 PostgreSQL，服务抖音（`fxg`）试点店铺，提供确定性的经营指标查询，以及受限的自然语言问答。

> 当前项目是内部试点版本，不是通用 Text2SQL 平台。推广实耗、净利润和全平台汇总等未具备可靠数据来源的能力不会被伪造或展示为已支持。

## 能做什么

- 通过 Streamlit 页面按日期、店铺、指标和维度查询经营数据，并下载 CSV。
- 支持支付金额、支付订单数、客单价、ERP 单据数、退款发生额、期间收支差额、同批退款率、商品销量和商品支付金额。
- 支持按合计、按日、按店铺、按商品查看结果，并可对比上一等长周期。
- 通过对话查询经营指标，支持连续追问和必要的口径澄清。
- 按明确的用户假设测算推广预算，例如“销售额 10 万元、推广费用率 12% 时最多花多少”。
- 从快麦同步订单和售后数据，处理分页、增量水位、版本更新、拆合单、退款去重和覆盖缺口。
- 将“真实为 0”和“没有覆盖数据”区分开，结果同时返回数据截止时间、覆盖状态、指标口径和限制说明。

## 工作方式

```text
快麦开放平台
       │
       ▼
同步进程 ──► PostgreSQL 事实表与覆盖状态
                         │
                         ▼
                 确定性指标查询
                    │       │
                    ▼       ▼
              Streamlit 页面  受限 Agent
                              │
                      query_business
                      evaluate_promotion
```

Agent 只负责理解问题、组装参数和解释结果；金额、权限、覆盖判断和指标计算由 Python 与参数化 SQL 完成。模型没有自由 SQL、Python 或 HTTP 工具，单次对话最多调用 4 次工具，总预算 30 秒。

## 环境要求

- Python 3.11
- [uv](https://docs.astral.sh/uv/) 0.12+
- PostgreSQL 17
- Windows（当前运行手册按 Windows 编写）

## 快速开始

### 1. 安装依赖

```powershell
uv sync --locked
```

### 2. 初始化数据库

由数据库管理员执行初始化脚本。脚本会创建事实表、只读报表视图、同步状态和 `bi_sync` / `bi_reader` 角色；角色密码由管理员单独配置。

```powershell
psql -d bi_agent -f sql/001_init.sql
```

### 3. 配置页面

复制配置模板，填写页面所需字段：

```powershell
Copy-Item .env.example .env.app
```

`.env.app` 至少需要：

```dotenv
APP_ENV=development
APP_ALLOWED_SUBJECTS=
BI_SHOP_IDS=店铺ERP_USER_ID
BI_READER_DSN=postgresql://bi_reader:密码@localhost:5432/bi_agent
```

开发环境只允许绑定本机回环地址。生产环境需要配置 Streamlit OIDC 和 `APP_ALLOWED_SUBJECTS`，并且页面只使用 `bi_reader` 只读连接。

### 4. 启动页面

```powershell
uv run --env-file .env.app streamlit run app.py --server.address 127.0.0.1
```

页面未配置模型时，固定筛选查询仍然可用。启用对话功能时，在 `.env.app` 中增加：

```dotenv
LLM_PROVIDER=qwen
LLM_MODEL=模型名称
QWEN_API_KEY=模型密钥
# 或使用 deepseek，并配置 DEEPSEEK_API_KEY
```

`LLM_PROVIDER` 只支持 `qwen` 和 `deepseek`；也可以用 `LLM_BASE_URL` 指定 HTTPS 兼容地址。

## 数据同步

同步进程使用独立的写入配置 `.env.sync`，不要把写入 DSN 或快麦凭证放进页面环境。至少需要 `BI_WRITER_DSN`、`BI_SHOP_IDS` 以及四个 `KUAI_MAI_*` 凭证字段。

常用命令：

```powershell
uv run --env-file .env.sync python -m bi_agent.sync shops
uv run --env-file .env.sync python -m bi_agent.sync probe --start 2026-09-05 --end 2026-09-06
uv run --env-file .env.sync python -m bi_agent.sync backfill --days 90
uv run --env-file .env.sync python -m bi_agent.sync incremental
uv run --env-file .env.sync python -m bi_agent.sync reconcile --days 7
uv run --env-file .env.sync python -m bi_agent.sync replay --entity orders --start 2026-09-01 --end 2026-09-02
uv run --env-file .env.sync python -m bi_agent.sync refresh-session
```

同步任务通过数据库 advisory lock 保证单实例运行；窗口失败时回滚本窗口数据，不推进成功水位，便于恢复后幂等重跑。

## 示例问题

启动页面后可以尝试：

```text
最近 7 天支付金额和支付订单数是多少？
按天看最近 7 天的支付金额。
9 月 1 日至 7 日实际退款发生多少？
假设 10 月销售额 10 万元、推广费用率 12%，最多花多少？
最近 7 天实际推广费率和 ROAS 是多少？
```

最后一个问题会返回缺数据，因为当前版本没有经过对账的推广实耗来源；缺数据不会被当成 0。

合成数据的完整演示路径见 [`docs/demo.md`](docs/demo.md)，指标口径见 [`docs/metrics.md`](docs/metrics.md)，运行与部署注意事项见 [`docs/runbook.md`](docs/runbook.md)。

## 测试与验收

### 核心离线测试

```powershell
uv run python -m unittest tests.test_core -v
```

### 数据库测试

配置独立测试库和 `.env.test` 后运行：

```powershell
uv run --env-file .env.test python -m unittest tests.test_db -v
uv run --env-file .env.test python -m tests.acceptance --offline
```

### 真实模型验收

仅在已准备对应 provider 凭证、测试数据库和授权后运行：

```powershell
uv run --env-file .env.qwen-test python -m tests.acceptance --provider-smoke
uv run --env-file .env.deepseek-test python -m tests.acceptance --provider-smoke
uv run --env-file .env.qwen-test python -m tests.acceptance --live
uv run --env-file .env.deepseek-test python -m tests.acceptance --live
```

`--offline` 只能验证工具协议和确定性业务结果，不能证明真实模型的理解准确率。

## 项目结构

```text
app.py                 Streamlit 页面、认证、查询和 CSV 下载
bi_agent/
  agent.py             受限 Agent、会话状态和工具编排
  config.py            页面、同步、模型配置加载
  kuaimai.py           快麦请求、签名、分页和错误分类
  sync.py              店铺、订单、售后同步命令
  metrics.py           查询参数、指标 SQL 和结果口径
  promotion.py         推广预算假设测算
  llm.py               Qwen/DeepSeek 兼容模型适配
sql/001_init.sql       PostgreSQL 表、视图、索引和权限
tests/                 核心、数据库和 20 题验收检查
docs/                  运行手册、演示说明和指标口径
```

## 安全边界

- `.env*`、真实接口响应、客户数据、导出文件、备份和日志不提交到 Git。
- 页面使用只读数据库身份；同步使用独立写入身份。
- 模型只接收匿名店铺标识、聚合结果和口径说明，不接收 ERP 单号、买家信息、DSN 或密钥。
- 店铺范围由服务端配置决定，不接受 URL 或模型参数越权扩大范围。
- 当前实现不宣称广告归因 ROAS、净利润或全平台支付数据已经可用。

## 当前状态

这是一个面向试点的内部版本。快麦真实数据、模型 provider、认证、定时同步、备份恢复和生产权限应按 [`docs/runbook.md`](docs/runbook.md) 中的验收步骤分别确认；未完成对账的能力保持关闭。
