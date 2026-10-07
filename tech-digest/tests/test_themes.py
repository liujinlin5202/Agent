# -*- coding: utf-8 -*-
"""主题日单测：周一工具/资源、周三技能/面试/学习、周五开源项目精选，其余日自由。"""
import unittest

from app.themes import theme_label, theme_of


class TestThemeDays(unittest.TestCase):
    def test_monday_is_tools(self):
        name, guide = theme_of("2026-09-14")          # 周一
        self.assertEqual(name, "工具与资源")
        self.assertIn("工具", guide)

    def test_wednesday_is_skills(self):
        name, guide = theme_of("2026-09-16")          # 周三
        self.assertEqual(name, "技能与学习")
        self.assertTrue("面试" in guide or "简历" in guide)

    def test_friday_is_open_source(self):
        name, guide = theme_of("2026-09-18")          # 周五
        self.assertEqual(name, "开源项目精选")
        self.assertIn("Trending", guide)

    def test_free_days_return_none(self):
        """周二/周四/周六/周日无主题日，选题交回 AI 自由判断。"""
        for day in ("2026-09-15", "2026-09-17", "2026-09-12", "2026-09-13"):
            self.assertIsNone(theme_of(day), day)

    def test_theme_label_empty_on_free_day(self):
        self.assertEqual(theme_label("2026-09-15"), "")
        self.assertEqual(theme_label("2026-09-14"), "工具与资源")

    def test_invalid_date_is_free(self):
        """日期解析失败按自由日处理（不抛异常，报告脚本容错）。"""
        self.assertIsNone(theme_of(""))
        self.assertIsNone(theme_of("not-a-date"))
        self.assertEqual(theme_label("2026-13-45"), "")


if __name__ == "__main__":
    unittest.main()
