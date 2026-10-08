# -*- coding: utf-8 -*-
"""pool 层单测（编辑部 M1）：入池三路 / 粗排 / 消费标记 / 生命周期 / 冷却 / 最新一班。

对应决策（docs/superpowers/plans/2026-10-08-editorial-m1-content-pool.md）：
  D3 schema（kind+url 身份）、D4 粗排公式、D5 去重三路、D6 mark_consumed 语义、
  D7 trending「最新一班」、D9 冷却。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.pool import (HEAT_DEFAULT, PoolStore, normalize_heat)
from app.store import connect_db


def _news(title, source="hacker-news", points=0, url=None, hours_ago=None):
    it = {"type": "news", "title": title,
          "url": f"https://x.com/{title}" if url is None else url,
          "source": source, "author": "a", "published_at": None,
          "fetched_at": "2026-10-08T07:07:00", "summary": f"{title} 的摘要。",
          "ai_summary": None, "confidence": None, "trending": None}
    if points:
        it["points"] = points
    if hours_ago is not None:
        it["published_at"] = (datetime.now() - timedelta(hours=hours_ago)
                              ).isoformat(timespec="seconds")
    return it


def _trending(rank, full="user/repo", today_stars=10):
    return {"type": "trending", "title": full, "url": f"https://github.com/{full}",
            "source": "github-trending", "author": full.split("/")[0],
            "published_at": None, "fetched_at": "2026-10-08T07:07:00",
            "summary": "repo desc", "ai_summary": None, "confidence": None,
            "trending": {"rank": rank, "language": "Go", "stars": 100,
                         "today_stars": today_stars}}


class PoolTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PoolStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _age(self, url, days):
        """把某行的 first_seen 拨回 days 天前（测池龄/TTL）。"""
        old = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        self.store.conn.execute(
            "UPDATE pool_items SET first_seen=?, last_seen=? WHERE url=?",
            (old, old, url))
        self.store.conn.commit()


class TestSchemaAndConn(PoolTestBase):
    def test_wal_mode_on(self):
        mode = self.store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_schema_idempotent(self):
        p = Path(self.tmp.name) / "test.db"
        s2 = PoolStore(p)          # 二次实例化不炸（部署日免手工迁移）
        s2.close()

    def test_kind_url_identity(self):
        """同一 URL 可以同时以 news 与 trending 两种身份入池（D3 身份=（kind,url)）。"""
        u = "https://github.com/foo/bar"
        self.store.upsert_news([_news("foo/bar 发布了新版本", url=u)])
        self.store.upsert_trending([_trending(1, "foo/bar")])
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM pool_items WHERE url=?", (u,)).fetchone()[0]
        self.assertEqual(n, 2)


class TestUpsertNews(PoolTestBase):
    def test_insert_refresh_drop_three_ways(self):
        long_title = "深度解析 RISC-V 向量扩展的架构设计与编译器实现"
        r1 = self.store.upsert_news([_news(long_title, points=100)])
        self.assertEqual(r1, dict(inserted=1, refreshed=0, dup_dropped=0, dup_samples=[]))
        # 同 URL 重抓 → 刷新不重复
        r2 = self.store.upsert_news([_news(long_title, points=200)])
        self.assertEqual((r2["inserted"], r2["refreshed"]), (0, 1))
        # 换 URL 但标题几乎一样（镜像站转载场景）→ 撞弃
        mirror = self.store.upsert_news(
            [_news(long_title + "（全文）", url="https://mirror.com/riscv")])
        self.assertEqual(mirror["dup_dropped"], 1)
        # 标题只一部分重叠（0.6 上下）不算重复——阈值语义不被误伤
        other = self.store.upsert_news(
            [_news("文章甲（转载）", url="https://x.com/文章甲（转载）")])
        self.assertEqual(other["inserted"], 1)

    def test_refresh_updates_raw_and_heat(self):
        self.store.upsert_news([_news("文章甲", points=10)])
        self.store.upsert_news([_news("文章甲", points=999)])
        row = self.store.conn.execute(
            "SELECT raw, heat FROM pool_items WHERE title='文章甲'").fetchone()
        self.assertIn("999", row["raw"])
        self.assertGreater(row["heat"], 0)     # 有信号源的 heat 分位重算

    def test_title_dup_also_checked_against_consumed(self):
        """撞查覆盖任意状态（daily_used 也拦）——3 天前发过的文章换 URL 不复活。"""
        t = "深度解析 RISC-V 向量扩展的架构设计与编译器实现"
        self.store.upsert_news([_news(t, points=50)])
        row = self.store.conn.execute(
            "UPDATE pool_items SET status='daily_used', digest_date='2026-10-05' "
            "WHERE title LIKE '深度解析%'").rowcount
        self.assertEqual(row, 1)
        r = self.store.upsert_news([_news(t, url="https://mirror.com/riscv")])
        self.assertEqual(r["dup_dropped"], 1)

    def test_history_param_blocks_bootstrap_republish(self):
        """history（已发快照）参与撞查：池子冷启动不放行近 14 天已发文章。"""
        t = "昨天的爆文独家分析全文"
        r = self.store.upsert_news(
            [_news(t, url="https://x.com/old")],
            history=[("2026-10-07", _news(t, url="https://old.com/1"))])
        self.assertEqual(r["inserted"], 0)

    def test_malformed_items_skipped(self):
        r = self.store.upsert_news([_news("无链接文章", url=""),
                                    {"type": "news", "title": "",
                                     "url": "https://x.com/9"}])
        r2 = self.store.upsert_news([_news("正常文章")])
        self.assertEqual(r["inserted"], 0)
        self.assertEqual(r2["inserted"], 1)


class TestNormalizeHeat(unittest.TestCase):
    def test_percentile_ordering(self):
        items = [_news(f"p{i}", points=v) for i, v in enumerate([10, 50, 100, 500])]
        heat = normalize_heat(items)
        # points 越大 heat 越高；最高 100、最低 0
        self.assertEqual(heat[normalize_url_of("p3")], 100.0)
        self.assertEqual(heat[normalize_url_of("p0")], 0.0)
        self.assertLess(heat[normalize_url_of("p0")], heat[normalize_url_of("p1")])

    def test_no_signal_gets_default(self):
        items = [_news("订阅文", source="ruanyifeng"), _news("HN 文", points=7)]
        heat = normalize_heat(items)
        self.assertEqual(heat[normalize_url_of("订阅文")], HEAT_DEFAULT)
        self.assertEqual(heat[normalize_url_of("HN 文")], 100.0)

    def test_zhihu_rank_inverted(self):
        items = [_news("热榜第一", source="zhihu-hot"), _news("热榜第五", source="zhihu-hot")]
        for i, it in enumerate(items):
            it["trending"] = {"rank": i + 1, "heat": 0}
        heat = normalize_heat(items)
        self.assertEqual(heat[normalize_url_of("热榜第一")], 100.0)
        self.assertEqual(heat[normalize_url_of("热榜第五")], 0.0)


def normalize_url_of(title):
    """测试工厂里 url=https://x.com/{title}，对应的规范化值。"""
    from app.dedup import normalize_url
    return normalize_url(f"https://x.com/{title}")


