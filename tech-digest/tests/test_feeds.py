# -*- coding: utf-8 -*-
"""通用 RSS/Atom 源单测：双格式解析、summary 清洗、字段映射、fetch 上限与异常。"""
import unittest
from unittest.mock import patch

from app.sources import feeds

RSS_FULL = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Ars Technica</title>
<item><title>Hackers reveal how Flock cameras track cars</title>
<link>https://arstechnica.com/security/flock</link>
<description>&lt;p&gt;Hello &amp;amp; hi&lt;/p&gt;</description>
<content:encoded><![CDATA[<p>full body should NOT go into summary</p>]]></content:encoded>
<dc:creator>Sean Gallagher</dc:creator>
<pubDate>Tue, 16 Sep 2026 08:00:00 +0000</pubDate></item>
<item><title>No link item</title><description>x</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>科技爱好者周刊</title>
<entry><title>第 412 期：禁止 issue，只用 PR</title>
<link rel="alternate" href="https://www.ruanyifeng.com/blog/2026/09/weekly-412.html"/>
<link rel="self" href="https://www.ruanyifeng.com/blog/atom.xml"/>
<content type="html">&lt;p&gt;本周话题&lt;/p&gt;</content>
<published>2026-09-11T00:00:00Z</published>
<author><name>阮一峰</name></author></entry>
</feed>"""


def _item(items):
    return items[0]


class TestParseRss(unittest.TestCase):
    def test_fields(self):
        items = feeds.parse_feed(RSS_FULL, "ars-technica")
        self.assertEqual(len(items), 1)          # 无 link 的条目被丢弃
        it = _item(items)
        self.assertEqual(it["title"], "Hackers reveal how Flock cameras track cars")
        self.assertEqual(it["url"], "https://arstechnica.com/security/flock")
        self.assertEqual(it["source"], "ars-technica")
        self.assertEqual(it["author"], "Sean Gallagher")
        self.assertIsNotNone(it["published_at"])
        # summary 取 description（清洗后），不吞 content:encoded 全文
        self.assertEqual(it["summary"], "Hello & hi")

    def test_summary_falls_back_to_content_encoded(self):
        no_desc = RSS_FULL.replace("<description>&lt;p&gt;Hello &amp;amp; hi&lt;/p&gt;</description>", "")
        items = feeds.parse_feed(no_desc, "ars-technica")
        self.assertIn("full body", _item(items)["summary"])


class TestParseAtom(unittest.TestCase):
    def test_fields(self):
        items = feeds.parse_feed(ATOM, "ruanyifeng")
        it = _item(items)
        self.assertIn("第 412 期", it["title"])
        self.assertEqual(it["url"], "https://www.ruanyifeng.com/blog/2026/09/weekly-412.html")
        self.assertEqual(it["author"], "阮一峰")
        self.assertIn("本周话题", it["summary"])
        self.assertIsNotNone(it["published_at"])

    def test_garbage_raises_value_error(self):
        with self.assertRaises(Exception):
            feeds.parse_feed("not xml at all", "x")


class _FakeResp:
    def __init__(self, text):
        self.text = text


class TestFetchFeed(unittest.TestCase):
    def test_fetch_caps(self):
        xml = RSS_FULL.replace("<item><title>No link item</title><description>x</description></item>", "")
        many = xml.replace("</channel></rss>", "".join(
            f'<item><title>t{i}</title><link>https://x/{i}</link><description>d</description></item>'
            for i in range(20)) + "</channel></rss>")
        with patch.object(feeds, "http_get", return_value=_FakeResp(many)):
            items, label, detail = feeds.fetch_feed("ars-technica", "https://x/feed", 10)
        self.assertEqual(label, "ars-technica")
        self.assertEqual(len(items), 10)

    def test_fetch_empty_raises(self):
        empty = '<rss version="2.0"><channel><title>t</title></channel></rss>'
        with patch.object(feeds, "http_get", return_value=_FakeResp(empty)):
            with self.assertRaises(RuntimeError):
                feeds.fetch_feed("ars-technica", "https://x/feed", 10)


if __name__ == "__main__":
    unittest.main()
