# -*- coding: utf-8 -*-
"""LLM 单通道客户端：秋坞网关（OpenAI 兼容 /chat/completions，qwen-3.8-flash）。

- reasoning_effort=none 实测有效关思考（qwen-3.8-flash 通道，省 token 省时延）
- trust_env=False：生产 pod 无代理，开发机 Windows 有系统代理（Clash 类会把
  127.0.0.1/内网请求也劫走）——一律直连
- 失败退避 1/4/16s+抖动 重试 ×3，尽 → None（与 tech-digest llmpool 同契约）
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable

import requests

log = logging.getLogger("post-curator.llm")

BACKOFF_STEPS = (1, 4, 16)


def chat(prompt: str, system: str | None, api_key: str, base_url: str,
         model: str, max_tokens: int = 512, timeout: int = 120,
         temperature: float = 0.2) -> str | None:
    """单次调用：非 200 / 网络异常 / 空响应 一律 None，不抛。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
    }
    url = base_url.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
            trust_env=False,
        )
        if resp.status_code != 200:
            log.warning("LLM HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content")
        return content or None
    except (requests.RequestException, ValueError) as e:
        log.warning("LLM 调用失败: %s", e)
        return None


def chat_with_backoff(prompt: str, system: str | None, api_key: str, base_url: str,
                      model: str, max_tokens: int = 512, timeout: int = 120,
                      attempts: int = 3,
                      sleeper: Callable[[float], None] = time.sleep) -> str | None:
    """退避重试封装：主通道烧尽即 None（无备通道，见 config 裁决注记）。"""
    for i in range(attempts):
        content = chat(prompt, system, api_key, base_url, model,
                       max_tokens=max_tokens, timeout=timeout)
        if content:
            return content
        if i < attempts - 1:
            wait = BACKOFF_STEPS[min(i, len(BACKOFF_STEPS) - 1)]
            wait += random.uniform(0, wait * 0.25)
            log.warning("LLM 失败（第 %d 次），%.1fs 后重试", i + 1, wait)
            sleeper(wait)
    return None
