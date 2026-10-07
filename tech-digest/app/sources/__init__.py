# -*- coding: utf-8 -*-
"""v2.0 多源采集层：每源独立 fail-open，输出统一 v2 条目（见技术文档 §2.1）。

条目模型（items_json 每项）：
    type: trending | news
    title / url / source / author / published_at(原文时间|None) / fetched_at
    summary(原文摘要|"") / ai_summary(None) / confidence(None)
    trending: {rank, language, stars, today_stars}  # 仅 trending
旧数据兼容：读取时 it.get("type", "trending")。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Sequence

from app.config import settings

log = logging.getLogger("tech-digest.sources")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_item(*, type_: str, title: str, url: str, source: str, author: str = "",
             published_at: str | None = None, summary: str = "",
             trending: dict | None = None) -> dict:
    return {
        "type": type_, "title": title, "url": url, "source": source,
        "author": author, "published_at": published_at, "fetched_at": now_iso(),
        "summary": summary, "ai_summary": None, "confidence": None,
        "trending": trending,
    }


# 每个源一个 fetch() -> (items, source_label, detail)；异常向上抛由调度层捕获
FetchFn = Callable[[], tuple[list[dict], str, dict]]


def fetch_all(source_names: Sequence[str] | None = None) -> tuple[list[dict], dict]:
    """抓取所有启用的源。返回 (items, stats)。

    stats: {source: {"source": 实际源标签, "fetched": n, "error": str|None, "elapsed_s": f}}
    单源失败：该源 error 非 None + fetched=0，整体继续；
    全部源失败：抛 RuntimeError（调用方按 error 处理，不产出空报）。
    """
    from app.sources import (devto, feeds, github, hacker_news, sspai,  # 延迟：注册表避免循环
                             wscn, zhihu_hot)

    registry: dict[str, FetchFn] = {
        "github-trending": github.fetch,
        "hacker-news": hacker_news.fetch,
        "sspai": sspai.fetch,
        "zhihu-hot": zhihu_hot.fetch,
        "devto": devto.fetch,
        "wallstreetcn": wscn.fetch,
        "ars-technica": feeds.fetch_ars,
        "ieee-spectrum": feeds.fetch_ieee,
        "freecodecamp": feeds.fetch_fcc,
        "ruanyifeng": feeds.fetch_ruanyifeng,
    }
    names = list(source_names or settings.tech_digest_sources)
    log.info("sources: %s", ",".join(names))
    items: list[dict] = []
    stats: dict = {}
    for name in names:
        fn = registry.get(name)
        if fn is None:
            stats[name] = {"source": name, "fetched": 0,
                           "error": f"未知源（可用: {','.join(registry)}）", "elapsed_s": 0}
            continue
        t0 = time.monotonic()
        try:
            src_items, src_label, detail = fn()
            stats[name] = {"source": src_label, "fetched": len(src_items),
                           "error": None, "elapsed_s": round(time.monotonic() - t0, 1),
                           "detail": detail}
            items.extend(src_items)
            log.info("source %s: %d 条 (%s) %.1fs", name, len(src_items), src_label,
                     stats[name]["elapsed_s"])
        except Exception as e:  # noqa: BLE001 单源失败降级
            err = str(e)[:200]
            stats[name] = {"source": name, "fetched": 0, "error": err,
                           "elapsed_s": round(time.monotonic() - t0, 1)}
            log.warning("source %s 失败: %s", name, err)
    if items:
        return items, stats
    raise RuntimeError(f"全部源失败: { {k: v['error'] for k, v in stats.items()} }")
