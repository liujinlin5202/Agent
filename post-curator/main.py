# -*- coding: utf-8 -*-
"""post-curator 入口（一次性进程，由秋坞 CronJob 触发）。

用法:
  python main.py scan                 # 每日班：近 3 天未加精帖 → LLM 评审 → 自动加精
  python main.py scan --dry-run       # 全链评审落审计，但不调写通道（门禁/演练用）
  python main.py scan --force         # 跳过评审冷却，窗口内全部重评
  python main.py scan --full          # 全量模式：全部未加精帖按互动信号预排 top N 评审
  python main.py scan --limit 5       # 只评前 5 帖（调试）

退出码: 0 成功 / 2 数据库不可用 / 3 LLM 全员失败 / 4 加精写通道失败。
失败由 10:45 qdock_cron_alert 邮件兜底（无备通道是既定裁决）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from app import forumdb, judge, state, writer
from app.config import load_settings
from app.llm import chat_with_backoff

log = logging.getLogger("post-curator")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def run(full: bool, dry_run: bool, force: bool, limit: int | None) -> int:
    cfg = load_settings()
    mode = "full" if full else "daily"

    try:
        if full:
            candidates = forumdb.fetch_all_unfeatured(cfg, cfg.full_top_n)
        else:
            candidates = forumdb.fetch_recent_unfeatured(cfg, cfg.lookback_days)
    except Exception as e:  # noqa: BLE001 数据库不可用=整班失败
        log.error("集市库不可用: %s", e)
        return 2

    cooling = set() if force else state.recently_judged(cfg.data_dir, cfg.rejudge_hours)
    eligible = [p for p in candidates if p["post_id"] not in cooling]
    todo = eligible[:limit] if limit and limit > 0 else eligible
    todo = todo[:cfg.max_judge_per_run]
    log.info("mode=%s 候选 %d，冷却跳过 %d，上限截断 %d，本轮评审 %d",
             mode, len(candidates), len(candidates) - len(eligible),
             len(eligible) - len(todo), len(todo))

    rows: list[dict] = []
    featured_ids: list[int] = []
    llm_fail = 0
    for p in todo:
        try:
            comments = forumdb.fetch_comments(cfg, p["post_id"], cfg.comments_per_post)
        except Exception as e:  # noqa: BLE001 单帖评论失败不炸整班
            log.warning("帖子 %s 评论读取失败，按无评论评审: %s", p["post_id"], e)
            comments = []
        prompt = judge.build_user_prompt(p, comments, cfg.post_char_cap, cfg.comment_char_cap)
        raw = chat_with_backoff(
            prompt, judge.SYSTEM_PROMPT, cfg.llm_api_key, cfg.llm_base_url,
            cfg.llm_model, max_tokens=cfg.llm_max_tokens, timeout=cfg.llm_timeout)
        verdict = judge.parse_verdict(raw)
        if verdict is None and raw is None:
            llm_fail += 1
        featured, reason, normalized = judge.gate(
            verdict, cfg.content_threshold, cfg.comment_threshold)
        log.info("帖 %s《%s》→ %s", p["post_id"], p["title"][:24], reason)
        rows.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "post_id": p["post_id"],
            "title": p["title"][:100],
            "stats": {k: p[k] for k in ("browse_num", "like_num", "dislike_num", "comment_num")},
            "comment_samples": len(comments),
            **normalized,
            "featured": featured,
            "reason": reason,
        })
        if featured:
            featured_ids.append(p["post_id"])

    state.append_decisions(cfg.data_dir, rows)

    if llm_fail and llm_fail == len(todo) and todo:
        log.error("LLM 通道全员失败（%d/%d），评审无效", llm_fail, len(todo))
        return 3

    if not featured_ids:
        log.info("本轮无可加精帖")
    elif dry_run:
        log.info("dry-run：跳过写通道，候选加精 %d 篇 %s", len(featured_ids), featured_ids)
    else:
        ok, msg = writer.mark_featured(
            featured_ids, cfg.internal_base_url, cfg.internal_token, cfg.http_timeout)
        for row in rows:
            if row["post_id"] in featured_ids:
                row["marked"] = ok
        if not ok:
            log.error("加精写通道失败: %s", msg)
            return 4
        log.info("写通道: %s", msg)

    judged = len(rows)
    log.info("post-curator summary: mode=%s candidates=%d judged=%d featured=%d marked=%s",
             mode, len(candidates), judged, len(featured_ids),
             "skip(dry-run)" if dry_run and featured_ids else
             (len(featured_ids) if not dry_run else 0))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="集市精华帖自动策展")
    parser.add_argument("command", nargs="?", default="scan", choices=["scan"],
                        help="scan=按窗口/全量评审（唯一命令）")
    parser.add_argument("--full", action="store_true", help="全量模式（手动触发）")
    parser.add_argument("--dry-run", action="store_true", help="评审+审计落盘，不调写通道")
    parser.add_argument("--force", action="store_true", help="跳过评审冷却")
    parser.add_argument("--limit", type=int, default=None, help="只评前 N 帖（调试）")
    args = parser.parse_args()
    _setup_logging()
    return run(full=args.full, dry_run=args.dry_run, force=args.force, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
