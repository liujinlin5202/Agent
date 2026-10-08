# M1 池子底座：实现决策记录（2026-10-08）

> 上位计划：[EDITORIAL_PLAN.md](../../docs/EDITORIAL_PLAN.md)（2026-10-08 用户裁决定稿）。
> 本文是 M1 的落地文档：每个实现决定 + 理由 + 业界对照，写给评审者与后续迭代者。
> M1 验收（上位计划 §6）：ingest 跑两轮池子有货；daily 照常发帖（内容源=pool）；
> 双门禁（ingest/daily --dry-run）绿。

## 0. 改动全景

| 文件 | 改动 | 性质 |
|---|---|---|
| `app/pool.py` | 新增：content pool 数据访问层（PoolStore） | 新能力 |
| `app/store.py` | `_conn` 提升为 `connect_db`，加 WAL + busy_timeout | 基础设施 |
| `app/dedup.py` | `is_duplicate` 加 quick_ratio 剪枝（池化后对比量放大，语义不变） | 性能 |
| `app/config.py` | 新增 6 个 pool 相关 env（全部有默认值） | 契约 |
| `main.py` | 新增 `ingest` 任务；daily/star 取材切 pool（空池回退老路径） | 核心改造 |
| `tests/test_pool.py` `tests/test_ingest.py` | 新增 38 个用例 | 测试 |
| `tests/test_main.py` | 两个 helper 显式打桩池函数（旧用例=回退路径回归） | 测试适配 |
| `docs/QIUDOCK_DEPLOY.md` | env 契约表补齐 + ingest 调度说明 | 文档 |
| `tests/test_llm.py` | 修 1 处环境依赖断言（见 §6/D12） | 存量修复 |

不改：publisher（发帖）、digest（渲染）、dedup 算法本体、weekly（M3 才接 reserved）、
LLM 链路（M2 才动）。**发布面不变**：仍每天一篇日报帖、周六星榜、周日周报。

## 1. 逐条决策记录

### D1 pool 落现有 SQLite 同库新表，不引新组件

上位计划 §5.2 已裁决（单机宿主、数据盘已挂载、上 PG 是过度设计）。落地时的
子决定：**pool_items 与现有表同库**（`tech-digest.db`）而非独立 db 文件。

理由：a) ingest/daily 两 pod 罕见同碰，WAL + busy_timeout 足够（上位计划已论证）；
b) 同库让 `Store.cleanup()` 的保留策略、备份、软链挂载（`/work/state`）全部零改动；
c) 两 db 文件会出现「一半数据在 A 盘快照、一半在 B」的割裂心智。

否决的备选：独立 `pool.db`——隔离更干净但运维面翻倍，换不来实际收益。

### D2 数据访问层 = 新 `app/pool.py` 的 PoolStore，连接工厂单一实现

- PoolStore 持自己的 sqlite3 连接（`app.store.connect_db` 工厂产出），与 Store 平级：
  ingest pod 只需要 pool 表，不必为写池子先建 daily_snapshot 全家表。
- **连接 pragma（WAL / busy_timeout）只写在 `connect_db` 一处**，Store 与 PoolStore
  共用——同语言禁两份实现（模块复用三原则）。Store 原私有 `_conn` 更名提升，
  全仓唯一调用方在 store.py 内部，无外部引用（已 grep 核实）。
- pool 的 DDL（POOL_SCHEMA）随 PoolStore 首次实例化自动建表+建索引：与 Store 的
  `executescript(SCHEMA)` 同款幂等模式，部署日无需手工迁移。

### D3 pool_items 表形状：信号列平铺 + raw 无损列

```sql
pool_items(id, kind, url UNIQUE, title, source, author, published_at,
           raw,              -- 原始 v2 条目 JSON，无损（渲染/深读都要回读原文形状）
           heat, source_weight, age_days,   -- 入池时的三个粗排信号
           status,           -- candidate | daily_used | reserved | expired
           score, score_detail, research, review,  -- M2 编辑部产物（本期只留位）
           digest_date,      -- 被哪期日报消费（反馈环地基，上位计划 §5.4）
           first_seen, last_seen)
```

