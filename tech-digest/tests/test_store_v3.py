# -*- coding: utf-8 -*-
"""存储层新增单测：发帖标题留档 / 近期标题查询 / 补楼幂等 / 浏览采样。

2026-09-13 日报 v3 改造带来的持久化需求：
  · 标题校验器（app/titles.py）要拿近 7 期标题做去重 → daily_snapshot 需留 post_title
  · 星榜完整版作为 1 楼评论补发 → 需要「当日是否已补楼」的幂等标记
  · 表现追踪（scripts/content_report.py）要按日采样浏览量 → post_views 表
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app.store import SCHEMA, Store


class TestPostTitle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_title_saved_and_read_back(self):
        self.store.save_daily_v2("2026-09-14", [], "md", post_title="显卡要变天？")
        self.assertEqual(self.store.recent_daily_titles(7, "2026-09-15"), ["显卡要变天？"])

    def test_recent_titles_excludes_self_and_blanks(self):
        self.store.save_daily_v2("2026-09-14", [], "md", post_title="A 标题")
        self.store.save_daily_v2("2026-09-15", [], "md", post_title="B 标题")
        self.store.save_daily_v2("2026-09-16", [], "md")            # 无标题（未发帖）
        # before_day=09-15 → 只回 09-14，不含当日、不含空标题
        self.assertEqual(self.store.recent_daily_titles(7, "2026-09-15"), ["A 标题"])
        self.assertEqual(self.store.recent_daily_titles(7, "2026-09-17"),
                         ["B 标题", "A 标题"])

    def test_recent_titles_limit_and_order(self):
        for i in range(1, 10):
            self.store.save_daily_v2(f"2026-09-{i:02d}", [], "md", post_title=f"标题{i}")
        got = self.store.recent_daily_titles(7, "2026-09-10")
        self.assertEqual(len(got), 7)
        self.assertEqual(got[0], "标题9")       # 最近的在前

    def test_save_daily_v2_backward_compatible(self):
        """旧调用（不传 post_title）仍然合法。"""
        self.assertEqual(self.store.save_daily_v2("2026-09-14", [], "md"), 1)
        self.assertEqual(self.store.recent_daily_titles(7, "2026-09-15"), [])


class TestDailyPostId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_none_when_not_posted(self):
        self.assertIsNone(self.store.daily_post_id("2026-09-14"))

    def test_reads_marked_post_id(self):
        self.store.mark_daily_posted("2026-09-14", 4321)
        self.assertEqual(self.store.daily_post_id("2026-09-14"), 4321)

    def test_zero_post_id_is_not_a_target(self):
        """后端成功时不返回 postID（发帖实测），0 不能当成可评论的目标。"""
        self.store.mark_daily_posted("2026-09-14", 0)
        self.assertIsNone(self.store.daily_post_id("2026-09-14"))


class TestStarPostId(unittest.TestCase):
    """v4 周六星榜专帖幂等：与日报同款 meta 标记，键名独立（互不干扰）。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_none_when_not_posted(self):
        self.assertFalse(self.store.is_star_posted("2026-09-19"))

    def test_mark_then_read(self):
        self.store.mark_star_posted("2026-09-19", 6789)
        self.assertTrue(self.store.is_star_posted("2026-09-19"))

    def test_independent_from_daily_marker(self):
        self.store.mark_star_posted("2026-09-19", 1)
        self.assertFalse(self.store.is_daily_posted("2026-09-19"))
        self.store.mark_daily_posted("2026-09-19", 2)
        self.assertTrue(self.store.is_star_posted("2026-09-19"))


class TestDailyComment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_idempotent_mark(self):
        self.assertFalse(self.store.is_daily_commented("2026-09-14"))
        self.store.mark_daily_commented("2026-09-14")
        self.assertTrue(self.store.is_daily_commented("2026-09-14"))
        # 重复标记不炸（同日重跑）
        self.store.mark_daily_commented("2026-09-14")
        self.assertTrue(self.store.is_daily_commented("2026-09-14"))


class TestViewSamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_append_and_resample_same_day(self):
        self.store.add_view_sample(100, "2026-09-14", "2026-09-14", 30, 1, 0, 5.0)
        self.store.add_view_sample(100, "2026-09-14", "2026-09-15", 64, 2, 1, 12.0)
        # 同日重复采样 → 覆盖，不产生第三行
        self.store.add_view_sample(100, "2026-09-14", "2026-09-15", 70, 3, 1, 15.0)
        rows = self.store.view_samples(since="2026-09-01")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["browse"], 70)
        self.assertEqual(rows[1]["sample_day"], "2026-09-15")

    def test_filter_since_and_order(self):
        self.store.add_view_sample(1, "2026-09-01", "2026-09-01", 10, 0, 0, 0)
        self.store.add_view_sample(2, "2026-09-14", "2026-09-14", 20, 0, 0, 0)
        rows = self.store.view_samples(since="2026-09-10")
        self.assertEqual([r["post_id"] for r in rows], [2])

    def test_cleanup_drops_old_samples(self):
        old = (date.today() - timedelta(days=200)).isoformat()
        self.store.add_view_sample(1, old, old, 10, 0, 0, 0)
        self.store.add_view_sample(2, date.today().isoformat(),
                                   date.today().isoformat(), 20, 0, 0, 0)
        self.store.cleanup()
        self.assertEqual([r["post_id"] for r in self.store.view_samples()], [2])


class TestMigration(unittest.TestCase):
    def test_old_db_gets_post_title_column(self):
        """旧库（无 post_title / post_views）打开后自动迁移，不丢历史数据。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            db = Path(tmp.name) / "old.db"
            import sqlite3
            conn = sqlite3.connect(str(db))
            conn.executescript(
                "CREATE TABLE daily_snapshot (date TEXT PRIMARY KEY, source TEXT NOT NULL,"
                " items_json TEXT NOT NULL, md_path TEXT, created_at TEXT NOT NULL);")
            conn.execute("INSERT INTO daily_snapshot VALUES('2026-09-01','s','[]','m','t')")
            conn.commit()
            conn.close()

            store = Store(db)
            cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(daily_snapshot)")}
            self.assertIn("post_title", cols)
            self.assertEqual([d["date"] for d in store.get_daily_since(date(2026, 1, 1))],
                             ["2026-09-01"])          # 历史数据仍在
            store.add_view_sample(1, "2026-09-01", "2026-09-01", 1, 0, 0, 0)  # 新表可用
            store.close()
        finally:
            tmp.cleanup()

    def test_schema_lists_new_tables(self):
        self.assertIn("post_views", SCHEMA)


if __name__ == "__main__":
    unittest.main()
