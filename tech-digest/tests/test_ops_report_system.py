# -*- coding: utf-8 -*-
"""系统层采集单测：磁盘、证书、阈值判定。"""
import unittest
from datetime import datetime, timezone

from ops_report.collect import system


DF_OUT = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
          "/dev/vda1        221247488 175859712  34119680      82% /\n")


class TestParseDf(unittest.TestCase):
    def test_parses_capacity(self):
        self.assertEqual(system.parse_df(DF_OUT),
                         {"pct": 82, "used": "175859712", "size": "221247488"})

    def test_garbage_is_none(self):
        self.assertIsNone(system.parse_df(""))
        self.assertIsNone(system.parse_df("only one line"))


class TestParseEnddate(unittest.TestCase):
    def test_parses_gmt(self):
        dt = system.parse_enddate("notAfter=Nov 22 02:29:45 2026 GMT")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_garbage_is_none(self):
        self.assertIsNone(system.parse_enddate("nonsense"))


class TestBuildSection(unittest.TestCase):
    def test_disk_below_warn_is_ok(self):
        c = system.build_section({"pct": 50, "used": "1G", "size": "2G"}, None, 80, 90)
        self.assertEqual(c.section.status, "ok")
        self.assertEqual(c.section.metrics["磁盘使用率"], "50%")

    def test_disk_above_warn_is_warn(self):
        c = system.build_section({"pct": 82, "used": "168G", "size": "216G"}, None, 80, 90)
        self.assertEqual(c.section.status, "warn")

    def test_disk_above_crit_is_critical(self):
        c = system.build_section({"pct": 95, "used": "a", "size": "b"}, None, 80, 90)
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("磁盘" in n for n in c.section.notes))

    def test_cert_near_expiry_warns(self):
        cert = {"domain": "<MARKET_DOMAIN>", "expires": "2026-09-25T00:00:00+00:00", "days_left": 5}
        c = system.build_section({"pct": 10, "used": "a", "size": "b"}, cert, 80, 90)
        self.assertEqual(c.section.status, "warn")
        self.assertEqual(c.section.metrics["最近到期证书"], "<MARKET_DOMAIN>（剩 5 天）")

    def test_both_unavailable_is_unknown(self):
        c = system.build_section(None, None, 80, 90)
        self.assertEqual(c.section.status, "unknown")

    def test_inactive_timer_is_critical(self):
        """定时器没激活 = 那个任务根本不会跑，属于 critical，不是「提醒一下」。"""
        timers = {"tech-digest-daily.timer": {"state": "inactive", "last": "未触发"},
                  "tech-digest-star.timer": {"state": "active", "last": "2026-09-19 09:25:55"}}
        c = system.build_section({"pct": 10, "used": "a", "size": "b"}, None, 80, 90,
                                 timers=timers)
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("tech-digest-daily" in n for n in c.section.notes))

    def test_dead_timer_escalates_warn_to_critical(self):
        """磁盘已达提醒线（warn）+ 死 timer → 仍须 critical：死 timer 意味着任务根本不会跑，
        status 字段驱动 Report.overall，不能被磁盘提醒压成 warn。"""
        timers = {"tech-digest-daily.timer": {"state": "inactive", "last": "未触发"}}
        c = system.build_section({"pct": 82, "used": "a", "size": "b"}, None, 80, 90,
                                 timers=timers)
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("磁盘" in n for n in c.section.notes))
        self.assertTrue(any("tech-digest-daily" in n for n in c.section.notes))

    def test_all_timers_active_is_ok(self):
        timers = {"tech-digest-daily.timer": {"state": "active", "last": "2026-09-20 09:15:29"}}
        c = system.build_section({"pct": 10, "used": "a", "size": "b"}, None, 80, 90,
                                 timers=timers)
        self.assertEqual(c.section.status, "ok")
        self.assertIn("定时器", c.section.metrics)


class TestSystemctlShow(unittest.TestCase):
    class P:
        def __init__(self, out, rc=0):
            self.stdout = out
            self.returncode = rc

    def test_returns_value(self):
        got = system._systemctl_show("x.timer", "ActiveState",
                                     runner=lambda *a, **k: self.P("active\n"))
        self.assertEqual(got, "active")

    def test_nonzero_rc_is_none(self):
        got = system._systemctl_show("x.timer", "ActiveState",
                                     runner=lambda *a, **k: self.P("", rc=1))
        self.assertIsNone(got)

    def test_missing_systemctl_is_none(self):
        def boom(*a, **k):
            raise FileNotFoundError("systemctl not found")

        self.assertIsNone(system._systemctl_show("x.timer", "ActiveState", runner=boom))


if __name__ == "__main__":
    unittest.main()
