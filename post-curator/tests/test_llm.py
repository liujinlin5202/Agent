# -*- coding: utf-8 -*-
"""LLM 客户端单测：响应解析 / 失败收敛 / 直连（trust_env=False）形状。"""
import unittest
from unittest import mock

from app import llm


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError()
        return self._body


def _body(content):
    return {"choices": [{"message": {"content": content}}]}


class TestChat(unittest.TestCase):
    @mock.patch("app.llm._post")
    def test_success(self, post):
        post.return_value = FakeResp(200, _body('{"ok": 1}'))
        out = llm.chat("p", "s", "key", "http://g/v1/llm", "m")
        self.assertEqual(out, '{"ok": 1}')
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], "m")
        self.assertEqual(kwargs["json"]["reasoning_effort"], "none")
        self.assertEqual(kwargs["json"]["messages"][0]["role"], "system")
        self.assertTrue(post.call_args[0][0].endswith("/chat/completions"))

    @mock.patch("app.llm._post")
    def test_http_error_none(self, post):
        post.return_value = FakeResp(429)
        self.assertIsNone(llm.chat("p", None, "key", "http://g", "m"))

    @mock.patch("app.llm._post")
    def test_empty_content_none(self, post):
        post.return_value = FakeResp(200, _body(""))
        self.assertIsNone(llm.chat("p", None, "key", "http://g", "m"))

    @mock.patch("app.llm._post")
    def test_network_error_none(self, post):
        import requests as _rq
        post.side_effect = _rq.ConnectionError("x")
        self.assertIsNone(llm.chat("p", None, "key", "http://g", "m"))

    def test_session_trust_env_off(self):
        s = llm.requests.Session()
        s.trust_env = False
        self.assertFalse(s.trust_env)
        # 回归锚：requests.post 根本不收 trust_env（TypeError），
        # 必须经 Session 属性设置——这里锁定 _post 的实现路径
        with mock.patch.object(llm.requests, "Session") as S:
            llm._post("http://x", json={})
            self.assertFalse(S.return_value.trust_env)


if __name__ == "__main__":
    unittest.main()
