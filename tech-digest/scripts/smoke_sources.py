# -*- coding: utf-8 -*-
"""P1 冒烟：真实抓取三源 + 模拟跨期去重（只读，不写库不发帖）。

用法: .venv/bin/python scripts/smoke_sources.py [sources...]
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根：app 包

from app.config import settings
from app.dedup import dedupe_news
from app.sources import fetch_all
from app.store import Store


def main() -> None:
    names = settings.tech_digest_sources
    print("== 源清单:", ",".join(names))
    items, stats = fetch_all(names)
    print("== 各源统计:")
    for name, s in stats.items():
        err = s["error"] or "-"
        print(f"  {name:16s} label={s['source']:24s} fetched={s['fetched']:3d} "
              f"elapsed={s['elapsed_s']:5.1f}s err={err}")
    news = [it for it in items if it["type"] == "news"]
    tr = [it for it in items if it["type"] == "trending"]
    print(f"== 合计 {len(items)} 条: news={len(news)} trending={len(tr)}")
    for it in news[:6]:
        print(f"  - [{it['source']:12s}] {it['title'][:60]} @ {it['published_at']}")
    for it in tr[:3]:
        t = it["trending"]
        print(f"  - [github      ] #{t['rank']} {it['title']} +{t['today_stars']} stars")
    # 去重模拟：近 14 天历史（news 类型）——超窗口无害，仅演示
    store = Store()
    try:
        days = store.get_daily_since(date.today() - timedelta(days=14))
        history = [(d["date"], it) for d in days for it in d["items"]
                   if (it.get("type") or "trending") == "news"]
        print(f"== 历史窗口 {len(days)} 天 / news 条目 {len(history)} 条")
        kept, info = dedupe_news(news, history)
        print(f"== 去重: kept={len(kept)} removed={len(info['removed'])}")
        for r in info["removed"]:
            print(f"  ~ 剔除: {r['title'][:50]} (撞 {r['dup_date']}: {r['dup_title'][:40]})")
    finally:
        store.close()


if __name__ == "__main__":
    main()
