# -*- coding: utf-8 -*-
"""组装层回归测试：v4 每天一篇爆文（挑 1 → 抓全文/降级导读 → 渲染 → 发帖）。

沿用的历史教训（测试仍在守）：
  · 2026-08-30 索引错位：AI 输出的是「候选顺序」的 idx，取条目必须按候选反查；
  · 2026-09-13 标题校验阶梯 + 周末只抓取不发帖；
  · 2026-09-14 --dry-run 零写入（曾写库递增 issue_next 害停更一天）。
v4 新增：候选排序（知乎→HN points 降序→sspai）、_fetch_body 降级链、周六星榜任务。
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import main
from main import (_daily_candidates, _fallback_title, _fetch_body, _pick_post_title,
                  _star_title, run_daily, run_star)


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


def _run(tmp, day="2026-09-17", pick=PICK, items=None, dry=True,
         body="正文全文。", mode="full", has_ai=True, gate=None, **kw):
    """跑一次 run_daily；返回 (rc, ctx, store, tmp)。dry=False 走真跑路径（凭据置空，不会发帖）。
    gate: _prefetch_gate 的桩返回值（None → []，即回退全量候选池的老行为）。"""
    tmp = Path(tmp)
    raw = items if items is not None else [
        _item("知乎爆文第一条", "zhihu-hot"),
        _item("HN 爆文第二条", "hacker-news", points=300)]
    stats = {"zhihu-hot": {"fetched": 1, "error": None},
             "hacker-news": {"fetched": 1, "error": None}}
    captured = {}
    store = FakeStore()

    def fake_render(day_, issue_no, ctx):
        captured["ctx"] = ctx
        return "md"

    with patch("main.Store", return_value=store), \
         patch("main.fetch_all", return_value=(raw, stats)), \
         patch("main._pool_daily_materials", return_value=None), \
         patch("main._mark_pool_consumed") as _mpc, \
         patch("main.settings.sse_market_api_key", "fake-key" if has_ai else ""), \
         patch("main.settings.deepseek_api_key", ""), \
         patch("main.settings.output_dir", tmp), \
         patch("main.settings.market_refresh_token", ""), \
         patch("main.settings.market_user_telephone", ""), \
         patch("main.settings.publish_weekend", True), \
         patch("main.date") as fdate, \
         patch("main._prefetch_gate", return_value=gate or []), \
         patch("main.daily_ai.ai_pick", return_value=pick), \
         patch("main.daily_ai.ai_glossary",
               return_value=[{"term": "查询计划（query plan）",
                              "expl": "数据库把 SQL 翻译成一步步执行方案的计划。"}]), \
         patch("main._fetch_body", return_value=(body, mode, "")), \
         patch("main.render_repost", side_effect=fake_render):
        fdate.today.return_value = date.fromisoformat(day)
        fdate.fromisoformat.side_effect = date.fromisoformat
        rc = run_daily(force=True, dry_run=dry)
    return rc, captured.get("ctx"), store, tmp


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


class TestPrefetchGate(unittest.TestCase):
    """可爬性预取闸门（2026-10-02）：ai_pick 只在「验证过能抓到全文」的候选里挑。

    起因：挑选发生在抓取之前，AI 只看标题+摘要+热度定生死——通稿/标题党与
    硬核文在这个信号集里无法区分，选完才发现抓不到全文（知乎 403/SPA 空壳），
    降级 AI 导读后读者收获只剩转述。
    """

    def _gate_run(self, items, fetch_map, mirror_map=None):
        """fetch_map: url → 正文字数（None=抛异常）；mirror_map: url → art|异常。"""
        calls = []

        def fake_fetch(url, **kw):
            calls.append(url)
            spec = fetch_map[url]
            if spec is None:
                raise RuntimeError("403")
            return {"text": "字" * spec, "author": "作者甲"}

        def fake_mirror(url):
            r = (mirror_map or {})[url]
            if isinstance(r, Exception):
                raise r
            return r

        with patch("main.extract.fetch_article", side_effect=fake_fetch), \
             patch("main.mirror.mirror_fetch", side_effect=fake_mirror), \
             patch("main.mirror.needs_mirror",
                   side_effect=lambda u: "见闻" in u):
            return main._prefetch_gate(items), calls

    def test_qualifies_and_stashes_on_copy(self):
        items = [_item("HN 硬核文", "hacker-news", points=200)]
        qualified, calls = self._gate_run(items, {"https://x.com/HN 硬核文": 2000})
        self.assertEqual(calls, ["https://x.com/HN 硬核文"])
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["body_len"], 2000)
        self.assertEqual(qualified[0]["_art"]["author"], "作者甲")
        self.assertTrue(qualified[0]["excerpt"])
        # 拷贝上挂缓存，原 news 条目绝不被污染（它们会被 save_daily_v2 序列化进库）
        self.assertNotIn("_art", items[0])
        self.assertNotIn("body_len", items[0])
        self.assertNotIn("excerpt", items[0])

    def test_short_body_rejected(self):
        """正文过短 = 通稿/短讯，不进挑选池（可读性下限）。"""
        items = [_item("薄通稿", "sspai")]
        qualified, _ = self._gate_run(items, {"https://x.com/薄通稿": main.GATE_MIN_CHARS - 1})
        self.assertEqual(qualified, [])

    def test_round_robin_across_sources(self):
        """源间轮转（HN→知乎→sspai→devto…），源内热度降序——防单源刷屏挤掉订阅源。"""
        items = [_item("HN1", "hacker-news", points=100),
                 _item("HN2", "hacker-news", points=90),
                 _item("知1", "zhihu-hot"), _item("知2", "zhihu-hot"),
                 _item("少1", "sspai"), _item("d1", "devto", points=5)]
        fetch_map = {it["url"]: None for it in items}
        qualified, calls = self._gate_run(items, fetch_map)
        self.assertEqual(qualified, [])
        self.assertEqual(calls, ["https://x.com/HN1", "https://x.com/知1",
                                 "https://x.com/少1", "https://x.com/d1",
                                 "https://x.com/HN2", "https://x.com/知2"])

    def test_attempt_cap(self):
        """试抓上限护栏：候选再多也不许超 GATE_MAX_ATTEMPTS 次请求。"""
        items = [_item(f"HN{i}", "hacker-news", points=100 - i) for i in range(20)]
        fetch_map = {it["url"]: None for it in items}
        _, calls = self._gate_run(items, fetch_map)
        self.assertEqual(len(calls), main.GATE_MAX_ATTEMPTS)

    def test_early_stop_at_qualify_cap(self):
        """凑满 GATE_QUALIFY 篇合格即停，不白抓。"""
        items = [_item(f"硬核{i}", "hacker-news", points=100 - i) for i in range(8)]
        fetch_map = {it["url"]: 2000 for it in items}
        qualified, calls = self._gate_run(items, fetch_map)
        self.assertEqual(len(qualified), main.GATE_QUALIFY)
        self.assertEqual(len(calls), main.GATE_QUALIFY)

    def test_fetch_error_skipped_not_raised(self):
        items = [_item("炸的", "sspai"), _item("好的", "hacker-news", points=1)]
        fetch_map = {"https://x.com/炸的": None, "https://x.com/好的": 2000}
        qualified, calls = self._gate_run(items, fetch_map)
        self.assertEqual([q["title"] for q in qualified], ["好的"])
        self.assertEqual(len(calls), 2)

    def test_wallstreetcn_mirror_fallback(self):
        items = [_item("见闻深度", "wallstreetcn")]
        qualified, _ = self._gate_run(
            items, {"https://x.com/见闻深度": None},
            mirror_map={"https://x.com/见闻深度": {"text": "字" * 2000, "author": "乙"}})
        self.assertEqual([q["title"] for q in qualified], ["见闻深度"])
        self.assertEqual(qualified[0]["_art"]["author"], "乙")

    def test_mirror_failure_not_qualified(self):
        items = [_item("见闻深度", "wallstreetcn")]
        qualified, _ = self._gate_run(
            items, {"https://x.com/见闻深度": None},
            mirror_map={"https://x.com/见闻深度": RuntimeError("net")})
        self.assertEqual(qualified, [])


class TestDailyPickFlow(unittest.TestCase):
    def test_pick_item_by_candidate_index(self):
        """AI 的 idx 是「候选顺序」——必须按候选反查条目（2026-08-30 错位教训）。"""
        with tempfile.TemporaryDirectory() as tmp:
            rc, ctx, _, _ = _run(tmp, pick={**PICK, "idx": 2})
        self.assertEqual(rc, 0)
        self.assertEqual(ctx["pick"]["item"]["title"], "HN 爆文第二条")
        self.assertEqual(ctx["pick"]["lead_kind"], "full")
        self.assertEqual(ctx["pick"]["lead"], "正文全文。")

    def test_ai_fail_falls_to_first_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, ctx, store, _ = _run(tmp, pick=None, body="", mode="digest")
        self.assertEqual(rc, 0)
        self.assertEqual(ctx["pick"]["item"]["title"], "知乎爆文第一条")  # 候选首条
        self.assertFalse(ctx["pick"]["why"])
        # 降级必须能从 run_log 看出来
        daily_logs = [d for k, s, d in store.logs
                      if k == "daily" and isinstance(d, dict) and "ai_used" in d]
        self.assertTrue(daily_logs and daily_logs[0]["ai_used"] is False)

    def test_no_ai_key_uses_summary_as_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, ctx, _, _ = _run(tmp, pick=None, body="", mode="digest", has_ai=False)
        self.assertEqual(rc, 0)
        # AI 完全没参与：用原文摘要顶上，标 summary（渲染时明示「不是全文」）
        self.assertIn("原文摘要内容", ctx["pick"]["lead"])
        self.assertEqual(ctx["pick"]["lead_kind"], "summary")

    def test_glossary_passthrough_and_no_ai_empty(self):
        """有 AI → 知识卡片进 ctx；无 AI → 空列表（不发卡）。"""
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx, _, _ = _run(tmp)
        self.assertEqual(ctx["pick"]["glossary"][0]["term"], "查询计划（query plan）")
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx, _, _ = _run(tmp, has_ai=False)
        self.assertEqual(ctx["pick"]["glossary"], [])

    def test_dry_run_skips_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(store.saved, [])

    def test_real_run_saves_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, store, _ = _run(tmp, dry=False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0][0], "2026-09-17")


class TestGateWiring(unittest.TestCase):
    """run_daily 接线：ai_pick 的候选池 = 闸门过的池；_fallback_title 同池（idx 对齐）。"""

    def _run_gated(self, gate, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            rc, ctx, store, _ = _run(tmp, gate=gate, **kw)
            return rc, ctx, store

    def test_pick_pool_is_gated(self):
        gated = [_item("闸门过的硬核文", "hacker-news", points=42)]
        rc, ctx, store = self._run_gated(gated)
        self.assertEqual(rc, 0)
        self.assertEqual(ctx["pick"]["item"]["title"], "闸门过的硬核文")
        daily_logs = [d for k, s, d in store.logs
                      if k == "daily" and isinstance(d, dict) and "ai_used" in d]
        self.assertEqual(daily_logs[0]["gate_qualified"], 1)

    def test_ai_fallback_takes_gated_first(self):
        """AI 失败降级首条候选时，取闸门池首条（可抓全文），不是全量池首条。"""
        gated = [_item("闸门过的硬核文", "hacker-news", points=42)]
        rc, ctx, _ = self._run_gated(gated, pick=None)
        self.assertEqual(rc, 0)
        self.assertEqual(ctx["pick"]["item"]["title"], "闸门过的硬核文")

    def test_gate_run_logged_in_detail(self):
        rc, _, store = self._run_gated([])
        self.assertEqual(rc, 0)
        daily_logs = [d for k, s, d in store.logs
                      if k == "daily" and isinstance(d, dict) and "ai_used" in d]
        self.assertEqual(daily_logs[0]["gate_qualified"], 0)


class TestFetchBody(unittest.TestCase):
    """全文抓取降级链：中文直用 → 英文中译 → 拿不到全文走导读。任一层失败不抛。"""

    def test_chinese_used_directly(self):
        with patch("main.extract.fetch_article",
                   return_value={"text": "这是中文全文。" * 100, "author": "作者甲"}), \
             patch("main.daily_ai.ai_translate") as tr:
            body, mode, author = _fetch_body(_item("t", "sspai"), has_ai=True)
        self.assertEqual(mode, "full")
        self.assertEqual(author, "作者甲")
        tr.assert_not_called()

    def test_english_translated(self):
        with patch("main.extract.fetch_article",
                   return_value={"text": "An English article " * 100, "author": ""}), \
             patch("main.daily_ai.ai_translate", return_value="中文译文"):
            body, mode, _ = _fetch_body(_item("t", "hacker-news"), has_ai=True)
        self.assertEqual((body, mode), ("中文译文", "full"))

    def test_gated_cache_skips_refetch(self):
        """闸门已抓到全文（_art 缓存）→ 直接用，不再发请求（全文只抓一次）。"""
        it = _item("闸门过的", "hacker-news", points=10)
        it["_art"] = {"text": "这是一篇中文全文，内容足够扎实可读。", "author": "丙"}
        with patch("main.extract.fetch_article") as fa, \
             patch("main.mirror.mirror_fetch") as mf:
            body, mode, author = _fetch_body(it, has_ai=False)
        fa.assert_not_called()
        mf.assert_not_called()
        self.assertEqual((mode, author), ("full", "丙"))
        self.assertIn("中文全文", body)

    def test_english_translate_fail_falls_to_digest(self):
        with patch("main.extract.fetch_article",
                   return_value={"text": "An English article " * 100, "author": ""}), \
             patch("main.daily_ai.ai_translate", return_value=None):
            body, mode, _ = _fetch_body(_item("t", "hacker-news"), has_ai=True)
        self.assertEqual((body, mode), ("", "digest"))

    def test_unfetchable_falls_to_digest(self):
        with patch("main.extract.fetch_article", return_value=None):
            body, mode, _ = _fetch_body(_item("t", "zhihu-hot"), has_ai=True)
        self.assertEqual((body, mode), ("", "digest"))

    def test_fetch_raise_swallowed(self):
        with patch("main.extract.fetch_article", side_effect=RuntimeError("403")):
            body, mode, _ = _fetch_body(_item("t", "zhihu-hot"), has_ai=False)
        self.assertEqual((body, mode), ("", "digest"))

    def test_wallstreetcn_uses_mirror(self):
        """wallstreetcn 文章页是 SPA：extract 返回 None → 镜像通道拿全文。"""
        it = _item("见闻深度", "wallstreetcn")
        it["url"] = "https://wallstreetcn.com/articles/3781850"
        with patch("main.extract.fetch_article", return_value=None), \
             patch("main.mirror.mirror_fetch",
                   return_value={"text": "镜像全文。" * 200, "author": "刘胜与"}) as mf:
            body, mode, author = _fetch_body(it, has_ai=True)
        self.assertEqual((mode, author), ("full", "刘胜与"))
        self.assertIn("镜像全文", body)
        mf.assert_called_once_with("https://wallstreetcn.com/articles/3781850")

    def test_mirror_fail_falls_to_digest(self):
        it = _item("见闻深度", "wallstreetcn")
        it["url"] = "https://wallstreetcn.com/articles/1"
        with patch("main.extract.fetch_article", return_value=None), \
             patch("main.mirror.mirror_fetch", side_effect=RuntimeError("net")):
            body, mode, _ = _fetch_body(it, has_ai=True)
        self.assertEqual((body, mode), ("", "digest"))

    def test_non_mirror_url_skips_mirror(self):
        """知乎 403 面不走镜像（无搜索式发现），维持 digest 降级。"""
        it = _item("知乎文", "zhihu-hot")
        with patch("main.extract.fetch_article", return_value=None), \
             patch("main.mirror.mirror_fetch") as mf:
            body, mode, _ = _fetch_body(it, has_ai=True)
        self.assertEqual((body, mode), ("", "digest"))
        mf.assert_not_called()


class TestPickPostTitle(unittest.TestCase):
    """发帖标题走 titles.pick：AI 候选 → 合规优先 → 兜底。"""

    def test_uses_compliant_candidate(self):
        ai = {"post_titles": ["显卡要变天？这 3 件事跟你有关", "备选二"]}
        self.assertEqual(_pick_post_title(ai, [], "兜底头条"),
                         "显卡要变天？这 3 件事跟你有关")

    def test_skips_banned_and_falls_through(self):
        ai = {"post_titles": ["今日速览", "秋招风向变了"]}
        self.assertEqual(_pick_post_title(ai, [], "兜底头条"), "秋招风向变了")

    def test_all_banned_falls_back(self):
        ai = {"post_titles": ["今日速览", "技术动态一览"]}
        self.assertEqual(_pick_post_title(ai, [], "兜底头条"), "兜底头条")

    def test_no_ai_uses_fallback(self):
        self.assertEqual(_pick_post_title(None, [], "兜底头条"), "兜底头条")

    def test_fallback_uses_picked_title(self):
        cands = [_item("存款利率下调，市场会有哪些影响？", "zhihu-hot")]
        t = _fallback_title({"idx": 1}, cands)
        self.assertIn("存款利率下调", t)

    def test_fallback_no_pick_uses_first(self):
        cands = [_item("HN Some Story", "hacker-news")]
        self.assertTrue(_fallback_title(None, cands))


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


class TestPickArticlePreference(unittest.TestCase):
    """倾向透传：_pick_article 必须把 preference 原样交给 ai_pick。"""

    def test_preference_passed_to_ai_pick(self):
        from app import daily_ai, preference
        captured = {}

        def fake_pick(candidates, day, preference=None):
            captured["preference"] = preference
            return None

        pref = preference.Preference(mode="prefer", text="jev 相关内容优先")
        with patch.object(daily_ai, "ai_pick", fake_pick), \
             patch("main.settings.sse_market_api_key", "fake-key"):
            main._pick_article([{"title": "T", "source": "hacker-news"}],
                               "2026-09-22", pref)
        self.assertIs(captured["preference"], pref)

    def test_none_preference_still_calls_ai_pick(self):
        from app import daily_ai
        captured = {}

        def fake_pick(candidates, day, preference=None):
            captured["preference"] = preference
            return None

        with patch.object(daily_ai, "ai_pick", fake_pick), \
             patch("main.settings.sse_market_api_key", "fake-key"):
            main._pick_article([{"title": "T", "source": "hacker-news"}],
                               "2026-09-22", None)
        self.assertIsNone(captured["preference"])


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


if __name__ == "__main__":
    unittest.main()
