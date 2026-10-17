# post-curator · 集市精华帖自动策展

秋坞定时任务（mode: job）：每日 10:20 扫描近 3 天未加精帖，把「正文 + 高赞评论」
交给 LLM 评审，对**高价值问询**（价值在评论区）与**高价值内容帖**自动加精；
**引战/灌水帖无论互动数据多高一票否决**。另备 `--full` 全量模式（不进调度，
手动 ad-hoc Job 触发）。

- 2026-10-14 立项。背景：后端原「优质贴」（`posts.is_high_quality`，64 篇，历来
  手工 SQL 设置）整体转「精华帖」概念；本任务是它的第一个自动化写入方。

## 快速使用

```bash
python main.py scan                 # 每日班（秋坞 CronJob 调的是这条）
python main.py scan --dry-run       # 评审+审计落盘，不调写通道（门禁用）
python main.py scan --full          # 全量模式：按互动信号预排 top N 评审
python main.py scan --limit 5       # 调试：只评前 5 帖
```

## 架构（三条路径）

| 路径 | 通道 | 说明 |
|---|---|---|
| 读 | `xiaoqiu_ro`@127.0.0.1:3506 → 只读视图 | `v_posts_public`/`v_pcomments_public`，零基表权限；job pod `net: host` 直达回环 |
| 写 | `POST /internal/featured`（Go 后端） | `X-Internal-Token` 守卫，单一写方，写后失效主页浏览缓存；本任务绝不直改库 |
| LLM | 秋坞网关 qwen-3.8-flash | `QDOCK_API_KEY=@platform` 哨兵注入租户钥；**只用 qwen，无备通道**（2026-10-14 用户裁决），失败由 10:45 告警邮件兜底 |

评审规则（`app/judge.py`）：LLM 输出 `{post_type, flame_bait, content_value 0-10,
comment_value 0-10, reason}`；**加精与否在代码里判**——`not flame_bait AND
(正文≥7 OR 评论区≥7)`，门槛可调参不重跑。0-10 小量表 pointwise + 引战一票否决，
是业界内容审核「级联流水线」（廉价预筛→LLM 评审→确定性规则）的落法。

## 秋坞接入（qiudock 仓 agents/post-curator.yaml）

- 调度：每日 `20 10 * * *`（错峰 tech-digest 09:15，赶在 10:45 告警巡检前）
- 全量模式手动触发：`kubectl create job --from=cronjob/post-curator-full` +
  RUN 追加 `--full`（不碰武装 CronJob，手法同 tech-digest 补发）
- 状态盘：`/work/state`（decisions.jsonl 追加留痕，一帖一评一行）

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | 秋坞网关 ClusterIP | OpenAI 兼容 /v1/llm |
| `LLM_MODEL` | `qwen-3.8-flash` | models.json 目录名 |
| `FORUM_DB_USER/PASSWORD` | - | xiaoqiu_ro（部署时注入，不入 git） |
| `INTERNAL_TOKEN` | - | 与 Go 三端一致（ops_tokens.env 正典） |
| `CURATOR_LOOKBACK_DAYS` | 3 | 每日扫描窗口 |
| `CURATOR_CONTENT_THRESHOLD` | 7.0 | 正文价值加精线 |
| `CURATOR_COMMENT_THRESHOLD` | 7.0 | 评论区价值加精线（问询帖核心） |
| `CURATOR_MAX_JUDGE` | 80 | 单次评审上限（成本护栏） |
| `CURATOR_FULL_TOP_N` | 600 | 全量模式评审 top N |
| `CURATOR_REJUDGE_HOURS` | 20 | 同帖评审冷却（3 天窗口内每日可复审一次） |

## 退出码

0 成功 / 2 数据库不可用 / 3 LLM 全员失败 / 4 写通道失败（判定已留痕，可人工补）。
