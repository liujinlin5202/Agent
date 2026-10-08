# -*- coding: utf-8 -*-
"""跨期去重：URL 规范化 + 标题相似度（技术文档 §4）。

仅作用于 news 条目：trending 是每日星榜数据，同一仓库隔天上榜属正常（周报聚合依赖它）。
历史窗口由调用方提供（近 7 期 daily_snapshot 的 news 条目，标注日期）。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

TITLE_SIM_THRESHOLD = 0.85
# 跟踪参数：规范化时剔除（常见分享/统计参数）
STOP_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
               "ref", "from", "spm", "share_token", "source", "fro"}


def normalize_url(url: str) -> str:
    """规范化：去 fragment、去跟踪参数、scheme/host 小写、去尾 '/'、query 键排序。"""
    if not url:
        return ""
    u = url.strip().split("#", 1)[0]
    if "://" in u:
        scheme, rest = u.split("://", 1)
        # 一般站点路径大小写敏感，不做路径 lower（避免误并）
        scheme = scheme.lower()
    else:
        scheme, rest = "", u
    host, _, path_q = rest.partition("/")
    path, _, qs = path_q.partition("?")
    path = path.rstrip("/")
    if qs:
        pairs = []
        for p in qs.split("&"):
            if not p:
                continue
            key = p.split("=", 1)[0].lower()
            if key in STOP_PARAMS:
                continue
            pairs.append(p)
        qs = "&".join(sorted(pairs))
    out = f"{scheme}://{host.lower()}"
    if path:
        out += "/" + path
    if qs:
        out += "?" + qs
    return out


def _title(item: dict) -> str:
    # 兼容 v1 条目（full_name 无 title）
    return (item.get("title") or item.get("full_name") or item.get("name") or "")


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def is_duplicate(a: dict, b: dict, threshold: float = TITLE_SIM_THRESHOLD) -> bool:
    """URL 精确（规范化后）或标题相似度 ≥ threshold → 重复。

    quick_ratio 剪枝（2026-10-08 M1）：池化后单班对比量从 O(班次×14 期) 涨到
    O(班次×14 天全池)，先算上界不上阈值的直接跳过完整 ratio——语义不变，
    只省无效计算。
    """
    ua, ub = normalize_url(a.get("url", "")), normalize_url(b.get("url", ""))
    if ua and ub and ua == ub:
        return True
    ta, tb = _title(a), _title(b)
    if not ta or not tb:
        return False
    sm = SequenceMatcher(None, ta.lower(), tb.lower())
    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
        return False
    return sm.ratio() >= threshold


def dedupe_news(items: list[dict], history: list[tuple[str, dict]]) \
        -> tuple[list[dict], dict]:
    """news 条目去重。

    history: [(date, item)]，近 7 期已发布条目（调用方只给 news 类型）。
    批内先查重复（多源同日撞文），再查历史窗口。
    返回 (kept, {"removed": [{"title","url","dup_date","dup_title"}]})，removed 为被剔除条目明细。
    """
    kept: list[dict] = []
    dups: list[dict] = []
    for it in items:
        hit: tuple[str | None, dict] | None = None
        for d, old in history:
            if is_duplicate(it, old):
                hit = (d, old)
                break
        if hit is None:
            for prev in kept:
                if is_duplicate(it, prev):
                    hit = (None, prev)
                    break
        if hit is not None:
            d, old = hit
            dups.append({"title": it["title"], "url": it["url"],
                         "dup_date": d or "(同批)", "dup_title": _title(old)})
        else:
            kept.append(it)
    return kept, {"removed": dups}
