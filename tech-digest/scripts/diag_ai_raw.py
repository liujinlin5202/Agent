# -*- coding: utf-8 -*-
"""诊断：从已存快照复刻候选 → 打印 LLM 原始输出 + sanitize 中间结果。

用途：看清模型到底输出了什么格式（标题绑定失败/编号回退/其他变形）。
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app import daily_ai, llm
from app.store import Store

store = Store()
try:
    days = store.get_daily_since(date.today() - timedelta(days=1))
    items = days[-1]["items"] if days else []
    news = [it for it in items if (it.get("type") or "trending") == "news"]
    sspai = [it for it in news if it.get("source") == "sspai"][:12]
    hn = [it for it in news if it.get("source") == "hacker-news"][:12]
    rest = [it for it in news if it.get("source") not in ("sspai", "hacker-news")][:12]
    cands = (sspai + hn + rest)[:20]
    print(f"=== 候选 {len(cands)} 条 ===")
    for i, c in enumerate(cands):
        print(f"{i + 1}. [{c['source']}] {c['title']!r}")
    prompt = daily_ai._build_prompt(cands)
    print("=== PROMPT 前 600 字 ===")
    print(prompt[:600])
    content = llm.chat(prompt, system=daily_ai.SYSTEM_PROMPT)
    print("=== RAW LLM (repr) ===")
    print(repr(content)[:4000] if content else "None")
    r = daily_ai.sanitize(content, cands)
    print("=== SANITIZE ===")
    import json
    print(json.dumps(r, ensure_ascii=False, indent=1))
finally:
    store.close()
