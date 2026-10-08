# -*- coding: utf-8 -*-
"""llmpool 单测（M2）：并行原语/串行默认/退避/异常隔离。

对应决策：M2 留底 D1（ThreadPool 伪并行，CONCURRENCY=1 严格串行）与
§5.5 退避契约（1/4/16s+抖动 → 备通道由 llm.chat 内部承接）。
"""
import threading
import unittest
from unittest.mock import patch

from app import llmpool


class TestChatWithBackoff(unittest.TestCase):
    def test_first_try_success_no_sleep(self):
        with patch.object(llmpool, "chat", return_value="ok") as c, \
             patch.object(llmpool.time, "sleep") as s:
            out = llmpool.chat_with_backoff("p", sleeper=s)
        self.assertEqual(out, "ok")
        self.assertEqual(c.call_count, 1)
        s.assert_not_called()

    def test_retries_then_backup_channel_via_chat(self):
        """chat 内部已含主备切换；退避只管「两通道皆空」的整轮重试。

        sleeper 必须显式传：它是定义期绑定的默认参数，patch time.sleep 拦不住。
        """
        calls = []

        def fake_chat(prompt, **kw):
            calls.append(1)
            return "recovered" if len(calls) >= 3 else None

        sleeps = []
        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.chat_with_backoff("p", sleeper=sleeps.append)
        self.assertEqual(out, "recovered")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)      # 第 1/2 次失败后各退避一次

    def test_all_fail_returns_none(self):
        with patch.object(llmpool, "chat", return_value=None), \
             patch.object(llmpool.time, "sleep"):
            out = llmpool.chat_with_backoff("p", attempts=3)
        self.assertIsNone(out)


class TestMapChat(unittest.TestCase):
    def test_concurrency_one_is_serial_and_ordered(self):
        order = []

        def fake_chat(prompt, **kw):
            order.append(prompt)
            return f"r:{prompt}"

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.map_chat([{"prompt": p} for p in "abc"], concurrency=1)
        self.assertEqual(out, ["r:a", "r:b", "r:c"])
        self.assertEqual(order, ["a", "b", "c"])   # 时间戳严格有序（伪并行=1 证据面）

    def test_concurrency_two_actually_parallel(self):
        """双任务互相等待对方到场：串行执行会死锁超时，真并发才能同时在场。"""
        gate = threading.Barrier(2, timeout=5)

        def fake_chat(prompt, **kw):
            gate.wait()                    # 两个调用必须同时在场
            return f"r:{prompt}"

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.map_chat([{"prompt": "a"}, {"prompt": "b"}],
                                   concurrency=2)
        self.assertEqual(out, ["r:a", "r:b"])

    def test_exception_isolated_per_task(self):
        def fake_chat(prompt, **kw):
            if prompt == "boom":
                raise RuntimeError("llm down")
            return f"r:{prompt}"

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.map_chat([{"prompt": "ok"}, {"prompt": "boom"}],
                                   concurrency=2)
        self.assertEqual(out, ["r:ok", None])

    def test_empty_tasks(self):
        self.assertEqual(llmpool.map_chat([]), [])


if __name__ == "__main__":
    unittest.main()
