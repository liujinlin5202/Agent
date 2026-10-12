# -*- coding: utf-8 -*-
"""digest v4 单测：单篇转载渲染（全文/导读两态）+ 周六星榜专帖渲染。"""
import unittest

from copy import deepcopy
from app import digest

from app.digest import FULL_CAP, _para_cut, render_repost, render_star_post

HN_ITEM = {"type": "news", "title": "Training a 4B model to produce faster query plans",
           "url": "https://example.com/qorl", "source": "hacker-news",
           "author": "rohan", "published_at": None, "fetched_at": "",
           "summary": "", "ai_summary": None, "confidence": None, "trending": None}
ZH_ITEM = {"type": "news", "title": "存款利率下调，市场会有哪些影响？",
           "url": "https://www.zhihu.com/question/111", "source": "zhihu-hot",
           "author": "", "published_at": None, "fetched_at": "",
           "summary": "", "ai_summary": None, "confidence": None,
           "trending": {"rank": 1, "heat": 4283}}

BODY = "第一段全文内容。\n\n第二段全文内容，讲奖励函数如何设计。\n\n第三段讲训练细节。"


def _ctx(mode="full", item=HN_ITEM, lead=BODY, lead_kind=None, author="",
         glossary=None, why="今天值得读这篇：跟数据库课设直接相关。"):
    return {"issue_no": 19,
            "pick": {"item": item, "why": why,
                     "lead": lead, "lead_kind": lead_kind or mode, "author": author,
                     "glossary": glossary or []}}


