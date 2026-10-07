# -*- coding: utf-8 -*-
"""Tech Digest 日报渲染 v4：每天一篇爆文（全文转载 or AI 深度导读）+ 周六星榜专帖。

2026-09-17 用户决策：日报从「3-4 条摘要 + 折叠星榜」改为「每天只挑一篇兼具
时效性与影响性的爆点文章」——能抓到全文的直接全文搬运（英文 AI 翻译成中文），
前面写「转载自 xxx」；抓不到全文的（知乎/公众号系反爬）降级为 AI 深度导读+链接。
GitHub 星榜移出日报，每周六单独发一帖（全量 15 条、不折叠）。

v3.1 的折叠星榜/速览/多条目结构随本次改版退役（周六星榜帖接棒星榜）。

输入 ctx（由 main.run_daily 组装）：
    {issue_no, pick: {item(候选条目), why(编者按), lead(正文块),
                      lead_kind: full|digest|summary|none, author}}
"""
from __future__ import annotations

import html as _html
import re
from datetime import date

from app import titles
from app.emoji import strip_emoji

STAR_TOTAL = 15            # 星榜条数
FULL_CAP = 8_000           # 转载正文中文字符预算（超长截断并注明）
COLLAPSE_AT = 3_500        # 全文超过此长度 → 预览 + <details> 收起剩余（v4.1）
PREVIEW_CHARS = 1_200      # 收起时正文预览长度
HEADLINE_MAX = 32
ENGLISH_HEADLINE_MAX = 24  # 纯英文标题 ≤24 字可直接作发帖标题兜底

SOURCE_LABEL = {"hacker-news": "Hacker News", "sspai": "少数派", "zhihu-hot": "知乎热榜",
                "devto": "dev.to", "ars-technica": "Ars Technica",
                "ieee-spectrum": "IEEE Spectrum", "freecodecamp": "freeCodeCamp",
                "ruanyifeng": "阮一峰的博客", "wallstreetcn": "华尔街见闻"}

SIGNATURE = "由爬虫和 AI 自动整理 · 第 {} 期 · 转载仅供学习交流，版权归原作者所有"
STAR_SIGNATURE = "由爬虫和 AI 自动整理 · 仅作学习交流，项目归原作者所有"
# lead_kind → 标记：明示「不是全文」，防读者误以为全文搬运
KIND_MARKER = {
    "digest": "以上是 AI 导读、不是全文；完整内容请看原文。",
    "summary": "以上是原文摘要，不是全文；完整内容请看原文。",
}
TRUNC_NOTE = "限于篇幅，后文有删节，完整版见原文。"
NOTHING_LINE = "（今天没能把全文搬过来，直接点下方原文阅读。）"

# v4.4 布局：全部作用域 td-* 类 + 单个 <style> 块（集市若剥样式 → 降级为普通
# 文本/原生 details，功能不丢）。色板取自 github-markdown-light，不引入新强调色：
#   td-note  编者按导读面板（浅灰底圆角，和正文区分开）
#   td-gloss 名词卡容器（卡片观感；summary 保留原生三角做展开指向）
#   td-fold:not(.td-gloss) 文章折叠 summary 做成胶囊按钮（无三角，tap 有回缩反馈）
#   td-hint  元信息（导读标记/删节说明）降灰调小，不与正文抢注意力
#   td-cta   「阅读原文」描边胶囊链接，移动端可点面积更大
FOLD_STYLE = (
    "<style>"
    ".td-fold summary{cursor:pointer}"
    ".td-fold .td-more{display:inline}.td-fold .td-less{display:none}"
    ".td-fold[open] .td-more{display:none}.td-fold[open] .td-less{display:inline}"
    ".td-note{background:#f6f8fa;border:1px solid #d8dee4;border-radius:8px;"
    "padding:10px 14px;margin:14px 0 18px}"
    ".td-gloss{background:#fafbfc;border:1px solid #d8dee4;border-radius:8px;"
    "padding:9px 14px;margin:12px 0}"
    ".td-gloss summary{font-weight:600}"
    ".td-fold:not(.td-gloss)>summary{display:inline-block;list-style:none;"
    "background:#f6f8fa;border:1px solid #d8dee4;border-radius:999px;"
    "padding:7px 16px;margin:6px 0;font-size:14px;user-select:none}"
    ".td-fold:not(.td-gloss)>summary:active{transform:scale(.97)}"
    ".td-hint{font-size:13px;color:#57606a;margin:10px 0 14px}"
    ".td-cta{display:inline-block;border:1px solid #d8dee4;border-radius:999px;"
    "padding:9px 18px;margin:2px 0 6px;color:#0969da;font-weight:500;"
    "text-decoration:none}"
    "</style>"
)


