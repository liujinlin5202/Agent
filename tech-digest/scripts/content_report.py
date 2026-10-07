#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发帖表现追踪：采样 + 复盘表（2026-09-13 新增）。

用法:
  python scripts/content_report.py sample            # 记录今天的浏览采样（timer 每日跑）
  python scripts/content_report.py sample --days 30  # 回补最近 30 天的帖子
  python scripts/content_report.py report            # 打印复盘表
  python scripts/content_report.py report --split 2026-09-14   # 改版日前后 h24 均值对比

为什么要有它：改内容之前得先能看见效果。日报发了 40 多期，浏览量一直靠肉眼翻库，
既没有统一口径，也没有「这期比上期好还是差」的答案——用户最初的判断
「浏览量减少了很多」就是这么来的（回看数据，8 月均值 47、9 月均值 100，
其实是小样本下的噪声，真实问题是一直没起来）。

两条口径分开记，别混：
  · h24 浏览 —— 发布次日的那次采样（daily 09:15 跑，正好是发布后 ~24 小时）；
  · 当前浏览 —— 最新一次采样，含长尾（实测 9/11 那期 h24 只有 46，后来涨到 277）。

数据源是集市库（app/marketdb.py，只读）。采样落在本地 SQLite 的 post_views：
集市库的 browse_num 是实时值、没有历史快照，不自己存就永远只能看到「现在」。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import marketdb, report          # noqa: E402
from app.config import settings           # noqa: E402
from app.store import Store               # noqa: E402


def _user_id() -> int | None:
    if not settings.market_user_telephone:
        print("未配置 MARKET_USER_TELEPHONE，无法定位账号", file=sys.stderr)
        return None
    uid = marketdb.user_id_of(settings.market_user_telephone)
    if not uid:
        print("集市库查不到该手机号对应的 userID", file=sys.stderr)
    return uid


def cmd_sample(args) -> int:
    """把「本人最近 N 天的帖子」的当前浏览数记一次采样（同一天重复跑会覆盖）。"""
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


def cmd_report(args) -> int:
    store = Store()
    try:
        samples = store.view_samples(since=args.since)
        titles = {s["day"]: store.daily_title(s["day"])
                  for s in samples} if samples else {}
        rows = report.report_rows(samples, titles)
        print(report.render_table(rows))
        print()
        print(report.summary(rows, args.split))
    finally:
        store.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="tech-digest 发帖表现追踪")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample", help="记录浏览采样")
    sp.add_argument("--days", type=int, default=7, help="回补天数（默认 7）")
    sp.set_defaults(func=cmd_sample)
    rp = sub.add_parser("report", help="打印复盘表")
    rp.add_argument("--since", default="", help="只看某日之后的帖子（YYYY-MM-DD）")
    rp.add_argument("--split", default=date.today().isoformat(),
                    help="改版日：前后各取一侧对比 h24 均值（默认今天）")
    rp.set_defaults(func=cmd_report)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
