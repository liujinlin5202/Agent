# -*- coding: utf-8 -*-
"""通用 RSS/Atom 源（2026-09-17 v4.1 加源；2026-10-15 扩到十三源）。

覆盖 Ars Technica / IEEE Spectrum / freeCodeCamp / 阮一峰周刊 /
lobste.rs / The Verge / TechCrunch / InfoQ / OSCHINA /
Netflix / Cloudflare / Meta / 美团技术团队，十三源只差
URL 和条数上限，解析共用：RSS2 <item> 与 Atom <entry> 双格式。
summary 取 description/summary（缺席回退 content:encoded/content），剥 HTML
截 300 字——feed 不承担全文，选中后走 extract.fetch_article 抓原文页。

订阅型源均无投票热度（编辑精选/期刊型），AI 挑选时按「来源权威性 + 时效」评估。
"""
from __future__ import annotations

import datetime
import html as html_mod
import re
import xml.etree.ElementTree as ET

from app.fetcher import http_get
from app.sources import new_item

ARS_URL = "https://arstechnica.com/feed/"
IEEE_URL = "https://spectrum.ieee.org/feeds/topic/computing.rss"
FCC_URL = "https://www.freecodecamp.org/news/rss/"
RUANYIFENG_URL = "https://www.ruanyifeng.com/blog/atom.xml"
# 2026-10-15 新源（URL 均当日实测 200；solidot 本机直连+代理均 000 不加）
LOBSTERS_URL = "https://lobste.rs/rss"
VERGE_URL = "https://www.theverge.com/rss/index.xml"
TECHCRUNCH_URL = "https://techcrunch.com/feed/"
INFOQ_URL = "https://feed.infoq.com"
OSCHINA_URL = "https://www.oschina.net/news/rss"
# 2026-10-15 二批：官方工程博客（research 长文密度最高的内容形态；URL 当日实测 200。
# Uber/字节/腾讯/阿里无公开 RSS——公众号首发是业界常态，不硬凑聚合站）
NETFLIX_URL = "https://netflixtechblog.com/feed"
CLOUDFLARE_URL = "https://blog.cloudflare.com/rss/"
METAENG_URL = "https://engineering.fb.com/feed/"
MEITUAN_URL = "https://tech.meituan.com/feed/"

_NS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_NS_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
_NS_ATOM = "{http://www.w3.org/2005/Atom}"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str, cap: int = 300) -> str:
    t = html_mod.unescape(_TAG_RE.sub(" ", text or ""))
    return _WS_RE.sub(" ", t).strip()[:cap]


def _local_iso(value: str | None) -> str | None:
    """pubDate(RFC822) / Atom ISO → 本地 ISO；解析不了返回 None 不抛。"""
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.datetime.strptime(v, fmt).astimezone().isoformat(
                timespec="seconds")
        except ValueError:
            pass
    try:
        dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def _rss_entry(item: ET.Element, source: str) -> dict | None:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    if not title or not link:
        return None
    desc = item.findtext("description") or ""
    if not desc.strip():
        desc = item.findtext(_NS_CONTENT) or ""
    author = (item.findtext(_NS_DC_CREATOR) or "").strip()
    return new_item(type_="news", title=title, url=link, source=source,
                    author=author, published_at=_local_iso(item.findtext("pubDate")),
                    summary=_clean(desc))


def _atom_link(entry: ET.Element) -> str:
    for le in entry.findall(_NS_ATOM + "link"):
        if le.get("rel") in (None, "alternate") and le.get("href"):
            return le.get("href").strip()
    return ""


def _atom_entry(entry: ET.Element, source: str) -> dict | None:
    title = (entry.findtext(_NS_ATOM + "title") or "").strip()
    link = _atom_link(entry)
    if not title or not link:
        return None
    content = entry.findtext(_NS_ATOM + "content") or ""
    if not content.strip():
        content = entry.findtext(_NS_ATOM + "summary") or ""
    author_el = entry.find(_NS_ATOM + "author/" + _NS_ATOM + "name")
    author = (author_el.text or "").strip() if author_el is not None else ""
    pub = entry.findtext(_NS_ATOM + "published") or entry.findtext(_NS_ATOM + "updated")
    return new_item(type_="news", title=title, url=link, source=source,
                    author=author, published_at=_local_iso(pub),
                    summary=_clean(content))


def parse_feed(xml_text: str, source: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "item":
            if it := _rss_entry(el, source):
                items.append(it)
        elif tag == "entry":
            if it := _atom_entry(el, source):
                items.append(it)
    return items


def fetch_feed(source: str, url: str, max_items: int) -> tuple[list[dict], str, dict]:
    resp = http_get(url, timeout=15)
    items = parse_feed(resp.text, source)[:max_items]
    if not items:
        raise RuntimeError(f"feed 无条目: {url}")
    return items, source, {"keep": len(items)}


def fetch_ars() -> tuple[list[dict], str, dict]:
    return fetch_feed("ars-technica", ARS_URL, 10)


def fetch_ieee() -> tuple[list[dict], str, dict]:
    return fetch_feed("ieee-spectrum", IEEE_URL, 12)


def fetch_fcc() -> tuple[list[dict], str, dict]:
    return fetch_feed("freecodecamp", FCC_URL, 8)


def fetch_ruanyifeng() -> tuple[list[dict], str, dict]:
    return fetch_feed("ruanyifeng", RUANYIFENG_URL, 5)


def fetch_lobsters() -> tuple[list[dict], str, dict]:
    return fetch_feed("lobste-rs", LOBSTERS_URL, 10)


def fetch_verge() -> tuple[list[dict], str, dict]:
    return fetch_feed("the-verge", VERGE_URL, 10)


def fetch_techcrunch() -> tuple[list[dict], str, dict]:
    return fetch_feed("techcrunch", TECHCRUNCH_URL, 10)


def fetch_infoq() -> tuple[list[dict], str, dict]:
    return fetch_feed("infoq", INFOQ_URL, 10)


def fetch_oschina() -> tuple[list[dict], str, dict]:
    return fetch_feed("oschina", OSCHINA_URL, 10)


def fetch_netflix() -> tuple[list[dict], str, dict]:
    return fetch_feed("netflix-tech", NETFLIX_URL, 10)


def fetch_cloudflare() -> tuple[list[dict], str, dict]:
    return fetch_feed("cloudflare", CLOUDFLARE_URL, 10)


def fetch_metaeng() -> tuple[list[dict], str, dict]:
    return fetch_feed("meta-eng", METAENG_URL, 10)


def fetch_meituan() -> tuple[list[dict], str, dict]:
    return fetch_feed("meituan-tech", MEITUAN_URL, 10)
