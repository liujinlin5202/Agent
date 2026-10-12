# -*- coding: utf-8 -*-
"""HTMLParser 正文提取：先确定容器，再保留可引用的结构块。

text / author 兼容旧调用方；blocks、issues 和抓取元数据供研究与质量闸门使用。
短文只标疑点；摘要、残缺正文及无法核对的 PDF 明确要求全文，不补写证据。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from hashlib import sha256
from html import escape
from html.parser import HTMLParser

log = logging.getLogger("tech-digest.extract")

EXTRACT_CAP = 60_000

_SKIP = {"head", "script", "style", "nav", "footer", "header", "aside", "noscript",
         "form", "button", "svg", "iframe", "select", "template"}
# id 只认原始四词：新词在 id 上会误伤标题锚点（claude.dev 实测
# id="share-the-chart-or-screenshot-itself" 的 H3 曾被整行删掉）
_JUNK_ID_RE = re.compile(r"comment|sidebar|newsletter|subscribe", re.I)
# class 命中即整块跳过的噪声容器（评论区资料卡曾污染全文，见 test_extract；
# sspai 页头/页尾与 freecodecamp banner 广告皆为类名命中——2026-10-07 实测）
_JUNK_CLASS_RE = re.compile(
    r"comment|sidebar|newsletter|subscribe|share|footer|popover|promo|banner|"
    r"copyright|related|recommend|toolbar|breadcrumb|author-card|"
    r"article__important|article__charge|article__header|advertisement|"
    r"(?:^|[\s_-])ads?(?:$|[\s_-])", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "br",
          "tr", "blockquote", "section", "table", "ul", "ol", "figure", "figcaption"}
_BODY_CLASSES = {"post-content", "gh-content", "article__main__content", "crayons-article__body"}

_WS_RE = re.compile(r"\s+")

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


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[str | _Node] = field(default_factory=list)
    skipped: bool = False
    hard_skipped: bool = False
    closed: bool = False


class _BodyParser(HTMLParser):
    """A small tree with explicit container boundaries and HTML void handling."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, closed=True)
        self.stack = [self.root]
        self.nodes: list[_Node] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        # Recover omitted paragraph/list/cell end tags without leaking skipped
        # containers. Nested lists have their own scope and must keep the parent li.
        if tag in _BLOCK - {"br"} or tag in {"article", "main"}:
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag == "p":
                    del self.stack[i:]
                    break
                if self.stack[i].tag in {"article", "section", "div", "li", "td", "th", "pre"}:
                    break
        if tag in {"li", "td", "th", "tr"}:
            stop = {
                "li": {"ul", "ol"}, "td": {"tr"}, "th": {"tr"},
                "tr": {"table", "tbody", "thead"},
            }[tag]
            close = {"td", "th"} if tag in {"td", "th"} else {tag}
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag in stop:
                    break
                if self.stack[i].tag in close:
                    del self.stack[i:]
                    break
        values = {k: v or "" for k, v in attrs}
        parent = self.stack[-1]
        hidden = "hidden" in values or values.get("aria-hidden", "").lower() == "true"
        tex = tag == "script" and values.get("type", "").lower().startswith("math/tex")
        hard_skipped = parent.hard_skipped or (tag in _SKIP and not tex) or hidden
        verified_body = (tag in {"section", "div"} and bool(set(values.get("class", "").split()) & _BODY_CLASSES)) \
            or (tag == "article" and "ltx_document" in values.get("class", "").split())
        node = _Node(
            tag, values, parent,
            # Real freeCodeCamp body lives in ad-layout/post-and-sidebar wrappers.
            # A verified body can override broad layout-name heuristics, but never
            # explicit hidden, scripts, forms or other excluded semantic containers.
            skipped=hard_skipped or _junk_attrs(attrs) or (parent.skipped and not verified_body),
            hard_skipped=hard_skipped,
            closed=tag in _VOID,
        )
        parent.children.append(node)
        self.nodes.append(node)
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            if key and values.get("content"):
                self.meta.setdefault(key, values["content"].strip())
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack[i].closed = True
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk(node: _Node):
    if node.skipped:
        return
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _raw_text(node: _Node) -> str:
    return "".join(_raw_text(c) if isinstance(c, _Node) else c for c in node.children)