class TestRenderRepost(unittest.TestCase):
    def test_full_mode_structure(self):
        md = render_repost("2026-09-18", 19, _ctx(author="Rohan Bansal"))
        self.assertIn("转载自", md)
        self.assertIn("Hacker News", md)
        self.assertIn("Rohan Bansal", md)
        self.assertIn("编者按", md)
        self.assertIn("今天值得读这篇", md)
        self.assertIn("第一段全文内容", md)
        self.assertIn('class="td-cta" href="https://example.com/qorl"', md)
        self.assertIn("第 19 期", md)
        self.assertIn("版权归原作者所有", md)
        self.assertNotIn("AI 导读", md)          # 全文模式不打导读标记

    def test_digest_mode_marks_itself(self):
        ctx = _ctx(mode="digest", lead="这是 AI 深度导读的内容。" * 20)
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("AI 导读", md)          # 明示不是全文
        self.assertIn('class="td-cta" href="https://example.com/qorl"', md)

    def test_summary_mode_has_own_marker(self):
        ctx = _ctx(mode="summary", lead="这是原文摘要。")
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("原文摘要", md)
        self.assertNotIn("AI 导读", md)

    def test_zhihu_heat_signal_shown(self):
        ctx = _ctx(mode="digest", item=ZH_ITEM, lead="这是 AI 深度导读的内容。" * 20)
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("知乎热榜", md)
        self.assertIn("第 1 名", md)
        self.assertIn("4283", md)

    def test_long_body_truncated_with_note(self):
        ctx = _ctx(lead="很长的段落。" * 3000)   # 18000 字 > FULL_CAP
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("有删节", md)
        # 裁剪后正文（含署名/链接）不应显著超出预算；style 块是固定装修，不计
        from app.digest import FOLD_STYLE
        self.assertLessEqual(len(md), FULL_CAP + 600 + len(FOLD_STYLE))

    def test_no_star_block_no_quick_list(self):
        """v4 日报就是一篇文章：星榜/速览/多条目结构都不应再出现。"""
        md = render_repost("2026-09-18", 19, _ctx())
        self.assertNotIn("<details", md)
        self.assertNotIn("速览", md)
        self.assertNotIn("星榜", md)

    def test_why_only_no_marker_no_dup(self):
        """只有编者按（AI 没写出导读）：编者按只出现一次，不打 AI 导读标记。"""
        ctx = _ctx(mode="none", lead="")
        md = render_repost("2026-09-18", 19, ctx)
        self.assertEqual(md.count("今天值得读这篇"), 1)
        self.assertNotIn("AI 导读", md)
        self.assertIn('class="td-cta" href="https://example.com/qorl"', md)

    def test_nothing_at_all_honest_line(self):
        """AI 与摘要全缺席：给一句诚实的引导，不空发不装 AI。"""
        ctx = _ctx(mode="none", lead="")
        ctx["pick"]["why"] = ""
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("没能", md)
        self.assertIn('class="td-cta" href="https://example.com/qorl"', md)

    def test_glossary_cards_after_editor_note(self):
        """知识卡片：details 折叠卡，位置在编者按之后。"""
        g = [{"term": "查询计划（query plan）", "expl": "数据库把 SQL 翻译成一步步执行方案的计划。"},
             {"term": "GRPO", "expl": "一种省显存的强化学习算法，适合大模型后训练。"}]
        md = render_repost("2026-09-18", 19, _ctx(glossary=g))
        self.assertIn("<details", md)
        self.assertIn("什么是 查询计划（query plan）", md)
        self.assertIn("省显存的强化学习算法", md)
        self.assertLess(md.index("编者按"), md.index("<details"))

    def test_no_title_heading_reverted(self):
        """v4.3 回退：正文不再放标题行，直接以转载署名开头（页头已有发帖标题）。"""
        md = render_repost("2026-09-18", 19, _ctx())
        self.assertFalse(md.split("\n")[0].startswith("##"))
        self.assertTrue(md.split("\n")[0].startswith("> 转载自"))

    def test_fold_labels_view_more_and_collapse(self):
        """长文折叠：收起态「查看更多」、展开态「收起」，靠 td-fold 作用域样式切换。"""
        body = "\n\n".join(f"第{i}段。" + "内容" * 80 for i in range(40))
        md = render_repost("2026-09-18", 19, _ctx(lead=body))
        self.assertIn('<details class="td-fold">', md)
        self.assertIn("查看更多", md)
        self.assertIn("全文约", md)                 # 收起态提示字数
        self.assertIn("td-less\">收起</span>", md)  # 展开态标签
        self.assertIn(".td-fold[open]", md)        # 作用域样式只出现一次
        self.assertEqual(md.count("<style>"), 1)

    def test_glossary_details_clickable_cursor(self):
        g = [{"term": "GRPO", "expl": "一种省显存的强化学习算法，适合大模型后训练。"}]
        md = render_repost("2026-09-18", 19, _ctx(glossary=g))
        self.assertIn('<details class="td-fold td-gloss"><summary>什么是', md)

    def test_no_glossary_no_cards(self):
        md = render_repost("2026-09-18", 19, _ctx())
        self.assertNotIn("<details", md)

    def test_long_full_collapses(self):
        """全文 >3500 字：前 ~1200 字预览 + details 收起剩余，预览在折叠块之前。"""
        body = "\n\n".join(f"第{i}段。" + "内容" * 80 for i in range(40))
        md = render_repost("2026-09-18", 19, _ctx(lead=body))
        self.assertIn('<details class="td-fold"><summary><span class="td-more">', md)
        self.assertIn("</details>", md)
        self.assertIn("第0段。", md)
        self.assertLess(md.index("第0段。"), md.index("<details"))

    def test_short_full_no_collapse(self):
        md = render_repost("2026-09-18", 19, _ctx())
        self.assertNotIn("<details", md)

    def test_digest_mode_never_collapses(self):
        md = render_repost("2026-09-18", 19, _ctx(mode="digest", lead="导读内容。" * 800))
        self.assertNotIn("<details", md)

    def test_para_cut_never_splits_code_fence(self):
        """截断不许落进代码围栏里：半个 ``` 会把折叠块和后文全吞成代码。"""
        t = "甲" * 400 + "\n\n" + "乙" * 550 + "\n```sql\n" + "SELECT " + "c" * 400 + "\n```"
        cut = _para_cut(t, 1000)
        self.assertEqual(cut.count("```") % 2, 0)   # 围栏必须成对
        self.assertLessEqual(len(cut), 1000)


