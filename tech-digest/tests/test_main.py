# -*- coding: utf-8 -*-
"""Entry regressions: research daily gates/state, source quotas and Saturday star.

Retired repost orchestration is not part of the current daily contract.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import main
from main import _daily_candidates, _star_title, run_daily, run_star


def _item(title, source, points=0):
    it = {"type": "news", "title": title, "url": f"https://x.com/{title}",
          "source": source, "author": "a", "published_at": None,
          "fetched_at": "2026-09-17T09:15:00", "summary": f"{title}的原文摘要内容。",
          "ai_summary": None, "confidence": None, "trending": None}
    if points:
        it["points"] = points
    return it


def _trending(rank=1, full="a/b"):
    return {"type": "trending", "title": full, "url": f"https://github.com/{full}",
            "source": "github-trending", "author": full.split("/")[0],
            "published_at": None, "fetched_at": "2026-09-17T09:15:00",
            "summary": "repo desc", "ai_summary": None, "confidence": None,
            "trending": {"rank": rank, "language": "Go", "stars": 100, "today_stars": 10}}


PICK = {"idx": 1, "why": "这篇值得读，跟数据库课设直接相关，实习面试也常考。" * 1,
        "digest": "深度导读内容。" * 30, "post_titles": ["显卡要变天？这 3 件事跟你有关"]}


class FakeStore:
    def __init__(self):
        self.logs = []
        self.posted = {}
        self.star_posted = {}
        self.titles = {}
        self.samples = []
        self.saved = []          # save_daily_v2 调用记录（dry-run 必须为空）

    def daily_exists(self, day):
        return False

    def get_daily_since(self, *a):
        return []

    def get_issue_no(self, day):
        return None

    def next_issue_no(self):
        return 1

    def is_daily_posted(self, day):
        return day in self.posted

    def mark_daily_posted(self, day, post_id):
        self.posted[day] = post_id

    def daily_post_id(self, day):
        return self.posted.get(day) or None

    def is_star_posted(self, day):
        return day in self.star_posted

    def mark_star_posted(self, day, post_id):
        self.star_posted[day] = post_id

    def recent_daily_titles(self, n, before_day=""):
        return []

    def save_daily_v2(self, day, items, md_path, post_title=""):
        self.saved.append((day, items, md_path, post_title))
        return 1

    def set_daily_title(self, day, title):
        self.titles[day] = title

    def daily_title(self, day):
        return self.titles.get(day, "")

    def add_view_sample(self, *a):
        self.samples.append(a)

    def log(self, *a):
        self.logs.append(a)

    def cleanup(self):
        pass

    def close(self):
        pass


def _run(tmp, day="2026-09-17", items=None, dry=True, result=None, has_ai=True):
    """Run the new pipeline contract; all generation/publication dependencies are offline."""
    from tests.test_main_reader import ready_result
    tmp = Path(tmp)
    result = ready_result() if result is None else result
    raw = items if items is not None else [ready_result()["item"]]
    captured = {}
    store = FakeStore()

    def pipeline(candidates, preference=None, pool=None, dry_run=False, recent_titles=None):
        captured.update(candidates=candidates, result=result, preference=preference,
                        dry_run=dry_run, recent_titles=recent_titles)
        return result

    with patch("main.Store", return_value=store), \
         patch("main.fetch_all", return_value=(raw, {})), \
         patch("main._pool_daily_materials", return_value=None), \
         patch("main._mark_pool_consumed"), \
         patch("main._sample_views"), \
         patch("main.should_skip", return_value=(False, "")), \
         patch("main.preference.load_preference", return_value=main.preference.Preference()), \
         patch("main.settings.sse_market_api_key", "offline-model" if has_ai else ""), \
         patch("main.settings.deepseek_api_key", ""), \
         patch("main.settings.output_dir", tmp), \
         patch("main.settings.data_dir", tmp), \
         patch("main.settings.market_refresh_token", ""), \
         patch("main.settings.market_user_telephone", ""), \
         patch("main.settings.publish_weekend", True), \
         patch("main.date") as fdate, \
         patch("main.editorial.run_pipeline", side_effect=pipeline), \
         patch("app.daily_ai.ai_pick", side_effect=AssertionError("legacy picker forbidden")), \
         patch("app.daily_ai.ai_translate", side_effect=AssertionError("legacy translation forbidden")), \
         patch("app.digest.render_repost", side_effect=AssertionError("legacy renderer forbidden")):
        fdate.today.return_value = date.fromisoformat(day)
        fdate.fromisoformat.side_effect = date.fromisoformat
        rc = run_daily(force=True, dry_run=dry)
    return rc, captured, store, tmp


class TestCandidatesOrder(unittest.TestCase):
    """v4.1 候选：知乎 → HN(points 降序) → sspai → devto(reactions 降序) → 订阅源轮转，上限 48（v4.5 加 3 源）。"""

    def test_order_and_points_sort(self):
        raw = [_item("HN 低分", "hacker-news", points=50),
               _item("HN 高分", "hacker-news", points=900),
               _item("知乎第一条", "zhihu-hot"),
               _item("知乎第二条", "zhihu-hot"),
               _item("少数派第一条", "sspai"),
               _item("devto 低赞", "devto", points=20),
               _item("devto 高赞", "devto", points=300),
               _item("Ars 深度", "ars-technica"),
               _item("阮一峰周刊", "ruanyifeng")]
        cands = _daily_candidates(raw)
        titles = [it["title"] for it in cands]
        self.assertEqual(titles[0], "知乎第一条")
        self.assertEqual(titles[1], "知乎第二条")
        self.assertEqual(titles[2], "HN 高分")       # points 降序
        self.assertEqual(titles[3], "HN 低分")
        self.assertEqual(titles[4], "少数派第一条")
        self.assertEqual(titles[5], "devto 高赞")    # reactions 降序
        self.assertEqual(titles[6], "devto 低赞")
        self.assertIn("Ars 深度", titles)
        self.assertIn("阮一峰周刊", titles)

    def test_cap_48_and_quotas(self):
        # 每源配额：知乎 12 + HN 8 + sspai 4 + devto 4 + 订阅源各 3；
        # 本例输入不含 v4.5 新源，40 条 = 配额绑定（< cap 48），不见截断。
        raw = ([_item(f"知乎{i}", "zhihu-hot") for i in range(30)]
               + [_item(f"HN{i}", "hacker-news", points=i) for i in range(20)]
               + [_item(f"sspai{i}", "sspai") for i in range(10)]
               + [_item(f"devto{i}", "devto", points=i) for i in range(10)]
               + [_item(f"ars{i}", "ars-technica") for i in range(8)]
               + [_item(f"ieee{i}", "ieee-spectrum") for i in range(8)]
               + [_item(f"fcc{i}", "freecodecamp") for i in range(8)]
               + [_item(f"rf{i}", "ruanyifeng") for i in range(8)])
        cands = _daily_candidates(raw)
        self.assertEqual(len(cands), 40)
        by_src = {}
        for it in cands:
            by_src[it["source"]] = by_src.get(it["source"], 0) + 1
        self.assertEqual(by_src, {"zhihu-hot": 12, "hacker-news": 8, "sspai": 4,
                                  "devto": 4, "ars-technica": 3, "ieee-spectrum": 3,
                                  "freecodecamp": 3, "ruanyifeng": 3})

    def test_wallstreetcn_quota(self):
        raw = [_item(f"见闻{i}", "wallstreetcn") for i in range(10)]
        cand = _daily_candidates(raw)
        self.assertEqual(sum(1 for c in cand if c["source"] == "wallstreetcn"), 3)


class TestDailyPickFlow(unittest.TestCase):
    """The daily entry consumes final reviewed strings, without legacy publication fallback."""

    def test_pipeline_final_selection_is_used(self):
        from tests.test_main_reader import ready_result
        result = ready_result()
        with tempfile.TemporaryDirectory() as tmp:
            rc, captured, _, _ = _run(tmp, result=result)
            body = (Path(tmp) / "2026-09-17-daily.md").read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIs(captured["result"], result)
        self.assertEqual(body, result["markdown"])

    def test_generation_failure_has_no_first_candidate_fallback(self):
        failed = {"status": "generation_error", "reasons": ["model_unavailable"],
                  "draft": None, "source_packet": None, "review": None, "attempts": []}
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp, result=failed, dry=False)
            self.assertFalse((Path(tmp) / "2026-09-17-daily.md").exists())
        self.assertEqual(rc, 1)
        self.assertEqual(store.saved, [])
        self.assertEqual(store.posted, {})

    def test_no_model_does_not_publish_an_original_summary(self):
        failed = {"status": "generation_error", "reasons": ["no_model"],
                  "draft": None, "source_packet": None, "review": None, "attempts": []}
        with tempfile.TemporaryDirectory() as tmp:
            rc, captured, store, _ = _run(tmp, result=failed, has_ai=False)
        self.assertEqual(rc, 1)
        self.assertEqual(len(captured["candidates"]), 1)
        self.assertEqual(store.saved, [])

    def test_recent_titles_and_dry_run_are_passed_before_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, captured, _, _ = _run(tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["recent_titles"], [])
        self.assertIs(captured["dry_run"], True)

    def test_dry_run_skips_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(store.saved, [])

    def test_real_run_saves_reviewed_title_in_snapshot(self):
        from tests.test_main_reader import ready_result
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp, dry=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0][0], "2026-09-17")
        self.assertEqual(store.saved[0][3], ready_result()["title"])


class TestGateWiring(unittest.TestCase):
    """The entry gate overrides a producer's ready claim before formal effects."""

    def test_low_review_does_not_save_formal_output(self):
        from tests.test_main_reader import ready_result
        result = ready_result()
        result["review"]["scores"]["depth"] = 6
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp, result=result, dry=False)
            self.assertFalse((Path(tmp) / "2026-09-17-daily.md").exists())
            self.assertTrue(list((Path(tmp) / "drafts").glob("*.json")))
        self.assertEqual(rc, 1)   # 质量拒稿=当天没发=契约失败（2026-10-15 rc 契约）
        self.assertEqual(store.saved, [])

    def test_changed_body_invalidates_prior_review(self):
        from tests.test_main_reader import ready_result
        result = ready_result()
        result["markdown"] += "review之后变更"
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp, result=result, dry=False)
        self.assertEqual(rc, 1)
        self.assertEqual(store.saved, [])
        self.assertTrue(any(d.get("detail") == "generation_error" for _, _, d in store.logs))

    def test_producer_quality_skip_cannot_be_overridden_by_valid_review(self):
        from tests.test_main_reader import ready_result
        result = ready_result()
        result["status"] = "skip_quality"
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp, result=result, dry=False)
        self.assertEqual(rc, 1)   # 生产者拒稿照旧不可越过；且 rc≠0（2026-10-15）
        self.assertEqual(store.saved, [])
        self.assertEqual(store.posted, {})


