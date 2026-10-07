# -*- coding: utf-8 -*-
"""daily_ai v4 单测：挑 1 篇爆文（标题逐字绑定 + 编者按/导读校验）+ 全文翻译 + 星榜简介。"""
import unittest
from unittest import mock

from app import daily_ai

HN_ITEM = {"type": "news", "title": "Training a 4B model to produce faster query plans",
           "url": "https://example.com/qorl", "source": "hacker-news",
           "author": "rohan", "published_at": None, "fetched_at": "",
           "summary": "A reinforcement learning policy update for query planning.",
           "ai_summary": None, "confidence": None, "trending": None}
ZH_ITEM = {"type": "news", "title": "存款利率下调，市场会有哪些影响？",
           "url": "https://www.zhihu.com/question/111", "source": "zhihu-hot",
           "author": "", "published_at": None, "fetched_at": "",
           "summary": "", "ai_summary": None, "confidence": None,
           "trending": {"rank": 1, "heat": 4283}}
CANDS = [HN_ITEM, ZH_ITEM]

GOOD_JSON = """{
  "title": "Training a 4B model to produce faster query plans",
  "why": "这篇文章讲用强化学习训练 4B 小模型重写 SQL 查询，查询计划比 Postgres 快 81%。做数据库课程设计、准备后端实习的同学值得全文精读，选课项目选题也能直接参考。",
  "digest": "文章分三部分：第一部分讲奖励函数如何设计，把查询执行时间直接作为 reward 信号；第二部分讲 4B 模型在 8 张消费级显卡上的训练细节；第三部分给出与 Postgres 优化器的对比基准，快 81%。对学生来说，这是把课堂里的查询优化理论变成可动手项目的完整范例。",
  "post_titles": ["4B 模型干翻 Postgres 查询优化器", "RL 重写 SQL，快 81%", "数据库课设的新选题"]
}"""


def _ai(content: str | None):
    return mock.patch.object(daily_ai.llm, "chat", return_value=content)


