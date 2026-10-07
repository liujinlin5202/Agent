# -*- coding: utf-8 -*-
"""全文提取器单测：fixture HTML，不联网。"""
import unittest

from app.extract import extract_article

FIXTURE = """<html><head><title>Training a 4B model | Rohan Bansal</title>
<meta name="author" content="Rohan Bansal">
<script>window.tracking = {a: 1};</script>
<style>body { color: red; }</style></head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<article>
  <h1>Training a 4B model to produce 81% faster query plans</h1>
  <p>Reinforcement learning can teach small models to rewrite SQL queries.
  This post walks through the reward design.</p>
  <pre>SELECT * FROM orders WHERE id = 1;</pre>
  <p>The result beat Postgres on every benchmark we tried.</p>
</article>
<footer>© 2026 Rohan</footer>
<aside>Newsletter signup spam</aside>
<script>analytics.fire();</script>
</body></html>"""


class TestExtract(unittest.TestCase):
    def test_prefers_article_block(self):
        r = extract_article(FIXTURE)
        self.assertIn("Reinforcement learning can teach", r["text"])
        self.assertIn("SELECT * FROM orders", r["text"])
        # nav/footer/aside/script/style 噪声不进正文
        self.assertNotIn("Newsletter", r["text"])
        self.assertNotIn("analytics.fire", r["text"])
        self.assertNotIn("About", r["text"])
        self.assertNotIn("© 2026", r["text"])

    def test_paragraphs_separated(self):
        r = extract_article(FIXTURE)
        self.assertIn("\n\n", r["text"])

    def test_author_from_meta(self):
        r = extract_article(FIXTURE)
        self.assertEqual(r["author"], "Rohan Bansal")

    def test_fallback_to_body_without_article(self):
        bare = ("<html><body><script>var x=1;</script>"
                "<p>第一段正文内容，足够长以通过过滤。</p>"
                "<p>第二段正文内容，同样需要保留下来。</p></body></html>")
        r = extract_article(bare)
        self.assertIn("第一段正文内容", r["text"])
        self.assertIn("第二段正文内容", r["text"])
        self.assertNotIn("var x=1", r["text"])

    def test_chinese_text_preserved(self):
        r = extract_article(FIXTURE.replace(
            "Reinforcement learning can teach small models to rewrite SQL queries.",
            "强化学习可以教会小模型重写 SQL 查询。"))
        self.assertIn("强化学习可以教会小模型", r["text"])

    def test_caps_length(self):
        from app.extract import EXTRACT_CAP
        huge = f"<article><p>{'很长' * 60000}</p></article>"
        r = extract_article(huge)
        self.assertLessEqual(len(r["text"]), EXTRACT_CAP)

    def test_title_h1_included(self):
        r = extract_article(FIXTURE)
        self.assertIn("81% faster query plans", r["text"])

    def test_preserves_newlines_in_pre(self):
        """<pre> 内缩进原样保留，且 pre 仍是独立块。

        旧实现的 _WS_RE 会把 pre 内连续空格/制表符压成单空格（换行虽保留）——
        Python/YAML 类缩进即语义的代码塌陷后不可读，翻译层的围栏保护救不回。
        """
        html = ("<html><body><article><p>Before code.</p>"
                "<pre>def f(x):\n    return x * 2\n\nSELECT a\nFROM b\nWHERE c = 1;</pre>"
                "<p>After code.</p></article></body></html>")
        r = extract_article(html)
        self.assertIn("def f(x):\n    return x * 2", r["text"])
        self.assertIn("SELECT a\nFROM b\nWHERE c = 1;", r["text"])
        self.assertIn("Before code.\n\ndef f(x):", r["text"])
        self.assertIn(";\n\nAfter code.", r["text"])

    def test_pre_with_entities_kept_raw(self):
        """pre 内 HTML 实体解码后原样保留（&lt;div&gt; 不当标签解析）。"""
        html = ("<html><body><article>"
                "<pre>if (a &lt; b) {\n  return &quot;x&quot;;\n}</pre>"
                "</article></body></html>")
        r = extract_article(html)
        self.assertIn("if (a < b) {\n  return \"x\";\n}", r["text"])


# dev.to 实测（2026-09-21）：评论区整块嵌在 <article> 里，每条评论自带
# 作者资料卡（Location/Work/Joined/Copy link/reactions），曾被当正文翻译进帖。
DEVTO_FIXTURE = """<html><head><meta name="author" content="Dhruv Jani"></head><body>
<article>
  <h1>AI Didn't Remove the Engineering Work</h1>
  <p>On September 15, India celebrates Engineer's Day for real reasons.</p>
  <div id="comments" class="crayons-comments">
    <div class="comment">
      <p>Dhruv Jani</p>
      <p>Location</p><p>Dallas-Fort Worth, Texas</p>
      <p>Work</p><p>Founder at SEOG</p>
      <p>Joined</p><p>Mar 7, 2026</p>
      <p>Copy link</p>
      <p>13 reactions</p>
      <p>View full discussion (98 comments)</p>
    </div>
  </div>
</article>
</body></html>"""


class TestCommentJunkStripped(unittest.TestCase):
    """噪声容器（评论区/侧栏/订阅框）按 id/class 整块剔除，错位闭合也要兜住。"""

    def test_comments_block_inside_article_stripped(self):
        r = extract_article(DEVTO_FIXTURE)
        self.assertIn("Engineer's Day", r["text"])          # 正文保留
        for junk in ("Dhruv Jani", "Joined", "Copy link", "reactions",
                     "View full discussion", "Dallas-Fort Worth"):
            self.assertNotIn(junk, r["text"])

    def test_class_only_junk_stripped(self):
        bare = ("<article><p>正文第一段在这里足够长了。</p>"
                "<div class='comments-area'><p>Visitor</p><p>Joined Jun 1</p></div>"
                "<p>正文第二段同样保留下来。</p></article>")
        r = extract_article(bare)
        self.assertIn("正文第一段", r["text"])
        self.assertIn("正文第二段", r["text"])
        self.assertNotIn("Joined Jun 1", r["text"])

    def test_junk_with_void_tags_and_misnesting(self):
        bare = ("<article><p>before junk paragraph here.</p>"
                "<div class='comments'><p>Dhruv<img src='/x.png'>Joined Mar 7</p>"
                "<div><p>reply text never closed"
                "</div></div>"
                "<p>after junk paragraph kept.</p></article>")
        r = extract_article(bare)
        self.assertIn("before junk", r["text"])
        self.assertIn("after junk", r["text"])
        self.assertNotIn("Dhruv", r["text"])
        self.assertNotIn("reply text", r["text"])

    def test_plain_class_unaffected(self):
        """正文容器类名含中性词（如 highlight-area）不受噪声正则误伤。"""
        bare = ("<article><p class='highlight-area'>中性类名的正文段落保留。</p></article>")
        r = extract_article(bare)
        self.assertIn("中性类名的正文段落", r["text"])


class TestLanguage(unittest.TestCase):
    def test_is_mostly_chinese_true(self):
        from app.extract import is_mostly_chinese
        self.assertTrue(is_mostly_chinese("这是一篇中文文章的正文内容，讲数据库查询优化。"))
        self.assertTrue(is_mostly_chinese("中文为主，夹杂少量 code 示例 block 的博客。"))

    def test_is_mostly_chinese_false(self):
        from app.extract import is_mostly_chinese
        self.assertFalse(is_mostly_chinese("RL can teach small models to rewrite SQL."))
        self.assertFalse(is_mostly_chinese(""))


if __name__ == "__main__":
    unittest.main()
