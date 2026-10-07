# -*- coding: utf-8 -*-
"""诊断：最近 2 次 daily 运行的 AI 使用状态 + 今日日报头部。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store

s = Store()
rows = s.conn.execute(
    "SELECT ts, detail FROM run_log WHERE task='daily' AND detail LIKE '%ai_used%'"
    " ORDER BY id DESC LIMIT 2").fetchall()
for ts, detail in rows:
    dt = json.loads(detail)
    print(ts, "| ai_used =", dt.get("ai_used"),
          "| theme_ok =", bool(dt.get("issue_no")),
          "| issue =", dt.get("issue_no"))
s.close()
