# -*- coding: utf-8 -*-
"""表现复盘表（app/report.py）单测。

2026-09-13 新增。改内容之前先得能看见效果：日报发了几十期，浏览量一直靠肉眼看
数据库，没有「同一口径下这期比上期好还是差」的答案。

口径（与 scripts/content_report.py sample 模式配套）：
  · daily 09:15 跑 → 给「昨天发的帖」记一次采样，正好是发布后 ~24 小时的浏览数；
  · h24 取发布次日那次采样，与总浏览分开看 —— 总浏览含长尾（实测 9/11 那期
    h24 只有 46，后来涨到 277），拿总数比会把长尾误当成当天内容的效果。
"""
import unittest

from app import report


def s(day, sample_day, browse, likes=0, comments=0, heat=0.0, post_id=1):
    return {"post_id": post_id, "day": day, "sample_day": sample_day,
            "sampled_at": f"{sample_day}T09:15:00", "browse": browse,
            "likes": likes, "comments": comments, "heat": heat}


class TestReportRows(unittest.TestCase):
    def test_h24_from_next_day_sample(self):
        rows = report.report_rows([s("2026-09-13", "2026-09-14", 58),
                                   s("2026-09-13", "2026-09-15", 64)])
        self.assertEqual(rows[0]["h24"], 58)
        self.assertEqual(rows[0]["browse"], 64)      # 当前 = 最新一次采样

    def test_missing_h24_is_none_not_zero(self):
        """没测到 ≠ 零浏览：缺次日采样必须留空，否则复盘表会显示成「没人看」。"""
        rows = report.report_rows([s("2026-09-13", "2026-09-15", 64)])
        self.assertIsNone(rows[0]["h24"])

    def test_weekday_and_theme(self):
        rows = report.report_rows([s("2026-09-14", "2026-09-15", 10)])   # 周一
        self.assertEqual(rows[0]["weekday"], "周一")
        self.assertEqual(rows[0]["theme"], "工具与资源")
        free = report.report_rows([s("2026-09-15", "2026-09-16", 10)])   # 周二：自由日
        self.assertEqual(free[0]["theme"], "")

    def test_sorted_by_day(self):
        rows = report.report_rows([s("2026-09-15", "2026-09-16", 1),
                                   s("2026-09-13", "2026-09-14", 2)])
        self.assertEqual([r["day"] for r in rows], ["2026-09-13", "2026-09-15"])

    def test_titles_attached(self):
        rows = report.report_rows([s("2026-09-13", "2026-09-14", 58)],
                                  titles={"2026-09-13": "显卡要变天？"})
        self.assertEqual(rows[0]["title"], "显卡要变天？")
        self.assertEqual(report.report_rows([s("2026-09-13", "2026-09-14", 58)])[0]["title"], "")

    def test_empty(self):
        self.assertEqual(report.report_rows([]), [])

    def test_bad_day_does_not_raise(self):
        rows = report.report_rows([s("bad-day", "2026-09-14", 5)])
        self.assertEqual(rows[0]["h24"], None)
        self.assertEqual(rows[0]["weekday"], "")


class TestRenderTable(unittest.TestCase):
    def test_renders_rows(self):
        md = report.render_table(report.report_rows(
            [s("2026-09-13", "2026-09-14", 58, likes=1, comments=2),
             s("2026-09-13", "2026-09-15", 64, likes=1, comments=3)],
            titles={"2026-09-13": "显卡要变天？"}))
        self.assertIn("| 日期 | 主题日 | 标题 | h24 浏览 | 当前浏览 | 赞 | 评 |", md)
        self.assertIn("| 09-13 周日 | — | 显卡要变天？ | 58 | 64 | 1 | 3 |", md)

    def test_missing_h24_rendered_as_dash(self):
        md = report.render_table(report.report_rows([s("2026-09-13", "2026-09-15", 64)]))
        self.assertIn("| — | 64 |", md)

    def test_escapes_pipe_in_title(self):
        md = report.render_table(report.report_rows(
            [s("2026-09-13", "2026-09-14", 5)], titles={"2026-09-13": "a|b"}))
        self.assertNotIn("a|b", md)

    def test_empty_message(self):
        self.assertIn("暂无", report.render_table([]))


class TestSummary(unittest.TestCase):
    """一行小结：本期均值 vs 上一期均值——改内容究竟有没有用，看这个数。"""

    def test_compares_two_windows(self):
        rows = report.report_rows([
            s("2026-09-01", "2026-09-02", 100), s("2026-09-02", "2026-09-03", 120),
            s("2026-09-08", "2026-09-09", 50), s("2026-09-09", "2026-09-10", 60),
        ])
        line = report.summary(rows, split="2026-09-05")
        self.assertIn("前 110", line)      # (100+120)/2
        self.assertIn("后 55", line)       # (50+60)/2
        self.assertIn("↓50%", line)

    def test_ignores_rows_without_h24(self):
        rows = report.report_rows([s("2026-09-01", "2026-09-03", 999)])
        self.assertIn("样本不足", report.summary(rows, split="2026-09-05"))

    def test_handles_no_baseline(self):
        rows = report.report_rows([s("2026-09-08", "2026-09-09", 50)])
        self.assertIn("样本不足", report.summary(rows, split="2026-09-05"))


if __name__ == "__main__":
    unittest.main()
