#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表现追踪：report 只读本地 SQLite；sample 是显式联网采样操作。

python scripts/content_report.py report --db path/to/tech-digest.db --split 2026-10-11
report 从 daily 成功发布的 run_log JSON 按 post_id 读取元数据，不访问集市或凭据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import report                    # noqa: E402


def _user_id() -> int | None:
    from app import marketdb
    from app.config import settings

    if not settings.market_user_telephone:
        print("未配置 MARKET_USER_TELEPHONE，无法定位账号", file=sys.stderr)
        return None
    uid = marketdb.user_id_of(settings.market_user_telephone)
    if not uid:
        print("集市库查不到该手机号对应的 userID", file=sys.stderr)
    return uid


def cmd_sample(args) -> int:
    """把「本人最近 N 天的帖子」的当前浏览数记一次采样（同一天重复跑会覆盖）。"""
    from app import marketdb
    from app.store import Store

    uid = _user_id()
    if not uid:
        return 1
    since = (date.today() - timedelta(days=args.days)).isoformat()
    posts = marketdb.posts_since(uid, since)
    if not posts:
        print(f"集市库没有 {since} 以来的公开帖（或库不可用）", file=sys.stderr)
        return 1
    today = date.today().isoformat()
    store = Store()
    try:
        for p in posts:
            store.add_view_sample(p["post_id"], p["day"], today,
                                  p["browse"], p["likes"], p["comments"], p["heat"])
        print(f"已采样 {len(posts)} 期（{since} ~ {today}）")
    finally:
        store.close()
    return 0


def _post_id(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        ident = int(value)
        return ident if ident > 0 else None
    except ValueError:
        return None


def _read_report_data(db_path: Path, since: str = "") -> tuple[list[dict], dict[int, str], dict[int, dict]]:
    """读取稳定的离线副本；缺库/缺采样表时返回空值，不创建目录或迁移。"""
    db_path = db_path.resolve()
    if not db_path.is_file():
        return [], {}, {}
    wal_path = Path(str(db_path) + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size:
        raise sqlite3.DatabaseError("本地数据库含未合并 WAL；请使用已关闭写入连接的完整离线副本，避免遗漏数据")
    # mode=ro 在 WAL 库仍可能创建 -wal/-shm。immutable 禁止该写入与锁操作；
    # 离线副本必须在读取期间保持不变，不能拿正在写入的数据库做实时报告。
    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "post_views" not in tables:
            return [], {}, {}
        query = "SELECT post_id, day, sample_day, sampled_at, browse, likes, comments, heat FROM post_views"
        params = []
        if since:
            query += " WHERE day >= ?"
            params.append(since)
        samples = [dict(row) for row in conn.execute(query + " ORDER BY day, sample_day, post_id", params)]
        ids = {snap["post_id"] for snap in samples}
        titles: dict[int, str] = {}
        post_meta: dict[int, dict] = {}
        if "run_log" in tables:
            logs = conn.execute("SELECT detail FROM run_log WHERE task='daily' AND status='ok' ORDER BY id")
            for log in logs:
                try:
                    detail = json.loads(log["detail"] or "{}")
                except (ValueError, TypeError):
                    continue
                if not isinstance(detail, dict) or detail.get("detail") != "published":
                    continue
                post_id = _post_id(detail.get("post_id"))
                if post_id not in ids:
                    continue
                fields = ("published_at", "time_basis", "content_version", "article_type", "learning_question")
                post_meta.setdefault(post_id, {}).update({key: detail[key] for key in fields
                                                         if detail.get(key) is not None})
                if isinstance(detail.get("title"), str) and detail["title"]:
                    titles[post_id] = detail["title"]
        if "daily_snapshot" in tables:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(daily_snapshot)")}
            if "post_title" in columns:
                published_ids = {}
                if "meta" in tables:
                    published_ids = {row["key"].removeprefix("daily_posted:"): _post_id(row["value"])
                                     for row in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'daily_posted:%'")}
                by_day: dict[str, set[int]] = {}
                for snap in samples:
                    by_day.setdefault(snap["day"], set()).add(snap["post_id"])
                for row in conn.execute("SELECT date, post_title FROM daily_snapshot"):
                    day_ids = by_day.get(row["date"], set())
                    if row["date"] in published_ids:
                        post_id = published_ids[row["date"]]
                    else:
                        post_id = next(iter(day_ids)) if len(day_ids) == 1 else None
                    if post_id in day_ids and row["post_title"]:
                        titles.setdefault(post_id, row["post_title"])
        return samples, titles, post_meta


def cmd_report(args) -> int:
    default_db = Path(__file__).resolve().parent.parent / "data" / "tech-digest.db"
    db_path = Path(getattr(args, "db", None) or default_db)
    try:
        samples, titles, post_meta = _read_report_data(db_path, args.since)
    except sqlite3.DatabaseError as exc:
        print(f"无法读取本地数据：{exc}", file=sys.stderr)
        return 1
    rows = report.report_rows(samples, titles, post_meta)
    print(report.render_table(rows))
    print()
    print(report.summary(rows, args.split))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="tech-digest 发帖表现追踪")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample", help="记录浏览采样")
    sp.add_argument("--days", type=int, default=7, help="回补天数（默认 7）")
    sp.set_defaults(func=cmd_sample)
    rp = sub.add_parser("report", help="只读本地数据库，打印复盘表")
    rp.add_argument("--db", default="", help="稳定的离线 SQLite 副本（默认 data/tech-digest.db）")
    rp.add_argument("--since", default="", help="只看某日之后的帖子（YYYY-MM-DD）")
    rp.add_argument("--split", default=date.today().isoformat(),
                    help="改版日：分组描述约24h采样的均值、中位数和计数（默认今天）")
    rp.set_defaults(func=cmd_report)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
