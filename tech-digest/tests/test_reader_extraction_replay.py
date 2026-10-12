"""Minimal real-container regression; no copied third-party article text."""
import unittest
from app.extract import extract_article


class TestVerifiedBodyInLayout(unittest.TestCase):
    def test_fcc_body_survives_ad_layout_and_sidebar_wrapper(self):
        html = '''<html><body><main><div class="inner ad-layout"><article>
          <section class="post-full-content"><div class="post-and-sidebar">
          <aside>侧栏广告</aside><section class="post-content">
          <h2>方法</h2><p>固定输入，再改变缓存是否命中，比较重复访问开销。</p>
          <div class="advertisement">正文内广告</div>
          <h2>限制</h2><p>输入发生变化时，旧缓存不能作为有效结果。</p>
          </section></div></section></article></div></main></body></html>'''
        result = extract_article(html)
        self.assertIn("固定输入", result["text"])
        self.assertIn("输入发生变化", result["text"])
        self.assertNotIn("广告", result["text"])
        self.assertEqual(result["method"], "site_selector")

    def test_verified_class_cannot_unhide_content_or_execute_scripts(self):
        for wrapper in ('<div hidden>{}</div>', '<aside>{}</aside>', '<form>{}</form>'):
            html = wrapper.format('<section class="post-content"><p>不能重新显露这段。</p></section>')
            self.assertNotIn("不能重新显露", extract_article(html)["text"])