class TestPublishAllowed(unittest.TestCase):
    """日报：周末只抓取入库不发帖——2026-09-13 用户决策，v4 沿用。"""

    def test_weekday_allowed(self):
        for d in ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"):
            with patch("main.settings.publish_weekend", False):
                self.assertTrue(main._publish_allowed(d), d)

    def test_weekend_blocked(self):
        for d in ("2026-09-19", "2026-09-20"):
            with patch("main.settings.publish_weekend", False):
                self.assertFalse(main._publish_allowed(d), d)

    def test_bad_day_defaults_allow(self):
        with patch("main.settings.publish_weekend", False):
            self.assertTrue(main._publish_allowed("not-a-day"))


class TestSampleViews(unittest.TestCase):
    def test_records_samples(self):
        store = FakeStore()
        posts = [{"post_id": 6127, "day": "2026-09-17", "browse": 35, "likes": 1,
                  "comments": 2, "heat": 27.0, "title": "t"}]
        with patch("main.settings.market_user_telephone", "10000000000"), \
             patch("main.marketdb.user_id_of", return_value=1268), \
             patch("main.marketdb.posts_since", return_value=posts):
            main._sample_views(store)
        self.assertEqual(len(store.samples), 1)

    def test_query_failure_is_swallowed(self):
        store = FakeStore()
        with patch("main.settings.market_user_telephone", "10000000000"), \
             patch("main.marketdb.user_id_of", side_effect=RuntimeError("docker 没了")):
            main._sample_views(store)          # 不抛
        self.assertEqual(store.samples, [])


