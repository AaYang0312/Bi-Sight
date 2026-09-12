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

### 平台路由（淘系 tb/tm）

- 订单源按 `bi.shops.platform` 路由：`tb`/`tm` 用 `erp.trade.outstock.simple.query`（销售出库·非敏感字段），其余平台（如抖音 fxg）继续用 `erp.trade.list.query`；`sync_state` 主键含 source，两通道水位/覆盖互不干扰。
- 先跑 `shops` 刷店铺档案再跑订单命令，缺档案的店会直接报错（防假覆盖）。
- 淘系口径为 **ERP 出库非敏感字段**，非平台账单口径；收件人/买家昵称/手机号等 PII 字段在规范化入口即丢弃并有守护用例，不得扩列。详见 `docs/superpowers/research/2026-09-12-taoxi-onboarding.md`。

## 页面启动

```powershell
uv run --env-file .env.app streamlit run app.py --server.address 127.0.0.1
```

development 只允许绑定本机回环地址（`st.get_option('server.address')` 校验，否则拒绝渲染）；生产使用 Streamlit 内置 OIDC（`.streamlit/secrets.toml`），`APP_ALLOWED_SUBJECTS` 是服务端名单（`issuer|sub`，逗号分隔）。

会话隔离：服务端身份 subject 变化时清空会话；日期选择器的包含结束日自动转换为排他end；查询由按钮触发，不自动读库。CSV 下载只含当前授权聚合数据，对 `= + - @`、制表符开头的文本做公式注入转义，金额列按数值输出；比率缺失显示“不可计算”，缺数据不画 0 线。模型未配置或失败时固定查询照常工作。

## 临时公网测试（仅本机）

1. 复制 `public_test.local.cmd.example` 为 `public_test.local.cmd`，填写 SSH 别名、测试域名、Basic Auth 用户名及端口；本地文件已被 Git 忽略，禁止写入口令。
2. 双击 `public_test.cmd` 开启入口；脚本检查本地页面、渲染 nginx 模板、清理残留转发、建立反向隧道并验证 HTTP 200。
3. 测试后执行 `public_test.cmd close`；必须确认服务器 nginx 临时配置、远端监听端口及本地隧道进程均已清理。

服务器证书与 htpasswd 由管理员预置，口令不写入脚本、配置模板或 Git。公网入口仅用于短时人工测试，不替代 OIDC 生产部署。

## 对话查询与Agent边界

- 单Agent只调用两个工具：`query_business`、`evaluate_promotion`；没有自由SQL/Python/HTTP工具。
- 每次提问最多4次工具调用、一次参数修正、总预算30秒；SQL与模型共用同一deadline，超时仍返回已取得的确定性卡片。
- 模型只看到匿名店铺编号（shop_1…）与本回合商品别名；实际店名/商品映射仅留服务端；ERP单号、买家数据、DSN、密钥不进入模型上下文。
- 会话状态按身份隔离；保留最近6个完整回合，裁剪时整体删除一个回合。
- 模型解释不重算金额；没有调用成功工具时不能声称查询到实际经营数字。

## 模型 provider

`LLM_PROVIDER=qwen|deepseek`，显式配置 `LLM_MODEL`；只使用所选 provider 的密钥。启动时选择，无自动切换和动态路由。

| provider | 官方 OpenAI 兼容地址（核验日期：2026-09-07） |
| --- | --- |
| qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| deepseek | `https://api.deepseek.com/v1` |

执行部署时需按上述地址所属地域核对账号可用性，再记录实际选择。地域变化只改部署地址（`LLM_BASE_URL`），不替换密钥。

联调门槛：离线 mock 回合通过（31项核心检查）只证明协议适配正确，不证明模型理解准确率；真实工具回合见「验收」章节（`--provider-smoke`），必须经过「模型提出工具调用→回传同ID结果→模型回答」。错误不自动重试、不切 provider；单请求总时限 30 秒预算由调用方传入。

