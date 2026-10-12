# 自动技术帖阅读价值 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有编辑流程上实现以知识增量为中心的论文/原理解读、推广内容排除、统一质量门槛和独立本地预览，并补齐阅读表现与理解反馈。

**Architecture:** 复用评委、研究员、作者、终审和内容池。增加纯函数的稿件结构与校验模块、统一发布判定模块，保留已有提取接口的 text / author 字段。所有日报生成路径先形成稿件包，再审核最终标题与 Markdown；失败产物进入草稿或跳过记录。

**Tech Stack:** Python、现有 HTMLParser 提取代码、SQLite、现有 LLM 调用池、unittest；本地效果使用现有 markdown-it / DOMPurify 预览。

**Spec:** [2026-10-11-reader-value-design.md](../specs/2026-10-11-reader-value-design.md)

状态：2026-10-11 用户确认预览后授权本地实施；2026-10-12 完成本地代码与离线回归，未上线。资源推荐已排除，操作实践优先级已降低。项目 .venv 不存在，离线验证使用已安装 Python 3.14.6，并检查业务代码符合 Python 3.11 语法。

## Global Constraints

- 本轮实现本地业务代码与离线预览；不发帖、不上传、不推送、不部署。此条按用户最新实施授权更新。
- 2026-10-12 后续授权更新：用户要求部署，可提交并推送本次 `tech-digest` 改动，核验秋坞任务使用的新代码；不主动额外发布文章。
- 帖子效果仅通过 Markdown 正文和现有渲染能力实现；不修改集市界面。
- 预览与业务发布隔离；本地预览不刷新登录令牌、不调用发布接口、不改生产数据库和期数。
- 来源、摘编性质、AI 参与和实测状态如实标注；没有实测就不写“亲测”。
- 所有发布候选及旧流程回退产物都必须经过相同的最终质量检查。
- 日期与采样比较统一使用带时区的时间；面向用户展示北京时间。
- 资源推荐、课程推广、工具清单、纯产品宣传和广告软文不进入发帖候选，也不作为缺稿时的兜底。
- 主体内容以技术论文解读、原理分析和有证据的深入工程研究为主；操作实践只保留能解释原理或检验结论的少量内容。

未来本地实现也不包含 push、远程演练或发布操作。旧文档中的自动 push 指示不适用于本次方案。工作区其他 sse_market_assist 改动不属于本计划。

## 共享数据契约

新增 `app/reader_article.py`，使用 dict 保持项目接口风格，入口处校验必需字段，不增加模型框架依赖。

```python
# source_packet
{
    "url": "https://example.org/article",
    "source_hash": "sha256 of captured body",
    "published_at": None,           # unknown remains None
    "fetched_at": "2026-10-11T02:00:00+00:00",
    "method": "site_selector",
    "text": "captured body",
    "author": "",
    "publication_status": "technical_report", # peer_reviewed | preprint | technical_report | engineering_blog | unknown
    "original_paper_url": None,     # when a secondary explanation links to a paper
    "evidence_kind": "empirical",  # empirical | theoretical | engineering_measurement
    "blocks": [
        {"id": "b1", "kind": "paragraph", "text": "captured statement"}
    ],
    "issues": [],                   # extraction issue codes
    "evidence": [
        {"id": "e1", "block_id": "b1", "statement": "supported statement"}
    ],
}

# draft
{
    "schema_version": 2,
    "article_type": "research",    # research | mechanism | experiment; no resource/tutorial category
    "source_url": "https://example.org/article",
    "source_hash": "sha256 of captured body",
    "title": "一个明确的技术问题",
    "learning_question": "读者将理解的技术问题或机制",
    "prerequisites": ["必要前置知识"],
    "takeaway": "读者可以用自己的话解释的原理或取舍",
    "opening": "研究问题、核心发现、关键限制",
    "sections": [
        {"heading": "核心方法为什么有效", "markdown": "方法说明与简化例子", "evidence_ids": ["e1"]}
    ],
    "limitation": "适用限制；来源未报告时明确未报告",
    "evidence_ids": ["e1"],
    "test_status": "not_independently_tested",
    "test_record": None,
    "research_checks": {
        "problem": "研究问题",
        "method": "核心方法或论证",
        "support": "与 evidence_kind 对应的实验证据、推导或工程测量",
        "conditions": "比较条件或理论假设",
        "limits": "结论边界与未证实事项",
    },
}

# review
{
    "status": "pass", # pass | revise | reject | error
    "scores": {"audience": 7, "utility": 7, "credibility": 8, "depth": 6, "novelty": 5},
    "reasons": [],
    "source_url": "https://example.org/article",
    "source_hash": "sha256 of captured body",
    "draft_hash": "sha256 of final title + newline + final markdown",
    "source_metadata_hash": "sha256 of reviewed provenance and structure (crawl time excluded)",
    "rubric_version": "research-v1",
}
```

