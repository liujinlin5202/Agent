# -*- coding: utf-8 -*-
"""编辑部流水线单测（M2）：评委/研究员/作者/终审 + 打回链 + 沉淀 + 断点续跑 + 回退。

对应 M2 决策留底 D2-D8。llm 调用全 mock，不烧真 token；
sanitize 护栏跑真实实现（daily_ai.sanitize_pick），守的是线上同一套阶梯。
"""
import tempfile
import gc
import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import editorial
from app.pool import PoolStore


def _cand(title, source="hacker-news", points=100):
    """池候选形状：v2 条目 + _pool_url/_pool_score（top_candidates 的产物）。"""
    url = f"https://x.com/{title}"
    it = {"type": "news", "title": title, "url": url, "source": source,
          "author": "a", "published_at": None,
          "fetched_at": "2026-10-08T07:07:00",
          "summary": f"{title} 的原文摘要，含具体技术内容与数字。",
          "ai_summary": None, "confidence": None, "trending": None,
          "points": points, "_pool_url": url, "_pool_score": 150.0}
    return it


def _judge_json(*titles, score=8):
    rows = [{"title": t, "novelty": score, "depth": score, "utility": score,
             "credibility": score, "audience": score} for t in titles]
    return json.dumps({"scores": rows}, ensure_ascii=False)


def _judge_mixed(scores: dict[str, int]) -> str:
    """不同条目不同分：{title: score}。"""
    rows = [{"title": t, "novelty": s, "depth": s, "utility": s,
             "credibility": s, "audience": s} for t, s in scores.items()]
    return json.dumps({"scores": rows}, ensure_ascii=False)


