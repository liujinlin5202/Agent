# -*- coding: utf-8 -*-
"""集市 MySQL 只读查询：xiaoqiu_ro 账号仅 SELECT 两个公开视图。

- v_posts_public：无私密帖、无 userID（匿名帖 author_alias='匿名'），
  含 browse/like/dislike/comment/heat/is_high_quality 全部策展所需字段
- v_pcomments_public：join posts 过滤私密帖评论，评论者一律 SHA2 别名

**只读是硬约束**：本模块只跑 SELECT。加精写路径一律走 Go 后端
/internal/featured（见 writer.py），不绕过业务层直改库。
"""
from __future__ import annotations

import logging
from typing import Any

import pymysql

log = logging.getLogger("post-curator")

_POST_COLS = (
    "postID, `partition`, title, ptext, post_time, browse_num, like_num, "
    "dislike_num, comment_num, heat, tag"
)


def _connect(cfg) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=cfg.db_host,
        port=cfg.db_port,
        user=cfg.db_user,
        password=cfg.db_password,
        database=cfg.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def _num(v: Any) -> int:
    """视图里 bigint/double 均可空——统一收敛成非负 int，prompt 侧零判空。"""
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _norm_post(row: dict) -> dict:
    return {
        "post_id": int(row["postID"]),
        "partition": row.get("partition") or "",
        "title": row.get("title") or "",
        "ptext": row.get("ptext") or "",
        "post_time": str(row.get("post_time") or ""),
        "browse_num": _num(row.get("browse_num")),
        "like_num": _num(row.get("like_num")),
        "dislike_num": _num(row.get("dislike_num")),
        "comment_num": _num(row.get("comment_num")),
        "heat": _num(row.get("heat")),
        "tag": row.get("tag") or "",
    }


def fetch_recent_unfeatured(cfg, days: int) -> list[dict]:
    """近 N 天未加精帖子（私密帖视图层已排除），新帖优先。"""
    sql = (
        f"SELECT {_POST_COLS} FROM v_posts_public "
        "WHERE post_time >= NOW() - INTERVAL %s DAY "
        "AND IFNULL(is_high_quality, 0) <> 1 "
        "ORDER BY post_time DESC"
    )
    with _connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days,))
            return [_norm_post(r) for r in cur.fetchall()]


def fetch_all_unfeatured(cfg, top_n: int) -> list[dict]:
    """全量模式：全部未加精帖按互动信号预排取 top N（信号=热度+赞×2+评论×3+浏览×0.1，
    评论权重最高——问询帖的价值长在评论区）。"""
    sql = (
        f"SELECT {_POST_COLS} FROM v_posts_public "
        "WHERE IFNULL(is_high_quality, 0) <> 1 "
        "ORDER BY (IFNULL(heat,0) + IFNULL(like_num,0)*2 + "
        "IFNULL(comment_num,0)*3 + IFNULL(browse_num,0)*0.1) DESC "
        "LIMIT %s"
    )
    with _connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (top_n,))
            return [_norm_post(r) for r in cur.fetchall()]


def fetch_comments(cfg, post_id: int, limit: int) -> list[dict]:
    """单帖高赞评论（按赞排序，赞可空）。"""
    sql = (
        "SELECT pctext, IFNULL(like_num, 0) AS like_num FROM v_pcomments_public "
        "WHERE ptargetID = %s "
        "ORDER BY IFNULL(like_num, 0) DESC, pcommentID ASC LIMIT %s"
    )
    with _connect(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (post_id, limit))
            return [
                {"text": r.get("pctext") or "", "like_num": _num(r.get("like_num"))}
                for r in cur.fetchall()
            ]
