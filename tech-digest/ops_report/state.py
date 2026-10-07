# -*- coding: utf-8 -*-
"""状态与告警文件。

· state.json 记 last_sent，供 2 天守卫判断；
· alerts/*.txt 是投递失败的唯一留痕处 —— 企业运维里「静默失败」比「失败」危险得多：
  邮件发不出去时没有别的通道能喊人，所以写文件，下次报告开头标红。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class State:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "state.json"

    def last_sent(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("last_sent")
        except (ValueError, OSError, AttributeError):
            return None

    def mark_sent(self, ts: str | None = None) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"last_sent": ts or datetime.now().isoformat(timespec="seconds")}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def alerts_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "alerts"


def add_alert(data_dir: Path, message: str, now: datetime | None = None) -> Path:
    d = alerts_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # 带微秒：同一秒内的两条告警若同名会后者覆盖前者，恰好丢掉一条失败留痕 —— 与「失败不静默」相悖。
    ts = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    path = d / f"{ts}.txt"
    path.write_text(message, encoding="utf-8")
    return path


def pending_alerts(data_dir: Path) -> list[str]:
    """未处理的告警原文（按文件名即时间序）。"""
    d = alerts_dir(data_dir)
    if not d.exists():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(d.glob("*.txt"))]


def clear_alerts(data_dir: Path) -> None:
    d = alerts_dir(data_dir)
    if not d.exists():
        return
    for p in d.glob("*.txt"):
        p.unlink()
