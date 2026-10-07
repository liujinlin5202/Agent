# -*- coding: utf-8 -*-
"""诊断：最近一次 daily 运行的源统计（确认 trending=15 是否源异常）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store

s = Store()
row = s.conn.execute(
    "SELECT ts, detail FROM run_log ORDER BY id DESC LIMIT 5").fetchall()
for ts, detail in row:
    dt = json.loads(detail)
    print(ts, "|", dt.get("detail"), "|",
          {k: v.get("fetched") for k, v in (dt.get("source_stats") or {}).items()},
          "| err=", {k: v.get("error") for k, v in (dt.get("source_stats") or {}).items() if v.get("error")})
s.close()
