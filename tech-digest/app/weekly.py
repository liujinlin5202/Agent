# -*- coding: utf-8 -*-
"""五段式周报（v4.5；v2.0 六段裁撤三节后的形态）。

    ① 本周回顾   AI 调用 1（附带 ③④）
    ② 深度长文   AI 调用 2（1-2 篇 ×500-1000 字；失败→该段降级注释）
    ③ 精选回看   AI 调用 1 附带（3 条 + 一句话；规则：days+关注度 top 且未进②）
    ④ 下周前瞻   AI 调用 1 附带（标注"预计"）
    ⑤ GitHub 星榜 程序周聚合 Top10（AI 调用 3 写中文简介，失败回退英文 desc）

2026-09-21 信息密度改版（用户决策）：裁撤「每日收录量 ASCII 图 / 语言分布 Top5 /
爬虫自检（HEAD 抽查）」——手机端表格渲染成黑块/截断，失效链接清单对读者无信息量；
星榜改两行列表（标题链接+简介在前，star 数据次行）并移到周报末尾。

输入约定（与 main.run_weekly 对应）：
    days = Store.get_daily_since(monday)       # [{date, issue_no, items}]
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from app import llm
from app.config import settings
from app.daily_ai import _unwrap, ai_trending_desc
from app.dedup import is_duplicate
from app.emoji import strip_emoji

SYSTEM_PROMPT = (
    "你是一名资深技术编辑，负责为一个小型技术社区撰写中文周报。"
    "你只基于给定的数据撰写，绝不编造不存在的仓库、星级、事件或事实。"
    "全文禁止使用 emoji 表情符号。"
)

CATEGORY_ORDER = ["AI 工具", "前端", "后端", "安全", "其他"]
DEEP_MIN_CHARS = 300     # 深度长文字数下限（不足视为失败→降级；规格 500-1000 字）
DEEP_CANDS = 8           # 深度长文候选数（v4.5: 5→8，AI 按信息价值择优写 1-2 篇）
REVIEW_MIN = 5           # ① 条数下限；不足则整段降级
PICKS_MAX = 3
STAR_TOP = 10            # ⑤ 周榜渲染条数
STAR_INTRO_CANDS = 12    # 送 AI 写中文简介的候选数


# ---------------- 聚合（v1.1 兼容） ----------------

def _star(it: dict, key: str):
    """兼容 v1 平铺字段与 v2 trending 子结构。"""
    t = it.get("trending")
    if isinstance(t, dict):
        return t.get(key)
    return it.get(key)


def aggregate(days: list[dict]) -> list[dict]:
    """trending 周聚合：按仓库全名去重统计（沿用 v1.1，技术文档 §7）。

    返回按（周累计今日星, 出现天数）降序的仓库列表。
    同时兼容 v1（full_name/desc/平铺字段）与 v2（title/summary/trending 子结构）条目。
    """
    stats: dict[str, dict] = {}
    for day in days:
        for it in day["items"]:
            if (it.get("type") or "trending") != "trending":
                continue
            name = it.get("full_name") or it.get("title") or ""
            if not name:
                continue
            s = stats.setdefault(name, {
                "full_name": name, "url": it.get("url", ""),
                "language": _star(it, "language"),
                "stars": _star(it, "stars") or 0,
                "desc": it.get("summary") or it.get("desc") or "",
                "days": 0, "total_today": 0, "peak_today": 0,
            })
            s["days"] += 1
            ts = _star(it, "today_stars") or 0
            s["total_today"] += ts
            s["peak_today"] = max(s["peak_today"], ts)
            stars = _star(it, "stars") or 0
            if stars > s["stars"]:
                s["stars"] = stars
            desc = it.get("summary") or it.get("desc") or ""
            if desc and desc != "(无描述)":
                s["desc"] = desc
    return sorted(stats.values(), key=lambda s: (-s["total_today"], -s["days"]))


def _agg_news(days: list[dict]) -> list[dict]:
    """news 跨天去重聚合（URL 规范化 + 标题相似 0.85，复用 dedup）。

    返回 [{item(首现原文, 含全文), days, first_date, all_summaries}]，
    按（出现天数, 首现日期）降序——周内最热且最早出现的排前。
    """
    merged: list[dict] = []
    for day in days:
        for it in day["items"]:
            if (it.get("type") or "trending") != "news":
                continue
            hit = next((m for m in merged if is_duplicate(it, m["item"])), None)
            if hit is None:
                merged.append({"item": dict(it), "days": 1,
                               "first_date": day["date"],
                               "all_summaries": [it.get("summary") or ""]})
            else:
                hit["days"] += 1
                s = it.get("summary") or ""
                if s and s not in hit["all_summaries"]:
                    hit["all_summaries"].append(s)
    merged.sort(key=lambda m: (-m["days"], m["first_date"]))
    return merged


def _top(merged: list[dict], n: int) -> list[dict]:
    return merged[:n]


def _md_date(iso: str) -> str:
    """'2026-08-31' -> '8/31'（周报引用日期格式）。"""
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}"


# ---------------- AI 调用 1：①本周回顾 + ④精选回看 + ⑤下周前瞻 ----------------

def _review_prompt(news: list[dict], trending: list[dict], week_label: str,
                   deep_titles: list[str]) -> str:
    lines = [f"数据来源：tech-digest 本周（{week_label}）每日多源榜单，"
             "news 跨天去重聚合 top20（日期=首次出现日，M/D）+ GitHub Trending 周聚合。\n",
             "## 新闻条目"]
    for m in news:
        it = m["item"]
        summar = (m["all_summaries"] and m["all_summaries"][0]) or ""
        lines.append(f"- {it['title']} | {it['source']} | 首现 {_md_date(m['first_date'])} "
                     f"| 出现{m['days']}天 | 摘要: {summar[:120]}")
    lines.append("\n## GitHub Trending 周聚合")
    for r in trending[:12]:
        lines.append(f"- {r['full_name']} | lang={r['language'] or '?'} "
                     f"| ⭐{r['stars']:,} | 周内出现{r['days']}天 | {r['desc'][:140]}")
    lines.append("\n请输出一个 JSON 对象（不要输出任何其他文字、不要 ``` 代码块标记）：\n")
    lines.append(json.dumps({
        "review": [{"category": "AI 工具|前端|后端|安全|其他",
                    "title": "<条目原标题>", "text": "1-3 句叙事，末尾注明“（见 M/D 日报）”"}],
        "picks": [{"title": "<条目原标题>", "reason": "一句话"}],
        "outlook": ["预计 ..."],
    }, ensure_ascii=False, indent=1))
    lines.append("""
