# -*- coding: utf-8 -*-
"""LLM 调用池（编辑部 M2）：并行原语 + 串行默认 + 指数退避。

契约（上位计划 §5.1 / M2 决策留底 D1 / 2026-10-13 评审缺口③④）：
  · 所有编辑流水线的 LLM 调用经 map_chat 下发——架构上是并行 fan-out，
    实际并发度 = TECH_DIGEST_LLM_CONCURRENCY（默认 1，严格串行）；
    换算力改 env，代码零改动。
  · 退避（缺口③修正后的真语义）：主通道重试 ×attempts（间隔 1/4/16s+抖动），
    重试期间绝不碰备通道；退避烧尽才落 DeepSeek 备通道一次，再尽 → None
    （调用方按「该步降级」处理）。llm.py 的单次语义不动，channel 参数区分通道。
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

BACKOFF_STEPS = (1, 4, 16)     # 秒；主通道重试间退避（上位计划 §5.5 契约）
BACKOFF_RETRY_ATTEMPTS = 4     # 首调 + 重试×3 → 恰好用满 1/4/16 三档退避


def chat_with_backoff(prompt: str, system: str | None = None,
                      max_tokens: int = 4096, timeout: int = 120,
                      attempts: int = BACKOFF_RETRY_ATTEMPTS,
                      sleeper: Callable[[float], None] = time.sleep) -> str | None:
    """单条调用：主通道退避重试 ×attempts（间隔 1/4/16s+抖动）→ 备通道一次 → None。

    sleeper 可注入（测试用假时钟）。缺口③修正：旧实现委托 llm.chat（auto），
    每次重试都先主后备、备通道被无谓 hammer 且 16s 档永远走不到；现在重试期
    固定 channel="primary"，备通道只在主通道退避烧尽后调用一次。
    """
    for i in range(max(1, attempts)):
        content = chat(prompt, system=system, max_tokens=max_tokens,
                       timeout=timeout, channel="primary")
        if content:
            return content
        if i < attempts - 1:
            wait = BACKOFF_STEPS[min(i, len(BACKOFF_STEPS) - 1)]
            wait += random.uniform(0, wait * 0.25)   # 抖动，防同班齐发
            log.warning("LLM 主通道失败（第 %d 次），%.1fs 后重试", i + 1, wait)
            sleeper(wait)
    content = chat(prompt, system=system, max_tokens=max_tokens,
                   timeout=timeout, channel="backup")
    if content:
        log.info("LLM 备用通道生效（主通道已重试 %d 次）", max(1, attempts))
        return content
    return None


def map_chat(tasks: Sequence[dict], concurrency: int | None = None,
             sleeper: Callable[[float], None] = time.sleep) -> list[str | None]:
    """把一批 chat 任务按并发度执行，返回与 tasks 等长的结果列表（可为 None）。

    tasks: [{prompt, system?, max_tokens?, timeout?}]
    单任务异常隔离（缺口④）：串行与并发两条路径一致——失败按 None 收敛，
    不炸整池（评委缺席≠停刊）。
    """
    conc = concurrency if concurrency is not None else max(1, settings.llm_concurrency)
    if not tasks:
        return []
    if conc == 1:
        # 串行快捷路径：不引入线程，时间戳严格有序（验收「伪并行=1」的证据面）
        results: list[str | None] = []
        for i, t in enumerate(tasks):
            try:
                results.append(chat_with_backoff(sleeper=sleeper, **t))
            except Exception as e:  # noqa: BLE001 单任务异常隔离（与并发路径同规）
                log.warning("LLM 任务 %d 异常（按 None 收敛）: %s", i, e)
                results.append(None)
        return results
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