源块 ID 在同一 source_hash 内稳定；正文哈希使用最终发帖字符串而非草稿字段。证据存在性由代码检查，内容是否支持结论由终审判断。research_checks 必须与原文证据对应，不能用填写泛泛句子绕过验证。utility 表示知识增量；research-v1 不复用 reader-v1 及更旧版本评分。仅有论文摘要、推广文字或抓取残缺时，不能仅靠补齐 JSON 字段使稿件合格。

## Task 1：稿件契约和统一判定

**Files:** Create `app/reader_article.py`, `app/publication_gate.py`, `tests/test_reader_article.py`, `tests/test_publication_gate.py`。

**Interfaces:**

- `validate_draft(draft: dict, source_packet: dict) -> list[str]`：返回结构/来源/证据错误码。
- `draft_hash(title: str, markdown: str) -> str`：SHA-256，UTF-8 编码。
- `weighted_score(scores: dict) -> float`：五维分数为 0—10，缺失或越界抛 ValueError。
- `decide_publication(title: str, markdown: str, draft: dict, source_packet: dict, review: dict | None) -> dict`：返回 `{"status": "ready|skip_quality|generation_error", "reasons": [...]}`。

- [x] 建立纯函数模块和最小有效稿件 fixture。各字段来自上述契约；文档中的 example.org 仅用于离线测试，不作为真实来源。
- [x] 写出以下关键行为用例并先确认失败：

```python
def test_equal_seven_scores_remain_seven(self):
    scores = dict(audience=7, utility=7, credibility=7, depth=7, novelty=7)
    self.assertEqual(weighted_score(scores), 7.0)

def test_final_title_changes_invalidate_review(self):
    self.assertNotEqual(draft_hash("原标题", "正文"), draft_hash("新标题", "正文"))

def test_missing_evidence_is_rejected(self):
    draft = dict(self.valid_draft, evidence_ids=["missing"])
    self.assertIn("missing_evidence", validate_draft(draft, self.source_packet))

def test_resource_type_is_rejected(self):
    draft = dict(self.valid_draft, article_type="resource")
    self.assertIn("excluded_article_type", validate_draft(draft, self.source_packet))

def test_missing_review_cannot_publish(self):
    result = decide_publication(self.title, self.md, self.valid_draft, self.source_packet, None)
    self.assertEqual(result["status"], "generation_error")
```

- [x] 实现权重 `0.30*utility + 0.30*depth + 0.25*credibility + 0.10*audience + 0.05*novelty`；utility 定义为知识增量。缺维度、NaN、无穷值、字符串伪数值和越界数值不得通过。
- [x] 判定实现顺序：确定性校验与类型排除 → 审核状态 → URL/来源/正文哈希/rubric 绑定 → 总分至少 7 且 utility/depth/credibility 分别至少 7、audience 至少 6。审核判为资源推荐或推广稿时，无论分数多高都返回 skip_quality。业务质量不合格与评审错误返回不同状态。
- [x] 分别覆盖只有 abstract、课程目录、纯功能列表的拒稿；企业署名但方法与证据充分的技术报告不能仅按域名被拒；有清楚假设与推导的理论文章不因缺少实测表格被拒。内容分类需要审核材料本身，不能仅信任作者自报的 article_type。
- [x] 检查同稿旧版本审核、标题与正文任一改动、source_hash 变化均使旧审核无效。只在完全绑定时允许复用。
- [x] 在 `tech-digest` 目录运行 `.venv\Scripts\python.exe -m unittest tests.test_reader_article tests.test_publication_gate -v`，不得触发网络和发布。

## Task 2：修复漏抓并保留正文结构

