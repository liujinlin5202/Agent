# -*- coding: utf-8 -*-
"""华尔街见闻源（2026-09-21 v4.5 加源）：行业深度/大厂动态的中文候选。

information-flow 列表 API（实测稳定）→ 外层 resource_type 过滤 + 内层 resource
取 id/title/author/content_short/display_time；is_paid/is_priced（付费/标价）与
非 article 类型（live 快讯）一律跳过。候选 url 用 wallstreetcn.com/articles/{id}，
正文由 app/mirror.py 走内容 API 取（文章页是 SPA，直抓只有空壳）。
"""
from __future__ import annotations

import datetime

from app.fetcher import http_get
from app.sources import new_item

FLOW_API = ("https://api-one-wscn.awtmt.com/apiv1/content/"
            "information-flow?accept=article&limit=30")
MAX_ITEMS = 12


def _iso(ts) -> str | None:
    """epoch 秒 → 本地 ISO；解析不了返回 None 不抛。"""
    try:
        return datetime.datetime.fromtimestamp(int(ts)).astimezone().isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _author_name(raw) -> str:
    """作者字段兼容 dict（{display_name}）与纯字符串两种返回。"""
    if isinstance(raw, dict):
        return str(raw.get("display_name") or "").strip()
    return str(raw or "").strip()


def parse_flow(payload: dict) -> list[dict]:
    """information-flow JSON → 统一 v2 条目（live/付费/标价/缺标题项跳过）。"""
    items = (payload.get("data") or {}).get("items") or []
    out: list[dict] = []
    for wrap in items:
        res = wrap.get("resource") or {}
        title = str(res.get("title") or "").strip()
        if not title or wrap.get("resource_type") != "article":
            continue
        if res.get("is_paid") or res.get("is_priced"):
            continue
        out.append(new_item(
            type_="news", title=title,
            url=f"https://wallstreetcn.com/articles/{res.get('id')}",
            source="wallstreetcn", author=_author_name(res.get("author")),
            published_at=_iso(res.get("display_time")),
            summary=str(res.get("content_short") or "").strip()[:300]))
    return out


def fetch() -> tuple[list[dict], str, dict]:
    resp = http_get(FLOW_API, timeout=15)
    items = parse_flow(resp.json() or {})[:MAX_ITEMS]
    if not items:
        raise RuntimeError("wscn information-flow 无有效条目")
    return items, "wallstreetcn", {"keep": len(items)}