class TestStarTask(unittest.TestCase):
    """周六星榜专帖：全量 15 条、只在周六发、幂等、dry-run 零发布。"""

    def _run_star(self, day="2026-09-19", dry=False, posted=False, ai_desc=None):
        store = FakeStore()
        if posted:
            store.mark_star_posted(day, 1)
        raw = [_trending(i, f"user/repo{i}") for i in range(1, 16)]
        stats = {"github-trending": {"fetched": 15, "error": None}}
        captured = {}
        with patch("main.Store", return_value=store), \
             patch("main.fetch_all", return_value=(raw, stats)), \
             patch("main.should_skip", return_value=(False, "")), \
             patch("main._pool_star_trending", return_value=[]), \
             patch("main.settings.sse_market_api_key", "fake-key"), \
             patch("main.settings.deepseek_api_key", ""), \
             patch("main.settings.output_dir", Path(tempfile.mkdtemp())), \
             patch("main.settings.market_refresh_token", "rt"), \
             patch("main.settings.market_user_telephone", "10000000000"), \
             patch("main.settings.daily_publish", True), \
             patch("main.date") as fdate, \
             patch("main.daily_ai.ai_trending_desc", return_value=ai_desc or {}), \
             patch("main.render_star_post",
                   side_effect=lambda d, t, dm: captured.update(
                       {"trending": t, "desc": dm}) or "star md"), \
             patch("app.publisher.publish_md",
                   return_value=(6789, "/postdetail/6789", None)), \
             patch("main._verify_published"), \
             patch("app.publisher.refresh_access_token", return_value={"ok": 1}):
            fdate.today.return_value = date.fromisoformat(day)
            fdate.fromisoformat.side_effect = date.fromisoformat
            rc = run_star(force=True, dry_run=dry)
        return rc, store, captured

    def test_saturday_publishes_and_marks(self):
        rc, store, captured = self._run_star()
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured["trending"]), 15)
        self.assertEqual(store.star_posted.get("2026-09-19"), 6789)

    def test_weekday_skipped(self):
        rc, store, captured = self._run_star(day="2026-09-17")   # 周四
        self.assertEqual(rc, 0)
        self.assertNotIn("2026-09-17", store.star_posted)
        self.assertFalse(captured)                                # 连渲染都没跑

    def test_dry_run_no_publish(self):
        rc, store, _ = self._run_star(dry=True)
        self.assertEqual(rc, 0)
        self.assertEqual(store.star_posted, {})

    def test_title_format(self):
        self.assertEqual(_star_title("2026-09-19"), "9/19 GitHub 开源星榜 Top 15")