def _author_json(title, why=60, digest=120):
    # why/digest 必须与标题+摘要词元重叠——跑的是真实 sanitize 护栏（内容相关性校验）
    return json.dumps({
        "title": title,
        "why": f"《{title}》的原文摘要里有具体技术内容与数字，跟数据库课设直接相关，"
               "实习面试常考，值得今天读。" * max(1, why // 40),
        "digest": f"《{title}》讲清了查询计划的生成机制与优化器实现，"
                  "原文摘要里的具体技术内容与数字都有出处，含代码。" * max(1, digest // 44),
        "post_titles": ["这个数据库细节，面试官最爱问"],
    }, ensure_ascii=False)


def _review_json(score=8, comments="很好，保持。"):
    return json.dumps({d: score for d in editorial._DIMS} | {"comments": comments},
                      ensure_ascii=False)


class EditorialTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.datadir = Path(self.tmp.name)
        self.pool = PoolStore(self.datadir / "tech-digest.db")
        self.patches = [
            patch("app.config.settings.data_dir", self.datadir),
            patch("app.pool.settings.data_dir", self.datadir),
            patch("app.editorial.settings.sse_market_api_key", "fake-key"),
            patch("app.editorial.settings.deepseek_api_key", ""),
            patch("app.editorial.daily_ai.ai_glossary", return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.pool.close()
        gc.collect()          # Windows 临时目录文件锁卫生（同 test_ingest）
        self.tmp.cleanup()


class TestJudge(EditorialTestBase):
    def test_scores_bound_by_title_and_saved(self):
        """标题精确绑定：绑上的落库，绑不上的弃评（D2）。"""
        for t in ("好的硬核文", "标题写错的文"):
            self.pool.upsert_news([_cand(t)])
        good = _cand("好的硬核文")

        def fake_map(tasks, **kw):
            return [_judge_json("好的硬核文")]      # 只评了一条，另一条绑定失败

        with patch.object(editorial.llmpool, "map_chat", side_effect=fake_map):
            out = editorial.judge_score([good, _cand("标题写错的文")], self.pool)
        self.assertIn("https://x.com/好的硬核文", out)
        self.assertNotIn("https://x.com/标题写错的文", out)
        self.assertEqual(out["https://x.com/好的硬核文"]["total"], 0.8)
        row = self.pool.get_item("https://x.com/好的硬核文")
        self.assertEqual(row["score"], 0.8)
        detail = json.loads(row["score_detail"])
        self.assertEqual(detail["kind"], "judge")
        self.assertEqual(detail["date"], editorial._today())

    def test_resume_reuses_today_scores(self):
        """断点续跑（D6）：当日已评条目不再烧 LLM。"""
        self.pool.upsert_news([_cand("已评过的文")])
        self.pool.save_stage("https://x.com/已评过的文", "score_detail", {
            "kind": "judge", "date": editorial._today(), "total": 0.9,
            "dims": {"novelty": 9}})
        with patch.object(editorial.llmpool, "map_chat") as m:
            out = editorial.judge_score([_cand("已评过的文")], self.pool)
        m.assert_not_called()
        self.assertEqual(out["https://x.com/已评过的文"]["total"], 0.9)

    def test_stale_scores_not_reused(self):
        """跨天产物不复用（评分时效性，D6）。"""
        self.pool.upsert_news([_cand("昨日评过的文")])
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.pool.save_stage("https://x.com/昨日评过的文", "score_detail", {
            "kind": "judge", "date": yesterday, "total": 0.1, "dims": {}})
        with patch.object(editorial.llmpool, "map_chat",
                          return_value=[_judge_json("昨日评过的文", score=9)]):
            out = editorial.judge_score([_cand("昨日评过的文")], self.pool)
        self.assertEqual(out["https://x.com/昨日评过的文"]["total"], 0.9)


class TestResearch(EditorialTestBase):
    def test_fulltext_success_saves_and_caches_art(self):
        self.pool.upsert_news([_cand("有全文的硬核文")])
        art = {"text": "正文内容足够长。" * 200, "author": "作者甲"}
        with patch.object(editorial.extract, "fetch_with_mirror", return_value=art), \
             patch.object(editorial.llmpool, "map_chat",
                          return_value=[json.dumps(
                              {"key_points": ["要点一 42ms", "要点二"], "quotes": ["引句"]},
                              ensure_ascii=False)]):
            research, arts = editorial.deep_research([_cand("有全文的硬核文")], self.pool)
        r = research["https://x.com/有全文的硬核文"]
        self.assertTrue(r["fulltext"])
        self.assertEqual(len(r["key_points"]), 2)
        self.assertIn("https://x.com/有全文的硬核文", arts)   # 全文缓存给正文步复用
        row = self.pool.get_item("https://x.com/有全文的硬核文")
        self.assertIsNotNone(json.loads(row["research"]))

    def test_fetch_fail_open_and_llm_garbage_open(self):
        """抓取失败/解析失败 → research=None，条目继续流转（§5.5 raw_summary 兜底）。"""
        with patch.object(editorial.extract, "fetch_with_mirror",
                          side_effect=lambda it: None), \
             patch.object(editorial.llmpool, "map_chat",
                          return_value=["不是 json"]):
            research, arts = editorial.deep_research([_cand("抓不到的文")], self.pool)
        self.assertIsNone(research["https://x.com/抓不到的文"])
        self.assertEqual(arts, {})


class TestReview(EditorialTestBase):
    def test_parse_and_weighted_total(self):
        pick = {"why": "编者按", "lead": "正文" * 100, "lead_kind": "full"}
        with patch.object(editorial.llmpool, "map_chat",
                          return_value=[_review_json(score=8)]):
            r = editorial.review_draft(_cand("某文"), pick, None)
        self.assertEqual(r["total"], 0.8)     # 五维全 8 → 8/10*1.0
        self.assertEqual(r["comments"], "很好，保持。")

    def test_garbage_returns_none(self):
        with patch.object(editorial.llmpool, "map_chat", return_value=["垃圾"]):
            self.assertIsNone(editorial.review_draft(
                _cand("某文"), {"why": "w", "lead": "l", "lead_kind": "full"}, None))


class TestPipeline(EditorialTestBase):
    def _seed(self, titles):
        self.pool.upsert_news([_cand(t) for t in titles])
        return [_cand(t) for t in titles]

    def test_happy_path_publishes_with_fulltext(self):
        cands = self._seed(["甲文", "乙文", "丙文"])
        # map_chat 调用序：评委 1 批 → 研究员 1 批 → 作者 → 终审
        with patch.object(editorial.llmpool, "map_chat") as m, \
             patch.object(editorial.extract, "fetch_with_mirror",
                          return_value={"text": "中文正文内容。" * 300,
                                        "author": "作者甲"}):
            m.side_effect = [
                [_judge_json("甲文", "乙文", "丙文", score=8)],           # 评委
                [json.dumps({"key_points": ["要点"], "quotes": []},
                            ensure_ascii=False)] * 3,                     # 研究员
                [_author_json("甲文")],                                    # 作者
                [_review_json(score=9)],                                   # 终审
            ]
            out = editorial.run_pipeline(cands, pool=self.pool)
        self.assertIsNotNone(out)
        pick = out["pick"]
        self.assertEqual(pick["item"]["title"], "甲文")
        self.assertEqual(pick["lead_kind"], "full")     # 中文全文直用
        self.assertEqual(pick["author"], "作者甲")
        self.assertIsNotNone(out["review"])
        row = self.pool.get_item("https://x.com/甲文")
        self.assertEqual(json.loads(row["review"])["total"], 0.9)   # 轨迹落库

    def test_low_score_sends_back_once_and_republishes(self):
        """<0.7 打回一轮：作者改稿 → 复审；复审后无论分数照发（D5）。"""
        cands = self._seed(["甲文", "乙文"])
        with patch.object(editorial.llmpool, "map_chat") as m, \
             patch.object(editorial.extract, "fetch_with_mirror",
                          side_effect=lambda it: None), \
             patch.object(editorial.settings, "review_threshold", 0.7):
            m.side_effect = [
                [_judge_json("甲文", "乙文", score=8)],
                ["不是 json", "不是 json"],                    # 研究员全失败（降级摘要）
                [_author_json("甲文")],                        # 首稿
                [_review_json(score=5, comments="编者按太空，补数字")],   # 终审 0.5
                [_author_json("乙文")],                        # 打回重写
                [_review_json(score=6, comments="可以")],      # 复审 0.6（仍低于线）
            ]
            out = editorial.run_pipeline(cands, pool=self.pool)
        self.assertIsNotNone(out)                              # 复审 <0.7 照发
        self.assertEqual(out["pick"]["item"]["title"], "乙文")
        self.assertEqual(out["review"]["total"], 0.6)

    def test_reserve_marks_unselected_high_scores(self):
        """落选且 ≥0.8 → reserved；<0.8 的保持 candidate（D7）。"""
        cands = self._seed(["甲文", "高分落选文", "低分落选文"])
        with patch.object(editorial.llmpool, "map_chat") as m, \
             patch.object(editorial.extract, "fetch_with_mirror",
                          side_effect=lambda it: None), \
             patch.object(editorial.settings, "reserve_score", 0.8):
            m.side_effect = [
                [_judge_mixed({"甲文": 9, "高分落选文": 9, "低分落选文": 5})],
                ["不是 json"] * 3,
                [_author_json("甲文")],
                [_review_json(score=9)],
            ]
            editorial.run_pipeline(cands, pool=self.pool)
        self.assertEqual(self.pool.get_item("https://x.com/高分落选文")["status"],
                         "reserved")
        self.assertEqual(self.pool.get_item("https://x.com/低分落选文")["status"],
                         "candidate")

    def test_judge_wipeout_returns_none(self):
        """评委全灭 → None（调用方回退 M1 路径，D8）。"""
        cands = self._seed(["甲文", "乙文"])
        with patch.object(editorial.llmpool, "map_chat",
                          return_value=["完全不是 json"]):
            self.assertIsNone(editorial.run_pipeline(cands, pool=self.pool))

    def test_no_ai_key_returns_none(self):
        cands = self._seed(["甲文"])
        with patch.object(editorial.settings, "sse_market_api_key", ""), \
             patch.object(editorial.settings, "deepseek_api_key", ""):
            self.assertIsNone(editorial.run_pipeline(cands, pool=self.pool))

    def test_pipeline_short_circuit_when_author_blocked(self):
        """作者被护栏全拦 → None（回退 M1）。"""
        cands = self._seed(["甲文", "乙文"])
        with patch.object(editorial.llmpool, "map_chat") as m, \
             patch.object(editorial.extract, "fetch_with_mirror",
                          side_effect=lambda it: None):
            m.side_effect = [
                [_judge_json("甲文", "乙文", score=8)],
                ["不是 json"] * 2,
                ['{"title": "根本不存在标题", "why": "x" * 50, "digest": "",'
                 ' "post_titles": []}'],               # 标题绑定失败 → None
            ]
            self.assertIsNone(editorial.run_pipeline(cands, pool=self.pool))

    def test_dry_run_writes_nothing(self):
        """dry-run 完整跑链路但零落库（M1 D10 契约在流水线上的延续）。"""
        cands = self._seed(["甲文", "乙文"])
        with patch.object(editorial.llmpool, "map_chat") as m, \
             patch.object(editorial.extract, "fetch_with_mirror",
                          return_value={"text": "中文正文。" * 300, "author": "a"}):
            m.side_effect = [
                [_judge_json("甲文", "乙文", score=9)],
                [json.dumps({"key_points": ["要点"], "quotes": []},
                            ensure_ascii=False)] * 2,
                [_author_json("甲文")],
                [_review_json(score=9)],
            ]
            out = editorial.run_pipeline(cands, pool=self.pool, dry_run=True)
        self.assertIsNotNone(out)                       # 链路照常跑通
        for t in ("甲文", "乙文"):
            row = self.pool.get_item(f"https://x.com/{t}")
            self.assertIsNone(row["score"])
            self.assertIsNone(row["score_detail"])
            self.assertIsNone(row["research"])
            self.assertIsNone(row["review"])
            self.assertEqual(row["status"], "candidate")  # reserved 也不标


class TestDailyWiring(unittest.TestCase):
    """run_daily 接线（D8）：池模式有 key → 流水线；流水线失能 → M1 路径。"""

    def _run_daily(self, editorial_return):
        import main
        import test_main                    # discover 以顶层模块名导入 tests/
        store = test_main.FakeStore()
        captured = {}
        with patch("main._pool_daily_materials",
                   return_value=([_cand("池内甲文"), _cand("池内乙文")], [])), \
             patch("main.Store", return_value=store), \
             patch("main.settings.sse_market_api_key", "fake-key"), \
             patch("main.settings.deepseek_api_key", ""), \
             patch("main.settings.output_dir", Path(tempfile.mkdtemp())), \
             patch("main.settings.market_refresh_token", ""), \
             patch("main.settings.market_user_telephone", ""), \
             patch("main.settings.publish_weekend", True), \
             patch("main.date") as fdate, \
             patch("main.editorial.run_pipeline", return_value=editorial_return) as ep, \
             patch("main._prefetch_gate", return_value=[]) as gate, \
             patch("main.daily_ai.ai_pick",
                   return_value={"idx": 1, "why": "池内甲文 的原文摘要讲了具体技术内容"
                                                 "与数字，跟学生相关。" * 2,
                                 "digest": "", "post_titles": ["池内甲文值得读"]}), \
             patch("main.daily_ai.ai_glossary", return_value=[]), \
             patch("main._fetch_body", return_value=("正文" * 200, "full", "")), \
             patch("main.render_repost", return_value="md"):
            fdate.today.return_value = date.today()
            fdate.fromisoformat.side_effect = date.fromisoformat
            rc = main.run_daily(force=True, dry_run=True)
        captured["ep_called"] = ep.called
        captured["gate_called"] = gate.called
        captured["log"] = store.logs
        return rc, captured

    def test_editorial_used_when_available(self):
        pick = {"item": _cand("池内甲文"), "idx": 1, "why": "编者按",
                "lead": "正文", "lead_kind": "full", "author": "a",
                "glossary": [], "post_titles": ["标题"]}
        rc, cap = self._run_daily({"pick": pick, "review": {"total": 0.9}})
        self.assertEqual(rc, 0)
        self.assertTrue(cap["ep_called"])
        self.assertFalse(cap["gate_called"])            # 流水线接管，不走闸门
        daily_logs = [entry[2] for entry in cap["log"]
                      if entry[0] == "daily" and "pipeline" in entry[2]]
        self.assertTrue(daily_logs)
        self.assertEqual(daily_logs[-1]["pipeline"], "editorial")
        self.assertEqual(daily_logs[-1]["review_total"], 0.9)

    def test_falls_back_to_legacy_when_pipeline_none(self):
        rc, cap = self._run_daily(None)
        self.assertEqual(rc, 0)
        self.assertTrue(cap["ep_called"])
        self.assertTrue(cap["gate_called"])             # M1 路径接管
        daily_logs = [entry[2] for entry in cap["log"]
                      if entry[0] == "daily" and "pipeline" in entry[2]]
        self.assertTrue(daily_logs)
        self.assertEqual(daily_logs[-1]["pipeline"], "legacy")




if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
