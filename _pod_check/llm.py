# -*- coding: utf-8 -*-
"""LLM 客户端：判断层（小 prompt → qwen，大 prompt → deepseek）+ 备用通道自动切换。

2026-09-10 主通道由 DeepSeek 官方直连改为集市自有网关（api.<MARKET_DOMAIN>/v1，
与 tech-digest 同网关同 key）；主通道任何异常由 LangChain with_fallbacks 兜到
DeepSeek 官方，并打印切换日志——静默降级会让网关故障无人察觉（2026-09-01 教训）。

2026-09-12 新增判断层（get_routed_llm）：按实际拼装后的 prompt 体量分流，
小 prompt 走网关 qwen3.8-27b-awq（思考模型，强制 reasoning_effort=none，
否则思考烧光 token 输出空内容），大 prompt 走 deepseek（现主通道）。
两条路由各自保留 DeepSeek 官方兜底：即使判断层把超大 prompt 误分给小模型
（真·爆上下文 400），with_fallbacks 也会自动改走 deepseek——分流只影响成本与
延迟，不影响可用性。查询改写等检索内部步骤不用判断层（体量恒定且影响召回质量，
保持原通道）。

Embedding 不走 langchain（兼容性实测不通过），见 app/embed.py。
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.prompt_values import PromptValue
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_openai import ChatOpenAI

from app.config import settings

# 生成参数（各通道保持一致，沿用现网已验证值）
_TEMPERATURE = 0.2
_MAX_TOKENS = 1536
# 单次 100s 上限、不重试：基线实测（42 例）最慢 ~31s；重试只会叠时长，保持 0。
_TIMEOUT = 100
_MAX_RETRIES = 0

# qwen 系（思考模型）必须显式关闭思考：不关会烧光 max_completion_tokens 输出空内容
# （2026-09-10 实测：网关 deepseek 系反而拒收 none，故只挂在 qwen 通道上）
_QWEN_EXTRA = {"reasoning_effort": "none"}

# 窗口安全余量：系统提示/角色包装/分词差异等的兜底冗余
_WINDOW_MARGIN = 2000


def estimate_prompt_tokens(text: str) -> int:
    """prompt token 预估（保守上界，宁大勿小）：非 ASCII 按 1 token/字，ASCII 按 3 字符/token。

    判断层用它决定"会不会爆上下文"，故取上界估计：Qwen/DeepSeek 分词器对中文实测
    约 0.6~0.75 token/字（非 ASCII 一律按 1/字取上界，中文/标点/假名/emoji 均覆盖），
    ASCII 实测约 4 字符/token（按 3 字符更保守）——宁可把边缘 prompt 分给大窗口的
    deepseek，也不冒险塞给小窗口模型。
    """
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return non_ascii + (len(text) - non_ascii + 2) // 3


def _make_llm(model: str, base_url: str, api_key: str,
              extra_body: dict | None = None) -> ChatOpenAI:
    """按统一参数构造一个 OpenAI 兼容客户端（extra_body 用于 qwen 的 reasoning_effort=none）。"""
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=_TEMPERATURE,
        max_completion_tokens=_MAX_TOKENS,
        timeout=_TIMEOUT,
        max_retries=_MAX_RETRIES,
        extra_body=extra_body or {},
    )


def _notify_failover(runnable: Runnable, label: str, failed: str = "主通道（集市网关）") -> Runnable:
    """包在备通道外层：被调用即说明主通道已失败，先打日志再执行。"""
    def _invoke(input, **kwargs):
        print(f"[llm] {failed}失败，已切换到{label}", flush=True)
        return runnable.invoke(input, **kwargs)
    return RunnableLambda(_invoke)


def _with_fallback(primary: ChatOpenAI, failed: str = "主通道（集市网关）") -> Runnable:
    """给主通道挂 DeepSeek 官方兜底；未配备通道 key 则原样返回（单通道）。"""
    if not settings.deepseek_api_key:
        return primary
    backup = _make_llm(settings.deepseek_model, settings.deepseek_base_url, settings.deepseek_api_key)
    return primary.with_fallbacks([_notify_failover(backup, "备用通道(DeepSeek)", failed)])


def _build_llm(primary_key: str, primary_url: str, primary_model: str,
               fallback_key: str = "", fallback_url: str = "", fallback_model: str = "") -> Runnable:
    """主通道必配；备通道配了 key 才挂上（未配则单通道返回）。"""
    primary = _make_llm(primary_model, primary_url, primary_key)
    if not fallback_key:
        return primary
    backup = _make_llm(fallback_model, fallback_url, fallback_key)
    return primary.with_fallbacks([_notify_failover(backup, "备用通道(DeepSeek)")])


@lru_cache(maxsize=1)
def get_llm() -> Runnable:
    """现主通道（网关 deepseek + DeepSeek 官方兜底）：检索改写等内部步骤沿用。"""
    return _build_llm(
        settings.sse_market_api_key, settings.sse_market_base_url, settings.sse_market_model,
        settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model,
    )


def window_is_safe(threshold: int, window: int) -> bool:
    """安全不变式：预估阈值 + 输出上限 + 余量 ≤ 小模型窗口（否则小通道有爆上下文风险）。"""
    return threshold + _MAX_TOKENS + _WINDOW_MARGIN <= window


def _make_router(small: Runnable, large: Runnable, threshold: int) -> Runnable:
    """判断层实现：按 prompt 预估 token 选通道（工厂函数便于单测注入假通道）。"""
    def _pick(prompt: PromptValue, config=None):
        text = prompt.to_string()
        est = estimate_prompt_tokens(text)
        to_small = est <= threshold
        print(
            f"[llm] 判断层：prompt {len(text)} 字符 ≈ {est} tokens "
            f"{'≤' if to_small else '>'} {threshold} → "
            f"{settings.small_model if to_small else settings.sse_market_model}",
            flush=True,
        )
        chain = small if to_small else large
        return chain.invoke(prompt, config=config)
    return RunnableLambda(_pick)


def _small_chain() -> Runnable:
    """小 prompt 通道：网关 qwen（关思考）+ DeepSeek 官方兜底。"""
    qwen = _make_llm(settings.small_model, settings.sse_market_base_url,
                     settings.sse_market_api_key, extra_body=_QWEN_EXTRA)
    return _with_fallback(qwen, failed=f"小模型通道（集市网关 {settings.small_model}）")


@lru_cache(maxsize=1)
def get_routed_llm() -> Runnable:
    """发帖应答/追问的模型通道：小 prompt → qwen，大 prompt → deepseek。

    route_enabled=0 或窗口自检不通过（阈值配置会爆小模型上下文）时不冒险，
    整体退化为现主通道（与判断层上线前完全一致的行为）。
    """
    if not settings.route_enabled:
        return get_llm()
    if not window_is_safe(settings.route_small_max_tokens, settings.small_model_window):
        print(
            f"[llm] 判断层窗口自检不通过：阈值 {settings.route_small_max_tokens} + 输出 "
            f"{_MAX_TOKENS} + 余量 {_WINDOW_MARGIN} > 小模型窗口 {settings.small_model_window}，"
            "本次全程改走主通道（不冒险分流）",
            flush=True,
        )
        return get_llm()
    return _make_router(_small_chain(), get_llm(), settings.route_small_max_tokens)
