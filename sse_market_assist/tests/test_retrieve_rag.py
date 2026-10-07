# -*- coding: utf-8 -*-
"""group-rag /kb/search 检索适配器单测（2026-09-22）。

- ASSIST_RETRIEVAL_PROVIDER=rag：retrieve() 走 RAG 适配器，本地 MySQL+Qdrant 链路零调用；
- local（默认）：行为与旧版完全一致，RAG 不可达；
- 适配器：解析 market_<postID>.txt → 回查主站 MySQL（过滤私密/已删/分区不符）
  → 组装与 merge_rank 同形候选 {postID, title, heat, partition, score=1/rank}；
- exclude_post_id 剔除自身帖；k 封顶 5；HTTP 异常向上抛，由 retrieve() 捕获→
  本次回退本地检索（2026-09-24 起），恢复后每次请求先试 RAG 自动切回。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import retrieve as ret
from app.config import settings


FAKE_RESULTS = {
    "results": [
        {"source": "market_101.txt", "category": "market", "content": "标题：A\n\n提问：…",
         "rank": 1, "score": 1.0},
        {"source": "market_102.txt", "category": "market", "content": "标题：B",
         "rank": 2, "score": 0.5},
        {"source": "market_103.txt", "category": "market", "content": "标题：C",
         "rank": 3, "score": 0.3333},
    ],
    "total": 3,
}


@pytest.fixture()
def rag_provider(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_provider", "rag")


def _patch_http(monkeypatch, payload=None, error=None):
    calls = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return payload if payload is not None else {"results": [], "total": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] = calls.get("n", 0) + 1
        calls.update(url=url, payload=json, timeout=timeout)
        if error:
            raise error
        return _Resp()

    monkeypatch.setattr(ret.httpx, "post", fake_post)
    return calls


# ---------------------------------------------------------------------------
# 1) provider 分流
# ---------------------------------------------------------------------------

def test_rag_provider_dispatches_to_adapter(monkeypatch, rag_provider):
    seen = {}

    def fake_adapter(partition, title, content, exclude_post_id=0, limit=None):
        seen["args"] = (partition, title, content, exclude_post_id, limit)
        return {"candidates": [], "via": "rag"}

    monkeypatch.setattr(ret, "retrieve_via_rag", fake_adapter)
    for name in ("keyword_search", "vector_search", "comment_evidence"):
        monkeypatch.setattr(ret, name, _forbidden(name))

    res = ret.retrieve("学习交流", "标题", "内容", 5, None)
    assert res["via"] == "rag"
    assert seen["args"] == ("学习交流", "标题", "内容", 5, None)


def test_local_provider_default_untouched(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_provider", "local")
    monkeypatch.setattr(ret, "retrieve_via_rag",
                        _forbidden("retrieve_via_rag"))
    monkeypatch.setattr(ret, "keyword_search", lambda *a, **kw: {})
    monkeypatch.setattr(ret, "vector_search", lambda *a, **kw: ({}, 0))
    monkeypatch.setattr(ret, "comment_evidence", lambda *a, **kw: [])
    monkeypatch.setattr(ret, "rewrite_queries", lambda t, c: ["q"])

    res = ret.retrieve("学习交流", "标题", "内容", 0, None)
    assert res["candidates"] == []
    assert res["mysql_hits_count"] == 0
    assert res["qdrant_hits_count"] == 0


def _forbidden(name):
    def _f(*a, **kw):
        raise AssertionError(f"provider 分流错误：{name} 不应被调用")
    return _f


# ---------------------------------------------------------------------------
# 2) 适配器：解析 / 过滤 / 组装
# ---------------------------------------------------------------------------

def test_rag_adapter_parse_and_filters(monkeypatch, rag_provider):
    calls = _patch_http(monkeypatch, FAKE_RESULTS)
    rows = [
        {"postID": 101, "title": "A帖", "heat": 3.0, "partition": "学习交流"},
        {"postID": 103, "title": "C帖", "heat": 0.0, "partition": "实习就业"},
        # 102 不在库 = 已删/私密 → 被过滤
    ]

    def fake_fetch_all(sql, params=None):
        assert "is_private=0" in sql, "必须做可见性回查"
        ids = [p for p in (params or ()) if isinstance(p, int)]
        return [r for r in rows if r["postID"] in ids]

    monkeypatch.setattr(ret, "fetch_all", fake_fetch_all)

    # exclude_post_id=102：RAG 返回 3 条，自身帖剔除，102 库中不存在
    res = ret.retrieve_via_rag("学习交流", "标题", "内容", 102, None)
    assert [c["postID"] for c in res["candidates"]] == [101, 103]
    assert res["candidates"][0]["score"] == 1.0
    assert res["candidates"][1]["score"] == pytest.approx(1 / 3, abs=1e-3)
    assert res["candidates"][0]["partition"] == "学习交流"
    assert res["candidates"][0]["heat"] == 3.0
    assert res["evidence"] == []
    assert res["mysql_hits_count"] == 2
    assert res["qdrant_hits_count"] == 3
    assert calls["url"].endswith("/kb/search")
    assert calls["payload"]["category"] == "market"
    assert calls["payload"]["k"] == 5


def test_rag_partition_filter_and_relax(monkeypatch, rag_provider):
    sqls: list[str] = []

    def fake_fetch_all(sql, params=None):
        sqls.append(sql)
        return []

    monkeypatch.setattr(ret, "fetch_all", fake_fetch_all)
    _patch_http(monkeypatch, FAKE_RESULTS)

    # 分区过滤生效；滤空后放宽为全分区重查一次
    ret.retrieve_via_rag("学习交流", "t", "c", 0, None)
    assert "`partition`=%s" in sqls[0]
    assert "`partition`=%s" not in sqls[1]

    sqls.clear()
    # 聚合流 = 跨分区：不加分区条件，也无放宽二查
    ret.retrieve_via_rag("主页", "t", "c", 0, None)
    assert len(sqls) == 1
    assert "`partition`=%s" not in sqls[0]


def test_rag_relax_recovers_when_partition_filters_all(monkeypatch, rag_provider):
    _patch_http(monkeypatch, FAKE_RESULTS)
    calls = {"n": 0}

    def fake_fetch_all(sql, params=None):
        calls["n"] += 1
        if "`partition`=%s" in sql:
            return []  # 分区内一无所获（RAG 强命中都在「主页」）
        return [
            {"postID": 101, "title": "A帖", "heat": 3.0, "partition": "主页"},
            {"postID": 103, "title": "C帖", "heat": 0.0, "partition": "实习就业"},
        ]

    monkeypatch.setattr(ret, "fetch_all", fake_fetch_all)
    res = ret.retrieve_via_rag("学习交流", "标题", "内容", 102, None)
    assert calls["n"] == 2, "应触发放宽二查"
    assert [c["postID"] for c in res["candidates"]] == [101, 103]
    assert res["mysql_hits_count"] == 2


def test_rag_k_cap_and_query_build(monkeypatch, rag_provider):
    calls = _patch_http(monkeypatch, {"results": [], "total": 0})

    ret.retrieve_via_rag("学习交流", "怎样保研", "正文" * 300, 0, 8)
    assert calls["payload"]["k"] == 5, "RAG 服务端上限 5，请求侧也应封顶"
    assert calls["payload"]["query"].startswith("怎样保研")

    ret.retrieve_via_rag("学习交流", "t2", "c2", 0, 3)
    assert calls["payload"]["k"] == 3


def test_rag_http_error_raises(monkeypatch, rag_provider):
    _patch_http(monkeypatch, error=RuntimeError("502 检索链路失败"))
    with pytest.raises(Exception):
        ret.retrieve_via_rag("学习交流", "t", "c", 0, None)


def test_rag_malformed_source_skipped(monkeypatch, rag_provider):
    _patch_http(monkeypatch, {"results": [
        {"source": "iwiki_9.txt", "category": "iwiki", "content": "x", "rank": 1, "score": 1.0},
        {"source": "market_abc.txt", "category": "market", "content": "y", "rank": 2, "score": 0.5},
        {"source": "market_104.txt", "category": "market", "content": "z", "rank": 3, "score": 0.3333},
    ], "total": 3})
    monkeypatch.setattr(ret, "fetch_all",
                        lambda sql, params=None: [{"postID": 104, "title": "D", "heat": 1.0,
                                                   "partition": "学习交流"}])
    res = ret.retrieve_via_rag("学习交流", "t", "c", 0, None)
    assert [c["postID"] for c in res["candidates"]] == [104]


# ---------------------------------------------------------------------------
# 3) RAG 失败 → 本次回退本地检索；恢复后自动切回（无粘性状态）
# ---------------------------------------------------------------------------

def test_rag_failure_falls_back_to_local(monkeypatch, rag_provider):
    order = []

    def broken_rag(*a, **kw):
        order.append("rag")
        raise RuntimeError("502 检索链路失败")

    def fake_local(partition, title, content, exclude_post_id=0, limit=None):
        order.append("local")
        return {"candidates": [{"postID": 9}], "via": "local",
                "mysql_hits_count": 0, "qdrant_hits_count": 0}

    monkeypatch.setattr(ret, "retrieve_via_rag", broken_rag)
    monkeypatch.setattr(ret, "_retrieve_local", fake_local)

    res = ret.retrieve("学习交流", "标题", "内容", 5, None)
    assert res["via"] == "local"
    assert res["candidates"] == [{"postID": 9}]
    assert order == ["rag", "local"], "必须先试 RAG，失败才回退本地"


def test_rag_recovery_switches_back_automatically(monkeypatch, rag_provider):
    """RAG 恢复后下一次请求自动回到 RAG（每次请求先试 RAG，不粘在本地）"""
    calls = {"rag": 0, "local": 0}

    def flaky_rag(*a, **kw):
        calls["rag"] += 1
        if calls["rag"] == 1:
            raise RuntimeError("502")
        return {"candidates": [], "via": "rag",
                "mysql_hits_count": 0, "qdrant_hits_count": 0}

    def fake_local(*a, **kw):
        calls["local"] += 1
        return {"candidates": [], "via": "local",
                "mysql_hits_count": 0, "qdrant_hits_count": 0}

    monkeypatch.setattr(ret, "retrieve_via_rag", flaky_rag)
    monkeypatch.setattr(ret, "_retrieve_local", fake_local)

    assert ret.retrieve("学习交流", "t", "c", 0, None)["via"] == "local"  # 宕机：回退
    assert ret.retrieve("学习交流", "t", "c", 0, None)["via"] == "rag"   # 恢复：自动切回
    assert calls == {"rag": 2, "local": 1}
