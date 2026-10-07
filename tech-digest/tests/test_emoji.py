# -*- coding: utf-8 -*-
"""emoji 禁令（2026-09-21 用户决策）：周报/日报/标题全面去 emoji，仅 GitHub 榜单的 ⭐ 豁免。"""
import unittest

from app import titles
from app.emoji import strip_emoji


class TestStripEmoji(unittest.TestCase):
    def test_strips_common_pictographs(self):
        self.assertEqual(strip_emoji("🤖 自动整理 🚀 深度🧠"), " 自动整理  深度")

    def test_strips_symbol_blocks_and_variation_selector(self):
        self.assertEqual(strip_emoji("⚠️ 注意 ⚡ 快"), " 注意  快")   # 26xx 杂项符号 + FE0F
        self.assertEqual(strip_emoji("⬆️ 收起"), " 收起")             # 2B00 块（⭐ 所在块）

    def test_star_exempt(self):
        s = "Go ｜ ⭐ 36,685 · 本周 +13,548"
        self.assertEqual(strip_emoji(s), s)

    def test_keeps_typography(self):
        s = "→ ｜ — · ……（见 8/31 日报）"
        self.assertEqual(strip_emoji(s), s)

    def test_empty_passthrough(self):
        self.assertEqual(strip_emoji(""), "")
        self.assertIsNone(strip_emoji(None))

    def test_idempotent(self):
        self.assertEqual(strip_emoji(strip_emoji("🥇 a⭐b")), strip_emoji("🥇 a⭐b"))


class TestTitleEmoji(unittest.TestCase):
    def test_clean_strips_emoji(self):
        self.assertEqual(titles.clean("🔥OpenAI 发布 GPT-6"), "OpenAI 发布 GPT-6")

    def test_pick_emoji_candidate_cleaned(self):
        self.assertEqual(titles.pick(["🚀 智谱开源多模态模型"], []), "智谱开源多模态模型")


if __name__ == "__main__":
    unittest.main()
