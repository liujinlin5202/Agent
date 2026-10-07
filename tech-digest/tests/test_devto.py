# -*- coding: utf-8 -*-
"""dev.to 热榜源单测：解析字段映射 + fetch 上限/异常。"""
import unittest
from unittest.mock import patch

from app.sources import devto

ART = {"type_of": "Article", "id": 1, "title": "Vibe Coding Isn't the Problem",
       "description": "A take on AI-assisted engineering.",
       "url": "https://dev.to/george/vibe-coding",
       "published_at": "2026-09-16T08:00:00Z",
       "public_reactions_count": 155, "comments_count": 182,
       "user": {"username": "george"}}


class TestParseArticle(unittest.TestCase):
    def test_full_article(self):
        it = devto.parse_article(dict(ART))
        self.assertEqual(it["title"], ART["title"])
        self.assertEqual(it["url"], ART["url"])
        self.assertEqual(it["source"], "devto")
        self.assertEqual(it["author"], "george")
        self.assertEqual(it["points"], 155)      # reactions 作为影响力硬信号
        self.assertIn("AI-assisted", it["summary"])
        self.assertIsNotNone(it["published_at"])

    def test_missing_url_dropped(self):
        bad = dict(ART, url="")
        self.assertIsNone(devto.parse_article(bad))

    def test_zero_reactions_no_points_key(self):
        it = devto.parse_article(dict(ART, public_reactions_count=0))
        self.assertNotIn("points", it)


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class TestFetch(unittest.TestCase):
    def test_fetch_maps_and_caps(self):
        arts = [dict(ART, id=i, url=f"https://dev.to/u/a{i}") for i in range(20)]
        with patch.object(devto, "http_get", return_value=_FakeResp(arts)):
            items, label, detail = devto.fetch()
        self.assertEqual(label, "devto")
        self.assertEqual(len(items), devto.MAX_ITEMS)
        self.assertEqual(detail["candidates"], 20)

    def test_fetch_empty_raises(self):
        with patch.object(devto, "http_get", return_value=_FakeResp([])):
            with self.assertRaises(RuntimeError):
                devto.fetch()

    def test_fetch_bad_shape_raises(self):
        with patch.object(devto, "http_get", return_value=_FakeResp({"error": "x"})):
            with self.assertRaises(RuntimeError):
                devto.fetch()


if __name__ == "__main__":
    unittest.main()