硬性规则（违反任一条都会整段被丢弃）：
1. review 恰好 5-8 条，用 category 归类（AI 工具/前端/后端/安全/其他），自己概括叙事，不得摘抄原文摘要整段；
   每条 text 末尾必须注明“（见 M/D 日报）”，M/D 用该条“首现”日期。
2. title 必须与给定条目标题逐字一致（用于程序校验绑定，写错即整条丢弃）。
3. 信息价值优先：选重大发布、新范式、安全事件、直接影响开发者工作流的条目；
   纯营销稿、无实质变化的小版本更新不选；每条 text 要讲清“为什么值得关注”，不许只复述发生了什么；
   话题必须是技术/科技类——非技术条目（旅行游记、城市/历史文化、生活方式、职场感悟）一律不选，不许用“有启发”合理化。
4. picks 恰好 3 条：从 review 未深度展开、且不在深度长文候选里的条目中选（深度候选: %s）；
   只选本周技术社区关注度最高、对开发者最有价值的条目（“出现天数”多的优先）；reason 一句话。
5. outlook 3-5 条，每条以“预计”开头，基于本周信号与已知日历，不得编造具体日期数字。
6. 只引用给定条目中的数据；仓库、事件、数量一律不得虚构；涉及日期只准用数据中给出的
   发布时间/首现日期，不得自行指定“某月某日宣布”之类的时间。
