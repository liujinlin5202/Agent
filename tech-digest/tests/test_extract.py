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


# sspai 实测（2026-10-07 postID=6651 事故）：少数派把页头家具（日期/阅读时长/
# 标题重复/作者卡/分享面板）与页尾（责编/版权/充电卡）整段包进
# <article class="normal-article">，且文章是中文——全文直发不过翻译层，
# 家具原样进了帖子。类名照抄线上 DOM（探针 2026-10-07）。
SSPAI_FIXTURE = """<html><head><meta name="author" content="大大大K"></head><body>
<article class="normal-article">
  <div class="article__important__header">
    <div class="article__important__header__inner">
      <h1>基于Vaultwarden和Keyguard的自托管密码管理实践</h1>
      <div class="article__important__header__meta">
        <span>2026年10月06日</span>
        <span class="article__important__header__reading-time">10 分钟阅读</span>
      </div>
      <div class="article__important__header__author">
        <div class="article-header-author">
          <span class="author-type">主作者</span>
          <div class="el-popover author-popover"><span>大大大K</span>
            <span class="author-type">少数派作者</span><span>Yes Sir</span></div>
        </div>
        <div class="article-header-author">
          <span class="author-type">联合作者</span>
          <div class="el-popover author-popover"><span>大大大K</span>
            <span class="author-type">少数派作者</span></div>
        </div>
      </div>
      <div class="article__header__menu">
        <span class="article__share__popover__wrapper">
          <div class="el-popover article__share__panel__popover">
            <div class="article__share__panel">
              <div class="article__share__panel__header">微信扫码分享</div>
              <div class="article__share__panel__footer">点击下方按钮可复制链接</div>
            </div>
          </div>
        </span>
      </div>
    </div>
  </div>
  <div class="article-body transparent">
    <div class="article__main__wrapper">
      <div class="article__main__content wangEditor-txt">
        <p>注：题图使用 Gemini 生成。</p>
        <p>几年来我一直在市面上的主流密码管理方案间游走，上一个让我暂时安顿下来的是 Enpass。</p>
        <p>但 Bitwarden 另有所长——碰巧家里有一台 24 小时开机的 NAS，自托管一个 Vaultwarden 自然而然提上了日程。</p>
        <p><a href="#">关注少数派小红书</a>，感受精彩数字生活</p>
        <p>实用、好用的正版软件，少数派为你呈现</p>
      </div>
    </div>
  </div>
  <div class="article__footer article__section__wrapper">
    <div class="article__footer__editor">本文责编：@克莱德</div>
    <p>© 本文著作权归作者所有，并授权少数派独家使用，未经少数派许可，不得转载使用。</p>
  </div>
  <div class="article__charge__card">
    <p class="article__charge__card__count">8位派友已充电</p>
  </div>
</article>
</body></html>"""


class TestSspaiFurnitureStripped(unittest.TestCase):
    """页头页尾家具一个不留，正文逐字保留；结尾两行站点推广嵌在正文容器里，
    容器名单够不着，靠正文级质量网（文字模式）兜住。"""

    def test_furniture_gone_body_kept(self):
        r = extract_article(SSPAI_FIXTURE)
        self.assertIn("几年来我一直在市面上的主流密码管理方案间游走", r["text"])
        self.assertIn("注：题图使用 Gemini 生成。", r["text"])
        for junk in ("微信扫码分享", "点击下方按钮可复制链接", "主作者", "联合作者",
                     "大大大K", "Yes Sir", "10 分钟阅读", "2026年10月06日",
                     "本文责编", "著作权归作者", "派友已充电",
                     "关注少数派小红书", "实用、好用的正版软件"):
            self.assertNotIn(junk, r["text"])
        self.assertNotIn("基于Vaultwarden和Keyguard的自托管密码管理实践", r["text"])
        # 正文顺序不乱：题图注在正文之前
        self.assertLess(r["text"].index("题图使用"), r["text"].index("NAS"))

    def test_author_meta_still_extracted(self):
        r = extract_article(SSPAI_FIXTURE)
        self.assertEqual(r["author"], "大大大K")


