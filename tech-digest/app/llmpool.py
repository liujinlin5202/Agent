# -*- coding: utf-8 -*-
"""LLM 调用池（编辑部 M2）：并行原语 + 串行默认 + 指数退避。

契约（上位计划 §5.1 / M2 决策留底 D1）：
  · 所有编辑流水线的 LLM 调用经 map_chat 下发——架构上是并行 fan-out，
    实际并发度 = TECH_DIGEST_LLM_CONCURRENCY（默认 1，严格串行）；
    换算力改 env，代码零改动。
  · 退避：主通道单条失败 1/4/16s+抖动 重试 ×3，尽才落 DeepSeek 备通道，
    再尽 → None（调用方按「该步降级」处理）。llm.py 的单次语义不动。
线程池而非 asyncio：llm.chat 是同步 requests，ThreadPool 是其一等并行原语，
与全仓零依赖风格一致。
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence

from app.config import settings
from app.llm import chat

log = logging.getLogger("tech-digest.llmpool")

BACKOFF_STEPS = (1, 4, 16)   # 秒；主通道重试间退避（上位计划 §5.5 契约）


def chat_with_backoff(prompt: str, system: str | None = None,
                      max_tokens: int = 4096, timeout: int = 120,
                      attempts: int = 3,
                      sleeper: Callable[[float], None] = time.sleep) -> str | None:
    """单条调用：主通道退避重试 ×attempts → 备通道（各一次）→ None。

    sleeper 可注入（测试用假时钟）。注意与 llm.chat 的区别只在「主通道
    不再一触即走」：退避烧尽才切备通道，避免把网关抖动直接变成备通道流量。
    """
    for i in range(attempts):
        content = chat(prompt, system=system, max_tokens=max_tokens, timeout=timeout)
        if content:
            return content
        if i < attempts - 1:
            wait = BACKOFF_STEPS[min(i, len(BACKOFF_STEPS) - 1)]
            wait += random.uniform(0, wait * 0.25)   # 抖动，防同班齐发
            log.warning("LLM 主通道失败（第 %d 次），%.1fs 后重试", i + 1, wait)
            sleeper(wait)
    return None


def map_chat(tasks: Sequence[dict], concurrency: int | None = None,
             sleeper: Callable[[float], None] = time.sleep) -> list[str | None]:
    """把一批 chat 任务按并发度执行，返回与 tasks 等长的结果列表（可为 None）。

    tasks: [{prompt, system?, max_tokens?, timeout?}]
    单任务异常隔离：失败按 None 收敛，不炸整池（评委缺席≠停刊）。
    """
    conc = concurrency if concurrency is not None else max(1, settings.llm_concurrency)
    if not tasks:
        return []
    if conc == 1:
        # 串行快捷路径：不引入线程，时间戳严格有序（验收「伪并行=1」的证据面）
        return [chat_with_backoff(sleeper=sleeper, **t) for t in tasks]
    results: list[str | None] = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(chat_with_backoff, sleeper=sleeper, **t): i
                for i, t in enumerate(tasks)}
        for fut, i in futs.items():
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001 单任务异常隔离
                log.warning("LLM 任务 %d 异常（按 None 收敛）: %s", i, e)
    return results
