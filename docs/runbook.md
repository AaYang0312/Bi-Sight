# BI Agent 运行手册

## 配置边界

| 文件 | 运行进程 | 必需内容 |
| --- | --- | --- |
| `.env.app` | FastAPI | `APP_*`、`BI_SHOP_IDS`、`BI_APP_DSN`、一个 provider 的模型配置 |
| `.env.sync` | 快麦同步 | `BI_WRITER_DSN`、`BI_SHOP_IDS`、`KUAI_MAI_*` |
| `.env.test` | 测试 | 独立 `*_test` 库的 DSN 与 fake 模型配置 |

三者都不提交。API 环境若出现 `BI_WRITER_DSN` 或快麦凭证会在启动时拒绝运行。

`APP_ENV=development` 时身份固定为 `local-development`。生产必须设置 HTTPS 的 `APP_PUBLIC_ORIGIN`、非空 `APP_ALLOWED_SUBJECTS`，并通过反向代理写入 `AUTH_SUBJECT_HEADER`（默认 `X-Auth-Request-Sub`）。代理必须先删除浏览器传来的同名头，再写入 OIDC 的真实 `sub`。

模型只支持 `LLM_PROVIDER=qwen|deepseek`。必须显式给出 `LLM_MODEL`，只填已选择 provider 的密钥；不做自动切换或重试。默认兼容地址是 Qwen 的 `https://dashscope.aliyuncs.com/compatible-mode/v1` 和 DeepSeek 的 `https://api.deepseek.com/v1`。真实联调前确认账号地域、型号和费用；当前两者均标记为“未实测”，直到在合成测试库跑完 smoke 和 20 题。

## 本地开发

先由管理员初始化本地数据库：

```powershell
psql -d bi_agent -f backend/sql/001_init.sql
```

分别启动后端和前端：

```powershell
Set-Location backend
uv sync --locked
uv run --env-file ../.env.app uvicorn bi_agent.api:create_runtime_app --factory --host 127.0.0.1 --port 8001 --reload
```

```powershell
Set-Location frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Vite 将 `/api` 代理到 `http://127.0.0.1:8001`。开发页必须通过 `http://127.0.0.1:5175` 打开，并设置 `APP_PUBLIC_ORIGIN=http://127.0.0.1:5175`，否则写请求会被 Origin 检查拒绝。

## 数据库和同步

`bi_sync` 写业务事实，`bi_reader` 只能读报表视图，`bi_app` 读取相同视图并读写 `bi.app_chats` 与 `bi.app_messages`。API 查询与消息写入都有五秒 SQL 超时；同一会话同时生成时返回 `409 chat_busy`。

```powershell
Set-Location backend
uv run --env-file ../.env.sync python -m bi_agent.sync shops
uv run --env-file ../.env.sync python -m bi_agent.sync backfill --days 90
uv run --env-file ../.env.sync python -m bi_agent.sync incremental
uv run --env-file ../.env.sync python -m bi_agent.sync reconcile --days 7
```

同步在完整分页、校验和事务提交后才推进水位。分页、权限或上游错误不会成为零业务数据；每日重核最近七天以处理晚到退款。

部署时可注册每小时同步任务，工作目录固定为 `backend`：

```powershell
$taskUv = (Get-Command uv).Source
$syncAction = New-ScheduledTaskAction -Execute $taskUv -Argument 'run --locked --env-file ../.env.sync python -m bi_agent.sync incremental' -WorkingDirectory 'D:\Projects\bi-agent\backend'
$syncTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$syncSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'BI Agent Hourly Sync' -Action $syncAction -Trigger $syncTrigger -Settings $syncSettings
```

另建每日低峰的 `reconcile --days 7` 任务。运行账户只读取同步配置；命令参数不含密码。

## 同源部署

发布 `frontend/dist` 静态文件。反向代理将 `/api/*` 转发到 `127.0.0.1:8000`，其余路径提供 SPA 回退；关闭 SSE 路径的响应缓冲。FastAPI 仅运行于回环地址且使用单 worker：

```powershell
Set-Location backend
uv run --env-file ../.env.app uvicorn bi_agent.api:create_runtime_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

共享试用前验证：未登录被代理拦截；身份 A 不能读取或修改 B 的会话；伪造身份头无效；错误 Origin 返回 403；SSE 首个 `status` 及时到达且刷新可以恢复最后消息。

## 检查、故障与恢复

```powershell
Set-Location backend
uv run python -m unittest tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
Set-Location ../frontend
npm test
npm run build
```

模型、数据库或工具失败时，消息 SSE 返回 `error` 后再返回 `done`；它不会包含调用栈、DSN、请求体或 ERP 标识。会话仅保存用户可见文本和脱敏的聚合附件。

备份使用受控的 PostgreSQL 服务名，并只恢复到预建的独立库：

```powershell
pg_dump --dbname="service=bi_backup" --format=custom --file=backups/restore-check.dump
psql "service=bi_restore_check" -c "SELECT current_database();"
pg_restore --dbname="service=bi_restore_check" --no-owner --no-privileges backups/restore-check.dump
```

首次真实恢复前先记录事实表行数、金额摘要、覆盖状态和会话数，恢复后比较同一摘要。备份文件放在受限 ACL 的加密磁盘，保留七天。
