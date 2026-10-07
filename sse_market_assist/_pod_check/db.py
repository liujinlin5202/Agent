# -*- coding: utf-8 -*-
"""MySQL 连接：主读（红线 4：绝不写现有业务表），仅向 assist_responses 表写入。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pymysql

from app.config import settings

# 仅允许写入的白名单表（防止误写现有业务表）
WRITE_ALLOWED_TABLES = {"assist_responses"}


def get_conn() -> pymysql.connections.Connection:
    """新建短连接（服务为低频检索，不维护连接池）。"""
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
        read_timeout=15,
    )


@contextmanager
def query() -> Iterator[pymysql.cursors.DictCursor]:
    """上下文管理器：用完即关。"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def fetch_all(sql: str, params: Optional[tuple] = None) -> list[dict[str, Any]]:
    """执行一条只读 SELECT 并返回全部行。"""
    with query() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Optional[tuple] = None) -> Optional[dict[str, Any]]:
    with query() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ---------------------------------------------------------------------------
# 写入（仅限白名单表）
# ---------------------------------------------------------------------------

def _check_write_sql(sql: str) -> None:
    """检查 SQL 语句是否只操作白名单表。"""
    upper = sql.upper().strip()
    for table in WRITE_ALLOWED_TABLES:
        if table in sql.lower():
            return
    msg = f"写入被拒绝：SQL 未命中白名单表 {WRITE_ALLOWED_TABLES}：{sql[:80]}"
    raise PermissionError(msg)


def execute_write(sql: str, params: Optional[tuple] = None) -> int:
    """执行 INSERT/UPDATE（仅允许 assist_responses 表）。返回影响行数。"""
    _check_write_sql(sql)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def ensure_assist_responses_table() -> None:
    """确保 assist_responses 表存在（建表幂等）。"""
    sql = """
    CREATE TABLE IF NOT EXISTS assist_responses (
      id INT AUTO_INCREMENT PRIMARY KEY,
      postID INT NOT NULL UNIQUE,
      `partition` VARCHAR(50) NOT NULL,
      title VARCHAR(200) NOT NULL,
      answer TEXT,
      references_json JSON,
      sources_type VARCHAR(20) DEFAULT 'local',
      web_search_query VARCHAR(500) DEFAULT '',
      web_results_json JSON,
      feedback_upvotes INT DEFAULT 0,
      feedback_downvotes INT DEFAULT 0,
      conversation_json JSON,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      INDEX idx_postID (postID)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("[db] 已确保 assist_responses 表存在", flush=True)
    finally:
        conn.close()
