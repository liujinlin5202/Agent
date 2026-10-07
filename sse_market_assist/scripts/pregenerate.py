#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量老帖批量预生成 AI 回答（2026-09-22 上线后补量）。

设计要点：
- 只处理 is_private=0 且无有效留底（assist_responses.answer 非空）的帖子，最新帖优先；
- 直接调 app.api._match_impl——与线上 /match 同一条路径，天然带防重守卫、
  RAG→本地检索降级、WebSearch 补充；answer 为空的历史行（refs 残留/失败）会被重试；
- 限速：两篇之间强制 sleep --interval 秒；连续失败 --fail-exit 篇即退出，防网关雪崩；
- 可随时 kill，重启自动续跑（已生成自动跳过）；/tmp/assist_pregenerate.lock 防双开。

用法（pod 内 /home/cloud/sse_market_assist）：
  验证跑 2 篇：.venv/bin/python scripts/pregenerate.py --once --limit 2 --interval 3
  常驻补量：  setsid nohup .venv/bin/python scripts/pregenerate.py \
              >> logs/pregenerate.log 2>&1 < /dev/null &
  看进度：    tail logs/pregenerate.log
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import _match_impl  # noqa: E402
from app.db import fetch_all  # noqa: E402

LOCK = "/tmp/assist_pregenerate.lock"

# 无有效留底的公开帖（最新优先）；answer 为空的行视为未生成
PENDING_SQL = """
SELECT p.postID, p.`partition`, p.title, p.ptext
FROM posts p
LEFT JOIN assist_responses a
       ON a.postID = p.postID AND TRIM(COALESCE(a.answer, '')) <> ''
WHERE a.postID IS NULL
  AND p.is_private = 0
  AND COALESCE(p.title, '') <> ''
ORDER BY p.postID DESC
LIMIT %s
"""

COUNT_SQL = """
SELECT COUNT(*) AS n
FROM posts p
LEFT JOIN assist_responses a
       ON a.postID = p.postID AND TRIM(COALESCE(a.answer, '')) <> ''
WHERE a.postID IS NULL AND p.is_private = 0
"""


def remaining() -> int:
    rows = fetch_all(COUNT_SQL)
    return int(rows[0]["n"]) if rows else 0


def already_running() -> bool:
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK).read().strip() or "0")
        except Exception:  # noqa: BLE001
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
                return True  # 上一实例仍在跑
            except OSError:
                pass  # 进程已死，清锁接管
        try:
            os.remove(LOCK)
        except OSError:
            pass
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="存量帖 AI 回答批量预生成（限速）")
    ap.add_argument("--once", action="store_true", help="跑完当前一批即退出（默认循环到清空）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多生成篇数（0=不限）")
    ap.add_argument("--batch", type=int, default=10, help="每批候选数")
    ap.add_argument("--interval", type=float, default=75.0, help="两篇之间的间隔秒数（默认 75）")
    ap.add_argument("--fail-exit", type=int, default=5, help="连续失败 N 篇即退出（默认 5）")
    args = ap.parse_args()

    if already_running():
        print("[pregen] 已有实例在跑（/tmp/assist_pregenerate.lock），本次退出", flush=True)
        return

    print(f"[pregen] 启动 pid={os.getpid()}：待生成约 {remaining()} 篇；"
          f"interval={args.interval}s batch={args.batch} once={args.once} limit={args.limit or '不限'}",
          flush=True)

    done = fails = 0
    try:
        while True:
            rows = fetch_all(PENDING_SQL, (args.batch,))
            if not rows:
                print("[pregen] 队列已清空，全部完成", flush=True)
                break
            for r in rows:
                if args.limit and done >= args.limit:
                    break
                t0 = time.time()
                try:
                    resp = _match_impl(r["partition"], r["title"],
                                       (r.get("ptext") or "")[:1000], int(r["postID"]), None)
                    meta = (resp.get("data") or {}).get("meta") or {}
                    ans = (resp.get("data") or {}).get("answer") or ""
                    ok = resp.get("ok") is True
                    print(f"[pregen] {r['postID']} ok={ok} {time.time() - t0:.1f}s "
                          f"used={meta.get('used')} sources={meta.get('sources_type')} "
                          f"cached={meta.get('cached')} ans={len(ans)}字 :: {(r['title'] or '')[:24]}",
                          flush=True)
                    fails = 0 if ok else fails + 1
                    done += 1
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    print(f"[pregen] {r['postID']} 异常: {repr(e)}", flush=True)
                if args.fail_exit and fails >= args.fail_exit:
                    print(f"[pregen] 连续失败 {fails} 篇，退出防雪崩（重启可续）", flush=True)
                    return
                time.sleep(args.interval)
            if args.once or (args.limit and done >= args.limit):
                break
        print(f"[pregen] 结束：本次生成 {done} 篇；剩余待生成约 {remaining()} 篇", flush=True)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    main()
