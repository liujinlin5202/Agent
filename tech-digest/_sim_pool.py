# -*- coding: utf-8 -*-
"""模拟真实测试 · Phase 1：完整复刻 run_daily 前半段（拉全源→去重→配额→可爬性
闸门），打印当日真实候选池与过闸名单。不改库、不发帖。跑完删除。"""
import sys
from datetime import date

sys.path.insert(0, ".")

import main  # noqa: E402
from app import dedup  # noqa: E402


def brief(it):
    t = (it.get("title") or "").replace("\n", " ")[:56]
    return f"{it.get('source')}: {t}"


def main_():
    day = date.today().isoformat()
    store = main.Store()
    try:
        items, stats = main.fetch_all()
        print("=== 源统计 ===")
        for n, s in stats.items():
            print(f"  {n}: count={s.get('count')} err={s.get('error') or '-'}")
        news_raw = [it for it in items if it["type"] == "news"]
        news, dedup_info = dedup.dedupe_news(news_raw, main._dedup_history(store, day))
        print(f"\nnews {len(news_raw)} -> 去重后 {len(news)}（剔除 {len(dedup_info['removed'])}）")
        cands = main._daily_candidates(news)
        print(f"\n=== 配额候选 {len(cands)} 条 ===")
        for i, it in enumerate(cands):
            print(f"  [{i:2d}] {brief(it)}")
        gated = main._prefetch_gate(cands)
        print(f"\n=== 可爬性闸门通过 {len(gated)} 条（AI 真实挑选池） ===")
        for i, it in enumerate(gated):
            print(f"  <{i}> {brief(it)}  body_len={it.get('body_len')}")
    finally:
        store.close()


if __name__ == "__main__":
    main_()
