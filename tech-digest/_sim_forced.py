# -*- coding: utf-8 -*-
"""模拟真实测试 · Phase 2：对 5 篇指定文章跑「如果挑中它」的完整生产链路
（提取/翻译 → ai_pick 单候选强制选中生成编者按 → ai_glossary → render_repost），
产物写 _sim_out/。不改库、不发帖。跑完删除。"""
import json
import sys
from datetime import date

sys.path.insert(0, ".")

import main  # noqa: E402
from app import daily_ai, dedup, preference  # noqa: E402
from app.digest import render_repost  # noqa: E402

# (匹配子串, 英文短名) —— 覆盖：中文直发 / 镜像通道 / 英译 / 巨型文 / digest 降级
TARGETS = [
    ("罗马：永恒之城", "sspai-matrix"),
    ("毛绒玩具当", "wscn-mirror"),
    ("Just Bet $1 Billion", "ieee-en"),
    ("Proof Their Hacking Target", "devto-en"),
    ("余承东称华为将不得不涨价", "zhihu-digest"),
]
OUT = ""


def main_():
    global OUT
    import pathlib
    out_dir = pathlib.Path("_sim_out")
    out_dir.mkdir(exist_ok=True)
    OUT = str(out_dir)

    day = date.today().isoformat()
    store = main.Store()
    summary = []
    try:
        items, _ = main.fetch_all()
        news_raw = [it for it in items if it["type"] == "news"]
        news, _ = dedup.dedupe_news(news_raw, main._dedup_history(store, day))
        cands = main._daily_candidates(news)
        has_ai = main._has_ai()
        pref = preference.load_preference()
        print(f"has_ai={has_ai} pref={pref.mode} candidates={len(cands)}")

        for k, (needle, label) in enumerate(TARGETS):
            item = next((it for it in cands
                         if needle in (it.get("title") or "")
                         or needle in (it.get("url") or "")), None)
            if item is None:
                print(f"[{k}] {label}: 未在候选中找到，跳过")
                continue
            item = dict(item)
            art = main._try_fetch_art(item)
            item["_art"] = art
            body, mode, author = main._fetch_body(item, has_ai)
            pick = daily_ai.ai_pick([item], day, preference=pref)
            why = (pick or {}).get("why", "").strip()
            dg = (pick or {}).get("digest", "").strip()
            smy = (item.get("summary") or "").strip()
            if mode == "full":
                lead, kind = body, "full"
            elif dg:
                lead, kind = dg, "digest"
            elif smy:
                lead, kind = smy, "summary"
            else:
                lead, kind = "", "none"
            glossary = daily_ai.ai_glossary(
                item.get("title") or "", body or dg or smy) if has_ai else []
            title = main._pick_post_title(pick, [],
                                          main._fallback_title(pick, [item]))
            md = render_repost(day, 38, {"issue_no": 38, "pick": {
                "item": item, "why": why, "lead": lead, "lead_kind": kind,
                "author": author, "glossary": glossary}})
            fn = out_dir / f"post_{k}_{label}.md"
            fn.write_text(md, encoding="utf-8")
            summary.append({
                "k": k, "label": label, "source": item.get("source"),
                "item_title": item.get("title"), "url": item.get("url"),
                "mode": mode, "lead_kind": kind, "lead_len": len(lead),
                "author": author, "post_title": title,
                "pick_ok": bool(pick), "why_len": len(why),
                "glossary": [g["term"] for g in glossary],
                "md_file": fn.name,
            })
            print(f"[{k}] {label}: mode={mode} lead={len(lead)}字 "
                  f"pick={'ok' if pick else 'FAIL'} why={len(why)}字 "
                  f"gloss={len(glossary)}张 标题={title[:30]}")
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        print("done ->", OUT)
    finally:
        store.close()


if __name__ == "__main__":
    main_()