class TestJunkMatchClassNotId(unittest.TestCase):
    """新噪声词只匹配 class：标题锚点 id 由文本 slug 化（claude.dev 实测
    id="share-the-chart-or-screenshot-itself" 的 H3 曾被整行误删）。"""

    def test_anchor_like_id_not_matched(self):
        html = ("<article><h3 id='share-the-chart-or-screenshot-itself'>"
                "Share the chart or screenshot itself</h3>"
                "<p>正文段落，内容足够长可以保留下来。</p></article>")
        r = extract_article(html)
        self.assertIn("Share the chart", r["text"])

    def test_new_keyword_class_still_matched(self):
        html = ("<article><p>正文段落，内容足够长可以保留下来。</p>"
                "<div class='share-panel'><p>微信扫码</p></div></article>")
        r = extract_article(html)
        self.assertIn("正文段落", r["text"])
        self.assertNotIn("微信扫码", r["text"])

    def test_old_keyword_id_still_matched(self):
        html = ("<article><p>正文段落，内容足够长可以保留下来。</p>"
                "<div id='comments-area'><p>Visitor said hi</p></div></article>")
        r = extract_article(html)
        self.assertNotIn("Visitor", r["text"])


class TestBodyQualityGate(unittest.TestCase):
    """正文级质量网（2026-10-07）：容器名单之外的家具漏网时，文本层兜底——
    ① 短块在邻域窗口内聚发 ≥3 次 = 家具突发（作者卡残影）；
    ② 已知推广/交互文案模式（短块才算，长段落里出现这些词是正常行文）。
    两类都只删非 <pre> 块。"""

    def test_clustered_repeat_dropped(self):
        chips = "".join("<div class='chip'><p>大大大K</p></div>" for _ in range(4))
        html = ("<article><p>正文第一段，内容足够长。</p>" + chips +
                "<p>正文第二段，同样保留下来。</p></article>")
        with self.assertLogs("tech-digest.extract", level="WARNING") as cm:
            r = extract_article(html)
        self.assertIn("正文第一段", r["text"])
        self.assertIn("正文第二段", r["text"])
        self.assertNotIn("大大大K", r["text"])
        self.assertTrue(any("质量网" in m for m in cm.output))

    def test_spread_repeat_kept(self):
        """代码块芯片标签分散出现（claude.dev 的 PROMPT ×6），不能误杀。"""
        filler = "".join(f"<p>第 {i} 段正文，讲一个独立的内容点。</p>"
                         for i in range(14))
        parts = ["<article>", "<p>PROMPT</p>", filler, "<p>PROMPT</p>", filler,
                 "<p>PROMPT</p>", "</article>"]
        r = extract_article("".join(parts))
        self.assertEqual(r["text"].count("PROMPT"), 3)

    def test_two_occurrences_kept(self):
        html = ("<article><p>写在最后</p><p>A 段内容。</p><p>写在最后</p>"
                "<p>B 段内容。</p></article>")
        r = extract_article(html)
        self.assertEqual(r["text"].count("写在最后"), 2)

    def test_long_repeated_block_kept(self):
        """>80 字的长块重复不算家具（本网只管人名/标签类短块）。"""
        long_p = "这是一段超过八十个字的正文段落，" * 6
        html = (f"<article><p>{long_p}</p><p>x</p><p>{long_p}</p>"
                f"<p>y</p><p>{long_p}</p></article>")
        r = extract_article(html)
        self.assertEqual(r["text"].count(long_p), 3)

    def test_pre_repeat_kept(self):
        pre = "<pre>SELECT 1;\nSELECT 1;\nSELECT 1;\nSELECT 1;</pre>"
        html = f"<article>{pre}<p>正文段落，内容足够长。</p></article>"
        r = extract_article(html)
        self.assertEqual(r["text"].count("SELECT 1;"), 4)

    def test_widget_pattern_dropped(self):
        html = ("<article><p>正文段落，内容足够长。</p>"
                "<p>点击下方按钮可复制链接</p><p>微信扫码分享</p></article>")
        r = extract_article(html)
        self.assertNotIn("复制链接", r["text"])
        self.assertNotIn("微信扫码", r["text"])

    def test_widget_words_in_long_prose_kept(self):
        """超过 60 字的长段里含推广短语 = 正常行文，不删（本网只认短块）。"""
        prose = ("原文页面由作者本人维护；在少数派的网页版里点击下方按钮可复制链接，"
                 "你转发时建议带上原作者与出处说明，这样对写作者也更友好一些。")
        html = f"<article><p>正文段落，内容足够长。</p><p>{prose}</p></article>"
        r = extract_article(html)
        self.assertIn("对写作者也更友好", r["text"])


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
