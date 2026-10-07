# -*- coding: utf-8 -*-
"""采集层解析单测：HN 单条过滤 + sspai RSS 解析（fixture，不联网）。"""
import unittest

from app.sources.hacker_news import parse_hit, parse_item
from app.sources.sspai import clean_html, parse_feed

SSPAI_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>少数派</title>
  <item>
    <title>新款 MacBook Pro 上手：M4 芯片的性能与续航</title>
    <link>https://sspai.com/post/90001</link>
    <dc:creator>少数派编辑部</dc:creator>
    <pubDate>Sat, 30 Aug 2026 08:00:00 +0800</pubDate>
    <description>&lt;p&gt;本文基于一周实测，聊聊&lt;b&gt;M4&lt;/b&gt; 的表现。&lt;/p&gt;</description>
  </item>
  <item>
    <title>无链接条目应跳过</title>
    <dc:creator>x</dc:creator>
    <pubDate>Sat, 30 Aug 2026 09:00:00 +0800</pubDate>
  </item>
  <item>
    <title>本周看什么 | 有奖活动</title>
    <link>https://sspai.com/post/90002</link>
    <dc:creator>x</dc:creator>
    <pubDate>Sat, 30 Aug 2026 09:00:00 +0800</pubDate>
  </item>
</channel>
</rss>"""


class TestHN(unittest.TestCase):
    def test_valid_story(self):
        raw = {"type": "story", "by": "who", "time": 1756512000,
               "title": "Show HN: My Awesome Thing", "url": "https://example.com",
               "score": 42}
        it = parse_item(raw)
        self.assertIsNotNone(it)
        self.assertEqual(it["type"], "news")
        self.assertEqual(it["author"], "who")
        self.assertIsNotNone(it["published_at"])  # UTC → 本地 ISO
        self.assertEqual(it["summary"], "")

    def test_score_kept_as_points(self):
        """v4 把 HN points 当作影响力硬信号（挑爆文的依据），解析必须保留。"""
        raw = {"type": "story", "by": "who", "time": 1756512000,
               "title": "Show HN: My Awesome Thing", "url": "https://example.com",
               "score": 2103}
        it = parse_item(raw)
        self.assertEqual(it["points"], 2103)

    def test_low_score_rejected(self):
        raw = {"type": "story", "by": "who", "time": 1756512000,
               "title": "t", "url": "https://example.com", "score": 5}
        self.assertIsNone(parse_item(raw))

    def test_no_url_rejected(self):
        raw = {"type": "story", "by": "who", "time": 1756512000,
               "title": "Ask HN: 纯文本讨论", "score": 300}
        self.assertIsNone(parse_item(raw))

    def test_non_story_rejected(self):
        raw = {"type": "job", "by": "who", "time": 1756512000,
               "title": "YC 招聘", "url": "https://example.com", "score": 300}
        self.assertIsNone(parse_item(raw))

    def test_dead_rejected(self):
        raw = {"type": "story", "by": "who", "time": 1756512000,
               "title": "t", "url": "https://example.com", "score": 300, "dead": True}
        self.assertIsNone(parse_item(raw))


class TestHNAlgolia(unittest.TestCase):
    def test_valid_hit(self):
        hit = {"title": "RISC-V is now officially supported by CPython", "url": "https://x.com",
               "author": "peter", "created_at": "2026-08-25T04:12:22Z", "points": 345}
        it = parse_hit(hit)
        self.assertIsNotNone(it)
        self.assertEqual(it["source"], "hacker-news")
        self.assertEqual(it["author"], "peter")
        self.assertIn("+08:00", it["published_at"])  # UTC → 本地
        self.assertEqual(it["summary"], "")

    def test_low_points_rejected(self):
        hit = {"title": "t", "url": "https://x.com", "author": "a",
               "created_at": "2026-08-25T04:12:22Z", "points": 3}
        self.assertIsNone(parse_hit(hit))

    def test_ask_hn_no_url_rejected(self):
        hit = {"title": "Ask HN: 纯文本讨论", "url": None, "author": "a",
               "created_at": "2026-08-25T04:12:22Z", "points": 999}
        self.assertIsNone(parse_hit(hit))

    def test_bad_created_at_tolerated(self):
        hit = {"title": "t", "url": "https://x.com", "author": "a",
               "created_at": "not-a-date", "points": 100}
        it = parse_hit(hit)
        self.assertIsNotNone(it)
        self.assertIsNone(it["published_at"])  # 时间解析失败不致命


class TestSSPai(unittest.TestCase):
    def test_parse_feed(self):
        items, skipped = parse_feed(SSPAI_FIXTURE)
        self.assertEqual(len(items), 1)     # 无 link 的条目被跳过
        self.assertEqual(skipped, 1)        # 生活类（有奖/本周看什么）被过滤
        it = items[0]
        self.assertEqual(it["source"], "sspai")
        self.assertEqual(it["author"], "少数派编辑部")
        self.assertEqual(it["title"], "新款 MacBook Pro 上手：M4 芯片的性能与续航")
        self.assertIn("+08:00", it["published_at"])
        self.assertIn("M4", it["summary"])
        self.assertNotIn("<b>", it["summary"])  # 已纯文本化

    def test_clean_html(self):
        self.assertEqual(clean_html("<p>a<b>b</b> c</p>"), "a b c")
        self.assertEqual(clean_html("&amp; 转义"), "& 转义")


if __name__ == "__main__":
    unittest.main()