- **raw 列存完整 v2 条目**：候选行送 gate/ai_pick/渲染时直接 `json.loads(raw)`，
  与老路径的 item dict 完全同构——下游零适配。信号列平铺出来是为了粗排一条
  SQL 拿全数据 + 将来反馈环直接 `SELECT heat, score` 分析，不用解 JSON。
- status 用 TEXT 不用整数：run_log/排查时人眼可读，量级（千行）不在乎性能。
- UNIQUE(url)：url 是 `dedup.normalize_url` 规范化后的值（去跟踪参数/fragment/
  排序 query），**去重的唯一权威键**；标题模糊查重是第二道（见 D5）。

### D4 粗排公式：heat 分位归一 + 线性新鲜衰减（简单可解释优先）

```
rank_score = heat + source_weight * 10 - pool_age_days * 5
```

- **heat ∈ [0,100]，每源批内分位归一**（ingest 时算好落库）：HN points /
  知乎热度 / devto 赞数量纲互不可比，min-max 会被单条爆款拉爆；分位（rank
  percentile）把「源内第几热」变成跨源可比。Anthropic 多 agent 复盘的 effort
  分级思想：粗排只要序不要绝对值，分位够用且天然稳健。
  无热度信号的订阅源（ars/ieee/fcc/阮一峰/见闻）固定 50 分——保底可见，
  与老 `_daily_candidates` 给订阅源保底配额的意图一致。
- **source_weight**：热榜源 8（zhihu/hn/devto/sspai），订阅源 6（内容质量稳定但
  无热度佐证），github-trending 不参与 news 粗排（kind 隔离）。
- **pool_age_days 按 first_seen 实时算**（不落入库的 age_days）：条目在池里
  每多待一天扣 5 分，7 天 TTL 到期自然沉底——「全窗口选最优」的时效杠杆就
  在这一项。入池时的 age_days 仅作快照记录（published_at 可考时的真实年龄）。
- 否决的备选：学 LTR 加权学习排序——反馈环数据（digest_date）M1 才开始攒，
  现在上学习排序是无米之炊；公式保持一个高中生能算，出问题一眼能定位。

### D5 入池去重：URL 精确（权威）+ 标题模糊（防线），语义与老路径对齐

- URL 命中已有行 → **刷新 last_seen + raw（不插新行）**：同一篇热榜文章隔班
  重现是常态，刷新让池龄与热度保持新鲜；标题/热度用最新值覆盖。
- 标题相似 ≥0.85 命中**近 14 天池内任意状态的行** → 丢弃不入池：
  14 天窗口对齐老路径 `_dedup_history` 的口径（v4 起就是 14 期）；查「任意
  状态」是为了拦住这个案例——文章 3 天前已发过帖（daily_used），今天换个
  URL（知乎专栏镜像）再上榜，若只查 candidate 会重复发布。
- 老路径的 `_dedup_history`（对 14 期快照去重）在回退模式下原样保留，
  两条路径各自完备，不互相依赖。

### D6 daily 取材切换：池优先、老路径兜底，「永不因池空停刊」

daily 新流程（池模式）：

```
pool.top_candidates(粗排 top24) → _prefetch_gate(可爬性预取，原样复用)
  → ai_pick(挑 1，原样复用) → _fetch_body → render → save_daily_v2 → publish
  → 发帖成功 → pool.mark_consumed(选中 url, digest_date=今天)
```

- **粗排截 24 再走老闸门**：gate 每次试抓是真实 HTTP（预算护栏 14 次上限），
  池子全量（可能上百条）直接灌 gate 会放大反爬风险；24 与老候选池规模（48
  上限、gate 只看前几组）同量级。
