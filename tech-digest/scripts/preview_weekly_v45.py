# -*- coding: utf-8 -*-
"""v4.5 预览（指定周）：用 W38 真实七天数据跑星榜段，验证 AI 中文简介 + 两行列表。"""
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "/root/market-deploy/agent/tech-digest")

from app.config import settings

settings.validate(need_ai=True)

from app.store import Store  # noqa: E402
from app.weekly import generate_weekly_md  # noqa: E402

monday = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-09-14")
store = Store()
try:
    days = store.get_daily_since(monday)
    recent = store.query("SELECT date, length(items_json) FROM daily_snapshot "
                         "ORDER BY date DESC LIMIT 5") if hasattr(store, "query") else []
finally:
    store.close()
md, used, detail = generate_weekly_md(days, f"preview-W{monday.isocalendar()[1]:02d}")
# 头部日期行对齐 argv 指定的周（render_weekly 缺省按今天算，预览指定历史周时会错位）
md = re.sub(r"^> \d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}",
            f"> {monday.isoformat()} ~ {(monday + timedelta(days=6)).isoformat()}",
            md, count=1, flags=re.M)
out = Path("/root/market-deploy/agent/tech-digest/output/v45-weekly-preview.md")
out.write_text(md, encoding="utf-8")
print(json.dumps({"monday": monday.isoformat(), "days": len(days),
                  "ai_used": used, "detail": detail, "chars": len(md)},
                 ensure_ascii=False))
print(out)