def _plain(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _is_formula(node: _Node) -> bool:
    return node.tag == "math" or (node.tag == "script" and node.attrs.get("type", "").lower().startswith("math/tex"))


def _markup(node: _Node) -> str:
    attributes = "".join(f' {k}="{escape(v, quote=True)}"' for k, v in node.attrs.items())
    content = "".join(_markup(c) if isinstance(c, _Node) else escape(c, quote=False) for c in node.children)
    return f"<{node.tag}{attributes}>{content}</{node.tag}>"


def _formula_metadata(node: _Node) -> dict:
    return {"mathml": _markup(node)} if node.tag == "math" else {"format": "tex"}


def _formula(node: _Node) -> str:
    if node.tag == "script":
        return _raw_text(node).strip()
    if node.attrs.get("alttext"):
        return node.attrs["alttext"].strip()
    for child in _walk(node):
        if child.tag == "annotation" and "tex" in child.attrs.get("encoding", "").lower():
            return _raw_text(child).strip()
    def render(current):
        if current.tag in {"mi", "mn", "mo", "mtext"}:
            return _plain(_raw_text(current))
        parts = [render(c) if isinstance(c, _Node) else _plain(c) for c in current.children if not isinstance(c, _Node) or c.tag not in {"annotation", "annotation-xml"}]
        parts = [p for p in parts if p]
        if len(parts) == 2 and current.tag in {"mfrac", "msup", "msub"}:
            left, right = parts
            return f"({left})/({right})" if current.tag == "mfrac" else f"{left}{'^' if current.tag == 'msup' else '_'}({right})"
        if current.tag == "msqrt":
            return "sqrt(" + " ".join(parts) + ")"
        return " ".join(parts)

    return render(node) or _plain(_raw_text(node))


def _inline(node: _Node, links: list[dict] | None = None, *, raw: bool = False) -> str:
    if node.skipped:
        return ""
    if node.tag == "br":
        return "\n"
    if _is_formula(node):
        return _formula(node)
    text = "".join(_inline(c, links, raw=raw) if isinstance(c, _Node) else c for c in node.children)
    if node.tag == "a" and not raw:
        url = node.attrs.get("href", "").strip()
        if url and not re.match(r"(?:javascript|data|vbscript):", url, re.I):
            label = _plain(text) or url
            if links is not None:
                links.append({"text": label, "url": url})
            return f"[{label}]({url})"
    return text


def _source_blocks(container: _Node) -> list[dict]:
    out: list[dict] = []

    def add(kind, text, **extra):
        value = text if kind == "code" else text.strip()
        if value.strip():
            out.append({"kind": kind, "text": value, **extra})

    def atomic(node, kind, **extra):
        links: list[dict] = []
        pending: list[str] = []

        def flush():
            text = _plain("".join(pending))
            if text:
                add(kind, text, **extra, **({"links": list(links)} if links else {}))
            pending.clear()
            links.clear()

        def visit_inline(child):
            if isinstance(child, str):
                pending.append(child)
            elif not child.skipped:
                if child.tag in {"ul", "ol"}:
                    return  # Nested list items are emitted once by emit().
                if _is_formula(child):
                    flush()
                    add("formula", _formula(child), **_formula_metadata(child))
                elif child.tag == "a":
                    pending.append(_inline(child, links))
                elif child.tag == "br":
                    pending.append(" ")
                else:
                    for grandchild in child.children:
                        visit_inline(grandchild)
                    if child.tag in _BLOCK:
                        pending.append(" ")

        for child in node.children:
            visit_inline(child)
        flush()

    def emit(node, list_depth=0):
        if node.skipped:
            return
        if node.tag == "pre":
            add("code", _inline(node, raw=True))
            return
        if _is_formula(node):
            add("formula", _formula(node), **_formula_metadata(node))
            return
        if node.tag == "table":
            for c in _walk(node):
                if c.tag == "caption":
                    atomic(c, "caption")
            rows: list[list[str]] = []
            links: list[dict] = []
            for row in _walk(node):
                if row.tag == "tr":
                    cells = [_plain(_inline(c, links)) for c in row.children if isinstance(c, _Node) and c.tag in {"td", "th"} and not c.skipped]
                    if cells:
                        rows.append(cells)
            if rows:
                formulas = [
                    {"text": _formula(c), **_formula_metadata(c)}
                    for c in _walk(node) if _is_formula(c)
                ]
                add(
                    "table", "\n".join("\t".join(r) for r in rows), rows=rows,
                    **({"links": links} if links else {}),
                    **({"formulas": formulas} if formulas else {}),
                )
            return
        if node.tag in {"ul", "ol"}:
            ordinal = int(node.attrs.get("start", "1")) if node.attrs.get("start", "1").lstrip("-").isdigit() else 1
            for child in node.children:
                if isinstance(child, _Node) and child.tag == "li" and not child.skipped:
                    if child.attrs.get("value", "").lstrip("-").isdigit():
                        ordinal = int(child.attrs["value"])
                    atomic(child, "list_item", ordered=node.tag == "ol", ordinal=ordinal if node.tag == "ol" else None, depth=list_depth)
                    for nested in _walk(child):
                        if nested is not child and nested.tag in {"ul", "ol"}:
                            ancestor = nested.parent
                            while ancestor is not None and ancestor is not child and ancestor.tag not in {"ul", "ol"}:
                                ancestor = ancestor.parent
                            if ancestor is child:
                                emit(nested, list_depth + 1)
                    ordinal += 1
            return
        if re.fullmatch(r"h[1-6]", node.tag):
            atomic(node, "heading", level=int(node.tag[1]))
            return
        if node.tag in {"p", "li", "figcaption", "caption"}:
            atomic(node, "caption" if node.tag in {"figcaption", "caption"} else "list_item" if node.tag == "li" else "paragraph")
            return
        if node.tag == "img":
            return  # Captions are text; image pixels are not verified evidence.
        pending: list[str] = []

        def flush():
            add("paragraph", _plain("".join(pending)))
            pending.clear()

        for child in node.children:
            if isinstance(child, str):
                pending.append(child)
            elif not child.skipped:
                if child.tag in _BLOCK or child.tag in {"article", "main"} or _is_formula(child):
                    flush()
                    emit(child, list_depth)
                else:
                    pending.append(_inline(child))
        flush()

    emit(container)
    return out


def _select_body(parser: _BodyParser) -> tuple[_Node, str]:
    def score(node):
        blocks = _source_blocks(node)
        prose = [b for b in blocks if b["kind"] in {"paragraph", "list_item"}]
        substantive = sum(min(len(b["text"]), 500) for b in prose if len(b["text"]) >= 20)
        structure = sum(b["kind"] in {"heading", "code", "table", "formula"} for b in blocks)
        return (bool(substantive or structure), substantive + 80 * structure, len(prose))

    visible = [n for n in parser.nodes if not n.skipped]
    specific = [n for n in visible if (n.tag in {"section", "div"} and set(n.attrs.get("class", "").split()) & _BODY_CLASSES) or (n.tag == "article" and "ltx_document" in n.attrs.get("class", "").split())]
    for candidates, method in ((specific, "site_selector"), ([n for n in visible if n.tag == "article"], "generic_article"), ([n for n in visible if n.tag == "main"], "generic_main")):
        if candidates:
            best = max(candidates, key=score)
            if score(best)[0]:
                return best, method
    return next((n for n in visible if n.tag == "body"), parser.root), "body_fallback"


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


def _clean_blocks(blocks: list[dict]) -> list[dict]:
    """正文级质量网：剔除聚集重复块与推广/交互模式块，命中必留 WARNING（监督）。"""
    texts = [b["text"] for b in blocks]
    protected = [b["kind"] not in {"paragraph", "heading"} for b in blocks]
    spam = _spam_texts(texts, protected)
    out: list[dict] = []
    n_rep = n_widget = 0
    for block, b, pre in zip(blocks, texts, protected):
        if not pre:
            if b in spam:
                n_rep += 1
                continue
            if ((len(b) <= _WIDGET_MAX_CHARS and _WIDGET_RE.search(b))
                    or b.startswith(_BOILERPLATE_PREFIXES)
                    or (b.startswith("> ") and len(b) <= _QUOTE_PROMO_MAX)):
                n_widget += 1
                continue
        out.append(block)
    if n_rep or n_widget:
        log.warning("正文质量网：剔除聚集重复块 %d 条、推广/交互模式 %d 条", n_rep, n_widget)
    return out


_PUBLICATION_STATUSES = {"peer_reviewed", "preprint", "technical_report", "engineering_blog", "unknown"}
_LOGIN_RE = re.compile(r"请登录后阅读|登录.{0,6}(?:阅读|查看全文)|(?:log\s*in|sign\s*in).{0,20}(?:read|continue)|subscribe to (?:read|continue)", re.I)
_METHOD_RE = re.compile(r"\b(?:methods?|methodology|approach|algorithm|framework|architecture|model|proof|derivation|theory)\b|方法|算法|推导|证明", re.I)
_SUPPORT_RE = re.compile(r"\b(?:results?|experiments?|evaluation|measurements?|findings|proof|derivation|theorem|analysis)\b|结果|实验|测量|评估|推导|证明", re.I)
_ABSTRACT_RE = re.compile(r"^(?:\d+[.\s]*)?(?:abstract|摘要)\b", re.I)
_TABLE_REF_RE = re.compile(r"\btable\s+\d+|表\s*\d+", re.I)
_FIGURE_REF_RE = re.compile(r"\b(?:figure|fig\.)\s*\d+|图\s*\d+", re.I)


def _date_value(raw: str | None) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", value):
        value = value.replace("/", "-")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _publication_metadata(meta: dict[str, str]) -> dict:
    published_at = next((parsed for key in ("article:published_time", "citation_publication_date", "datepublished", "pubdate") if (parsed := _date_value(meta.get(key)))), None)
    status = meta.get("publication_status") or meta.get("citation_publication_status")
    if status not in _PUBLICATION_STATUSES:
        status = "preprint" if meta.get("citation_arxiv_id") else "unknown"
    return {"published_at": published_at, "publication_status": status, "original_paper_url": None}


def _has_section(blocks: list[dict], pattern: re.Pattern) -> bool:
    """A heading alone is not a captured method or result."""
    for i, block in enumerate(blocks):
        if block["kind"] != "heading" or block.get("level") == 1 or not pattern.search(block["text"]):
            continue
        for following in blocks[i + 1:]:
            if following["kind"] == "heading" and following.get("level", 6) <= block.get("level", 6):
                break
            if following["kind"] in {"paragraph", "list_item", "table", "code", "formula"} and following["text"].strip():
                return True
    return False


def _paper_issues(parser: _BodyParser, container: _Node, blocks: list[dict]) -> list[str]:
    visible = list(_walk(container))
    pdf_link = bool(parser.meta.get("citation_pdf_url")) or any(
        n.tag == "a" and re.search(r"\.pdf(?:[?#]|$)", n.attrs.get("href", ""), re.I)
        for n in visible
    )
    non_link_body = any(
        b["kind"] in {"code", "table", "formula"}
        or (b["kind"] in {"paragraph", "list_item"}
            and len(re.sub(r"\[[^\]]*\]\([^)]*\)", "", b["text"]).strip()) >= 25)
        for b in blocks
    )
    if pdf_link and not non_link_body:
        return ["pdf_fulltext_required", "fulltext_required"]
    is_paper = any("ltx_document" in n.attrs.get("class", "").split() for n in visible) or any(k.startswith("citation_") for k in parser.meta)
    abstract = any(set(n.attrs.get("class", "").split()) & {"abstract", "ltx_abstract"} for n in visible) or any(b["kind"] == "heading" and _ABSTRACT_RE.search(b["text"]) for b in blocks)
    if not is_paper and not abstract:
        return []
    issues: list[str] = []
    has_method = _has_section(blocks, _METHOD_RE)
    has_support = _has_section(blocks, _SUPPORT_RE)
    if abstract and not has_method and not has_support:
        issues.append("abstract_only")
    if not has_method:
        issues.append("missing_method")
    if not has_support:
        issues.append("missing_support")
    text = "\n\n".join(b["text"] for b in blocks)
    if _TABLE_REF_RE.search(text) and not any(b["kind"] == "table" for b in blocks):
        if "missing_support" not in issues:
            issues.append("missing_support")
    if _FIGURE_REF_RE.search(text):
        # HTML pixels/SVG paths are not verified chart values. Captions survive,
        # but a cited chart needs checked text/table evidence before drafting.
        figures = [n for n in visible if n.tag == "figure"]
        def has_graphics(node):
            return node.tag in {"img", "svg"} or any(has_graphics(c) for c in node.children if isinstance(c, _Node))

        def has_textual_evidence(node):
            return any(b["kind"] in {"table", "formula", "code"} for b in _source_blocks(node))

        if not figures or any(has_graphics(n) or not has_textual_evidence(n) for n in figures):
            issues.append("critical_figure_missing")
    if issues:
        issues.append("fulltext_required")
    return issues


def _cap_blocks(blocks: list[dict]) -> tuple[list[dict], bool]:
    capped: list[dict] = []
    remaining = EXTRACT_CAP
    for block in blocks:
        separator = 2 if capped else 0
        space = remaining - separator
        if space <= 0:
            return capped, True
        if len(block["text"]) > space:
            # A partial table/link must not retain uncaptured rows or targets.
            capped.append({"kind": block["kind"], "text": block["text"][:space]})
            return capped, True
        capped.append(block)
        remaining -= separator + len(block["text"])
    return capped, False


def extract_article(html_text: str) -> dict:
    """HTML → text/author + stable blocks, capture issues and method.

    Block IDs are deterministic within the SHA256 of exact returned text. Code
    whitespace is verbatim after HTML entity decoding; cells retain row boundaries.
    """
    if (html_text or "").lstrip().startswith("%PDF-"):
        return {"text": "", "author": "", "blocks": [], "issues": ["pdf_fulltext_required", "fulltext_required"], "method": "pdf_unsupported", **_publication_metadata({})}
    parser = _BodyParser()
    parser.feed(html_text or "")
    parser.close()
    container, method = _select_body(parser)
    blocks = _clean_blocks(_source_blocks(container))
    issues = _paper_issues(parser, container, blocks)
    raw = _raw_text(container)
    substantive = any(b["kind"] in {"code", "table", "formula"} or (b["kind"] == "paragraph" and len(b["text"]) >= 80 and not _LOGIN_RE.search(b["text"])) for b in blocks)
    gate_form = any(n.tag == "form" and _LOGIN_RE.search(_raw_text(n)) for n in parser.nodes)
    if _LOGIN_RE.search(raw) and (gate_form or not substantive):
        blocks = []
        issues.append("login_required")
    if not blocks:
        issues.append("empty_content")
    elif sum(len(b["text"]) for b in blocks) < 500:
        issues.append("short_text")
    blocks, capped = _cap_blocks(blocks)
    current = container
    unclosed = False
    while current is not None:
        if current.tag in {"article", "section", "div", "main"} and not current.closed:
            unclosed = True
        current = current.parent
    if capped or unclosed:
        issues.extend(("truncated", "fulltext_required"))
    blocks = [{"id": f"b{i}", **b} for i, b in enumerate(blocks, 1)]
    return {"text": "\n\n".join(b["text"] for b in blocks), "author": parser.meta.get("author", parser.meta.get("citation_author", "")), "blocks": blocks, "issues": list(dict.fromkeys(issues)), "method": method, **_publication_metadata(parser.meta)}


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
    """Capture HTML with provenance; preserve short or invalid capture diagnostics.

    Empty/login/PDF captures carry issues for the caller's rejection or mirror path.
    Only network failures raise; publication dates never come from the crawl time.
    """
    from app.fetcher import http_get

    resp = http_get(url, timeout=timeout)
    headers = getattr(resp, "headers", {}) or {}
    content_type = next((str(v).split(";", 1)[0].strip().lower() for k, v in headers.items() if k.lower() == "content-type"), "")
    content = getattr(resp, "content", b"")
    if content_type == "application/pdf" or (isinstance(content, bytes) and content.lstrip().startswith(b"%PDF-")):
        r = {"text": "", "author": "", "blocks": [], "issues": ["pdf_fulltext_required", "fulltext_required"], "method": "pdf_unsupported"}
    elif content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        r = {"text": "", "author": "", "blocks": [], "issues": ["unsupported_content_type", "fulltext_required"], "method": "unsupported_content_type"}
    else:
        r = extract_article(resp.text)
    return _with_source_metadata(r, url)


def _with_source_metadata(article: dict, url: str, *, mirror_capture: bool = False) -> dict:
    result = dict(article)
    text = result.get("text") or ""
    result.setdefault("author", "")
    result["published_at"] = _date_value(result.get("published_at"))
    if result.get("publication_status") not in _PUBLICATION_STATUSES:
        result["publication_status"] = "unknown"
    result.setdefault("original_paper_url", None)
    issues = list(result.get("issues") or [])
    if mirror_capture:
        result["method"] = "mirror"
        if not result.get("blocks"):
            # Legacy mirror API exports only plain text. Do not guess code/table
            # fidelity from that text, or claim unavailable page metadata.
            result["blocks"] = [{"id": f"b{i}", "kind": "paragraph", "text": t} for i, t in enumerate(text.split("\n\n"), 1) if t.strip()]
            issues.append("structure_unverified")
        if len(text) > EXTRACT_CAP:
            blocks, _ = _cap_blocks(result["blocks"])
            result["blocks"] = [{**b, "id": f"b{i}"} for i, b in enumerate(blocks, 1)]
            text = result["text"] = "\n\n".join(b["text"] for b in result["blocks"])
            issues.extend(("truncated", "fulltext_required"))
        if not text.strip():
            issues.append("empty_content")
        elif len(text) < 500:
            issues.append("short_text")
    result.update(
        url=url, source_hash=sha256(text.encode("utf-8")).hexdigest(),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        issues=list(dict.fromkeys(issues)),
    )
    return result


def fetch_with_mirror(item: dict) -> dict | None:
    """原文抓取公共阶梯（2026-10-08 M2 自 main._try_fetch_art 提升为单一实现）：

    extract → 镜像（wallstreetcn 等 SPA 空壳走内容 API）。全文只抓一次，
    调用方（闸门/深读/正文）共用缓存。抓取失败只记日志；未获响应返回 None，
    无效响应保留 issues 交调用方拒稿，绝不向上抛。
    """
    from app import mirror

    url = item.get("url") or ""
    try:
        art = fetch_article(url)
    except Exception as e:  # noqa: BLE001 单篇抓取失败 → 镜像通道
        log.warning("原文抓取失败（%s）→ 尝试镜像通道: %s", item.get("title"), e)
        art = None
    unusable = art is None or not (art.get("text") or "").strip() or bool(set(art.get("issues") or []) & {"empty_content", "login_required", "fulltext_required"})
    if unusable and mirror.needs_mirror(url):
        try:
            mirrored = mirror.mirror_fetch(url)
            if mirrored is not None and (mirrored.get("text") or "").strip():
                art = _with_source_metadata(mirrored, url, mirror_capture=True)
        except Exception as e:  # noqa: BLE001 镜像失败 → 交还调用方降级
            log.warning("镜像通道失败（%s）: %s", item.get("title"), e)
    return art