**Files:** Modify `app/extract.py`; Create `tests/fixtures/reader_articles/` 中最小 HTML 样本；Modify `tests/test_extract.py`, `tests/test_mirror.py`。

**Interfaces:** `extract_article(html_text: str) -> dict` 保留 text / author，新增 blocks / issues / method；`fetch_article` 增加 URL、日期和来源哈希等数据，不破坏现有调用方。

- [x] 制作最小 freeCodeCamp 结构 fixture：article 内有广告、`section.post-content` 有实际段落和列表。用自写短文本复现容器结构，不把整篇第三方文章复制进测试库。
- [x] 增加行为用例：提取结果包含正文独有句子、排除广告；原文的列表、标题、代码、公式、图注和实验表格保留；无 h2 的完整短技术论证不会仅因短而判无效。旧 n8n HTML 仅用于提取回归，不能作为合格内容样本。

```python
def test_code_indentation_is_preserved(self):
    result = extract_article('<article><pre><code>if ok:\n    run()\n</code></pre></article>')
    code = next(b for b in result["blocks"] if b["kind"] == "code")
    self.assertIn("\n    run()", code["text"])

def test_login_shell_is_not_substantive(self):
    result = extract_article('<body><form>请登录后阅读</form></body>')
    self.assertFalse(result["text"].strip())
```

- [x] 用当前 HTMLParser 构造正文块；先明确容器边界与 void 元素闭合处理，再做文本清理。正文候选按内容特征选择，不能按最长 article 字符串直接认定正文。
- [x] 使用已验证站点结构和通用结构两类路径；镜像结果补齐相同 metadata，正文能力未知时记录 issues，不伪造已保留代码/表格。
- [x] 将字数过短改为疑点信号；保留语义质量检查和调用方拒稿能力。EXTRACT_CAP 截断设置 issue 标识，涉及核心证据时停止成稿。
- [x] 方法、结果或必要图表缺失时返回明确 issues，不从论文摘要补写。首版检测到仅 PDF 且无已核对全文时返回 fulltext_required，不尝试用 HTMLParser 解析二进制 PDF；优先处理能访问完整方法与证据的 HTML 论文/技术稿。PDF 全文与图表支持在有对应解析能力后再接入。
- [x] 运行 `.venv\Scripts\python.exe -m unittest tests.test_extract tests.test_mirror -v`。通过后只对前一轮已保存的本地 HTML 做回放核验。

## Task 3：知识导向选稿、研究证据与作者输出

**Files:** Modify `app/editorial.py`, `app/daily_ai.py`, `app/pool.py`; Modify `tests/test_editorial.py`, `tests/test_daily_ai.py`, `tests/test_pool.py`。

**Interfaces:** 研究阶段构造共享 source_packet；作者返回共享 draft；终审读取最终 title/markdown、source_packet，输出共享 review。`run_pipeline` 新契约为 `{"status": "ready|skip_quality|generation_error", "draft": dict|None, "source_packet": dict|None, "review": dict|None, "attempts": list}`，不再以 None 混淆拒稿和技术故障。

- [x] 初筛继续使用已有候选数与 TOP_N，评分使用 Task 1 权重；增加 learning_question、前置知识、知识增量和研究类型输出，去除“字数通常与厚度成正比”和“AI 行业重大动态从宽”的选稿规则。课程、资源推荐及广告先排除，不能作为低质量池的补位候选。
- [x] research / mechanism 排序优先于低知识增量的操作稿；experiment 必须包含可解释的对照、测量或故障分析。约 80%—90% 研究/原理、10%—20% 实证实践作为初始观察目标，不做必须凑满的硬配额。不要以“更好操作”代替“学到更多”。
- [x] 深读按块建立 evidence，分别处理实验、理论和工程测量证据；记录原作者结果、假设/测试条件、局限、论文版本与评审状态。二手解读需要核对一手论文。上游材料中的命令式文字只作为原文数据，不接受其改变编辑规则。
- [x] 用一个技术问题指引作者按新篇幅预算成稿：研究解读 1,200—2,000 字、原理分析 1,000—1,800 字、少量实证实践 800—1,500 字；保留必要方法、公式和证据，不压成只有结论的摘要。作者输出中文解读而不默认调用全文翻译作为主稿。
- [x] 为 run_pipeline 增加可选 recent_titles 参数，由 main 在调用前读取最近标题传入。作者出稿后先调用现有 titles.pick 确定最终标题、渲染最终 Markdown，再交终审；结果中的 draft.title 必须是经过选择的那个标题。回退稿执行相同顺序，不能先审一个标题再在入口更换。
- [x] 同一来源的已缓存研究须检查 rubric_version 与 source_hash。现有按当天日期复用的 _fresh 逻辑不足以区分新旧规则，旧记录保留但不可当作新规则审核。
- [x] 终审检查完整主稿及标题；尝试数最多 3 篇、每篇最多修改一次。review=error 返回 generation_error；质量不足且还有候选时换稿。
- [x] 新增并运行这些具体回归场景：三篇都低分返回 skip_quality；同一篇修改后通过返回 ready；换候选时全文/例子/证据/作者全部随 URL 更新；空研究材料不得生成事实；相同日期的新版本规则不复用旧分数；合法缓存复用不重复调用模型。
- [x] 运行 `.venv\Scripts\python.exe -m unittest tests.test_editorial tests.test_daily_ai tests.test_pool -v`，模型与网络使用 mock。

