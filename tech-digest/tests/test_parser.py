# -*- coding: utf-8 -*-
"""解析器单测：内嵌真实页面片段 fixture（2026-08-30 快照裁剪）。"""
import unittest

from app import parser

FIXTURE = """
<article class="Box-row">
  <div class="d-md-flex">
    <h2 class="h3 lh-condensed">
      <a href="/tt-a1i/archify" data-view-component="true">archify</a>
    </h2>
    <div class="d-inline-block">
      <span itemprop="programmingLanguage">JavaScript</span>
    </div>
    <a class="tmp-mr-3 Link Link--muted d-inline-block" href="/tt-a1i/archify/stargazers">
      <svg aria-label="star" class="octicon octicon-star"></svg> 31.2k
    </a>
    <p>Agent skill for beautiful, verifiable architecture diagrams</p>
    <span class="d-inline-block float-sm-right">
      <svg class="octicon octicon-star"></svg>
      3,902 stars today
    </span>
  </div>
</article>
<article class="Box-row">
  <h2><a href="/p-e-w/heretic">heretic</a></h2>
  <div><span itemprop="programmingLanguage">Python</span></div>
  <a href="/p-e-w/heretic/stargazers">28,718</a>
  <p> Fully automatic censorship removal for language models </p>
  <span class="d-inline-block float-sm-right"><svg></svg>150 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/bigskysoftware/htmx">htmx</a></h2>
  <a href="/bigskysoftware/htmx/stargazers">49.1k</a>
  <p>&lt;/&gt; htmx - high power tools for HTML</p>
  <span class="d-inline-block float-sm-right"><svg></svg>no stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/cosmin/stars">stars</a></h2>
  <a href="/cosmin/stars/stargazers">1.2m</a>
  <span class="d-inline-block float-sm-right"><svg></svg>12 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/no/desc">no-desc</a></h2>
  <a href="/no/desc/stargazers">999</a>
  <span class="d-inline-block float-sm-right"><svg></svg>3 stars today</span>
</article>
"""


class TestParseNum(unittest.TestCase):
    def test_suffix(self):
        self.assertEqual(parser.parse_num("2.3k"), 2300)
        self.assertEqual(parser.parse_num("1.2m"), 1_200_000)
        self.assertEqual(parser.parse_num("5,678"), 5678)
        self.assertEqual(parser.parse_num("999"), 999)
        self.assertEqual(parser.parse_num(""), 0)
        self.assertEqual(parser.parse_num("n/a"), 0)


class TestParseTrending(unittest.TestCase):
    def test_full_fields(self):
        items = parser.parse_trending(FIXTURE)
        self.assertEqual(len(items), 5)
        it = items[0]
        self.assertEqual(it["full_name"], "tt-a1i/archify")
        self.assertEqual(it["url"], "https://github.com/tt-a1i/archify")
        self.assertEqual(it["language"], "JavaScript")
        self.assertEqual(it["stars"], 31_200)      # 31.2k → 31200（页面显示为 k 缩写）
        self.assertEqual(it["today_stars"], 3902)  # 纯文本 "3,902 stars today"

    def test_desc_and_missing_lang(self):
        items = parser.parse_trending(FIXTURE)
        # 第 2 条（heretic）有描述（含首尾空白应被清理）
        self.assertEqual(items[1]["desc"], "Fully automatic censorship removal for language models")
        # 第 5 条无 <p> 无语言 span
        self.assertEqual(items[4]["desc"], "(无描述)")
        self.assertIsNone(items[4]["language"])

    def test_today_zero_when_absent(self):
        items = parser.parse_trending(FIXTURE)
        # no stars today → 0
        self.assertEqual(items[2]["today_stars"], 0)

    def test_too_few_rows_returns_empty(self):
        too_few = FIXTURE.replace('<article class="Box-row">', '<article class="Box-row">', 1)
        html = too_few[: too_few.find("</article>") + len("</article>")]
        self.assertEqual(parser.parse_trending(html), [])


class TestParseApi(unittest.TestCase):
    def test_api_items(self):
        data = {"items": [
            {"full_name": "a/b", "html_url": "https://github.com/a/b",
             "language": "Go", "stargazers_count": 123, "description": "d1"},
            {"full_name": "a/b", "stargazers_count": 1},  # 重复，应被过滤
        ]}
        items = parser.parse_api(data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["today_stars"], 0)

    def test_seen_filter(self):
        data = {"items": [{"full_name": "x/y", "stargazers_count": 5}]}
        self.assertEqual(parser.parse_api(data, {"x/y"}), [])


if __name__ == "__main__":
    unittest.main()
