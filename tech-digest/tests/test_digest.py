# -*- coding: utf-8 -*-
"""digest v4 单测：单篇转载渲染（全文/导读两态）+ 周六星榜专帖渲染。"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
