# -*- coding: utf-8 -*-
"""LLM 客户端：主通道 market-deploy（OpenAI 兼容）+ 备用通道 DeepSeek（Anthropic Messages 兼容）。

双通道自动切换（2026-09-01 上线）：主通道失败（非 200 / 网络异常 / 空响应）且配置了
DEEPSEEK_API_KEY 时自动走备用通道。背景：平台 AI 网关 frp 链路故障（带 Bearer 的
chat 请求被转发到远端网关时 http 打 https 端口，稳定 400），平台侧修复周期不可控。

两通道均纯 requests、零新依赖；失败返回 None（调用方降级模板）。
"""
from __future__ import annotations

import logging

import requests

from app.config import settings

log = logging.getLogger("tech-digest")

ANTHROPIC_VERSION = "2023-06-01"


def _chat_sse(prompt: str, system: str | None,
              max_tokens: int, timeout: int) -> str | None:
    """主通道：market-deploy 网关（OpenAI 兼容 /chat/completions，qwen3.8-27b-awq）。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": settings.sse_market_model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        # 实测（2026-08-30）: 该模型默认 reasoning_effort=xhigh，长 prompt 输出超 60s
        # 被 api.<MARKET_DOMAIN> 前置 nginx 504 网关超时打断；设 none 后单次 15s 完成。
        "reasoning_effort": "none",
    }
    url = settings.sse_market_base_url.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.sse_market_api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content if content and content.strip() else None
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None


def _chat_deepseek(prompt: str, system: str | None,
                   max_tokens: int, timeout: int) -> str | None:
    """备用通道：DeepSeek Anthropic 兼容端点（POST {base}/v1/messages）。

    报文格式按 Anthropic Messages API：x-api-key 头 + anthropic-version，
    system 为顶层字段，响应取 content[] 中 type=="text" 的块拼接。
    """
    payload: dict = {
        "model": settings.deepseek_model,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        # 实测（2026-09-01）: 该模型默认开思考，content 里先出 thinking 块；
        # 日报大 prompt 下 4096 token 全被思考烧光、text 块为空 → 36s 白跑。
        # thinking disabled 后直接出 text（与主通道 qwen 的 reasoning_effort="none" 同理）。
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    url = settings.deepseek_base_url.rstrip("/") + "/v1/messages"
    try:
        resp = requests.post(
            url,
            headers={"x-api-key": settings.deepseek_api_key,
                     "anthropic-version": ANTHROPIC_VERSION,
                     "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")
        return text if text.strip() else None
    except (requests.RequestException, ValueError):
        return None


def chat(prompt: str, system: str | None = None,
         max_tokens: int = 4096, timeout: int = 120) -> str | None:
    """单轮 chat：主通道 market-deploy → 失败自动切 DeepSeek 备用。

    两通道都没配 key 或都失败 → None（调用方降级模板）。
    """
    if not settings.sse_market_api_key and not settings.deepseek_api_key:
        return None
    if settings.sse_market_api_key:
        content = _chat_sse(prompt, system, max_tokens, timeout)
        if content:
            return content
        log.warning("AI 主通道（market-deploy）失败，尝试备用通道")
    if settings.deepseek_api_key:
        content = _chat_deepseek(prompt, system, max_tokens, timeout)
        if content:
            log.info("AI 备用通道（DeepSeek %s）生效", settings.deepseek_model)
            return content
        log.warning("AI 备用通道（DeepSeek）也失败")
    return None