class TestTopCandidates(PoolTestBase):
    def test_rank_formula_heat_beats_age(self):
        self.store.upsert_news([_news("高热新文", points=500, hours_ago=1)])
        self.store.upsert_news([_news("低热新文", source="ruanyifeng", hours_ago=1)])
        cands = self.store.top_candidates()
        self.assertEqual([c["title"] for c in cands], ["高热新文", "低热新文"])

    def test_ttl_window_excludes_old(self):
        self.store.upsert_news([_news("陈年旧文", points=999)])
        self._age(normalize_url_of("陈年旧文"), days=8)     # 默认池龄 7 天
        self.assertEqual(self.store.top_candidates(), [])

    def test_consumed_excluded(self):
        self.store.upsert_news([_news("已发文章", points=999)])
        self.store.mark_consumed([normalize_url_of("已发文章")], "2026-10-08")
        self.assertEqual(self.store.top_candidates(), [])

    def test_limit_and_pool_meta_keys(self):
        self.store.upsert_news([_news(f"文{i}", points=100 + i) for i in range(6)])
        cands = self.store.top_candidates(limit=3)
        self.assertEqual(len(cands), 3)
        self.assertIn("_pool_url", cands[0])
        self.assertIn("_pool_score", cands[0])
        # 粗排序：heat 高的先来
        self.assertEqual(cands[0]["title"], "文5")

    def test_empty_pool_returns_empty(self):
        self.assertEqual(self.store.top_candidates(), [])


