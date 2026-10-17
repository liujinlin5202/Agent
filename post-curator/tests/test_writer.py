# -*- coding: utf-8 -*-
"""写通道单测：请求形状 / 响应包判定 / fail-closed（mock，不发真请求）。"""
import unittest
from unittest import mock

from app import writer


class FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class TestWriter(unittest.TestCase):
    def test_empty_ids_noop(self):
        ok, msg = writer.mark_featured([], "http://x", "tok")
        self.assertTrue(ok)
        self.assertIn("无待加精", msg)

    def test_missing_token_fail_closed(self):
        ok, msg = writer.mark_featured([1], "http://x", "")
        self.assertFalse(ok)
        self.assertIn("fail-closed", msg)

    @mock.patch("app.writer._post")
    def test_success(self, post):
        post.return_value = FakeResp(200, {"code": 200, "data": {"marked": 2}, "msg": "ok"})
        ok, msg = writer.mark_featured([1, 2], "http://x:8080", "tok", timeout=7)
        self.assertTrue(ok)
        self.assertIn("2", msg)
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"], {"post_ids": [1, 2]})
        self.assertEqual(kwargs["headers"]["X-Internal-Token"], "tok")
        self.assertEqual(kwargs["timeout"], 7)

    @mock.patch("app.writer._post")
    def test_chunk_over_500(self, post):
        post.return_value = FakeResp(200, {"code": 200, "data": {"marked": 3}, "msg": "ok"})
        ids = list(range(1200))
        ok, msg = writer.mark_featured(ids, "http://x", "tok")
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 3)  # 500+500+200
        sizes = [len(post.call_args_list[i][1]["json"]["post_ids"]) for i in range(3)]
        self.assertEqual(sizes, [500, 500, 200])
        self.assertIn("9", msg)  # marked 合计=各批之和 3×3

    @mock.patch("app.writer._post")
    def test_token_guard_403(self, post):
        post.return_value = FakeResp(403, None, text="forbidden")
        ok, msg = writer.mark_featured([1], "http://x", "bad")
        self.assertFalse(ok)
        self.assertIn("403", msg)

    @mock.patch("app.writer._post")
    def test_business_fail(self, post):
        post.return_value = FakeResp(200, {"code": 400, "data": None, "msg": "参数缺失"})
        ok, msg = writer.mark_featured([1], "http://x", "tok")
        self.assertFalse(ok)
        self.assertIn("参数缺失", msg)

    @mock.patch("app.writer._post")
    def test_network_error(self, post):
        import requests as _rq
        post.side_effect = _rq.ConnectionError("refused")
        ok, msg = writer.mark_featured([1], "http://x", "tok")
        self.assertFalse(ok)
        self.assertIn("请求异常", msg)


if __name__ == "__main__":
    unittest.main()
