# -*- coding: utf-8 -*-
"""解析模块：GitHub Trending HTML 主源 + Search API 备源。

2026-08-30 MVP 实验校准（见技术文档 §2.1.1）：
- 条目容器 article.Box-row（19 条实测）
- 顶部星数 a[href$="/stargazers"]（a 标签仍存在）
- 今日新增在底部 span.d-inline-block.float-sm-right 内为纯文本 "N stars today"（已无 a 标签）
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

# 结构探测阈值：Box-row 命中数低于此值判为改版
MIN_ROWS = 5


def parse_num(text: str) -> int:
    """'2.3k' -> 2300, '1.2m' -> 1200000, '5,678' -> 5678"""
    t = (text or "").strip().lower().replace(",", "")
    if not t:
        return 0
    m = re.match(r"^([\d.]+)\s*([km]?)$", t)
    if not m:
        return 0
    val = float(m.group(1))
    suffix = m.group(2)
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    return int(val)


def parse_trending(html: str) -> list[dict]:
    """解析 Trending HTML。结构不符合预期时返回空列表（调用方走备源）。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("article.Box-row")
    if len(rows) < MIN_ROWS:
        return []
    items = []
    for i, row in enumerate(rows, 1):
        a = row.select_one("h2 a")
        if a is None:
            continue
        href = a.get("href", "").strip("/")
        if not href or "/" not in href:
            continue
        lang_el = row.select_one('span[itemprop="programmingLanguage"]')
        lang = lang_el.get_text(strip=True) if lang_el else None
        star_counts = row.select('a[href$="/stargazers"]')
        stars = parse_num(star_counts[0].get_text()) if star_counts else 0
        today_stars = 0
        bottom = row.select_one("span.d-inline-block.float-sm-right")
        if bottom:
            txt = bottom.get_text(" ", strip=True).lower()
            m = re.search(r"([\d,]+)\s*stars?\s*today", txt)
            if m:
                today_stars = int(m.group(1).replace(",", ""))
        desc_el = row.select_one("p")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        items.append({
            "rank": i,
            "full_name": href,
            "url": f"https://github.com/{href}",
            "language": lang,
            "stars": stars,
            "today_stars": today_stars,
            "desc": desc or "(无描述)",
        })
    return items


def parse_api(json_data: dict, seen_full_names: set[str] | None = None) -> list[dict]:
    """解析 Search API 响应（降级源）。seen: 已入库仓库过滤（避免周报重复）；响应内重复自动去重。"""
    seen = set(seen_full_names or set())
    items = []
    for i, repo in enumerate(json_data.get("items", []), 1):
        full_name = repo.get("full_name", "")
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        items.append({
            "rank": i,
            "full_name": full_name,
            "url": repo.get("html_url", f"https://github.com/{full_name}"),
            "language": repo.get("language"),
            "stars": repo.get("stargazers_count", 0),
            "today_stars": 0,  # Search API 无法取得今日增量
            "desc": repo.get("description") or "(无描述)",
        })
    return items
