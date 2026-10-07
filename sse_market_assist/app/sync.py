# -*- coding: utf-8 -*-
"""索引同步：帖子 + 一级回复 + 楼中楼 → 新 collection market_assist_v1。

- 全量：上线时跑一次（或 --resume 断点续传）；
- 增量：按时间戳水位拉取新内容（--incremental，可定时）；
- 清理：is_private=1 的帖子点按 postID 过滤删除（--cleanup）；
- 限速：embedding ≤ ASSIST_EMBED_QPS（DashScope 额度保护）；
- 全程只读 MySQL、只写新 collection（红线 2/4）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from qdrant_client.models import PointStruct

from app import db
from app.config import BASE_DIR, EXCLUDE_PARTITIONS, settings
from app.embed import embed_texts
from app.qdrant_store import delete_by_post_ids, existing_ids, stable_point_id, upsert_points

STATE_FILE = BASE_DIR / "data" / "sync_state.json"

# 与文档 4.1 对齐的三条只读 SQL（排除私密帖；EXCLUDE_PARTITIONS 为空时不加分区条件）
def _exclude_sql(qualifier: str = "") -> str:
    """生成 `partition NOT IN (...)` 条件（参数化）；无排除分区时返回恒真。

    qualifier 为 JOIN 场景的表前缀（如 "p."）：调用方写 `"AND " + _exclude_sql("p.")`，
    排除列表为空时也不会拼出 `AND p.1=1` 这类非法 SQL（2026-09-08 曾因此崩溃）。
    """
    if not EXCLUDE_PARTITIONS:
        return "1=1"
    ph = ",".join(["%s"] * len(EXCLUDE_PARTITIONS))
    return f"{qualifier}`partition` NOT IN ({ph})"


POSTS_SQL = (
    "SELECT postID, `partition`, title, ptext, post_time, heat, is_anonymous "
    "FROM posts WHERE is_private=0 AND " + _exclude_sql()
)
REPLIES_SQL = (
    "SELECT c.pcommentID, c.ptargetID, c.pctext, c.like_num, c.time, "
    "       p.postID, p.`partition`, p.title, p.is_anonymous "
    "FROM pcomments c JOIN posts p ON p.postID=c.ptargetID "
    "WHERE p.is_private=0 AND " + _exclude_sql("p.")
)
SUBREPLIES_SQL = (
    "SELECT cc.ccommentID, cc.ctargetID, cc.cctext, cc.like_num, cc.time, "
    "       p.postID, p.`partition`, p.title, p.is_anonymous "
    "FROM ccomments cc JOIN pcomments c ON c.pcommentID=cc.ctargetID "
    "JOIN posts p ON p.postID=c.ptargetID "
    "WHERE p.is_private=0 AND " + _exclude_sql("p.")
)

_DOC_TYPE = {"posts": "post", "replies": "reply", "subreplies": "subreply"}

# (表名 → (基础 SQL, SQL 中的时间列, 结果集键)) —— 时间列需限定表名避免 JOIN 歧义
_TABLE_SPEC = {
    "posts": (POSTS_SQL, "post_time", "post_time"),
    "replies": (REPLIES_SQL, "c.time", "time"),
    "subreplies": (SUBREPLIES_SQL, "cc.time", "time"),
}


def _chunk_text(text: str, size: Optional[int] = None, overlap: Optional[int] = None) -> list[str]:
    """按字符分块（800 字 / 120 重叠），与文档 4.1 一致。"""
    size = size or settings.chunk_size
    overlap = overlap or settings.chunk_overlap
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return chunks


def _embed_with_limit(texts: list[str]) -> list[list[float]]:
    """分批请求 embedding（原始 HTTP，单批内部自带重试），限速 ≤ embed_qps。"""
    interval = 1.0 / settings.embed_qps if settings.embed_qps > 0 else 0
    vecs: list[list[float]] = []
    for i in range(0, len(texts), settings.embed_batch):
        batch = texts[i : i + settings.embed_batch]
        vecs.extend(embed_texts(batch))
        if interval:
            time.sleep(interval)
    return vecs


def _row_to_items(row: dict[str, Any], doc_type: str) -> Iterator[tuple[str, dict[str, Any], str]]:
    """一行 SQL 结果 → (origin, payload, 嵌入文本) 三元组（长文本分块）。"""
    post_id = int(row["postID"])
    title = (row.get("title") or "").strip()
    partition = str(row["partition"])
    is_anon = bool(row.get("is_anonymous"))
    post_time = str(row.get("post_time") or row.get("time") or "")
    heat = float(row.get("heat") or 0)
    like = int(row.get("like_num") or 0)

    if doc_type == "post":
        for idx, chunk in enumerate(_chunk_text(row.get("ptext") or "")):
            origin = f"post:{post_id}:chunk:{idx}"
            payload = {
                "partition": partition, "doc_type": "post", "postID": post_id,
                "commentID": 0, "title": title, "text": chunk, "chunk_index": idx,
                "post_time": post_time, "like_num": 0, "heat": heat,
                "is_anonymous": is_anon, "origin_id": origin,
            }
            yield origin, payload, f"[帖子] 标题:{title}\n内容:{chunk}"
        return

    if doc_type == "reply":
        text, comment_id = row.get("pctext") or "", int(row["pcommentID"])
        tag = "回复"
    else:  # subreply（不索引 usertargetName，避免泄露昵称）
        text, comment_id = row.get("cctext") or "", int(row["ccommentID"])
        tag = "楼中楼"
    for idx, chunk in enumerate(_chunk_text(text)):
        origin = f"{doc_type}:{comment_id}:chunk:{idx}"
        payload = {
            "partition": partition, "doc_type": doc_type, "postID": post_id,
            "commentID": comment_id, "title": title, "text": chunk, "chunk_index": idx,
            "post_time": post_time, "like_num": like, "heat": 0.0,
            "is_anonymous": is_anon, "origin_id": origin,
        }
        yield origin, payload, f"[{tag}] 帖子标题:{title}\n{tag}内容:{chunk}"


def _sync_rows(rows: list[dict[str, Any]], doc_type: str, label: str,
               skip_ids: Optional[set[int]] = None) -> int:
    """批量嵌入并 upsert；skip_ids 中的 origin 已入库则跳过（断点续传）。"""
    skip_ids = skip_ids or set()
    pending: list[tuple[str, dict[str, Any]]] = []
    emb_texts: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal count
        if not pending:
            return
        vecs = _embed_with_limit(emb_texts)
        pts = [
            PointStruct(id=stable_point_id(o), vector=v, payload=p)
            for (o, p), v in zip(pending, vecs)
        ]
        upsert_points(pts)
        count += len(pts)
        pending.clear()
        emb_texts.clear()

    for row in rows:
        for origin, payload, emb_text in _row_to_items(row, doc_type):
            if stable_point_id(origin) in skip_ids:
                continue
            pending.append((origin, payload))
            emb_texts.append(emb_text)
            if len(pending) >= settings.embed_batch:
                flush()
                if count % 500 == 0 and count:
                    print(f"[sync] {label} 已入库 {count} 点", flush=True)
    flush()
    print(f"[sync] {label} 完成：新增 {count} 点", flush=True)
    return count


def _max_time(sql: str, params: tuple) -> Optional[str]:
    row = db.fetch_one(sql, params)
    return str(row["mt"]) if row and row["mt"] else None


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def full_sync() -> dict[str, int]:
    """全量同步（不重建 collection；已存在的 origin 自动跳过，可断点续传）。

    水位在同步开始前快照并立即落盘：断点续跑沿用原水位，
    保证中断期间新写入的内容（时间 < 续跑时 MAX）不会被漏掉；
    同步期间的新内容因时间 >= 水位，由增量/watch 捕获。
    """
    print("[sync] 全量同步开始", flush=True)
    params: tuple = EXCLUDE_PARTITIONS
    state = _load_state()
    # 仅首次全量快照水位；断点续跑沿用旧水位（否则中断期间的新内容可能被漏掉）
    if not state.get("posts_wm"):
        state["posts_wm"] = _max_time(
            "SELECT MAX(post_time) AS mt FROM posts WHERE is_private=0 AND " + _exclude_sql(), params) \
            or "1970-01-01 00:00:00"
        state["replies_wm"] = _max_time(
            "SELECT MAX(c.time) AS mt FROM pcomments c JOIN posts p ON p.postID=c.ptargetID "
            "WHERE p.is_private=0 AND " + _exclude_sql("p."), params) \
            or "1970-01-01 00:00:00"
        state["subreplies_wm"] = _max_time(
            "SELECT MAX(cc.time) AS mt FROM ccomments cc "
            "JOIN pcomments c ON c.pcommentID=cc.ctargetID "
            "JOIN posts p ON p.postID=c.ptargetID "
            "WHERE p.is_private=0 AND " + _exclude_sql("p."), params) \
            or "1970-01-01 00:00:00"
        _save_state(state)
    skip = existing_ids()
    print(f"[sync] 已存在 {len(skip)} 个点，将跳过", flush=True)
    stats = {
        "posts": _sync_rows(db.fetch_all(POSTS_SQL, params), "post", "帖子", skip),
        "replies": _sync_rows(db.fetch_all(REPLIES_SQL, params), "reply", "回复", skip),
        "subreplies": _sync_rows(db.fetch_all(SUBREPLIES_SQL, params), "subreply", "楼中楼", skip),
    }
    state["full_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(state)
    print("[sync] 全量同步完成:", stats, flush=True)
    return stats


def _incremental_rows(table: str, wm: str) -> tuple[list[dict[str, Any]], str]:
    """拉取水位之后的新行与新水位（时间条件用 >=，详见注释）。

    用 >= 而非 >：MySQL 时间列精确到秒，发帖/评论可能落在水位同一秒，
    > 会永久漏掉该秒新写入的行；水位同秒的旧行每周期重复拉取，
    但由 stable_point_id 幂等跳过（内存集合比对，代价可忽略）。
    """
    base_sql, sql_col, result_key = _TABLE_SPEC[table]
    params: tuple = EXCLUDE_PARTITIONS
    rows = db.fetch_all(base_sql + f" AND {sql_col} >= %s", params + (wm,))
    new_wm = max(str(r[result_key]) for r in rows) if rows else wm
    return rows, new_wm


def incremental_sync() -> dict[str, int]:
    """按时间水位拉取新增内容入索引（watch 定时调用；对"发帖时检索已有数据"无影响）。"""
    state = _load_state()
    new_state = dict(state)
    skip = existing_ids()
    stats: dict[str, int] = {}
    for table in _TABLE_SPEC:
        wm = state.get(f"{table}_wm") or "1970-01-01 00:00:00"
        rows, new_wm = _incremental_rows(table, wm)
        stats[table] = _sync_rows(rows, _DOC_TYPE[table], f"{table}增量", skip_ids=skip)
        new_state[f"{table}_wm"] = new_wm
    _save_state(new_state)
    print("[sync] 增量同步完成:", stats, flush=True)
    return stats


def watch_sync(interval: float | None = None, cleanup_every: int | None = None) -> None:
    """常驻 watch：周期增量同步 + 定期私密清理 = 发帖/评论自动入索引。

    不阻塞发帖的保证：本进程与发帖链路完全无关——不触碰 Go 后端、不在
    发帖请求路径上，仅周期轮询 MySQL 新内容；embedding 失败（欠费等）
    只影响本周期，下周期自动重试，绝不影响发帖（红线 1）。
    """
    interval = settings.sync_interval if interval is None else interval
    cleanup_every = settings.sync_cleanup_cycles if cleanup_every is None else cleanup_every
    cycle = 0
    print(f"[sync] watch 启动：每 {interval}s 增量同步，每 {cleanup_every} 周期清理私密一次", flush=True)
    while True:
        cycle += 1
        try:
            stats = incremental_sync()
            if cleanup_every and cycle % cleanup_every == 0:
                try:
                    stats["cleaned"] = cleanup_private()
                except Exception as e:  # noqa: BLE001
                    print(f"[sync] 私密清理失败（下周期重试）: {e!r}", flush=True)
            print(f"[sync] 第 {cycle} 周期完成: {stats}", flush=True)
        except KeyboardInterrupt:
            print("[sync] watch 收到停止信号，退出", flush=True)
            raise
        except Exception as e:  # noqa: BLE001  单周期失败不退出（欠费/网络恢复后自动续上）
            print(f"[sync] 第 {cycle} 周期失败（{interval}s 后重试）: {e!r}", flush=True)
        time.sleep(interval)


def cleanup_private() -> int:
    """删除 is_private=1 的帖子在新 collection 中的所有点（含其回复/楼中楼）。"""
    rows = db.fetch_all("SELECT postID FROM posts WHERE is_private=1")
    ids = [int(r["postID"]) for r in rows]
    removed = delete_by_post_ids(ids)
    print(f"[sync] 私密清理：{len(ids)} 帖 → 删除 {removed} 点", flush=True)
    return removed
