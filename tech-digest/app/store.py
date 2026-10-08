# -*- coding: utf-8 -*-
"""SQLite 存储：schema / 幂等查询 / 保留策略。

表结构见技术文档 §3。WAL + busy_timeout（2026-10-08 M1）：ingest 与 digest
两个 pod 可能罕见同碰一个库文件，WAL 允许读写并行、busy_timeout 让后到方
等锁而非直接报 database is locked。连接工厂 connect_db 是全仓唯一落点
（PoolStore 同款复用），pragma 不许在别处再写一份。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import settings

BUSY_TIMEOUT_MS = 5_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshot (
  date       TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  items_json TEXT NOT NULL,
  md_path    TEXT,
  created_at TEXT NOT NULL,
  issue_no   INTEGER
);
CREATE TABLE IF NOT EXISTS weekly_report (
  week_label   TEXT PRIMARY KEY,
  md_path      TEXT,
  ai_used      INTEGER NOT NULL DEFAULT 0,
  post_id      INTEGER,
  post_url     TEXT,
  published_at TEXT,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     TEXT NOT NULL,
  task   TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_views (
  post_id    INTEGER NOT NULL,
  day        TEXT NOT NULL,
  sample_day TEXT NOT NULL,
  sampled_at TEXT NOT NULL,
  browse     INTEGER NOT NULL DEFAULT 0,
  likes      INTEGER NOT NULL DEFAULT 0,
  comments   INTEGER NOT NULL DEFAULT 0,
  heat       REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (post_id, sample_day)
);
"""


