# -*- coding: utf-8 -*-
"""只读检查：8/30 快照 / issue 计数器状态。"""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.store import Store

s = Store()
try:
    print("issue_no(2026-08-30):", s.get_issue_no("2026-08-30"))
    print("next_issue_no:", s.next_issue_no())
    print("daily_exists:", s.daily_exists("2026-08-30"))
finally:
    s.close()
