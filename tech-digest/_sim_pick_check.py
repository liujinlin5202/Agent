# -*- coding: utf-8 -*-
"""新标准实弹抽查：今日真实闸门池跑一次 ai_pick，验证技术话题硬否决。
（罗马游记在池内——若仍被选中说明闸门不够硬。）跑完删除。"""
import sys
from datetime import date

sys.path.insert(0, ".")

import main  # noqa: E402
from app import daily_ai, dedup, preference  # noqa: E402


def brief(it):
    return f"{it.get('source')}: {(it.get('title') or '')[:42]}"


def main_():
    day = date.today().isoformat()
    store = main.Store()
    try:
        items, _ = main.fetch_all()
        news_raw = [it for it in items if it["type"] == "news"]
        news, _ = dedup.dedupe_news(news_raw, main._dedup_history(store, day))
        cands = main._daily_candidates(news)
        gated = main._prefetch_gate(cands)
        pool = gated or cands
        print(f"=== 闸门池 {len(pool)} 篇 ===")
        for i, it in enumerate(pool):
            print(f"  [{i}] {brief(it)}")
        pref = preference.load_preference()
        pick = daily_ai.ai_pick(pool, day, preference=pref)
        if not pick:
            print("\n结果: pick=None（护栏全拒或 LLM 失败）")
            return
        it = pool[pick["idx"] - 1]
        print(f"\n=== 选中: {brief(it)}")
        print("编者按:", pick["why"][:150])
    finally:
        store.close()


if __name__ == "__main__":
    main_()