def connect_db(db_path: Path) -> sqlite3.Connection:
    """全仓唯一的 SQLite 连接工厂：Store 与 PoolStore 共用。

    WAL 是库级持久属性（设一次随库生效）；busy_timeout 是连接级，每次开连接
    都要设。journal_mode 返回值不检查——旧连接持有的锁可能导致切换失败，
    那时库已是 WAL 或单进程场景，静默沿用现状即可。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:  # 锁冲突时切换失败 → 单进程降级可接受
        pass
    return conn


class Store:
    def __init__(self, db_path: Path | None = None):
        path = db_path or (settings.data_dir / "tech-digest.db")
        self.db_path = path
        self.conn = connect_db(path)
        self.conn.executescript(SCHEMA)
        # 增量迁移：旧库缺列（SQLite 无 IF NOT EXISTS 语法，逐列 PRAGMA 探测）
        # daily_snapshot.issue_no —— v1.x → v2.0 期数
        # daily_snapshot.post_title —— 2026-09-13 日报 v3（标题校验器要拿近 7 期做去重）
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(daily_snapshot)")}
        if "issue_no" not in cols:
            self.conn.execute("ALTER TABLE daily_snapshot ADD COLUMN issue_no INTEGER")
        if "post_title" not in cols:
            self.conn.execute("ALTER TABLE daily_snapshot ADD COLUMN post_title TEXT")
        self.conn.commit()

    # ---------- daily ----------
    def daily_exists(self, day: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM daily_snapshot WHERE date=?", (day,)).fetchone()
        return row is not None

    def save_daily(self, day: str, source: str, items: list[dict], md_path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_snapshot(date, source, items_json, md_path, created_at) "
            "VALUES(?,?,?,?,?)",
            (day, source, json.dumps(items, ensure_ascii=False), md_path,
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def get_daily_since(self, since: date) -> list[dict]:
        rows = self.conn.execute(
            "SELECT date, source, items_json, issue_no FROM daily_snapshot WHERE date>=? ORDER BY date",
            (since.isoformat(),)).fetchall()
        out = []
        for r in rows:
            out.append({
                "date": r["date"],
                "source": r["source"],
                "issue_no": r["issue_no"],
                "items": json.loads(r["items_json"]),
            })
        return out

    # ---------- v2.0：期数 ----------
    def next_issue_no(self) -> int:
        """下一个可用期数（未生成当日快照时为当前值）。"""
        row = self.conn.execute("SELECT value FROM meta WHERE key='issue_next'").fetchone()
        return int(row["value"]) if row else 1

    def get_issue_no(self, day: str) -> int | None:
        row = self.conn.execute(
            "SELECT issue_no FROM daily_snapshot WHERE date=?", (day,)).fetchone()
        return row["issue_no"] if row else None

    def is_daily_posted(self, day: str) -> bool:
        """当日日报是否已发帖（--force 重跑不重发）。"""
        row = self.conn.execute(
            "SELECT 1 FROM meta WHERE key='daily_posted:' || ?", (day,)).fetchone()
        return row is not None

    def mark_daily_posted(self, day: str, post_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('daily_posted:' || ?, ?)",
            (day, str(post_id)))
        self.conn.commit()

    def daily_post_id(self, day: str) -> int | None:
        """当日日报的 postID；未发帖或后端未返回（0）→ None。

        后端发帖成功时 data 为 nil、不返回 postID（2026-08-30 实测），此时
        meta 里存的是 0 —— 0 不是可评论的目标，必须当 None 处理，
        否则补楼会 POST postID=0（sync 侧的同类坑：match 不带 postID 落 0）。
        """
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='daily_posted:' || ?", (day,)).fetchone()
        if not row:
            return None
        try:
            pid = int(row["value"])
        except (TypeError, ValueError):
            return None
        return pid or None

    # ---- v4 周六星榜专帖幂等（2026-09-17）：与日报标记同款 meta，键名独立 ----

    def is_star_posted(self, day: str) -> bool:
        """当日星榜帖是否已发布（--force 重跑不重发）。"""
        row = self.conn.execute(
            "SELECT 1 FROM meta WHERE key='star_posted:' || ?", (day,)).fetchone()
        return row is not None

    def mark_star_posted(self, day: str, post_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('star_posted:' || ?, ?)",
            (day, str(post_id)))
        self.conn.commit()

    def is_daily_commented(self, day: str) -> bool:
        """当日是否已补楼（星榜完整版作为 1 楼评论）——同日重跑不重复评论。"""
        row = self.conn.execute(
            "SELECT 1 FROM meta WHERE key='daily_commented:' || ?", (day,)).fetchone()
        return row is not None

    def mark_daily_commented(self, day: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('daily_commented:' || ?, ?)",
            (day, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def recent_daily_titles(self, n: int, before_day: str = "") -> list[str]:
        """最近 n 期已发帖的标题（新→旧）。before_day 排除当日——发帖前调用时，
        当日行已入库（save_daily_v2 先于 publish），不排除会把今天算进「近期」。
        """
        q = ("SELECT post_title FROM daily_snapshot "
             "WHERE post_title IS NOT NULL AND post_title != ''")
        args: list = []
        if before_day:
            q += " AND date < ?"
            args.append(before_day)
        rows = self.conn.execute(q + " ORDER BY date DESC LIMIT ?", [*args, n]).fetchall()
        return [r["post_title"] for r in rows]

    def save_daily_v2(self, day: str, items: list[dict], md_path: str,
                      post_title: str = "") -> int:
        """v2.0 多源快照 + 期数绑定（同一事务：INSERT OR REPLACE + issue_next 递增）。

        同日重跑（--force）：保留原期数、不递增（幂等防跳号）；
        首次生成：取 issue_next 并 +1。返回本期 issue_no。
        post_title：实际发帖标题（未发帖为空）——供标题去重与表现追踪复盘。
        """
        now = datetime.now().isoformat(timespec="seconds")
        with self.conn:  # 事务
            existing = self.conn.execute(
                "SELECT issue_no, post_title FROM daily_snapshot WHERE date=?", (day,)).fetchone()
            if existing and existing["issue_no"]:
                issue_no = existing["issue_no"]
            else:
                row = self.conn.execute("SELECT value FROM meta WHERE key='issue_next'").fetchone()
                issue_no = int(row["value"]) if row else 1
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('issue_next', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(issue_no + 1),))
            # 标题：同日重跑且本次未带标题时保留原值（发帖晚于落盘，别把已发出的标题抹掉）
            if not post_title and existing and existing["post_title"]:
                post_title = existing["post_title"]
            self.conn.execute(
                "INSERT OR REPLACE INTO daily_snapshot(date, source, items_json, md_path, "
                "created_at, issue_no, post_title) VALUES(?,?,?,?,?,?,?)",
                (day, "multi", json.dumps(items, ensure_ascii=False), md_path, now,
                 issue_no, post_title or None))
        return issue_no

    def set_daily_title(self, day: str, title: str) -> None:
        """发帖后回填标题（render 在 publish 之前，标题此刻才最终确定）。"""
        self.conn.execute(
            "UPDATE daily_snapshot SET post_title=? WHERE date=?", (title, day))
        self.conn.commit()

    def daily_title(self, day: str) -> str:
        """当期已记录的发帖标题（未发帖为空串）。

        补楼用：后端发帖不返回 postID，只能靠「本人 + 标题逐字一致」反查（marketdb）。
        """
        row = self.conn.execute(
            "SELECT post_title FROM daily_snapshot WHERE date=?", (day,)).fetchone()
        return (row["post_title"] or "") if row else ""

    # ---------- 表现追踪（scripts/content_report.py）----------
    def add_view_sample(self, post_id: int, day: str, sample_day: str,
                        browse: int, likes: int, comments: int, heat: float) -> None:
        """记一次浏览采样。同一 post 同一采样日覆盖（重跑不产生重复行）。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO post_views(post_id, day, sample_day, sampled_at, "
            "browse, likes, comments, heat) VALUES(?,?,?,?,?,?,?,?)",
            (post_id, day, sample_day, datetime.now().isoformat(timespec="seconds"),
             browse, likes, comments, heat))
        self.conn.commit()

    def view_samples(self, since: str = "") -> list[dict]:
        """采样明细（按发布日、采样日升序）。since 为发布日的下界。"""
        q = ("SELECT post_id, day, sample_day, sampled_at, browse, likes, comments, heat "
             "FROM post_views")
        args: list = []
        if since:
            q += " WHERE day >= ?"
            args.append(since)
        rows = self.conn.execute(q + " ORDER BY day, sample_day, post_id", args).fetchall()
        return [dict(r) for r in rows]

    # ---------- weekly ----------
    def weekly_exists(self, week_label: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM weekly_report WHERE week_label=?", (week_label,)).fetchone()
        return row is not None

    def save_weekly(self, week_label: str, md_path: str, ai_used: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO weekly_report(week_label, md_path, ai_used, created_at) "
            "VALUES(?,?,?,?)",
            (week_label, md_path, int(ai_used), datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def mark_published(self, week_label: str, post_id: int, post_url: str) -> None:
        self.conn.execute(
            "UPDATE weekly_report SET post_id=?, post_url=?, published_at=? WHERE week_label=?",
            (post_id, post_url, datetime.now().isoformat(timespec="seconds"), week_label))
        self.conn.commit()

    # ---------- run_log ----------
    def run_log_since(self, since: str, task: str | None = None) -> list[dict]:
        """运行日志（周报⑥自检用）：timestamp >= since；可选按 task 过滤。"""
        q = "SELECT ts, task, status, detail FROM run_log WHERE ts>=?"
        args: list = [since]
        if task:
            q += " AND task=?"
            args.append(task)
        rows = self.conn.execute(q + " ORDER BY ts", args).fetchall()
        return [{"ts": r["ts"], "task": r["task"], "status": r["status"],
                 "detail": json.loads(r["detail"] or "{}")} for r in rows]

    def log(self, task: str, status: str, detail: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO run_log(ts, task, status, detail) VALUES(?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), task, status,
             json.dumps(detail or {}, ensure_ascii=False)))
        self.conn.commit()

    # ---------- 保留策略 ----------
    def cleanup(self) -> dict:
        """daily 保留 N 天；run_log 保留 M 条；浏览采样随 daily 一起过期。返回清理数。"""
        cutoff = (date.today() - timedelta(days=settings.daily_retain_days)).isoformat()
        c1 = self.conn.execute("DELETE FROM daily_snapshot WHERE date<?", (cutoff,)).rowcount
        c2 = self.conn.execute(
            "DELETE FROM run_log WHERE id NOT IN "
            "(SELECT id FROM run_log ORDER BY id DESC LIMIT ?)",
            (settings.run_log_retain,)).rowcount
        c3 = self.conn.execute("DELETE FROM post_views WHERE day<?", (cutoff,)).rowcount
        self.conn.commit()
        return {"daily_deleted": c1, "runlog_deleted": c2, "views_deleted": c3}

    def close(self) -> None:
        self.conn.close()
