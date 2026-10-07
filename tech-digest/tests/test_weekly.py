# -*- coding: utf-8 -*-
"""weekly 五段式单测：跨天聚合 / 星榜两行列表与 AI 简介 / AI 解析绑定 / 价值规则 / 降级渲染。

v4.5（2026-09-21）：裁撤每日收录量/语言分布/爬虫自检三节，对应旧测试一并移除。
"""
import json
import unittest
from datetime import date
from unittest import mock

from app import weekly
from app.weekly import (_agg_news, _parse_deep, _parse_review,
                        _star_desc_prompt_items, _star_section, aggregate,
                        generate_weekly_md, render_weekly, week_monday)


def it(**kw):
    base = {"type": "news", "title": "t", "url": "https://x.com/a", "source": "sspai",
            "author": "a", "published_at": "2026-08-30T10:00:00+08:00",
            "fetched_at": "2026-08-30T09:15:22", "summary": "",
            "ai_summary": None, "confidence": None, "trending": None}
    base.update(kw)
    return base


def day(date_str: str, items: list[dict]) -> dict:
    return {"date": date_str, "source": "mixed", "issue_no": 1, "items": items}


class TestAgg(unittest.TestCase):
    def test_news_url_dedup_across_days(self):
        d = [day("2026-08-31", [it(title="A", url="https://x.com/a")]),
             day("2026-09-01", [it(title="A", url="https://x.com/a", summary="新摘要")])]
        m = _agg_news(d)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["days"], 2)
        self.assertEqual(m[0]["first_date"], "2026-08-31")
        self.assertIn("新摘要", m[0]["all_summaries"])

    def test_news_title_similar_dedup(self):
        d = [day("2026-08-31", [it(title="OpenAI releases GPT-5 Turbo model",
                                   url="https://x.com/1")]),
             day("2026-09-01", [it(title="OpenAI releases GPT-5 Turbo model!",
                                   url="https://x.com/2")])]
        self.assertEqual(len(_agg_news(d)), 1)

    def test_items_type_default_trending_ignored(self):
        """v1 兼容：无 type 的旧条目视为 trending，不进 news 聚合。"""
        d = [day("2026-08-31", [it(type=None, title="shows on trending")])]
        self.assertEqual(_agg_news(d), [])
        self.assertEqual(len(aggregate(d)), 1)

    def test_aggregate_v2_substruct_sorted_by_total(self):
        d = [day("2026-08-31", [it(type="trending", title="repo-b", url="https://g/b",
                                   trending={"rank": 1, "language": "Go", "stars": 100,
                                             "today_stars": 5}),
                                it(type="trending", title="repo-a", url="https://g/a",
                                   trending={"rank": 2, "language": "Rust", "stars": 50,
                                             "today_stars": 9})])]
        agg = aggregate(d)
        self.assertEqual(agg[0]["full_name"], "repo-a")   # total_today 9 > 5
        self.assertEqual(agg[0]["language"], "Rust")


class TestStarSection(unittest.TestCase):
    """⑤ 星榜：两行一条列表（标题链接+简介首行，star 数据次行），拒绝表格。"""

    AGG = [{"full_name": "owner/repo", "url": "https://github.com/owner/repo",
            "language": "Go", "stars": 1200, "days": 5, "total_today": 345,
            "peak_today": 90, "desc": "an english desc"}]

    def test_two_line_entry_title_first(self):
        md = _star_section(self.AGG, {})
        lines = md.splitlines()
        self.assertEqual(lines[0], "## ⭐ 本周 GitHub 星榜（按周增长 Top 10）")
        self.assertEqual(lines[2],
                         "1. **[owner/repo](https://github.com/owner/repo)** — an english desc")
        self.assertEqual(lines[3], "   Go ｜ ⭐ 1,200 · 本周 +345")
        self.assertNotIn("|", md)          # 手机端表格截断 → 不再产出任何 ASCII 表格行

    def test_ai_intro_preferred_over_english(self):
        md = _star_section(self.AGG, {"owner/repo": "把配置文件变成类型安全的校验器"})
        self.assertIn("— 把配置文件变成类型安全的校验器", md)
        self.assertNotIn("an english desc", md)

    def test_no_desc_no_dash(self):
        agg = [dict(self.AGG[0], desc="")]
        lines = _star_section(agg, {}).splitlines()
        self.assertEqual(lines[2], "1. **[owner/repo](https://github.com/owner/repo)**")

    def test_title_brackets_escaped(self):
        agg = [dict(self.AGG[0], full_name="ow/na[me]")]
        self.assertIn("**[ow/na【me】](https://github.com/owner/repo)**",
                      _star_section(agg, {}))

    def test_top10_cap(self):
        agg = [dict(self.AGG[0], full_name=f"o/r{i}", url=f"https://g/{i}")
               for i in range(12)]
        md = _star_section(agg, {})
        self.assertIn("10. **[o/r9]", md)
        self.assertNotIn("11. **", md)

    def test_desc_prompt_items_shape(self):
        """周聚合行 → ai_trending_desc 的 daily 条目形状（title/summary/trending.today_stars）。"""
        items = _star_desc_prompt_items(self.AGG * 3)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "owner/repo")
        self.assertEqual(items[0]["summary"], "an english desc")
        self.assertEqual(items[0]["trending"]["today_stars"], 345)
        self.assertEqual(items[0]["trending"]["language"], "Go")


