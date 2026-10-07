# -*- coding: utf-8 -*-
"""判断层单测：token 预估、按体量分流、日志、窗口安全不变式、开关回退、兜底语义。

"会不会爆上下文"的保证由两层构成，均有断言覆盖：
1. 结构层：窗口安全不变式（阈值 + 输出 + 余量 ≤ 小模型窗口），越界自动全程改走主通道；
2. 兜底层：qwen 通道带 DeepSeek 兜底，真被 400（context 超限）也会自动切换。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.prompt_values import StringPromptValue
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from langchain_openai import ChatOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import context as ctx_mod
from app.config import settings
from app.llm import (
    _MAX_TOKENS,
    _QWEN_EXTRA,
    _make_llm,
    _make_router,
    _notify_failover,
    _small_chain,
    estimate_prompt_tokens,
    get_llm,
    get_routed_llm,
    window_is_safe,
)


# ---------------------------------------------------------------------------
# 1. token 预估
# ---------------------------------------------------------------------------

def test_estimate_ascii_and_cjk():
    assert estimate_prompt_tokens("a" * 300) == 100        # 非 CJK：3 字符/token
    assert estimate_prompt_tokens("中" * 300) == 300        # CJK：1 token/字（保守上界）
    assert estimate_prompt_tokens("，。！？" * 100) == 400   # 全角标点按 CJK 计


def test_estimate_is_upper_bound():
    """估计值必须 ≤ 字符数（1 token/字是上界），且对真实材料量级合理。"""
    text = "[1] 帖子《食堂推荐》 分区:主页 热度:12.5\n正文片段:荔园二楼馄饨 13 点截止，见 fenqi.cn"
    est = estimate_prompt_tokens(text)
    assert 0 < est <= len(text)
    # 单调性：材料越长估计越大
    assert estimate_prompt_tokens(text * 3) > est


# ---------------------------------------------------------------------------
# 2. 按体量分流
# ---------------------------------------------------------------------------

def _stub(name: str, calls: list[str]) -> RunnableLambda:
    def _f(prompt):
        calls.append(name)
        return name
    return RunnableLambda(_f)


def test_router_splits_by_size():
    calls: list[str] = []
    router = _make_router(_stub("small", calls), _stub("large", calls), threshold=50)
    assert router.invoke(StringPromptValue(text="短" * 50)) == "small"    # est=50 ≤ 50
    assert router.invoke(StringPromptValue(text="长" * 51)) == "large"    # est=51 > 50
    assert calls == ["small", "large"]


def test_router_logs_decision(capsys):
    router = _make_router(_stub("small", []), _stub("large", []), threshold=10)
    router.invoke(StringPromptValue(text="你好"))
    router.invoke(StringPromptValue(text="字" * 999))
    out = capsys.readouterr().out
    assert "判断层" in out and "qwen" in out          # 小 → qwen（settings.small_model）
    assert "deepseek" in out                          # 大 → deepseek（settings.sse_market_model）


# ---------------------------------------------------------------------------
# 3. 窗口安全不变式（会不会爆上下文的结构层保证）
# ---------------------------------------------------------------------------

def test_window_is_safe_boundary():
    assert window_is_safe(20000, 32000) is True
    assert window_is_safe(32000, 32000) is False       # 阈值顶满窗口必爆


def test_default_config_is_safe():
    """出厂配置必须满足不变式，否则判断层一上线就分流出爆上下文风险。"""
    assert window_is_safe(settings.route_small_max_tokens, settings.small_model_window), (
        f"阈值 {settings.route_small_max_tokens} 超出小模型窗口 {settings.small_model_window} 的安全范围"
    )


def test_worst_case_material_goes_to_large_channel():
    """满额检索材料（8 引用全截断）必须 > 阈值 → 交大窗口通道（常量变更护栏）。"""
    per_ref = (ctx_mod.SNIPPET_LEN
               + ctx_mod.TOP_REPLIES * (ctx_mod.REPLY_LEN + ctx_mod.TOP_SUBREPLIES * ctx_mod.SUBREPLY_LEN)
               + ctx_mod.RELATED_PER_POST * ctx_mod.REPLY_LEN)
    refs = settings.top_k + ctx_mod.ORPHAN_CAP
    worst = estimate_prompt_tokens("字" * (per_ref * refs + 1000))   # 材料 + 问题 + 系统提示
    assert worst > settings.route_small_max_tokens, (
        f"最坏材料 ≈{worst} tokens 未超阈值 {settings.route_small_max_tokens}，拆分失去意义"
    )
    assert worst <= settings.small_model_window + _MAX_TOKENS, (
        "最坏情况也不能超出（大窗口）通道上限"
    )


def test_typical_material_goes_to_small_channel():
    """典型材料（5 引用、片段约半满）应 ≤ 阈值 → 走 qwen，否则判断层形同虚设。"""
    typical = estimate_prompt_tokens("字" * (5 * 1500 + 1000))
    assert typical <= settings.route_small_max_tokens


# ---------------------------------------------------------------------------
# 4. 通道构造（qwen 必须关思考；deepseek 不能带 reasoning_effort）
# ---------------------------------------------------------------------------

def test_small_model_request_carries_reasoning_off():
    """qwen 通道必须显式 reasoning_effort=none（否则思考烧光 token 输出空内容）。

    langchain-openai 的 payload 里 extra_body 为嵌套字段，由 openai SDK 发请求时
    合并进 JSON body（真机验证见 pod 试运行探针的 langchain 直调）。
    """
    llm = _make_llm(settings.small_model, settings.sse_market_base_url, "k", extra_body=_QWEN_EXTRA)
    assert llm.model_name == settings.small_model
    payload = llm._get_request_payload([HumanMessage(content="hi")])
    assert payload["extra_body"] == {"reasoning_effort": "none"}


def test_main_channel_has_no_reasoning_effort():
    """网关 deepseek 系拒收 reasoning_effort=none（400 ModelArts.81001），必须不带。"""
    llm = _make_llm(settings.sse_market_model, settings.sse_market_base_url, "k")
    payload = llm._get_request_payload([HumanMessage(content="hi")])
    assert not payload.get("extra_body") and "reasoning_effort" not in payload


def test_small_chain_fallback_structure(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "k2")
    chain = _small_chain()
    assert isinstance(chain, RunnableWithFallbacks)
    assert chain.runnable.model_name == settings.small_model
    assert chain.runnable.extra_body == {"reasoning_effort": "none"}
    assert len(chain.fallbacks) == 1


def test_small_chain_single_when_no_backup_key(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    assert isinstance(_small_chain(), ChatOpenAI)


# ---------------------------------------------------------------------------
# 5. 开关与自检回退（= 一键回滚到当前状态）
# ---------------------------------------------------------------------------

def test_route_disabled_falls_back_to_main_channel(monkeypatch):
    get_routed_llm.cache_clear()
    monkeypatch.setattr(settings, "route_enabled", False)
    try:
        assert get_routed_llm() is get_llm()
    finally:
        get_routed_llm.cache_clear()


def test_unsafe_threshold_falls_back_to_main_channel(monkeypatch, capsys):
    """阈值越界（会爆小模型上下文）时整体退化为现主通道，绝不冒险分流。"""
    get_routed_llm.cache_clear()
    monkeypatch.setattr(settings, "route_small_max_tokens", settings.small_model_window)
    try:
        assert get_routed_llm() is get_llm()
    finally:
        get_routed_llm.cache_clear()
    assert "窗口自检不通过" in capsys.readouterr().out


def test_routed_llm_is_router_by_default(monkeypatch):
    get_routed_llm.cache_clear()
    monkeypatch.setattr(settings, "route_enabled", True)
    monkeypatch.setattr(settings, "route_small_max_tokens", 20000)
    monkeypatch.setattr(settings, "small_model_window", 32000)
    try:
        assert isinstance(get_routed_llm(), RunnableLambda)
    finally:
        get_routed_llm.cache_clear()


# ---------------------------------------------------------------------------
# 6. 兜底语义：误判导致的 400（爆上下文）也会被自动切换接住
# ---------------------------------------------------------------------------

def test_fallback_recovers_from_context_error(capsys):
    def boom(_):
        raise RuntimeError("This model's maximum context length is 32768 tokens")

    chain = RunnableLambda(boom).with_fallbacks(
        [_notify_failover(RunnableLambda(lambda _: "兜底回答"), "备用通道(DeepSeek)")])
    assert chain.invoke("超大 prompt") == "兜底回答"
    assert "已切换到备用通道(DeepSeek)" in capsys.readouterr().out


def test_failover_label_names_failed_channel(capsys):
    """qwen 分支失败时日志须指明是小模型通道，否则与主通道故障无法区分。"""
    def boom(_):
        raise RuntimeError("qwen 挂了")

    chain = RunnableLambda(boom).with_fallbacks([
        _notify_failover(RunnableLambda(lambda _: "兜底回答"), "备用通道(DeepSeek)",
                         f"小模型通道（集市网关 {settings.small_model}）")])
    assert chain.invoke("p") == "兜底回答"
    out = capsys.readouterr().out
    assert "小模型通道" in out and settings.small_model in out
