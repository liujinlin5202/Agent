# M3 高价值沉淀消费：weekly 深度长文段接 reserved（2026-10-08）

> 上位计划 [EDITORIAL_PLAN.md](../../docs/EDITORIAL_PLAN.md) §4：
> 「weekly 的『深度长文』段消费本周 reserved 沉淀——高价值二次曝光」。
> 验收：下周日首个新段落上线；reserved 样本可追溯（score_detail 完整）。
> 前两里程碑：[M1](2026-10-08-editorial-m1-content-pool.md) /
> [M2](2026-10-08-editorial-m2-quality-ring.md)。

## 0. 改动全景

| 文件 | 改动 | 性质 |
|---|---|---|
| `app/weekly.py` | 深度长文段取材优先本周 reserved（含研究笔记素材），空则走原路径 | 核心改造 |
| `app/pool.py` | 加 `reserved_since(monday)` 查询 | 配套 |
| `tests/test_weekly.py` | 新增 reserved 取材/回退用例 | 测试 |

发布面不变：周报仍五段式、周日 10:00 一篇；reserved 生命周期（M1 sweep
90 天过期）与标记（M2 落选 ≥0.8）已在库，本期只补「消费」这一环。

## 1. 决策记录

### D1 取材口径：本周 reserved + 有评分，按 score 降序取前 8

- 「本周」按 first_seen ≥ 本周周一（条目入池时间）——与周报聚合窗口一致；
  上周沉淀的条目属于上周选题，不再复读。
- 必须有 score（M2 评委产物）——reserved 的入场券本来就是 score≥0.8，
  双重保险防脏数据。
- 上限 8 = 现有 DEEP_CANDS，LLM 预算不变（仍是 1 次深度长文调用）。

### D2 素材升级：研究笔记直接进深度长文 prompt

reserved 条目自带 M2 的 research（要点/引文）——深度长文 prompt 在标题/
摘要外附要点清单，500-1000 字成稿从「凭摘要推演」升级为「凭精读笔记写作」。
这是本周高价值文章的**第二次**消费（第一次是落选当天的评分），也是
「沉淀复用」验收的落点。

### D3 空沉淀/池故障 → 原路径整体回退

本周无 reserved（M2 上线首周大概率如此：评分今天才开始积累）→ 深度长文
段走现状 `_agg_news` 取材，一字不改。PoolStore 异常同理 fail-open。
**验收里「下周日首个新段落上线」的预期就是：首周日大概率还是老路径，
等 reserved 积累一周后新段落自然出现——这是特性不是缺陷，记录在案。**

### D4 reserved 不因周报消费而迁移状态

周报消费是「曝光」不是「消耗」：reserved 保持 reserved 至 90 天过期
（M1 sweep）。理由：深度长文是二创转述而非全文搬运，不产生「重复发布」；
保留状态让将来反馈环（M1 D-decision 预留的 digest_date 接口）能继续追
踪这批文章的长期价值。

## 2. 测试计划

- 本周 reserved 有货 → 深度长文 prompt 含标题+研究要点；无 → 原取材；
  pool 异常 → 原取材；跨周 reserved 不进素材。
- 回归全量绿；LLM 全 mock。

## 3. 验证记录（2026-10-08 实测）

- 全量回归：614 用例全绿（M3 新增 4）。
- weekly --dry-run 真实 pod：rc=0，deep/intro 两段 AI 正常，review 段走既有
  独立降级（非本轮改动面）；reserved 为空 → 老路径，回退无恙。
- 新段落首秀预期：本周末 reserved 开始积累，下周日（10-18）周报的深度长文
  段首次出现「reserved 沉淀 + 研究笔记」取材形态。
