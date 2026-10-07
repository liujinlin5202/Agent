# -*- coding: utf-8 -*-
"""llm 双通道单测：主通道 market-deploy → 失败自动切 DeepSeek（Anthropic Messages 格式）。

全部 mock app.llm.requests.post，不发真实网络请求。
"""
import unittest
from unittest import mock

from app import llm
from app.config import Settings


def resp(status=200, json_data=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = json_data or {}
    return r


class TestChatFallback(unittest.TestCase):
    def setUp(self):
        # 每个测试独立 settings，避免污染全局单例
        self.s = Settings()
        llm.settings = self.s

    def tearDown(self):
        from app.config import settings as global_settings
        llm.settings = global_settings

    def test_primary_ok_no_fallback(self):
        self.s.sse_market_api_key, self.s.deepseek_api_key = "sse-key", "ds-key"
        with mock.patch.object(llm.requests, "post",
                               return_value=resp(json_data={
                                   "choices": [{"message": {"content": "hi"}}]})) as p:
            out = llm.chat("ping")
        self.assertEqual(out, "hi")
        self.assertEqual(p.call_count, 1)  # 只调了主通道
        self.assertIn("chat/completions", p.call_args[0][0])

    def test_primary_fail_switch_deepseek(self):
        self.s.sse_market_api_key, self.s.deepseek_api_key = "sse-key", "ds-key"
        calls = []

        def fake_post(url, **kw):
            calls.append(url)
            if "market" in url:
                return resp(status=504)
            return resp(json_data={"content": [{"type": "text", "text": "backup"}]})

        with mock.patch.object(llm.requests, "post", side_effect=fake_post):
            out = llm.chat("ping")
        self.assertEqual(out, "backup")
        self.assertEqual(len(calls), 2)
        self.assertIn("market", calls[0])
        self.assertIn("deepseek.com", calls[1])

    def test_deepseek_wire_format(self):
        """备用通道报文必须是 Anthropic Messages 格式（x-api-key + 顶层 system）。"""
        self.s.sse_market_api_key, self.s.deepseek_api_key = "", "ds-key"
        with mock.patch.object(llm.requests, "post",
                               return_value=resp(json_data={
                                   "content": [{"type": "text", "text": "a"},
                                               {"type": "thinking", "thinking": "x"},
                                               {"type": "text", "text": "b"}]})) as p:
            out = llm.chat("ping", system="sys-prompt")
        self.assertEqual(out, "ab")  # 只拼 text 块
        url, kwargs = p.call_args[0][0], p.call_args[1]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(kwargs["headers"]["x-api-key"], "ds-key")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        payload = kwargs["json"]
        self.assertEqual(payload["model"], self.s.deepseek_model)
        self.assertEqual(payload["system"], "sys-prompt")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "ping"}])
        # 思考必须关闭：默认开思考时大 prompt 会把 max_tokens 烧在 thinking 块上
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("Authorization", kwargs["headers"])

    def test_primary_sends_openai_format(self):
        self.s.sse_market_api_key = "sse-key"
        with mock.patch.object(llm.requests, "post",
                               return_value=resp(json_data={
                                   "choices": [{"message": {"content": "hi"}}]})) as p:
            llm.chat("ping", system="sys-prompt")
        kwargs = p.call_args[1]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sse-key")
        msgs = kwargs["json"]["messages"]
        self.assertEqual(msgs[0], {"role": "system", "content": "sys-prompt"})
        self.assertEqual(msgs[1], {"role": "user", "content": "ping"})

    def test_both_fail_none(self):
        self.s.sse_market_api_key, self.s.deepseek_api_key = "sse-key", "ds-key"
        with mock.patch.object(llm.requests, "post",
                               return_value=resp(status=400, json_data={"error": {}})):
            self.assertIsNone(llm.chat("ping"))

    def test_deepseek_only_single_channel(self):
        self.s.sse_market_api_key, self.s.deepseek_api_key = "", "ds-key"
        with mock.patch.object(llm.requests, "post",
                               return_value=resp(json_data={
                                   "content": [{"type": "text", "text": "ok"}]})) as p:
            out = llm.chat("ping")
        self.assertEqual(out, "ok")
        self.assertEqual(p.call_count, 1)  # 无主通道 key 时不打主通道

    def test_no_keys_no_request(self):
        self.s.sse_market_api_key, self.s.deepseek_api_key = "", ""
        with mock.patch.object(llm.requests, "post") as p:
            self.assertIsNone(llm.chat("ping"))
        p.assert_not_called()

    def test_deepseek_empty_content_none(self):
        """content 全空 / 缺字段 → None（不把空串当成功）。"""
        self.s.sse_market_api_key, self.s.deepseek_api_key = "", "ds-key"
        for data in ({"content": []}, {"content": [{"type": "text", "text": "  "}]}, {}):
            with mock.patch.object(llm.requests, "post",
                                   return_value=resp(json_data=data)):
                self.assertIsNone(llm.chat("ping"))


class TestValidateEitherKey(unittest.TestCase):
    def test_need_ai_accepts_deepseek_only(self):
        s = Settings()
        s.sse_market_api_key, s.deepseek_api_key = "", "ds-key"
        s.validate(need_ai=True)  # 不应抛错

    def test_need_ai_accepts_sse_only(self):
        s = Settings()
        s.sse_market_api_key, s.deepseek_api_key = "sse-key", ""
        s.validate(need_ai=True)

    def test_need_ai_rejects_neither(self):
        s = Settings()
        s.sse_market_api_key, s.deepseek_api_key = "", ""
        with self.assertRaises(RuntimeError):
            s.validate(need_ai=True)


if __name__ == "__main__":
    unittest.main()
