# -*- coding: utf-8 -*-
"""离线复盘测试：时间窗口、同快照比率、小样本与 SQLite 零写入。"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app import report


def s(day, sample_day, browse, likes=0, comments=0, heat=0.0, post_id=None):
    if post_id is None:
        post_id = int(day.replace("-", "")) if day.replace("-", "").isdigit() else 1
    return {"post_id": post_id, "day": day, "sample_day": sample_day,
            "sampled_at": f"{sample_day}T09:15:00", "browse": browse,
            "likes": likes, "comments": comments, "heat": heat}


def timed(sampled_at, browse=10, likes=1, comments=2, day="2026-10-09", post_id=1):
    snap = s(day, sampled_at[:10], browse, likes, comments, post_id=post_id)
    snap["sampled_at"] = sampled_at
    return snap


def metadata(published_at="2026-10-09T09:00:00+00:00", time_basis="local_confirmation"):
    return {1: {"published_at": published_at, "time_basis": time_basis,
                "content_version": "research-v1", "article_type": "research",
                "learning_question": "为什么该方法有效？"}}


class TestReportRows(unittest.TestCase):
    def test_separate_posts_on_same_day(self):
        rows = report.report_rows([
            s("2026-09-13", "2026-09-14", 10, post_id=1),
            s("2026-09-13", "2026-09-14", 30, post_id=2),
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["post_id"] for r in rows], [1, 2])
        self.assertEqual([r["h24"] for r in rows], [10, 30])

    def test_h24_from_next_day_sample(self):
        rows = report.report_rows([s("2026-09-13", "2026-09-14", 58),
                                   s("2026-09-13", "2026-09-15", 64)])
        self.assertEqual(rows[0]["h24"], 58)
        self.assertEqual(rows[0]["browse"], 64)      # 当前 = 最新一次采样

    def test_missing_h24_is_none_not_zero(self):
        """没测到 ≠ 零浏览：缺次日采样必须留空，否则复盘表会显示成「没人看」。"""
        rows = report.report_rows([s("2026-09-13", "2026-09-15", 64)])
        self.assertIsNone(rows[0]["h24"])

    def test_weekday_and_theme(self):
        rows = report.report_rows([s("2026-09-14", "2026-09-15", 10)])   # 周一
        self.assertEqual(rows[0]["weekday"], "周一")
        self.assertEqual(rows[0]["theme"], "工具与资源")
        free = report.report_rows([s("2026-09-15", "2026-09-16", 10)])   # 周二：自由日
        self.assertEqual(free[0]["theme"], "")

    def test_sorted_by_day(self):
        rows = report.report_rows([s("2026-09-15", "2026-09-16", 1),
                                   s("2026-09-13", "2026-09-14", 2)])
        self.assertEqual([r["day"] for r in rows], ["2026-09-13", "2026-09-15"])

    def test_titles_attached(self):
        rows = report.report_rows([s("2026-09-13", "2026-09-14", 58)],
                                  titles={"2026-09-13": "显卡要变天？"})
        self.assertEqual(rows[0]["title"], "显卡要变天？")
        self.assertEqual(report.report_rows([s("2026-09-13", "2026-09-14", 58)])[0]["title"], "")

    def test_empty(self):
        self.assertEqual(report.report_rows([]), [])

    def test_same_post_is_not_double_counted_when_sample_day_label_changes(self):
        rows = report.report_rows([
            s("2026-10-09", "2026-10-10", 10, post_id=1),
            s("2026-10-10", "2026-10-11", 20, post_id=1),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["day"], "2026-10-09")
        self.assertEqual(rows[0]["h24"], 10)
        self.assertEqual(rows[0]["browse"], 20)

    def test_bad_day_does_not_raise(self):
        rows = report.report_rows([s("bad-day", "2026-09-14", 5)])
        self.assertEqual(rows[0]["h24"], None)
        self.assertEqual(rows[0]["weekday"], "")


class TestObservationWindow(unittest.TestCase):
    def test_offsets_are_compared_as_instants(self):
        row = report.report_rows([timed("2026-10-10T17:00:00+08:00")],
                                 post_meta=metadata("2026-10-09T09:00:00Z"))[0]
        self.assertEqual(row["h24"], 10)
        self.assertEqual(row["observation_age_hours"], 24.0)
        self.assertEqual(row["sample_basis"], "h24_window")
        self.assertEqual(row["content_version"], "research-v1")
        self.assertEqual(row["time_basis"], "local_confirmation")
        self.assertEqual(row["article_type"], "research")
        self.assertEqual(row["learning_question"], "为什么该方法有效？")

    def test_window_boundaries_are_inclusive(self):
        for stamp, expected, age in [
            ("2026-10-10T05:59:59+00:00", None, None),
            ("2026-10-10T06:00:00+00:00", 10, 21.0),
            ("2026-10-10T12:00:00+00:00", 10, 27.0),
            ("2026-10-10T12:00:01+00:00", None, None),
        ]:
            with self.subTest(stamp=stamp):
                row = report.report_rows([timed(stamp)], post_meta=metadata())[0]
                self.assertEqual(row["h24"], expected)
                self.assertEqual(row["observation_age_hours"], age)
                self.assertEqual(row["sample_basis"], "missing" if age is None else "h24_window")

    def test_closest_to_24h_wins_even_on_publication_day(self):
        row = report.report_rows([
            timed("2026-10-10T03:30:00+00:00", browse=100),
            timed("2026-10-09T23:30:00+00:00", browse=20),
        ], post_meta=metadata("2026-10-09T00:30:00+00:00"))[0]
        self.assertEqual(row["h24"], 20)
        self.assertEqual(row["browse"], 100)
        self.assertEqual(row["observation_age_hours"], 23.0)

    def test_friday_to_monday_is_missing_not_24h(self):
        row = report.report_rows([timed("2026-10-12T09:00:00+00:00", browse=72)],
                                 post_meta=metadata())[0]
        self.assertIsNone(row["h24"])
        self.assertIsNone(row["observation_age_hours"])
        self.assertEqual(row["sample_basis"], "missing")
        self.assertEqual(row["browse"], 72)
        self.assertEqual(row["current_observation_age_hours"], 72.0)

    def test_naive_or_missing_publication_is_next_day_approximate(self):
        for pub in (None, "2026-10-09T09:00:00", "not-a-timestamp"):
            with self.subTest(pub=pub):
                row = report.report_rows([timed("2026-10-10T09:00:00+00:00")],
                                         post_meta=metadata(pub))[0]
                self.assertEqual(row["h24"], 10)
                self.assertEqual(row["sample_basis"], "next_day_approximate")
                self.assertIsNone(row["observation_age_hours"])

    def test_naive_or_missing_sample_time_is_next_day_approximate(self):
        for stamp in (None, "2026-10-10T09:00:00", "invalid"):
            with self.subTest(stamp=stamp):
                snap = s("2026-10-09", "2026-10-10", 10, post_id=1)
                snap["sampled_at"] = stamp
                row = report.report_rows([snap], post_meta=metadata())[0]
                self.assertEqual(row["h24"], 10)
                self.assertEqual(row["sample_basis"], "next_day_approximate")
                self.assertIsNone(row["observation_age_hours"])

    def test_aware_next_day_outside_window_never_falls_back(self):
        row = report.report_rows([timed("2026-10-10T09:00:00+00:00", browse=99)],
                                 post_meta=metadata("2026-10-09T23:00:00+00:00"))[0]
        self.assertIsNone(row["h24"])
        self.assertEqual(row["sample_basis"], "missing")

    def test_legacy_has_no_invented_version_or_precision(self):
        row = report.report_rows([s("2026-10-09", "2026-10-10", 10)])[0]
        self.assertEqual(row["sample_basis"], "next_day_approximate")
        self.assertIsNone(row["observation_age_hours"])
        self.assertIsNone(row["content_version"])

    def test_ratios_use_selected_snapshot_and_keep_current_totals(self):
        row = report.report_rows([
            timed("2026-10-10T09:00:00+00:00", browse=10, likes=1, comments=2),
            timed("2026-10-11T09:00:00+00:00", browse=100, likes=40, comments=50),
        ], post_meta=metadata())[0]
        self.assertEqual((row["h24"], row["h24_likes"], row["h24_comments"]), (10, 1, 2))
        self.assertEqual((row["browse"], row["likes"], row["comments"]), (100, 40, 50))
        self.assertAlmostEqual(row["like_rate"], 0.1)
        self.assertAlmostEqual(row["comment_rate"], 0.2)

    def test_zero_views_keep_zero_counts_and_undefined_ratios(self):
        row = report.report_rows([timed("2026-10-10T09:00:00+00:00", browse=0,
                                       likes=0, comments=0)], post_meta=metadata())[0]
        self.assertEqual(row["h24"], 0)
        self.assertEqual(row["h24_likes"], 0)
        self.assertIsNone(row["like_rate"])
        self.assertIsNone(row["comment_rate"])

    def test_missing_window_keeps_snapshot_counts_and_ratios_missing(self):
        row = report.report_rows([timed("2026-10-12T09:00:00+00:00")],
                                 post_meta=metadata())[0]
        for key in ("h24", "h24_likes", "h24_comments", "like_rate", "comment_rate"):
            self.assertIsNone(row[key], key)

    def test_same_post_sample_day_last_record_overwrites(self):
        row = report.report_rows([
            timed("2026-10-10T06:00:00+00:00", browse=10),
            timed("2026-10-10T09:00:00+00:00", browse=20),
        ], post_meta=metadata())[0]
        self.assertEqual(row["h24"], 20)
        self.assertEqual(row["observation_age_hours"], 24.0)

    def test_titles_by_post_id_take_precedence_over_day(self):
        rows = report.report_rows([
            s("2026-10-09", "2026-10-10", 10, post_id=1),
            s("2026-10-09", "2026-10-10", 20, post_id=2),
        ], titles={1: "第一篇", 2: "第二篇", "2026-10-09": "日期标题"})
        self.assertEqual([row["title"] for row in rows], ["第一篇", "第二篇"])

    def test_current_snapshot_uses_instants_not_offset_text_order(self):
        row = report.report_rows([
            timed("2026-10-10T23:00:00-10:00", browse=50),
            timed("2026-10-11T01:00:00+14:00", browse=20),
        ], post_meta=metadata())[0]
        self.assertEqual(row["browse"], 50)


class TestRenderTable(unittest.TestCase):
    def test_renders_rows(self):
        md = report.render_table(report.report_rows(
            [s("2026-09-13", "2026-09-14", 58, likes=1, comments=2),
             s("2026-09-13", "2026-09-15", 64, likes=1, comments=3)],
            titles={"2026-09-13": "显卡要变天？"}))
        self.assertIn("当前浏览次数", md)
        self.assertIn("同窗口赞/浏览", md)
        self.assertIn("显卡要变天？", md)
        self.assertIn("次日近似", md)
        self.assertIn("1/58", md)
        self.assertIn("2/58", md)
        self.assertIn("| 64 | 1 | 3 |", md)

    def test_exposes_version_actual_age_and_local_time_basis(self):
        md = report.render_table(report.report_rows(
            [timed("2026-10-10T09:00:00+00:00")], post_meta=metadata()))
        for text in ("research-v1", "research", "为什么该方法有效？", "24.0", "21–27h 窗口",
                     "本地确认", "曝光：不可用", "读完率：不可用"):
            self.assertIn(text, md)

    def test_missing_h24_rendered_as_dash(self):
        md = report.render_table(report.report_rows([s("2026-09-13", "2026-09-15", 64)]))
        self.assertIn("| — | 64 |", md)

    def test_escapes_pipe_in_title(self):
        md = report.render_table(report.report_rows(
            [s("2026-09-13", "2026-09-14", 5)], titles={"2026-09-13": "a|b"}))
        self.assertNotIn("a|b", md)

    def test_empty_message(self):
        self.assertIn("暂无", report.render_table([]))


class TestSummary(unittest.TestCase):
    """缺失不填零；不同口径分组；均值、中位数与原始计数共同呈现。"""

    def test_compares_two_windows(self):
        rows = report.report_rows([
            s("2026-09-01", "2026-09-02", 100), s("2026-09-02", "2026-09-03", 120),
            s("2026-09-08", "2026-09-09", 50), s("2026-09-09", "2026-09-10", 60),
        ])
        line = report.summary(rows, split="2026-09-05")
        self.assertIn("改版前", line)
        self.assertIn("改版后", line)
        self.assertIn("均值=110.0", line)
        self.assertIn("中位数=110.0", line)
        self.assertIn("均值=55.0", line)
        self.assertIn("N=2", line)
        self.assertIn("浏览总数=220", line)
        self.assertIn("浏览总数=110", line)
        self.assertIn("仅作描述", line)
        self.assertNotIn("↓50%", line)

    def test_one_post_reports_raw_counts_without_growth_claim(self):
        rows = report.report_rows([timed("2026-10-10T09:00:00+00:00")], post_meta=metadata())
        line = report.summary(rows, split="2026-10-09")
        for text in ("N=1", "均值=10.0", "中位数=10.0", "赞/浏览=1/10", "评/浏览=2/10",
                     "样本不足", "曝光：不可用", "CTR：不可用", "读完率：不可用"):
            self.assertIn(text, line)

    def test_versions_and_sample_bases_have_separate_summaries(self):
        meta = metadata()
        meta[2] = {**meta[1], "content_version": "reader-v1"}
        rows = report.report_rows([
            timed("2026-10-10T09:00:00+00:00", post_id=1, browse=10),
            timed("2026-10-10T09:00:00+00:00", post_id=2, browse=90),
            s("2026-10-09", "2026-10-10", 1000, post_id=3),
        ], post_meta=meta)
        line = report.summary(rows, split="2026-10-09")
        self.assertIn("research-v1", line)
        self.assertIn("reader-v1", line)
        self.assertIn("未记录", line)
        self.assertIn("均值=10.0", line)
        self.assertIn("均值=90.0", line)
        self.assertIn("均值=1000.0", line)
        self.assertNotIn("均值=366.7", line)

    def test_zero_denominator_is_unavailable_with_raw_counts(self):
        rows = report.report_rows([s("2026-10-09", "2026-10-10", 0)])
        line = report.summary(rows, split="2026-10-09")
        self.assertIn("赞/浏览=0/0（不可用）", line)
        self.assertIn("N=1", line)

    def test_missing_rows_count_as_posts_not_measured_zeroes(self):
        rows = report.report_rows([s("2026-10-09", "2026-10-12", 999)])
        line = report.summary(rows, split="2026-10-09")
        self.assertIn("篇数=1", line)
        self.assertIn("N=0", line)
        self.assertIn("均值=—", line)

    def test_ignores_rows_without_h24(self):
        rows = report.report_rows([s("2026-09-01", "2026-09-03", 999)])
        self.assertIn("样本不足", report.summary(rows, split="2026-09-05"))

    def test_outlier_does_not_replace_median_with_mean(self):
        rows = report.report_rows([
            s("2026-10-07", "2026-10-08", 1),
            s("2026-10-08", "2026-10-09", 2),
            s("2026-10-09", "2026-10-10", 100),
        ])
        line = report.summary(rows, split="2026-10-07")
        self.assertIn("N=3", line)
        self.assertIn("均值=34.3", line)
        self.assertIn("中位数=2.0", line)
        self.assertIn("浏览样本=[1, 2, 100]", line)

    def test_handles_no_baseline(self):
        rows = report.report_rows([s("2026-09-08", "2026-09-09", 50)])
        self.assertIn("样本不足", report.summary(rows, split="2026-09-05"))


class TestReadOnlyReportCommand(unittest.TestCase):
    """只使用临时 SQLite；子进程阻止导入凭据、Store、publisher 与 marketdb。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "离线 # report.sqlite"

    def make_db(self, samples=(), logs=(), legacy=False):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE post_views (
                    post_id INTEGER NOT NULL, day TEXT NOT NULL, sample_day TEXT NOT NULL,
                    sampled_at TEXT NOT NULL, browse INTEGER, likes INTEGER, comments INTEGER,
                    heat REAL, PRIMARY KEY(post_id, sample_day));
                CREATE TABLE run_log (
                    id INTEGER PRIMARY KEY, ts TEXT, task TEXT, status TEXT, detail TEXT);
                CREATE TABLE daily_snapshot (date TEXT PRIMARY KEY);
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            if not legacy:
                conn.execute("ALTER TABLE daily_snapshot ADD COLUMN post_title TEXT")
            for snap in samples:
                conn.execute("INSERT OR REPLACE INTO post_views VALUES (?,?,?,?,?,?,?,?)",
                             [snap[k] for k in ("post_id", "day", "sample_day", "sampled_at",
                                                "browse", "likes", "comments", "heat")])
            for ident, task, status, detail in logs:
                conn.execute("INSERT INTO run_log VALUES (?,?,?,?,?)",
                             (ident, "2026-10-09T09:00:00", task, status,
                              detail if isinstance(detail, str) else json.dumps(detail)))

    def invoke(self, db=None, since=""):
        project = Path(__file__).resolve().parents[1]
        guard = """
import importlib.abc
import runpy
import sqlite3
import sys
from pathlib import Path
db_path = Path(sys.argv[sys.argv.index('--db') + 1])
def db_files():
    paths = (db_path, Path(str(db_path) + '-wal'), Path(str(db_path) + '-shm'),
             Path(str(db_path) + '-journal'))
    return {p.name: p.read_bytes() for p in paths if p.exists()}
initial_files = db_files()
class ObservedConnection(sqlite3.Connection):
    def execute(self, *args, **kwargs):
        result = super().execute(*args, **kwargs)
        if db_files() != initial_files:
            raise AssertionError('offline report changed database or created sidecar files')
        return result
connect = sqlite3.connect
def observed_connect(*args, **kwargs):
    return connect(*args, factory=ObservedConnection, **kwargs)
sqlite3.connect = observed_connect
class OfflineOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('app.store', 'app.config', 'app.marketdb', 'app.publisher'):
            raise AssertionError('offline report imported ' + fullname)
sys.meta_path.insert(0, OfflineOnly())
sys.argv = ['scripts/content_report.py', *sys.argv[1:]]
runpy.run_path('scripts/content_report.py', run_name='__main__')
"""
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", "-c", guard, "report", "--db",
                               str(db or self.db), "--since", since, "--split", "2026-10-09"],
                              cwd=project, env=env, text=True, encoding="utf-8",
                              capture_output=True, timeout=15)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_missing_database_does_not_create_parent_or_database(self):
        missing = Path(self.temp.name) / "missing" / "digest.sqlite"
        output = self.assert_success(self.invoke(missing))
        self.assertIn("无本地数据", output)
        self.assertFalse(missing.parent.exists())

    def test_reads_publication_metadata_by_post_id_and_ignores_other_logs(self):
        first = {"detail": "published", "post_id": 1, "title": "第一篇",
                 **metadata()[1]}
        second = {**first, "post_id": "2", "title": "第二篇", "article_type": "mechanism"}
        self.make_db([
            timed("2026-10-10T09:00:00+00:00", browse=10, post_id=1),
            timed("2026-10-10T09:00:00+00:00", browse=20, post_id=2),
        ], [(1, "daily", "ok", first), (2, "daily", "ok", second),
            (3, "daily", "degraded", {**first, "title": "失败日志"}),
            (4, "weekly", "ok", {**first, "title": "周报日志"}),
            (5, "daily", "ok", {**first, "detail": "dry_run_token_ok", "title": "预览日志"}),
            (6, "daily", "ok", "{bad json"), (7, "daily", "ok", []),
            (8, "daily", "ok", {"detail": "published", "post_id": None}),
            (9, "daily", "ok", {"detail": "published", "post_id": 0})])
        output = self.assert_success(self.invoke())
        self.assertIn("第一篇", output)
        self.assertIn("第二篇", output)
        self.assertIn("整体篇数=2", output)
        self.assertIn("research-v1", output)
        self.assertIn("21–27h 窗口", output)
        self.assertIn("24.0", output)
        for forbidden in ("失败日志", "周报日志", "预览日志"):
            self.assertNotIn(forbidden, output)

    def test_legacy_schema_is_read_without_migration_or_sidecar_files(self):
        self.make_db([s("2026-10-09", "2026-10-10", 10)], legacy=True)
        before_bytes = self.db.read_bytes()
        before_mtime = self.db.stat().st_mtime_ns
        before_files = sorted(p.name for p in self.db.parent.iterdir())
        output = self.assert_success(self.invoke())
        self.assertIn("次日近似", output)
        self.assertEqual(self.db.read_bytes(), before_bytes)
        self.assertEqual(self.db.stat().st_mtime_ns, before_mtime)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before_files)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(daily_snapshot)")], ["date"])
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_old_database_without_report_tables_returns_empty_result(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
        before = self.db.read_bytes()
        output = self.assert_success(self.invoke())
        self.assertIn("无本地数据", output)
        self.assertEqual(self.db.read_bytes(), before)

    def test_checkpointed_wal_database_is_read_without_creating_wal_or_shm(self):
        self.make_db([s("2026-10-09", "2026-10-10", 10)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
        self.assertFalse(Path(str(self.db) + "-wal").exists())
        self.assertFalse(Path(str(self.db) + "-shm").exists())
        output = self.assert_success(self.invoke())
        self.assertIn("浏览总数=10", output)
        self.assertFalse(Path(str(self.db) + "-wal").exists())
        self.assertFalse(Path(str(self.db) + "-shm").exists())

    def test_uncheckpointed_wal_is_rejected_without_silently_reporting_stale_counts(self):
        self.make_db([s("2026-10-09", "2026-10-10", 10)])
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE post_views SET browse=20")
            conn.commit()
            self.assertTrue(Path(str(self.db) + "-wal").exists())
            before = {p.name: p.read_bytes() for p in self.db.parent.iterdir()}
            result = self.invoke()
            self.assertEqual(result.returncode, 1)
            self.assertIn("未合并 WAL", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual({p.name: p.read_bytes() for p in self.db.parent.iterdir()}, before)
        output = self.assert_success(self.invoke())
        self.assertIn("浏览总数=20", output)
        self.assertFalse(Path(str(self.db) + "-wal").exists())
        self.assertFalse(Path(str(self.db) + "-shm").exists())

    def test_since_filter_and_daily_overwrite_are_preserved(self):
        self.make_db([
            s("2026-10-08", "2026-10-09", 999, post_id=8),
            s("2026-10-09", "2026-10-10", 10),
            s("2026-10-09", "2026-10-10", 20),
        ])
        output = self.assert_success(self.invoke(since="2026-10-09"))
        self.assertIn("整体篇数=1", output)
        self.assertIn("浏览总数=20", output)
        self.assertNotIn("999", output)

    def test_title_from_snapshot_is_bound_to_meta_post_id(self):
        self.make_db([
            s("2026-10-09", "2026-10-10", 10, post_id=1),
            s("2026-10-09", "2026-10-10", 20, post_id=2),
        ])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO daily_snapshot VALUES (?,?)", ("2026-10-09", "日报标题"))
            conn.execute("INSERT INTO meta VALUES (?,?)", ("daily_posted:2026-10-09", "2"))
        output = self.assert_success(self.invoke())
        post1 = next(line for line in output.splitlines() if line.startswith("| 10-09") and "| 1 |" in line)
        post2 = next(line for line in output.splitlines() if line.startswith("| 10-09") and "| 2 |" in line)
        self.assertNotIn("日报标题", post1)
        self.assertIn("日报标题", post2)

    def test_source_time_in_log_is_not_publication_time(self):
        self.make_db([timed("2026-10-10T09:00:00+00:00")], [
            (1, "daily", "ok", {"detail": "published", "post_id": 1,
                                 "source_published_at": "2026-10-09T09:00:00+00:00"})])
        output = self.assert_success(self.invoke())
        self.assertIn("次日近似", output)
        self.assertNotIn("21–27h 窗口", output)


if __name__ == "__main__":
    unittest.main()
