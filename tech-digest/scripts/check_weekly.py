# -*- coding: utf-8 -*-
"""查 weekly_report 与 weekly run_log：W35 周报何时生成/发布。"""
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store

s = Store()
try:
    print("== weekly_report ==")
    for r in s.conn.execute("SELECT week_label, md_path, ai_used, post_id, post_url, "
                            "published_at, created_at FROM weekly_report ORDER BY created_at"):
        print(dict(r))
    print("== run_log weekly ==")
    for r in s.conn.execute(
            "SELECT id, ts, status, substr(detail,1,200) FROM run_log "
            "WHERE task='weekly' ORDER BY id"):
        print(dict(r))
finally:
    s.close()
