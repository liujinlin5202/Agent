# -*- coding: utf-8 -*-
"""dev.to 热榜源（2026-09-17 v4.1 加源）。

官方公开 API /api/articles?top=7（本周热门），无需鉴权，返回 title/url/
description/public_reactions_count/user。reactions 作为影响力硬信号
（与 HN points 同地位）进 AI 挑选与转载署名。社区调性对本科生友好。
"""
from __future__ import annotations

import datetime

from app.fetcher import http_get
from app.sources import new_item

DEVTO_URL = "https://dev.to/api/articles"
MAX_ITEMS = 8


def _local_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def parse_article(a: dict) -> dict | None:
    title, url = (a.get("title") or "").strip(), a.get("url")
    if not title or not url:
        return None
    pts = int(a.get("public_reactions_count") or 0)
    item = new_item(
        type_="news", title=title, url=url, source="devto",
        author=((a.get("user") or {}).get("username") or ""),
        published_at=_local_iso(a.get("published_at")),
        summary=(a.get("description") or "").strip(),
    )
    if pts:
        item["points"] = pts   # 影响力硬信号（同 HN points 约定）
    return item


def fetch() -> tuple[list[dict], str, dict]:
    resp = http_get(DEVTO_URL, timeout=12, params={"top": "7", "per_page": "20"})
    arts = resp.json()
    if not isinstance(arts, list):
        raise RuntimeError(f"dev.to 响应异常: {str(arts)[:100]}")
    items = [it for a in arts if (it := parse_article(a))][:MAX_ITEMS]
    if not items:
        raise RuntimeError("dev.to 无合格条目")
    return items, "devto", {"candidates": len(arts), "keep": len(items)}
