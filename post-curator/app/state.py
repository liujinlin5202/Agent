# -*- coding: utf-8 -*-
"""审计状态盘：decisions.jsonl（一次评审一行，追加永不改写）。

两个消费面：
  1. 运维排查——每帖的判定依据、分数、是否落库全留痕
  2. 评审冷却——rejudge_hours 窗口内评过的帖不再重评（3 天窗口内一帖
     约评 3 次，评论区晚熟的问询帖有翻案机会；--force 跳过冷却全量重评）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("post-curator.state")


def decisions_path(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "decisions.jsonl"


def append_decisions(data_dir: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path = decisions_path(data_dir)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def load_decisions(data_dir: Path) -> list[dict]:
    path = decisions_path(data_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # 半行损坏按不存在处理
    return rows


def recently_judged(data_dir: Path, rejudge_hours: int,
                    now: datetime | None = None) -> set[int]:
    """冷却窗口内评过的 postID 集合。时钟用行内 ts（UTC ISO），损坏行跳过。"""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=rejudge_hours)
    out: set[int] = set()
    for row in load_decisions(data_dir):
        try:
            ts = datetime.fromisoformat(row["ts"])
            pid = int(row["post_id"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts >= cutoff:
            out.add(pid)
    return out