class TestAiPick(unittest.TestCase):
    def test_pick_exact_title_binding(self):
        with _ai(GOOD_JSON):
            r = daily_ai.ai_pick(CANDS, "2026-09-18")
        self.assertIsNotNone(r)
        self.assertEqual(r["idx"], 1)
        self.assertIn("强化学习", r["why"])
        self.assertIn("奖励函数", r["digest"])
        self.assertEqual(len(r["post_titles"]), 3)

    def test_pick_wrong_title_dropped(self):
        bad = GOOD_JSON.replace(
            "Training a 4B model to produce faster query plans", "不存在的标题")
        with _ai(bad):
            self.assertIsNone(daily_ai.ai_pick(CANDS, "2026-09-18"))

    def test_reject_reason_logged_for_diagnosis(self):
        """护栏拒掉输出时必须留痕（凌晨自动发帖排障全靠这行日志）。"""
        bad = GOOD_JSON.replace(
            "Training a 4B model to produce faster query plans", "不存在的标题")
        with _ai(bad), self.assertLogs("tech-digest.daily_ai", level="WARNING") as cm:
            self.assertIsNone(daily_ai.ai_pick(CANDS, "2026-09-18"))
        self.assertTrue(any("标题绑定" in m for m in cm.output))

    def test_reject_reason_why_too_short_logged(self):
        bad = GOOD_JSON.replace(
            "这篇文章讲用强化学习训练 4B 小模型重写 SQL 查询，查询计划比 Postgres 快 81%。"
            "做数据库课程设计、准备后端实习的同学值得全文精读，选课项目选题也能直接参考。",
            "太短了")
        with _ai(bad), self.assertLogs("tech-digest.daily_ai", level="WARNING") as cm:
            self.assertIsNone(daily_ai.ai_pick(CANDS, "2026-09-18"))
        self.assertTrue(any("编者按" in m for m in cm.output))

    def test_pick_why_off_topic_dropped(self):
        # 中文候选（知乎）+ 编者按讲的是别的事（GPU），零词元重叠 → 整体丢弃
        zh_pick = GOOD_JSON.replace(
            "Training a 4B model to produce faster query plans",
            "存款利率下调，市场会有哪些影响？",
        ).replace(
            "这篇文章讲用强化学习训练 4B 小模型重写 SQL 查询，查询计划比 Postgres 快 81%。"
            "做数据库课程设计、准备后端实习的同学值得全文精读，选课项目选题也能直接参考。",
            "英伟达 GPU 架构又进化了，做深度学习的同学该关注显卡市场行情变化。")
        with _ai(zh_pick):
            self.assertIsNone(daily_ai.ai_pick(CANDS, "2026-09-18"))

    def test_pick_english_candidate_skips_lexical_check(self):
        # 纯英文候选 + 纯中文编者按：词元上结构性不可能重叠，靠标题绑定守门，不应误杀
        with _ai(GOOD_JSON):
            r = daily_ai.ai_pick(CANDS, "2026-09-18")
        self.assertEqual(r["idx"], 1)

    def test_pick_wrapped_title_unwrapped(self):
        with _ai(GOOD_JSON.replace(
                '"title": "Training a 4B model to produce faster query plans"',
                '"title": "《Training a 4B model to produce faster query plans》"')):
            r = daily_ai.ai_pick(CANDS, "2026-09-18")
        self.assertEqual(r["idx"], 1)

    def test_pick_llm_failure_none(self):
        with _ai(None):
            self.assertIsNone(daily_ai.ai_pick(CANDS, "2026-09-18"))
        with _ai("不是 JSON"):
            self.assertIsNone(daily_ai.ai_pick(CANDS, "2026-09-18"))

    def test_pick_empty_candidates_none(self):
        self.assertIsNone(daily_ai.ai_pick([], "2026-09-18"))

    def test_pick_no_ai_call_without_candidates_signal(self):
        # 知乎条目（无 summary）也能被选中：编者按只要跟标题有重叠即可
        zh_pick = GOOD_JSON.replace(
            "Training a 4B model to produce faster query plans",
            "存款利率下调，市场会有哪些影响？",
        ).replace(
            "这篇文章讲用强化学习训练 4B 小模型重写 SQL 查询，查询计划比 Postgres 快 81%。"
            "做数据库课程设计、准备后端实习的同学值得全文精读，选课项目选题也能直接参考。",
            "存款利率下调是今天知乎热榜第一名。市场会有哪些影响，值得每位关注理财与宏观形势"
            "的同学认真了解一遍。")
        with _ai(zh_pick):
            r = daily_ai.ai_pick(CANDS, "2026-09-18")
        self.assertEqual(r["idx"], 2)

    def test_prompt_contains_signals(self):
        """热度信号（HN points / 知乎排名）必须进 prompt——时效×影响是挑选依据。"""
        prompt = daily_ai._build_pick_prompt(CANDS)
        self.assertIn("faster query plans", prompt)
        self.assertIn("存款利率下调", prompt)
        self.assertIn("4283", prompt)   # 知乎热度
        self.assertIn("知乎热榜", prompt)

    def test_prompt_prefers_big_names_with_freshness(self):
        """知名大厂/人物关联优先（读者更容易接受），但时效是硬前提——过气旧闻不要。"""
        prompt = daily_ai._build_pick_prompt(CANDS)
        self.assertIn("知名大厂", prompt)
        self.assertIn("知名人物", prompt)
        self.assertIn("过气", prompt)   # 同一条里必须带时效前提，防大厂旧文被翻出来


class TestTranslate(unittest.TestCase):
    def test_translate_returns_text(self):
        with _ai("强化学习可以教会小模型重写 SQL。") as m:
            r = daily_ai.ai_translate("RL can teach small models to rewrite SQL.")
        self.assertEqual(r, "强化学习可以教会小模型重写 SQL。")
        self.assertIn("RL can teach", m.call_args[0][0])

    def test_translate_prompt_asks_key_sentence_bold(self):
        """翻译提示词要求对关键句加粗（v4.3 用户要求）。"""
        with mock.patch.object(daily_ai.llm, "chat", return_value="x") as m:
            daily_ai.ai_translate("RL can teach small models to rewrite SQL.")
        sys_prompt = m.call_args.kwargs.get("system") or ""
        self.assertIn("加粗", sys_prompt)
        self.assertIn("译", sys_prompt)

    def test_translate_prompt_preserves_code_blocks(self):
        """翻译必须保留代码块：v4.3 重译把原文 9 个 SQL 代码块全丢了。"""
        with mock.patch.object(daily_ai.llm, "chat", return_value="x") as m:
            daily_ai.ai_translate("SELECT 1;")
        sys_prompt = m.call_args.kwargs.get("system") or ""
        self.assertIn("代码块", sys_prompt)
        self.assertIn("原样保留", sys_prompt)

    def test_translate_prompt_discards_page_noise(self):
        """提取层漏网的评论区/资料卡噪声（Joined/Location/Copy link），翻译层兜底丢弃。"""
        with mock.patch.object(daily_ai.llm, "chat", return_value="x") as m:
            daily_ai.ai_translate("RL can teach small models to rewrite SQL.")
        sys_prompt = m.call_args.kwargs.get("system") or ""
        self.assertIn("评论区", sys_prompt)
        self.assertIn("丢弃", sys_prompt)

    def test_translate_long_input_capped(self):
        from app.daily_ai import TRANSLATE_SRC_CAP
        with _ai("译") as m:
            daily_ai.ai_translate("x" * (TRANSLATE_SRC_CAP + 5000))
        self.assertLessEqual(len(m.call_args[0][0]), TRANSLATE_SRC_CAP + 500)

    def test_translate_failure_none(self):
        with _ai(None):
            self.assertIsNone(daily_ai.ai_translate("hello"))


