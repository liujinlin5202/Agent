# -*- coding: utf-8 -*-
"""索引同步单测：行转换、水位同秒边界、全量水位快照时机、watch 循环容错。

全部用 mock（不依赖真实 MySQL/Qdrant/embedding API），纯本地可跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.sync as sync_mod
from app.sync import (
    STATE_FILE,
    _incremental_rows,
    _row_to_items,
    full_sync,
    incremental_sync,
    watch_sync,
)

POST_ROW = {
    "postID": 101, "partition": "学习交流", "title": "标题A", "ptext": "正文" * 500,
    "post_time": "2026-08-28 10:00:00", "heat": 3.5, "is_anonymous": 0,
}
REPLY_ROW = {
    "pcommentID": 201, "ptargetID": 101, "pctext": "回复内容", "like_num": 7,
    "time": "2026-08-28 10:00:01", "postID": 101, "partition": "学习交流",
    "title": "标题A", "is_anonymous": 1,
}
SUBREPLY_ROW = {
    "ccommentID": 301, "ctargetID": 201, "cctext": "楼中楼内容", "like_num": 2,
    "time": "2026-08-28 10:00:02", "postID": 101, "partition": "学习交流",
    "title": "标题A", "is_anonymous": 0,
}


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    """把 STATE_FILE 指向临时目录，避免测试污染真实状态文件。"""
    monkeypatch.setattr(sync_mod, "STATE_FILE", tmp_path / "sync_state.json")
    return tmp_path / "sync_state.json"


def _noop_db(monkeypatch, rows_by_table=None, max_times=None):
    """按 SQL 特征路由的假 db：fetch_all 返回对应表行，fetch_one 返回 MAX 时间。"""
    rows_by_table = rows_by_table or {"posts": [], "replies": [], "subreplies": []}
    max_times = max_times or {}

    def fake_fetch_all(sql, params=None):
        captured.append((sql, params))
        if "FROM posts WHERE" in sql:
            return rows_by_table.get("posts", [])
        if "FROM pcomments c JOIN" in sql:
            return rows_by_table.get("replies", [])
        if "FROM ccomments cc JOIN" in sql:
            return rows_by_table.get("subreplies", [])
        return []

    def fake_fetch_one(sql, params=None):
        if "FROM posts" in sql:
            return {"mt": max_times.get("posts")} if max_times.get("posts") else None
        if "FROM pcomments" in sql:
            return {"mt": max_times.get("replies")} if max_times.get("replies") else None
        return {"mt": max_times.get("subreplies")} if max_times.get("subreplies") else None

    captured: list = []
    monkeypatch.setattr(sync_mod.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(sync_mod.db, "fetch_one", fake_fetch_one)
    return captured


def _noop_embed_and_upsert(monkeypatch, dim=1024):
    """embed 返回全零向量；upsert 只记录点数。"""
    def fake_embed(texts):
        return [[0.0] * dim for _ in texts]

    def fake_upsert(pts):
        up_count.append(len(pts))

    up_count: list = []
    monkeypatch.setattr(sync_mod, "embed_texts", fake_embed)
    monkeypatch.setattr(sync_mod, "upsert_points", fake_upsert)
    return up_count


# ---------- _row_to_items ----------

def test_row_to_items_post_chunking():
    items = list(_row_to_items(POST_ROW, "post"))
    assert len(items) >= 2  # 2000 字 > 800 → 分块
    origin, payload, emb = items[0]
    assert origin == "post:101:chunk:0"
    assert payload["doc_type"] == "post" and payload["partition"] == "学习交流"
    assert payload["postID"] == 101 and payload["heat"] == 3.5
    assert emb.startswith("[帖子] 标题:标题A")


def test_row_to_items_reply_and_subreply():
    o, p, e = list(_row_to_items(REPLY_ROW, "reply"))[0]
    assert o == "reply:201:chunk:0" and p["commentID"] == 201 and p["like_num"] == 7
    assert p["is_anonymous"] is True and "回复内容" in e
    o2, p2, e2 = list(_row_to_items(SUBREPLY_ROW, "subreply"))[0]
    assert o2 == "subreply:301:chunk:0" and p2["postID"] == 101
    assert "楼中楼" in e2
    # 隐私红线：payload 不含任何用户名/昵称字段（usertargetName 不索引入库）
    assert "usertargetName" not in p2 and "usertargetName" not in p


# ---------- _incremental_rows：同秒边界 ----------

def test_incremental_rows_uses_ge_watermark(monkeypatch):
    rows = [REPLY_ROW]
    captured = _noop_db(monkeypatch, rows_by_table={"replies": rows})
    got, new_wm = _incremental_rows("replies", "2026-08-28 10:00:00")
    sql, params = captured[0]
    assert ">= %s" in sql  # 必须 >=，否则水位同秒写入的行会被永久漏掉
    assert params[-1] == "2026-08-28 10:00:00"
    assert got == rows and new_wm == "2026-08-28 10:00:01"


def test_incremental_rows_empty_keeps_watermark(monkeypatch):
    _noop_db(monkeypatch)
    got, new_wm = _incremental_rows("posts", "2026-08-28 09:00:00")
    assert got == [] and new_wm == "2026-08-28 09:00:00"


# ---------- incremental_sync：水位推进 + 幂等跳过 ----------

def test_incremental_sync_advances_watermark(monkeypatch, tmp_state):
    _noop_db(monkeypatch, rows_by_table={"posts": [POST_ROW]})
    _noop_embed_and_upsert(monkeypatch)
    monkeypatch.setattr(sync_mod, "existing_ids", lambda: set())
    stats = incremental_sync()
    assert stats["posts"] == len(list(_row_to_items(POST_ROW, "post")))
    saved = json.loads(tmp_state.read_text("utf-8"))
    assert saved["posts_wm"] == "2026-08-28 10:00:00"


def test_incremental_sync_skips_existing(monkeypatch, tmp_state):
    # 同秒边界重复拉取的旧行由 stable_point_id 幂等跳过（existing_ids 命中）
    _noop_db(monkeypatch, rows_by_table={"posts": [POST_ROW]})
    up = _noop_embed_and_upsert(monkeypatch)
    from app.qdrant_store import stable_point_id

    existing = {stable_point_id(f"post:101:chunk:{i}") for i in range(10)}
    monkeypatch.setattr(sync_mod, "existing_ids", lambda: existing)
    stats = incremental_sync()
    assert stats["posts"] == 0
    assert sum(up) == 0


# ---------- full_sync：水位只在首次快照 ----------

def test_full_sync_snapshots_watermark_only_once(monkeypatch, tmp_state):
    # 第一次全量：水位快照 12:00
    _noop_db(monkeypatch, max_times={
        "posts": "2026-08-28 12:00:00",
        "replies": "2026-08-28 12:00:01",
        "subreplies": "2026-08-28 12:00:02",
    })
    _noop_embed_and_upsert(monkeypatch)
    monkeypatch.setattr(sync_mod, "existing_ids", lambda: set())
    full_sync()
    saved = json.loads(tmp_state.read_text("utf-8"))
    assert saved["posts_wm"] == "2026-08-28 12:00:00"

    # 第二次全量（断点续跑）：期间 MAX 已推进到 13:00，但沿用原水位，
    # 保证中断期间的新内容（时间 < 13:00）不会被漏掉
    _noop_db(monkeypatch, max_times={"posts": "2026-08-28 13:00:00"})
    full_sync()
    saved = json.loads(tmp_state.read_text("utf-8"))
    assert saved["posts_wm"] == "2026-08-28 12:00:00"


# ---------- _exclude_sql：排除列表为空时不得拼出非法 SQL ----------

def test_exclude_sql_empty_list_yields_valid_sql():
    """2026-09-08 起 EXCLUDE_PARTITIONS 为空（主页纳入索引）。此前的实现返回 "1=1"，
    被 `"AND p." + _exclude_sql()` 拼成 `p.1=1` → Unknown column 'p.1' 全量/增量同步崩溃。"""
    assert "p.1" not in sync_mod.REPLIES_SQL
    assert "p.1" not in sync_mod.SUBREPLIES_SQL
    assert "p.1" not in sync_mod.POSTS_SQL


def test_exclude_sql_qualifier(monkeypatch):
    monkeypatch.setattr(sync_mod, "EXCLUDE_PARTITIONS", ())
    assert sync_mod._exclude_sql() == "1=1"
    assert sync_mod._exclude_sql("p.") == "1=1"
    monkeypatch.setattr(sync_mod, "EXCLUDE_PARTITIONS", ("主页",))
    assert sync_mod._exclude_sql("p.") == "p.`partition` NOT IN (%s)"
    assert sync_mod._exclude_sql() == "`partition` NOT IN (%s)"


# ---------- watch_sync：循环 + 容错 ----------

def test_watch_loop_survives_failure_and_cleans_up(monkeypatch):
    calls: dict = {"n": 0}
    sleeps: list = []
    cleaned: list = []

    def fake_incremental():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("embedding 欠费（模拟）")
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return {"posts": 1}

    monkeypatch.setattr(sync_mod, "incremental_sync", fake_incremental)
    monkeypatch.setattr(sync_mod, "cleanup_private", lambda: cleaned.append(1) or 3)
    monkeypatch.setattr(sync_mod.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(KeyboardInterrupt):
        watch_sync(interval=0.01, cleanup_every=2)

    assert calls["n"] == 3          # 失败周期后继续跑了 2 个周期
    assert len(sleeps) == 2         # 每个周期（含失败）后 sleep
    assert cleaned == [1]           # 第 2 个成功周期触发了私密清理
