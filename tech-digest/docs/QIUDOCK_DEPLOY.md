# tech-digest 秋坞（QiuDock）部署

> 2026-10-08 割接完成。本服务现由秋坞平台调度（k8s CronJob×3），原 systemd timer
> 已停用（unit 文件保留 = 回滚锚）。本文记录部署形态、环境变量契约与运维入口，
> 供仓库协作者对照。
>
> **同日补充**：`ops_report`（每两天 21:30 的整机巡检邮件）也于 2026-10-08 停用
> （`systemctl disable --now ops-report.timer`，unit 保留可恢复）。它是巡检工具
> 而非业务组件，需 docker.sock+SSH 供给、暂无秋坞形态；恢复 =
> `systemctl enable --now ops-report.timer`。
>
> **下一步演进**：编辑部迭代计划（content pool + 编辑部流水线 + 高价值沉淀）见
> [EDITORIAL_PLAN.md](EDITORIAL_PLAN.md)——M1 池子底座 / M2 质量环 / M3 沉淀，
> 含全部决策理由与上手指南。

## 部署形态

```
GitHub monorepo（本仓库）
  → 秋坞 repo 模式：pod 每次运行时 clone → pip → sh -c "run"
  → spec: qiudock 仓 agents/tech-digest.yaml（调度/门禁/挂载/env 的唯一事实源）
  → 门禁 = 试运行 --dry-run（渲染落盘、不发帖不写库），通过才武装（suspend 翻转）
```

- 调度：`daily 09:15`（deadline 24h 补跑）/ `star 周六 09:25` / `weekly 周日 10:00`，
  与原 systemd timer 一致；`Forbid` 不重叠、`BackoffLimit=0` 不自动重试（同日幂等
  由 data/ SQLite 去重保证，重跑走控制台「立即运行」或 POST /v1/agents/tech-digest/tasks/<task>/run）。
- 仓库零改动适配（run 命令内完成）：依赖在 run 内安装（`pip install requests beautifulsoup4`，
  走清华镜像）；data 状态盘挂载到 `/work/state` 再软链为 `tech-digest/data`
  （挂载点若落在 clone 目录内会导致 git clone 失败）。

## 代码改动（已合入 main）

仅一处：`app/config.py` 的 `SSE_MARKET_API_KEY` 未配置时回落 `QDOCK_API_KEY`。
独立部署（systemd/.env）行为不变；秋坞部署时 LLM 鉴权自动用平台注入的租户钥匙。

## 环境变量契约

| 变量 | 秋坞部署取值 | 说明 |
|---|---|---|
| `QDOCK_API_KEY` | `@platform` 哨兵 | 部署时注入 `agkeys-tech-digest` secret 的租户钥匙，raw key 不进 spec/历史 |
| `SSE_MARKET_BASE_URL` | `http://10.43.196.33:7700/v1/llm` | 秋坞 LLM 网关（OpenAI 兼容），平台 ClusterIP |
| `SSE_MARKET_MODEL` | `qwen-3.8-flash` | models.json 目录名；见下方模型变更记录 |
| `DEEPSEEK_API_KEY/MODEL` | 部署时注入 | 备用通道（Anthropic 兼容），与主通道故障域隔离 |
| `MARKET_REFRESH_TOKEN/USER_TELEPHONE/BASE_URL` | 部署时注入 | 集市发帖凭证（BASE_URL=172.18.0.4:8080 docker 桥直连） |
| `TECH_DIGEST_*` | spec 显式设定 | 调优项，与原 .env 同值 |

secrets 一律不入库：spec 文件里是占位符，部署时从服务器
`/root/SSE_Market/SSE-Agent/tech-digest/.env` 注入。

## 模型变更记录（重要）

- 旧模型 `qwen3.8-27b-awq` 已从 api.ssemarket.cn 下架：**2026-09-01 起主通道持续
  失败（"模型不存在或未启用"），线上日报实际一直走 DeepSeek 备用通道**（llm.py
  双通道自动切换，日志仅记 "AI 备用通道生效"）。
- 2026-10-08 切秋坞网关 + `qwen-3.8-flash`（GPU 实际模型 qwen3.8-flash-next），
  `reasoning_effort: "none"` 实测有效（关思考、直接出 content），主通道恢复。

## 可观测性

- 秋坞控制台 → tech-digest：定时任务面板（计划一览 / 立即运行 / cron 运行历史）、
  版本·部署（版本链 / 门禁记录 / 一键回滚）。
- LLM 调用经网关全量记账：audit `llm.call`（tenant/model/tokens/latency）+
  Langfuse span + 网关指标（qdock_llm_calls_total 等）。
- 失败告警：宿主 crontab `45 10 * * * /usr/local/bin/qdock_cron_alert.sh` 查当日
  cron 台账，失败/缺席发邮件（复用 Go /internal/mail 链路）。
- pod 日志：小秋 Ops 面板 工作负载 → pod 下钻（tail/previous），或
  `kubectl logs -n qiudock ag-tech-digest-<task>-<version>-<job>`。

## 回滚

1. 秋坞内回滚：控制台一键回滚到锚点版本（上一版 CronJob 保留在 suspend 状态）。
2. 回 systemd：`systemctl enable --now tech-digest-daily.timer tech-digest-star.timer
   tech-digest-weekly.timer`（unit 未删）。同日双发由 data/ 去重兜底，但建议择一运行。
