import importlib
import unittest

from tests.reader_fixtures import article_packet


class TestReaderArticle(unittest.TestCase):
    def setUp(self):
        try:
            self.article = importlib.import_module("app.reader_article")
        except ModuleNotFoundError:
            self.fail("研究型稿件校验尚未实现")
        self.source, self.draft = article_packet()

    def test_original_complete_packet_is_accepted(self):
        self.assertEqual(self.article.validate_draft(self.draft, self.source), [])

    def test_resource_content_cannot_use_research_contract(self):
        self.draft["article_type"] = "resource"
        self.assertIn("excluded_article_type", self.article.validate_draft(self.draft, self.source))

    def test_evidence_must_reference_a_real_captured_block(self):
        self.source["evidence"][0]["block_id"] = "nonexistent"
        self.assertIn("missing_evidence", self.article.validate_draft(self.draft, self.source))

    def test_source_replacement_invalidates_draft(self):
        self.source["text"] += "输入已经变化"
        self.assertIn("source_hash_mismatch", self.article.validate_draft(self.draft, self.source))

    def test_blocks_cannot_be_replaced_while_preserving_source_hash(self):
        self.source["blocks"][0]["text"] = "原文没有的证据"
        self.assertIn("unbound_source_block", self.article.validate_draft(self.draft, self.source))

    def test_abstract_only_never_becomes_full_research_by_filling_fields(self):
        self.source["issues"] = ["abstract_only"]
        self.assertIn("abstract_only", self.article.validate_draft(self.draft, self.source))

    def test_truncated_evidence_requires_another_source_capture(self):
        self.source["issues"] = ["truncated"]
        self.assertIn("truncated", self.article.validate_draft(self.draft, self.source))

    def test_claiming_independent_measurement_requires_record(self):
        self.draft["test_status"] = "independently_tested"
        self.assertIn("missing_test_record", self.article.validate_draft(self.draft, self.source))

    def test_theoretical_proof_is_not_required_to_have_experiment(self):
        self.source["evidence_kind"] = "theoretical"
        self.draft["research_checks"]["support"] = "在固定输入假设下，缓存命中不再读取原始数据。"
        self.assertEqual(self.article.validate_draft(self.draft, self.source), [])

    def test_complete_single_section_theory_is_allowed(self):
        self.source["evidence_kind"] = "theoretical"
        self.draft["sections"] = [self.draft["sections"][0]]
        self.assertEqual(self.article.validate_draft(self.draft, self.source), [])

    def test_html_decoration_is_rejected_but_code_example_is_retained(self):
        self.draft["sections"][0]["markdown"] = '<div style="color:red">证据</div>'
        self.assertIn("unsupported_markup", self.article.validate_draft(self.draft, self.source))
        self.draft["sections"][0]["markdown"] = '```html\n<div>示例标签</div>\n```'
        self.assertNotIn("unsupported_markup", self.article.validate_draft(self.draft, self.source))

    def test_missing_core_research_check_is_rejected(self):
        self.draft["research_checks"]["method"] = ""
        self.assertIn("missing_research_checks", self.article.validate_draft(self.draft, self.source))

    def test_final_markdown_fence_must_be_closed(self):
        self.assertIn("unclosed_code_fence", self.article.markdown_issues("```python\nprint(1)"))

    def test_malformed_model_structures_return_errors(self):
        for field in ("article_type", "test_status"):
            with self.subTest(field=field):
                original = self.draft[field]
                self.draft[field] = []
                self.assertTrue(self.article.validate_draft(self.draft, self.source))
                self.draft[field] = original
        self.assertTrue(self.article.validate_draft(self.draft, None))
        self.source["evidence"][0]["block_id"] = []
        self.assertIn("missing_evidence", self.article.validate_draft(self.draft, self.source))
        self.source["blocks"] = None
        self.assertIn("missing_source_blocks", self.article.validate_draft(self.draft, self.source))


if __name__ == "__main__":
    unittest.main()
