# BI Agent 运行手册

内部工具：对话查询抖音试点店铺经营情况、按明确假设测算推广预算。结果可对账，模型 provider 可选择。

## 环境要求与已记录版本

| 组件 | 要求 | 本机记录 |
| --- | --- | --- |
| Python | 3.11（`.python-version` 固定） | uv 托管 3.11 |
| uv | ≥0.12 | uv 0.12.7 |
| PostgreSQL | 17 | 见下文「数据库准备」 |
| 操作系统 | Windows（tzdata 保证 `Asia/Shanghai` 可用） | Windows |

安装依赖：`uv sync --locked`。运行任何命令都在项目根目录 `D:\Projects\bi-agent`，配置用 uv 的 `--env-file` 注入。

## 配置文件分工

| 文件 | 用途 | 进 Git？ |
| --- | --- | --- |
| `.env.example` | 配置名称与非敏感默认值说明 | 是 |
| `.env.app` | 页面应用：`APP_ENV/APP_ALLOWED_SUBJECTS/BI_SHOP_IDS/BI_READER_DSN` + 模型配置 | 否 |
| `.env.sync` | 同步进程：`BI_WRITER_DSN/BI_SHOP_IDS` + `KUAI_MAI_*` 四个凭证字段 | 否 |
| `.env.test` | 测试：测试库 DSN 与 fake 模型配置 | 否 |
| `.streamlit/secrets.toml` | 生产 OIDC 登录配置 | 否 |

字段含义见 `.env.example` 注释。`KUAI_MAI_APP_TITLE/COMPANY_ID` 不作为快麦公共参数自动发送。

应用环境不得包含写入 DSN 和 ERP 凭证；同步环境不得包含模型密钥之外的页面配置。

## 数据库准备

DDL 只由管理员执行；`bi_sync` 拥有 `bi` schema 事实表读写权限；`bi_reader` 只有 `reporting` schema 指定视图的 SELECT 权限（默认只读、5 秒超时）。角色密码通过管理员 `\password` 或现有密钥设施设置，SQL 文件不含密码。

初始化：管理员连接后执行 `sql/001_init.sql`。

## 同步命令（任务4完成后可用）

```powershell
uv run --env-file .env.sync python -m bi_agent.sync shops
uv run --env-file .env.sync python -m bi_agent.sync probe --start 2026-09-05 --end 2026-09-06
uv run --env-file .env.sync python -m bi_agent.sync backfill --days 90
uv run --env-file .env.sync python -m bi_agent.sync incremental
uv run --env-file .env.sync python -m bi_agent.sync reconcile --days 7
uv run --env-file .env.sync python -m bi_agent.sync replay --entity orders --start 2026-09-01 --end 2026-09-02
uv run --env-file .env.sync python -m bi_agent.sync refresh-session
```

单实例：全程持有数据库 advisory 锁，重复启动立即失败。

## 页面启动

```powershell
uv run --env-file .env.app streamlit run app.py --server.address 127.0.0.1
```

development 只允许绑定本机回环地址；生产使用 Streamlit 内置 OIDC（`.streamlit/secrets.toml`），`APP_ALLOWED_SUBJECTS` 是服务端名单。

## 模型 provider

`LLM_PROVIDER=qwen|deepseek`，显式配置 `LLM_MODEL`；只使用所选 provider 的密钥。启动时选择，无自动切换和动态路由。

| provider | 官方 OpenAI 兼容地址（核验日期：2026-09-07） |
| --- | --- |
| qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| deepseek | `https://api.deepseek.com/v1` |

执行部署时需按上述地址所属地域核对账号可用性，再记录实际选择。真实联调命令与验收状态见 `docs/demo.md`。

## 故障与恢复

见下文「备份与恢复」（任务10完成后补充完整步骤）。同步失败时页面显示影响范围与最后成功时间，历史成功报表仍可查看。

## 安全红线

- `.env*`、真实导出、备份、接口响应、业务截图不进 Git。
- 日志只输出字段白名单（request_id、工具名、匿名店铺、日期范围、行数、耗时、data_as_of、错误类别）；不记录凭证、签名串、完整请求响应或客户信息。
- 模型只接收聚合结果、匿名标签和口径说明；ERP 单号与买家数据不发给模型。
