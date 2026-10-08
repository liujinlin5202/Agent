# -*- coding: utf-8 -*-
"""ingest / 池模式取材 集成单测（编辑部 M1）。

覆盖决策：D6（daily 池模式与空池回退、发帖成功才标消费）、D7（star 最新一班）、
D9（403 触发冷却 + 冷却源剔除）、D10（ingest dry-run 零写池）。
"""
import tempfile
import gc
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import main
from app.pool import PoolStore
from main import run_ingest


def _news(title, source="hacker-news", points=0):
    it = {"type": "news", "title": title, "url": f"https://x.com/{title}",
          "source": source, "author": "a", "published_at": None,
          "fetched_at": "2026-10-08T07:07:00", "summary": f"{title} 的摘要。",
          "ai_summary": None, "confidence": None, "trending": None}
    if points:
        it["points"] = points
    return it


def _trending(rank, full):
    return {"type": "trending", "title": full, "url": f"https://github.com/{full}",
            "source": "github-trending", "author": full.split("/")[0],
            "published_at": None, "fetched_at": "2026-10-08T07:07:00",
            "summary": "repo", "ai_summary": None, "confidence": None,
            "trending": {"rank": rank, "language": "Go", "stars": 100,
                         "today_stars": 10}}


class IngestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.datadir = Path(self.tmp.name)
        self.patches = [
            patch("app.config.settings.data_dir", self.datadir),
            patch("app.pool.settings.data_dir", self.datadir),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        # Windows 下 sqlite 语句句柄的引用循环会延迟 db 文件句柄释放，不 collect
        # 会导致临时目录清理报文件锁（Linux 无此问题，加一行只为开发机卫生）
        gc.collect()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _pool(self):
        return PoolStore(self.datadir / "tech-digest.db")


class TestRunIngest(IngestTestBase):
    def _run(self, items=None, stats=None, raises=False, dry=False):
        items = items if items is not None else [_news("爆文一", points=100),
                                                 _trending(1, "a/one")]
        stats = stats if stats is not None else {
            "hacker-news": {"source": "hacker-news", "fetched": 1, "error": None},
            "github-trending": {"source": "github-trending", "fetched": 1,
                                "error": None}}
        with patch.object(main, "fetch_all") as f:
            if raises:
                f.side_effect = RuntimeError("全部源失败: {}")
            else:
                f.return_value = (items, stats)
            rc = run_ingest(dry_run=dry)
        return rc, f

    def test_ingest_fills_pool(self):
        rc, f = self._run()
        self.assertEqual(rc, 0)
        pool = self._pool()
        self.assertEqual(pool.stats().get("news:candidate"), 1)
        self.assertEqual(pool.stats().get("trending:candidate"), 1)
        pool.close()

    def test_sources_filtered_by_cooldown_next_run(self):
        """403 源进冷却；下一班 fetch_all 收到的源列表里没有它（D9）。"""
        stats = {"zhihu-hot": {"source": "zhihu-hot", "fetched": 0,
                               "error": "GET failed after 3 attempts: "
                                        "HTTP 403 for https://zhihu.com/hot"}}
        self._run(items=[_news("HN 文", points=5)], stats=stats)
        pool = self._pool()
        self.assertIn("zhihu-hot", pool.cooldowns())
        pool.close()
        rc, f = self._run()
        self.assertEqual(rc, 0)
        called_sources = f.call_args[0][0]
        self.assertNotIn("zhihu-hot", called_sources)
        self.assertIn("hacker-news", called_sources)

    def test_all_sources_fail_pool_untouched(self):
        self._run()
        rc, _ = self._run(raises=True)
        self.assertEqual(rc, 1)
        pool = self._pool()
        self.assertEqual(pool.stats().get("news:candidate"), 1)   # 原状

    def test_dry_run_writes_nothing_to_pool(self):
        rc, _ = self._run(dry=True)
        self.assertEqual(rc, 0)
        pool = self._pool()
        self.assertEqual(pool.stats(), {})        # 零写池（D10）

    def test_second_run_refreshes_not_duplicates(self):
        """跑两轮：同 URL 刷新，池子不翻倍（验收「跑两轮池子有货」的反面校验）。"""
        self._run()
        self._run()
        pool = self._pool()
        self.assertEqual(pool.stats().get("news:candidate"), 1)


class TestDailyPoolMode(IngestTestBase):
    def _seed_pool(self):
        pool = self._pool()
        pool.upsert_news([_news("池内高热文", points=900),
                          _news("池内次热文", points=100)])
        pool.close()

    def _run_daily(self, pick=None):
        pick = pick or {"idx": 1, "why": "这篇值得读，跟数据库课设直接相关，实习面试也常考。",
                        "digest": "", "post_titles": ["池内高热文值得今天读吗"]}
        captured = {}
        with patch("main._pool_daily_materials") as pm, \
             patch("main.Store") as fake_store, \
             patch("main.settings.sse_market_api_key", "fake-key"), \
             patch("main.settings.deepseek_api_key", ""), \
             patch("main.settings.output_dir", self.datadir), \
             patch("main.settings.market_refresh_token", ""), \
             patch("main.settings.market_user_telephone", ""), \
             patch("main.settings.publish_weekend", True), \
             patch("main.date") as fdate, \
             patch("main._prefetch_gate", side_effect=lambda c: c[:1]), \
             patch("main.daily_ai.ai_pick", return_value=pick), \
             patch("main.daily_ai.ai_glossary", return_value=[]), \
             patch("main._fetch_body", return_value=("全文。" * 100, "full", "")), \
             patch("main.render_repost", return_value="md"), \
             patch("app.pool.settings.data_dir", self.datadir):
            pm.return_value = self._materials()
            fs = fake_store.return_value
            fs.daily_exists.return_value = False
            fs.is_daily_posted.return_value = True     # 发帖成功路径
            fs.get_issue_no.return_value = None
            fs.next_issue_no.return_value = 42
            fs.recent_daily_titles.return_value = []
            fdate.today.return_value = datetime.now().date()
            fdate.fromisoformat.side_effect = __import__("datetime").date.fromisoformat
            rc = main.run_daily(force=True, dry_run=False)
            captured["saved"] = fs.save_daily_v2.call_args
        return rc, captured

    def _materials(self):
        pool = self._pool()
        try:
            return pool.top_candidates(limit=24), pool.latest_trending()
        finally:
            pool.close()

    def test_pool_mode_picks_from_pool_and_consumes(self):
        self._seed_pool()
        rc, captured = self._run_daily()
        self.assertEqual(rc, 0)
        saved_items = captured["saved"][0][1]
        self.assertEqual(saved_items[0]["title"], "池内高热文")   # 候选来自池
        pool = self._pool()
        row = pool.conn.execute(
            "SELECT status, digest_date FROM pool_items WHERE title='池内高热文'"
        ).fetchone()
        pool.close()
        self.assertEqual(row["status"], "daily_used")            # 发帖成功 → 标消费
        self.assertTrue(row["digest_date"])

    def test_pool_mode_skips_consumed_next_day(self):
        self._seed_pool()
        self._run_daily()
        pool = self._pool()
        rest = pool.top_candidates(limit=24)
        pool.close()
        self.assertEqual([c["title"] for c in rest], ["池内次热文"])


class TestStarPoolMode(IngestTestBase):
    def test_star_uses_pool_latest_batch(self):
        pool = self._pool()
        pool.upsert_trending([_trending(2, "a/two"), _trending(1, "a/one")])
        pool.close()
        from main import _pool_star_trending
        got = _pool_star_trending()
        self.assertEqual([it["title"] for it in got], ["a/one", "a/two"])

    def test_star_empty_pool_returns_empty(self):
        from main import _pool_star_trending
        self.assertEqual(_pool_star_trending(), [])


if __name__ == "__main__":
    unittest.main()
