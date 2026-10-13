# tech-digest code cleanup implementation plan

> **For agentic workers:** Use superpowers:executing-plans. Track tasks below in order.

**Goal:** Remove retired daily orchestration and duplicate research quality policy without changing live publication behavior.

**Architecture:** The live daily entry remains research-v1. Candidate and final-review scoring share one pure policy function in publication_gate. Standalone legacy render/translation tools remain supported where tracked consumers exist.

**Tech Stack:** Python >=3.11, unittest, stdlib plus existing requests/beautifulsoup4 and optional pymysql.

**Spec:** User requested inspection and optimization of redundant/unused project code; research-v1 contract in docs/READER_VALUE.md.

## Global constraints

- Scope tech-digest only; preserve other dirty workspace files and newer unrelated commits.
- Keep types, score weights, threshold 7, dimension floors and same-day duplicate protection.
- Tests use temporary state and blocked external network; do not read .env or production state.
- Local cleanup does not require business runs or publishing.

## Task 1: Shared quality policy

- [x] Add table-driven boundary tests for `assess_quality(scores) -> (score, eligible)`; invalid scores raise ValueError. Expected literal cases: all7 pass, depth6 fail even with remaining10, audience5 fail, novelty0 with other8 passes, Boolean/NaN rejected.
- [x] Run tests and observe missing helper failure.
- [x] Move the existing weighted/minimum checks into the helper; route editorial qualification and final publication gate through it without weakening review/source checks.
- [x] Run affected gate/editorial/main integration tests.

## Task 2: Retired daily orchestration

- [x] Verify tracked callers for `_gate_groups`, `_try_fetch_art`, `_prefetch_gate`, `_fallback_title`, `_pick_post_title`, `_has_ai`, `_pick_article`, `_fetch_body`.
- [x] Remove the seven retired helpers and their unused imports/constants from main.py; retain `_has_ai` because run_star still uses it. Remove obsolete `_sim_forced.py`, `_sim_pool.py`, `_sim_pick_check.py` (explicitly documented one-shot scripts), replace usage guidance with supported offline reader preview.
- [x] Remove tests exercising retired private helpers. Retain active daily, candidate order, source extraction, policy, posting, snapshot, and star/weekly tests. Update boundary mocks to guard live external legacy functions without requiring retired symbols.
- [x] Run all relevant integration tests, then full suite once.

## Task 3: Review and integration

- [x] Clean confirmed unused imports from production modules; no broad formatting change.
- [x] Review remaining tracked consumers, Python3.11 grammar, lint, diff and test results.
- [x] Independent code review; repair material findings and rerun affected checks.
- [x] Integrate scoped files into primary workspace, preserving unrelated file hashes. Record removed lines, retained behavior and verification limits.

## Verification and compatibility notes

- Baseline: 816 tests. A local proxy test initially received an empty HTTP 502 because Windows system proxy fallback remains active even after environment clearing. Isolated runner sets NO_PROXY=*; the original test passes without a production change.
- Shared policy: new boundary tests first failed because the helper was absent, then affected gate/editorial/main tests passed.
- Final worktree suite: 793 tests, 0 failures/errors, 1 Windows-only chmod skip. The count is 816 minus 25 tests for removed private helpers plus 2 shared-policy tests. Existing source extraction, translation, title rules, reader rendering, publication/hash binding, pool, metrics, star and weekly regressions remain.
- Ruff checks F on all of main.py, app/ and scripts/ pass. All changed Python source parses under Python3.11 grammar; this cleanup has not been executed in a new Linux runtime.
- Standalone legacy renderer/translation utilities remain where consumers or compatibility tests exist. They are not accessible through the live daily fallback.
- The retired _sim scripts explicitly said to delete after use and modelled the former repost workflow. Supported offline replacement: python scripts/preview_reader_article.py --input docs/examples/research-reader/lost-in-the-middle.json --output-dir ../_preview/reader-value/lost-in-the-middle . This previews a local research packet without login, model calls or state reads.

## 清理结果

- 删除主入口中7个已退休私有函数，以及旧预抓取闸门常量。星榜仍使用的 `_has_ai` 保留。
- 删除零引用 `_gated_clause` 和 `digest.fallback_title`，移除10个原有未使用导入；随旧函数退役，额外清除不再需要的 extract/titles/headline_of 导入。
- 退役3个标注“跑完删除”的旧模拟脚本，以及依赖早已删除 API 的 `scripts/diag_ai_raw.py`。原始模型连通诊断仍可使用 `scripts/diag_llm.py`；研究正文预览使用 `scripts/preview_reader_article.py`。
- 初筛和终审共同使用 `assess_quality`，避免各自修改阈值后不一致。加权分只计算一次，返回分数和是否合格。
- 复用标题中文词组与正文空白规范化结果，减少同次处理中重复计算。
- 旧转载渲染、翻译、标题及星榜/周报公共能力仍有兼容调用/测试，因此没有按“日报不用”一律删除。
- 当前基线包含此前 e62bb78/d6bf31c 的审稿轨迹、持久草稿、重试隔离和 TCP 统计修复；本次清理没有覆盖这些更新。

## 最终核验

- 独立代码评审无 P1/P2 阻塞项。161051组整数评分、16807组小数边界评分及50组非法输入的新旧策略结果一致；92组完整发布决策一致。
- 3份文档样稿及6组夹具变体的正文渲染字节完全一致。
- 主工作区整合后再次通过793项测试（0失败/错误，1项Windows权限跳过），全部业务/脚本 Ruff F 检查通过。
- 主工作区无关文件哈希保持不变；本次改动留在本地，未推送和部署。
