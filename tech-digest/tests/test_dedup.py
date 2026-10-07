# -*- coding: utf-8 -*-
"""去重单测：URL 规范化 / 标题相似 / 批内+历史窗口 / trending 豁免。"""
import unittest

from app.dedup import dedupe_news, is_duplicate, normalize_url, title_similarity


def news(title, url, **kw):
    d = {"type": "news", "title": title, "url": url, "source": "sspai",
         "author": "", "published_at": None, "fetched_at": "2026-08-30T09:00:00",
         "summary": "", "ai_summary": None, "confidence": None, "trending": None}
    d.update(kw)
    return d


class TestNormalize(unittest.TestCase):
    def test_tracking_params(self):
        self.assertEqual(
            normalize_url("https://a.com/p?utm_source=x&b=2#frag"),
            "https://a.com/p?b=2")

    def test_hash_and_case(self):
        self.assertEqual(
            normalize_url("https://A.com/Path/?x=1#sec"),
            "https://a.com/Path?x=1")

    def test_ref_param(self):
        self.assertEqual(
            normalize_url("https://a.com/news?ref=weixin&id=9"),
            "https://a.com/news?id=9")

    def test_trailing_slash(self):
        self.assertEqual(normalize_url("https://a.com/"), "https://a.com")

    def test_empty(self):
        self.assertEqual(normalize_url(""), "")


class TestSimilarity(unittest.TestCase):
    def test_high(self):
        self.assertGreaterEqual(
            title_similarity("Go 1.24 正式发布", "Go 1.24 正式发布了"), 0.85)

    def test_low(self):
        self.assertLess(
            title_similarity("React 19 正式发布", "Laravel 12 带来全新脚手架"), 0.85)

    def test_same(self):
        self.assertAlmostEqual(title_similarity("abc", "abc"), 1.0)


class TestIsDuplicate(unittest.TestCase):
    def test_same_url(self):
        a = news("a", "https://x.com/p?utm_source=1")
        b = news("b", "https://x.com/p")
        self.assertTrue(is_duplicate(a, b))

    def test_title_threshold(self):
        a = news("Go 1.24 正式发布", "https://x.com/a")
        b = news("Go 1.24 正式发布了", "https://x.com/b")
        self.assertTrue(is_duplicate(a, b))

    def test_distinct(self):
        a = news("React 19 正式发布", "https://x.com/a")
        b = news("Laravel 12 发布", "https://x.com/b")
        self.assertFalse(is_duplicate(a, b))

    def test_v1_history_trending_ignored_by_caller(self):
        # v1 条目（无 title）与 news 标题对比：全名匹配不到中文标题，不误杀
        old = {"full_name": "a/b", "url": "https://github.com/a/b",
               "language": "Go", "stars": 1, "today_stars": 1, "desc": "x"}
        a = news("某中文深度文章", "https://x.com/z")
        self.assertFalse(is_duplicate(a, old))


class TestDedupeNews(unittest.TestCase):
    def test_history_dup(self):
        hist = [("2026-08-29", news("Go 1.24 正式发布", "https://x.com/a"))]
        items = [news("Go 1.24 正式发布", "https://x.com/a"),
                 news("新东西", "https://x.com/b")]
        kept, info = dedupe_news(items, hist)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "新东西")
        self.assertEqual(info["removed"][0]["dup_date"], "2026-08-29")

    def test_in_batch_dup(self):
        items = [news("同题", "https://x.com/a"), news("同题", "https://x.com/b")]
        kept, info = dedupe_news(items, [])
        self.assertEqual(len(kept), 1)
        self.assertEqual(info["removed"][0]["dup_date"], "(同批)")

    def test_normalized_url_dup_history(self):
        hist = [("2026-08-28", news("标题 A", "https://x.com/p?utm_source=y"))]
        items = [news("标题 B", "https://x.com/p")]
        kept, info = dedupe_news(items, hist)
        self.assertEqual(len(kept), 0)
        self.assertEqual(info["removed"][0]["dup_title"], "标题 A")


if __name__ == "__main__":
    unittest.main()