- gate / ai_pick / _fetch_body / render / publish **一行不改**：M1 只换候选来源，
  挑选与发布行为与线上逐字节兼容——这就是「daily 照常发帖」验收的实现方式。
- **空池回退**：`top_candidates` 返回空 / PoolStore 抛异常 → 打一行日志 +
  `run_log` 记 `pool_fallback`，整段走老路径（即时 fetch_all → 去重 → 老候选）。
  触发面：ingest 部署前的首日、ingest 连续故障、新库初始化。
- **mark_consumed 只在发帖成功后调**：周末不发帖、发布失败、dry-run 都不算
  消费——候选明天还能被选，不会因为一次未遂发布丢失。
- daily_snapshot 照常写（weekly 聚合与老路径标题去重依赖它）：池模式下存
  「当日粗排候选的 raw + 最近 trending 快照」，weekly 的五段聚合照常工作（见 D8）。

### D7 star 取材切 pool：trending 也入池，48h 新鲜度线

- ingest 同时把 github-trending 的 trending 条目入池（kind='trending'），
  UNIQUE(url) 同样生效；**同名仓库隔日重抓 → 刷新 last_seen + raw**（today_stars
  每天在变，raw 必须刷新，star 帖的「今日 +N」才不会发陈旧数字）。
- star 读池：`latest_trending(limit=15, max_age_h=48)`，按 trending.rank 升序
  （即当日榜序）。48h 线的依据：ingest 每 2h 一班，正常情况下周六 09:25 必有
  当天 07:07 的数据；连续两班 ingest 挂掉才需要警惕，48h 给足冗余又不至于
  把「周三的星榜」当「今日星榜」发出去。
- 池里 48h 内无 trending → 回退 `fetch_all(["github-trending"])` 老路径。
- trending 行不参与 candidate/expired 生命周期（kind 隔离），由独立规则清理
  （last_seen 超 7 天的行由 sweep 顺带 DELETE，防池子被 trending 无限填充）。

### D8 weekly 在 M1 完全不动——快照契约保持是前提

weekly 聚合 `daily_snapshot`（`get_daily_since(monday)`）。M1 坚持每日快照
照写（D6），weekly 五段零改动。上位计划里 weekly「深度长文段消费 reserved」
是 **M3** 的事；M1 若顺手改 weekly 会把两个里程碑的验收搅在一起。边界纪律：
每个里程碑独立可上线、可回滚。

已知语义偏移（记录在案，M3 复审）：池模式下每天快照=当日粗排候选，池里
驻留多天的落选条目会被多天快照重复收录，weekly 的「周内出现 N 天」从
「多源同报」变成「持续热度」——语义其实更准，但 M3 改 weekly 深读段时要
把这一点讲给渲染逻辑。

### D9 反爬冷却从 M2 提前到 M1（对上位计划的一处有序调整）

上位计划 §5.5 把「该源冷却 24h」列在 M2 兜底清单里，但它是 ingest 的自带
属性：没有冷却，ingest 每 2h 一班会对已 403 的源一天硬顶 12 次——上位计划
§4 的 ingest 框图里它本来就有，属于「计划文字归档到 M2、逻辑属于 ingest」。
提前落地，实现：源错误消息匹配 403/405 → `meta` 表记
`pool_cooldown:<source> = <恢复时刻 ISO>`，ingest 每班开头剔除冷却中的源，
到点自动解冻。meta 表复用现有基础设施，不加新表。

### D10 ingest 的 --dry-run 语义：不写池、写 run_log

门禁（evals ingest-dryrun）要验证「抓取与入池链路健康」，但 dry-run 写池会
污染生产数据（与 2026-09-14 daily dry-run 递增期数的事故同型）。定界：
**dry-run 跑完整链路（抓取→归一→查重计算→信号计算），唯一区别是不落
pool_items**；run_log 照写（`dry_run` 标记）——daily 的 dry-run 也是这么处理
run_log 的（`dry_run_token_ok` 会入库），口径一致。观测面（控制台运行历史）
因此能看到门禁试跑记录。

