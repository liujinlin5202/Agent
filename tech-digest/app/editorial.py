# -*- coding: utf-8 -*-
"""编辑部四角色流水线（M2）：评委 → 研究员 → 作者 → 终审。

决策留底：docs/superpowers/plans/2026-10-08-editorial-m2-quality-ring.md（D1-D8）
  · 评委：批量 rubric 评分（12 条/批），标题精确绑定，单条弃评不停刊
  · 研究员：top7 定向深读（全文抓取 fail-open，要点/引文写回 pool）
  · 作者：挑 1 + 编者按/导读/标题（护栏全量继承 daily_ai.sanitize_pick）
  · 终审：单次 0-1 分（五维加权），<0.7 打回改一轮，复审后无论分数照发
  · 断点续跑：产物 JSON 带日期戳，当日已有即复用（pod 被逐/重跑不重复烧 LLM）
  · 任一关键步整体失败 → run_pipeline 返回 None（调用方回退 M1 路径，永不停刊）
"""
from __future__ import annotations

import json
import logging
from datetime import date

from app import daily_ai, extract, llmpool
from app.config import settings
from app.pool import PoolStore
from app.preference import Preference

log = logging.getLogger("tech-digest.editorial")

TOP_N = 7          # 深读/作者候选数（上位计划 §4「top6-8」取 7）
JUDGE_BATCH = 12   # 评委批大小（24 候选 = 2 次调用，LLM 预算内）
MIN_SCORED = 2     # 评委有效产出下限：低于此数视为评委整体失能 → 回退 M1 路径

SYSTEM_PROMPT = (
    "你是校园技术社区「每日一篇」编辑部的编辑，读者是高校在校学生（会写代码，"
    "关心课程、实习、竞赛和项目）。你只基于给定材料撰写或评分，绝不编造标题、"
    "作者或事实。全文禁止使用 emoji 表情符号。"
)

_DIMS = ("novelty", "depth", "utility", "credibility", "audience")


def _today() -> str:
    return date.today().isoformat()


