# M2 编辑部质量环：实现决策记录（2026-10-08）

> 上位计划：[EDITORIAL_PLAN.md](../../docs/EDITORIAL_PLAN.md) §4 编辑部流水线 +
> §5.5 兜底清单。M1 已上线（见
> [M1 决策留底](2026-10-08-editorial-m1-content-pool.md)）。
> M2 验收（上位计划 §6）：评分与评审轨迹全落库；<0.7 打回链路演练一次；
> 网关故障演练走通备通道；伪并行并发度=1 验证。

## 0. 改动全景

| 文件 | 改动 | 性质 |
|---|---|---|
| `app/llmpool.py` | 新增：LLM 调用池（并行原语 + 指数退避，并发度读 env） | 新能力 |
| `app/editorial.py` | 新增：编辑部四角色流水线（评委→研究员→作者→终审） | 核心能力 |
| `app/pool.py` | 加 `get_item(url)` / `save_stage()`（断点续跑落库） | 配套 |
| `main.py` | run_daily 池模式接流水线（失败回退 M1 路径） | 接线 |
| `app/config.py` | 无新增 env（复用 §7 契约：CONCURRENCY/REVIEW_THRESHOLD/RESERVE_SCORE） | 契约不变 |
| `tests/test_llmpool.py` `tests/test_editorial.py` | 新增用例 | 测试 |

发布面不变：仍每天一篇日报帖；batch/star/weekly 链路零改动。

## 1. 流水线终态（daily 池模式）

```
pool.top_candidates(24)
  ① 评委  judge_score(cands)      2 次 LLM（12 条/批）→ 全员 score+score_detail 落库
  ② 研究员 deep_research(top7)     ≤7 次全文抓取（fail-open）+ ≤7 次 LLM 要点/引文 → research 落库
  ③ 作者  author_draft(top+research) 1-2 次 LLM（挑 1 + 编者按/导读/标题；英文则+翻译）
  ④ 终审  review_draft(draft)      1 次 LLM 0-1 分（五维加权）
     <0.7 → 打回作者带意见改一轮 → 复审 1 次（复审后无论分数照发，轨迹留库）
  发帖成功 → 选中者 daily_used（M1 机制）；score≥0.8 未入选 → reserved
任一关键步整体失败 → 整体回退 M1 路径（gate + ai_pick），永不停刊
```

LLM 预算：2 + 7 + 2 + 1 + 1 ≈ 13 次 ≈ 上位计划「12-15 次 × ~8s ≈ 3-5 分钟」。

## 2. 逐条决策

### D1 LLMWorkerPool = ThreadPoolExecutor 包 llm.chat（并行原语，串行默认）

`app/llmpool.py`：`map_chat(tasks, concurrency)`——`max_workers=settings.llm_concurrency`。
concurrency=1（现役）= 严格串行；换算力改 env 零代码改动（上位计划 §5.1 契约）。
指数退避在池的调用包装里做（1/4/16s+抖动 ×3，主通道重试尽才切 DeepSeek）——
llm.py 的单次语义不动，weekly/star 等老调用点行为不变（它们不进池，M2 不扩scope）。
用线程池不用 asyncio：llm.chat 是同步 requests，包 asyncio 是假并行；ThreadPool
是一等并行的同步等价物，且与全仓零依赖风格一致。

### D2 评委评分：批内 JSON 数组 + 标题精确绑定 + 单条失败跳过

- 批 12 条（24 候选 = 2 次调用，预算内）。输出
  `{"scores":[{"title":..., "novelty":0-10, "depth":..., "utility":...,
  "credibility":..., "audience":...}]}`；`_strip_fence` 解析（复用 daily_ai 工具）。
- **标题精确绑定**沿用 v3 实测护栏（ai_pick 的教训：编号记忆不可靠）；绑不上的
  条目该条弃评（score=None，进不了 top）——评委缺席≠停刊（§5.5）。
- 维度分 0-10 整数；总分 = Σ(维度/10 × 0.2)（五维等权）。
- **偏好注入**：preference.json 的 prefer 文本作为「加分项不是硬性条件」软注入
  评委 prompt（与 ai_pick 的 prefer_clause 同款契约）——上位计划「权重读
  preference 配置」落为：等权是基线，偏好影响评委判断而非数值权重，不新增
  配置面（preference.json 结构零改动）。

### D3 研究员：只深读 top7，全文抓取 fail-open，产物写回 pool

- 上位计划 §4「对 top6-8 深读」取 7（预算 7 次抓取 + 7 次 LLM）。
- **M1 的可爬性预取闸门退出主路径**：闸门的价值（「AI 只看摘要分不清通稿」）
  由深读步承接——研究员对 top 逐条 `_try_fetch_art`（全文只抓一次，含镜像通道），
  单条 403/超时 → 该条 research=None，作者降级用摘要成稿（§5.5 raw_summary 兜底）。
  闸门模块原样保留在 M1 回退路径里。这把发布前的抓取量从「14 次闸门试抓」
  降到「≤7 次定向深读」，反爬面反而收窄。