### D11 调度间隔 env 的落位：契约先行，消费者是平台 spec

`TECH_DIGEST_INGEST_INTERVAL_H` 在代码里没有逻辑消费者（间隔由调度器表达，
不在进程里 sleep）。它进 config 与契约表的原因：a) 秋坞 spec 的 schedule 与
deadline 要引用同一语义值，env 是单一事实源；b) 将来 ingest 若做自调节
（反爬升级自动降频），开关已就位。M1 阶段它的消费者是 qiudock spec
`agents/tech-digest.yaml`（`7 */2 * * *`、deadline 14400）——**该改动属
qiudock 仓，跨仓动作按纪律需另行确认后提交**。

### D12 修存量测试 test_llm 的环境依赖断言（顺手的卫生修复，非本里程碑范围扩张）

`test_primary_fail_switch_deepseek` 断言主通道 URL 含小写 `market`，实际依赖
服务器 `.env` 的 `api.ssemarket.cn`；无 `.env` 的环境（CI、新克隆）用默认
占位符 `api.<MARKET_DOMAIN>`，大写，必挂。修法：两处断言改大小写不敏感
（`"market" in url.lower()`）。测试意图（主备通道分流）不变。
克隆基线实测：549 用例 1 失败 1 跳过，失败即此条，与本轮改动无关。

### D13 ingest 不接负载闸门

daily/star 沿用 `/proc/loadavg` 负载闸门（"空闲才爬"），ingest 刻意不接：
入池是低风险纯 HTTP（实测全班 ~50s、10 源、约 110 请求），且池子断供的
代价（daily 被迫回退即时抓取、时效质变消失）远高于一次爬取。负载保护交由
秋坞 job 的显式 resources（400m/512Mi）承担；如需人工停池，平台侧 suspend
ingest 调度即可（daily 自动回退老路径，发布不断）。

### D14 测试基建的 Windows 卫生（test_ingest tearDown 加 gc.collect）

Windows 开发机上，sqlite 语句句柄的引用循环会延迟 db 文件句柄释放，临时
目录清理报 WinError 32 文件锁（断言全过，纯清理竞态；Linux 无此问题）。
tearDown 先 `gc.collect()` 再删目录。修的就是开发体验，不碰测试语义。

## 2. 新增配置契约（与上位计划 §7 一字不差，全部有默认值）

| 环境变量 | 默认 | M1 消费者 |
|---|---|---|
| `TECH_DIGEST_INGEST_INTERVAL_H` | 2 | 文档/spec 语义值（见 D11） |
| `TECH_DIGEST_LLM_CONCURRENCY` | 1 | M2 LLMWorkerPool（本期仅入 config） |
| `TECH_DIGEST_POOL_TTL_DAYS` | 7 | sweep：candidate 过期 |
| `TECH_DIGEST_RESERVED_TTL_DAYS` | 90 | sweep：reserved 过期 |
| `TECH_DIGEST_REVIEW_THRESHOLD` | 0.7 | M2 终审打回线（本期仅入 config） |
| `TECH_DIGEST_RESERVE_SCORE` | 0.8 | M2 沉淀线（本期仅入 config） |

## 3. 流程终态

```
ingest（每 2h，7 分错峰，零 LLM）
  剔除冷却中源 → fetch_all → news/trending 分流
  → news：URL 命中刷新 last_seen+raw ／ 标题撞 14 天池丢弃 ／ 否则 insert candidate
  → trending：URL 命中刷新（今日星数滚动更新）／ insert
  → sweep：candidate 超 TTL→expired；reserved 超期→expired；过期 90 天→DELETE；
           trending 超 7 天→DELETE
  → run_log(inset, ok/degraded, {per-source stats, pool counts})

daily 09:15（发布面不变）
  pool.top_candidates(24) ──空/异常──→ 老路径 fetch_all（永不因池空停刊）
  → _prefetch_gate → ai_pick → _fetch_body → render → save_daily_v2 → publish
  → 发帖成功 → mark_consumed(url, digest_date)

star 周六 09:25
  pool.latest_trending(15, 48h) ──空──→ fetch_all(["github-trending"])
  → render_star_post（渲染零改动）

weekly 周日 10:00：零改动（吃 daily_snapshot）
```

