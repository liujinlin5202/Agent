# -*- coding: utf-8 -*-
"""报告窗口与「每两天」守卫。

为什么不用 systemd 的日历表达式做「每两天」：OnCalendar 无法可靠表达跨月奇偶
（*-*-01/2 在 31 天的月份会漏或重）。用「每天 21:30 触发 + 这里判断日期差」：
节奏稳、服务器停机后可补发、改每天/每周只动 OPS_INTERVAL_DAYS 一个数。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def should_send(last_sent: str | None, now: datetime, interval_days: int = 2) -> bool:
    """距上次发送不足 interval_days（按自然日）→ False。从未发过 → True。"""
    if not last_sent:
        return True
    try:
        last = datetime.fromisoformat(last_sent).date()
    except (ValueError, TypeError):
        return True
    return (now.date() - last).days >= interval_days


def report_window(last_sent: str | None, now: datetime,
                  interval_days: int = 2) -> tuple[datetime, datetime]:
    """窗口 = (上次发送, 现在]；首次运行（或上次时间坏了）回看 interval_days 天。"""
    fallback = now - timedelta(days=interval_days)
    if not last_sent:
        return fallback, now
    try:
        return datetime.fromisoformat(last_sent), now
    except (ValueError, TypeError):
        return fallback, now