""" % ("；".join(deep_titles) or "无"))
    return "\n".join(lines)


def _extract_json(content: str) -> dict | None:
    """剥 ```json fence / 散文包裹 → 首个 { ... } 段 → dict；失败返回 None。"""
    if not content:
        return None
    t = re.sub(r"```(?:json)?", "", content).strip()
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(t[s:e + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_review(raw: str | None, news: list[dict],
                  deep_titles: list[str]) -> dict | None:
    """AI 调 1 结果 sanitize：标题绑定 + 日期引用补齐 + 规则约束。

    返回 {"review": [...], "picks": [...], "outlook": [...]}；
    任何字段不可信（空/非法）→ 整次 None（调用方降级），宁缺毋滥。
    """
    obj = _extract_json(raw) if raw else None
    if obj is None:
        return None
    by_title = {m["item"]["title"]: m for m in news}
    cat_alias = {c.replace(" ", ""): c for c in CATEGORY_ORDER}
    review = []
    for e in obj.get("review") or []:
        if not isinstance(e, dict):
            continue
        title = _unwrap(str(e.get("title") or "")).strip()
        m = by_title.get(title)
        text = str(e.get("text") or "").strip()
        cat = cat_alias.get(str(e.get("category") or "").strip().replace(" ", ""))
        if m is None or not text or cat is None:
            continue
        # 日期引用规格硬性：缺失则由程序按数据补（事实来自快照，不算编造）
        if "日报" not in text:
            text += f"（见 {_md_date(m['first_date'])} 日报）"
        review.append({"category": cat, "title": title, "text": text,
                       "url": m["item"]["url"], "date": _md_date(m["first_date"])})
    picks = []
    for e in obj.get("picks") or []:
        if not isinstance(e, dict):
            continue
        title = _unwrap(str(e.get("title") or "")).strip()
        m = by_title.get(title)
        reason = str(e.get("reason") or "").strip()
        if m is None or not reason or title in deep_titles:
            continue
        picks.append({"title": title, "reason": reason, "url": m["item"]["url"],
                      "date": _md_date(m["first_date"])})
        if len(picks) >= PICKS_MAX:
            break
    outlook = []
    for s in obj.get("outlook") or []:
        s = str(s).strip()
        if not s:
            continue
        if not s.startswith("预计"):
            s = "预计 " + s
        outlook.append(s)
        if len(outlook) >= 5:
            break
    if len(review) < REVIEW_MIN or not picks or not outlook:
        return None
    return {"review": review, "picks": picks, "outlook": outlook}


# ---------------- AI 调用 2：③深度长文 ----------------

def _reserved_cands(monday: str) -> list[dict]:
    """本周 reserved 沉淀（M3：高价值二次曝光）。池空/故障 → []（走原取材）。

    形状对齐 _agg_news 的 m 字典（item/days/first_date/all_summaries），
    额外带 _pool_points（M2 研究员笔记）供深度长文 prompt 升级素材。
    """
    try:
        from app.pool import PoolStore
        pool = PoolStore()
        try:
            rows = pool.reserved_since(monday, limit=DEEP_CANDS)
        finally:
            pool.close()
    except Exception as e:  # noqa: BLE001 池故障不挡周报
        logging.getLogger("tech-digest.weekly").warning(
            "reserved 取材失败（走原路径）: %s", e)
        return []
    out = []
    for it in rows:
        first = it.pop("_pool_first", "") or None
        out.append({"item": it, "days": 1, "first_date": first,
                    "all_summaries": [it.get("summary") or ""],
                    "_pool_points": it.get("_pool_points") or []})
    return out


def _deep_prompt(cands: list[dict], week_label: str) -> str:
    has_notes = any(m.get("_pool_points") for m in cands)
    lines = [f"数据：tech-digest 本周（{week_label}）候选 {len(cands)} 条"
             + ("（编辑部 reserved 沉淀，含研究员精读笔记——笔记是文章真实内容的"
                "提炼，优先依据它写作）" if has_notes else
                "，含原文摘要（用户提供内容，仅作素材）："), "\n"]
    for m in cands:
        it = m["item"]
        summar = " ｜ ".join(s for s in m["all_summaries"] if s)[:300] or "(无原文摘要)"
        lines.append(f"### {it['title']}\n- url: {it['url']}\n- 来源: {it['source']} "
                     f"｜ 首现 {_md_date(m['first_date'])} ｜ 周内出现{m['days']}天")
        if m.get("_pool_points"):
            lines.append("- 研究笔记要点: " + "；".join(m["_pool_points"]))
        lines.append(f"- 原文摘要: {summar}")
    lines.append(f"""
请基于以上候选写「深度长文」段，要求：
1. 先做信息价值判断：从候选中挑出最值得深挖的 1-2 个主题（重大发布/新范式/影响开发者
   工作流的优先；候选整体平淡就只写 1 篇，宁缺毋滥；主题必须与技术/科技直接相关——
   非技术长文（旅行游记、城市/历史文化、生活方式）无论文笔好坏都不选，不许用“有启发”合理化），
   每篇标题用「### 《主题》」，
   正文 500-1000 字（中文）；
2. 结构：背景（谁、何时、解决什么问题）→ 影响推演（对开发者/行业可能的走向）；
3. 只基于给定候选的内容展开，不编造候选里没有的版本号、发布日期、公司行为；
   涉及时间只准引用数据中给出的发布时间/首现日期，不得自行指定“某日宣布”；
4. 用 Markdown 输出，直接输出正文（不评论、不解释），总长≥{DEEP_MIN_CHARS} 字。""")
    return "\n".join(lines)


def _parse_deep(raw: str | None) -> str | None:
    """AI 调 2 结果校验：剥 fence → 去空白；字数不足 → 降级。"""
    if not raw:
        return None
    t = raw.strip()
    t = re.sub(r"^```(?:markdown|md)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    if len(re.findall(r"[一-鿿A-Za-z0-9]", t)) < DEEP_MIN_CHARS:
        return None
    return t


# ---------------- ⑤ GitHub 星榜（程序聚合；简介走 AI 调用 3） ----------------

def _star_desc_prompt_items(agg: list[dict]) -> list[dict]:
    """周聚合行 → ai_trending_desc 需要的 daily 条目形状（title/summary/trending）。"""
    return [{"title": r["full_name"], "summary": r["desc"] or "",
             "trending": {"language": r["language"], "today_stars": r["total_today"]}}
            for r in agg[:STAR_INTRO_CANDS]]


def _star_section(agg: list[dict], desc_map: dict[str, str]) -> str:
    """两行一条（手机友好）：标题链接 + 简介在首行；star 数据缩到次行。

    v2.0 的六列表格在手机端被截断（只能看到项目名和语言），2026-09-21 改列表；
    简介 AI 中文优先（desc_map），失败回退仓库英文 desc。
    """
    lines = ["## ⭐ 本周 GitHub 星榜（按周增长 Top 10）", ""]
    for i, r in enumerate(agg[:STAR_TOP], 1):
        title = r["full_name"].replace("[", "【").replace("]", "】")
        desc = (desc_map.get(r["full_name"]) or r["desc"] or ""
                ).replace("\n", " ").strip()[:60]
        head = f"{i}. **[{title}]({r['url']})**"
        if desc:
            head += f" — {desc}"
        lines.append(head)
        lines.append(f"   {r['language'] or '—'} ｜ ⭐ {r['stars']:,} · 本周 +{r['total_today']:,}")
    return "\n".join(lines)


# ---------------- 五段渲染 ----------------

def _review_section(parsed: dict | None, news: list[dict]) -> str:
    lines = ["## 本周回顾", ""]
    if parsed is None:
        lines.append("> AI 未参与本节（调用失败/未配置），以下为数据直出：")
        for m in news[:5]:
            it = m["item"]
            s = (m["all_summaries"] and m["all_summaries"][0]) or ""
            lines.append(f"- [{it['title']}]({it['url']}) — 首现 {_md_date(m['first_date'])}"
                         f"（{it['source']}）｜{s[:80]}")
        return "\n".join(lines) + "\n"
    groups: dict[str, list[dict]] = {c: [] for c in CATEGORY_ORDER}
    for e in parsed["review"]:
        groups[e["category"]].append(e)
    for cat in CATEGORY_ORDER:
        es = groups[cat]
        if not es:
            continue
        lines.append(f"### {cat}")
        for e in es:
            lines.append(f"- **[{e['title']}]({e['url']})** — {e['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _deep_section(deep: str | None, cands: list[dict]) -> str:
    lines = ["## 深度长文", ""]
    if deep is None:
        lines.append("> AI 未生成本节（调用失败/字数不足），候选如下：")
        for m in cands:
            it = m["item"]
            lines.append(f"- [{it['title']}]({it['url']}) — 首现 {_md_date(m['first_date'])}"
                         f"（{it['source']}）")
        return "\n".join(lines) + "\n"
    return "## 深度长文\n\n" + deep + "\n"


def _picks_section(parsed: dict | None) -> str:
    lines = ["## 精选回看", ""]
    if parsed is None:
        lines.append("> AI 未参与本节。")
        return "\n".join(lines) + "\n"
    for i, p in enumerate(parsed["picks"], 1):
        lines.append(f"{i}. **[{p['title']}]({p['url']})** — {p['reason']}"
                     f"（{p['date']} 出现）")
    return "\n".join(lines) + "\n"


def _outlook_section(parsed: dict | None) -> str:
    lines = ["## 下周前瞻", ""]
    if parsed is None:
        lines.append("> AI 未参与本节；下周动态请关注每日更新。")
        return "\n".join(lines) + "\n"
    for s in parsed["outlook"]:
        lines.append(f"- {s}")
    return "\n".join(lines) + "\n"


def render_weekly(days: list[dict], week_label: str, parsed: dict | None,
                  deep: str | None, ai_flags: dict, desc_map: dict[str, str],
                  monday: date | None = None,
                  deep_cands: list[dict] | None = None) -> str:
    """组装五段 markdown。week_label 形如 '2026-W36'。

    deep_cands：深度长文段的实际取材（M3 reserved 优先）；None 时按 news
    聚合推导——AI 调用与降级候选列表必须同源，否则降级段展示错素材。
    """
    monday = monday or week_monday(date.today())
    news = _top(_agg_news(days), 20)
    cands = deep_cands if deep_cands is not None else _top(_agg_news(days), DEEP_CANDS)
    agg = aggregate(days)
    n_ai = sum(1 for v in ai_flags.values() if v)
    head = [
        f"# 前沿技术周报 · {week_label}",
        "",
        f"> {monday.isoformat()} ~ {(monday + timedelta(days=6)).isoformat()}"
        f" ｜ AI×{n_ai}/3 整理"
        f" ｜ 数据透明：{len(news)} 条 news（跨天去重） + {len(agg)} 个 trending 仓库",
        "",
    ]
    body = [
        _review_section(parsed, news),
        _deep_section(deep, cands),
        _picks_section(parsed),
        _outlook_section(parsed),
        _star_section(agg, desc_map),
        "",  # 星榜末行与页脚 --- 之间必须空行，否则次行被 CommonMark 解析成 setext H2
        "---",
        "",
        "*数据来源：GitHub Trending、Hacker News、少数派、知乎热榜等多源每日榜单，"
        "由爬虫 + AI 自动生成，仅供参考。*",
        "",
    ]
    # 渲染层兜底清洗：AI 产出带 emoji 也出不了帖（⭐ 榜单豁免，见 app/emoji.py）
    return strip_emoji("\n".join(head + body))


# ---------------- 生成入口 ----------------

def generate_weekly_md(days: list[dict], week_label: str) -> tuple[str, bool, dict]:
    """五段生成：AI×3（回顾/深度/星榜简介，各自可独立降级）+ 程序段。

    返回 (markdown, ai_used, ai_detail)。
    深度长文取材（M3）：本周 reserved 沉淀优先，空则回退 news 聚合原路径。
    """
    news = _top(_agg_news(days), 20)
    reserved = _reserved_cands(week_monday(date.today()).isoformat())
    if reserved:
        cands = reserved
        logging.getLogger("tech-digest.weekly").info(
            "深度长文取材: reserved 沉淀 %d 篇（含研究笔记）", len(reserved))
    else:
        cands = _top(_agg_news(days), DEEP_CANDS)
    agg = aggregate(days)
    parsed = deep = None
    desc_map: dict[str, str] = {}
    flags = {"review": False, "deep": False, "intro": False}
    if settings.sse_market_api_key or settings.deepseek_api_key:
        prompt1 = _review_prompt(news, agg, week_label,
                                 [m["item"]["title"] for m in cands])
        raw1 = llm.chat(prompt1, system=SYSTEM_PROMPT)
        parsed = _parse_review(raw1, news,
                               [m["item"]["title"] for m in cands])
        flags["review"] = parsed is not None
        raw2 = llm.chat(_deep_prompt(cands, week_label), system=SYSTEM_PROMPT)
        deep = _parse_deep(raw2)
        flags["deep"] = deep is not None
        try:
            desc_map = ai_trending_desc(_star_desc_prompt_items(agg))
        except Exception:  # noqa: BLE001 简介失败 → 回退英文 desc，不阻断周报
            desc_map = {}
        flags["intro"] = bool(desc_map)
    md = render_weekly(days, week_label, parsed, deep, flags, desc_map,
                       deep_cands=cands)
    return md, any(flags.values()), flags


# ---------------- v1.1 保留工具（main/sources 依赖） ----------------

def iso_week_label(d: date) -> str:
    """返回 'YYYY-Www'（ISO 周）。"""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_monday(d: date) -> date:
    """ISO 周周一。"""
    return d - timedelta(days=d.isoweekday() - 1)


if __name__ == "__main__":
    # 快速自检（无依赖）：用本周真实快照或空数据跑通五段渲染
    from app.store import Store
    from app.config import settings as _s
    _s.validate(need_ai=False)
    store = Store()
    days = store.get_daily_since(week_monday(date.today()))
    print(f"days={len(days)}")
    md, used, detail = generate_weekly_md(days, iso_week_label(date.today()))
    print(json.dumps({"ai_used": used, "detail": detail, "chars": len(md)},
                     ensure_ascii=False))