## 4. 测试计划（unittest，随仓惯例）

- `tests/test_pool.py`：schema 幂等/WAL pragma；news 入池三路（新增/刷新/标题撞弃）；
  粗排公式与排序；mark_consumed 语义（状态+digest_date）；sweep 全规则；
  latest_trending 的 48h 线与 rank 序；冷却读写与解冻。
- `tests/test_ingest.py`：run_ingest 全流程（monkeypatch fetch_all）——入池计数、
  403 触发冷却、dry-run 零写池、源冷却剔除；daily 池模式/空池回退切换；
  star 池模式/回退；发帖成功才 mark_consumed。
- 回归：全量 549+ 用例保持绿（含修后的 test_llm）。

## 5. 部署与回滚（实际执行记录，2026-10-08）

1. 本仓 push main（2c4aea0..bca8608）→ 秋坞 POST /v1/deploys（`scripts/deploy_td.py`
   做占位符注入）→ 版本 **v-2610080640**，门禁 2/2、CronJob×4（含 ingest）武装，
   回滚锚 v-2610080401。
2. qiudock 仓 `agents/tech-digest.yaml`（commit 8a9c030）：schedules 增 ingest
   （`7 */2 * * *`，deadline 14400）、env 增 §2 六项、evals 增 ingest-dryrun。
3. **部署验收揪出平台级 bug**（agent-run hollow exec，python 从未在平台 job 里
   执行过，割接日门禁 15s 通过即其假象）——独立复盘见
   [INC-20261008-QD02](2026-10-08-incident-agent-run-hollow-exec.md)。当日热修
   镜像（digest f94f7cfe）并收编脚本入 qiudock 仓（a8fd308）。
4. 验证记录（§7 更新）：生产 ingest 班全链路 62s 跑通、池 64+13 入库；
   clone 失败场景正确 Failed（可见失败）。
5. 回滚：控制台一键回滚锚点；ingest 调度可单独 suspend（池停摆，daily 自动
   回退老路径，发布不断）。

## 6. 基线快照（开工前实测，2026-10-08）

- 代码 = GitHub main 2c4aea0（编辑部计划定稿版）。
- 全量测试 549 个：1 失败（D12 所述环境依赖）+ 1 跳过，其余全绿。
- Windows 本机 Python 3.11.11，requests/bs4 可用。

## 7. 验证记录（2026-10-08，Windows 开发机实测）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 全量回归 | `python -m unittest discover -s tests` | **587 用例全绿**（549 存量 + 29 pool + 9 ingest；1 跳过为存量） |
| ingest dry-run（门禁同款） | `python main.py ingest --dry-run` | 10 源全通，news=97 / trending=13，50.8s，rc=0，零写池 |
| ingest 真跑 ×2（验收「两轮」） | `python main.py ingest` | 第 1 轮 +97/+13；第 2 轮 +0（同 URL 全部刷新），池稳定 97/13 不翻倍 |
| 反爬冷却实弹 | 同上第 2 轮 | 知乎源 403 → 自动冷却 24h，本班剔除，其余源不受累（D9 实弹） |
| daily 池模式 | `python main.py daily --dry-run` | 取材=池内粗排 24 + trending 快照 13；闸门 6/24 验证可抓；无 key 环境按设计兜底挑首条、digest 降级渲染；rc=0 零写库 |
| 粗排公式抽查 | `PoolStore.top_candidates` | 热度高者 180 分（heat 100 + 权重 8×10），衰减与排序符合 D4 |

待服务器侧验收（部署后）：双 dry-run 门禁绿；ingest 跑两班池子有货；次日
daily 照常发帖且 run_log `material: "pool"`。