class TestLayoutV44(unittest.TestCase):
    """v4.4 布局美化：编者按面板 / 名词卡容器 / 折叠胶囊 / 元信息降灰 / 原文按钮。"""

    def test_editor_note_panel_with_bold_label(self):
        md = render_repost("2026-09-18", 19, _ctx())
        self.assertIn('<div class="td-note"><strong>编者按</strong>：今天值得读这篇', md)

    def test_editor_note_ai_bold_converted_to_strong(self):
        """td-note 是裸 HTML 容器，markdown 不再解析——AI 的 **加粗** 要转成 <strong>。"""
        ctx = _ctx(mode="none", lead="", why="关键结论 **快 81%**，值得细读一遍原文内容做验证。")
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("<strong>快 81%</strong>", md)
        self.assertNotIn("**", md)

    def test_editor_note_html_escaped(self):
        ctx = _ctx(mode="none", lead="", why="文中 <tag> 与 & 符号需要转义之后才能放进面板啊。")
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn("&lt;tag&gt;", md)
        self.assertIn("&amp;", md)

    def test_glossary_cards_get_card_container(self):
        g = [{"term": "GRPO", "expl": "一种省显存的强化学习算法，适合大模型后训练。"}]
        md = render_repost("2026-09-18", 19, _ctx(glossary=g))
        self.assertIn('<details class="td-fold td-gloss"><summary>什么是', md)

    def test_style_block_has_layout_rules_single_block(self):
        g = [{"term": "GRPO", "expl": "一种省显存的强化学习算法，适合大模型后训练。"}]
        md = render_repost("2026-09-18", 19, _ctx(glossary=g))
        for rule in (".td-note{", ".td-gloss{", ":not(.td-gloss)", ".td-hint{", ".td-cta{"):
            self.assertIn(rule, md)
        self.assertEqual(md.count("<style>"), 1)

    def test_fold_summary_is_pill_glossary_is_not(self):
        """文章折叠 summary 是胶囊（无原生三角）；名词卡保留原生三角做指向。"""
        body = "\n\n".join(f"第{i}段。" + "内容" * 80 for i in range(40))
        g = [{"term": "GRPO", "expl": "一种省显存的强化学习算法，适合大模型后训练。"}]
        md = render_repost("2026-09-18", 19, _ctx(lead=body, glossary=g))
        self.assertIn("display:inline-block", md)   # 胶囊
        self.assertIn("list-style:none", md)

    def test_meta_lines_use_hint_style(self):
        ctx = _ctx(mode="digest", lead="导读内容。" * 50)
        md = render_repost("2026-09-18", 19, ctx)
        self.assertIn('<div class="td-hint">以上是 AI 导读', md)

    def test_ai_text_emoji_stripped(self):
        """emoji 禁令：AI 产出（编者按/导读/简介）经渲染层清洗，⭐ 豁免。"""
        ctx = _ctx(mode="none", lead="", why="编者按内容 🔥 需要清洗之后才能放进面板啊。")
        md = render_repost("2026-09-18", 19, ctx)
        self.assertNotIn("🔥", md)
        self.assertIn("编者按内容  需要清洗", md)

    def test_read_original_is_chip_link(self):
        md = render_repost("2026-09-18", 19, _ctx())
        self.assertIn('<a class="td-cta" href="https://example.com/qorl">阅读原文</a>', md)
        self.assertNotIn("[阅读原文]", md)


def _trend(i):
    return {"type": "trending", "title": f"user/repo{i}", "url": f"https://github.com/user/repo{i}",
            "source": "github-trending", "author": "user", "published_at": None,
            "fetched_at": "", "summary": f"desc {i}", "ai_summary": None,
            "confidence": None,
            "trending": {"rank": i, "today_stars": 100 * (16 - i), "stars": 999, "language": "Go"}}


