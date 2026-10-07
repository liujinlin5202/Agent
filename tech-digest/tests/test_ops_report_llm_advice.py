# -*- coding: utf-8 -*-
"""LLM 建议层单测：LLM 挂了也必须出报告（走模板兜底）。"""
import unittest

from ops_report import llm_advice, model


def make_report():
    digest = model.Section(key="digest", name="自动发帖机器人", status="ok",
                           metrics={"应发档期": "2 次", "发帖成功率": "100%"})
    assist = model.Section(key="assist", name="自动回复 AI", status="critical",
                           metrics={"请求成功率": "0%", "请求总数": "14577 次"},
                           notes=["pod 上应答进程不在：所有 /api/v1/assist/ 请求都会 502"])
    assist.errors = [model.ErrorCluster(signature="upstream prematurely closed connection",
                                        count=13223, first_ts="2026-09-18T21:30:00",
                                        last_ts="2026-09-20T21:30:00",
                                        sample="upstream prematurely closed connection",
                                        source="nginx")]
    return model.Report(generated_at="2026-09-20T21:30:00",
                        window_start="2026-09-18T21:30:00",
                        window_end="2026-09-20T21:30:00",
                        sections=[digest, assist])


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_metrics_and_errors(self):
        p = llm_advice.build_prompt(make_report())
        self.assertIn("14577", p)
        self.assertIn("upstream prematurely closed connection", p)
        self.assertIn("自动回复 AI", p)


class TestFallback(unittest.TestCase):
    def test_fallback_lists_status_and_notes(self):
        txt = llm_advice.fallback_text(model.Section(
            key="x", name="板块", status="critical", metrics={"请求成功率": "0%"},
            notes=["原因说明"]))
        self.assertIn("critical", txt)
        self.assertIn("请求成功率 0%", txt)
        self.assertIn("原因说明", txt)


class TestWriteAdvice(unittest.TestCase):
    def test_uses_llm_when_available(self):
        calls = []

        def fake_chat(prompt, system=None, max_tokens=0, timeout=0):
            calls.append(prompt)
            return "LLM 分析结论"

        rep = make_report()
        got = llm_advice.write_advice(rep, chat=fake_chat)
        self.assertEqual(got["assist"], "LLM 分析结论")
        self.assertEqual(len(calls), 1)          # 两次调用合并为一次，省 token
        self.assertTrue(any(a.origin == "llm" for a in rep.section("assist").advice))

    def test_falls_back_when_llm_returns_none(self):
        rep = make_report()
        got = llm_advice.write_advice(rep, chat=lambda *a, **k: None)
        self.assertIn("critical", got["assist"])         # 模板兜底仍有内容
        self.assertTrue(any(a.origin == "template" for a in rep.section("assist").advice))

    def test_falls_back_when_chat_raises(self):
        def boom(*a, **k):
            raise RuntimeError("network down")

        rep = make_report()
        got = llm_advice.write_advice(rep, chat=boom)
        self.assertTrue(got["digest"])
        self.assertTrue(got["assist"])


if __name__ == "__main__":
    unittest.main()
