# -*- coding: utf-8 -*-
"""WebSearch 模块：站内相关内容不足时自动搜索站外资料补充。

Provider：bing（默认）——pod 直连 www.bing.com 实测可达（2026-09-08）；
duckduckgo（duckduckgo_search 库）在 pod 上其域名不可达（Network unreachable），
代码保留但勿作默认。任何失败均静默返回空列表，不抛异常。
"""
from __future__ import annotations

import base64
import time
import urllib.parse
from typing import Any, TypedDict

import httpx

from app.config import settings


class WebSearchResult(TypedDict):
    title: str
    snippet: str
    url: str


def web_search(query: str, max_results: int | None = None) -> list[WebSearchResult]:
    """通用 web search 入口，按 provider 路由。

    失败时返回空列表（静默降级，不抛异常）。
    """
    if not settings.web_search_enabled:
        return []
    provider = settings.web_search_provider
    limit = max_results or settings.web_search_max_results
    try:
        if provider == "bing":
            return _search_bing(query, limit)
        if provider == "duckduckgo":
            return _search_duckduckgo(query, limit)
        print(f"[web_search] 未知 provider: {provider}", flush=True)
        return []
    except Exception as e:  # noqa: BLE001
        print(f"[web_search] {provider} 搜索失败: {repr(e)}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Bing（爬结果页 HTML；pod 出口实测可达）
# ---------------------------------------------------------------------------

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_BING_URL = "https://www.bing.com/search?q={q}&mkt=zh-CN&setlang=zh-hans&count={n}"


def _clean_bing_url(href: str) -> str:
    """解码 Bing 跳转链接（/ck/a?...&u=a1<base64> 形式）→ 原始 URL；无效则空串。"""
    if "/ck/a" not in href:
        return href
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    u = (qs.get("u") or [""])[0]
    if not u.startswith("a1"):
        return ""
    try:
        return base64.urlsafe_b64decode(u[2:] + "===").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def _extract_bing_results(page_html: str, max_results: int) -> list[WebSearchResult]:
    """解析 Bing 结果页：<li class="b_algo"> 内 h2>a（标题/链接）+ p（摘要）。"""
    from html.parser import HTMLParser

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.items: list[WebSearchResult] = []
            self._cur: dict[str, str] | None = None
            self._in_title = False
            self._in_snippet = False

        def handle_starttag(self, tag, attrs):
            cls = dict(attrs).get("class") or ""
            if tag == "li" and "b_algo" in cls and self._cur is None:
                self._cur = {"title": "", "url": "", "snippet": ""}
                return
            if self._cur is None:
                return
            if tag == "h2":
                self._in_title = True
            elif tag == "a" and self._in_title and not self._cur["url"]:
                self._cur["url"] = _clean_bing_url(dict(attrs).get("href") or "")
            elif tag == "p" and not self._in_snippet and not self._cur["snippet"]:
                self._in_snippet = True

        def handle_endtag(self, tag):
            if self._cur is None:
                return
            if tag == "h2":
                self._in_title = False
            elif tag == "p":
                self._in_snippet = False
            elif tag == "li":
                cur, self._cur = self._cur, None
                title = " ".join(cur["title"].split())
                snippet = " ".join(cur["snippet"].split())
                if cur["url"] and (title or snippet):
                    self.items.append(WebSearchResult(title=title, snippet=snippet, url=cur["url"]))

        def handle_data(self, data):
            if self._cur is None:
                return
            if self._in_title:
                self._cur["title"] += data
            elif self._in_snippet:
                self._cur["snippet"] += data

    parser = _Parser()
    parser.feed(page_html)
    return parser.items[:max_results]


# 词典/工具站结果对"找讨论观点"无益（如"副"字释义），直接过滤；社区类内容优先
_JUNK_URL_HINTS = ("zdic", "hanyu", "cidian", "guoxuedashi", "wanshupu", "kaipian", "hgcha")
_JUNK_TITLE_HINTS = ("词典", "字典", "释义", "新华", "汉字", "成语", "拼音", "组词", "笔顺", "汉语文字")
_PREFERRED_URL_HINTS = (
    "zhihu.com", "xiaohongshu.com", "tieba.baidu.com", "zhidao.baidu.com",
    "baijiahao.baidu.com", "bilibili.com", "douban.com", "csdn.net", "juejin.cn",
    "wenku.baidu.com", "360doc.com", "doc88.com",
)


def _rank_bing_results(items: list[WebSearchResult], max_results: int) -> list[WebSearchResult]:
    """过滤低质结果（字典/词条页）并优先高价值社区，取前 max_results。"""
    keep: list[WebSearchResult] = []
    for r in items:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        if not url or not title:
            continue
        if len(title) < 6:  # 纯单字/短词条（如"副"）
            continue
        if any(h in (url + title).lower() for h in _JUNK_URL_HINTS):
            continue
        if any(h in title for h in _JUNK_TITLE_HINTS):
            continue
        # 百度百科单字词条（如"副"）不算有用结果（"保研"这类 2 字条目保留）
        if "baike.baidu.com/item/" in url:
            name = urllib.parse.unquote(
                url.split("baike.baidu.com/item/", 1)[1].split("?")[0].split("/")[0])
            if len(name) <= 1:
                continue
        keep.append(r)
    pref = [r for r in keep
            if any(h in (r["url"] + r["title"]).lower() for h in _PREFERRED_URL_HINTS)]
    rest = [r for r in keep if r not in pref]
    return (pref + rest)[:max_results]


def _search_bing(query: str, max_results: int) -> list[WebSearchResult]:
    """Bing 网页搜索（爬 HTML 解析 b_algo 结果，过滤低质词条页）。"""
    q = urllib.parse.quote(query)
    url = _BING_URL.format(q=q, n=20)  # 多取再过滤，保证过滤后仍有足够结果
    t0 = time.time()
    with httpx.Client(follow_redirects=True, timeout=settings.web_search_timeout) as client:
        resp = client.get(url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        resp.raise_for_status()
    print(f"[web_search] bing 页面 {len(resp.text)} bytes, {int((time.time() - t0) * 1000)}ms", flush=True)
    results = _rank_bing_results(_extract_bing_results(resp.text, 20), max_results)
    if not results:
        print("[web_search] bing 无可用结果（可能被反爬或改版）", flush=True)
    else:
        print(f"[web_search] bing 过滤后 {len(results)} 条", flush=True)
    return results


# ---------------------------------------------------------------------------
# DuckDuckGo（保留：pod 域名不可达，勿作默认）
# ---------------------------------------------------------------------------

def _search_duckduckgo(query: str, max_results: int) -> list[WebSearchResult]:
    """使用 duckduckgo_search 库搜索。"""
    from duckduckgo_search import DDGS

    timeout = settings.web_search_timeout
    results: list[WebSearchResult] = []
    t0 = time.time()
    with DDGS(timeout=timeout) as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            if time.time() - t0 > timeout:
                print("[web_search] DuckDuckGo 搜索超时", flush=True)
                break
            title = (r.get("title") or "").strip()
            snippet = (r.get("body") or r.get("snippet") or "").strip()
            url = (r.get("href") or r.get("link") or "").strip()
            if title and snippet:
                results.append(WebSearchResult(title=title, snippet=snippet, url=url))
    return results


def format_web_results(results: list[WebSearchResult]) -> str:
    """将 WebSearch 结果格式化为 prompt 可用的文本块。"""
    if not results:
        return ""
    lines = ["\n【站外搜索结果】（以下内容来自 Web 搜索，仅供参考）"]
    for i, r in enumerate(results, start=1):
        lines.append(f"  🌐 [{i}] {r['title']}")
        lines.append(f"      摘要：{r['snippet']}")
        lines.append(f"      链接：{r['url']}")
    lines.append("（站外信息可能与站内讨论角度不同，请注意甄别）\n")
    return "\n".join(lines)
