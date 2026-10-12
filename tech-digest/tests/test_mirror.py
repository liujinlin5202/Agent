# -*- coding: utf-8 -*-
"""镜像通道单测：URL 识别、内容 API 变换、短文/错码/非文章页 fail-open。"""
import unittest
from datetime import datetime
from hashlib import sha256
from types import SimpleNamespace
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


class TestMirrorSourceMetadata(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("requests.sessions.Session.request", side_effect=AssertionError("offline test forbids network")).start()
        self.url = "https://wallstreetcn.com/articles/3781850"

    def test_legacy_mirror_attaches_metadata_without_claiming_structure(self):
        from app.extract import fetch_with_mirror
        legacy = {"text": "Measured body " * 100, "author": "Local Author"}
        with patch("app.fetcher.http_get", side_effect=RuntimeError("local unavailable response")), patch.object(mirror, "mirror_fetch", return_value=legacy):
            r = fetch_with_mirror({"url": self.url, "title": "offline sample", "published_at": "2026-10-10"})
        self.assertEqual(r.get("url"), self.url)
        self.assertEqual(r.get("source_hash"), sha256(legacy["text"].encode("utf-8")).hexdigest())
        self.assertIsNotNone(datetime.fromisoformat(r["fetched_at"]).tzinfo)
        self.assertIsNone(r["published_at"])
        self.assertEqual(r.get("publication_status"), "unknown")
        self.assertEqual(r.get("method"), "mirror")
        self.assertIn("structure_unverified", r.get("issues", []))
        self.assertTrue(r["blocks"])
        self.assertTrue(all(b["kind"] == "paragraph" for b in r["blocks"]))
        self.assertNotIn("source_hash", legacy)

    def test_real_mirror_path_works_from_offline_empty_shell(self):
        from app.extract import fetch_with_mirror
        response = SimpleNamespace(text='<html><body><div id="app"></div></body></html>', content=b"", headers={"Content-Type": "text/html"})
        payload = {"code": "20000", "data": {"content": _BODY, "author": "Ada"}}
        with patch("app.fetcher.http_get", return_value=response), patch.object(mirror, "http_get", return_value=_FakeResp(payload)):
            r = fetch_with_mirror({"url": self.url})
        self.assertEqual(r["author"], "Ada")
        self.assertIn("正文内容测试段落", r["text"])
        self.assertEqual(r.get("method"), "mirror")
        self.assertIn("structure_unverified", r.get("issues", []))

    def test_mirror_does_not_mask_invalid_capture_if_it_fails(self):
        from app.extract import fetch_with_mirror
        response = SimpleNamespace(text='<body><form>请登录后阅读</form></body>', content=b"", headers={"Content-Type": "text/html"})
        with patch("app.fetcher.http_get", return_value=response), patch.object(mirror, "mirror_fetch", return_value=None):
            r = fetch_with_mirror({"url": self.url})
        self.assertIsNotNone(r)
        self.assertIn("login_required", r["issues"])
        self.assertEqual(r["url"], self.url)

    def test_complete_html_uses_its_verified_structure(self):
        from app.extract import fetch_with_mirror
        html = '<article><p>Compare a fixed workload, then divide setup cost by n.</p></article>'
        response = SimpleNamespace(text=html, content=html.encode(), headers={"Content-Type": "text/html"})
        with patch("app.fetcher.http_get", return_value=response), patch.object(mirror, "mirror_fetch", side_effect=AssertionError("complete short body must be kept")):
            r = fetch_with_mirror({"url": self.url})
        self.assertIsNotNone(r)
        self.assertIn("divide setup cost", r["text"])
        self.assertNotEqual(r.get("method"), "mirror")
        self.assertNotIn("structure_unverified", r["issues"])

    def test_oversized_legacy_mirror_is_explicitly_truncated(self):
        from app import extract
        legacy = {"text": "m" * 80, "author": "Ada"}
        with patch("app.fetcher.http_get", side_effect=RuntimeError("offline unavailable")), patch.object(mirror, "mirror_fetch", return_value=legacy), patch.object(extract, "EXTRACT_CAP", 40):
            r = extract.fetch_with_mirror({"url": self.url})
        self.assertLessEqual(len(r["text"]), 40)
        self.assertIn("truncated", r["issues"])
        self.assertIn("fulltext_required", r["issues"])
        self.assertEqual(r["source_hash"], sha256(r["text"].encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
