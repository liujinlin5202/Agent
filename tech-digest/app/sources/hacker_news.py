# -*- coding: utf-8 -*-
"""Hacker News 源。

主：Algolia 官方附属 API /api/v1/search?tags=front_page（单请求批量返回首页故事，
    含 url/points/author/created_at，国内直连 ~1.5s，实测稳定）。
兜底：Firebase 官方 API（topstories → item 逐条，国内 ~20s/条很慢，仅截断到 8 条）。
"""
from __future__ import annotations

import datetime

from app.fetcher import http_get
from app.sources import new_item

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
TOPSTORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
MIN_SCORE = 20
MAX_ITEMS = 15           # 收录上限（有 url 且 score 达标的）
FALLBACK_ITEMS = 8       # Firebase 兜底上限（该通道太慢，压缩预算）
CANDIDATES = MAX_ITEMS * 2


def _local_iso(value: int | str | None) -> str | None:
    """Unix 时间戳或 Algolia ISO（UTC）→ 服务器本地 ISO(+08:00)。"""
    if not value:
        return None
    try:
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            dt = datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)
        else:
            dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _make(url: str, title: str, author: str, published: str | None,
          points: int = 0) -> dict:
    item = new_item(
        type_="news", title=title, url=url, source="hacker-news",
        author=author or "", published_at=published, summary="",  # HN 无原文摘要，由 AI 生成
    )
    if points:
        item["points"] = points   # v4 影响力硬信号（挑爆文 + 转载署名背书）
    return item


# ---------------- Algolia（主） ----------------

def parse_hit(hit: dict) -> dict | None:
    """Algolia 单条：必须有外链 + points>=MIN_SCORE。"""
    title, url = hit.get("title"), hit.get("url")
    if not title or not url:
        return None
    pts = int(hit.get("points", 0) or 0)
    if pts < MIN_SCORE:
        return None
    return _make(url, title, hit.get("author"), _local_iso(hit.get("created_at")), pts)


def _fetch_algolia() -> tuple[list[dict], str, dict]:
    resp = http_get(ALGOLIA_URL, timeout=12,
                    params={"tags": "front_page", "hitsPerPage": str(CANDIDATES)})
    hits = (resp.json() or {}).get("hits")
    if not isinstance(hits, list):
        raise RuntimeError("Algolia 响应异常")
    items = [it for h in hits if (it := parse_hit(h))][: MAX_ITEMS]
    if not items:
        raise RuntimeError(f"Algolia 无合格条目: {hits[:1]}")
    return items, "hacker-news", {"api": "algolia", "candidates": len(hits), "keep": len(items)}


# ---------------- Firebase（兜底） ----------------

def parse_item(raw: dict) -> dict | None:
    """Firebase 单条校正/过滤：必须 story、有外链、score>=MIN_SCORE。不满足返回 None。"""
    if raw.get("type") != "story" or raw.get("deleted") or raw.get("dead"):
        return None
    url, title = raw.get("url"), raw.get("title")
    if not url or not title:
        return None
    pts = int(raw.get("score", 0) or 0)
    if pts < MIN_SCORE:
        return None
    return _make(url, title, raw.get("by"), _local_iso(raw.get("time")), pts)


def _fetch_firebase(max_items: int) -> tuple[list[dict], str, dict]:
    ids = http_get(TOPSTORIES_URL, timeout=12).json()
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("topstories 响应异常")
    items: list[dict] = []
    errors = 0
    for sid in ids[: max_items * 3]:
        if len(items) >= max_items:
            break
        try:
            raw = http_get(ITEM_URL.format(id=sid), timeout=10, retries=1).json()
        except Exception:  # noqa: BLE001 单条失败跳过
            errors += 1
            continue
        it = parse_item(raw)
        if it:
            items.append(it)
    if not items:
        raise RuntimeError(f"Firebase 无合格条目: {errors} 错误")
    return items, "hacker-news", {"api": "firebase", "candidates": len(ids),
                                  "keep": len(items), "errors": errors}


def fetch() -> tuple[list[dict], str, dict]:
    try:
        return _fetch_algolia()
    except Exception as alg_err:  # noqa: BLE001 走兜底
        try:
            return _fetch_firebase(FALLBACK_ITEMS)
        except Exception as fb_err:  # noqa: BLE001
            raise RuntimeError(f"Algolia({alg_err}); Firebase({fb_err})") from fb_err
