# -*- coding: utf-8 -*-
"""手动索引入口：
  .venv/bin/python scripts/run_sync.py --fresh        # 清空新 collection 后全量重建（消耗全部 embedding 额度，慎用）
  .venv/bin/python scripts/run_sync.py --full         # 全量同步（已入库点自动跳过，可断点续传）
  .venv/bin/python scripts/run_sync.py --incremental  # 按时间水位增量
  .venv/bin/python scripts/run_sync.py --cleanup      # 删除 is_private=1 的帖子点
  .venv/bin/python scripts/run_sync.py --watch        # 常驻：周期增量同步 = 发帖/评论自动入索引（独立进程，不阻塞发帖）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.qdrant_store import ensure_collection, recreate_collection
from app.sync import STATE_FILE, cleanup_private, full_sync, incremental_sync, watch_sync


def main() -> None:
    parser = argparse.ArgumentParser(description="market_assist_v1 索引同步")
    parser.add_argument("--fresh", action="store_true", help="清空新 collection 后全量重建")
    parser.add_argument("--full", action="store_true", help="全量同步（跳过已入库点）")
    parser.add_argument("--incremental", action="store_true", help="增量同步（按时间水位）")
    parser.add_argument("--cleanup", action="store_true", help="删除 is_private=1 的帖子点")
    parser.add_argument("--watch", action="store_true", help="常驻监听：周期增量同步（发帖/评论自动入索引，不阻塞发帖）")
    args = parser.parse_args()

    if args.fresh:
        recreate_collection()
        STATE_FILE.unlink(missing_ok=True)  # 重置水位，让全量重新快照（重建索引场景）
        full_sync()
    elif args.full:
        ensure_collection()
        full_sync()
    elif args.incremental:
        ensure_collection()
        incremental_sync()
    elif args.cleanup:
        cleanup_private()
    elif args.watch:
        ensure_collection()
        watch_sync()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