class TestReviewParse(unittest.TestCase):
    NEWS = [{"item": it(title="华为发布新款折叠屏", url="https://x.com/hw"),
             "days": 3, "first_date": "2026-08-31", "all_summaries": ["x"]},
            {"item": it(title="RISC-V 官方支持", url="https://x.com/riscv"),
             "days": 2, "first_date": "2026-09-01", "all_summaries": ["y"]},
            {"item": it(title="SQLite 文档数据库", url="https://x.com/sq"),
             "days": 1, "first_date": "2026-09-02", "all_summaries": ["z"]},
            {"item": it(title="CPython 提速", url="https://x.com/cp"),
             "days": 1, "first_date": "2026-09-02", "all_summaries": ["w"]},
            {"item": it(title="EVE Online 迁移", url="https://x.com/eve"),
             "days": 1, "first_date": "2026-09-03", "all_summaries": ["v"]}]

    def _payload(self, **kw):
        d = {"review": [{"category": "前端", "title": t, "text": "事情进展如何如何。"}
                        for t in ("华为发布新款折叠屏", "RISC-V 官方支持", "SQLite 文档数据库",
                                  "CPython 提速", "EVE Online 迁移")],
             "picks": [{"title": "华为发布新款折叠屏", "reason": "国产芯进展"},
                       {"title": "CPython 提速", "reason": "性能跃升"},
                       {"title": "不存在条目", "reason": "应为非法"}],
             "outlook": ["下季度值得关注", "预计 X 会发布"]}
        d.update(kw)
        return d

    def _run(self, **kw):
        return _parse_review(json.dumps(self._payload(**kw), ensure_ascii=False),
                             self.NEWS, deep_titles=[])

    def test_bind_and_fill_date_ref(self):
        r = self._run()
        self.assertEqual(len(r["review"]), 5)
        first = r["review"][0]
        self.assertEqual(first["title"], "华为发布新款折叠屏")
        self.assertIn("（见 8/31 日报）", first["text"])     # 程序补齐日期引用
        self.assertEqual(len(r["picks"]), 2)                # 不存在条目被剔除
        self.assertEqual(r["outlook"][0], "预计 下季度值得关注")

    def test_picks_exclude_deep_titles(self):
        _r = _parse_review(json.dumps(self._payload(), ensure_ascii=False), self.NEWS,
                           deep_titles=["华为发布新款折叠屏"])
        self.assertEqual([p["title"] for p in _r["picks"]], ["CPython 提速"])

    def test_fence_and_prose_wrapped(self):
        payload = json.dumps(self._payload(), ensure_ascii=False)
        for wrapped in ("```json\n" + payload + "\n```", "好的：" + payload):
            self.assertIsNotNone(_parse_review(wrapped, self.NEWS, []))

    def test_insufficient_rejected(self):
        self.assertIsNone(_parse_review(json.dumps(
            self._payload(review=self._payload()["review"][:2]), ensure_ascii=False),
            self.NEWS, []))
        self.assertIsNone(_parse_review("抱歉，没法写。", self.NEWS, []))
        self.assertIsNone(_parse_review(None, self.NEWS, []))
        self.assertIsNone(_parse_review("[]", self.NEWS, []))

    def test_unknown_titles_dropped_enough(self):
        """全部标题写错 → review 不足 5 条 → 整次降级（宁缺毋滥）。"""
        kw = {"review": [{"category": "前端", "title": "编的标题", "text": "x。"} for _ in range(5)]}
        self.assertIsNone(_parse_review(json.dumps(self._payload(**kw), ensure_ascii=False),
                                        self.NEWS, []))


class TestDeepParse(unittest.TestCase):
    def test_too_short_rejected(self):
        self.assertIsNone(_parse_deep("### 《短篇》\n只有一句话。"))
        self.assertIsNone(_parse_deep(None))
        self.assertIsNone(_parse_deep(""))

    def test_long_accepted_and_fence_stripped(self):
        body = "背景与影响推演。" * 60
        r = _parse_deep("```markdown\n### 《长篇》\n" + body + "\n```")
        self.assertIsNotNone(r)
        self.assertTrue(r.startswith("### 《长篇》"))


class TestPromptValueCriteria(unittest.TestCase):
    """v4.5 信息价值判断：回顾选条与深度择优的 prompt 硬性规则。"""

    def test_review_prompt_value_rules(self):
        p = weekly._review_prompt([], [], "2026-W38", [])
        self.assertIn("信息价值优先", p)
        self.assertIn("营销稿", p)
        self.assertIn("为什么值得关注", p)
        self.assertIn("非技术", p)        # 2026-10-07 技术话题闸（同日日报标准）

    def test_deep_prompt_value_pick_and_cands(self):
        cands = [{"item": it(title=f"候选条目{i}", url=f"https://x.com/{i}"),
                  "days": 1, "first_date": "2026-09-01", "all_summaries": ["s"]}
                 for i in range(weekly.DEEP_CANDS)]
        p = weekly._deep_prompt(cands, "2026-W38")
        self.assertIn("信息价值判断", p)
        self.assertIn("宁缺毋滥", p)
        self.assertIn("非技术", p)        # 2026-10-07 技术话题闸
        for i in range(weekly.DEEP_CANDS):       # 8 条候选全部给出，AI 自己择优
            self.assertIn(f"候选条目{i}", p)