class TestRenderStarPost(unittest.TestCase):
    def test_full_board_no_collapse(self):
        md = render_star_post("2026-09-19", [_trend(i) for i in range(1, 16)], {})
        self.assertNotIn("<details", md)          # 专帖全量展开，不再折叠
        self.assertIn("user/repo1", md)
        self.assertIn("user/repo15", md)
        self.assertIn("1. [user/repo1]", md)
        self.assertNotIn("🥇", md)                # 奖牌随 emoji 禁令移除
        self.assertNotIn("🥈", md)
        self.assertIn("2026-09-19", md)
        self.assertIn("每周六", md)

    def test_ai_desc_emoji_stripped_star_kept(self):
        md = render_star_post("2026-09-19", [_trend(1)],
                              {"user/repo1": "纯 Go 写的酷工具 🚀 ⭐"})
        self.assertNotIn("🚀", md)
        self.assertIn("纯 Go 写的酷工具  ⭐", md)   # ⭐ 榜单豁免

    def test_ai_desc_preferred(self):
        md = render_star_post("2026-09-19", [_trend(1)], {"user/repo1": "纯 Go 写的酷工具"})
        self.assertIn("纯 Go 写的酷工具", md)
        self.assertNotIn("desc 1", md)

    def test_fallback_english_desc(self):
        md = render_star_post("2026-09-19", [_trend(1)], {})
        self.assertIn("desc 1", md)

    def test_empty_trending_empty_md(self):
        self.assertEqual(render_star_post("2026-09-19", [], {}), "")

    def test_capped_at_15(self):
        md = render_star_post("2026-09-19", [_trend(i) for i in range(1, 26)], {})
        self.assertIn("user/repo15", md)
        self.assertNotIn("user/repo16", md)