def _fresh(payload, kind: str) -> dict | None:
    """读既有产物：是 dict、kind 对、且是当日 → 返回（断点续跑复用，D6）。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if isinstance(payload, dict) and payload.get("kind") == kind \
            and payload.get("date") == _today():
        return payload
    return None


# ---------------- ① 评委 ----------------

def _judge_prompt(batch: list[dict], preference: Preference | None) -> str:
    prefer = ""
    if preference is not None and preference.active:
        prefer = (f"\n编辑当前倾向：{preference.text}。这是加分项不是硬性条件，"
                  "倾向话题里质量差的照实打低分。\n")
    lines = ["以下是候选文章（标题是唯一标识）：\n"]
    for it in batch:
        summary = (it.get("summary") or "").replace("\n", " ")[:200]
        lines.append(f"《{it['title']}》（来源: {it.get('source')}｜热度: "
                     f"{daily_ai._signal(it)}）\n   摘要: {summary or '（无）'}")
    lines.append(
        "\n请逐条按五个维度打分（0-10 整数）：novelty=新颖性、depth=深度、"
        "utility=实用性（读完能带走什么）、credibility=可信度（信源与实据）、"
        "audience=受众匹配（学生：课程/实习/竞赛/项目）。"
        '只输出 JSON：{"scores": [{"title": "逐字一致", "novelty": 0, '
        '"depth": 0, "utility": 0, "credibility": 0, "audience": 0}]}\n'
        "标准：讲清机制/原理/实现、有可复现步骤或一手数据的打高分；通稿/融资/"
        "榜单/营销软文各维全零；AI 行业重大动态可信度与新颖性从宽。"
        f"{prefer}"
    )
    return "\n".join(lines)


def judge_score(candidates: list[dict], pool: PoolStore,
                preference: Preference | None = None,
                dry_run: bool = False) -> dict[str, dict]:
    """批量评分。返回 {规范化url: {"total": 0-1, "dims": {...}}}。

    当日已评过的条目直接复用（断点续跑）；标题绑定失败/维度非法的条目弃评
    （score 不落库，进不了 top——评委缺席≠停刊，§5.5）。
    dry_run=True 时只算不落库（M1 D10 契约：门禁试跑零写库）。
    """
    out: dict[str, dict] = {}
    todo: list[tuple[str, dict]] = []
    for it in candidates:
        url = it.get("_pool_url") or ""
        if not url:
            continue
        existing = _fresh((pool.get_item(url) or {}).get("score_detail"), "judge")
        if existing:
            out[url] = {"total": float(existing.get("total") or 0.0),
                        "dims": existing.get("dims") or {}}
            continue
        todo.append((url, it))
    for i in range(0, len(todo), JUDGE_BATCH):
        batch = todo[i:i + JUDGE_BATCH]
        content = llmpool.map_chat([
            {"prompt": _judge_prompt([it for _, it in batch], preference),
             "system": SYSTEM_PROMPT, "max_tokens": 2048}])[0]
        try:
            data = json.loads(daily_ai._strip_fence(content))
        except (ValueError, TypeError):
            log.warning("评委批 %d 解析失败，该批弃评（%d 条）",
                        i // JUDGE_BATCH + 1, len(batch))
            continue
        by_title = {(it.get("title") or "").strip().lower(): (u, it)
                    for u, it in batch}
        for row in data.get("scores") or []:
            if not isinstance(row, dict):
                continue
            hit = by_title.get(
                daily_ai._unwrap(str(row.get("title") or "")).strip().lower())
            if hit is None:
                continue
            url, it = hit
            dims: dict[str, float] = {}
            for d in _DIMS:
                try:
                    dims[d] = max(0.0, min(10.0, float(row.get(d))))
                except (TypeError, ValueError):
                    dims = {}
                    break
            if not dims:
                log.warning("评委对《%s》维度分非法，弃评", it.get("title", "")[:30])
                continue
            total = round(sum(dims.values()) / 10.0 * 0.2, 3)
            rounded = {k: int(v) for k, v in dims.items()}
            out[url] = {"total": total, "dims": rounded}
            if not dry_run:
                pool.save_stage(url, "score", total)
                pool.save_stage(url, "score_detail", {
                    "kind": "judge", "date": _today(), "total": total,
                    "dims": rounded})
    return out


# ---------------- ② 研究员 ----------------

def _research_prompt(item: dict, text: str) -> str:
    excerpt = " ".join((text or "").split())[:4000]
    return (
        f"《{item['title']}》\n正文：{excerpt}\n\n"
        "你是研究员。精读上文，产出给作者写导读用的研究笔记。只输出 JSON：\n"
        '{"key_points": ["核心要点，共 3-6 条，每条一句话，带文中的具体数字/名词"],\n'
        ' "quotes": ["可直接引用的关键句，最多 2 条，逐字摘自原文"]}\n'
        "只提炼上文真实存在的内容，禁止编造。"
    )


def deep_research(top: list[dict], pool: PoolStore,
                  dry_run: bool = False) -> tuple[dict[str, dict | None],
                                                  dict[str, dict]]:
    """top 逐条深读。返回 ({url: research 或 None}, {url: art 全文缓存})。

    art 缓存供正文步复用（全文只抓一次）；抓取失败该条 research=None，
    作者降级用摘要成稿（§5.5 raw_summary 兜底）。dry_run 只算不落库。
    """
    title_of = {it.get("_pool_url"): it.get("title", "") for it in top}
    tasks: list[dict] = []
    order: list[str] = []
    arts: dict[str, dict] = {}
    for it in top:
        url = it.get("_pool_url") or ""
        if not url:
            continue
        if _fresh((pool.get_item(url) or {}).get("research"), "research"):
            continue
        art = extract.fetch_with_mirror(it)
        if art and len(art.get("text") or "") >= 500:
            arts[url] = art
        tasks.append({"prompt": _research_prompt(it, (art or {}).get("text") or ""),
                      "system": SYSTEM_PROMPT, "max_tokens": 1024})
        order.append(url)
    results = llmpool.map_chat(tasks)
    research: dict[str, dict | None] = {}
    for url, content in zip(order, results):
        row = None
        try:
            data = json.loads(daily_ai._strip_fence(content))
            points = [str(p).strip() for p in (data.get("key_points") or [])
                      if str(p).strip()]
            quotes = [str(q).strip() for q in (data.get("quotes") or [])
                      if str(q).strip()]
            if points:
                row = {"kind": "research", "date": _today(),
                       "key_points": points[:6], "quotes": quotes[:2],
                       "fulltext": url in arts}
        except (ValueError, TypeError, AttributeError):
            row = None
        research[url] = row
        if row is None:
            log.warning("研究员：《%s》深读失败（抓取/解析），作者将降级用摘要",
                        title_of.get(url, "")[:30])
        elif not dry_run:
            pool.save_stage(url, "research", row)
    return research, arts


# ---------------- ③ 作者 ----------------

def _author_prompt(top: list[dict], research: dict[str, dict | None]) -> str:
    lines = [f"以下是编辑部初评选出的 {len(top)} 篇候选（含研究员笔记）。"
             "请挑出**恰好 1 篇**今天最值得全文转载到校园技术社区的文章并成稿。\n"]
    for it in top:
        r = research.get(it.get("_pool_url"))
        lines.append(f"《{it['title']}》（来源: {it.get('source')}｜"
                     f"初评 {it.get('_judge_total', 0):.2f}）")
        summary = (it.get("summary") or "").replace("\n", " ")[:200]
        if summary:
            lines.append(f"   摘要: {summary}")
        if r:
            lines.append("   研究笔记要点: " + "；".join(r.get("key_points") or []))
            if r.get("quotes"):
                lines.append("   可引用: " + "；".join(r["quotes"]))
        else:
            lines.append("   研究笔记: 无（深读失败，只有摘要可依据）")
        lines.append("")
    lines.append(
        "挑选标准：信息价值一票否决（读完要能带走具体的东西；通稿/融资/榜单/"
        "营销软文不选，AI 行业重大动态从宽）；有研究员笔记且笔记扎实的优先——"
        "那代表文章真实有料；没有笔记支撑的候选，只有摘要本身足够有料才可选。\n"
        '只输出一个 JSON 对象（不要 markdown 代码块）：\n'
        '{"title": "候选原始标题（必须与上面完全一字不差）",\n'
        ' "why": "编者按 100-200 字：今天为什么值得读（结合热度/研究要点），'
        '跟学生有什么关系",\n'
        ' "digest": "500-800 字深度导读：讲清核心内容与脉络（若原文拿不到全文，'
        '这段将代替全文发布，请写得完整可读）",\n'
        ' "post_titles": ["≤30 字发帖标题候选，共 3 个"]}\n'
        "写作规则：说人话，像同学之间转述；核心技术名词保留英文；"
        "导读里最关键的 2-3 个结论或数字用 **加粗**；post_titles 钩子式，"
        "禁止「速览/动态/盘点/重磅」。"
    )
    return "\n".join(lines)


def _author_call(top: list[dict], research: dict[str, dict | None],
                 review_comments: str | None = None) -> dict | None:
    """作者调用（首稿或打回重写）+ sanitize。护栏任一层不过 → None。"""
    prompt = _author_prompt(top, research)
    if review_comments:
        prompt += (f"\n\n你的上一稿被终审打回（{review_comments[:600]}）。"
                   "请基于评审意见修改（可换选别的候选），重新输出同结构 JSON。")
    content = llmpool.map_chat([
        {"prompt": prompt, "system": SYSTEM_PROMPT, "max_tokens": 3000}])[0]
    return daily_ai.sanitize_pick(content, top)


def _build_lead(item: dict, draft: dict,
                arts: dict[str, dict]) -> tuple[str, str]:
    """正文阶梯：全文（含翻译）> 导读 > 摘要。返回 (lead, lead_kind)。"""
    art = arts.get(item.get("_pool_url") or "")
    text = (art or {}).get("text") or ""
    if text:
        if extract.is_mostly_chinese(text):
            return text, "full"
        zh = daily_ai.ai_translate(text)
        if zh:
            return zh, "full"
        log.warning("英文全文翻译失败 → 降级导读")
    digest = (draft.get("digest") or "").strip()
    if digest:
        return digest, "digest"
    summary = (item.get("summary") or "").strip()
    if summary:
        return summary, "summary"
    return "", "none"


# ---------------- ④ 终审 ----------------

def _review_prompt(item: dict, pick_d: dict, research: dict | None) -> str:
    lead_excerpt = " ".join((pick_d.get("lead") or "").split())[:2500]
    notes = ""
    if research:
        notes = "研究员笔记要点: " + "；".join(research.get("key_points") or []) + "\n"
    return (
        f"今天拟发布：《{item['title']}》（正文形态："
        + {"full": "全文/译文", "digest": "AI 导读", "summary": "原文摘要",
           "none": "仅标题"}[pick_d["lead_kind"]] + "）\n\n"
        f"编者按：{pick_d.get('why', '')}\n\n{notes}"
        f"正文（节选）：{lead_excerpt or '（无）'}\n\n"
        "你是终审主编。按五维打分（0-10）：novelty 新颖性、depth 深度、"
        "utility 实用性、credibility 可信度、audience 受众匹配（学生视角）。"
        "要点：编者按与正文是否讲了真东西；空话/串条目/与标题无关一律低分。\n"
        '只输出 JSON：{"novelty": 0, "depth": 0, "utility": 0, '
        '"credibility": 0, "audience": 0, "comments": "给作者的具体修改意见'
        '（通过也写一句；打回必须指出改什么，120 字内）"}'
    )


def review_draft(item: dict, pick_d: dict,
                 research: dict | None) -> dict | None:
    """单次 0-1 终审。LLM 失败 → None（照发，轨迹留空——评审是增强不是门锁）。"""
    content = llmpool.map_chat([
        {"prompt": _review_prompt(item, pick_d, research),
         "system": SYSTEM_PROMPT, "max_tokens": 800}])[0]
    try:
        data = json.loads(daily_ai._strip_fence(content))
        dims = {}
        for d in _DIMS:
            dims[d] = max(0.0, min(10.0, float(data.get(d))))
        total = round(sum(dims.values()) / 10.0 * 0.2, 3)
        return {"kind": "review", "date": _today(), "total": total,
                "dims": {k: int(v) for k, v in dims.items()},
                "comments": str(data.get("comments") or "").strip()[:400]}
    except (ValueError, TypeError, AttributeError):
        log.warning("终审输出解析失败，按无评审发布")
        return None


# ---------------- 流水线编排 ----------------

def run_pipeline(candidates: list[dict], preference: Preference | None = None,
                 pool: PoolStore | None = None,
                 dry_run: bool = False) -> dict | None:
    """编辑部流水线入口。返回 {"pick": pick_d, "review": review 或 None}。

    pick_d 与 main.run_daily 契约同构（item/idx/why/lead/lead_kind/author/
    glossary/post_titles）。任一关键步整体失败 → None（调用方回退 M1 路径）。
    dry_run=True：完整跑链路（含真实 LLM），但产物与状态零落库（D10 契约）。
    """
    if not candidates:
        return None
    if not (settings.sse_market_api_key or settings.deepseek_api_key):
        return None
    owned = pool is None
    pool = pool or PoolStore()
    try:
        # ① 评委
        scores = judge_score(candidates, pool, preference, dry_run=dry_run)
        for it in candidates:
            s = scores.get(it.get("_pool_url") or "")
            if s:
                it["_judge_total"] = s["total"]
        scored = sorted((it for it in candidates if "_judge_total" in it),
                        key=lambda it: -it["_judge_total"])
        if len(scored) < MIN_SCORED:
            log.warning("评委有效产出 %d < %d，编辑部停摆（回退 M1 路径）",
                        len(scored), MIN_SCORED)
            return None
        top = scored[:TOP_N]
        log.info("编辑部: 评委通过 %d/%d，深读 top%d（头部 %s）",
                 len(scored), len(candidates), len(top),
                 ["%.2f %s" % (it["_judge_total"], it["title"][:18])
                  for it in top[:3]])
        top_by_url = {it.get("_pool_url"): it for it in top}

        # ② 研究员（当日已有产物的条目自动跳过抓取与调用）
        research, arts = deep_research(top, pool, dry_run=dry_run)
        for it in top:
            if research.get(it.get("_pool_url")):
                it["_research"] = research[it.get("_pool_url")]

        # ③ 作者（首稿）
        draft = _author_call(top, research)
        if draft is None:
            log.warning("作者成稿被护栏拦下，编辑部停摆（回退 M1 路径）")
            return None
        item = top[draft["idx"] - 1]

        lead, kind = _build_lead(item, draft, arts)
        pick_d = {"item": item, "idx": draft["idx"], "why": draft["why"],
                  "lead": lead, "lead_kind": kind,
                  "author": (arts.get(item.get("_pool_url")) or {}).get("author") or "",
                  "glossary": [], "post_titles": draft.get("post_titles") or []}

        # ④ 终审（< 阈值 → 打回一轮 → 复审；复审后无论分数照发，D5）
        review = review_draft(item, pick_d, research.get(item.get("_pool_url")))
        if review is not None and review["total"] < settings.review_threshold:
            log.warning("终审 %.2f < %.2f，打回作者改一轮",
                        review["total"], settings.review_threshold)
            revised = _author_call(top, research, review_comments=review["comments"])
            if revised is not None:
                item = top[revised["idx"] - 1]
                pick_d.update({"item": item, "idx": revised["idx"],
                               "why": revised["why"],
                               "post_titles": revised.get("post_titles") or [],
                               "author": (arts.get(item.get("_pool_url"))
                                          or {}).get("author") or ""})
                if pick_d["lead_kind"] != "full":
                    lead, kind = _build_lead(item, revised, arts)
                    pick_d["lead"], pick_d["lead_kind"] = lead, kind
            review = review_draft(item, pick_d,
                                  research.get(item.get("_pool_url"))) or review
            log.info("复审 %.2f（无论分数照发，轨迹留库）",
                     (review or {}).get("total", -1))
        if review is not None and not dry_run:
            pool.save_stage(item["_pool_url"], "review", review)

        # 高价值沉淀（D7）：top 里未入选且 ≥ 沉淀线 → reserved
        reserve_urls = [it["_pool_url"] for it in top
                        if it is not item
                        and it.get("_judge_total", 0) >= settings.reserve_score]
        if reserve_urls and not dry_run:
            n = pool.mark_reserved(reserve_urls)
            log.info("沉淀: %d 篇落选高分候选标 reserved（>=%.2f）",
                     n, settings.reserve_score)

        # 知识卡片（装饰性，最后补；失败置空不影响发布）
        try:
            pick_d["glossary"] = daily_ai.ai_glossary(
                item.get("title") or "",
                pick_d["lead"] or pick_d["why"] or (item.get("summary") or ""))
        except Exception:  # noqa: BLE001
            pick_d["glossary"] = []
        return {"pick": pick_d, "review": review}
    finally:
        if owned:
            pool.close()
