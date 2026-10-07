# -*- coding: utf-8 -*-
"""发帖标题校验单测：30 字上限 / 禁日期前缀 / 禁废话词 / 与近 7 期去重 / 兜底阶梯。

背景（2026-09-13 用户决策）：发帖标题此前是「M/D + 头条」的报告体，在信息流里
毫无点击理由。改为钩子式短标题，并加确定性校验——AI 给的标题不可信：
它天然爱写「今日速览」「XX 动态」这类任何一天都成立的词。
"""
import unittest

from app.titles import (
    MAX_TITLE_CHARS,
    clean,
    is_usable,
    pick,
    similarity,
)


class TestClean(unittest.TestCase):
    def test_strips_wrappers_and_numbering(self):
        self.assertEqual(clean("《显卡要变天？》"), "显卡要变天？")
        self.assertEqual(clean("1、显卡要变天？"), "显卡要变天？")
        self.assertEqual(clean("  显卡   要变天？ "), "显卡 要变天？")

    def test_strips_date_prefix(self):
        self.assertEqual(clean("9/12 显卡要变天？"), "显卡要变天？")
        self.assertEqual(clean("2026-09-12 显卡要变天？"), "显卡要变天？")
        self.assertEqual(clean("9月12日：显卡要变天？"), "显卡要变天？")

    def test_truncates_over_30(self):
        out = clean("很" * 50)
        self.assertEqual(len(out), MAX_TITLE_CHARS)
        self.assertTrue(out.endswith("…"))


class TestSimilarity(unittest.TestCase):
    def test_near_duplicate_is_high(self):
        a = "显卡要变天？今天这 3 件事跟你有关"
        b = "显卡要变天？今天这 4 件事跟你有关"
        self.assertGreater(similarity(a, b), 0.5)

    def test_unrelated_is_low(self):
        self.assertLess(similarity("显卡要变天？", "简历别再写课程作业了"), 0.2)

    def test_empty_is_zero(self):
        self.assertEqual(similarity("", "任意"), 0.0)


class TestIsUsable(unittest.TestCase):
    def test_ok_title(self):
        ok, _ = is_usable("显卡要变天？今天这 3 件事跟你有关", [])
        self.assertTrue(ok)

    def test_too_long_rejected(self):
        ok, why = is_usable("很" * 31, [])
        self.assertFalse(ok)
        self.assertIn("30", why)

    def test_date_prefix_rejected(self):
        ok, why = is_usable("9/12 显卡要变天？", [])
        self.assertFalse(ok)
        self.assertIn("日期", why)

    def test_banned_words_rejected(self):
        for t in ("今日速览：三件大事", "AI 行业动态", "本周开源一览",
                  "生态爆发的一周", "重磅：新模型发布"):
            ok, why = is_usable(t, [])
            self.assertFalse(ok, t)
            self.assertIn("禁用词", why)

    def test_duplicate_of_recent_rejected(self):
        recent = ["显卡要变天？今天这 3 件事跟你有关"]
        ok, why = is_usable("显卡要变天？今天这 4 件事跟你有关", recent)
        self.assertFalse(ok)
        self.assertIn("近 7 期", why)


class TestPick(unittest.TestCase):
    def test_takes_first_usable(self):
        self.assertEqual(
            pick(["今日速览：三件事", "显卡要变天？下一张卡再等等", "第三条"],
                 recent=[]),
            "显卡要变天？下一张卡再等等")

    def test_falls_back_when_all_bad(self):
        self.assertEqual(pick(["今日速览", "AI 动态"], recent=[], fallback="新模型发布"),
                         "新模型发布")

    def test_prefers_similar_over_fallback(self):
        """候选只栽在「与近 7 期太像」时，宁可复用也别退化成兜底模板。"""
        recent = ["显卡要变天？今天这 3 件事跟你有关"]
        self.assertEqual(
            pick(["显卡要变天？今天这 4 件事跟你有关"], recent=recent, fallback=""),
            "显卡要变天？今天这 4 件事跟你有关")

    def test_empty_when_nothing_usable(self):
        self.assertEqual(pick([], recent=[], fallback=""), "")
        self.assertEqual(pick(["今日速览"], recent=[], fallback=""), "")

    def test_fallback_is_cleaned(self):
        self.assertEqual(pick([], recent=[], fallback="9/12 显卡要变天？"),
                         "显卡要变天？")

    def test_result_always_within_limit(self):
        out = pick(["很" * 40], recent=[], fallback="短标题")
        self.assertEqual(out, "短标题")
        self.assertLessEqual(len(out), MAX_TITLE_CHARS)


class TestPickSalvage(unittest.TestCase):
    """候选只栽在「可确定性修复」的问题上（日期前缀/包裹符号/序号）→ 修好再用。

    接 main.py 时发现：prompt 已禁日期前缀，但模型仍会带（规则遵守率不是 100%）。
    原实现一律拒收 → 3 个候选全带日期时直接掉到兜底头条（平庸），
    而日期前缀恰恰是 clean() 能 100% 确定性去掉的东西。丢弃一个好钩子不如修一下。
    """

    def test_date_prefixed_candidate_salvaged(self):
        self.assertEqual(pick(["9/13 显卡要变天", "备选"], recent=[], fallback="兜底"),
                         "显卡要变天")

    def test_wrapped_candidate_salvaged(self):
        self.assertEqual(pick(["《显卡要变天？》"], recent=[], fallback="兜底"),
                         "显卡要变天？")

    def test_overlong_not_salvaged_by_truncation(self):
        """超长候选不能靠截断「修」成合规——截断出来的标题是断句，不如换下一个候选。"""
        self.assertEqual(pick(["很" * 40], recent=[], fallback="短标题"), "短标题")

    def test_banned_word_not_salvaged(self):
        """禁用词是 clean() 修不掉的（要改词，不是去装饰）→ 不抢救，走下一个。"""
        self.assertEqual(pick(["9/13 今日速览"], recent=[], fallback="兜底"), "兜底")

    def test_salvage_still_respects_recent(self):
        """抢救不是免检：修掉日期前缀后仍撞近期标题 → 不抢救。"""
        recent = ["显卡要变天？今天这 3 件事跟你有关"]
        self.assertEqual(
            pick(["9/13 显卡要变天？今天这 4 件事跟你有关"], recent=recent, fallback="兜底"),
            "兜底")


if __name__ == "__main__":
    unittest.main()