class TestAiTrendingDesc(unittest.TestCase):
    TREND = [{"type": "trending", "title": "user/cool-repo", "url": "https://github.com/user/cool-repo",
              "source": "github-trending", "author": "user", "published_at": None,
              "fetched_at": "", "summary": "A cool tool", "ai_summary": None,
              "confidence": None, "trending": {"rank": 1, "today_stars": 652, "stars": 1234, "language": "Go"}}]

    def test_desc_bound_by_repo_name(self):
        with _ai('{"trending_desc": {"user/cool-repo": "纯 Go 实现的酷工具"}}'):
            d = daily_ai.ai_trending_desc(self.TREND)
        self.assertEqual(d.get("user/cool-repo"), "纯 Go 实现的酷工具")

    def test_desc_invalid_key_or_no_chinese_dropped(self):
        with _ai('{"trending_desc": {"wrong/repo": "中文", "user/cool-repo": "no chinese"}}'):
            d = daily_ai.ai_trending_desc(self.TREND)
        self.assertEqual(d, {})

    def test_failure_empty(self):
        with _ai(None):
            self.assertEqual(daily_ai.ai_trending_desc(self.TREND), {})


class TestGlossary(unittest.TestCase):
    """知识卡片（v4.1）：≤3 个术语卡，解释必须含中文；任何失败 → []。"""

    GOOD = '{"glossary": [{"term": "GRPO", "expl": "一种省显存的强化学习算法，适合大模型后训练。"}, {"term": "查询计划（query plan）", "expl": "数据库把 SQL 翻译成一步步执行方案的计划。"}, {"term": "", "expl": "空术语应当被丢弃这里凑字数内容足够长"}, {"term": "好术语", "expl": "no cjk here at all"}, {"term": "查询计划", "expl": "太短"}]}'

    def test_sanitize_filters(self):
        out = daily_ai.sanitize_glossary(self.GOOD)
        terms = [g["term"] for g in out]
        self.assertEqual(terms, ["GRPO", "查询计划（query plan）"])

    def test_sanitize_caps_3(self):
        many = '{"glossary": [' + ",".join(
            f'{{"term": "术语{i}", "expl": "解释内容足够长可以过审{i}xxx"}}' for i in range(6)) + "]}"
        self.assertEqual(len(daily_ai.sanitize_glossary(many)), 3)

    def test_sanitize_long_expl_clipped(self):
        raw = '{"glossary": [{"term": "术语", "expl": "%s"}]}' % ("长" * 300)
        out = daily_ai.sanitize_glossary(raw)
        self.assertEqual(len(out[0]["expl"]), 120)

    def test_sanitize_garbage_empty(self):
        self.assertEqual(daily_ai.sanitize_glossary("not json"), [])
        self.assertEqual(daily_ai.sanitize_glossary('{"glossary": "x"}'), [])
        self.assertEqual(daily_ai.sanitize_glossary(None), [])

    def test_ai_glossary_success(self):
        with _ai(self.GOOD):
            out = daily_ai.ai_glossary("Training a 4B model", "query plans " * 50)
        self.assertEqual([g["term"] for g in out], ["GRPO", "查询计划（query plan）"])

    def test_ai_glossary_llm_failure_returns_empty(self):
        with mock.patch.object(daily_ai.llm, "chat", side_effect=RuntimeError("boom")):
            self.assertEqual(daily_ai.ai_glossary("t", "c"), [])

    def test_ai_glossary_needs_title_and_context(self):
        self.assertEqual(daily_ai.ai_glossary("", "context"), [])
        self.assertEqual(daily_ai.ai_glossary("title", ""), [])


