# -*- coding: utf-8 -*-
"""发帖机器人采集单测（fixture 取自 2026-09-20 服务器真实 run_log）。"""
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ops_report.collect import digest


def row(ts, task, status, detail):
    return {"ts": ts, "task": task, "status": status, "detail": detail}


COLLECT_OK = row("2026-09-18T09:17:42", "daily", "ok",
                 {"source_stats": {"github-trending": {"fetched": 20},
                                   "hacker-news": {"fetched": 15}}})
PUBLISHED = row("2026-09-18T09:17:42", "daily", "ok",
                {"detail": "published", "post_id": 6291, "title": "Linux"})
WEEKEND_SKIP = row("2026-09-19T09:19:54", "daily", "skipped",
                   {"detail": "weekend", "day": "2026-09-19"})
STAR_PUBLISHED = row("2026-09-19T09:26:13", "star", "ok",
                     {"detail": "published", "post_id": 6311})
DEGRADED = row("2026-09-18T00:09:18", "daily", "degraded",
               {"detail": "publish", "error": "token 校验失败"})
DRY_RUN = row("2026-09-18T00:09:18", "daily", "ok", {"detail": "dry_run_token_ok"})
DUP_SKIP = row("2026-09-17T19:31:45", "daily", "skipped", {"detail": "publish_dup"})


