# -*- coding: utf-8 -*-
"""tech-digest 入口（一次性进程，由 systemd timer 触发）。

用法:
  python main.py ingest                 # 高频抓取入池（每 2h 一班，零 LLM；编辑部 M1）
  python main.py ingest --dry-run       # 跑完整抓取/查重链路但零写池（门禁用）
  python main.py daily                  # 单篇研究学习文章，经审核与最终检查后落盘、发帖
  python main.py daily --dry-run        # 审核通过后落盘，仍有日志/清理/采样及 token 校验
  python main.py daily --force          # 未发布快照允许重试；已发布则立即退出
  python main.py daily --private        # 日报作为私密帖发布（默认公开帖）
  python main.py star                   # 周六 GitHub 星榜专帖（Top15 全量，不折叠）
  python main.py star --dry-run / --force / --private
  python main.py weekly                 # 周日五段式 AI 周报 + 集市发帖 → output/{Wxx}-weekly.md
  python main.py weekly --dry-run       # 仅预览五段到 {Wxx}-weekly-preview.md（不落库不发帖）

编辑部 M1（2026-10-08，计划与决策留底见 docs/EDITORIAL_PLAN.md 与
docs/superpowers/plans/2026-10-08-editorial-m1-content-pool.md）：
  · ingest 每 2h 抓全源入池（content pool，SQLite pool_items 表，零 LLM）
  · daily 优先池内全窗口粗排 top24，池空/池故障则即时抓取候选，两者均进入
    research-v1 pipeline；生成或审核失败跳过，不使用旧转载/摘要路径补位。
    独立最终检查通过后才存正式稿，确认新发布成功后才标 daily_used。
  · star 取 trending 池的「最新一班」（48h 内）；空则回退即时抓取

v4 历史（2026-09-17；日报内容路径现已由 research-v1 取代）：
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
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from uuid import uuid4

from app import daily_ai, dedup, editorial, extract, marketdb, preference, publication_gate, titles
from app.config import settings
from app.digest import headline_of, render_repost, render_star_post
from app.dedup import normalize_url
from app.loadgate import should_skip
from app.pool import PoolStore
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


# ---------------- ingest（M1：高频抓取入池，零 LLM） ----------------

# 池模式 daily 送可爬性闸门的候选上限：gate 试抓是真实 HTTP，预算护栏与老路径
# 同量级（_prefetch_gate 自身 14 次上限不变），池子全量直灌会放大反爬风险
POOL_CANDIDATE_LIMIT = 24

# 反爬命中判定：fetcher 对 4xx 不重试，错误消息带状态码；命中即冷却该源 24h（D9）
_COOLDOWN_CODES = ("403", "405")
_COOLDOWN_HOURS = 24


def run_ingest(dry_run: bool = False) -> int:
    """抓全源入池。零 LLM；单源失败不影响其余源；全源失败池子保持原状。"""
    store = Store()
    pool = PoolStore()
    try:
        # 不接负载闸门（决策 D13）：入池是低风险 HTTP，池子断供的代价比一次爬取高
        cooling = pool.cooldowns()
        sources = [s for s in settings.tech_digest_sources if s not in cooling]
        if cooling:
            log.info("反爬冷却中，本班跳过: %s", cooling)
        t0 = time.monotonic()
        try:
            items, stats = fetch_all(sources)
        except RuntimeError as e:
            store.log("ingest", "error", {"detail": "all_sources_failed",
                                          "error": str(e)[:200], "dry_run": dry_run})
            log.error("ingest: %s", e)
            return 1
        for name, s in stats.items():
            if s["error"] and any(c in str(s["error"]) for c in _COOLDOWN_CODES):
                until = pool.set_cooldown(name, _COOLDOWN_HOURS)
                log.warning("源 %s 反爬命中，冷却至 %s", name, until)
        news = [it for it in items if it.get("type") == "news"]
        trending = [it for it in items if it.get("type") == "trending"]
        slim_stats = {n: {k: v for k, v in s.items() if k != "detail"}
                      for n, s in stats.items()}
        if dry_run:
            # 决策 D10：完整跑抓取/查重计算链路，唯一区别是不落 pool_items；
            # run_log 照写（口径同 daily dry-run 的 dry_run_token_ok）
            store.log("ingest", "ok", {"detail": "dry_run", "news": len(news),
                                       "trending": len(trending),
                                       "source_stats": slim_stats,
                                       "cooldowns": cooling,
                                       "elapsed_s": round(time.monotonic() - t0, 1)})
            log.info("ingest dry-run: news=%d trending=%d（未入池）",
                     len(news), len(trending))
            return 0
        # history 对齐：池子冷启动时库里没有已发文章的痕迹，不对齐会「换 URL
        # 复活近 14 天已发文章」；_dedup_history 与老路径同源
        r_news = pool.upsert_news(news, history=_dedup_history(store, date.today().isoformat()))
        r_trend = pool.upsert_trending(trending)
        swept = pool.sweep()
        pool_stats = pool.stats()
        degraded = any(s["error"] for s in stats.values())
        store.log("ingest", "degraded" if degraded else "ok", {
            "source_stats": slim_stats,
            "news": len(news), "trending": len(trending),
            "pool_news": {k: v for k, v in r_news.items() if k != "dup_samples"},
            "dup_samples": r_news["dup_samples"],
            "pool_trending": r_trend, "sweep": swept, "pool": pool_stats,
            "cooldowns": pool.cooldowns(),
            "elapsed_s": round(time.monotonic() - t0, 1),
        })
        log.info("ingest: news +%d/%d trending +%d/%d pool=%s",
                 r_news["inserted"], len(news), r_trend["inserted"],
                 len(trending), pool_stats)
        return 0
    finally:
        pool.close()
        store.close()


def _pool_daily_materials() -> tuple[list[dict], list[dict]] | None:
    """daily 的池模式取材（D6）：(粗排候选, 最近 trending 快照)。

    空池返回 None、池故障打日志后返回 None——调用方即时抓取候选，
    然后进入相同的研究文章 pipeline 与最终检查。
    """
    ps = None
    try:
        ps = PoolStore()
        cands = ps.top_candidates(limit=POOL_CANDIDATE_LIMIT)
        trending = ps.latest_trending(limit=settings.top_n)
        if not cands:
            log.info("池子无可用候选（空池/新库），回退即时抓取")
            return None
        log.info("池模式取材: 候选 %d / trending 快照 %d", len(cands), len(trending))
        return cands, trending
    except Exception as e:  # noqa: BLE001 池故障不挡发帖
        log.warning("池子取材失败（回退即时抓取）: %s", e)
        return None
    finally:
        if ps is not None:
            ps.close()


def _pool_star_trending() -> list[dict]:
    """star 的池模式取材（D7）：trending 池「最新一班」top15。空/异常 → []。"""
    ps = None
    try:
        ps = PoolStore()
        rows = ps.latest_trending(limit=settings.top_n)
        if rows:
            log.info("池模式取材: trending %d（最近一班）", len(rows))
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("池子取 trending 失败（回退即时抓取）: %s", e)
        return []
    finally:
        if ps is not None:
            ps.close()


def _mark_pool_consumed(item: dict, day: str) -> None:
    """发帖成功后把消费的候选标 daily_used（D6：未发帖/周末/dry-run 不算消费）。

    回退路径发布的文章也要对齐池子状态（它可能同时在池里挂着 candidate，
    不标会在池模式下隔天重复发布）。观测手段，失败只记日志。
    """
    target = item.get("_pool_url") or normalize_url(item.get("url") or "")
    if not target:
        return
    try:
        ps = PoolStore()
        try:
            n = ps.mark_consumed([target], day)
        finally:
            ps.close()
        if n:
            log.info("池: 候选已标 daily_used（digest_date=%s）", day)
    except Exception as e:  # noqa: BLE001
        log.warning("池消费标记失败（不影响发帖）: %s", e)


# ---------------- daily（research-v1：单篇研究学习文章） ----------------

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
    """原文抓取公共阶梯（实现已提升到 extract.fetch_with_mirror，M2 起单一实现）。"""
    return extract.fetch_with_mirror(item)


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


def _verify_published(store: Store, task: str, post_id: int | None, title: str) -> int | None:
    """发帖后只读回读：确认帖子真的公开可见（2026-09-20 星榜帖 #6311 被写私密事故）。

    观测动作，绝不影响发帖结果：发帖此时已成功、幂等标记已写，定位不到/库不可用/
    任何异常都只记日志，绝不因此重发。postID 未返回（后端 data=nil）时按
    「本人 + 标题 + 10 分钟时间窗」从库里捞回（marketdb.find_post_id 同款口径）。
    返回有效 postID；可见性未知、私密或校验异常不丢弃已确认的帖子身份。
    """
    try:
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            post_id = None
            uid = marketdb.user_id_of(settings.market_user_telephone or "")
            since = datetime.now() - timedelta(minutes=10)
            post_id = marketdb.find_post_id(uid, title, since) if uid else None
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            post_id = None
            store.log(task, "degraded", {"detail": "publish_verify",
                                         "reason": "post_not_located"})
            log.error("回读校验：%s 帖无法定位（postID 未返回且库中查不到），公开性未知", task)
            return None
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
    return post_id


def _publish_md(store: Store, task: str, day: str, title: str, md: str,
                dry_run: bool, private: bool = False, content_meta: dict | None = None) -> bool:
    """只有本次新发布获成功确认才返回 True；跳过、预览及失败均为 False。"""
    if task == "daily" and not _publish_allowed(day):
        store.log(task, "skipped", {"detail": "weekend", "day": day})
        log.info("周末只抓取入库、不发帖（数据仍进周报池）：%s 跳过发帖", day)
        return False
    if not settings.daily_publish:
        store.log(task, "degraded",
                  {"detail": "publish", "reason": "TECH_DIGEST_DAILY_PUBLISH=0"})
        log.info("发帖已关闭（TECH_DIGEST_DAILY_PUBLISH=0），仅落盘")
        return False
    if not settings.market_refresh_token or not settings.market_user_telephone:
        store.log(task, "degraded",
                  {"detail": "publish", "reason": "未配置 MARKET_REFRESH_TOKEN/TELEPHONE"})
        log.warning("未配置发帖凭据，仅落盘")
        return False
    posted = store.is_daily_posted(day) if task == "daily" else store.is_star_posted(day)
    if posted:
        store.log(task, "skipped", {"detail": "publish_dup"})
        log.info("今日已发布过（meta 标记），--force 重跑不重发")
        return False
    from app import publisher  # 延迟导入：所有跳过分支均无需发帖模块
    if dry_run:
        auth = publisher.refresh_access_token()
        if auth:
            log.info("dry-run: access token 校验通过（未发帖）")
            store.log(task, "ok", {"detail": "dry_run_token_ok"})
        else:
            store.log(task, "degraded", {"detail": "publish", "reason": "dry_run_token_fail"})
            log.error("dry-run: token 校验失败")
        return False
    for attempt in (1, 2):
        post_id, url, err = publisher.publish_md(title, md, private=private)
        if err is None:
            if task == "daily":
                store.mark_daily_posted(day, post_id or 0)
                published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                store.set_daily_title(day, title)
            else:
                store.mark_star_posted(day, post_id or 0)
            detail = {"detail": "published", "post_id": post_id,
                      "post_url": url, "title": title}
            if task == "daily":
                detail.update(published_at=published_at,
                              time_basis="local_confirmation")
                if content_meta is not None:
                    detail.update(content_version="research-v1",
                                  article_type=content_meta.get("article_type"),
                                  learning_question=content_meta.get("learning_question"),
                                  title_hash=sha256(title.encode("utf-8")).hexdigest(),
                                  body_hash=sha256(md.encode("utf-8")).hexdigest(),
                                  word_count=len(md),  # 契约口径：最终 Markdown 字符数
                                  evidence_count=content_meta.get("evidence_count", 0))
                    for key in ("source_hash", "draft_hash"):
                        if content_meta.get(key):
                            detail[key] = content_meta[key]
            if task != "daily":
                store.log(task, "ok", detail)
            log.info("%s 已发布: postID=%s %s", task, post_id or "(未返回)", url or "")
            if not private:  # 有意发私密（预览）不用回读
                resolved_id = _verify_published(store, task, post_id, title)
                if task == "daily" and not post_id and isinstance(resolved_id, int) \
                        and not isinstance(resolved_id, bool) and resolved_id > 0:
                    detail["post_id"] = resolved_id
                    store.mark_daily_posted(day, resolved_id)
            if task == "daily":
                # 原确认时间及内容哈希在回读前捕获，成功日志仅写一次。
                store.log(task, "ok", detail)
            return True
        log.warning("%s 发帖第 %d 次失败: %s", task, attempt, err)
        if attempt == 2:
            store.log(task, "degraded", {"detail": "publish", "error": err})
    return False


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


def _daily_publication_decision(result) -> dict:
    """生产者 ready 与独立验证的 ready 必须同时满足。"""
    packet = result if isinstance(result, dict) else {}
    try:
        decision = publication_gate.decide_publication(
            packet.get("title"), packet.get("markdown"), packet.get("draft"),
            packet.get("source_packet"), packet.get("review"))
    except Exception as exc:
        return {"status": "generation_error", "reasons": ["publication_gate_error", str(exc)[:200]]}
    reasons = packet.get("reasons")
    reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
    if not isinstance(decision, dict) or decision.get("status") not in ("ready", "skip_quality", "generation_error"):
        return {"status": "generation_error", "reasons": reasons + ["invalid_gate_result"]}
    reasons += decision.get("reasons", [])
    producer_status = packet.get("status")
    if producer_status not in ("ready", "skip_quality", "generation_error"):
        return {"status": "generation_error", "reasons": reasons + ["invalid_pipeline_result"]}
    if producer_status != "ready":
        return {"status": producer_status, "reasons": reasons}
    if any(not isinstance(packet.get(k), dict) for k in ("draft", "source_packet", "item")) \
            or any(not isinstance(packet.get(k), str) or not packet[k].strip() for k in ("title", "markdown")):
        return {"status": "generation_error", "reasons": reasons + ["malformed_ready_result"]}
    return {**decision, "reasons": reasons}


def _save_daily_draft(day: str, result, decision: dict):
    """拒稿仅保存在独立草稿目录，不占用正式日期快照或期数。"""
    drafts = settings.output_dir / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = drafts / f"{day}-{stamp}-{uuid4().hex}.json"
    record = {"day": day, "status": decision["status"], "reasons": decision.get("reasons", []), "result": result}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8", newline="")
    return path


def run_daily(force: bool, dry_run: bool = False, private: bool = False) -> int:
    store = Store()
    try:
        day = date.today().isoformat()
        # 未发布快照允许重试；已发布时 force 也不能取新候选或标新消费。
        if store.is_daily_posted(day):
            store.log("daily", "skipped", {"detail": "publish_dup", "day": day})
            return 0
        skip, reason = should_skip(settings.load_threshold)
        if skip and settings.check_load:
            store.log("daily", "skipped", {"detail": "load", "reason": reason})
            return 0
        started = time.monotonic()
        news, trending, stats = [], [], {}
        dedup_info = {"removed": []}
        pool_mode = False
        try:
            material = _pool_daily_materials()
            pool_mode = material is not None
            if material is not None:
                news, trending = material
            else:
                items, stats = fetch_all()
                trending = [it for it in items if it["type"] == "trending"]
                raw_news = [it for it in items if it["type"] == "news"]
                news, dedup_info = dedup.dedupe_news(raw_news, _dedup_history(store, day))
            pref = preference.load_preference()
            recent = store.recent_daily_titles(7, before_day=day)
            candidates = news if pool_mode else _daily_candidates(news)
            result = editorial.run_pipeline(candidates, preference=pref, dry_run=dry_run, recent_titles=recent)
        except Exception as exc:  # 原文、模型、生成失败均不允许旧内容路径补位
            result = {"status": "generation_error", "reasons": ["pipeline_exception", str(exc)[:200]],
                      "draft": None, "source_packet": None, "review": None, "attempts": []}
        decision = _daily_publication_decision(result)
        if decision["status"] != "ready":
            try:
                draft_path = _save_daily_draft(day, result, decision)
            except OSError as exc:
                store.log("daily", "error", {"detail": "generation_error", "reasons": ["draft_write_failed"],
                                             "error": str(exc)[:200]})
                return 1
            status = decision["status"]
            store.log("daily", "error" if status == "generation_error" else "skipped", {
                "detail": status, "reasons": decision.get("reasons", []), "draft_path": str(draft_path),
                "material": "pool" if pool_mode else "live",
                "attempts": result.get("attempts", []) if isinstance(result, dict) else [],
            })
            log.warning("daily %s: %s", status, decision.get("reasons", []))
            return 1 if status == "generation_error" else 0

        # 独立检查通过后才允许正式文件、快照、期数、清理和发布。
        # 不重选标题、不重渲染、不添加期数或其他正文：审核字符串原样提交。
        title, md = result["title"], result["markdown"]
        draft, source, review, item = (result[k] for k in ("draft", "source_packet", "review", "item"))
        issue_no = store.get_issue_no(day) or store.next_issue_no()
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        out = settings.output_dir / f"{day}-daily.md"
        out.write_text(md, encoding="utf-8", newline="")
        if not dry_run:
            issue_no = store.save_daily_v2(day, news + trending, str(out), post_title=title)
        store.cleanup()
        src_errors = {name: data["error"] for name, data in stats.items() if data.get("error")}
        store.log("daily", "degraded" if src_errors else "ok", {
            "material": "pool" if pool_mode else "live", "pipeline": "editorial",
            "content_version": "research-v1", "article_type": draft["article_type"],
            "learning_question": draft["learning_question"], "review_score": decision.get("score"),
            "source_hash": source["source_hash"], "draft_hash": review["draft_hash"],
            "source_stats": {name: {k: v for k, v in data.items() if k != "detail"} for name, data in stats.items()},
            "dedup_removed": len(dedup_info["removed"]), "dedup_detail": dedup_info["removed"][:5],
            "news": len(news), "trending": len(trending), "issue_no": issue_no,
            "title": title, "dry_run": dry_run, "attempts": result.get("attempts", []),
            "elapsed_s": round(time.monotonic() - started, 1), **({"errors": src_errors} if src_errors else {}),
        })
        published = _publish_md(store, "daily", day, title, md, dry_run, private,
                                content_meta={"article_type": draft["article_type"],
                                              "learning_question": draft["learning_question"],
                                              "evidence_count": len(draft["evidence_ids"]),
                                              "source_hash": source["source_hash"],
                                              "draft_hash": review["draft_hash"]})
        if published is True and not dry_run:
            _mark_pool_consumed(item, day)
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
        # 池模式（D7）：trending 池「最新一班」；空/池故障回退即时抓取
        trending = _pool_star_trending()
        if trending:
            stats = {"github-trending": {"source": "pool(最近一班)",
                                          "fetched": len(trending), "error": None,
                                          "elapsed_s": round(time.monotonic() - t0, 1)}}
        else:
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
    ap.add_argument("task", choices=["ingest", "daily", "star", "weekly"])
    ap.add_argument("--force", action="store_true", help="重复触发也重新执行")
    ap.add_argument("--dry-run", action="store_true",
                    help="渲染落盘但不发帖、不写库（ingest: 不落池）；token 校验到为止")
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
    if args.task == "ingest":
        rc = run_ingest(args.dry_run)
    elif args.task == "daily":
        rc = run_daily(args.force, args.dry_run, args.private)
    elif args.task == "star":
        rc = run_star(args.force, args.dry_run, args.private)
    else:
        rc = run_weekly(args.force, args.dry_run, args.private)
    log.info("=== done %s in %.1fs rc=%s ===", args.task, time.monotonic() - t0, rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
