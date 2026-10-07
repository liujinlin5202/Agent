# -*- coding: utf-8 -*-
"""负载门控：读取 /proc/loadavg 1min 值，超阈值则跳过任务（"空闲才爬"）。"""
from __future__ import annotations

from pathlib import Path


def current_load1() -> float | None:
    """返回 1 分钟负载均值；读取失败（非 Linux）返回 None。"""
    try:
        text = Path("/proc/loadavg").read_text()
        return float(text.split()[0])
    except (OSError, ValueError, IndexError):
        return None


def should_skip(threshold: float) -> tuple[bool, str | None]:
    """返回 (是否跳过, 原因)。load 无法读取时不跳过（fail-open，见技术文档 §7）。"""
    load = current_load1()
    if load is None:
        return False, None
    if load >= threshold:
        return True, f"load={load:.2f} >= threshold={threshold}"
    return False, None