class TestPickPromptPreference(unittest.TestCase):
    """人工倾向软注入（2026-09-22 spec）：加分项非硬条件，默认模式 prompt 逐字不变。"""

    def test_prefer_injects_soft_clause(self):
        from app.preference import Preference
        pref = Preference(mode="prefer", text="jev 相关内容优先")
        prompt = daily_ai._build_pick_prompt(CANDS, pref)
        self.assertIn("编辑当前倾向：jev 相关内容优先", prompt)
        self.assertIn("加分项，不是硬性条件", prompt)
        self.assertIn("必须放弃倾向", prompt)
        self.assertIn("宁可发非倾向的高价值文章", prompt)
        # 软规则在 6 条标准之后、写作规则之前
        self.assertGreater(prompt.index("深度长文 > 短讯"), -1)
        self.assertLess(prompt.index("编辑当前倾向"), prompt.index("写作规则"))

    def test_default_mode_prompt_unchanged(self):
        from app.preference import Preference
        baseline = daily_ai._build_pick_prompt(CANDS)
        self.assertEqual(daily_ai._build_pick_prompt(CANDS, None), baseline)
        self.assertEqual(daily_ai._build_pick_prompt(CANDS, Preference()), baseline)
        self.assertEqual(
            daily_ai._build_pick_prompt(CANDS, Preference(mode="prefer", text="")),
            baseline)

    def test_ai_pick_passes_preference_into_prompt(self):
        from app.preference import Preference
        captured = {}
        real_build = daily_ai._build_pick_prompt

        def spy(candidates, preference=None):
            captured["preference"] = preference
            return real_build(candidates, preference)

        pref = Preference(mode="prefer", text="jev 相关内容优先")
        with mock.patch.object(daily_ai, "_build_pick_prompt", spy), _ai(GOOD_JSON):
            daily_ai.ai_pick(CANDS, "2026-09-22", pref)
        self.assertIs(captured["preference"], pref)


class TestPickPromptGating(unittest.TestCase):
    """选文标准 v3（2026-10-02，用户反馈「这两天选文信息量低」）：
    信息价值一票否决 + 预取闸门的正文预览进 prompt。"""

    def test_info_value_rules_in_prompt(self):
        prompt = daily_ai._build_pick_prompt(CANDS)
        for kw in ("信息价值", "通稿", "营销软文", "小版本更新", "观点评论",
                   "知名大厂", "过气", "深度长文 > 短讯"):
            self.assertIn(kw, prompt)

    def test_body_len_and_excerpt_shown(self):
        gated = [dict(HN_ITEM, body_len=4321, excerpt="The reward design is simple.")]
        prompt = daily_ai._build_pick_prompt(gated)
        self.assertIn("正文约 4321 字", prompt)
        self.assertIn("已验证可抓全文", prompt)
        self.assertIn("正文预览: The reward design is simple.", prompt)
        # 全池都过闸门时，prompt 明示「用预览判断内容质量」
        self.assertIn("预览", prompt)

    def test_ungated_pool_has_no_excerpt_markers(self):
        """闸门全军覆没回退全量池：不带闸门痕迹，prompt 与旧形态一致。"""
        prompt = daily_ai._build_pick_prompt(CANDS)
        self.assertNotIn("已验证可抓全文", prompt)
        self.assertNotIn("正文预览", prompt)

    def test_ai_industry_news_carveout(self):
        """选文标准 v4（2026-10-03，用户：帖子主要写 AI 话题）：
        AI 相关的行业动态/产品发布/并购不再一票否决，非 AI 同类仍禁。"""
        prompt = daily_ai._build_pick_prompt(CANDS)
        self.assertIn("AI 话题例外", prompt)
        self.assertIn("行业动态", prompt)
        self.assertIn("不在禁选之列", prompt)
        self.assertIn("非 AI 话题的同类消息仍然不选", prompt)

    def test_non_tech_article_veto(self):
        """2026-10-07 用户：「跟技术有什么关系，不要把这种选进来」——
        模拟演示实锤：罗马游记（少数派 Matrix）在可抓池里，编者按还能用
        「对架构思维有启发」把它合理化。非技术长文必须硬否决。"""
        prompt = daily_ai._build_pick_prompt(CANDS)
        self.assertIn("必须是技术/科技类", prompt)
        self.assertIn("旅行游记", prompt)
        self.assertIn("合理化", prompt)


