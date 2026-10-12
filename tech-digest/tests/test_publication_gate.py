import importlib
import unittest

from tests.reader_fixtures import article_packet, passing_review


class TestPublicationGate(unittest.TestCase):
    def setUp(self):
        try:
            self.gate = importlib.import_module("app.publication_gate")
        except ModuleNotFoundError:
            self.fail("统一发布门槛尚未实现")
        self.source, self.draft = article_packet()
        self.title = self.draft["title"]
        self.md = "## 为什么缓存能减少重复读取？\n\n固定输入的缓存对照。"
        self.review = passing_review(self.title, self.md, self.source)

    def decide(self):
        return self.gate.decide_publication(self.title, self.md, self.draft, self.source, self.review)

    def test_audited_final_draft_can_publish(self):
        self.assertEqual(self.decide()["status"], "ready")

    def test_all_seven_scores_remain_seven(self):
        self.assertEqual(self.gate.weighted_score(dict.fromkeys(("audience", "utility", "credibility", "depth", "novelty"), 7)), 7.0)

    def test_numeric_strings_boolean_nan_and_out_of_range_do_not_pass(self):
        for value in ("8", True, float("nan"), float("inf"), -1, 11):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.gate.weighted_score(dict(utility=value, depth=8, credibility=8, audience=8, novelty=8))

    def test_missing_review_is_generation_error(self):
        self.review = None
        self.assertEqual(self.decide()["status"], "generation_error")

    def test_high_overall_score_cannot_hide_poor_depth(self):
        self.review["scores"] = dict(utility=10, depth=6, credibility=10, audience=10, novelty=10)
        self.assertEqual(self.decide()["status"], "skip_quality")

    def test_low_review_does_not_publish(self):
        self.review["status"] = "revise"
        self.assertEqual(self.decide()["status"], "skip_quality")

    def test_reviewed_title_and_body_are_bound(self):
        for field in ("title", "md"):
            with self.subTest(field=field):
                old = getattr(self, field)
                setattr(self, field, old + "变化")
                self.assertEqual(self.decide()["status"], "generation_error")
                setattr(self, field, old)

    def test_old_rubric_review_cannot_be_reused(self):
        self.review["rubric_version"] = "reader-v1"
        self.assertEqual(self.decide()["status"], "generation_error")

    def test_source_hash_change_invalidates_review(self):
        self.review["source_hash"] = "0" * 64
        self.assertEqual(self.decide()["status"], "generation_error")

    def test_source_metadata_change_invalidates_review(self):
        self.source["method"] = "different_extraction_method"
        self.assertEqual(self.decide()["status"], "generation_error")

    def test_crawl_time_does_not_invalidate_unchanged_review(self):
        self.source["fetched_at"] = "2026-10-12T04:00:00+00:00"
        self.assertEqual(self.decide()["status"], "ready")

    def test_resource_classification_beats_author_self_report_and_high_score(self):
        self.review["article_type"] = "resource"
        self.assertEqual(self.decide()["status"], "skip_quality")

    def test_promotion_classification_is_rejected(self):
        self.review["content_purpose"] = "promotion"
        self.assertEqual(self.decide()["status"], "skip_quality")

    def test_incomplete_source_review_cannot_publish(self):
        self.review["source_complete"] = False
        self.assertEqual(self.decide()["status"], "skip_quality")


if __name__ == "__main__":
    unittest.main()
