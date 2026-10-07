# -*- coding: utf-8 -*-
"""全文提取器：从原文 HTML 提纯文本，供全文转载/翻译用。零新依赖（HTMLParser）。

策略：优先 <article> 块（独立博客的正文容器），没有则退回 <body>；
script/style/nav/footer/aside 等噪声标签，以及评论区/侧栏/订阅框等噪声容器
（按 id/class 识别，dev.to 的评论区整块嵌在 <article> 里，2026-09-21 实测）
整块剔除；块级标签归并为段落（\n\n）。超长截断到 EXTRACT_CAP——翻译层再按
中文预算二次裁剪。
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

EXTRACT_CAP = 60_000

_SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript",
         "form", "button", "svg", "iframe", "select", "template"}
# id/class 命中即整块跳过的噪声容器（评论区资料卡曾污染全文，见 test_extract）
_JUNK_ATTR_RE = re.compile(r"comment|sidebar|newsletter|subscribe", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "br",
          "tr", "blockquote", "section", "table", "ul", "ol", "figure", "figcaption"}

_META_AUTHOR_RE = re.compile(
    r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)',
    re.I)
_ARTICLE_RE = re.compile(r"<article\b", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _junk_attrs(attrs: list[tuple[str, str | None]]) -> bool:
    return any(k in ("id", "class") and v and _JUNK_ATTR_RE.search(v)
               for k, v in attrs)


class _TextGrab(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_stack: list[str] = []   # 噪声标签/容器的未闭合栈（容忍错位闭合）
        self._pre = 0                      # <pre> 深度：内部文本原样保留（\x00 哨兵定界）

    def handle_starttag(self, tag, attrs):
        if self._skip_stack:
            if tag not in _VOID:
                self._skip_stack.append(tag)
            return
        if tag in _SKIP or _junk_attrs(attrs):
            self._skip_stack.append(tag)
        elif tag == "pre":
            self._pre += 1
            self.parts.append("\x00")
        elif tag in _BLOCK and self.parts:
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        if not self._skip_stack and tag == "br":
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if self._skip_stack:
            if tag in self._skip_stack:
                while self._skip_stack and self._skip_stack.pop() != tag:
                    pass
            return
        if tag == "pre" and self._pre:
            self._pre -= 1
            self.parts.append("\x00")
        if tag in _BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self._skip_stack:
            return
        if self._pre:
            self.parts.append(data)   # 代码：换行/缩进/空行原样保留
        elif data.strip():
            self.parts.append(data)


def _slice_article(html_text: str) -> str:
    """有 <article> 就只取该块（同页若有多个取最长），无则原文。"""
    starts = [m.start() for m in _ARTICLE_RE.finditer(html_text)]
    if not starts:
        return html_text
    best = ""
    for s in starts:
        close = html_text.find("</article>", s)
        chunk = html_text[s:close if close != -1 else s + 200_000]
        if len(chunk) > len(best):
            best = chunk
    return best if best.strip() else html_text


def extract_article(html_text: str) -> dict:
    """HTML → {"text": 段落化纯文本, "author": meta author 或 ""}。

    普通段落做空白折叠；\\x00 哨兵包住的是 <pre> 原文，缩进原样保留
    （旧实现 _WS_RE 会把 pre 内连续空格/制表符压成单空格——Python/YAML 类
    缩进即语义的代码塌陷后不可读）。
    """
    p = _TextGrab()
    p.feed(_slice_article(html_text))
    text = unescape("".join(p.parts))
    blocks: list[str] = []
    for i, seg in enumerate(text.split("\x00")):
        if i % 2:                     # 奇数段 = pre 内部
            pre = seg.strip("\n").rstrip()
            if pre.strip():
                blocks.append(pre)
        else:
            paras = (_WS_RE.sub(" ", ln).strip() for ln in seg.split("\n\n"))
            blocks.extend(ln for ln in paras if ln)
    text = "\n\n".join(blocks)[:EXTRACT_CAP]
    am = _META_AUTHOR_RE.search(html_text)
    return {"text": text, "author": (am.group(1).strip() if am else "")}


def is_mostly_chinese(text: str) -> bool:
    """正文是否以中文为主（决定全文转载走「直用」还是「AI 翻译」）。

    口径：CJK 字数 ≥ 全文字母数字总量的一半。按总长度会被空格/标点稀释，
    混代码样例的中文技术文会误判成英文。
    """
    t = text or ""
    cjk = len(_CJK_RE.findall(t))
    if not cjk:
        return False
    return cjk >= 0.5 * (cjk + len(_ASCII_WORD_RE.findall(t)))


_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]")


def fetch_article(url: str, *, timeout: int = 12) -> dict | None:
    """抓原文并提取。全文过短（<500 字符）视为不可抓（SPA 空壳/付费墙），返回 None。

    fail-open 由调用方处理：本函数只抛网络异常。
    """
    from app.fetcher import http_get

    resp = http_get(url, timeout=timeout)
    r = extract_article(resp.text)
    if len(r["text"]) < 500:
        return None
    return r