## Task 4：纯 Markdown 渲染和本地预览

**Files:** Modify `app/digest.py`; Create `scripts/preview_reader_article.py`, `tests/test_reader_preview.py`; Modify `tests/test_digest.py`。

**Interfaces:** 新增 `render_reader_article(draft: dict, source_packet: dict) -> str`。新脚本命令为 `python scripts/preview_reader_article.py --input packet.json --output-dir ../_preview/reader-value/<sample>`；packet.json 包含 source_packet / draft，输出 body.md / preview.html / check.json。

- [x] 渲染只读取输入包，按研究问题、已有方法瓶颈、核心方法、证据、局限、可带走的认识、来源顺序生成 Markdown。用户正文没有来源候选淘汰原因、装饰样式和预览按钮。
- [x] `test_status=not_independently_tested` 明示结果来自原作者、未独立复现；只有有具体 test_record 时才允许独立实测措辞。标明预印本/同行评审/技术报告等来源状态。理论文章展示假设与推导，不硬套实测结果章节。资源推荐类型应在渲染前被拒。
- [x] 复用已有离线 preview builder 的渲染配置，迁移为项目脚本时将必要模板放入 `scripts/preview_assets/`，不得依赖仅存在于某个开发者 _preview 里的隐式路径。
- [x] 输入 HTML 只作为内容转换对象，Markdown 由既有 DOMPurify 规则处理；最终正文不输出自定义 style/script。超篇幅产生编辑提示，不按字符截断代码、列表或结论。
- [x] 增加本地预览隔离用例。以子进程运行离线 fixture，阻断 requests 出站并使用独立临时目录；记录运行前后数据库文件哈希、正式输出目录、期数，确认没有改动。
- [x] 检查核心方法和证据完整，以及桌面、360px、320px 下的正文。必要的代码块/公式可以内部横向滚动，整页不应被撑宽；变量定义和图表条件保持可见；关键推理不能全部藏进折叠。数学渲染能力不足时使用有符号解释的可读表示，不修改站点前端。
- [x] 用实验研究解读、理论/算法分析、深入工程研究各一篇做对照样稿，人工审查事实、推理和第一屏；不包含课程/资源推荐正样本。简化例子与真实实验分开标明。
- [x] 运行 `.venv\Scripts\python.exe -m unittest tests.test_digest tests.test_reader_preview -v`。

## Task 5：日报接线与不可绕过的最终检查

**Files:** Modify `main.py`, `app/store.py`; Modify `tests/test_main.py`, `tests/test_publish_state.py`, `tests/test_store.py`。

**Interfaces:** main 消费 Task 3 的 status 结果。日报调用 `_publish_md` 前，使用最终 title/markdown 调用 Task 1 的 decide_publication。现有 star/weekly 发布行为不套用日报专属稿件结构。

