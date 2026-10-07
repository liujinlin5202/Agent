# -*- coding: utf-8 -*-
"""分析层：错误聚类。

聚类的意义：同一类报错在窗口里刷 13223 次也只占一行——人要看的是「有几类问题」，
不是「刷了多少屏」。签名归一化把数字/ID/时间戳替换成占位符，同类自然合并。
"""
from __future__ import annotations

import re

from ops_report.model import ErrorCluster

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?")
_HEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")

# 预期内的行为：命中即标 expected=True，不进告警口径
EXPECTED_MARKERS = ("周末只抓取入库", "跳过发帖", "dry-run", "dry_run", "publish_dup")


def signature(text: str) -> str:
    s = _TS.sub("<ts>", text or "")
    s = _HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    return _WS.sub(" ", s).strip()[:220]


def is_expected(text: str) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in EXPECTED_MARKERS)


def cluster(entries: list[dict]) -> list[ErrorCluster]:
    """[{ts, text, source}] → 按签名聚合，次数降序（同数按首次时间）。"""
    agg: dict = {}
    for e in entries or []:
        text = e.get("text") or ""
        sig = signature(text)
        item = agg.get(sig)
        if item is None:
            agg[sig] = {"count": 1, "first_ts": e.get("ts", ""), "last_ts": e.get("ts", ""),
                        "sample": text[:300], "source": e.get("source", ""),
                        "expected": is_expected(text)}
        else:
            item["count"] += 1
            item["last_ts"] = e.get("ts", item["last_ts"])
    out = [ErrorCluster(signature=sig, **data) for sig, data in agg.items()]
    out.sort(key=lambda c: (-c.count, c.first_ts))
    return out


def annotate(section, entries: list[dict]) -> None:
    """把原始错误条目聚类后写入 section.errors。"""
    section.errors = cluster(entries)


def actionable(section) -> list[ErrorCluster]:
    """剔除预期行为的错误（报告正文只列真正要看的）。"""
    return [c for c in section.errors if not c.expected]