class TestExpectedSlots(unittest.TestCase):
    def test_workday_weekend_mapping(self):
        # 2026-09-18 周五、09-19 周六、09-20 周日
        slots = digest.expected_slots(date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(slots, [
            {"task": "daily", "day": "2026-09-18"},
            {"task": "star", "day": "2026-09-19"},
            {"task": "weekly", "day": "2026-09-20"},
        ])

    def test_empty_when_end_before_start(self):
        self.assertEqual(digest.expected_slots(date(2026, 9, 20), date(2026, 9, 19)), [])


class TestOutcomeKind(unittest.TestCase):
    def test_published(self):
        self.assertEqual(digest.outcome_kind(PUBLISHED), "published")

    def test_expected_skips(self):
        self.assertEqual(digest.outcome_kind(WEEKEND_SKIP), "expected_skip")
        self.assertEqual(digest.outcome_kind(DUP_SKIP), "expected_skip")

    def test_degraded(self):
        self.assertEqual(digest.outcome_kind(DEGRADED), "degraded")

    def test_dry_run(self):
        self.assertEqual(digest.outcome_kind(DRY_RUN), "dry_run")


class TestBuildSection(unittest.TestCase):
    def test_success_rate_counts_only_publishing_slots(self):
        """窗口 09-18~09-20：应发 3 档（daily/star/weekly）；发了 2 次（daily+star）→ 67%。"""
        c = digest.build_section(
            [COLLECT_OK, PUBLISHED, WEEKEND_SKIP, STAR_PUBLISHED],
            [], date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(c.section.metrics["应发档期"], "3 次")
        self.assertEqual(c.section.metrics["实际发布"], "2 次")
        self.assertEqual(c.section.metrics["发帖成功率"], "67%")
        self.assertEqual(c.section.status, "warn")

    def test_all_published_is_ok(self):
        c = digest.build_section(
            [PUBLISHED, STAR_PUBLISHED, row("2026-09-20T10:03:31", "weekly", "ok",
                                            {"detail": "published", "post_id": 6322})],
            [], date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(c.section.metrics["发帖成功率"], "100%")
        self.assertEqual(c.section.status, "ok")

    def test_no_rows_at_all_is_critical(self):
        """整段没有任何运行记录（服务/定时器死了）→ 必须报 critical，不能报 unknown。"""
        c = digest.build_section([], [], date(2026, 9, 18), date(2026, 9, 20))
        self.assertEqual(c.section.status, "critical")
        self.assertTrue(any("没有任何运行记录" in n for n in c.section.notes))

    def test_degraded_marks_warn(self):
        c = digest.build_section([PUBLISHED, DEGRADED], [], date(2026, 9, 18),
                                 date(2026, 9, 18))
        self.assertEqual(c.section.metrics["降级"], "1 次")
        self.assertEqual(c.section.status, "warn")

    def test_expected_skip_not_counted_as_failure(self):
        """周末跳过是设计行为，不能算失败——否则每周都误报。"""
        c = digest.build_section([WEEKEND_SKIP], [], date(2026, 9, 19), date(2026, 9, 19))
        self.assertEqual(c.section.metrics["实际发布"], "0 次")
        self.assertIn("预期跳过", c.section.metrics)

    def test_source_health_listed(self):
        c = digest.build_section([COLLECT_OK, PUBLISHED], [], date(2026, 9, 18),
                                 date(2026, 9, 18))
        self.assertIn("github-trending: 20 条", c.section.metrics["数据源"])


class TestReadRunLog(unittest.TestCase):
    def test_reads_only_and_missing_file_is_empty(self):
        self.assertEqual(digest.read_run_log(Path("/nonexistent/x.db"), "2026-01-01"), [])
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE run_log(id INTEGER PRIMARY KEY, ts TEXT, task TEXT,"
                         " status TEXT, detail TEXT)")
            conn.execute("INSERT INTO run_log(ts,task,status,detail) VALUES"
                         "('2026-09-18T09:17:42','daily','ok','{\"detail\": \"published\"}')")
            conn.commit()
            conn.close()
            rows = digest.read_run_log(db, "2026-09-01")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["detail"]["detail"], "published")
            # 只读：写入必须失败
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO run_log(ts,task,status,detail)"
                             " VALUES('x','x','x','{}')")
            conn.close()

    def test_filters_by_since(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE run_log(id INTEGER PRIMARY KEY, ts TEXT, task TEXT,"
                         " status TEXT, detail TEXT)")
            conn.execute("INSERT INTO run_log(ts,task,status,detail)"
                         " VALUES('2026-09-01T09:00:00','daily','ok','{}')")
            conn.commit()
            conn.close()
            self.assertEqual(digest.read_run_log(db, "2026-09-10"), [])


class TestParseLogErrors(unittest.TestCase):
    def test_extracts_warning_and_error_after_since(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.log"
            p.write_text(
                "2026-09-17 18:44:09,523 WARNING 原文抓取失败（某文）→ 降级导读: HTTP 403\n"
                "2026-09-18 09:17:42,000 INFO daily ok: issue=#21\n"
                "2026-09-19 09:26:13,823 INFO star 已发布\n"
                "2026-09-20 09:15:30,000 ERROR 发帖第 3 次失败: boom\n",
                encoding="utf-8")
            got = digest.parse_log_errors(p, datetime(2026, 9, 18, 0, 0))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["level"], "ERROR")
            self.assertIn("发帖第 3 次失败", got[0]["text"])

    def test_missing_file_is_empty(self):
        self.assertEqual(digest.parse_log_errors(Path("/nope/x.log"),
                                                 datetime(2026, 1, 1)), [])


class TestCollect(unittest.TestCase):
    def test_db_read_failure_is_unknown_with_real_cause(self):
        """库被锁/损坏 ≠ 机器人停了：必须报 unknown + 真实原因，不能误报 critical。"""
        s = SimpleNamespace(digest_db=Path("x.db"), digest_log=Path("x.log"))
        with mock.patch.object(digest, "read_run_log",
                               side_effect=sqlite3.OperationalError("database is locked")):
            c = digest.collect(s, datetime(2026, 9, 18), datetime(2026, 9, 20))
        self.assertEqual(c.section.status, "unknown")
        self.assertEqual(c.section.metrics, {})
        self.assertTrue(any("run_log 读取失败" in n for n in c.section.notes))
        self.assertFalse(any("没有任何运行记录" in n for n in c.section.notes))

    def test_unreadable_log_does_not_break_section(self):
        """日志不可读不能炸掉整个板块：不抛、状态不受影响、note 说明日志读取失败。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE run_log(id INTEGER PRIMARY KEY, ts TEXT, task TEXT,"
                         " status TEXT, detail TEXT)")
            conn.execute("INSERT INTO run_log(ts,task,status,detail) VALUES"
                         "('2026-09-18T09:17:42','daily','ok',"
                         "'{\"detail\": \"published\"}')")
            conn.commit()
            conn.close()
            s = SimpleNamespace(digest_db=db, digest_log=Path(td))  # 目录当日志 → OSError
            c = digest.collect(s, datetime(2026, 9, 18), datetime(2026, 9, 18))
            self.assertEqual(c.section.status, "ok")   # 状态只由 run_log 决定
            self.assertTrue(any("日志文件读取失败" in n for n in c.section.notes))
            self.assertEqual(c.raw_errors, [])


if __name__ == "__main__":
    unittest.main()
