# -*- coding: utf-8 -*-
"""镜像通道单测：URL 识别、内容 API 变换、短文/错码/非文章页 fail-open。"""
import unittest
from unittest.mock import patch

from app import mirror

_BODY = "<p>" + "正文内容测试段落。" * 100 + "</p>"   # 提纯后远超 500 字符


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class TestNeedsMirror(unittest.TestCase):
    def test_wallstreetcn_article_true(self):
        self.assertTrue(mirror.needs_mirror("https://wallstreetcn.com/articles/3781850"))

    def test_other_surfaces_false(self):
        for u in ("https://zhuanlan.zhihu.com/p/1",
                  "https://mp.weixin.qq.com/s/abc",
                  "https://wallstreetcn.com/live/1",   # 非 articles 路径
                  ""):
            self.assertFalse(mirror.needs_mirror(u))


class TestMirrorFetch(unittest.TestCase):
    def test_returns_text_and_author(self):
        payload = {"code": "20000",
                   "data": {"content": _BODY, "author": {"display_name": "刘胜与"}}}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)) as g:
            r = mirror.mirror_fetch("https://wallstreetcn.com/articles/3781850")
        self.assertIn("正文内容测试段落", r["text"])
        self.assertEqual(r["author"], "刘胜与")
        self.assertIn("3781850", g.call_args[0][0])

    def test_author_plain_string(self):
        payload = {"code": "20000", "data": {"content": _BODY, "author": "张三"}}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            r = mirror.mirror_fetch("https://wallstreetcn.com/articles/1")
        self.assertEqual(r["author"], "张三")

    def test_short_content_returns_none(self):
        payload = {"code": "20000", "data": {"content": "<p>太短</p>", "author": ""}}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            self.assertIsNone(mirror.mirror_fetch("https://wallstreetcn.com/articles/1"))

    def test_bad_code_returns_none(self):
        payload = {"code": "40400", "data": None}
        with patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            self.assertIsNone(mirror.mirror_fetch("https://wallstreetcn.com/articles/1"))

    def test_non_article_url_returns_none_without_http(self):
        with patch.object(mirror, "http_get") as g:
            self.assertIsNone(mirror.mirror_fetch("https://x.com/1"))
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