class TestReaderArticle(unittest.TestCase):
    def render(self, packet):
        renderer = getattr(digest, "render_reader_article", None)
        self.assertTrue(callable(renderer), "reader renderer must be implemented")
        return renderer(packet["draft"], packet["source_packet"])

    def test_reader_preserves_semantic_sections_and_provenance(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        md = self.render(packet)
        self.assertTrue(md.startswith("## " + packet["draft"]["title"] + "\n\n"))
        for field in ("opening", "limitation", "takeaway"):
            self.assertIn(packet["draft"][field], md)
        for section in packet["draft"]["sections"]:
            self.assertIn(section["markdown"], md)
        self.assertIn("技术报告", md)
        self.assertIn("前置知识", md)
        self.assertIn("原作者", md)
        self.assertIn("未独立复现", md)
        self.assertIn("AI", md)
        self.assertNotIn("淘汰", md)
        self.assertNotIn("<details", md)
        self.assertNotIn("<style", md)
        self.assertNotIn("预览", md)
        self.assertLess(md.index("### 核心方法"), md.index("### 证据"))
        self.assertLess(md.index(packet["draft"]["limitation"]), md.index("\n\n---\n\n"))

    def test_reader_long_code_and_conclusion_are_never_truncated(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        code = "```python\n" + "values = [1, 2, 3]  # 完整代码\n" * 500 + "```"
        packet["draft"]["sections"][1]["markdown"] += "\n\n" + code
        md = self.render(packet)
        self.assertIn(code, md)
        self.assertIn(packet["draft"]["takeaway"], md)
        self.assertNotIn("删节", md)
        self.assertNotIn("<details", md)

    def test_reader_does_not_duplicate_existing_limits_and_takeaway(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        draft = packet["draft"]
        draft["sections"].extend([
            {"heading": "局限", "markdown": draft["limitation"], "evidence_ids": ["e1"]},
            {"heading": "可带走的认识", "markdown": draft["takeaway"], "evidence_ids": ["e1"]},
        ])
        md = self.render(packet)
        self.assertEqual(md.count(draft["limitation"]), 1)
        self.assertEqual(md.count(draft["takeaway"]), 1)

    def test_reader_theoretical_proof_does_not_invent_measurements(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet("theoretical")
        md = self.render(packet)
        self.assertIn("假设与推导", md)
        self.assertIn("n", md)
        self.assertNotIn("### 实测结果", md)
        self.assertNotIn("### 实验结果", md)
        self.assertIn("未独立复现", md)
        self.assertIn("论证依据来自原文", md)
        self.assertNotIn("推导来自原作者", md)

    def test_reader_rejects_resource_and_missing_test_record(self):
        from tests.test_reader_preview import sample_packet
        for edits in ({"article_type": "resource"}, {"test_status": "independently_tested", "test_record": None}):
            packet = sample_packet()
            packet["draft"].update(edits)
            with self.assertRaises(ValueError):
                self.render(packet)

    def test_reader_converts_html_and_unfolds_essential_reasoning(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        packet["draft"]["sections"][1]["markdown"] = (
            '<details><summary>核心推理</summary><p style="color:red">先比较 '
            '<strong>同一负载</strong>。<a href="https://example.org/article">原文条件</a></p>'
            '<script>alert(1)</script><style>p{display:none}</style></details>\n\n'
            '```python\nif a < b: print("<strong>literal</strong>")\n```'
        )
        md = self.render(packet)
        self.assertIn("核心推理", md)
        self.assertIn("**同一负载**", md)
        self.assertIn("[原文条件](https://example.org/article)", md)
        self.assertIn('if a < b: print("<strong>literal</strong>")', md)
        self.assertNotIn("<details>", md)
        self.assertNotIn("alert(1)", md)
        self.assertNotIn("display:none", md)
        self.assertNotIn('style="', md)

    def test_reader_marks_all_publication_statuses_and_secondary_paper(self):
        from tests.test_reader_preview import sample_packet
        for status, label in (("peer_reviewed", "同行评审"), ("preprint", "预印本"),
                              ("engineering_blog", "工程博客"), ("unknown", "未知")):
            with self.subTest(status=status):
                packet = sample_packet()
                packet["source_packet"]["publication_status"] = status
                packet["source_packet"]["original_paper_url"] = "https://example.org/paper"
                md = self.render(packet)
                self.assertIn(label, md)
                self.assertIn("[原始论文](https://example.org/paper)", md)

    def test_reader_does_not_mutate_packet(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        before = deepcopy(packet)
        self.render(packet)
        self.assertEqual(packet, before)

    def test_reader_source_dates_are_honest_and_aware_dates_use_beijing(self):
        from tests.test_reader_preview import sample_packet
        for value, expected in (
            ("2026-10-11T18:30:00+00:00", "2026-10-12（北京时间）"),
            ("2026-10-12T00:30:00+09:00", "2026-10-11（北京时间）"),
            ("2026-10-11T18:30:00Z", "2026-10-12（北京时间）"),
            ("2024-02-20", "2024-02-20"),
            (None, "未确认"), ("", "未确认"), ("2024-02-30", "未确认"),
            ("2026-10-11T18:30:00", "未确认"), ("not a date", "未确认"),
        ):
            with self.subTest(value=value):
                packet = sample_packet()
                packet["source_packet"]["published_at"] = value
                md = self.render(packet)
                footer = md.split("\n\n---\n\n", 1)[1]
                self.assertIn("原文日期：" + expected, footer)
                self.assertNotIn(packet["source_packet"]["fetched_at"], footer)
                if value == "2024-02-20":
                    self.assertNotIn("北京时间", footer)

    def test_reader_source_title_and_only_captured_version_are_attributed(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        source = packet["source_packet"]
        source.update(title="Captured original title", source_version="arXiv v3")
        before = deepcopy(packet)
        md = self.render(packet)
        self.assertIn("[Captured original title](https://example.org/article)", md)
        self.assertIn("来源版本：arXiv v3", md)
        self.assertIn(source["author"], md)
        self.assertEqual(packet, before)
        for version in (None, "", "  ", {"status": "peer_reviewed"}):
            source["source_version"] = version
            source["title"] = ""
            source["publication_status"] = "peer_reviewed"
            md = self.render(packet)
            self.assertIn("[原文](https://example.org/article)", md)
            self.assertNotIn("来源版本：", md)

    def test_reader_source_metadata_cannot_inject_html_or_markdown(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        source = packet["source_packet"]
        source.update(title='原题 [链接](https://evil.invalid) <script>x</script>',
                      author='作者\n![图](https://evil.invalid/image) **假署名**',
                      source_version='`v3` <img src=x onerror=alert(1)>')
        md = self.render(packet)
        footer = md.split("\n\n---\n\n", 1)[1]
        self.assertIn(r"原题 \[链接\]\(https://evil", footer)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", footer)
        self.assertIn(r"作者 \!\[图\]", footer)
        self.assertIn(r"\*\*假署名\*\*", footer)
        self.assertIn(r"\`v3\`", footer)
        self.assertNotIn("<script>", footer)
        self.assertNotIn("<img", footer)
        self.assertNotIn("\n![", footer)

    def test_reader_source_urls_reject_active_schemes_and_escape_destinations(self):
        from tests.test_reader_preview import sample_packet
        for url in ("javascript:alert(1)", "data:text/html,attack", "//evil.invalid", "https://user:pass@example.org"):
            packet = sample_packet()
            packet["source_packet"]["url"] = url
            packet["draft"]["source_url"] = url
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "invalid_source_url"):
                self.render(packet)
        packet = sample_packet()
        url = "https://example.org/paper_(v3)?q=foo bar"
        packet["source_packet"]["url"] = packet["draft"]["source_url"] = url
        packet["source_packet"]["original_paper_url"] = "javascript:alert(1)"
        md = self.render(packet)
        self.assertIn("[原文](https://example.org/paper_%28v3%29?q=foo%20bar)", md)
        self.assertNotIn("[原始论文]", md)

    def test_reader_requires_concrete_independent_test_record(self):
        from tests.test_reader_preview import sample_packet
        for record in ("亲测通过", {"result": "通过"}, {"environment": "", "command": "", "result": ""}):
            packet = sample_packet()
            packet["draft"].update(test_status="independently_tested", test_record=record)
            with self.assertRaises(ValueError):
                self.render(packet)
        packet = sample_packet()
        packet["draft"].update(test_status="independently_tested", test_record={
            "environment": "离线临时测试；固定输入 A/B/C", "command": "python check_local.py",
            "result": "本地格式测试通过，不是模型性能复现", "limitations": "仅检查离线固定输入",
        })
        md = self.render(packet)
        self.assertIn("独立实测", md)
        self.assertIn("python check_local.py", md)
        self.assertIn("仅检查离线固定输入", md)

    def test_reader_invalid_evidence_and_unclosed_fence_are_rejected(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        packet["draft"]["sections"][1]["evidence_ids"] = ["e-missing"]
        with self.assertRaisesRegex(ValueError, "missing_evidence"):
            self.render(packet)
        packet = sample_packet()
        packet["draft"]["sections"][1]["markdown"] += "\n```python\nprint('broken')"
        with self.assertRaisesRegex(ValueError, "unclosed_code_fence"):
            self.render(packet)

    def test_reader_html_table_and_pre_keep_conditions_and_code(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        packet["draft"]["sections"][1]["markdown"] = (
            '<p>假设 n 为元素数，比较成本恒定。</p>'
            '<table><tr><th>条件</th><th>比较数</th></tr><tr><td>n ≥ 1</td><td>n-1</td></tr></table>'
            '<pre><code>if a &lt; b:\n    value = "&lt;T&gt;"\n</code></pre>'
        )
        md = self.render(packet)
        self.assertIn("假设 n 为元素数", md)
        self.assertIn("| 条件 | 比较数 |", md)
        self.assertIn("| n ≥ 1 | n-1 |", md)
        self.assertIn('if a < b:\n    value = "<T>"', md)
        self.assertNotIn("<table>", md)

    def test_reader_does_not_repeat_semantic_limit_or_takeaway_headings(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        packet["draft"]["sections"].extend([
            {"heading": "研究局限", "markdown": "这组比较也无法解释其他任务中的因果机制。", "evidence_ids": ["e1"]},
            {"heading": "可带走的认识", "markdown": "先检查对照是否只改变一个条件。", "evidence_ids": ["e1"]},
        ])
        md = self.render(packet)
        self.assertEqual(md.count("### 可带走的认识"), 1)
        self.assertNotIn("### 局限\n", md)
        self.assertIn(packet["draft"]["limitation"], md)
        self.assertIn(packet["draft"]["takeaway"], md)

    def test_reader_malformed_source_returns_validation_error(self):
        from tests.test_reader_preview import sample_packet
        packet = sample_packet()
        packet["source_packet"]["blocks"] = None
        try:
            self.render(packet)
        except Exception as exc:
            self.assertIsInstance(exc, ValueError)
            self.assertIn("missing_source_blocks", str(exc))
        else:
            self.fail("malformed source must be rejected")


if __name__ == "__main__":
    unittest.main()
