# -*- coding: utf-8 -*-
"""一次性脚本：把已发布的私密帖（postID=5725）补写进 weekly_report 记录。

12:12 真实发帖时后端响应不带 postID（data=nil），mark_published 未执行；
postID 已通过 DB 查询确认（posts 表 5725，is_private=1，技术分享）。
"""
import sqlite3
from pathlib import Path
from datetime import datetime

DB = Path(__file__).resolve().parent.parent / "data" / "tech-digest.db"
POST_ID = 5725
URL = "/postdetail/5725"

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("SELECT week_label, post_id, published_at FROM weekly_report WHERE post_id IS NULL OR post_id = 0")
rows = cur.fetchall()
print("未记录发布结果的周报:", rows)
cur.execute(
    "UPDATE weekly_report SET post_id = ?, post_url = ?, published_at = ? WHERE post_id IS NULL OR post_id = 0",
    (POST_ID, URL, datetime.now().isoformat(timespec="seconds")),
)
con.commit()
cur.execute("SELECT week_label, post_id, post_url, published_at FROM weekly_report")
for r in cur.fetchall():
    print(r)
con.close()
