# -*- coding: utf-8 -*-
"""镜像通道：wallstreetcn 文章页是 SPA（直抓只有空壳），改走其内容 API。

背景（2026-09-21 实测）：候选选中华尔街见闻文章时 extract.fetch_article 拿到的是
SPA 空壳（<500 字符）；而 api-one-wscn 内容 API 按文章 id 确定性返回全文 HTML，
两天多轮验证稳定——不需要任何搜索/发现环节。知乎/公众号 403 面不走本模块
（数据中心 IP 上一切「按标题搜镜像」的端点数小时内衰减，实测证伪），它们继续
走 digest 导读降级。
"""
from __future__ import annotations

import re

from app.extract import extract_article
from app.fetcher import http_get

_ARTICLE_URL_RE = re.compile(
    r"https?://(?:www\.)?wallstreetcn\.com/articles/(\d+)", re.I)
CONTENT_API = ("https://api-one-wscn.awtmt.com/apiv1/content/"
               "articles/{id}?extract=0")

MIN_TEXT_CHARS = 500   # 与 extract.fetch_article 同一口径：过短视为不可用


def needs_mirror(url: str) -> bool:
    """该 URL 是否走镜像通道（wallstreetcn 文章页 = SPA 空壳）。"""
    return bool(_ARTICLE_URL_RE.search(url or ""))


def _author_name(raw) -> str:
    """作者字段兼容 dict（{display_name}）与纯字符串两种返回。"""
    if isinstance(raw, dict):
        return str(raw.get("display_name") or "").strip()
    return str(raw or "").strip()


def mirror_fetch(url: str, *, timeout: int = 12) -> dict | None:
    """wallstreetcn 文章 → 内容 API 全文。返回 {"text", "author"}；不可用 → None。

    fail-open 由调用方处理：本函数只抛网络异常。
    """
    m = _ARTICLE_URL_RE.search(url or "")
    if not m:
        return None
    resp = http_get(CONTENT_API.format(id=m.group(1)), timeout=timeout)
    data = resp.json() or {}
    if str(data.get("code")) != "20000":
        return None
    d = data.get("data") or {}
    r = extract_article(d.get("content") or "")
    if len(r["text"]) < MIN_TEXT_CHARS:
        return None
    return {"text": r["text"], "author": _author_name(d.get("author"))}
