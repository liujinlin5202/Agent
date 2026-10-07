# -*- coding: utf-8 -*-
"""LLM 双通道单测：构造、参数一致性、主通道失败时的切换与日志。"""
from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from langchain_openai import ChatOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import _build_llm, _make_llm, _notify_failover


def test_make_llm_params():
    llm = _make_llm("m1", "https://gw.example/v1", "k1")
    assert llm.model_name == "m1"
    assert llm.openai_api_base == "https://gw.example/v1"
    assert llm.temperature == 0.2
    assert llm.max_tokens == 1536
    assert llm.request_timeout == 100
    assert llm.max_retries == 0


def test_single_channel_when_no_fallback_key():
    """备通道未配 key（空串）时退化为单通道，返回裸 ChatOpenAI。"""
    llm = _build_llm("k1", "https://gw.example/v1", "m1", "", "", "")
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "m1"


def test_dual_channel_structure():
    llm = _build_llm("k1", "https://gw.example/v1", "m1",
                     "k2", "https://ds.example/v1", "m2")
    assert isinstance(llm, RunnableWithFallbacks)
    primary = llm.runnable
    assert isinstance(primary, ChatOpenAI)
    assert primary.model_name == "m1"
    assert primary.openai_api_base == "https://gw.example/v1"
    assert len(llm.fallbacks) == 1  # 备通道已挂上


def test_failover_switches_and_logs(capsys):
    """主通道抛异常 → 真实落到备通道，且 stdout 留下可观测日志。"""
    def boom(_):
        raise RuntimeError("主通道挂了")

    primary = RunnableLambda(boom)
    backup = RunnableLambda(lambda _: "来自备用通道")
    chain = primary.with_fallbacks([_notify_failover(backup, "备用通道(DeepSeek)")])

    out = chain.invoke("hi")

    assert out == "来自备用通道"
    assert "已切换到备用通道(DeepSeek)" in capsys.readouterr().out
