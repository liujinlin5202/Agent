# 自动巡检报告子系统（ops_report）· 设计文档

- **日期**：2026-09-20
- **状态**：已实现并部署（2026-09-22）——首封报告与测试邮件已发送；正式周期自 09-22 起每两天 21:30（下次 09-24 21:30）
- **归属**：tech-digest 项目内（用户要求：不新开独立目录）
- **监控对象**：集市自动发帖机器人（tech-digest）、集市发帖自动回复 AI（sse_market_assist @ pod）

---

## 1. 背景与目标

用户要求「模拟实际企业操作」：**每两天 21:30** 把两个系统的运行日志汇总成一封运维报告邮件，发往 `abc3509429932@163.com`。报告须包含：

1. 自动发帖机器人：**任务成功率** + 是否有报错 + 解决与运维的思路方法；
2. 发帖自动回复 AI：**当前情况** + 任务成功率 + 是否有报错 + 解决与运维的思路方法。

目标：把「人工翻日志找问题」变成「定期收到结论 + 可执行建议」——常态无感，异常显性。

## 2. 现状侦察结论（2026-09-20，只读实测）

| 项 | 结论 |
|---|---|
| 发帖机器人 | **健康**。daily/star/weekly 三个 systemd timer 全激活；近 7 天 run_log 28 条（ok 22 / skipped 5 / degraded 1）；09-19 star 发布 postID=6311、09-20 weekly 发布 postID=6322 |
| 应答 AI | **全线不可用**。pod（k8s `group-market-rag-*`）上 `main.py` 进程不存在、8080 无监听；host nginx → frps 13012 → pod 全部 502（`upstream prematurely closed connection`）；48h 内 `/api/v1/assist/*` 请求 **14577 次、0 次成功**（502×13223、421×1333、499×19、404×2） |
| 邮件通路 | 集市服务器 → `smtp.163.com` 的 465/587/994 均可连；25 端口不可用（云厂商默认封禁，属正常，走 465 SSL） |
| 磁盘 | 根分区 168G/216G = **82%**（2026-08-30 曾因磁盘 97% 整机挂死，阈值需关注） |
| pod 可达性 | 集市服务器本机可直连 pod SSH `127.0.0.1:22012`；服务器已装 `sshpass` |

## 3. 方案（已选：路线 A —— 服务器本地定时）

- 运行在集市服务器，systemd timer 驱动，**不依赖任何个人电脑开机**；
- 代码放 **tech-digest 项目内**（`tech-digest/ops_report/`），复用其 venv、`.env` 加载方式、`deploy/` systemd 模式、`app/marketdb.py` 的集市库只读通道（docker exec → 库 `market`）；
- **纯只读**：不改动 tech-digest / assist 的任何现有代码、数据、路由；
- 已考虑并排除：本地 Windows 定时（电脑不开机即失效）、部署到 pod（无 systemd 且会被 K8s 重建）。

## 4. 目录结构

```text
tech-digest/                              # 本地与服务器同构
├── ops_report/                           # 新增子包（无独立顶层目录）
│   ├── __init__.py
│   ├── __main__.py                       # 入口：python -m ops_report {run,dry-run,test-mail}
│   ├── config.py                         # 读 ops_report/.env（SMTP、pod、收件人、阈值）
│   ├── window.py                         # 报告窗口计算（上次发送 → 现在）
│   ├── collect/
│   │   ├── digest.py                     # 发帖机器人：run_log + 日志文件 + timer 状态
│   │   ├── assist.py                     # 应答 AI：nginx 日志 + pod 探活与日志 + 集市库应答数
│   │   └── system.py                     # 磁盘 / 证书 / 服务状态
│   ├── analyze.py                        # 指标计算 + 错误聚类
│   ├── knowledge.py                      # 错误→根因→处置 知识库
│   ├── llm_advice.py                     # LLM 写「分析与建议」（失败→模板兜底）
│   ├── render.py                         # HTML 邮件 + JSON 归档
│   ├── mailer.py                         # SMTP 465 发送 + 重试
│   └── state.py                          # 2 天守卫 + 告警文件
├── tests/test_ops_report_*.py            # 单测（随 tech-digest 现有 tests 一起跑）
├── deploy/ops-report.service             # 新增
├── deploy/ops-report.timer               # 新增（21:30）
├── ops_report/.env                       # 凭据（600 权限，.gitignore 已覆盖 .env）
└── ops_report/data/                      # state.json / reports/ 归档 / alerts/
```

## 5. 数据源与指标口径

### 5.1 自动发帖机器人

数据源：`data/tech-digest.db` 的 `run_log` 表（结构化，每档期写 2 行：采集行含 `source_stats`、发布行含 `published/skipped/degraded`）+ `log/tech-digest.log` + systemd timer 状态。

- **应发帖档期** = 窗口内 daily（工作日）+ star（周六）+ weekly（周日）逐次核对；
- **发帖成功率** = 发布行中 `published` 的 ok 数 ÷ 应发帖档期数；
- **预期跳过**（不计失败）= `weekend` / `not_saturday` / `publish_dup`，单列展示；
- **降级率** = `degraded` 行 ÷ 总运行行（原因归类：dry-run token 失败 / 发帖重试后失败 / 未配置凭据）；
- **采集健康** = `source_stats` 中每个源 `fetched > 0` 且无 error；
- **内容效果（加分项）** = `post_views` 表窗口内帖子的 h24 浏览数。

### 5.2 自动回复 AI

