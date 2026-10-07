# -*- coding: utf-8 -*-
"""窗口与守卫单测。

守卫是「每两天」这个需求的唯一实现处：日历表达式做不到跨月奇偶，
所以用「日跑 + 日期差 >= interval_days」来保证节奏，重启后也能补跑。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ops_report import state, window


class TestShouldSend(unittest.TestCase):
    def test_first_run_always_sends(self):
        self.assertTrue(window.should_send(None, datetime(2026, 9, 20, 21, 30)))

    def test_same_day_after_send_skips(self):
        self.assertFalse(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 20, 21, 31)))

    def test_next_day_skips(self):
        self.assertFalse(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 21, 21, 30)))

    def test_two_days_later_sends(self):
        self.assertTrue(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 22, 21, 30)))

    def test_catches_up_after_downtime(self):
        """服务器停了一周，恢复后应立刻补发，而不是继续等。"""
        self.assertTrue(window.should_send(
            "2026-09-13T21:30:00", datetime(2026, 9, 20, 21, 30)))

    def test_non_string_last_sent_is_treated_as_never_sent(self):
        """state.json 被手改成 {"last_sent": 123} 时按「从未发过」兜底，不能崩。"""
        self.assertTrue(window.should_send(123, datetime(2026, 9, 22, 21, 30)))
        start, end = window.report_window(123, datetime(2026, 9, 22, 21, 30))
        self.assertEqual(start, end - timedelta(days=2))

    def test_custom_interval(self):
        self.assertTrue(window.should_send(
            "2026-09-20T21:30:00", datetime(2026, 9, 21, 21, 30), interval_days=1))


class TestReportWindow(unittest.TestCase):
    def test_first_run_looks_back_interval_days(self):
        start, end = window.report_window(None, datetime(2026, 9, 20, 21, 30))
        self.assertEqual(start, datetime(2026, 9, 18, 21, 30))
        self.assertEqual(end, datetime(2026, 9, 20, 21, 30))

    def test_window_starts_at_last_sent(self):
        # 起点取上次发送的整点时刻（含秒），不取回看兜底值 —— 用 21:30:05 而非 21:30:00，
        # 正是为了与「回看 interval_days」区分开，避免两种情况同值时断言失真。
        start, end = window.report_window(
            "2026-09-18T21:30:05", datetime(2026, 9, 20, 21, 30))
        self.assertEqual(start.year, 2026)
        self.assertEqual(start, datetime.fromisoformat("2026-09-18T21:30:05"))
        self.assertEqual(end, datetime(2026, 9, 20, 21, 30))

    def test_broken_last_sent_falls_back(self):
        start, end = window.report_window("不是时间", datetime(2026, 9, 20, 21, 30))
        self.assertEqual((end - start).days, 2)


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            st = state.State(Path(td))
            self.assertIsNone(st.last_sent())
            st.mark_sent("2026-09-20T21:30:00")
            self.assertEqual(state.State(Path(td)).last_sent(), "2026-09-20T21:30:00")

    def test_mark_sent_defaults_to_now(self):
        with tempfile.TemporaryDirectory() as td:
            st = state.State(Path(td))
            st.mark_sent()
            self.assertTrue(st.last_sent().startswith("20"))

    def test_corrupt_state_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            p.write_text("{坏掉的", encoding="utf-8")
            self.assertIsNone(state.State(Path(td)).last_sent())

    def test_alerts_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(state.pending_alerts(d), [])
            state.add_alert(d, "第一次投递失败")
            state.add_alert(d, "第二次投递失败")
            self.assertEqual(state.pending_alerts(d), ["第一次投递失败", "第二次投递失败"])
            state.clear_alerts(d)
            self.assertEqual(state.pending_alerts(d), [])


if __name__ == "__main__":
    unittest.main()
