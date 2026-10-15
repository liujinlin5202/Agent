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

    def test_resource_recommendation_does_not_use_candidate_limit(self):
        self.store.upsert_news([_news("本周资源推荐", points=999), _news("缓存失效的形式化推导", points=10)])
        self.assertEqual([it["title"] for it in self.store.top_candidates(limit=1)], ["缓存失效的形式化推导"])


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


class TestTopCandidatesQuota(PoolTestBase):
    def test_per_source_quota(self):
        # 12 条 HN + 4 条 sspai：单源配额 6 → HN 最多 6 条，sspai 不被挤掉。
        # 标题须实质差异（D5 标题模糊去重 0.85 会剔相似标题）。
        topics = [
            "Redis 事件循环与管道延迟的实测分析",
            "Kafka 分区再均衡的坑与修复路径",
            "Postgres 查询计划器如何选择索引",
            "gRPC 长连接在弱网下的重连策略",
            "LLVM 寄存器分配的两代算法对比",
            "WebGPU 着色器编译管线的坑",
            "eBPF 观测内核锁竞争的实操",
            "io_uring 与 epoll 的吞吐差距从哪来",
            "ZGC 毫秒级停顿的着色指针原理",
            "Arrow 列式内存布局对缓存命中率的影响",
            "Torch 编译模式在推理侧的收益实测",
            "Ray 分布式调度器的故障恢复设计",
        ]
        sspai_titles = [
            "少数派深度：跨平台框架的渲染架构对比",
            "从零实现一个终端复用器的心得",
            "开发者如何做一次靠谱的技术选型",
            "静态站点生成器的模板引擎评测",
        ]
        items = ([_news(t, source="hacker-news", points=1000 - i)
                  for i, t in enumerate(topics)]
                 + [_news(t, source="sspai") for t in sspai_titles])
        self.store.upsert_news(items)
        cands = self.store.top_candidates(limit=24)
        by_src = {}
        for c in cands:
            by_src[c["source"]] = by_src.get(c["source"], 0) + 1
        self.assertEqual(by_src.get("hacker-news"), 6)
        self.assertEqual(by_src.get("sspai"), 4)

    def test_dead_sources_excluded_before_selection(self):
        # 知乎死源在配额分配前剔除——不能先吃 8 席再被调用方过滤（10-15 实证）
        items = ([_news(f"知乎热榜话题 {i}：一篇深度分析", source="zhihu-hot")
                  for i in range(10)]
                 + [_news("HN 上一篇机制分析长文", source="hacker-news", points=500)])
        self.store.upsert_news(items)
        cands = self.store.top_candidates(limit=24)
        self.assertTrue(cands)
        self.assertTrue(all("zhihu" not in (c.get("url") or "") for c in cands))
        self.assertTrue(all(c["source"] != "zhihu-hot" for c in cands))

    def test_depth_floor_guarantees_subscription_seats(self):
        # 订阅系/工程博客无投票热度（heat=50 保底），纯排名会全被 HN 压掉；
        # 深度源每源保底 2 席 → judge 每天都能看到 research 长文形态。
        topics = [
            "Hacker News 头条之一：某数据中心故障复盘",
            "Hacker News 头条之二：某语言运行时剖析",
            "Hacker News 头条之三：某数据库论文导读",
            "Hacker News 头条之四：某浏览器引擎漫谈",
            "Hacker News 头条之五：某编译器优化实测",
            "Hacker News 头条之六：某分布式一致性证明",
            "Hacker News 头条之七：某操作系统内核笔记",
            "Hacker News 头条之八：某网络协议栈测量",
            "Cloudflare 边缘网络上的机器学习推理实践",
            "Cloudflare Workers 如何把冷启动压到毫秒级",
            "Netflix 推荐系统的在线学习方法演进",
            "Netflix 视频编码管线如何做 A/B 实验",
        ]
        items = ([_news(t, source="hacker-news", points=2000 - i)
                  for i, t in enumerate(topics[:8])]
                 + [_news(topics[8], source="cloudflare", points=0),
                    _news(topics[9], source="cloudflare", points=0),
                    _news(topics[10], source="netflix-tech", points=0),
                    _news(topics[11], source="netflix-tech", points=0)])
        self.store.upsert_news(items)
        cands = self.store.top_candidates(limit=24)
        by_src = {}
        for c in cands:
            by_src[c["source"]] = by_src.get(c["source"], 0) + 1
        self.assertEqual(by_src.get("cloudflare"), 2)   # 保底 2 席
        self.assertEqual(by_src.get("netflix-tech"), 2)
        self.assertEqual(by_src.get("hacker-news"), 6)  # 配额封顶


if __name__ == "__main__":
    unittest.main()