- 产物 JSON `{"key_points":[...], "quotes":[...], "readable":bool}` 落
  `pool_items.research`（M1 预留列）；LLM 失败 → research=None，条目继续流转。

### D4 作者：挑 1 + 成稿，护栏全量继承

作者 prompt = top7 + research 产物，输出与 ai_pick 同构
（title/why/digest/post_titles）。**三道实测护栏原样生效**：标题精确绑定、
编者按/导读与原文内容相关性校验（`is_content_related`）、长度阶梯。英文全文
走 ai_translate（含完整性机械断言）。与 ai_pick 的差异只有 prompt：作者看得到
深读要点/引文——这是「评信息量低」的正面解法。

### D5 终审：单次 0-1 分 + 一轮打回，复审后无论分数照发

- Anthropic 复盘收编第 3 条：LLM-as-judge 单次调用 0-1 分最稳。输入=编者按+
  导读（即读者实际所得）+ 深读要点，输出五维分+加权总分+**具体修改意见**。
- < `TECH_DIGEST_REVIEW_THRESHOLD`(0.7) → 打回作者（prompt 附评审意见）改一轮
  → 复审一次。**复审后无论分数照发**：轨迹（初评分/意见/修订稿/复评分）全落
  `pool_items.review`；发布面不变原则（宁发带评审注记的稿子，不停刊）。
- 终审 LLM 整体失败 → review=None 照发（评审是增强，不是门锁——这是与
  「评测门禁」的场景区别：本平台评测门禁管部署，编辑部终审管质量轨迹）。

### D6 断点续跑：产物带日期戳，重跑跳过已完成步

M2 验收要求「班次中断 → 重跑跳过已完成步骤」。实现：score_detail/research/
review JSON 内带 `"date": 今天`；流水线每步先查该字段，当日已有产物直接复用
（pod 被逐后平台 deadline 补跑、或人工 --force 重跑均不重复烧 LLM）。
schema 零改动（M1 预留列齐备）。跨天产物不复用（评分时效性）。

### D7 reserved 标记：≥0.8 未入选 → reserved（一次遍历）

作者选定后：未选中且 score≥`TECH_DIGEST_RESERVE_SCORE`(0.8) 的 top 候选标
reserved（weekly「深度长文」段 M3 消费）。落选但 <0.8 的保持 candidate——
明天池子粗排还能再选它们。

### D8 run_daily 接线：editorial 整体失败 → M1 路径整体回退

run_daily 池模式改为：先走 editorial 流水线；返回 None（评委全灭/作者失败/
异常）→ 打日志 + 走 M1 路径（gate + ai_pick），再不行走老路径（即时抓取）。
三级降级链，永不因单点停刊（§5.5 精神）。run_log detail 增 `pipeline` 字段
（editorial/legacy）与 `review_total` 供观测。

### D9 dry-run 零落库契约延伸到流水线产物

M1 D10 定过死规矩：门禁试跑零写库（2026-09-14 dry-run 写库害停更一天的
事故原型）。M2 流水线会写 score/score_detail/research/review 产物列并标
reserved——全部挂 `dry_run` 开关：试跑完整执行（含真实 LLM 调用与全文抓取，
验证的就是这条链），唯一区别是产物不落库、状态不迁移。resume 读取不受影响
（只读）。

### D10 验证策略与 repo 模式的代码版本现实

repo 模式下 CronJob 运行时 clone 的是 GitHub main 头，spec 版本锚定的是
调度/环境而非代码 SHA——「push 后、部署前」存在固有窗口。M2 的兜底结构
（流水线失败 → M1 路径 → 老路径）使最坏情况 = 以 M1 行为发帖，故 push 后
立即跑真实 pod 演练（dry-run 全路径 + 网关故障切换）即可把窗口风险压到近零。
不为此引入分支部署流（平台尚无该机制，造假流程是更大风险）。

## 3. 测试计划

- `tests/test_llmpool.py`：concurrency=1 串行有序；=2 并发；退避触发（mock 时钟）
  与通道降级；单任务异常不炸池。
- `tests/test_editorial.py`：评委批解析+标题绑定+单条弃评；研究员 fail-open；
  作者护栏；终审打回一轮流（<0.7→改→复审→照发）；reserved 标记；断点续跑
  （当日产物复用不重烧）；editorial 整体失败 → M1 回退切换。
- 回归：全量测试绿；编辑流水线 llm.chat 全 mock，不烧真 token。

## 4. 验收演练清单（部署后服务器实测）

1. 评分与评审轨迹落库：`SELECT title, score, substr(score_detail,1,50),
   substr(research,1,50), substr(review,1,50) FROM pool_items WHERE digest_date=今天`。
2. <0.7 打回演练：本地单测覆盖 + 生产日志 grep「打回」。
3. 网关故障演练：主通道指错端口跑一班 → 日志见证退避→DeepSeek 切换→成稿。
4. 并发度=1 验证：run_log 时间戳严格串行；改 CONCURRENCY=2 复测并行（换算力开关演练）。