class TestMarkConsumed(PoolTestBase):
    def test_consumed_sets_status_and_digest_date(self):
        self.store.upsert_news([_news("文章甲")])
        n = self.store.mark_consumed([normalize_url_of("文章甲")], "2026-10-08")
        self.assertEqual(n, 1)
        row = self.store.conn.execute(
            "SELECT status, digest_date FROM pool_items").fetchone()
        self.assertEqual(row["status"], "daily_used")
        self.assertEqual(row["digest_date"], "2026-10-08")

    def test_only_candidate_touched(self):
        self.store.upsert_news([_news("文章甲")])
        self.store.mark_consumed([normalize_url_of("文章甲")], "2026-10-08")
        # 已是 daily_used，再标不动（幂等且不改 digest_date）
        n = self.store.mark_consumed([normalize_url_of("文章甲")], "2026-10-09")
        self.assertEqual(n, 0)
        row = self.store.conn.execute("SELECT digest_date FROM pool_items").fetchone()
        self.assertEqual(row["digest_date"], "2026-10-08")

    def test_trending_not_consumable(self):
        self.store.upsert_trending([_trending(1, "a/b")])
        n = self.store.mark_consumed(["https://github.com/a/b"], "2026-10-08")
        self.assertEqual(n, 0)


class TestSweep(PoolTestBase):
    def _statuses(self):
        return dict(self.store.conn.execute(
            "SELECT url, status FROM pool_items").fetchall())

    def test_candidate_expired_by_ttl(self):
        self.store.upsert_news([_news("旧文")])
        self._age(normalize_url_of("旧文"), days=8)
        res = self.store.sweep()
        self.assertEqual(res["expired_candidate"], 1)
        self.assertEqual(self._statuses()[normalize_url_of("旧文")], "expired")

    def test_reserved_expired_by_reserved_ttl(self):
        self.store.upsert_news([_news("沉淀文")])
        self.store.conn.execute("UPDATE pool_items SET status='reserved'")
        self.store.conn.commit()
        self._age(normalize_url_of("沉淀文"), days=91)
        res = self.store.sweep()
        self.assertEqual(res["expired_reserved"], 1)

    def test_expired_purged_after_retain(self):
        """古董行（>保留期）在同一班 sweep 里直接清除，不留 expired 观察态。"""
        self.store.upsert_news([_news("古董文")])
        self._age(normalize_url_of("古董文"), days=95)
        res = self.store.sweep()
        self.assertEqual(res["expired_candidate"], 1)
        self.assertEqual(res["purged"], 1)
        self.assertEqual(self.store.stats().get("news:expired"), None)

    def test_expired_within_retain_stays(self):
        """8 天过期、未到 90 天保留期的行保持 expired（可追溯）。"""
        self.store.upsert_news([_news("旧文")])
        self._age(normalize_url_of("旧文"), days=8)
        self.store.sweep()
        self.assertEqual(self.store.stats().get("news:expired"), 1)

    def test_trending_purged_after_seven_days(self):
        self.store.upsert_trending([_trending(1, "a/b")])
        self.store.conn.execute(
            "UPDATE pool_items SET last_seen=? WHERE kind='trending'",
            ((datetime.now() - timedelta(days=8)).isoformat(timespec="seconds"),))
        self.store.conn.commit()
        res = self.store.sweep()
        self.assertEqual(res["trending_purged"], 1)


class TestLatestTrending(PoolTestBase):
    def test_empty_pool(self):
        self.assertEqual(self.store.latest_trending(), [])

    def test_latest_batch_only_sorted_by_rank(self):
        """隔班掉榜的仓库不混进「今日榜单」：只取最新一班（3h 窗）触碰过的行。"""
        self.store.upsert_trending([_trending(2, "a/two"), _trending(1, "a/one")])
        # 拨回 26h 前（昨天的一班），再入一批新的
        old = (datetime.now() - timedelta(hours=26)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE pool_items SET last_seen=?", (old,))
        self.store.conn.commit()
        self.store.upsert_trending([_trending(3, "a/three"), _trending(1, "a/one")])
        got = [it["title"] for it in self.store.latest_trending()]
        self.assertEqual(got, ["a/one", "a/three"])       # rank 升序；a/two 不在

    def test_stale_batch_returns_empty(self):
        self.store.upsert_trending([_trending(1, "a/old")])
        old = (datetime.now() - timedelta(hours=72)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE pool_items SET last_seen=?", (old,))
        self.store.conn.commit()
        self.assertEqual(self.store.latest_trending(max_age_h=48), [])


class TestCooldown(PoolTestBase):
    def test_set_and_read(self):
        self.store.set_cooldown("zhihu-hot", 24)
        self.assertEqual(list(self.store.cooldowns()), ["zhihu-hot"])

    def test_expired_cleaned(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.conn.execute(
            "INSERT INTO meta(key, value) VALUES('pool_cooldown:devto', ?)", (past,))
        self.store.conn.commit()
        self.assertEqual(self.store.cooldowns(), {})
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key='pool_cooldown:devto'").fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
