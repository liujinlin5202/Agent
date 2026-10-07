# -*- coding: utf-8 -*-
"""少数派（sspai）RSS 源：标准库 xml.etree 解析，零新依赖。

字段：title / link / pubDate（RFC822 → 本地 ISO）/ dc:creator / description（去 HTML 截断）。
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from app.fetcher import http_get
from app.sources import new_item

FEED_URL = "https://sspai.com/feed"
MAX_ITEMS = 20
MAX_SUMMARY = 200

# 生活/消费类噪声过滤（技术社区不播报）；命中即跳过，计入 stats
LIFE_TITLE_RE = re.compile(
    r"有奖|选购|烟灶|本周看什么|影视|影评|追剧|优惠|促销|购物|双11|618", re.I)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_html(raw: str) -> str:
    """description 纯文本化：去标签 → unescape → 压空白 → 截断。"""
    t = _TAG_RE.sub(" ", raw or "")
    t = html.unescape(t)
    return _WS_RE.sub(" ", t).strip()[:MAX_SUMMARY]


def parse_feed(xml_text: str) -> list[dict]:
    """解析 RSS。返回 v2 条目（source=sspai）。异常条目跳过。

    返回 (items, skipped)：skipped 为生活类被过滤条数（供自检统计）。
    """
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    skipped = 0
    for el in root.iter():
        if not el.tag.lower().endswith("item"):
            continue
        fields: dict[str, str] = {}
        for child in el:
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if tag in ("title", "link", "pubdate", "creator", "description"):
                fields[tag] = (child.text or "").strip()
        title, url = fields.get("title", ""), fields.get("link", "")
        if not title or not url:
            continue
        if LIFE_TITLE_RE.search(title):
            skipped += 1
            continue
        published = None
        if fields.get("pubdate"):
            try:
                published = (parsedate_to_datetime(fields["pubdate"])
                             .astimezone().isoformat(timespec="seconds"))
            except (TypeError, ValueError):
                published = None
        items.append(new_item(
            type_="news", title=title, url=url, source="sspai",
            author=fields.get("creator", ""), published_at=published,
            summary=clean_html(fields.get("description", "")),
        ))
        if len(items) >= MAX_ITEMS:
            break
    return items, skipped


def fetch() -> tuple[list[dict], str, dict]:
    xml_text = http_get(FEED_URL, timeout=15).text
    items, skipped = parse_feed(xml_text)
    if not items:
        raise RuntimeError("RSS 解析无条目（疑改版或网关拦截）")
    return items, "sspai", {"raw_len": len(xml_text), "keep": len(items),
                            "life_filtered": skipped}
