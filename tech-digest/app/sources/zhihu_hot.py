# -*- coding: utf-8 -*-
"""知乎热榜源：经 tophub.today 中转（知乎官方 API 已要登录，/hot 页 SSR 为空）。

抓 tophub 知乎热榜页（n/mproPpoq6O），提取 zhihu.com/question 链接 + 排名 + 热度。
只能拿到标题与热度排名，拿不到回答全文（知乎 403 反爬）——全文转载路线在
extract/翻译层另行降级。零新依赖，正则解析。
"""
from __future__ import annotations

import re

from app.fetcher import http_get
from app.sources import new_item

PAGE_URL = "https://tophub.today/n/mproPpoq6O"
MAX_ITEMS = 20

# 真实结构（2026-09-17 实抓）：<td>N.</td> 排名 + <a href=zhihu question 链接> 标题
# + <div class="item-desc">4283 万热度</div>；同链接桌面/移动双份出现，按 URL 去重保首次
_ROW_RE = re.compile(
    r'<a[^>]*href="(https://www\.zhihu\.com/question/[^"]+)"[^>]*>([^<]+)</a>'
    r'(?:.{0,400}?<div class="item-desc">([\d,]+)\s*[^<]*</div>)?',
    re.S)
_ZHIME_RE = re.compile(r"([\d,]+)")


def parse_page(html_text: str) -> list[dict]:
    """解析 tophub 知乎热榜页 → v2 条目（source=zhihu-hot，热度进 trending.heat）。"""
    items: list[dict] = []
    seen: set[str] = set()
    for m in _ROW_RE.finditer(html_text):
        url, title, heat_raw = m.group(1), m.group(2).strip(), m.group(3)
        if url in seen or not title:
            continue
        seen.add(url)
        heat = 0
        if heat_raw:
            hm = _ZHIME_RE.match(heat_raw.replace(",", ""))
            heat = int(hm.group(1)) if hm else 0
        items.append(new_item(
            type_="news", title=title, url=url, source="zhihu-hot",
            trending={"rank": len(items) + 1, "heat": heat}),
        )
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise ValueError("tophub 页面未解析到知乎热榜条目（疑改版或登录墙）")
    return items


def fetch() -> tuple[list[dict], str, dict]:
    html_text = http_get(PAGE_URL, timeout=15).text
    items = parse_page(html_text)
    return items, "zhihu-hot", {"raw_len": len(html_text), "keep": len(items)}