## 故障与恢复

- 同步全程持有数据库 advisory 锁，重复启动立即失败（“已有同步任务运行”）。
- 窗口拉取、校验和事务提交后才推进成功水位；分页失败、形状异常、权限错误不会变成零业务。
- 窗口失败时：该窗口整体回滚，另开短事务记录 `last_attempt_at/last_error_code`，旧成功水位和已完成窗口保留；重跑幂等。
- 同步失败时页面显示影响范围与最后成功时间，历史成功报表仍可查看。
- 模拟上游超时验证：水位不动 → 页面提示失败 → 恢复后成功。

## 定时任务（部署时注册，本计划不创建）

```powershell
$taskUv = (Get-Command uv).Source
$syncAction = New-ScheduledTaskAction -Execute $taskUv -Argument 'run --locked --env-file .env.sync python -m bi_agent.sync incremental' -WorkingDirectory 'D:\Projects\bi-agent'
$syncTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$syncSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'BI Agent Hourly Sync' -Action $syncAction -Trigger $syncTrigger -Settings $syncSettings
```

另建每日低峰 `reconcile --days 7` 动作；同用数据库锁，错过时下一次再跑。运行账户仅能读取同步凭证；运行参数不含密码；后台辅助进程使用隐藏窗口。

## 脱敏日志

同步 CLI 日志写入 `logs/sync.log`：一行JSON、字段白名单（request_id、工具名/实体、匿名店铺、日期范围、行数、耗时、data_as_of、错误类别），轮转 10MiB×5。禁止 `logger.exception` 无审查输出包含请求体/DSN的异常。页面显示同步失败影响范围与最后成功时间，历史成功报表仍可查看；用一次模拟上游超时确认水位不动、页面提示失败、随后恢复成功。

## 备份与恢复（部署时执行）

`bi_backup` / `bi_restore_check` 是本机 pg_service.conf 中的连接服务名；备份身份需有事实表读取权限，不能误用只看视图的应用账号。备份文件位于受限ACL及加密磁盘；每日备份、保留7天。

```powershell
New-Item -ItemType Directory -Path backups -Force
# 备份前通过同一advisory锁暂停同步写入，记录行数/金额汇总/覆盖状态摘要
pg_dump --dbname="service=bi_backup" --format=custom --file=backups/restore-check.dump
# 恢复目标由管理员预建独立空库 bi_agent_restore，先确认服务确实指向此库，不覆盖业务库
psql "service=bi_restore_check" -c "SELECT current_database();"
pg_restore --dbname="service=bi_restore_check" --no-owner --no-privileges backups/restore-check.dump
# 恢复后比较相同摘要，并验证关键报表可查询；记录一次真正恢复成功的日期与步骤（单有dump文件不算通过）
```

## 验收与试用

验收命令与provider联调状态表见 `docs/demo.md`。试用规则：

- 一店小范围试用一周：每日检查同步覆盖和失败、抽查一个经营问题、记录失败问法及口径分歧。
- 对接口审批/字段限制形成明确问题单；不为“所有平台都有店铺记录”提前开放全平台汇总；页面始终标出试点范围。
- 推广实耗接入前置条件（向快麦实施确认）：具体方法名/文档、当前账号授权、费用粒度、币种、修正规则、更新时间、归因窗口。拿到并对账后另建 `promotion_daily` 及真实费用规则；若快麦不提供，由经营者选择广告平台导出CSV或授权API。淘系/拼多多分别取得奇门/方舟的实际授权文档并对账后才能扩展支付能力。

## 安全红线

- `.env*`、真实导出、备份、接口响应、业务截图不进 Git。
- 日志只输出字段白名单（request_id、工具名、匿名店铺、日期范围、行数、耗时、data_as_of、错误类别）；不记录凭证、签名串、完整请求响应或客户信息。
- 模型只接收聚合结果、匿名标签和口径说明；ERP 单号与买家数据不发给模型。
