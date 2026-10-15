# -*- coding: utf-8 -*-
"""全文提取器单测：fixture HTML，不联网。"""
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from hashlib import sha256
from datetime import datetime

from app.extract import extract_article

READER_FIXTURES = Path(__file__).parent / "fixtures" / "reader_articles"

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

    def test_literal_quote_promo_dropped(self):
        """少数派把推广行写成 <p>&gt; ...</p> 字面文本——提取后以「> 」开头的
        短块是家具铁证（真 <blockquote> 不会产生 > 前缀）。"""
        html = ("<article><p>正文段落，内容足够长。</p>"
                "<p>&gt; 守护孩子的天马行空，从零开始美术教育启蒙</p></article>")
        r = extract_article(html)
        self.assertNotIn("守护孩子", r["text"])

    def test_blockquote_element_not_confused(self):
        """真 <blockquote> 引用是正文，提取器本就不输出 > 前缀，不受影响。"""
        html = ("<article><blockquote><p>被引用的一段话，属于正文内容。</p></blockquote>"
                "<p>正文段落，内容足够长。</p></article>")
        r = extract_article(html)
        self.assertIn("被引用的一段话", r["text"])


# sspai Matrix 专栏模板（2026-10-07 实测 post/114845）：页头是另一套类名
# article__header__*（Matrix精选/日期/阅读时长/标题），社区声明与推广行照旧
# 嵌在正文容器里——推广行是 <p>&gt; ...字面文本，提取后以「> 」开头。
SSPAI_MATRIX_FIXTURE = """<html><body>
<article class="normal-article">
  <div class="article__header article__section__wrapper">
    <div class="article__header__content">
      <h1>罗马：永恒之城，永恒于世</h1>
      <div class="article__header__meta"><div class="article__header__meta__main">
        <span class="article__header__tag"><span>Matrix精选</span></span>
        <span>2026年10月07日</span>
        <span class="article__header__reading-time">22 分钟阅读</span>
      </div></div>
    </div>
  </div>
  <div class="article-body"><div class="article__main__wrapper">
    <div class="article__main__content wangEditor-txt">
      <p>罗马城的老建筑保存得出奇地好，这是一篇城市漫步记录的开头。</p>
      <p>Matrix 首页推荐</p>
      <p>文章代表作者个人观点，少数派仅对标题和排版略作修改。</p>
      <p>Matrix 是少数派的写作社区，我们主张分享真实的产品体验，有实用价值的经验与思考。我们会不定期挑选 Matrix 最优质的文章，展示来自用户的最真实的体验和思考。</p>
      <p>&gt; 守护孩子的天马行空，从零开始美术教育启蒙</p>
    </div>
  </div></div>
</article>
</body></html>"""


class TestSspaiMatrixTemplate(unittest.TestCase):
    """Matrix 专栏页头家具 + 固定社区声明 + 「> 」字面推广行。"""

    def test_matrix_furniture_gone_body_kept(self):
        r = extract_article(SSPAI_MATRIX_FIXTURE)
        self.assertIn("罗马城的老建筑保存得出奇地好", r["text"])
        for junk in ("Matrix精选", "22 分钟阅读", "2026年10月07日",
                     "Matrix 是少数派的写作社区", "守护孩子的天马行空",
                     "文章代表作者个人观点", "Matrix 首页推荐",
                     "罗马：永恒之城"):
            self.assertNotIn(junk, r["text"])


class TestAuthorBylineCard(unittest.TestCase):
    """freeCodeCamp 等站的 author-card 署名卡（姓名×2 + 简历段）不进正文。"""

    def test_author_card_stripped(self):
        html = ("<article><section class='author-card'>"
                "<span class='author-card-name'>Jane Dev</span>"
                "<p>Engineering Lead with 7+ years of experience.</p></section>"
                "<p>正文段落，内容足够长可以保留下来。</p></article>")
        r = extract_article(html)
        self.assertNotIn("Jane Dev", r["text"])
        self.assertNotIn("Engineering Lead", r["text"])
        self.assertIn("正文段落", r["text"])


