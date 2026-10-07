# -*- coding: utf-8 -*-
"""全文提取器：从原文 HTML 提纯文本，供全文转载/翻译用。零新依赖（HTMLParser）。

策略：优先 <article> 块（独立博客的正文容器），没有则退回 <body>；
script/style/nav/footer/aside 等噪声标签，以及评论区/侧栏/订阅框等噪声容器
（按 id/class 识别，dev.to 的评论区整块嵌在 <article> 里，2026-09-21 实测）
整块剔除；块级标签归并为段落（\n\n）。超长截断到 EXTRACT_CAP——翻译层再按
中文预算二次裁剪。

正文级质量网（2026-10-07 sspai 事故）：少数派把页头作者卡/分享面板/页尾
版权卡整段包进 <article>，且中文全文直发不过翻译层，家具原样进了帖子。
容器名单按站点枚举永远追不全，故叠加两道文本层兜底——① 短块在邻域窗口内
聚发 ≥3 次（作者卡重影）；② 已知推广/交互文案模式。都只删非 <pre> 块。
"""
from __future__ import annotations

import logging
import re
from html import unescape
from html.parser import HTMLParser

log = logging.getLogger("tech-digest.extract")

EXTRACT_CAP = 60_000

_SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript",
         "form", "button", "svg", "iframe", "select", "template"}
# id 只认原始四词：新词在 id 上会误伤标题锚点（claude.dev 实测
# id="share-the-chart-or-screenshot-itself" 的 H3 曾被整行删掉）
_JUNK_ID_RE = re.compile(r"comment|sidebar|newsletter|subscribe", re.I)
# class 命中即整块跳过的噪声容器（评论区资料卡曾污染全文，见 test_extract；
# sspai 页头/页尾与 freecodecamp banner 广告皆为类名命中——2026-10-07 实测）
_JUNK_CLASS_RE = re.compile(
    r"comment|sidebar|newsletter|subscribe|share|footer|popover|promo|banner|"
    r"copyright|related|recommend|toolbar|breadcrumb|author-card|"
    r"article__important|article__charge|article__header", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "br",
          "tr", "blockquote", "section", "table", "ul", "ol", "figure", "figcaption"}

_META_AUTHOR_RE = re.compile(
    r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)',
    re.I)
_ARTICLE_RE = re.compile(r"<article\b", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")

# ---- 正文级质量网阈值（2026-10-07） ----
_WIDGET_RE = re.compile(
    r"微信扫码分享|点击下方按钮可复制链接|本文责编|位派友已充电|著作权归作者|"
    r"未经.{0,8}许可.{0,8}转载|关注少数派小红书|少数派为你呈现|Matrix 首页推荐")
_WIDGET_MAX_CHARS = 60   # 只认短块：长段落里出现这些词是正常行文
_REPEAT_MAX_CHARS = 80   # 重复块只可能是人名/标签类家具，长正文重复不归它管
_REPEAT_MIN_COUNT = 3
_REPEAT_WINDOW = 15      # 窗口内聚发才算：芯片标签（PROMPT 跨 23 块）与表格行
                         # 残片（跨 24 块）是分散重复；家具重影实测都 ≤15（2026-10-07）
# 少数派 Matrix 专栏每篇固定的社区声明（实测逐字相同，按前缀删）
_BOILERPLATE_PREFIXES = ("Matrix 是少数派的写作社区", "文章代表作者个人观点")
# 少数派把推广行写成 <p>&gt; ...</p> 字面文本——提取后以「> 」开头的块是家具
# 铁证（真 <blockquote> 不会产生 > 前缀）；限短块防误伤 markdown 教学长段
_QUOTE_PROMO_MAX = 80


def _junk_attrs(attrs: list[tuple[str, str | None]]) -> bool:
    for k, v in attrs:
        if not v:
            continue
        if k == "class" and _JUNK_CLASS_RE.search(v):
            return True
        if k == "id" and _JUNK_ID_RE.search(v):
            return True
    return False


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


def _spam_texts(blocks: list[str], is_pre: list[bool]) -> set[str]:
    """邻域窗口内聚发短块（≥3 次）判为家具残影：作者卡/标签墙都是扎堆重复，
    章节芯片标签是分散重复——只有前者该删（claude.dev PROMPT×6 实测）。"""
    idx_by_text: dict[str, list[int]] = {}
    for i, (b, pre) in enumerate(zip(blocks, is_pre)):
        if pre or len(b) > _REPEAT_MAX_CHARS:
            continue
        idx_by_text.setdefault(b, []).append(i)
    spam: set[str] = set()
    for text, idxs in idx_by_text.items():
        if len(idxs) < _REPEAT_MIN_COUNT:
            continue
        for j in range(len(idxs) - _REPEAT_MIN_COUNT + 1):
            if idxs[j + _REPEAT_MIN_COUNT - 1] - idxs[j] <= _REPEAT_WINDOW:
                spam.add(text)
                break
    return spam


def _clean_blocks(blocks: list[str], is_pre: list[bool]) -> list[str]:
    """正文级质量网：剔除聚集重复块与推广/交互模式块，命中必留 WARNING（监督）。"""
    spam = _spam_texts(blocks, is_pre)
    out: list[str] = []
    n_rep = n_widget = 0
    for b, pre in zip(blocks, is_pre):
        if not pre:
            if b in spam:
                n_rep += 1
                continue
            if ((len(b) <= _WIDGET_MAX_CHARS and _WIDGET_RE.search(b))
                    or b.startswith(_BOILERPLATE_PREFIXES)
                    or (b.startswith("> ") and len(b) <= _QUOTE_PROMO_MAX)):
                n_widget += 1
                continue
        out.append(b)
    if n_rep or n_widget:
        log.warning("正文质量网：剔除聚集重复块 %d 条、推广/交互模式 %d 条", n_rep, n_widget)
    return out


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
    is_pre: list[bool] = []
    for i, seg in enumerate(text.split("\x00")):
        if i % 2:                     # 奇数段 = pre 内部
            pre = seg.strip("\n").rstrip()
            if pre.strip():
                blocks.append(pre)
                is_pre.append(True)
        else:
            paras = (_WS_RE.sub(" ", ln).strip() for ln in seg.split("\n\n"))
            for ln in paras:
                if ln:
                    blocks.append(ln)
                    is_pre.append(False)
    text = "\n\n".join(_clean_blocks(blocks, is_pre))[:EXTRACT_CAP]
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
