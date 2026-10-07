# -*- coding: utf-8 -*-
"""ops_report 基础层单测：数据契约 + 配置加载。

契约测试的意义：collect / analyze / render 三层只通过这些字段名通信，
字段名一变三层同时坏——先钉住。
"""
import os
import tempfile
import unittest
from pathlib import Path

from ops_report import config, model


class TestModel(unittest.TestCase):
    def test_report_overall_picks_worst_section(self):
        r = model.Report(generated_at="t", window_start="a", window_end="b", sections=[
            model.Section(key="digest", name="发帖", status="ok"),
            model.Section(key="assist", name="应答", status="critical"),
            model.Section(key="system", name="系统", status="warn"),
        ])
        self.assertEqual(r.overall, "critical")

    def test_report_overall_unknown_beats_ok(self):
        r = model.Report(generated_at="t", window_start="a", window_end="b", sections=[
            model.Section(key="digest", name="发帖", status="ok"),
            model.Section(key="assist", name="应答", status="unknown"),
        ])
        self.assertEqual(r.overall, "unknown")

    def test_report_overall_empty_is_ok(self):
        r = model.Report(generated_at="t", window_start="a", window_end="b")
        self.assertEqual(r.overall, "ok")

    def test_collected_defaults(self):
        c = model.Collected(section=model.Section(key="x", name="y", status="ok"))
        self.assertEqual(c.raw_errors, [])


class TestConfig(unittest.TestCase):
    def test_dotenv_loads_without_overriding_existing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("OPS_TEST_A=from_file\nOPS_TEST_B=\"quoted\"\n"
                           "OPS_TEST_C='single'\n# 注释行\n", encoding="utf-8")
            for k in ("OPS_TEST_A", "OPS_TEST_B", "OPS_TEST_C"):
                os.environ.pop(k, None)
            os.environ["OPS_TEST_B"] = "from_env"
            try:
                config.load_env(env)
                self.assertEqual(os.environ["OPS_TEST_A"], "from_file")
                self.assertEqual(os.environ["OPS_TEST_B"], "from_env")   # 已存在不覆盖
                self.assertEqual(os.environ["OPS_TEST_C"], "single")     # 单引号被剥掉
            finally:
                for k in ("OPS_TEST_A", "OPS_TEST_B", "OPS_TEST_C"):
                    os.environ.pop(k, None)

    def test_defaults_when_no_env(self):
        s = config.OpsSettings()
        self.assertEqual(s.smtp_host, "smtp.163.com")
        self.assertEqual(s.smtp_port, 465)
        self.assertEqual(s.interval_days, 2)
        self.assertEqual(s.pod_ssh_port, 22012)
        self.assertEqual(s.market_db_name, "market")
        self.assertEqual(s.disk_crit_pct, 90)


if __name__ == "__main__":
    unittest.main()
