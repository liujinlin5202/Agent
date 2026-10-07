# -*- coding: utf-8 -*-
"""P2 收尾：清理 2026-08-30 预览快照（4 次 dry-run 产物），重置期号计数器。

明天（2026-08-31 周一）09:15 定时器将生成第 1 期真实公开日报 #1。
"""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.store import Store

DAY = "2026-08-30"

s = Store()
try:
    before = s.conn.execute(
        "SELECT date, source, issue_no, length(items_json) FROM daily_snapshot"
    ).fetchall()
    print("清理前 daily_snapshot:", before)
    print("清理前 meta issue_next =",
          s.conn.execute("SELECT value FROM meta WHERE key='issue_next'").fetchone())
    print("清理前 daily_posted:", s.conn.execute(
        "SELECT COUNT(*) FROM meta WHERE key=?", (f"daily_posted:{DAY}",)).fetchone())

    s.conn.execute("DELETE FROM daily_snapshot WHERE date=?", (DAY,))
    s.conn.execute("DELETE FROM meta WHERE key=?", (f"daily_posted:{DAY}",))
    s.conn.execute(
        "INSERT INTO meta(key, value) VALUES('issue_next', '1')"
        " ON CONFLICT(key) DO UPDATE SET value='1'")
    s.conn.commit()

    print("清理后 daily_snapshot:",
          s.conn.execute("SELECT COUNT(*) FROM daily_snapshot").fetchone()[0], "行")
    print("清理后 meta issue_next =",
          s.conn.execute("SELECT value FROM meta WHERE key='issue_next'").fetchone())
    print("✅ 预览数据已清理，明日 09:15 将发布首期 #1")
finally:
    s.close()
