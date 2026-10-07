# -*- coding: utf-8 -*-
"""GitHub Trending 源：HTML 主源 + Search API 备源（语义与 v1.1 一致），输出 v2 条目。"""
from __future__ import annotations

import datetime

from app import parser
from app.config import settings
from app.fetcher import http_get
from app.sources import new_item
from app.weekly import week_monday

TRENDING_URL = "https://github.com/trending"
SEARCH_URL = "https://api.github.com/search/repositories"


def _to_v2(raw: dict) -> dict:
    return new_item(
        type_="trending",
        title=raw["full_name"],
        url=raw["url"],
        source="github-trending",
        author=raw["full_name"].split("/", 1)[0],
        summary=raw["desc"],
        trending={"rank": raw["rank"], "language": raw["language"],
                  "stars": raw["stars"], "today_stars": raw["today_stars"]},
    )


def fetch() -> tuple[list[dict], str, dict]:
    """主源 → 备源。返回 (v2 items, 源标签, detail)。全失败抛异常。"""
    detail: dict = {}
    try:
        resp = http_get(TRENDING_URL, params={"since": "daily"})
        raw = parser.parse_trending(resp.text)
        detail["html_size"] = len(resp.text)
        if raw:
            return [_to_v2(x) for x in raw], "github-trending", detail
        detail["parser_note"] = f"Box-row 命中 < {parser.MIN_ROWS}，疑改版"
    except Exception as e:  # noqa: BLE001 单源失败降级
        detail["primary_error"] = str(e)[:200]
    # 备源：7 天内新建仓库按星数排序（语义近似，输出会标注）
    since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    resp = http_get(SEARCH_URL, params={
        "q": f"created:>={since}", "sort": "stars", "order": "desc",
        "per_page": settings.top_n + 5,
    })
    seen: set[str] = set()
    try:
        from app.store import Store
        store = Store()
        seen = {it.get("title") or it["full_name"]
                for d in store.get_daily_since(week_monday(datetime.date.today()))
                for it in d["items"]}
        store.close()
    except Exception:  # noqa: BLE001
        pass
    raw = parser.parse_api(resp.json(), seen)[: settings.top_n]
    if not raw:
        raise RuntimeError(f"备源也无数据: {detail}")
    return [_to_v2(x) for x in raw], "github-api-fallback", detail