def _bold_html(text: str) -> str:
    """裸 HTML 容器内 markdown 不再解析：AI 文本的 **加粗** 转 <strong>，
    其余 HTML 字符转义，防面板被意外标签打穿。"""
    t = _html.escape(text or "", quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)


def fold_summary(word_count: int | None = None) -> str:
    """长文折叠块 summary：收起态「查看更多（全文约 N 字）」、展开态「收起」。"""
    more = "查看更多" + (f"（全文约 {word_count} 字）" if word_count else "")
    return (f'<details class="td-fold"><summary>'
            f'<span class="td-more">{more}</span>'
            f'<span class="td-less">收起</span></summary>')

_CJK_RE = re.compile(r"[一-鿿]")


def _clean_headline(text: str) -> str:
    """剥「派早报：」「快讯|」类前后缀装饰与尾部括号/等字，留核心。"""
    t = (text or "").strip()
    t = re.sub(r"^[^：:/|]{1,14}[：:/|]", "", t)
    t = re.sub(r"^[（(【\[][^）)】\]]{1,10}[）)】\]]", "", t)
    t = re.sub(r"[（(][^）)]{1,20}[）)]$", "", t)
    t = re.sub(r"[…\.]{2,}$", "", t)
    t = t.rstrip("，,、；;：:。. ")
    t = re.sub(r"等$", "", t).rstrip("，, ")
    return t.strip()


def headline_of(focus_title: str | None, focus_title_zh: str = "", theme: str = "") -> str:
    """兜底发帖标题：优先中文（译名/原题），短英文标题可用。"""
    for cand in (focus_title_zh or "", focus_title or "", theme or ""):
        h = _clean_headline(cand)
        if not h:
            continue
        if _CJK_RE.search(h):
            if len(h) > HEADLINE_MAX:
                return h[:HEADLINE_MAX].rstrip("，,。 ") + "…"
            return h
        if len(h) <= ENGLISH_HEADLINE_MAX:
            return h
    return ""


def fallback_title(pick_title: str) -> str:
    """v4 兜底发帖标题：选中文章的原题清洗（英文长题回退 headline_of 逻辑）。"""
    return headline_of(pick_title)


def _signal_of(item: dict) -> str:
    """热度信号一句话（影响力背书，放转载署名行）。"""
    src = item.get("source") or ""
    t = item.get("trending") or {}
    if src == "zhihu-hot":
        return f"热榜第 {t.get('rank', '?')} 名（{t.get('heat', 0)} 万热度）"
    if src == "hacker-news":
        pts = item.get("points") or 0
        return f"{pts} points" if pts else ""
    if src == "devto":
        pts = item.get("points") or 0
        return f"{pts} 人赞" if pts else ""
    return ""


def _para_cut(text: str, limit: int) -> str:
    """截到 limit 内，优先落在段落边界（\\n\\n），不切半段；也不许切进代码
    围栏——不配对的 ``` 会把折叠块和后文整个吞成代码块。"""
    t = text or ""
    if len(t) <= limit:
        return t
    cut = t[:limit]
    brk = cut.rfind("\n\n")
    if brk > limit // 2:
        cut = cut[:brk]
    if cut.count("```") % 2:
        cut = cut[:cut.rfind("```")].rstrip()
    return cut


def _glossary_cards(glossary: list[dict] | None) -> list[str]:
    """知识卡片：details 折叠卡。集市正文不能跑 JS，点击展开用原生 <summary>。"""
    cards: list[str] = []
    for g in (glossary or [])[:3]:
        term = (g.get("term") or "").strip()
        expl = (g.get("expl") or "").strip()
        if term and expl:
            cards += [f'<details class="td-fold td-gloss"><summary>什么是 {term}？</summary>',
                      "", _bold_html(expl), "", "</details>", ""]
    return cards