class TestPublishVerify(unittest.TestCase):
    """发帖后 is_private 回读（2026-09-20 星榜帖 #6311 被写私密事故的防线）。

    口径：观测动作，绝不影响发帖结果——任何失败只记 degraded/日志，绝不重发；
    private=True 是有意发私密（预览用），不回读。
    """

    DAY = "2026-10-01"  # 周四，工作日可发帖

    def _publish(self, post_id=None, is_private=False, verify_raise=False):
        store = FakeStore()
        priv = ({"side_effect": Exception("db boom")} if verify_raise
                else {"return_value": is_private})
        with patch("app.publisher.publish_md",
                   return_value=(post_id,
                                 f"/postdetail/{post_id}" if post_id else None,
                                 None)), \
             patch("main.settings.daily_publish", True), \
             patch("main.settings.market_refresh_token", "rt"), \
             patch("main.settings.market_user_telephone", "13800000000"), \
             patch("main.marketdb.user_id_of", return_value=7), \
             patch("main.marketdb.find_post_id", return_value=post_id or 6311), \
             patch("main.marketdb.post_is_private", **priv):
            main._publish_md(store, "daily", self.DAY, "测试标题", "# 正文",
                             dry_run=False)
        return store

    @staticmethod
    def _verify_logs(store):
        return [e for e in store.logs
                if len(e) > 2 and isinstance(e[2], dict)
                and e[2].get("detail") == "publish_verify"]

    def test_private_post_flagged(self):
        store = self._publish(post_id=6311, is_private=True)
        verify = self._verify_logs(store)
        self.assertEqual(len(verify), 1)
        self.assertEqual(verify[0][1], "degraded")
        self.assertIn("6311", verify[0][2].get("error", ""))

    def test_public_post_no_flag(self):
        store = self._publish(post_id=6311, is_private=False)
        self.assertEqual(self._verify_logs(store), [])

    def test_post_id_resolved_via_find_post_id(self):
        """后端成功不返回 postID（data=nil）→ 库中按标题+时间窗捞回再校验。"""
        store = self._publish(post_id=None, is_private=True)
        verify = self._verify_logs(store)
        self.assertEqual(len(verify), 1)
        self.assertIn("6311", verify[0][2].get("error", ""))

    def test_post_not_located_flagged(self):
        """定位不到帖子 = 无法确认公开 = 与被写私密同样危险，必须记 degraded。"""
        store = FakeStore()
        with patch("app.publisher.publish_md",
                   return_value=(None, None, None)), \
             patch("main.settings.daily_publish", True), \
             patch("main.settings.market_refresh_token", "rt"), \
             patch("main.settings.market_user_telephone", "13800000000"), \
             patch("main.marketdb.user_id_of", return_value=None):
            main._publish_md(store, "daily", self.DAY, "测试标题", "# 正文",
                             dry_run=False)
        verify = self._verify_logs(store)
        self.assertEqual(len(verify), 1)
        self.assertEqual(verify[0][1], "degraded")

    def test_db_down_no_flag_but_publish_ok(self):
        """库不可用（可见性未知）：只记 warning，不 degraded，发帖照常记 ok。"""
        store = FakeStore()
        with patch("app.publisher.publish_md",
                   return_value=(6311, "/postdetail/6311", None)), \
             patch("main.settings.daily_publish", True), \
             patch("main.settings.market_refresh_token", "rt"), \
             patch("main.settings.market_user_telephone", "13800000000"), \
             patch("main.marketdb.post_is_private", return_value=None):
            main._publish_md(store, "daily", self.DAY, "测试标题", "# 正文",
                             dry_run=False)
        self.assertEqual(self._verify_logs(store), [])
        self.assertTrue(any(e[1] == "ok" for e in store.logs))

    def test_verify_crash_swallowed_publish_still_ok(self):
        """校验自己崩了也不能弄崩已成功的发帖（观测不碍业务）。"""
        store = self._publish(post_id=6311, verify_raise=True)
        self.assertTrue(any(e[1] == "ok" for e in store.logs))

    def test_intentional_private_skips_verify(self):
        store = FakeStore()
        with patch("app.publisher.publish_md",
                   return_value=(6311, "/postdetail/6311", None)), \
             patch("main.settings.daily_publish", True), \
             patch("main.settings.market_refresh_token", "rt"), \
             patch("main.settings.market_user_telephone", "13800000000"), \
             patch("main.marketdb.post_is_private") as priv:
            main._publish_md(store, "daily", self.DAY, "测试标题", "# 正文",
                             dry_run=False, private=True)
        priv.assert_not_called()