class TestLanguage(unittest.TestCase):
    def test_is_mostly_chinese_true(self):
        from app.extract import is_mostly_chinese
        self.assertTrue(is_mostly_chinese("这是一篇中文文章的正文内容，讲数据库查询优化。"))
        self.assertTrue(is_mostly_chinese("中文为主，夹杂少量 code 示例 block 的博客。"))

    def test_is_mostly_chinese_false(self):
        from app.extract import is_mostly_chinese
        self.assertFalse(is_mostly_chinese("RL can teach small models to rewrite SQL."))
        self.assertFalse(is_mostly_chinese(""))


class TestReaderBlocks(unittest.TestCase):
    def fixture(self, name):
        return (READER_FIXTURES / name).read_text(encoding="utf-8")

    def test_verified_body_beats_article_shell(self):
        r = extract_article(self.fixture("freecodecamp.html"))
        self.assertIn("Body-only sentence", r["text"])
        for noise in ("Outer advertisement", "Article shell furniture", "biography"):
            self.assertNotIn(noise, r["text"])
        self.assertEqual(r.get("method"), "site_selector")
        self.assertEqual(r["author"], "Ada Reader")

    def test_heading_and_ordered_items_survive(self):
        r = extract_article(self.fixture("freecodecamp.html"))
        blocks = r.get("blocks", [])
        self.assertTrue(any(b["kind"] == "heading" and b["text"] == "Compare equal workloads" for b in blocks))
        items = [b for b in blocks if b["kind"] == "list_item"]
        self.assertEqual([b["text"] for b in items], ["Hold the number of messages fixed.", "Measure the same transport."])
        self.assertEqual([b.get("ordinal") for b in items], [1, 2])

    def test_links_keep_targets(self):
        r = extract_article(self.fixture("freecodecamp.html"))
        link_blocks = [b for b in r.get("blocks", []) if b.get("links")]
        self.assertTrue(link_blocks)
        self.assertIn({"text": "original measurement", "url": "https://example.org/paper"}, link_blocks[0]["links"])
        self.assertIn("https://example.org/paper", r["text"])

    def test_code_indentation_is_preserved(self):
        r = extract_article('<article><pre><code>if ok:\n    run()\n</code></pre></article>')
        code = [b for b in r.get("blocks", []) if b["kind"] == "code"]
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0]["text"], "if ok:\n    run()\n")

    def test_math_tex_and_figure_caption_survive(self):
        r = extract_article(self.fixture("freecodecamp.html"))
        self.assertIn("T = L + n c", r["text"])
        self.assertTrue(any(b["kind"] == "formula" and "T = L + n c" in b["text"] for b in r.get("blocks", [])))
        self.assertTrue(any(b["kind"] == "caption" and b["text"].startswith("Figure 1:") for b in r.get("blocks", [])))

    def test_mathml_without_tex_keeps_token_boundaries(self):
        r = extract_article('<article><math><mi>x</mi><mo>=</mo><mi>a</mi><mo>+</mo><mi>b</mi></math></article>')
        formulas = [b for b in r.get("blocks", []) if b["kind"] == "formula"]
        self.assertEqual([b["text"] for b in formulas], ["x = a + b"])

    def test_table_cells_keep_row_boundaries(self):
        r = extract_article(self.fixture("freecodecamp.html"))
        tables = [b for b in r.get("blocks", []) if b["kind"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].get("rows"), [["Strategy", "Latency (ms)"], ["One batch", "12"], ["Two calls", "22"]])
        self.assertIn("One batch\t12", tables[0]["text"])
        self.assertTrue(any(b["kind"] == "caption" and b["text"] == "Controlled latency measurement" for b in r["blocks"]))

    def test_block_ids_are_stable_and_unique(self):
        first = extract_article(self.fixture("freecodecamp.html"))
        second = extract_article(self.fixture("freecodecamp.html"))
        self.assertTrue(first.get("blocks"))
        self.assertEqual(first["blocks"], second["blocks"])
        ids = [b["id"] for b in first["blocks"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(b["kind"] and isinstance(b["text"], str) for b in first["blocks"]))

    def test_raw_html_length_does_not_select_noisy_article(self):
        html = '<article><script>' + 'tracker;' * 3000 + '</script><div class="advertisement">Ad</div></article><article><h1>Reasoning</h1><p>Real method uses a fixed connection cost.</p></article>'
        r = extract_article(html)
        self.assertIn("Real method", r["text"])
        self.assertNotIn("Ad", r["text"])

    def test_void_and_misnested_tags_do_not_swallow_body(self):
        r = extract_article('<article><img class="banner" src="ad.png"><p>Before<br/>line.</p><div class="comments"><p>Noise<img src="x"></div><p>After evidence.</p></article>')
        self.assertIn("Before", r["text"])
        self.assertIn("After evidence", r["text"])
        self.assertNotIn("Noise", r["text"])

    def test_short_full_argument_is_retained_with_signal(self):
        r = extract_article('<article><p>Batching amortizes one setup cost over n messages, so the per-message setup cost is L/n when the workload is fixed.</p></article>')
        self.assertIn("L/n", r["text"])
        self.assertIn("short_text", r.get("issues", []))
        self.assertNotIn("fulltext_required", r["issues"])

    def test_login_shell_is_not_substantive(self):
        r = extract_article('<body><form>请登录后阅读</form></body>')
        self.assertFalse(r["text"].strip())
        self.assertIn("login_required", r.get("issues", []))

    def test_empty_shell_has_explicit_issue(self):
        r = extract_article('<html><body><div id="app"></div></body></html>')
        self.assertEqual(r["text"], "")
        self.assertIn("empty_content", r.get("issues", []))

    def test_arxiv_html_preserves_methods_and_results(self):
        r = extract_article(self.fixture("arxiv_full.html"))
        self.assertIn("Compare one batch to eight separate calls", r["text"])
        self.assertIn("saves seven setup costs", r["text"])
        self.assertEqual(r.get("method"), "site_selector")
        for issue in ("abstract_only", "missing_method", "missing_support", "fulltext_required"):
            self.assertNotIn(issue, r.get("issues", []))
        self.assertEqual(r.get("publication_status"), "preprint")

    def test_abstract_landing_page_requires_fulltext(self):
        r = extract_article(self.fixture("paper_abstract.html"))
        self.assertIn("includes no methods", r["text"])
        self.assertIn("abstract_only", r.get("issues", []))
        self.assertIn("fulltext_required", r.get("issues", []))
        self.assertEqual(r.get("publication_status"), "unknown")

    def test_incomplete_paper_identifies_missing_support(self):
        r = extract_article('<article class="ltx_document"><section class="ltx_section"><h2>Methods</h2><p>We compare two fixed workloads.</p></section></article>')
        self.assertIn("missing_support", r.get("issues", []))
        self.assertIn("fulltext_required", r.get("issues", []))

    def test_theoretical_paper_accepts_derivation_as_support(self):
        r = extract_article('<article class="ltx_document"><section class="ltx_section"><h2>Assumptions</h2><p>Assume latency is constant.</p></section><section class="ltx_section"><h2>Proof</h2><p>Divide the fixed latency by the number of messages to obtain L/n.</p></section></article>')
        self.assertTrue(r.get("blocks"))
        self.assertNotIn("missing_support", r.get("issues", []))
        self.assertNotIn("fulltext_required", r.get("issues", []))

    def test_missing_referenced_table_is_flagged(self):
        r = extract_article('<article class="ltx_document"><h2>Methods</h2><p>Compare equal workloads.</p><h2>Results</h2><p>See Table 1 for the measured outcomes.</p></article>')
        self.assertIn("missing_support", r.get("issues", []))
        self.assertIn("fulltext_required", r.get("issues", []))

    def test_truncated_capture_is_flagged_without_invented_blocks(self):
        from app import extract
        with patch.object(extract, "EXTRACT_CAP", 40):
            r = extract_article('<article><p>' + 'x' * 60 + '</p><p>Unavailable result.</p></article>')
        self.assertEqual(len(r["text"]), 40)
        self.assertIn("truncated", r.get("issues", []))
        self.assertIn("fulltext_required", r.get("issues", []))
        self.assertEqual("\n\n".join(b["text"] for b in r.get("blocks", [])), r["text"])
        self.assertNotIn("Unavailable", str(r["blocks"]))

    def test_unclosed_article_reports_incomplete_capture(self):
        r = extract_article('<article><h2>Methods</h2><p>A captured prefix')
        self.assertIn("truncated", r.get("issues", []))
        self.assertIn("fulltext_required", r.get("issues", []))

    def test_paper_title_is_not_a_result_section(self):
        r = extract_article('<article class="ltx_document"><h1>An analysis of batching</h1><h2>Methods</h2><p>Keep workload fixed.</p></article>')
        self.assertIn("missing_support", r["issues"])

    def test_empty_methods_heading_is_not_evidence(self):
        r = extract_article('<article class="ltx_document"><h2>Methods</h2><h2>Results</h2><p>Observed latency was lower.</p></article>')
        self.assertIn("missing_method", r["issues"])
        self.assertIn("fulltext_required", r["issues"])

    def test_generic_article_excludes_main_shell_prose(self):
        r = extract_article('<main><p>Outside article page copy ' + 'noise ' * 100 + '</p><article><h1>Latency</h1><p>Inside article method pays setup once.</p></article></main>')
        self.assertIn("Inside article", r["text"])
        self.assertNotIn("Outside article", r["text"])

    def test_optional_paragraph_end_preserves_following_heading(self):
        r = extract_article('<article><p>First argument.<h2>Result</h2><p>Second argument.</article>')
        self.assertTrue(any(b["kind"] == "heading" and b["text"] == "Result" for b in r["blocks"]))

    def test_mathml_fraction_structure_is_preserved(self):
        r = extract_article('<article><math><mfrac><mi>L</mi><mi>n</mi></mfrac></math></article>')
        formula = next(b for b in r["blocks"] if b["kind"] == "formula")
        self.assertIn("<mfrac>", formula.get("mathml", ""))
        self.assertIn("(L)/(n)", formula["text"])

    def test_mathjax_tex_script_is_not_dropped_as_tracking(self):
        r = extract_article('<article><p>Setup cost follows.</p><script type="math/tex; mode=display">L/n</script><script>analytics.fire()</script></article>')
        self.assertIn("L/n", r["text"])
        self.assertTrue(any(b["kind"] == "formula" for b in r["blocks"]))
        self.assertNotIn("analytics", r["text"])

    def test_nested_figure_pixels_are_unverified_support(self):
        r = extract_article('<article class="ltx_document"><h2>Methods</h2><p>Fixed workload.</p><h2>Results</h2><p>Figure 1 reports the outcomes.</p><figure><div><img src="plot.png"></div><figcaption>Figure 1: measured latency.</figcaption></figure></article>')
        self.assertIn("critical_figure_missing", r["issues"])
        self.assertIn("fulltext_required", r["issues"])
        self.assertTrue(any(b["kind"] == "caption" for b in r["blocks"]))

    def test_caption_alone_is_not_a_captured_critical_chart(self):
        r = extract_article('<article class="ltx_document"><h2>Methods</h2><p>Fixed workload.</p><h2>Results</h2><p>Figure 1 reports measured outcomes.</p><figure><figcaption>Figure 1: latency comparison.</figcaption></figure></article>')
        self.assertIn("critical_figure_missing", r["issues"])
        self.assertIn("fulltext_required", r["issues"])

    def test_login_prompt_with_title_is_still_a_shell(self):
        r = extract_article('<article><h1>A gated paper</h1><form>Sign in to read the full article</form></article>')
        self.assertEqual(r["text"], "")
        self.assertIn("login_required", r["issues"])

    def test_login_words_inside_full_argument_do_not_erase_it(self):
        r = extract_article('<article><h1>Access control</h1><p>A short prompt may say sign in to read the documentation.</p><p>The mechanism checks a session before resolving a protected object, and authorization still needs to be evaluated on every request regardless of transport reuse.</p><pre>check_permission(user, object_id)</pre></article>')
        self.assertIn("authorization still", r["text"])
        self.assertNotIn("login_required", r["issues"])

    def test_pdf_download_landing_page_requires_fulltext(self):
        r = extract_article('<main><h1>A paper</h1><p><a href="https://example.org/paper.pdf">Download full paper</a></p></main>')
        self.assertIn("fulltext_required", r["issues"])
        self.assertIn("pdf_fulltext_required", r["issues"])

    def test_pdf_citation_does_not_invalidate_complete_short_argument(self):
        r = extract_article('<article><p>Under fixed connection latency, batching shares setup cost among messages and yields L/n per message.</p><p><a href="https://example.org/paper.pdf">Supporting paper</a></p></article>')
        self.assertNotIn("fulltext_required", r["issues"])

    def test_nested_list_inside_layout_wrapper_survives(self):
        r = extract_article('<article><ul><li>Outer condition<div><ol><li>Nested measurement</li></ol></div></li></ul></article>')
        items = [b for b in r["blocks"] if b["kind"] == "list_item"]
        self.assertEqual([b["text"] for b in items], ["Outer condition", "Nested measurement"])
        self.assertEqual([b.get("depth") for b in items], [0, 1])

    def test_formula_structure_inside_table_cell_survives(self):
        r = extract_article('<article><table><tr><td>Cost</td><td><math><mfrac><mi>L</mi><mi>n</mi></mfrac></math></td></tr></table></article>')
        table = next(b for b in r["blocks"] if b["kind"] == "table")
        self.assertEqual(table["rows"], [["Cost", "(L)/(n)"]])
        self.assertTrue(any("<mfrac>" in f.get("mathml", "") for f in table.get("formulas", [])))


class TestReaderFetch(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("requests.sessions.Session.request", side_effect=AssertionError("offline test forbids network")).start()

    def fetch(self, html, **kwargs):
        from app.extract import fetch_article
        response = SimpleNamespace(text=html, content=html.encode("utf-8"), headers={"Content-Type": "text/html"})
        with patch("app.fetcher.http_get", return_value=response):
            return fetch_article("https://example.org/article", **kwargs)

    def test_fetch_keeps_short_argument_and_attaches_source_metadata(self):
        r = self.fetch('<article><p>One setup cost shared by n messages gives L/n under a fixed workload.</p></article>')
        self.assertIsNotNone(r)
        self.assertEqual(r.get("url"), "https://example.org/article")
        self.assertEqual(r.get("source_hash"), sha256(r["text"].encode("utf-8")).hexdigest())
        self.assertIsNotNone(datetime.fromisoformat(r["fetched_at"]).tzinfo)
        self.assertIsNone(r["published_at"])
        self.assertEqual(r.get("publication_status"), "unknown")

    def test_page_date_is_used_without_crawl_date_guessing(self):
        r = self.fetch('<html><head><meta content="2026-10-01T03:00:00+00:00" property="article:published_time"></head><body><article><p>A technical statement.</p></article></body></html>')
        self.assertIsNotNone(r)
        self.assertEqual(r.get("published_at"), "2026-10-01T03:00:00+00:00")
        self.assertNotEqual(r["published_at"], r["fetched_at"])

    def test_arxiv_date_metadata_is_normalized(self):
        r = self.fetch((READER_FIXTURES / "arxiv_full.html").read_text(encoding="utf-8"))
        self.assertIsNotNone(r)
        self.assertEqual(r.get("published_at"), "2026-10-01")

    def test_invalid_date_remains_unknown(self):
        r = self.fetch('<head><meta property="article:published_time" content="not-a-date"></head><article><p>Measured condition.</p></article>')
        self.assertIsNotNone(r)
        self.assertIsNone(r.get("published_at"))

    def test_publication_status_requires_metadata_support(self):
        r = self.fetch('<head><meta name="publication_status" content="technical_report"></head><article><p>A measured technical report.</p></article>')
        self.assertIsNotNone(r)
        self.assertEqual(r.get("publication_status"), "technical_report")

    def test_pdf_is_not_fed_to_html_parser(self):
        from app.extract import fetch_article
        from app import extract
        response = SimpleNamespace(content=b"%PDF-1.7\nnot HTML", text="%PDF-1.7", headers={"Content-Type": "application/pdf"})
        with patch("app.fetcher.http_get", return_value=response), patch.object(extract, "extract_article", side_effect=AssertionError("PDF is not HTML")):
            r = fetch_article("https://example.org/paper.pdf")
        self.assertIsNotNone(r)
        self.assertEqual(r["text"], "")
        self.assertIn("fulltext_required", r["issues"])
        self.assertEqual(r.get("method"), "pdf_unsupported")

    def test_pdf_magic_overrides_misleading_content_type(self):
        from app.extract import fetch_article
        response = SimpleNamespace(content=b"%PDF-1.7\nnot HTML", text="%PDF-1.7", headers={"Content-Type": "text/html"})
        with patch("app.fetcher.http_get", return_value=response):
            r = fetch_article("https://example.org/download")
        self.assertIsNotNone(r)
        self.assertIn("fulltext_required", r.get("issues", []))
        self.assertEqual(r.get("method"), "pdf_unsupported")

    def test_non_html_response_is_an_explicit_failure(self):
        from app.extract import fetch_article
        response = SimpleNamespace(content=b'{"message":"login"}', text='{"message":"login"}', headers={"Content-Type": "application/json"})
        with patch("app.fetcher.http_get", return_value=response):
            r = fetch_article("https://example.org/article")
        self.assertEqual(r["text"], "")
        self.assertIn("unsupported_content_type", r["issues"])


class TestFetchCache(unittest.TestCase):
    """运行内抓取缓存（2026-10-15）：预检与研究阶段共享，None 也缓存。"""

    def setUp(self):
        from app import extract
        extract.clear_fetch_cache()

    def test_same_url_fetched_once(self):
        from app import extract
        calls = []

        def fake(item):
            calls.append(item["url"])
            return {"text": "正文" * 10}

        with patch("app.extract.fetch_with_mirror", side_effect=fake):
            a = extract.fetch_cached({"url": "https://x.com/a", "title": "t"})
            b = extract.fetch_cached({"url": "https://x.com/a", "title": "t"})
        self.assertEqual(calls, ["https://x.com/a"])
        self.assertIs(a, b)

    def test_pool_url_key_preferred(self):
        from app import extract
        calls = []

        def fake(item):
            calls.append(item.get("_pool_url") or item["url"])
            return {"text": "正文"}

        with patch("app.extract.fetch_with_mirror", side_effect=fake):
            extract.fetch_cached({"url": "https://x.com/raw", "_pool_url": "https://x.com/norm"})
            extract.fetch_cached({"url": "https://x.com/raw", "_pool_url": "https://x.com/norm"})
        self.assertEqual(calls, ["https://x.com/norm"])

    def test_failed_fetch_not_retried_within_run(self):
        from app import extract
        with patch("app.extract.fetch_with_mirror", return_value=None) as m:
            extract.fetch_cached({"url": "https://x.com/f", "title": "t"})
            extract.fetch_cached({"url": "https://x.com/f", "title": "t"})
        self.assertEqual(m.call_count, 1)   # 同一运行内死候选不重复抓（None 也缓存）


if __name__ == "__main__":
    unittest.main()
