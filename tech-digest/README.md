# tech-digest · 前沿技术周刊

服务器空闲时自动抓取多源技术动态（GitHub Trending + Hacker News + 少数派 + 知乎热榜等，
每日 09:15），产出每日《Tech Digest》日报并自动发布到集市「技术分享」分区（公开帖）；
周日 10:00 汇总一周生成**五段式周报**（本周回顾 / 深度长文 / 精选回看 /
下周前瞻 / GitHub 周榜）并自动发布。

- 需求与验收：`../前沿技术周刊自动爬虫与AI周报功能-用户需求文档PRD.md`
- 设计：`../前沿技术周刊-技术实现文档.md`

## 快速使用

```bash
.venv/bin/python main.py daily --dry-run        # 日报：渲染落盘，不发帖（校验到 token）
.venv/bin/python main.py daily --force          # 日报：同日重新生成
.venv/bin/python main.py weekly --dry-run       # 周报：仅预览五段 + 校验 token（不落库不发帖）
.venv/bin/python main.py weekly --force         # 周报：重新生成并重新发布
.venv/bin/python main.py weekly --private       # 周报：发布为私密帖（默认公开帖）
```

- 输出：`output/YYYY-MM-DD-daily.md`、`output/YYYY-Www-weekly.md`、`*-weekly-preview.md`（预览）
- 状态：`data/tech-digest.db`（daily_snapshot / weekly_report / run_log / meta 期数计数）
- 日志：`log/tech-digest.log` + `journalctl -u tech-digest-daily`

## 调度（systemd timer）

| 任务 | 时间 | 说明 |
| --- | --- | --- |
| daily | `*-*-* 09:15` | 多源抓取 → AI 日报 → 发帖；同日幂等，发帖失败重试 1 次 |
| weekly | `Sun *-*-* 10:00` | 六段式周报；AI×2 独立降级；--dry-run 不落库（不挡正式发布） |

## 部署

```bash
scp -r tech-digest market-server:/root/market-deploy/agent/
ssh market-server "bash /root/market-deploy/agent/tech-digest/scripts/deploy.sh"
# 然后编辑 .env 填入 SSE_MARKET_API_KEY / MARKET_REFRESH_TOKEN / MARKET_USER_TELEPHONE
```

设计红线（详见 PRD §1.3）：一次性进程、零耦合、磁盘 ≤3MB/月、
AI 双通道自动切换（主 market-deploy qwen3.8-27b-awq → 备 DeepSeek，2026-09-01 起）、失败降级不中断。
部署后 .env 需配 SSE_MARKET_API_KEY（或 DEEPSEEK_API_KEY 至少其一）。