class TestThinPoolTopup(unittest.TestCase):
    """薄池 live 补齐（2026-10-15）：去死源后健康候选 < 12 时合并即时抓取。"""

    def _news(self, url):
        return {"type": "news", "title": url, "url": url, "source": "sspai",
                "author": "", "published_at": None,
                "fetched_at": "2026-10-15T08:00:00", "summary": "摘要内容。",
                "ai_summary": None, "confidence": None, "trending": None}

    def test_thin_pool_merges_live_candidates(self):
        import tempfile
        from tests.test_main_reader import ready_result
        tmp = tempfile.mkdtemp()
        pool_cands = [self._news(f"https://pool.example/{i}") for i in range(3)]
        live_news = ([self._news(f"https://pool.example/{i}") for i in range(3)]   # 与池重复
                     + [dict(self._news(f"https://live.example/{i}"), source="hacker-news",
                             points=100 - i) for i in range(10)])
        captured = {}
        store = FakeStore()
        result = ready_result()

        def pipeline(candidates, preference=None, pool=None, dry_run=False, recent_titles=None):
            captured.update(candidates=candidates)
            return result

        with patch("main.Store", return_value=store), \
             patch("main._pool_daily_materials", return_value=(pool_cands, [])), \
             patch("main.fetch_all", return_value=(live_news, {})), \
             patch("main._mark_pool_consumed"), patch("main._sample_views"), \
             patch("main.should_skip", return_value=(False, "")), \
             patch("main.preference.load_preference", return_value=main.preference.Preference()), \
             patch("main.settings.sse_market_api_key", "offline-model"), \
             patch("main.settings.deepseek_api_key", ""), \
             patch("main.settings.output_dir", Path(tmp)), \
             patch("main.settings.data_dir", Path(tmp)), \
             patch("main.settings.market_refresh_token", ""), \
             patch("main.settings.market_user_telephone", ""), \
             patch("main.settings.publish_weekend", True), \
             patch("main.editorial.run_pipeline", side_effect=pipeline):
            rc = run_daily(force=True, dry_run=True)
        self.assertEqual(rc, 0)
        urls = [c["url"] for c in captured["candidates"]]
        self.assertEqual(urls[:3], [f"https://pool.example/{i}" for i in range(3)])  # 池内候选排前
        live_urls = [u for u in urls if u.startswith("https://live.example/")]
        self.assertEqual(len(live_urls), 8)      # HN 桶 [:8]；pool 重复 URL 已去重
        self.assertEqual(len(urls), 11)


