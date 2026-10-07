# -*- coding: utf-8 -*-
"""match 防二次生成守卫单测（2026-09-22 改造）。

目标行为：
- match（postID>0）已有留底 → 直接返回库里的那份，检索/LLM 零调用，meta.cached=True；
- match（postID>0）无留底 → 生成并落库；并发同 postID 单飞（只生成一次）；
- refs（只检索不生成）不受守卫影响，永远现场检索；
- 留底 answer 为空（如历史 refs 写入的 None 行）不算已生成，走正常生成；
- postID<=0（异常调用）生成但不落库，杜绝 postID=0 垃圾行；
- 检索降级（如 group-rag 宕机）产生的占位回答不落库——留底必须来自健康检索，
  否则会被守卫冻结成永久"没找到"（2026-09-24 事故教训，~500 帖被写死）；
- ASSIST_DEDUP_ENABLED=0 一键回滚：守卫全关，行为同旧版。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api as api_mod
from app.config import settings


SAVED_ROW = {
    "postID": 777,
    "partition": "学习交流",
    "title": "旧帖",
    "answer": "**库里的旧回答**",
    "references_json": '[{"postID": 1, "title": "r1", "url": "u", "snippet": "", "heat": 1.0}]',
    "sources_type": "local",
    "web_search_query": None,
    "web_results_json": None,
    "feedback_upvotes": 3,
    "feedback_downvotes": 0,
    "created_at": None,
}


def _stub_retrieve_result():
    return {"queries": ["q1"],
            "candidates": [{"postID": 12345, "title": "相关帖", "snippet": "片段",
                            "heat": 1.0, "score": 0.9, "comment_evidence": []}],
            "evidence": [],
            "mysql_hits_count": 2, "qdrant_hits_count": 0}


@pytest.fixture()
def no_web_search(monkeypatch):
    monkeypatch.setattr(settings, "web_search_enabled", False)
    # build_context 会直连 MySQL；format_context 输出只喂给已打桩的 build_answer
    monkeypatch.setattr(api_mod.ctx_mod, "build_context", lambda cands, ev=None: cands)
    monkeypatch.setattr(api_mod.ctx_mod, "format_context", lambda enriched: "CTX")


# ---------------------------------------------------------------------------
# 1) 已有留底 → 直接返回，检索/LLM 零调用
# ---------------------------------------------------------------------------

def test_match_returns_saved_without_generation(monkeypatch, no_web_search):
    calls = {"retrieve": 0, "llm": 0}

    def fake_fetch_one(sql, params=None):
        assert "assist_responses" in sql
        return dict(SAVED_ROW)

    def forbidden_retrieve(*a, **kw):
        calls["retrieve"] += 1
        raise AssertionError("有留底时不应触发检索")

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", forbidden_retrieve)

    resp = api_mod._match_impl("学习交流", "任意", "任意", 777, None)
    assert resp["ok"] is True
    assert resp["data"]["answer"] == "**库里的旧回答**"
    assert resp["data"]["meta"]["cached"] is True
    assert resp["data"]["meta"]["sources_type"] == "local"
    assert calls["retrieve"] == 0


# ---------------------------------------------------------------------------
# 2) 无留底 → 生成并落库一次
# ---------------------------------------------------------------------------

def test_match_generates_and_saves_when_no_saved(monkeypatch, no_web_search):
    writes: list[tuple] = []

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            return None
        if "FROM posts" in sql:  # postID 回查分区
            return {"partition": "学习交流"}
        return None

    def fake_retrieve(*a, **kw):
        return _stub_retrieve_result()

    def fake_build_answer(*a, **kw):
        return "新生成的回答"

    def fake_execute_write(sql, params=None):
        writes.append((sql, params))
        return 1

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(api_mod, "build_answer", fake_build_answer)
    monkeypatch.setattr(api_mod, "execute_write", fake_execute_write)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 888, None)
    assert resp["ok"] is True
    assert resp["data"]["answer"] == "新生成的回答"
    assert resp["data"]["meta"]["cached"] is False
    assert len(writes) == 1 and "assist_responses" in writes[0][0]


# ---------------------------------------------------------------------------
# 3) refs（只检索不生成）不受守卫影响
# ---------------------------------------------------------------------------

def test_refs_does_not_use_saved(monkeypatch, no_web_search):
    saved_queried = {"n": 0}

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            saved_queried["n"] += 1
            return dict(SAVED_ROW)
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    def fake_retrieve(*a, **kw):
        return _stub_retrieve_result()

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", fake_retrieve)
    monkeypatch.setattr(api_mod, "execute_write", lambda *a, **kw: 1)  # refs 仍会写 answer=None 行

    resp = api_mod._match_impl("学习交流", "标题", "内容", 777, None, generate_answer=False,
                               use_saved=False)
    assert resp["ok"] is True
    assert saved_queried["n"] == 0  # refs 不查留底
    assert resp["data"]["answer"] is None  # 不生成


# ---------------------------------------------------------------------------
# 4) 并发同 postID → 单飞，只生成一次
# ---------------------------------------------------------------------------

def test_concurrent_match_single_flight(monkeypatch, no_web_search):
    calls = {"n": 0}
    m = threading.Lock()
    saved: dict[int, dict] = {}  # execute_write 落库 → fetch_one 可见（模拟真实库）

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            time.sleep(0.02)  # 拉大竞态窗口：无锁时三线程都会走到检索
            return saved.get(999)  # 落库前 None，落库后有值
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    def slow_retrieve(*a, **kw):
        with m:
            calls["n"] += 1
        time.sleep(0.4)  # 模拟慢检索/LLM
        return _stub_retrieve_result()

    def fake_execute_write(sql, params=None):
        # _save_response 参数序：postID, partition, title, answer, ...
        saved[params[0]] = {"answer": params[3], "sources_type": "local",
                            "references_json": "[]"}
        return 1

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", slow_retrieve)
    monkeypatch.setattr(api_mod, "build_answer", lambda *a, **kw: "并发回答")
    monkeypatch.setattr(api_mod, "execute_write", fake_execute_write)

    api_mod._post_locks.clear()
    barrier = threading.Barrier(3)
    results: list[dict] = []

    def worker():
        barrier.wait()
        results.append(api_mod._match_impl("学习交流", "标题", "内容", 999, None))

    ts = [threading.Thread(target=worker) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    assert len(results) == 3
    assert calls["n"] == 1, f"应只生成一次，实际 {calls['n']} 次"
    answers = {r["data"]["answer"] for r in results}
    assert answers == {"并发回答"}


# ---------------------------------------------------------------------------
# 5) answer 为空的留底行（历史 refs 写入）不算已生成
# ---------------------------------------------------------------------------

def test_saved_row_with_empty_answer_regenerates(monkeypatch, no_web_search):
    row = dict(SAVED_ROW)
    row["answer"] = None

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            return row
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", lambda *a, **kw: _stub_retrieve_result())
    monkeypatch.setattr(api_mod, "build_answer", lambda *a, **kw: "重新生成")
    monkeypatch.setattr(api_mod, "execute_write", lambda *a, **kw: 1)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 777, None)
    assert resp["data"]["answer"] == "重新生成"


# ---------------------------------------------------------------------------
# 6) postID<=0 → 照常生成但不落库（杜绝 postID=0 垃圾行）
# ---------------------------------------------------------------------------

def test_no_postid_generates_without_save(monkeypatch, no_web_search):
    writes: list[tuple] = []

    def fake_fetch_one(sql, params=None):
        return None  # 无 postID：不查留底也不回查分区

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", lambda *a, **kw: _stub_retrieve_result())
    monkeypatch.setattr(api_mod, "build_answer", lambda *a, **kw: "无 ID 回答")
    monkeypatch.setattr(api_mod, "execute_write", lambda sql, params=None: writes.append(sql) or 1)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 0, None)
    assert resp["data"]["answer"] == "无 ID 回答"
    assert not any("assist_responses" in w for w in writes)


# ---------------------------------------------------------------------------
# 7) 回滚开关 ASSIST_DEDUP_ENABLED=0 → 守卫全关
# ---------------------------------------------------------------------------

def test_dedup_disabled_rolls_back(monkeypatch, no_web_search):
    monkeypatch.setattr(settings, "dedup_enabled", False)

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            raise AssertionError("回滚状态下不应查留底")
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", lambda *a, **kw: _stub_retrieve_result())
    monkeypatch.setattr(api_mod, "build_answer", lambda *a, **kw: "旧版行为")
    monkeypatch.setattr(api_mod, "execute_write", lambda *a, **kw: 1)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 777, None)
    assert resp["data"]["answer"] == "旧版行为"
    assert "cached" not in resp["data"]["meta"]


# ---------------------------------------------------------------------------
# 8) 已生成留底后再 refs → UPSERT 不得把 answer 清空（防二次生成的关键）
# ---------------------------------------------------------------------------

def test_refs_upsert_does_not_clobber_saved_answer(monkeypatch, no_web_search):
    sqls: list[str] = []

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            return dict(SAVED_ROW)  # 已有真实生成的留底
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    def fake_execute_write(sql, params=None):
        sqls.append(sql)
        return 1

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", lambda *a, **kw: _stub_retrieve_result())
    monkeypatch.setattr(api_mod, "execute_write", fake_execute_write)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 777, None,
                               generate_answer=False, use_saved=False)
    assert resp["ok"] is True
    assert len(sqls) == 1
    # 空回答到达时必须保留库内原 answer（UPSERT 带 IF 守卫）
    normalized = "".join(sqls[0].split())
    assert "answer=IF(VALUES(answer)" in normalized, "UPSERT 缺少空回答守卫"


# ---------------------------------------------------------------------------
# 9) 检索降级（如 group-rag 宕机 502）→ 占位回答不落库（帖子保持可重试）
# ---------------------------------------------------------------------------

def test_degraded_no_hit_does_not_persist(monkeypatch, no_web_search):
    writes: list[str] = []

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            return None  # 无留底
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    def broken_retrieve(*a, **kw):
        raise RuntimeError("502 检索链路失败：Error finding id")  # 模拟 group-rag 宕机

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", broken_retrieve)
    monkeypatch.setattr(api_mod, "execute_write",
                        lambda sql, params=None: writes.append(sql) or 1)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 888, None)
    assert resp["ok"] is True
    assert resp["data"]["answer"] == api_mod.NO_HIT_ANSWER  # 调用方仍拿到占位回答
    assert resp["data"]["meta"]["degraded"] is True
    assert not any("assist_responses" in w for w in writes), \
        "降级产生的占位回答不应落库（否则被守卫永久冻结）"


# ---------------------------------------------------------------------------
# 10) 检索健康但确实无相关内容 → NO_HIT 照常落库（有效留底，防反复重试）
# ---------------------------------------------------------------------------

def test_healthy_no_hit_still_persists(monkeypatch, no_web_search):
    writes: list[str] = []

    def fake_fetch_one(sql, params=None):
        if "assist_responses" in sql:
            return None
        if "FROM posts" in sql:
            return {"partition": "学习交流"}
        return None

    def empty_retrieve(*a, **kw):
        result = _stub_retrieve_result()
        result["candidates"] = []  # 检索正常跑完，只是没命中
        return result

    monkeypatch.setattr(api_mod, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(api_mod, "retrieve", empty_retrieve)
    monkeypatch.setattr(api_mod, "execute_write",
                        lambda sql, params=None: writes.append(sql) or 1)

    resp = api_mod._match_impl("学习交流", "标题", "内容", 888, None)
    assert resp["data"]["answer"] == api_mod.NO_HIT_ANSWER
    assert resp["data"]["meta"]["degraded"] is False
    assert len(writes) == 1 and "assist_responses" in writes[0]
