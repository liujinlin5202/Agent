"""Offline research-editorial regression: real pool, contracts and final gate."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app import editorial
from app.pool import PoolStore
from tests.reader_fixtures import article_packet


def candidate(number=1):
    source, _ = article_packet()
    return {"type": "news", "title": f"缓存机制研究{number}", "source": "hacker-news",
            "url": source["url"] + f"/{number}", "_pool_url": source["url"] + f"/{number}",
            "summary": "固定输入的缓存对照解释重复读取与失效条件。", "author": "作者", "points": 100}


def judge_reply(cands, score=8, article_type="mechanism"):
    return json.dumps({"scores": [{"title": c["title"],
        **dict.fromkeys(("utility", "depth", "credibility", "audience", "novelty"), score),
        "article_type": article_type, "content_purpose": "knowledge" if article_type == "mechanism" else "resource",
        "learning_question": "缓存怎样避免重复读取？", "prerequisites": ["了解重复读取"]} for c in cands]})


def research_reply():
    source, draft = article_packet()
    return json.dumps({"article_type": "mechanism", "content_purpose": "knowledge",
        "evidence_kind": source["evidence_kind"], "source_complete": True,
        "is_secondary": False, "original_paper_url": None,
        "research_checks": draft["research_checks"], "evidence": source["evidence"],
        "key_points": [draft["takeaway"]], "learning_question": draft["learning_question"]})


def author_reply():
    _, draft = article_packet()
    draft.pop("source_url")
    draft.pop("source_hash")
    draft["post_titles"] = [draft["title"]]
    return json.dumps(draft)


def review_reply(score=8, status="pass"):
    return json.dumps({"status": status,
        "scores": dict.fromkeys(("utility", "depth", "credibility", "audience", "novelty"), score),
        "reasons": ["需要补充证据"] if status != "pass" else [],
        "article_type": "mechanism", "content_purpose": "knowledge",
        "source_complete": True, "evidence_supported": True})


class TestAttemptTrail(unittest.TestCase):
    """缺口①（2026-10-13 评审）：完整审稿轨迹——研究阶段跳过、被拒评审、
    修改前后草稿都要进 attempts 并落库；今天之前这项是空数组，线上无法归因。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = PoolStore(Path(self.tmp.name) / "pool.db")
        self.key = patch.object(editorial.settings, "sse_market_api_key", "offline-test-key")
        self.key.start()
        self.source, self.draft = article_packet()

    def tearDown(self):
        self.key.stop()
        self.pool.close()
        self.tmp.cleanup()

    def run_pipeline(self, cands, batches, source=None, **kwargs):
        self.pool.upsert_news(cands)
        with patch.object(editorial.llmpool, "map_chat", side_effect=batches), \
                patch.object(editorial.extract, "fetch_with_mirror", return_value=source or self.source):
            return editorial.run_pipeline(cands, pool=self.pool, **kwargs)

    def persisted(self, url):
        row = self.pool.get_item(url)
        return json.loads(row["attempts"] or "[]") if row else []

    def test_research_skip_is_recorded_in_attempts(self):
        c = candidate()
        result = self.run_pipeline([c], [[judge_reply([c])]], source={"text": ""})
        self.assertEqual(result["status"], "generation_error")
        entry = next(a for a in result["attempts"] if a.get("stage") == "research")
        self.assertEqual(entry["url"], c["_pool_url"])
        self.assertEqual(entry["status"], "generation_error")
        self.assertIn("source_fetch_failed", entry["reasons"])
        self.assertTrue(self.persisted(c["_pool_url"]))

    def test_validate_failure_attempt_carries_draft(self):
        c = candidate()
        bad = json.dumps({**json.loads(author_reply()), "article_type": "resource"})
        result = self.run_pipeline([c], [[judge_reply([c])], [research_reply()], [bad],
                                      [author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")
        entry = next(a for a in result["attempts"] if a.get("stage") == "author")
        self.assertIsInstance(entry.get("draft"), dict)
        self.assertTrue(entry.get("title"))
        self.assertTrue(self.persisted(c["_pool_url"]))

    def test_rejected_and_revised_attempts_carry_review_and_hash(self):
        c = candidate()
        result = self.run_pipeline([c], [[judge_reply([c])], [research_reply()],
                                         [author_reply()], [review_reply(status="revise")],
                                         [author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["attempts"]), 2)
        first, second = result["attempts"]
        self.assertEqual(first["status"], "skip_quality")
        self.assertIsInstance(first.get("review"), dict)          # 被拒评审完整留存
        self.assertIsInstance(first.get("draft"), dict)           # 修改前草稿
        self.assertIsInstance(second.get("draft"), dict)          # 修改后草稿
        self.assertTrue(second.get("draft_hash"))
        self.assertTrue(self.persisted(c["_pool_url"]))


    def test_judge_prompt_demands_complete_fields(self):
        """glm-5.3 实测（2026-10-13）：评分 JSON 漏 article_type → 三篇 0.8+ 被
        _qualified 当未分类排除。字段缺失是格式失败，prompt 必须明示缺一不可。"""
        c = candidate()
        captured = {}

        def fake_map_chat(tasks):
            captured.setdefault("prompt", tasks[0]["prompt"])   # 只留首次=评委 prompt
            return [judge_reply([c])]

        with patch.object(editorial.llmpool, "map_chat", side_effect=fake_map_chat), \
                patch.object(editorial.extract, "fetch_with_mirror", return_value=self.source):
            editorial.run_pipeline([c], pool=self.pool)
        self.assertIn("缺一不可", captured["prompt"])
        self.assertIn("article_type", captured["prompt"])


class TestResearchEditorial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = PoolStore(Path(self.tmp.name) / "pool.db")
        self.key = patch.object(editorial.settings, "sse_market_api_key", "offline-test-key")
        self.key.start()
        self.source, self.draft = article_packet()

    def tearDown(self):
        self.key.stop()
        self.pool.close()
        self.tmp.cleanup()

    def run_pipeline(self, cands, batches, source=None, **kwargs):
        self.pool.upsert_news(cands)
        with patch.object(editorial.llmpool, "map_chat", side_effect=batches), \
                patch.object(editorial.extract, "fetch_with_mirror", return_value=source or self.source):
            return editorial.run_pipeline(cands, pool=self.pool, **kwargs)

    def test_accepted_draft_uses_reviewed_final_title_and_markdown(self):
        c = [candidate()]
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["title"], result["draft"]["title"])
        self.assertEqual(result["markdown"], result["markdown"].strip())
        self.assertIn("固定输入", result["markdown"])
        self.assertEqual(result["source_packet"]["url"], c[0]["url"])
        research = json.loads(self.pool.get_item(c[0]["url"])["research"])
        self.assertEqual(research["source_packet"]["text"], result["source_packet"]["text"])
        self.assertEqual(research["source_packet"]["blocks"], result["source_packet"]["blocks"])

    def test_three_low_score_candidates_are_skipped(self):
        c = [candidate(i) for i in range(3)]
        result = self.run_pipeline(c, [[judge_reply(c, score=3)]])
        self.assertEqual(result["status"], "skip_quality")
        self.assertIsNone(result["draft"])

    def test_course_recommendation_cannot_be_selected_even_with_high_score(self):
        c = [candidate()]
        result = self.run_pipeline(c, [[judge_reply(c, score=10, article_type="resource")]])
        self.assertEqual(result["status"], "skip_quality")

    def test_explicit_resource_title_is_quality_skip_without_model_call(self):
        c = [candidate()]
        c[0]["title"] = "开发工具清单"
        result = self.run_pipeline(c, [])
        self.assertEqual(result["status"], "skip_quality")

    def test_invalid_review_numeric_score_is_generation_error(self):
        c = [candidate()]
        bad = json.loads(review_reply(status="revise"))
        bad["scores"]["depth"] = "8"
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [json.dumps(bad)]])
        self.assertEqual(result["status"], "generation_error")

    def test_secondary_source_reads_verified_original_before_writing(self):
        c = [candidate()]
        source = deepcopy(self.source)
        primary_url = "https://arxiv.org/html/1234.56789v1"
        source["text"] += "\n" + primary_url
        secondary = json.loads(research_reply())
        secondary.update(is_secondary=True, original_paper_url=primary_url)
        with patch.object(editorial.llmpool, "map_chat", side_effect=[[judge_reply(c)], [json.dumps(secondary)],
                [research_reply()], [author_reply()], [review_reply()]]), \
                patch.object(editorial.extract, "fetch_with_mirror", side_effect=[source, self.source]) as fetch:
            result = editorial.run_pipeline(c, pool=self.pool)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source_packet"]["url"], primary_url)
        self.assertEqual(fetch.call_count, 2)

    def test_unbound_original_url_cannot_be_fetched(self):
        c = [candidate()]
        secondary = json.loads(research_reply())
        secondary.update(is_secondary=True, original_paper_url="http://127.0.0.1/private")
        result = self.run_pipeline(c, [[judge_reply(c)], [json.dumps(secondary)]])
        self.assertEqual(result["status"], "skip_quality")

    def test_empty_research_evidence_never_reaches_author(self):
        c = [candidate()]
        notes = json.loads(research_reply())
        notes["evidence"] = []
        result = self.run_pipeline(c, [[judge_reply(c)], [json.dumps(notes)]])
        self.assertEqual(result["status"], "skip_quality")
        self.assertIsNone(result["draft"])

    def test_archived_full_source_is_not_duplicated_into_author_prompt(self):
        c = [candidate()]
        with patch.object(editorial.llmpool, "map_chat", side_effect=[[judge_reply(c)], [research_reply()],
                [author_reply()], [review_reply()]]) as chat, \
                patch.object(editorial.extract, "fetch_with_mirror", return_value=self.source):
            result = editorial.run_pipeline(c, pool=self.pool)
        self.assertEqual(result["status"], "ready")
        prompt = chat.call_args_list[2].args[0][0]["prompt"]
        self.assertNotIn('"source_packet"', prompt)
        self.assertIn('"source_blocks"', prompt)

    def test_model_exception_returns_explicit_generation_error(self):
        with patch.object(editorial.llmpool, "map_chat", side_effect=RuntimeError("offline failure")):
            result = editorial.run_pipeline([candidate()], pool=self.pool)
        self.assertEqual(result["status"], "generation_error")

    def test_revised_draft_is_reviewed_again_and_can_pass(self):
        c = [candidate()]
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()],
            [review_reply(score=4, status="revise")], [author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["attempts"]), 2)

    def test_overlength_draft_is_revised_before_review_without_truncation(self):
        c = [candidate()]
        too_long = json.loads(author_reply())
        too_long["sections"][0]["markdown"] = '```python\n' + 'x=1\n' * 2300 + '```'
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [json.dumps(too_long)],
                                        [author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["attempts"][0]["reasons"], ["content_too_long_for_publication"])
        self.assertNotIn("内容过长已截断", result["markdown"])

    def test_failed_review_does_not_become_legacy_publication(self):
        c = [candidate()]
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [None]])
        self.assertEqual(result["status"], "generation_error")

    def test_failed_revision_stays_rejected(self):
        c = [candidate()]
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()],
            [review_reply(score=3, status="revise")], [author_reply()], [review_reply(score=3, status="revise")]])
        self.assertEqual(result["status"], "skip_quality")

    def test_empty_source_is_not_replaced_by_abstract(self):
        c = [candidate()]
        self.pool.upsert_news(c)
        with patch.object(editorial.llmpool, "map_chat", side_effect=[[judge_reply(c)]]), \
                patch.object(editorial.extract, "fetch_with_mirror", return_value=None):
            result = editorial.run_pipeline(c, pool=self.pool)
        self.assertEqual(result["status"], "generation_error")
        self.assertIsNone(result["draft"])

    def test_abstract_only_capture_never_reaches_author(self):
        c = [candidate()]
        source = deepcopy(self.source)
        source["issues"] = ["abstract_only"]
        result = self.run_pipeline(c, [[judge_reply(c)]], source=source)
        self.assertEqual(result["status"], "skip_quality")

    def test_old_same_day_score_is_not_reused(self):
        c = [candidate()]
        self.pool.upsert_news(c)
        self.pool.save_stage(c[0]["url"], "score_detail", {"kind": "judge", "date": editorial._today(),
                            "total": .9, "dims": dict.fromkeys(editorial._DIMS, 9)})
        with patch.object(editorial.llmpool, "map_chat", return_value=[judge_reply(c, score=3)]) as chat:
            scores = editorial.judge_score(c, self.pool)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(scores[c[0]["url"]]["total"], .3)

    def test_valid_cached_packet_reuses_models_only_when_source_matches(self):
        c = [candidate()]
        first = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [review_reply()]])
        self.assertEqual(first["status"], "ready")
        with patch.object(editorial.llmpool, "map_chat", side_effect=AssertionError("模型不应重复调用")), \
                patch.object(editorial.extract, "fetch_with_mirror", return_value=self.source):
            second = editorial.run_pipeline(c, pool=self.pool)
        self.assertEqual(second["status"], "ready")
        self.assertEqual(first["markdown"], second["markdown"])

    def test_invalid_cached_draft_is_a_cache_miss(self):
        c = [candidate()]
        first = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [review_reply()]])
        cached = json.loads(self.pool.get_item(c[0]["url"])["review"])
        cached["draft"]["sections"] = []
        self.pool.save_stage(c[0]["url"], "review", cached)
        result = self.run_pipeline(c, [[author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")

    def test_source_metadata_change_invalidates_caches(self):
        c = [candidate()]
        self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [review_reply()]])
        changed = deepcopy(self.source)
        changed.update(published_at="2026-10-12", title="新来源信息")
        result = self.run_pipeline(c, [[research_reply()], [author_reply()], [review_reply()]], source=changed)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source_packet"]["published_at"], "2026-10-12")

    def test_malformed_author_title_can_be_revised(self):
        c = [candidate()]
        bad = json.loads(author_reply())
        bad.update(title={}, post_titles=[])
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [json.dumps(bad)],
                                       [author_reply()], [review_reply()]])
        self.assertEqual(result["status"], "ready")

    def test_evidence_budget_preserves_method_section_context(self):
        blocks = [{"id":"intro", "kind":"paragraph", "text":"i"*52},
            {"id":"methods", "kind":"heading", "text":"Methods"},
            {"id":"method-body", "kind":"paragraph", "text":"Keep X fixed and divide L by n."},
            {"id":"results", "kind":"heading", "text":"Results"},
            {"id":"formula", "kind":"formula", "text":"L/n"},
            {"id":"limits", "kind":"heading", "text":"Limitations"}]
        view, omitted = editorial._evidence_view({"blocks": blocks}, budget=80)
        self.assertIn("method-body", [b["id"] for b in view])
        self.assertIn("intro", omitted)

    def test_unrecognized_title_and_invalid_numeric_score_are_not_scored(self):
        c = [candidate()]
        row = json.loads(judge_reply(c))["scores"][0]
        row["title"] = "不存在的标题"
        with patch.object(editorial.llmpool, "map_chat", return_value=[json.dumps({"scores": [row]})]):
            self.assertEqual(editorial.judge_score(c, self.pool), {})
        row["title"] = c[0]["title"]
        row["depth"] = "8"
        with patch.object(editorial.llmpool, "map_chat", return_value=[json.dumps({"scores": [row]})]):
            self.assertEqual(editorial.judge_score(c, self.pool), {})

    def test_dry_run_leaves_stages_and_pool_status_unchanged(self):
        c = [candidate()]
        self.pool.upsert_news(c)
        before = dict(self.pool.get_item(c[0]["url"]))
        result = self.run_pipeline(c, [[judge_reply(c)], [research_reply()], [author_reply()], [review_reply()]], dry_run=True)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(dict(self.pool.get_item(c[0]["url"]))["research"], before["research"])
        self.assertIsNone(self.pool.get_item(c[0]["url"])["review"])

    def test_new_candidate_after_rejection_uses_its_own_source(self):
        c = [candidate(1), candidate(2)]
        sources = {it["url"]: deepcopy(self.source) for it in c}
        sources[c[1]["url"]]["author"] = "第二篇作者"
        self.pool.upsert_news(c)
        with patch.object(editorial.extract, "fetch_with_mirror", side_effect=lambda it: sources[it["url"]]), \
                patch.object(editorial.llmpool, "map_chat", side_effect=[
                    [judge_reply(c)], [research_reply(), research_reply()],
                    [author_reply()], [review_reply(3, "reject")],
                    [author_reply()], [review_reply()]]):
            result = editorial.run_pipeline(c, pool=self.pool)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source_packet"]["url"], c[1]["url"])
        self.assertEqual(result["source_packet"]["author"], "第二篇作者")
        self.assertIn(c[1]["url"], result["markdown"])


if __name__ == "__main__":
    unittest.main()
