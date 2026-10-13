# -*- coding: utf-8 -*-
"""llmpool 单测（M2 + 2026-10-13 缺口③④修复）。

契约（M2 留底 D1 + 评审缺口③④）：
  · chat_with_backoff：主通道重试 ×attempts（退避 1/4/16s+抖动）→ 尽才落
    DeepSeek 备通道一次 → None。主通道重试期间绝不碰备通道；
  · map_chat 串行/并发两条路径都必须做单任务异常隔离（失败按 None 收敛）。
"""
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

    def test_primary_channel_only_during_retries(self):
        """缺口③：主通道重试期间绝不切备通道——所有重试调用必须 channel=primary。"""
        calls = []

        def fake_chat(prompt, **kw):
            calls.append(kw.get("channel"))
            return "from-primary" if len(calls) >= 3 else None

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.chat_with_backoff("p", sleeper=lambda s: None)
        self.assertEqual(out, "from-primary")
        self.assertEqual(calls, ["primary", "primary", "primary"])

    def test_backup_only_after_primary_exhausted(self):
        """退避烧尽才落备通道：前 N 次全 primary，最后一次 backup。"""
        calls = []

        def fake_chat(prompt, **kw):
            calls.append(kw.get("channel"))
            return None

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.chat_with_backoff("p", sleeper=lambda s: None)
        self.assertIsNone(out)
        self.assertEqual(calls, ["primary"] * llmpool.BACKOFF_RETRY_ATTEMPTS + ["backup"])

    def test_backoff_waits_reach_16s(self):
        """缺口③：默认重试必须走到写明的 16s 退避（1/4/16，含抖动区间）。"""
        waits = []
        with patch.object(llmpool, "chat", return_value=None):
            llmpool.chat_with_backoff("p", sleeper=waits.append)
        self.assertEqual(len(waits), 3)
        for w, base in zip(waits, llmpool.BACKOFF_STEPS):
            self.assertGreaterEqual(w, base)
            self.assertLessEqual(w, base * 1.25)

    def test_retries_then_recover(self):
        calls = []

        def fake_chat(prompt, **kw):
            calls.append(kw.get("channel"))
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
        import threading
        gate = threading.Barrier(2, timeout=5)

        def fake_chat(prompt, **kw):
            gate.wait()                    # 两个调用必须同时在场
            return f"r:{prompt}"

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.map_chat([{"prompt": "a"}, {"prompt": "b"}],
                                   concurrency=2)
        self.assertEqual(out, ["r:a", "r:b"])

    def test_exception_isolated_per_task_concurrent(self):
        def fake_chat(prompt, **kw):
            if prompt == "boom":
                raise RuntimeError("llm down")
            return f"r:{prompt}"

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.map_chat([{"prompt": "ok"}, {"prompt": "boom"}],
                                   concurrency=2)
        self.assertEqual(out, ["r:ok", None])

    def test_exception_isolated_per_task_serial(self):
        """缺口④：串行快捷路径同样要隔离单任务异常——一条炸不停整条编辑流程。"""
        def fake_chat(prompt, **kw):
            if prompt == "boom":
                raise RuntimeError("llm down")
            return f"r:{prompt}"

        with patch.object(llmpool, "chat", side_effect=fake_chat):
            out = llmpool.map_chat([{"prompt": "ok"}, {"prompt": "boom"},
                                    {"prompt": "ok2"}], concurrency=1)
        self.assertEqual(out, ["r:ok", None, "r:ok2"])

    def test_empty_tasks(self):
        self.assertEqual(llmpool.map_chat([]), [])


if __name__ == "__main__":
    unittest.main()
