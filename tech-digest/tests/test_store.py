# -*- coding: utf-8 -*-
"""存储层单测：幂等 / 保留策略 / 查询。"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app.store import Store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _items(self, n=2):
        return [{"rank": i, "full_name": f"o/r{i}", "url": "u", "language": "Py",
                 "stars": 10, "today_stars": 1, "desc": "d"} for i in range(1, n + 1)]

    def test_daily_idempotent(self):
        day = date.today().isoformat()
        self.assertFalse(self.store.daily_exists(day))
        self.store.save_daily(day, "github-trending", self._items(), "md")
        self.assertTrue(self.store.daily_exists(day))
        # 重复保存（INSERT OR REPLACE）后仍只有一行
        self.store.save_daily(day, "github-trending", self._items(3), "md2")
        days = self.store.get_daily_since(date.today() - timedelta(days=7))
        self.assertEqual(len(days), 1)
        self.assertEqual(len(days[0]["items"]), 3)  # 新数据覆盖

    def test_get_daily_since_range(self):
        old = (date.today() - timedelta(days=10)).isoformat()
        self.store.save_daily(old, "s", self._items(), "m1")
        self.store.save_daily(date.today().isoformat(), "s", self._items(), "m2")
        days = self.store.get_daily_since(date.today() - timedelta(days=7))
        self.assertEqual(len(days), 1)

    def test_weekly_publish_flow(self):
        week = "2026-W35"
        self.assertFalse(self.store.weekly_exists(week))
        self.store.save_weekly(week, "md", ai_used=True)
        self.assertTrue(self.store.weekly_exists(week))
        self.store.mark_published(week, 12345, "/postdetail/12345")
        row = self.store.conn.execute(
            "SELECT post_id, post_url FROM weekly_report WHERE week_label=?", (week,)).fetchone()
        self.assertEqual(row["post_id"], 12345)

    def test_cleanup(self):
        self.store.save_daily(date.today().isoformat(), "s", self._items(), "m")
        self.store.save_daily((date.today() - timedelta(days=200)).isoformat(), "s",
                              self._items(), "m2")
        self.store.save_weekly("2026-W01", "md", False)
        res = self.store.cleanup()
        self.assertEqual(res["daily_deleted"], 1)
        self.assertTrue(self.store.daily_exists(date.today().isoformat()))


if __name__ == "__main__":
    unittest.main()
