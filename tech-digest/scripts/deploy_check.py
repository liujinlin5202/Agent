# -*- coding: utf-8 -*-
"""服务器部署自检：导入 + run_log_since + 五段渲染冒烟（空数据不落盘）。"""
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store
from app.weekly import generate_weekly_md, render_weekly

from app.daily_ai import _unwrap
assert _unwrap("《标题》") == "标题"

s = Store()
rows = s.run_log_since("2026-08-30", "daily")
print("run_log_since:", len(rows), "行")
s.close()

# 渲染冒烟：无数据也能五段齐全（不写入任何文件）
md = render_weekly([], "2026-W36", None, None,
                   {"review": False, "deep": False, "intro": False}, {})
for h in ("本周回顾", "深度长文", "精选回看", "下周前瞻", "本周 GitHub 星榜"):
    assert h in md, h
print("render_weekly 五段齐全, chars =", len(md))
print("import ok")