四路数据交叉印证（服务挂了也能出报告）：

1. **请求侧**：host nginx 访问日志（`docker logs sse_market_server-nginx_proxy-1 --since`）→ 窗口内 `/api/v1/assist/` 总请求数、状态码分布、**可用率 = 非 5xx ÷ 总数**；
2. **存活性**：SSH 进 pod（`sshpass` → `127.0.0.1:22012`）检查 `main.py` 进程与 8080 监听；
3. **链路侧**（服务存活时）：pod 上 `logs/{service,assist,sync}.log` → 错误聚类（LLM 通道切换、检索降级、embedding 失败、同步周期失败）；
4. **业务侧**：集市库 `market.assist_responses` → 窗口内新增应答数、用户顶/踩。

### 5.3 系统层

磁盘水位（≥80% 提醒、≥90% 告警）、证书到期天数（certbot；取不到则标 N/A）、两个服务的运行状态与上次退出结果。

## 6. 报错分析与运维建议的产出

两层结构，确定性与智能性分开：

1. **知识库（确定性）**：`现象签名 → 根因 → 处置步骤`，命中即给标准处置。首批沉淀自真实事故：
   - `upstream prematurely closed connection` / 502 → pod 进程消失 → 重启流程 + 存活探针检查；
   - 磁盘 ≥90% → 历史整机事故 → 清理 build cache / 日志轮转；
   - nginx conf 改了但容器未重启 → `restart nginx_proxy`；
   - embedding HTTP 502 → 向量服务不可用 → 检查 embed 服务；
   - market token 过期 → 重新登录取 token；
   - `weekend` / `not_saturday` / dry-run 降级 → **预期行为，不告警**。
2. **LLM 增强**：把「指标 + 错误聚类 + 知识库命中项」交给 LLM 写本次「分析与建议」，对未命中过的新错误给出候选根因。复用 tech-digest 已验证的 LLM 通道（`SSE_MARKET_*` / `DEEPSEEK_API_KEY`）；调用失败自动退回知识库模板。

## 7. 邮件报告结构

```text
主题：【集市运维报告】09-20 ~ 09-22 ｜ 发帖成功率 100% ｜ 应答可用率 0% ⚠️

一、结论摘要        （3 行内：整体健康度 + 需立刻处理的事）
二、自动发帖机器人   （成功率表 / 报错聚类明细 / 运维建议）
三、自动回复 AI      （可用率 / 错误码分布 / 内部降级统计 / 运维建议）
四、系统层          （磁盘、证书、服务状态）
五、附录            （归档路径、原始数据 JSON 位置）
```

同时归档 HTML + 原始 JSON 到 `ops_report/data/reports/YYYY-MM-DD.{html,json}`，可追溯。

## 8. 调度与首次发送

- `ops-report.timer`：`OnCalendar=*-*-* 21:30` + `Persistent=true`（每天触发，重启后补跑）；
- **2 天守卫**：`state.json` 记录上次发送日期，日期差 < 2 天则跳过退出。
  - 为什么不用「每两天」日历表达式：systemd 无法可靠表达跨月奇偶；日跑 + 守卫还能在服务器重启后补跑，且以后改成每天/每周只需改一个配置值。
- **部署后立即发一封测试邮件**（验证 SMTP 与渲染全链路）；正式周期从 **2026-09-22 21:30** 起算。

## 9. 失败兜底

- 邮件发送失败 → 重试 3 次（间隔 60s）→ 仍失败写 `data/alerts/`，并在下次报告开头标红；
- 单个采集源失败（如 pod 连不上）→ 该模块标记「数据不可得」，**报告照发**，不让整体失败；
- LLM 不可用 → 知识库模板兜底；
- 采集量保护：nginx 日志只做 `--since` 窗口内的流式计数/分类，不落全量（48h 有 1.4 万+ 条）。

## 10. 安全

- 163 授权码（用户 2026-09-20 提供）与 pod SSH 口令写入 `ops_report/.env`：权限 600、`.gitignore` 已覆盖 `.env`、**不打印进日志、不写入本仓库任何文件**；
- 运行归档 `ops_report/data/` 加入 `.gitignore`（属服务器运行时产物，非源码）；
- 全部采集只读（MySQL 只 SELECT，与 `marketdb.py` 同约束）。

## 11. 测试策略

- pytest 单测：成功率计算（空窗口 / 全降级 / 周末规则）、错误聚类归一化、2 天守卫、HTML 渲染、SMTP 重试（mock）、各采集器降级路径（连不上 pod / 无 docker）；
- 端到端：`python -m ops_report dry-run` 生成报告不发送，人工确认后再开真发。

## 12. 非目标（YAGNI）

- 不做短信/电话告警、不做实时监控面板、不做自动修复（只给建议）；
- 不改动两个业务服务的代码；
- **本期不修 assist 的 502**（用户指定只建报告系统；该事件会被第一封报告如实记录）。

## 13. 风险

| 风险 | 缓解 |
|---|---|
| pod SSH 口令轮换 | 该模块降级为「数据不可得」，报告照发并提示口令失效 |
| 163 授权码失效 | 发送失败走重试 + 本地告警文件，下次报告标红 |
| 报告代码与 tech-digest 同目录，可能互相影响 | 独立子包（`ops_report/`），只读引用、不改动现有文件；单测与现有测试并存，互不加载对方代码 |
| 时间语义 | 全部按服务器本地时区（CST）计算，窗口以「上次发送时间」为界防漏防重 |