class TestRenderAndGenerate(unittest.TestCase):
    DAYS = [day("2026-08-31", [it(title="第一件", url="https://x.com/1"),
                               it(title="repo", type="trending", url="https://g/r",
                                  trending={"rank": 1, "language": "Go", "stars": 5,
                                            "today_stars": 5})])]
    FLAGS = {"review": False, "deep": False, "intro": False}

    def test_render_fallback_no_ai_all_sections(self):
        md = render_weekly(self.DAYS, "2026-W36", None, None, dict(self.FLAGS), {})
        for h in ("## 本周回顾", "## 深度长文", "## 精选回看",
                  "## 下周前瞻", "## ⭐ 本周 GitHub 星榜",
                  "# 前沿技术周报 · 2026-W36"):
            self.assertIn(h, md)
        for gone in ("每日收录量", "语言分布", "爬虫自检", "链接抽查", "趋势分析"):
            self.assertNotIn(gone, md)
        self.assertNotIn("🤖", md)             # 2026-09-21 emoji 禁令（⭐ 除外）
        self.assertIn("AI×0/3", md)
        self.assertIn("AI 未参与", md)

    def test_star_section_last_and_mobile_format(self):
        md = render_weekly(self.DAYS, "2026-W36", None, None, dict(self.FLAGS), {})
        # star 段排最后（用户决策：star 可以排后面）
        self.assertGreater(md.index("下周前瞻"), md.index("深度长文"))
        self.assertGreater(md.index("本周 GitHub 星榜"), md.index("下周前瞻"))
        # 两行条目：标题链接首行，star 数据次行缩进
        self.assertIn("1. **[repo](https://g/r)**", md)
        self.assertIn("   Go ｜ ⭐ 5 · 本周 +5", md)
        # 页脚 --- 前必须有空行：否则 star 次行被解析成 setext H2
        self.assertIn("本周 +5\n\n---", md)

    def test_render_with_parsed(self):
        parsed = {"review": [{"category": "后端", "title": "第一件", "url": "https://x.com/1",
                              "text": "进展良好（见 8/31 日报）", "date": "8/31"}],
                  "picks": [{"title": "第一件", "reason": "值得", "url": "https://x.com/1",
                             "date": "8/31"}],
                  "outlook": ["预计 X"]}
        md = render_weekly(self.DAYS, "2026-W36", parsed, "### 《深》\n" + "文" * 400,
                           {"review": True, "deep": True, "intro": False}, {})
        self.assertIn("### 后端", md)
        self.assertIn("**[第一件](https://x.com/1)**", md)
        self.assertIn("## 深度长文", md)
        self.assertIn("AI×2/3", md)

    def test_ai_text_emoji_stripped_star_kept(self):
        """AI 产出带 emoji → 渲染层清洗；⭐（GitHub 榜单豁免）保留。"""
        parsed = {"review": [{"category": "AI 工具", "title": "第一件", "url": "https://x.com/1",
                              "text": "🔥 重磅发布（见 8/31 日报）", "date": "8/31"}],
                  "picks": [{"title": "第一件", "reason": "值得 🚀", "url": "https://x.com/1",
                             "date": "8/31"}],
                  "outlook": ["预计 X"]}
        md = render_weekly(self.DAYS, "2026-W36", parsed, "### 《深》\n" + "文" * 400,
                           {"review": True, "deep": True, "intro": False},
                           {"repo": "自动整理 🤖 工具 ⭐"})
        for gone in ("🔥", "🚀", "🤖"):
            self.assertNotIn(gone, md)
        self.assertIn("自动整理  工具 ⭐", md)

    def test_generate_no_key_pure_data(self):
        # 两通道 key 都清空：服务器 .env 常驻真实 key，只清主通道会漏走备用通道
        with mock.patch.object(weekly.settings, "sse_market_api_key", ""), \
             mock.patch.object(weekly.settings, "deepseek_api_key", ""):
            md, used, detail = generate_weekly_md(self.DAYS, "2026-W36")
        self.assertFalse(used)
        self.assertEqual(detail, {"review": False, "deep": False, "intro": False})
        self.assertIn("本周 GitHub 星榜", md)
        self.assertIn("AI 未参与", md)
        self.assertNotIn("链接抽查", md)

    def test_week_helpers(self):
        self.assertEqual(weekly.iso_week_label(date(2026, 8, 31)), "2026-W36")
        self.assertEqual(week_monday(date(2026, 9, 6)), date(2026, 8, 31))


if __name__ == "__main__":
    unittest.main()
