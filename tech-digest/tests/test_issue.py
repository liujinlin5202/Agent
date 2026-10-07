# -*- coding: utf-8 -*-
"""期数单测：递增 / 同日重跑幂等 / 旧库迁移。"""
import tempfile
import unittest
from pathlib import Path

from app.store import Store

ITEMS = [{"type": "trending", "title": "a/b", "url": "https://github.com/a/b",
          "source": "github-trending", "author": "a", "published_at": None,
          "fetched_at": "2026-08-30T09:00:00", "summary": "", "ai_summary": None,
          "confidence": None, "trending": None}]


class TestIssue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.store = Store(self.db)
        # addCleanup 为 LIFO：先注册 tmp 清理，最后注册 close → close 先执行（Windows 文件锁）
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_initial_one(self):
        self.assertEqual(self.store.next_issue_no(), 1)

    def test_increment(self):
        n1 = self.store.save_daily_v2("2026-08-31", ITEMS, "x.md")
        self.assertEqual(n1, 1)
        self.assertEqual(self.store.next_issue_no(), 2)
        self.assertEqual(self.store.get_issue_no("2026-08-31"), 1)
        n2 = self.store.save_daily_v2("2026-09-01", ITEMS, "y.md")
        self.assertEqual(n2, 2)

    def test_rerun_same_day_idempotent(self):
        n1 = self.store.save_daily_v2("2026-08-31", ITEMS, "x.md")
        n2 = self.store.save_daily_v2("2026-08-31", ITEMS, "x.md")  # --force 重跑
        self.assertEqual((n1, n2), (1, 1))
        self.assertEqual(self.store.next_issue_no(), 2)  # 不跳号
        self.assertEqual(self.store.get_daily_since(__import__("datetime").date(2026, 8, 31))[0]["issue_no"], 1)

    def test_v1_schema_migrates(self):
        """旧库（无 issue_no 列/meta 表）打开后自动补齐并正常计数。"""
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            old_db = Path(td) / "old.db"
            con = sqlite3.connect(old_db)
            con.executescript("""
CREATE TABLE daily_snapshot (date TEXT PRIMARY KEY, source TEXT NOT NULL,
  items_json TEXT NOT NULL, md_path TEXT, created_at TEXT NOT NULL);
CREATE TABLE weekly_report (week_label TEXT PRIMARY KEY, md_path TEXT,
  ai_used INTEGER NOT NULL DEFAULT 0, post_id INTEGER, post_url TEXT,
  published_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE run_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  task TEXT NOT NULL, status TEXT NOT NULL, detail TEXT);
INSERT INTO daily_snapshot VALUES('2026-08-29','github-trending','[]','a.md','2026-08-29T09:00:00');
""")
            con.commit()
            con.close()
            store = Store(old_db)  # 触发迁移
            try:
                self.assertEqual(store.next_issue_no(), 1)  # 旧数据不计期数，从头开始
                n = store.save_daily_v2("2026-08-30", ITEMS, "b.md")
                self.assertEqual(n, 1)
                self.assertEqual(store.next_issue_no(), 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
