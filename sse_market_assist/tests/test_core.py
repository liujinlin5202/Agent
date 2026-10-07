# -*- coding: utf-8 -*-
"""核心纯函数单测：分区白名单、分块、点 ID 稳定性、关键词提取、合并排序。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import EXCLUDE_PARTITIONS, PARTITIONS, settings
from app.qdrant_store import stable_point_id
from app.retrieve import extract_keywords, merge_rank
from app.sync import _chunk_text


def test_partitions_whitelist():
    assert "学习交流" in PARTITIONS
    assert "主页" not in PARTITIONS  # 聚合流不可发帖
    assert len(PARTITIONS) == len(set(PARTITIONS))


def test_exclude_partitions():
    # 2026-09-08 起"主页"帖纳入索引 + 跨分区检索，排除列表应为空
    assert EXCLUDE_PARTITIONS == ()


def test_stable_point_id_deterministic():
    assert stable_point_id("post:1:chunk:0") == stable_point_id("post:1:chunk:0")
    assert stable_point_id("post:1:chunk:0") != stable_point_id("post:1:chunk:1")


def test_chunk_text():
    assert _chunk_text("") == []
    assert _chunk_text("短文本", size=800) == ["短文本"]
    chunks = _chunk_text("a" * 850, size=800, overlap=120)
    assert len(chunks) == 2
    assert chunks[1][:120] == chunks[0][-120:]  # 重叠区衔接


def test_extract_keywords():
    kws = extract_keywords("零基础如何入门Python", limit=8)
    assert "python" in kws
    assert any("基础" in k for k in kws)
    assert len(kws) <= 8


def test_merge_rank_topn():
    meta = {
        1: {"title": "t1", "heat": 10.0},
        2: {"title": "t2", "heat": 1.0},
        3: {"title": "t3", "heat": 5.0},
    }
    mysql = {1: 6.0, 2: 1.0}
    vec = {1: 0.9, 3: 0.5}
    cands = merge_rank(mysql, vec, meta, top_n=2)
    assert cands[0]["postID"] == 1  # 向量+关键词双命中且热度最高
    assert len(cands) == 2


def test_merge_rank_empty():
    assert merge_rank({}, {}, {}, top_n=5) == []


def test_llm_channels_config():
    """2026-09-10 起：主通道=集市网关，备通道=DeepSeek 官方。"""
    assert settings.sse_market_base_url.startswith("https://api.<MARKET_DOMAIN>")
    assert settings.sse_market_model
    assert settings.sse_market_api_key  # 必填项，缺失时 import config 即 KeyError
    assert settings.deepseek_base_url.startswith("https://api.deepseek.com")
    assert isinstance(settings.deepseek_api_key, str)  # 备通道可空，但不能缺失该属性
