# -*- coding: utf-8 -*-
"""掘金热文源：官方 recommend API（POST JSON，无需登录；2026-10-15 实测）。

热文取 recommend_all_feed（sort_type=200 热度序）；热信号 hot_index（缺则
view_count），normalize_heat 只用批内分位、量纲无所谓。文章 URL =
https://juejin.cn/post/{article_id}，文章页 SSR、extract 直抓全文。
"""
from __future__ import annotations

import requests

from app.fetcher import UA
from app.sources import new_item

API_URL = "https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed"
MAX_ITEMS = 20


def parse_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("err_no") != 0:
        raise RuntimeError(f"掘金 API 异常: {str(payload)[:120]}")
    items: list[dict] = []
    for entry in payload.get("data") or []:
        info = ((entry.get("item_info") or {}).get("article_info") or {})
        title = (info.get("title") or "").strip()
        aid = str(info.get("article_id") or "").strip()
        if not title or not aid:
            continue
        it = new_item(
            type_="news", title=title, url=f"https://juejin.cn/post/{aid}",
            source="juejin",
            published_at=None,
            summary=(info.get("brief_content") or "").strip())
        it["points"] = int(info.get("hot_index") or info.get("view_count") or 0)
        items.append(it)
        if len(items) >= MAX_ITEMS:
            break
    return items


def fetch() -> tuple[list[dict], str, dict]:
    resp = requests.post(API_URL, timeout=15, headers={"User-Agent": UA},
                         json={"id_type": 2, "sort_type": 200,
                               "cursor": "0", "limit": MAX_ITEMS})
    resp.raise_for_status()
    items = parse_payload(resp.json())
    if not items:
        raise RuntimeError("掘金 feed 无条目")
    return items, "juejin", {"keep": len(items)}