def render_repost(day: str, issue_no: int, ctx: dict) -> str:
    """v4 日报渲染：单篇转载。main 组装好 pick dict，这里只管排版。

    pick 契约：{item, why(编者按|""), lead(正文块|""), lead_kind: full|digest|summary|none,
    author, glossary([{term, expl}] 知识卡片|[])}。lead_kind 决定「不是全文」提示的措辞；lead 与 why 全空时给一句诚实的
    引导——宁可直说没搬到，也不空发或假装有导读。
    """
    pick = ctx.get("pick") or {}
    item = pick.get("item") or {}
    why = (pick.get("why") or "").strip()
    lead = (pick.get("lead") or "").strip()
    kind = pick.get("lead_kind") or "none"
    author = (pick.get("author") or item.get("author") or "").strip()
    src = SOURCE_LABEL.get(item.get("source") or "", item.get("source") or "")
    signal = _signal_of(item)

    glossary_cards = _glossary_cards(pick.get("glossary"))
    has_fold = bool(glossary_cards) or (kind == "full" and len(lead) > COLLAPSE_AT)
    parts: list[str] = []
    attr = f"> 转载自 {src or item.get('source') or '网络'}"
    if author:
        attr += f" · {author}"
    if signal:
        attr += f" · {signal}"
    parts += [attr, ""]
    if why:
        parts += [f'<div class="td-note"><strong>编者按</strong>：{_bold_html(why)}', "</div>", ""]
    if glossary_cards:
        parts += glossary_cards
    if lead:
        if kind == "full" and len(lead) > COLLAPSE_AT:
            # v4.1 长文收起：预览 + <details>（全文预算 FULL_CAP 不变，超出仍截断注明）
            preview = _para_cut(lead, PREVIEW_CHARS)
            rest_src = lead[len(preview):].lstrip()
            budget = max(FULL_CAP - len(preview), 500)
            truncated = len(rest_src) > budget
            parts += [preview, "", fold_summary(len(lead)),
                      "", _para_cut(rest_src, budget) if truncated else rest_src,
                      "", "</details>", ""]
            if truncated:
                parts += [f'<div class="td-hint">{TRUNC_NOTE}</div>', ""]
        elif len(lead) > FULL_CAP:
            parts += [_para_cut(lead, FULL_CAP), "", f'<div class="td-hint">{TRUNC_NOTE}</div>', ""]
        else:
            parts += [lead, ""]
        marker = KIND_MARKER.get(kind)
        if marker:
            parts += [f'<div class="td-hint">{marker}</div>', ""]
    elif not why:
        parts += [f'<div class="td-hint">{NOTHING_LINE}</div>', ""]
    if has_fold:
        parts = [FOLD_STYLE, ""] + parts   # 样式块放最前，正文区不留空洞
    if item.get("url"):
        href = _html.escape(item["url"], quote=True)
        parts += [f'<a class="td-cta" href="{href}">阅读原文</a>', ""]
    parts += ["---", "", SIGNATURE.format(issue_no), ""]
    return strip_emoji("\n".join(parts))


def _star_entry_md(it: dict, desc_map: dict[str, str]) -> str:
    """榜单条目 → 两行 markdown：名字行（名次+链接+今日⭐+语言）+ 一句话简介。

    ⭐ 是全帖唯一保留的 emoji（GitHub 星数标识，2026-09-21 emoji 禁令豁免）。
    """
    t = it.get("trending") or {}
    rank = t.get("rank") or 0
    lang = f" · {t['language']}" if t.get("language") else ""
    title = (it.get("title") or "").replace("[", "【").replace("]", "】")
    desc = (desc_map.get(it.get("title")) or it.get("summary") or
            "").replace("\n", " ").strip()[:60]
    head = f"{rank}. [{title}]({it['url']}) · **+{t.get('today_stars', 0):,}** ⭐{lang}"
    return f"{head}\n{desc}" if desc else head


_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def render_star_post(day: str, trending: list[dict], desc_map: dict[str, str]) -> str:
    """周六星榜专帖：全量 15 条、不折叠（v3.1 的 <details> 是为塞进日报设计的，
    独立帖没必要再折叠）。纯 markdown，无裸 HTML。"""
    items = [it for it in (trending or [])[:STAR_TOTAL] if it.get("url")]
    if not items:
        return ""
    try:
        wd = _WEEKDAYS[date.fromisoformat(day).weekday()]
    except ValueError:
        wd = ""
    title_tail = f"（{wd}）" if wd else ""
    lines = [f"## GitHub 开源星榜 · {day}{title_tail}", ""]
    for it in items:
        lines += [_star_entry_md(it, desc_map or {}), ""]
    lines += ["---", "", "星榜每周六发一次 · 数据来自 GitHub Trending 每日榜", "",
              STAR_SIGNATURE, ""]
    return strip_emoji("\n".join(lines))
