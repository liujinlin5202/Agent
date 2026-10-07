# -*- coding: utf-8 -*-
"""编排层单测：守卫、降级、投递失败留痕——用注入的假采集器，不碰网络。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ops_report import __main__ as cli
from ops_report import config, model


def fake_collectors(section_status="ok"):
    def digest(settings, start, end):
        return model.Collected(model.Section(key="digest", name="自动发帖机器人",
                                             status=section_status,
                                             metrics={"发帖成功率": "100%"}))
    def assist(settings, start, end):
        return model.Collected(model.Section(key="assist", name="自动回复 AI",
                                             status="critical",
                                             metrics={"请求成功率": "0%"}))
    def system(settings):
        return model.Collected(model.Section(key="system", name="系统层", status="ok",
                                             metrics={"磁盘使用率": "82%"}))
    return {"digest": digest, "assist": assist, "system": system}


class TestBuildReport(unittest.TestCase):
    def test_builds_all_sections_and_advice(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            rep, advice = cli.build_report(
                s, now=datetime(2026, 9, 20, 21, 30),
                collectors=fake_collectors(), chat=lambda *a, **k: None)
            self.assertEqual([x.key for x in rep.sections], ["digest", "assist", "system"])
            self.assertEqual(rep.overall, "critical")
            self.assertTrue(advice["assist"])

    def test_collector_exception_degrades_to_unknown(self):
        cols = fake_collectors()

        def boom(settings, start, end):
            raise RuntimeError("采集炸了")

        cols["assist"] = boom
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            rep, _ = cli.build_report(s, now=datetime(2026, 9, 20, 21, 30),
                                      collectors=cols, chat=lambda *a, **k: None)
            sec = rep.section("assist")
            self.assertEqual(sec.status, "unknown")
            self.assertTrue(any("采集异常" in n for n in sec.notes))


class TestMainGuard(unittest.TestCase):
    def test_dry_run_writes_archive_and_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            rc = cli.main(["dry-run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None)
            self.assertEqual(rc, 0)
            self.assertTrue((Path(td) / "reports" / "2026-09-20.html").exists())
            self.assertFalse((Path(td) / "state.json").exists())

    def test_run_sends_and_marks_sent(self):
        sent = []
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td), smtp_user="a@163.com",
                                   smtp_auth_code="x", mail_to="b@163.com")
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None,
                          send=lambda *a, **k: (sent.append(a), (True, "已发送"))[1])
            self.assertEqual(rc, 0)
            self.assertEqual(len(sent), 1)
            self.assertEqual((Path(td) / "state.json").exists(), True)

    def test_run_skips_when_within_interval(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td))
            (Path(td) / "state.json").write_text('{"last_sent": "2026-09-20T21:30:00"}',
                                                 encoding="utf-8")
            called = []
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 21, 21, 30),
                          collectors=fake_collectors(),
                          send=lambda *a, **k: called.append(1))
            self.assertEqual(rc, 0)
            self.assertEqual(called, [])            # 守卫生效：没发
            self.assertFalse((Path(td) / "reports" / "2026-09-21.html").exists())

    def test_send_failure_writes_alert_and_returns_one(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td), smtp_user="a@163.com",
                                   smtp_auth_code="x", mail_to="b@163.com")
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None,
                          send=lambda *a, **k: (False, "认证失败"))
            self.assertEqual(rc, 1)
            alerts = list((Path(td) / "alerts").glob("*.txt"))
            self.assertEqual(len(alerts), 1)
            self.assertIn("认证失败", alerts[0].read_text(encoding="utf-8"))
            self.assertFalse((Path(td) / "state.json").exists())   # 未发成功不推进节奏

    def test_pending_alert_is_cleared_after_successful_send(self):
        with tempfile.TemporaryDirectory() as td:
            s = config.OpsSettings(data_dir=Path(td), smtp_user="a@163.com",
                                   smtp_auth_code="x", mail_to="b@163.com")
            d = Path(td) / "alerts"
            d.mkdir(parents=True)
            (d / "20260920-000000.txt").write_text("旧的失败", encoding="utf-8")
            rc = cli.main(["run"], settings=s, now=datetime(2026, 9, 20, 21, 30),
                          collectors=fake_collectors(), chat=lambda *a, **k: None,
                          send=lambda *a, **k: (True, "已发送"))
            self.assertEqual(rc, 0)
            self.assertEqual(list(d.glob("*.txt")), [])             # 送出去了就清掉


if __name__ == "__main__":
    unittest.main()