class TestRcContract(unittest.TestCase):
    """rc 契约（2026-10-15）：当天未发布即 rc=1（产品契约=每天一篇）。

    豁免：dup（今日已发）、weekend 关、显式关/凭据缺失（运营态）、dry-run。
    收掉的假成功：skip_quality rc=0（10-15 静默停发事故）、发帖两连败 rc=0
    （INC-QD03 挂账）、负载熔断 rc=0。
    """

    def test_skip_quality_returns_nonzero(self):
        import tempfile
        from tests.test_main_reader import ready_result
        tmp = tempfile.mkdtemp()
        result = ready_result()
        result.update(status="skip_quality", reasons=["no_qualified_candidates"])
        rc, _, store, _ = _run(tmp, result=result)
        self.assertEqual(rc, 1)

    def test_load_skip_returns_nonzero(self):
        store = FakeStore()
        with patch("main.Store", return_value=store), \
             patch("main.should_skip", return_value=(True, "load 9.9")):
            rc = run_daily(force=True, dry_run=False)
        self.assertEqual(rc, 1)

    def test_publish_failure_returns_nonzero(self):
        import tempfile
        from tests.test_main_reader import ready_result
        tmp = tempfile.mkdtemp()
        result = ready_result()
        store = FakeStore()
        raw = [result["item"]]

        def pipeline(candidates, preference=None, pool=None, dry_run=False, recent_titles=None):
            return result

        with patch("main.Store", return_value=store), \
             patch("main.fetch_all", return_value=(raw, {})), \
             patch("main._pool_daily_materials", return_value=None), \
             patch("main._mark_pool_consumed"), patch("main._sample_views"), \
             patch("main.should_skip", return_value=(False, "")), \
             patch("main.preference.load_preference", return_value=main.preference.Preference()), \
             patch("main.settings.output_dir", Path(tmp)), \
             patch("main.settings.data_dir", Path(tmp)), \
             patch("main.settings.market_refresh_token", "tok"), \
             patch("main.settings.market_user_telephone", "13800000000"), \
             patch("main.settings.publish_weekend", True), \
             patch("main.editorial.run_pipeline", side_effect=pipeline), \
             patch("app.publisher.refresh_access_token", return_value=("a", "b")), \
             patch("app.publisher.publish_md", return_value=(None, None, "HTTP 500")):
            rc = run_daily(force=True, dry_run=False)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
