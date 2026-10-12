import unittest
from app import daily_ai


class TestReaderCandidates(unittest.TestCase):
    def test_recommendations_and_promotions_are_excluded(self):
        for title in ("本周资源推荐", "免费课程推荐", "最佳开发工具清单", "20 free courses for developers", "报名优惠：AI训练营"):
            with self.subTest(title=title):
                self.assertTrue(daily_ai.excluded_candidate({"title": title}))

    def test_technical_resource_management_and_company_research_remain(self):
        for title in ("Resource allocation with a formal proof", "Google研究：缓存失效机制", "How memory management works"):
            self.assertFalse(daily_ai.excluded_candidate({"title": title}))

    def test_compatibility_prompt_does_not_allow_ai_news_exception(self):
        prompt = daily_ai._build_pick_prompt([])
        self.assertNotIn("AI 话题例外", prompt)
        self.assertNotIn("代替全文发布", prompt)
        self.assertIn("资源推荐", prompt)
        self.assertIn("知识", prompt)