- [x] 对旧 M1 / 即时抓取回退的候选也生成新 source_packet / draft；不合格不得用 full / summary / none 模式直接进入日报发布。
- [x] 将 recent_daily_titles 读取移到生成步骤前，把结果传给 run_pipeline；读取已经审核过的 draft.title 后不再另选标题。若后续代码必须改标题或正文，则原 review 失效，重新终审后才可提交最终判定。
- [x] 最终校验置于 save_daily_v2、正式输出快照和消费标记之前；失败只写独立草稿和运行日志。期数仍只按现有正式快照契约分配，质量不通过不调用 save_daily_v2。
- [x] 明确 quality 跳过和 retry 故障状态；跳过不会触发 daily_exists，从而允许后续合法重试。已发布则仍由 daily_posted 幂等阻止重复发布。
- [x] 实施这些必须断言的用例：

```python
# 在 test_main 现有 FakeStore + mock 结构内分别执行：
publish_mock.assert_not_called()
self.assertEqual(store.saved, [])
self.assertEqual(store.posted, {})
# 同时确认 pool.mark_consumed 没有被调用。
```

上述断言分别覆盖：终审低分、终审失败、旧路径生成空摘要、审核后标题变化、审核后正文变化。另测成功发布一次后同日强制重跑仍只调用一次 publish。

- [x] 新本地预览脚本不调用 run_daily。历史 dry_run 的登录校验、cleanup、采样行为不当作离线预览；如后续重构 dry_run，单独列变更，不混入本轮。
- [x] 运行 `.venv\Scripts\python.exe -m unittest tests.test_main tests.test_publish_state tests.test_store -v`。检查未发布稿没有正式日报快照，已发布历史记录不被迁移覆盖。

## Task 6：可比较的表现记录和离线复盘

**Files:** Modify `app/report.py`, `scripts/content_report.py`, `main.py`; Modify `tests/test_report.py`。已有 post_views / run_log 优先复用，首版不新增用户追踪事件或自动反馈权重。

**Interfaces:** report_rows 额外接受可选 `post_meta: dict[int, dict] | None`，每个 post_id 的 metadata 包含 published_at / time_basis / content_version / article_type / learning_question。输出新增 observation_age_hours、sample_basis、content_version；旧调用可不传 metadata。

- [x] 发布日志记录 UTC 时刻和内容版本，平台 created_at 优先；只有本地确认时间则标 time_basis=local_confirmation。来源文章的发布时间不得当作集市帖发布时间。
- [x] 以实际时间差筛选 21—27 小时采样；缺乏时区或可靠发布时间的历史数据标为次日近似。没有窗口内样本则该值缺失，不拿当前浏览替代。
- [x] 浏览、赞和评必须来自同一条快照。保留当前总浏览作为另一个字段，不混算点赞/浏览比。
- [x] 将离线 report 的数据库连接改为 SQLite URI mode=ro，不实例化会执行 schema 迁移和 WAL 设置的 Store；数据库不存在时返回“无本地数据”。sample 模式是另一条显式操作，本计划不运行。
- [x] 新增时区、周末延迟采样、缺失采样、0 浏览、同一天多条记录现有覆盖语义和小样本报告用例。例如周五发布到周一采样约 72 小时，不得归入精确 h24。
- [x] 报告输出篇数、中位数、均值和原始分子分母。未取得曝光/完成事件时字段标不可用，不自动推算 CTR 或读完率。
- [x] 人工理解反馈单独记录：读前认识、读后机制解释、指出的证据与局限、阅读卡点。它不写入浏览统计，不用受访者百分比声称全站学习效果。选题/排版试验的两组都排除课程与推广内容，历史跨定位差异仅作描述。
- [x] 运行 `.venv\Scripts\python.exe -m unittest tests.test_report -v`；完成后按设计文档阶段 A 做本地基线表，缺失数据保持缺失。

## 本地完成后的审阅材料

- 三篇研究型样稿的 Markdown、同渲染器 HTML 预览、来源与证据记录；覆盖实验、理论和深入工程分析。
- 与本计划对应的代码变更清单和上述关键回归结果。
- 正文改动、来源限制、实测状态、质量跳过原因均可追溯。
- 明确列出采样存在但完成率不存在的观测边界。
- 文档更新至 EDITORIAL_PLAN.md 时保留旧策略的历史背景，注明新规则版本与实际启用状态，不提前宣称上线。

后续若实施，可在当前会话按任务顺序完成本地改动，也可在确有可调用子代理时分派边界明确的任务。实际上传、推送、发帖和上线试验不包含在本次授权中。
