# 研究解读：人工编辑样稿

三份 JSON 保留 schema 2 的 source_packet / draft，供同一个纯 Markdown 渲染器做离线展示。它们是人工选题和整理的样稿，不是自动选稿、模型质量提升、在线评测、独立复现或终审通过的证据。review_status 均为 not_semantically_reviewed。正文从技术问题开始，编辑流程说明留在本页和 check.json。

## 来源核对记录

2026-10-12（北京时间）通过公开网页阅读工具核对下列官方 HTML 与官方源码文本；没有打开 PDF、第三方转载/广告页，也没有调用模型或现场 API。网页核对不等于完整原始抓取或独立实验。

| 输入 | 已核对的一手资料 | 核对范围 |
| --- | --- | --- |
| lost-in-the-middle.json | [arXiv v3 全文](https://arxiv.org/html/2307.03172v3)、[ACL 发表条目](https://aclanthology.org/2024.tacl-1.9/) | v3 标题、作者和版本日；全文 §2 的问题集/控制设置/表 1，§4 的位置机制对照；ACL 条目的期刊、年份、页码与许可。 |
| bisect-invariant.json | [Python 3.14 bisect 文档](https://docs.python.org/3.14/library/bisect.html)、[CPython v3.14.0 源码](https://raw.githubusercontent.com/python/cpython/v3.14.0/Lib/bisect.py) | bisect_left 分区契约、key 语义、线程警告与 Performance Notes；源码 key=None 分支的 lo/hi 更新及 C 加速导入。页面标注文档版本 3.14.8，源码明确固定 v3.14.0，二者不冒称同一个快照。 |
| sqlite-small-blobs.json | [SQLite 官方 Faster Than The Filesystem](https://sqlite.org/fasterthanfs.html) | §1 的近似性与测试周；§2 的同一批 BLOB、缓存预热、读写路径、WAL/NORMAL 与 checkpoint 条件；§2.3 和 §3 的负载/环境边界。 |

Lost 样稿沿用已批准的中文正文五个语义章节，保持问题集、10/20/30 文档、一份答案文档、0613 模型与两种基线的含义，以及查询前后各放一次对两类任务的不同效果。主来源是 **arXiv v3 预印本**，publication_status=preprint。TACL 2024 是后来发表的版本，在 source_notes 与 ACL 链接中单独说明；不把后来同行评审身份赋予 v3。

bisect 篇是理论/算法解释，不是假装有实测数据的论文解读。文档与参考代码提供契约及算法依据，初始化、保持、终止的中文不变式证明由编辑整理。SQLite 篇是官方历史工程测量的解读；区分开关文件成本的解释与实际比较，保留缓存、事务、同步和持久化条件，未执行基准。每篇包含问题、方法、支持条件、局限与认识；简化例子注明假设或未执行。符号均有定义，不依赖数学插件。

## 证据摘录与时间

- method=manual_evidence_excerpt，blocks.kind=evidence_excerpt。每块带 origin_url、origin_locator、excerpt_mode；内容是最小中文证据转述，不是全文 raw capture。没有整段转载原文或完整源码。
- source_hash_scope=concatenated_editorial_evidence_excerpts：source_hash 绑定 text 中的证据转述，不能当作远端全文哈希。evidence_ids 绑定已有证据/块；代码检查存在性，论据是否足以支持正文仍需人工终审。
- 历史实际捕获时间未记录，三份 fetched_at 均为 null。compiled_at 仅表示本地编辑编制时间；此次网页核对不会回填或冒充历史捕获日期。
- Lost 的 published_at=2023-11-20 是主 URL 对应 arXiv v3 的已知版本日期，不是期刊发表日；另两篇没有确认的发布日期，保持 null。SQLite 的测量周是实验条件，不是网页发布日期。
- 渲染器将带时区的发布时间换算为北京时间日期并标注；纯日期保持原值；缺失、无效和无时区时间显示“原文日期：未确认”。不从抓取/编制时间或评审状态推算日期、版本。

## 许可与归属

[ACL 发表版](https://aclanthology.org/2024.tacl-1.9/)按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 提供，作者七人完整署名，中文表达经过编辑。arXiv 页面自身标的是永久非独占许可；JSON 的 CC BY 说明明确仅指 ACL 发表版，不用于替 arXiv v3 重新授权。这里仅保存有限证据转述与原创中文解释，不镜像全文。

[Python 许可页](https://docs.python.org/3.14/license.html)列出 PSF License Version 2，文档示例另适用 Zero-Clause BSD；保留 Python Software Foundation / CPython contributors 归属，伪代码由编辑整理。[SQLite 版权页](https://sqlite.org/copyright.html)确认代码和文档为公有领域，样稿仍保留官方来源归属。这些许可页也于本次通过公开官方网页核对。

## 本地构建与检查范围

从 tech-digest 目录直接调用独立脚本：

```powershell
python scripts/preview_reader_article.py --input docs/examples/research-reader/lost-in-the-middle.json --output-dir ../_preview/reader-value/lost-in-the-middle
python scripts/preview_reader_article.py --input docs/examples/research-reader/bisect-invariant.json --output-dir ../_preview/reader-value/bisect-invariant
python scripts/preview_reader_article.py --input docs/examples/research-reader/sqlite-small-blobs.json --output-dir ../_preview/reader-value/sqlite-small-blobs
```

每次生成 body.md、preview.html、check.json；预览内嵌邻接 runtime 资源，可离线打开。脚本不导入 main、配置、模型、存储或 publisher。check.valid=true 仅表示结构、Markdown 和证据引用绑定检查通过；validation_scope=structure_and_reference_binding_only、semantic_review_status=not_performed 明示范围，样稿还记录 editorial_sample_only=true、independently_tested=false。没有生成语义终审通过记录。

本次整合预览放在主工作区被忽略的 _preview/reader-value/manual-examples-2026-10-12/。每份保留 920、360、320px 的真实 Chrome 第一屏与完整截图，browser-check.json 记录页面宽度、内部代码宽度和脚本/网络观察。截图只证明本地渲染效果。

原 Task 4 的合成测试与截图留在工作树 scripts/preview_assets/review/，属于开发审查证据。最终整合仅需 preview_assets 的邻接 runtime 文件；review 子目录不是生产依赖。全部样稿待人工语义终审。