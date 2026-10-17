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


def pending_featured(data_dir: Path) -> list[int]:
    """台账里 featured=true 但 marked 未确认为 True 的 postID（去重升序）。

    开班自愈的欠账来源。搭配 mark_decisions 用：成功落库即回填原行，
    欠账集合收敛到真失败，不会随历史无界滚雪球。"""
    ids = {int(r["post_id"]) for r in load_decisions(data_dir)
           if r.get("featured") and r.get("marked") is not True}
    return sorted(ids)


def mark_decisions(data_dir: Path, post_ids: set[int], marked: bool) -> int:
    """把指定 postID 的 featured 行回填落库结果（原行重写，tmp+rename 原子）。

    台账本追加-only，但 marked 必须写回原行——只追加的话自愈会把已成功
    的行当欠账反复空跑。单写方进程 + 平台 Forbid 并发，整文件重写安全。"""
    path = decisions_path(data_dir)
    rows = load_decisions(data_dir)
    hit = 0
    for r in rows:
        try:
            if r.get("featured") and r.get("marked") is not True \
                    and int(r.get("post_id", -1)) in post_ids:
                r["marked"] = marked
                hit += 1
        except (TypeError, ValueError):
            continue
    if hit:
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(path)
    return hit