class TestTranslationIntact(unittest.TestCase):
    """翻译完整性机械断言：09-18 事故（重译把 9 个 SQL 代码块删光）不能只靠
    prompt 约定防——模型一次抖动就静默复发。断言 = 机制防线。"""

    def test_deleted_fence_caught(self):
        """09-18 事故形态：代码块被整段删光。"""
        orig = "a\n\n```sql\nSELECT 1;\n```\n\nb"
        bad = "甲\n\n乙"
        ok, why = daily_ai.translation_intact(orig, bad)
        self.assertFalse(ok)
        self.assertIn("围栏", why)

    def test_extra_fence_caught(self):
        """译文凭空多出代码块 = 模型编造内容，同样拦截。"""
        ok, why = daily_ai.translation_intact("plain text only", "甲\n\n```\ncode\n```")
        self.assertFalse(ok)
        self.assertIn("围栏", why)

    def test_paragraph_loss_caught(self):
        orig = "\n\n".join(f"第{i}段讲一个独立的观点，内容足够长。" for i in range(10))
        ok, why = daily_ai.translation_intact(orig, "只有一段概述。")
        self.assertFalse(ok)
        self.assertIn("段落", why)

    def test_number_loss_caught(self):
        """关键数据不许静默蒸发（81%/延迟/版本号是技术文的硬信息）。"""
        orig = "The model has 4B params, latency 81 ms, throughput 1234 tokens."
        bad = "模型参数更小，延迟更低，吞吐更高。"
        ok, why = daily_ai.translation_intact(orig, bad)
        self.assertFalse(ok)
        self.assertIn("数字", why)

    def test_good_translation_passes(self):
        orig = ("RL trains a 4B model.\n\n```sql\nSELECT 1;\n```\n\n"
                "Latency dropped 81 ms. Throughput 1234 tokens.")
        trans = ("强化学习训练了一个 4B 模型。\n\n```sql\nSELECT 1;\n```\n\n"
                 "延迟下降 **81 毫秒**。吞吐 1234 tokens。")
        ok, why = daily_ai.translation_intact(orig, trans)
        self.assertTrue(ok, why)

    def test_no_numbers_and_no_fences_ok(self):
        ok, why = daily_ai.translation_intact("a\n\nb\\nc", "甲\n\n乙丙")
        self.assertTrue(ok, why)


class TestTranslateGuard(unittest.TestCase):
    """ai_translate 的断言→重试 1 次→降级 None 链（调用方走 digest→summary→none）。"""

    ORIG = "text\n\n```sql\nSELECT 1;\n```\n\nmore"

    def test_bad_then_good_retry_succeeds(self):
        bad = "译文把代码弄丢了"
        good = "甲\n\n```sql\nSELECT 1;\n```\n\n乙"
        with mock.patch.object(daily_ai.llm, "chat", side_effect=[bad, good]) as m:
            r = daily_ai.ai_translate(self.ORIG)
        self.assertEqual(r, good)
        self.assertEqual(m.call_count, 2)

    def test_bad_twice_degrades_none(self):
        with mock.patch.object(daily_ai.llm, "chat", return_value="译文无代码"):
            self.assertIsNone(daily_ai.ai_translate(self.ORIG))

    def test_empty_and_refusal_no_retry(self):
        """空响应/拒答维持原行为：直接 None，不空耗重试。"""
        with _ai(None) as m:
            self.assertIsNone(daily_ai.ai_translate("hello"))
        self.assertEqual(m.call_count, 1)

    def test_prompt_still_asks_fence_preservation(self):
        """断言是兜底，prompt 约定仍在（降低重试率）。"""
        with mock.patch.object(daily_ai.llm, "chat", return_value="x") as m:
            daily_ai.ai_translate("SELECT 1;")
        sys_prompt = m.call_args.kwargs.get("system") or ""
        self.assertIn("代码块", sys_prompt)
        self.assertIn("原样保留", sys_prompt)


if __name__ == "__main__":
    unittest.main()
