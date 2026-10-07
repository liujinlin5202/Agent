# -*- coding: utf-8 -*-
"""tech-digest 入口（一次性进程，由 systemd timer 触发）。

用法:
  python main.py daily                  # 每天 1 篇爆文（全文转载或 AI 导读）→ 落盘 + 发帖
  python main.py daily --dry-run        # 日报：渲染落盘但不发帖、不写库（token 校验到为止）
  python main.py daily --force          # 同日重复触发也重新执行（已发帖则不重发）
  python main.py daily --private        # 日报作为私密帖发布（默认公开帖）
  python main.py star                   # 周六 GitHub 星榜专帖（Top15 全量，不折叠）
  python main.py star --dry-run / --force / --private
  python main.py weekly                 # 周日五段式 AI 周报 + 集市发帖 → output/{Wxx}-weekly.md
  python main.py weekly --dry-run       # 仅预览五段到 {Wxx}-weekly-preview.md（不落库不发帖）

v4（2026-09-17 用户决策）：
  · 日报从「3-4 条摘要 + 折叠星榜」改为「每天只挑 1 篇爆点文章」——
    时效性×影响性×学生相关性，AI 挑 1，全文直接搬运（英文 AI 翻译成中文），
    前面写「转载自 xxx」；抓不到全文的（知乎/公众号系反爬）降级 AI 深度导读+链接。
  · GitHub 星榜移出日报 → 每周六单独发一帖（全量 15 条、不折叠）。
  · 周日周报不变。

工作日（周一~周五）只发日报不发星榜；周末日报只抓取入库、不发帖（数据进周报池，
TECH_DIGEST_WEEKEND_PUBLISH=1 可打开）。星榜帖只在周六发（不受上述开关影响）。

1 楼补楼（评论区发星榜）已于 2026-09-14 下线：评论区不再发任何东西。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

from app import daily_ai, dedup, extract, marketdb, mirror, preference, titles
from app.config import settings
from app.digest import headline_of, render_repost, render_star_post
from app.loadgate import should_skip
from app.sources import fetch_all
from app.store import Store
from app.weekly import generate_weekly_md, iso_week_label, week_monday

log = logging.getLogger("tech-digest")


def setup_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(settings.log_dir / "tech-digest.log",
                                  maxBytes=500_000, backupCount=14, encoding="utf-8")
    handler.setFormatter(fmt)
    root.addHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)


# ---------------- daily（v4：每天 1 篇爆文） ----------------

def _daily_candidates(news: list[dict]) -> list[dict]:
    """v4.5 候选池（10 源）：知乎热榜 → HN → sspai → devto → 订阅/见闻轮转，上限 48。

    排序只影响候选池截断（每源取前 N），真正「挑哪篇」由 AI 按
    时效×影响×学生相关 决定——候选行里的热度信号就是它的判断依据。
    订阅源（Ars/IEEE/fcc/阮一峰/见闻）无投票热度，各取 3 条保证 AI 每天能看到它们；
    v4.5 新源排在订阅元组前（极端满员日截断优先保新源）。
    """
    zhihu = [it for it in news if it["source"] == "zhihu-hot"][:12]
    hn = sorted((it for it in news if it["source"] == "hacker-news"),
                key=lambda it: -(it.get("points") or 0))[:8]
    sspai = [it for it in news if it["source"] == "sspai"][:4]
    devto = sorted((it for it in news if it["source"] == "devto"),
                   key=lambda it: -(it.get("points") or 0))[:4]
    feeds: list[dict] = []
    for src in ("wallstreetcn", "ars-technica", "ieee-spectrum", "freecodecamp",
                "ruanyifeng"):
        feeds += [it for it in news if it["source"] == src][:3]
    rest = [it for it in news if it["source"] not in (
        "zhihu-hot", "hacker-news", "sspai", "devto", "wallstreetcn",
        "ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng")][:5]
    return (zhihu + hn + sspai + devto + feeds + rest)[:48]


# ---- 可爬性预取闸门（2026-10-02，用户反馈「这两天选文信息量低」）----------------

GATE_MIN_CHARS = 1_500    # 正文低于此长度视为没料（通稿/短讯），不进挑选池
GATE_QUALIFY = 6          # 凑满几篇「验证可抓全文」就停
GATE_MAX_ATTEMPTS = 14    # 试抓上限（预算护栏：最坏 14 次请求也在 5min 红线内）
GATE_EXCERPT_CHARS = 400  # 送进挑选 prompt 的正文预览长度

# 轮转顺序：每轮各源出一个（防单源刷屏挤掉订阅源；HN/devto 源内按热度降序）
_GATE_CYCLE = ("hacker-news", "zhihu-hot", "sspai", "devto", "wallstreetcn",
               "ars-technica", "ieee-spectrum", "freecodecamp", "ruanyifeng",
               "__rest__")


def _gate_groups(candidates: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {k: [] for k in _GATE_CYCLE}
    for it in candidates:
        groups.get(it.get("source") or "", groups["__rest__"]).append(it)
    groups["hacker-news"].sort(key=lambda it: -(it.get("points") or 0))
    groups["devto"].sort(key=lambda it: -(it.get("points") or 0))
    return groups


def _try_fetch_art(item: dict) -> dict | None:
    """原文抓取公共阶梯（闸门与 _fetch_body 共用，全文只抓一次）：extract → 镜像。"""
    art = None
    try:
        art = extract.fetch_article(item["url"])
    except Exception as e:  # noqa: BLE001 单篇抓取失败 → 镜像通道
        log.warning("原文抓取失败（%s）→ 尝试镜像通道: %s", item.get("title"), e)
    if art is None and mirror.needs_mirror(item.get("url") or ""):
        # wallstreetcn 文章页是 SPA 空壳 → 内容 API 确定性取全文（app/mirror.py）
        try:
            art = mirror.mirror_fetch(item["url"])
        except Exception as e:  # noqa: BLE001 镜像失败 → 交还调用方降级
            log.warning("镜像通道失败（%s）: %s", item.get("title"), e)
    return art


def _prefetch_gate(candidates: list[dict]) -> list[dict]:
    """可爬性预取闸门：ai_pick 只在「验证过能抓到全文」的候选里挑。

    起因：挑选发生在抓取之前，AI 只看标题+200 字摘要+热度定生死——通稿/
    标题党和硬核文在这个信号集里无法区分，选完才发现抓不到全文（知乎 403/
    SPA 空壳）降级 AI 导读，读者收获只剩转述。闸门按源轮转试抓，凑满
    GATE_QUALIFY 篇合格（正文 ≥GATE_MIN_CHARS 字）或到 GATE_MAX_ATTEMPTS
    即停。返回候选的浅拷贝：_art 全文缓存挂在拷贝上（_fetch_body 直接用，
    不重抓），绝不污染 news 原条目——它们会被 save_daily_v2 序列化进库。
    全军覆没返回 []，调用方回退全量候选池（老行为，宁降级不缺席）。
    """
    groups = _gate_groups(candidates)
    qualified: list[dict] = []
    attempts = 0
    while attempts < GATE_MAX_ATTEMPTS and len(qualified) < GATE_QUALIFY:
        progressed = False
        for key in _GATE_CYCLE:
            if attempts >= GATE_MAX_ATTEMPTS or len(qualified) >= GATE_QUALIFY:
                break
            g = groups[key]
            if not g:
                continue
            progressed = True
            it = g.pop(0)
            attempts += 1
            art = _try_fetch_art(it)
            text = (art or {}).get("text") or ""
            if len(text) < GATE_MIN_CHARS:
                continue
            cand = dict(it)
            cand["_art"] = art
            cand["body_len"] = len(text)
            cand["excerpt"] = " ".join(text.split())[:GATE_EXCERPT_CHARS]
            qualified.append(cand)
        if not progressed:
            break
    return qualified


def _dedup_history(store: Store, day: str) -> list[tuple[str, dict]]:
    """去重窗口：今日之前最近 14 个快照的 news 条目（v1 条目视为 trending，天然豁免）。

    2026-09-17 v4 扩到 14 期：摘要日报重复一次还能忍，爆文全文重复转载太刺眼。
    """
    days = [d for d in store.get_daily_since(date.today() - timedelta(days=30))
            if d["date"] < day][-14:]
    return [(d["date"], it) for d in days for it in d["items"]
            if (it.get("type") or "trending") == "news"]


def _fallback_title(pick: dict | None, candidates: list[dict]) -> str:
    """兜底发帖标题：选中文章的原题清洗到 30 字内。

    v3 的教训仍在：不要回退到「AI 生态爆发」这类抽象主题——那是实测浏览最低的一类。
    v4 更简单：兜底就是「这篇文章自己的标题」。
    """
    lead = ""
    if pick:
        idx = pick.get("idx") or 0
        if 1 <= idx <= len(candidates):
            lead = candidates[idx - 1].get("title") or ""
    if not lead and candidates:
        lead = candidates[0].get("title") or ""
    return headline_of(lead, "", "")


def _pick_post_title(ai_result: dict | None, recent: list[str], fallback: str = "") -> str:
    """发帖标题：AI 候选过 app/titles.py 的校验阶梯，全不合格才用兜底。"""
    return titles.pick((ai_result or {}).get("post_titles") or [], recent, fallback)


def _has_ai() -> bool:
    return bool(settings.sse_market_api_key or settings.deepseek_api_key)


def _pick_article(candidates: list[dict], day: str,
                  preference: preference.Preference | None = None) -> tuple[dict | None, dict | None, bool]:
    """AI 挑 1 篇爆文。失败 → 首条候选 + 空按语（不缺席，只降级）。降级路径不掺倾向。"""
    pick = None
    if _has_ai() and candidates:
        try:
            pick = daily_ai.ai_pick(candidates, day, preference=preference)
        except Exception as e:  # noqa: BLE001 AI 是增强项，失败不阻塞发帖
            log.warning("AI 挑选失败（降级首条候选）: %s", e)
            pick = None
    if pick:
        item = candidates[pick["idx"] - 1]
        return item, pick, True
    return candidates[0] if candidates else None, None, False


def _fetch_body(item: dict, has_ai: bool) -> tuple[str, str, str]:
    """抓选中文章的全文 → (body, mode, author)。

    mode: full=全文可发（中文直用 / 英文中译）；digest=拿不到全文，走 AI 导读。
    item 带 _art（预取闸门的全文缓存）时直接用，不再发请求——全文只抓一次。
    wallstreetcn 候选走镜像通道（SPA 文章页 → 内容 API，见 app/mirror.py）；
    知乎/公众号 403 面维持 digest 降级。
    原文抓取失败（反爬/SPA/付费墙）与翻译失败都不抛——降级导读，当天不缺席。
    """
    art = item.get("_art") or _try_fetch_art(item)
    if art:
        author = art.get("author") or ""
        text = art.get("text") or ""
        if extract.is_mostly_chinese(text):
            return text, "full", author
        if has_ai:
            try:
                zh = daily_ai.ai_translate(text)
            except Exception as e:  # noqa: BLE001
                log.warning("翻译调用失败: %s", e)
                zh = None
            if zh:
                return zh, "full", author
            log.warning("英文全文翻译失败 → 降级导读")
    return "", "digest", ""


def _publish_allowed(day: str) -> bool:
    """日报是否发帖：工作日发，周末只抓取入库（2026-09-13 用户决策）。

    周末数据仍进 daily_snapshot，周报（周日）照常聚合，只是不发日报帖。
    日期解析失败一律放行——宁可多发一次，也不要因为一个畸形参数静默不发。
    """
    if settings.publish_weekend:
        return True
    try:
        return date.fromisoformat(day).weekday() < 5
    except ValueError:
        return True


def _verify_published(store: Store, task: str, post_id: int | None, title: str) -> None:
    """发帖后只读回读：确认帖子真的公开可见（2026-09-20 星榜帖 #6311 被写私密事故）。

    观测动作，绝不影响发帖结果：发帖此时已成功、幂等标记已写，定位不到/库不可用/
    任何异常都只记日志，绝不因此重发。postID 未返回（后端 data=nil）时按
    「本人 + 标题 + 10 分钟时间窗」从库里捞回（marketdb.find_post_id 同款口径）。
    """
    try:
        if not post_id:
            uid = marketdb.user_id_of(settings.market_user_telephone or "")
            since = datetime.now() - timedelta(minutes=10)
            post_id = marketdb.find_post_id(uid, title, since) if uid else None
        if not post_id:
            store.log(task, "degraded", {"detail": "publish_verify",
                                         "reason": "post_not_located"})
            log.error("回读校验：%s 帖无法定位（postID 未返回且库中查不到），公开性未知", task)
            return
        private = marketdb.post_is_private(post_id)
        if private is None:
            log.warning("回读校验：集市库不可用，帖 %s 可见性未知", post_id)
        elif private:
            store.log(task, "degraded", {"detail": "publish_verify",
                                         "error": f"post {post_id} is_private=1（发帖被写私密）"})
            log.error("回读校验失败：%s 帖 %s 被写私密，需人工处理！", task, post_id)
        else:
            log.info("回读校验通过：帖 %s 公开可见", post_id)
    except Exception as e:  # noqa: BLE001 —— 校验绝不能弄崩已成功的发帖
        log.warning("回读校验异常（忽略）: %s", e)


def _publish_md(store: Store, task: str, day: str, title: str, md: str,
                dry_run: bool, private: bool = False) -> None:
    """通用发帖（幂等：已发过不重发；失败重试 ≤1 次）。task: daily | star。"""
    from app import publisher  # 延迟导入：无 key 环境不依赖发帖配置
    if task == "daily" and not _publish_allowed(day):
        store.log(task, "skipped", {"detail": "weekend", "day": day})
        log.info("周末只抓取入库、不发帖（数据仍进周报池）：%s 跳过发帖", day)
        return
    if not settings.daily_publish:
        store.log(task, "degraded",
                  {"detail": "publish", "reason": "TECH_DIGEST_DAILY_PUBLISH=0"})
        log.info("发帖已关闭（TECH_DIGEST_DAILY_PUBLISH=0），仅落盘")
        return
    if not settings.market_refresh_token or not settings.market_user_telephone:
        store.log(task, "degraded",
                  {"detail": "publish", "reason": "未配置 MARKET_REFRESH_TOKEN/TELEPHONE"})
        log.warning("未配置发帖凭据，仅落盘")
        return
    posted = store.is_daily_posted(day) if task == "daily" else store.is_star_posted(day)
    if posted:
        store.log(task, "skipped", {"detail": "publish_dup"})
        log.info("今日已发布过（meta 标记），--force 重跑不重发")
        return
    if dry_run:
        auth = publisher.refresh_access_token()
        if auth:
            log.info("dry-run: access token 校验通过（未发帖）")
            store.log(task, "ok", {"detail": "dry_run_token_ok"})
        else:
            store.log(task, "degraded", {"detail": "publish", "reason": "dry_run_token_fail"})
            log.error("dry-run: token 校验失败")
        return
    for attempt in (1, 2):
        post_id, url, err = publisher.publish_md(title, md, private=private)
        if err is None:
            if task == "daily":
                store.mark_daily_posted(day, post_id or 0)
                store.set_daily_title(day, title)
            else:
                store.mark_star_posted(day, post_id or 0)
            store.log(task, "ok", {"detail": "published", "post_id": post_id,
                                   "post_url": url, "title": title})
            log.info("%s 已发布: postID=%s %s", task, post_id or "(未返回)", url or "")
            if not private:  # 有意发私密（预览）不用回读
                _verify_published(store, task, post_id, title)
            return
        log.warning("%s 发帖第 %d 次失败: %s", task, attempt, err)
        if attempt == 2:
            store.log(task, "degraded", {"detail": "publish", "error": err})


def _sample_views(store: Store) -> None:
    """给最近几期的帖子记一次浏览采样（表现追踪的原始数据源）。

    集市库的 browse_num 是实时值、没有历史快照，不自己存就永远只知道「现在」。
    daily 09:15 跑 → 对昨天发的帖正好是发布后 ~24 小时，这就是复盘要的 h24 口径。

    采样是观测手段，绝不能影响发帖：任何失败只记一行日志。
    """
    try:
        uid = marketdb.user_id_of(settings.market_user_telephone)
        if not uid:
            return
        since = (date.today() - timedelta(days=7)).isoformat()
        posts = marketdb.posts_since(uid, since)
        today = date.today().isoformat()
        for p in posts:
            store.add_view_sample(p["post_id"], p["day"], today, p["browse"],
                                  p["likes"], p["comments"], p["heat"])
        if posts:
            log.info("表现采样：%d 期", len(posts))
    except Exception as e:   # noqa: BLE001 —— 观测手段不得影响发帖
        log.warning("表现采样失败（忽略，不影响发帖）: %s", e)


def run_daily(force: bool, dry_run: bool = False, private: bool = False) -> int:
    store = Store()
    try:
        day = date.today().isoformat()
        if store.daily_exists(day) and not force:
            store.log("daily", "skipped", {"detail": "dup"})
            log.info("今日快照已存在，跳过（--force 可重新生成）")
            return 0
        skip, reason = should_skip(settings.load_threshold)
        if skip and settings.check_load:
            store.log("daily", "skipped", {"detail": "load", "reason": reason})
            log.info("负载过高，跳过本次: %s", reason)
            return 0
        t0 = time.monotonic()
        items, stats = fetch_all()
        elapsed = time.monotonic() - t0
        trending = [it for it in items if it["type"] == "trending"]
        news_raw = [it for it in items if it["type"] == "news"]
        news, dedup_info = dedup.dedupe_news(news_raw, _dedup_history(store, day))
        dedup_removed = len(dedup_info["removed"])

        pref = preference.load_preference()
        if pref.active:
            log.info("倾向模式: prefer（%s…）", pref.text[:30])
        else:
            log.info("倾向模式: 默认")
        candidates = _daily_candidates(news)
        gated = _prefetch_gate(candidates)
        pool = gated or candidates
        if gated:
            log.info("可爬性闸门: %d/%d 篇验证可抓全文，AI 只在此池中挑选",
                     len(gated), len(candidates))
        else:
            log.warning("可爬性闸门: 0 篇通过（全军覆没），回退全量候选池")
        item, pick, ai_used = _pick_article(pool, day, pref)
        if not item:
            store.log("daily", "error", {"detail": "no_candidates"})
            log.error("没有任何候选文章（全部源失败/去重清空），不产出日报")
            return 1

        has_ai = _has_ai()
        body, mode, author = _fetch_body(item, has_ai)
        why = (pick or {}).get("why", "").strip()
        digest = (pick or {}).get("digest", "").strip()
        # lead/lead_kind 契约（见 digest.render_repost）：全文 > AI 导读 > 原文摘要 > 空手
        if mode == "full":
            lead, kind = body, "full"
        elif digest:
            lead, kind = digest, "digest"
        elif (item.get("summary") or "").strip():
            # AI 没参与（无 key / 调用失败）：拿原文摘要顶上，渲染时明示「不是全文」
            lead, kind = item["summary"].strip(), "summary"
        else:
            lead, kind = "", "none"
        # 知识卡片（v4.1）：AI 失败返回 []，纯装饰不影响发布。
        # 语境优先级：全文译文 > AI 导读（digest 模式下正文缺席）> 原文摘要
        glossary = daily_ai.ai_glossary(
            item.get("title") or "",
            body or digest or (item.get("summary") or "")) if has_ai else []
        pick_d = {"item": item, "why": why, "lead": lead, "lead_kind": kind,
                  "author": author, "glossary": glossary}

        issue_no = store.get_issue_no(day) or store.next_issue_no()
        recent = store.recent_daily_titles(7, before_day=day)
        title = _pick_post_title(pick, recent, _fallback_title(pick, pool))
        ctx = {"issue_no": issue_no, "pick": pick_d}
        md = render_repost(day, issue_no, ctx)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        out = settings.output_dir / f"{day}-daily.md"
        out.write_text(md, encoding="utf-8")
        # 快照与期数绑定：只许真跑写库。--dry-run 曾把快照写进库并递增 issue_next
        # （2026-09-14 实锤），害 09:15 真跑被幂等检查跳过、停更一天——dry-run 必须零写入。
        if not dry_run:
            store.save_daily_v2(day, news + trending, str(out))
        store.cleanup()

        src_errors = {n: s["error"] for n, s in stats.items() if s["error"]}
        degraded = bool(src_errors) or (has_ai and (not ai_used or mode == "digest"))
        store.log("daily", "degraded" if degraded else "ok", {
            "source_stats": {n: {k: v for k, v in s.items() if k != "detail"}
                             for n, s in stats.items()},
            "dedup_removed": dedup_removed,
            "dedup_detail": dedup_info["removed"][:5],
            "news": len(news), "trending": len(trending),
            "issue_no": issue_no, "ai_used": ai_used, "mode": mode,
            "gate_qualified": len(gated),
            "pick_title": item.get("title", ""), "title": title,
            "pref_mode": "prefer" if pref.active else "default",
            "pref_text": pref.text[:30],
            "elapsed_s": round(elapsed, 1), **({"errors": src_errors} if src_errors else {}),
        })
        log.info("daily ok: issue=#%d mode=%s pick=%s 标题=%s -> %s",
                 issue_no, mode, item.get("title", "")[:30], title or "(空)", out)
        _publish_md(store, "daily", day, title, md, dry_run, private)
        _sample_views(store)
        return 0
    finally:
        store.close()


# ---------------- star（周六 GitHub 星榜专帖） ----------------

def _star_title(day: str) -> str:
    try:
        d = date.fromisoformat(day)
        return f"{d.month}/{d.day} GitHub 开源星榜 Top 15"
    except ValueError:
        return "GitHub 开源星榜 Top 15"


def _star_publish_allowed(day: str) -> bool:
    """星榜帖只在周六发（2026-09-17 用户决策：星榜调整到周六发一次）。

    timer 只会周六触发；这里兜底：手动误跑在工作日也不发。
    publish_weekend 开关不拦星榜——它本来就是周末内容。
    """
    try:
        return date.fromisoformat(day).weekday() == 5
    except ValueError:
        return True


def run_star(force: bool, dry_run: bool = False, private: bool = False) -> int:
    store = Store()
    try:
        day = date.today().isoformat()
        if not _star_publish_allowed(day):
            store.log("star", "skipped", {"detail": "not_saturday", "day": day})
            log.info("星榜帖只在周六发（今天 %s），跳过", day)
            return 0
        if store.is_star_posted(day) and not force:
            store.log("star", "skipped", {"detail": "dup"})
            log.info("今日星榜已发过，跳过（--force 可重新生成）")
            return 0
        skip, reason = should_skip(settings.load_threshold)
        if skip and settings.check_load:
            store.log("star", "skipped", {"detail": "load", "reason": reason})
            log.info("负载过高，跳过本次: %s", reason)
            return 0
        t0 = time.monotonic()
        items, stats = fetch_all(["github-trending"])
        trending = [it for it in items if it["type"] == "trending"]
        if not trending:
            store.log("star", "error", {"detail": "no_trending"})
            log.error("GitHub Trending 抓取为空，不产出星榜")
            return 1
        desc_map: dict[str, str] = {}
        if _has_ai():
            try:
                desc_map = daily_ai.ai_trending_desc(trending[:15])
            except Exception as e:  # noqa: BLE001
                log.warning("星榜中文简介失败（回退英文简介）: %s", e)
        md = render_star_post(day, trending, desc_map)
        if not md:
            store.log("star", "error", {"detail": "empty_md"})
            return 1
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        out = settings.output_dir / f"{day}-star.md"
        out.write_text(md, encoding="utf-8")
        store.cleanup()
        store.log("star", "ok" if not stats["github-trending"]["error"] else "degraded", {
            "source_stats": {n: {k: v for k, v in s.items() if k != "detail"}
                             for n, s in stats.items()},
            "trending": len(trending), "ai_desc": len(desc_map),
            "elapsed_s": round(time.monotonic() - t0, 1),
        })
        title = _star_title(day)
        log.info("star ok: trending=%d desc=%d -> %s", len(trending), len(desc_map), out)
        _publish_md(store, "star", day, title, md, dry_run, private)
        return 0
    finally:
        store.close()


# ---------------- weekly ----------------

def run_weekly(force: bool, dry_run: bool, private: bool = False) -> int:
    from app import publisher  # 延迟导入：daily 不依赖发帖配置
    store = Store()
    try:
        week = iso_week_label(date.today())
        if store.weekly_exists(week) and not force:
            store.log("weekly", "skipped", {"detail": "dup"})
            log.info("本周周报已生成，跳过（--force 可重新生成）")
            return 0
        skip, reason = should_skip(settings.load_threshold)
        if skip and settings.check_load:
            store.log("weekly", "skipped", {"detail": "load", "reason": reason})
            log.info("负载过高，跳过本次: %s", reason)
            return 0
        monday = week_monday(date.today())
        days = store.get_daily_since(monday)
        if not days:
            store.log("weekly", "error", {"detail": "no_daily_data"})
            log.error("本周无每日榜数据（周录起点 %s），无法生成周报", monday)
            return 1
        md, ai_used, ai_detail = generate_weekly_md(days, week)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        if dry_run:
            # dry-run 只落预览文件、不写 weekly_report：否则周六预览会挡住周日真实发布
            # （weekly_exists 幂等检查）——2026-09-05 预览 / 09-06 真发的关键前提。
            out = settings.output_dir / f"{week}-weekly-preview.md"
            out.write_text(md, encoding="utf-8")
            log.info("weekly preview: ai=%s -> %s（未落库未发帖）", ai_detail, out)
            auth = publisher.refresh_access_token()
            if auth:
                log.info("dry-run: access token 校验通过（未发帖）")
                store.log("weekly", "ok", {"detail": "dry_run_token_ok", "ai": ai_detail})
            else:
                store.log("weekly", "degraded", {"detail": "publish", "reason": "dry_run_token_fail"})
                log.error("dry-run: token 校验失败")
            store.cleanup()
            return 0
        out = settings.output_dir / f"{week}-weekly.md"
        out.write_text(md, encoding="utf-8")
        store.save_weekly(week, str(out), ai_used)
        log.info("weekly md: ai=%s rows=%d -> %s", ai_detail, len(days), out)

        # 发布（仅真实执行；dry-run 已在上方提前 return）
        if not settings.market_refresh_token:
            store.log("weekly", "degraded",
                      {"detail": "publish", "reason": "未配置 MARKET_REFRESH_TOKEN"})
            log.warning("未配置发帖凭据，周报仅落盘")
            store.cleanup()
            return 0
        for attempt in (1, 2):
            post_id, url, err = publisher.publish_md(
                f"前沿技术周报 · {week}", md, private=private)
            if err is None:
                store.mark_published(week, post_id or 0, url or "")
                store.log("weekly", "ok", {"detail": "published", "post_id": post_id,
                                           "post_url": url, "ai": ai_used})
                log.info("已发布: postID=%s %s", post_id or "(未返回)", url or "")
                if not private:
                    _verify_published(store, "weekly", post_id, f"前沿技术周报 · {week}")
                break
            log.warning("发帖第 %d 次失败: %s", attempt, err)
            if attempt == 2:
                store.log("weekly", "degraded", {"detail": "publish", "error": err})
        store.cleanup()
        return 0
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="tech-digest: 每日爆文转载 + 周六星榜 + AI 周报")
    ap.add_argument("task", choices=["daily", "star", "weekly"])
    ap.add_argument("--force", action="store_true", help="重复触发也重新执行")
    ap.add_argument("--dry-run", action="store_true",
                    help="渲染落盘但不发帖、不写库（token 校验到为止）")
    ap.add_argument("--private", action="store_true",
                    help="发布为私密帖（仅自己可见；默认公开帖）")
    args = ap.parse_args()
    setup_logging()
    try:
        settings.validate(need_ai=(args.task == "weekly"))
    except RuntimeError as e:
        log.error("配置错误: %s", e)
        return 2
    log.info("=== start %s (force=%s dry_run=%s) ===", args.task, args.force, args.dry_run)
    t0 = time.monotonic()
    if args.task == "daily":
        rc = run_daily(args.force, args.dry_run, args.private)
    elif args.task == "star":
        rc = run_star(args.force, args.dry_run, args.private)
    else:
        rc = run_weekly(args.force, args.dry_run, args.private)
    log.info("=== done %s in %.1fs rc=%s ===", args.task, time.monotonic() - t0, rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
